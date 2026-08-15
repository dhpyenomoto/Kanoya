"""収集スケジューリング — 「価格が動く場所にだけリクエストを割り当てる」.

競合8施設 × 120日先を毎日全数取得すると月28,800リクエストになる。
リードタイム帯ごとに更新頻度を変え、週末・イベント日だけ頻度を引き上げることで、
カバレッジをほぼ落とさずにリクエスト数を数分の1へ圧縮する。

スロット方式:
  各宿泊日に安定したスロット番号 (stay_date.toordinal() % N) を割り当て、
  run_date.toordinal() % N == slot の日に取得する。
  これにより ①各宿泊日はきっかり N 日ごとに更新され、
  ②負荷が日々に均等分散される（特定の日に集中しない）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .config import Settings


@dataclass
class CollectionTask:
    stay_date: date
    lead_days: int
    tier: str
    reason: str


@dataclass
class Plan:
    run_date: date
    tasks: list[CollectionTask]
    horizon_days: int

    @property
    def request_count(self) -> int:
        """エリア一括検索は 1宿泊日あたり1リクエスト."""
        return len(self.tasks)

    def by_tier(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.tasks:
            out[t.tier] = out.get(t.tier, 0) + 1
        return out


def _tier_for(lead: int, tiers: list[dict]) -> dict | None:
    for tier in tiers:
        if int(tier["min_lead"]) <= lead <= int(tier["max_lead"]):
            return tier
    return None


def _step_up(every: int, tiers: list[dict], steps: int) -> int:
    """階層の頻度リスト上で steps 段階だけ高頻度側へ移す."""
    ladder = sorted({int(t["every_n_days"]) for t in tiers})
    if every not in ladder:
        return every
    return ladder[max(0, ladder.index(every) - steps)]


def build_plan(sources_config: dict, settings: Settings, run_date: date,
               horizon_days: int | None = None) -> Plan:
    collection = sources_config["collection"]
    tiers = collection["tiers"]
    boost = collection.get("priority_boost", {})
    horizon = horizon_days or int(collection["horizon_days"])

    tasks: list[CollectionTask] = []
    for lead in range(horizon + 1):
        stay = run_date + timedelta(days=lead)
        tier = _tier_for(lead, tiers)
        if tier is None:
            continue

        every = int(tier["every_n_days"])
        reason = tier["name"]

        # 週末・イベント日は「1段階上の階層」の頻度へ引き上げる。
        # 遠い日付は繁忙日でも日々は動かないため、リードタイム上限で歯止めをかける。
        dow = settings.dow_of(stay)
        is_weekend = dow in ("FRI", "SAT") or settings.is_holiday_eve(stay)
        event_score, event_label = settings.event_score_of(stay)
        step = int(boost.get("step_up", 1))

        if is_weekend and lead <= int(boost.get("weekend_max_lead", 10**9)):
            stepped = _step_up(every, tiers, step)
            if stepped < every:
                every, reason = stepped, f"{tier['name']}+週末"
        if (event_score >= float(boost.get("event_min_score", 0.5))
                and lead <= int(boost.get("event_max_lead", 10**9))):
            stepped = _step_up(every, tiers, step)
            if stepped < every:
                every, reason = stepped, f"{tier['name']}+{event_label or 'イベント'}"

        if every <= 1 or (stay.toordinal() % every) == (run_date.toordinal() % every):
            tasks.append(CollectionTask(stay, lead, tier["name"], reason))

    return Plan(run_date=run_date, tasks=tasks, horizon_days=horizon)


def estimate_monthly(sources_config: dict, settings: Settings, start: date,
                     days: int = 30, competitor_count: int = 1) -> dict:
    """1ヶ月分の収集計画をシミュレートし、リクエスト数と費用を見積もる.

    3つの水準を比較する（削減効果を過大に見せないため）:
      per_property  施設ごとに個別取得（最も素朴）
      area_daily    エリア一括だが全宿泊日を毎日取得
      tiered        エリア一括 ＋ 階層化スケジュール（本設計）
    """
    budget = sources_config["budget"]
    horizon = int(sources_config["collection"]["horizon_days"])

    total = 0
    per_tier: dict[str, int] = {}
    for offset in range(days):
        plan = build_plan(sources_config, settings, start + timedelta(days=offset))
        total += plan.request_count
        for tier, count in plan.by_tier().items():
            per_tier[tier] = per_tier.get(tier, 0) + count

    area_daily = horizon * days
    per_property = area_daily * max(1, competitor_count)
    unit = float(budget["serpapi_cost_per_search_usd"])
    cap = int(budget["monthly_search_cap"])

    return {
        "days": days,
        "requests": total,
        "requests_area_daily": area_daily,
        "requests_per_property": per_property,
        "compression_vs_area": (area_daily / total) if total else 0.0,
        "compression_vs_per_property": (per_property / total) if total else 0.0,
        "per_tier": per_tier,
        "cost_usd": total * unit,
        "cost_usd_area_daily": area_daily * unit,
        "cost_usd_per_property": per_property * unit,
        "monthly_cap": cap,
        "within_cap": total <= cap,
    }
