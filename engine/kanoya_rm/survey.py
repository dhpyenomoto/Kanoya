"""マーケットサーベイ — 収集した競合価格から ADR 判断の材料を組み立てる.

収集した生価格をそのまま眺めても判断はできない。ここで行うのは、

  1. NAR正規化後の競合価格分布（p25 / 中央値 / p75）に自社を位置づける
  2. 「価格が安いから需要がある」のか「売止だから見えないだけ」なのかを分ける
  3. 価格・在庫・予約ペースの3つが揃った日にだけ、強い判断シグナルを出す

3つ揃わない日は HOLD にする。単一シグナルでの値動きは、
5室規模では1件の予約で簡単に裏切られるため、意図的に慎重側へ倒している。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .compset import CompSnapshot
from .config import Settings

SIGNAL_LABEL = {
    "RAISE": "値上げ余地",
    "HOLD": "維持",
    "LOWER": "値下げ検討",
    "DATA_THIN": "判断不可（データ不足）",
}

# 競合レートがこの日数より古い日は、判断材料として弱いものとして扱う
STALE_DAYS = 10


@dataclass
class DayPosition:
    stay_date: date
    dow: str
    season_label: str
    day_class: str
    lead_days: int

    comp_n: int = 0
    comp_p25: float = 0.0
    comp_median: float = 0.0
    comp_p75: float = 0.0
    soldout_ratio: float = 0.0
    pressure: float = 0.0

    our_current: float = 0.0
    our_recommended: float = 0.0
    position: float = 0.0        # 自社現行 ÷ 競合NAR中央値
    percentile: float = 0.0      # 競合分布内での自社の位置（0..1）
    remaining: int = 0
    pace_z: float = 0.0

    signal: str = "HOLD"
    reasons: list[str] = field(default_factory=list)
    opportunity_yen: float = 0.0
    data_age_days: int = 0
    conflict: bool = False      # 市場ポジション判定とエンジン推奨が逆を向いている


@dataclass
class SurveyReport:
    property_name: str
    run_date: date
    days: list[DayPosition]
    comp_names: dict[str, str]
    fixture: bool
    thin_days: int
    total_opportunity: float
    avg_position: float
    needs_review: dict[str, list[str]]
    stale_days: int = 0
    max_data_age: int = 0


def _percentile_of(value: float, sample: list[float]) -> float:
    if not sample or value <= 0:
        return 0.0
    below = sum(1 for v in sample if v < value)
    return below / len(sample)


def _classify(pos: DayPosition, target_position: float,
              raise_pressure: float, lower_pressure: float) -> None:
    """3シグナル（価格・在庫・ペース）の合議で判断を出す."""
    if pos.comp_n < 3:
        pos.signal = "DATA_THIN"
        pos.reasons.append(f"有効サンプル{pos.comp_n}件（3件未満）")
        return

    price_low = pos.position > 0 and pos.position < target_position
    price_high = pos.position >= target_position * 1.15
    market_tight = pos.soldout_ratio >= raise_pressure
    market_soft = pos.soldout_ratio <= lower_pressure
    pace_ahead = pos.pace_z > 0.1
    pace_behind = pos.pace_z < -0.3

    raise_votes = sum([price_low, market_tight, pace_ahead])
    lower_votes = sum([price_high, market_soft, pace_behind])

    if raise_votes >= 2 and pos.remaining > 0:
        pos.signal = "RAISE"
        if price_low:
            pos.reasons.append(f"市場中央値の{pos.position:.2f}倍（目標{target_position:.2f}倍を下回る）")
        if market_tight:
            pos.reasons.append(f"競合売止率{pos.soldout_ratio:.0%}")
        if pace_ahead:
            pos.reasons.append("予約ペースが前倒し")
    elif lower_votes >= 2:
        pos.signal = "LOWER"
        if price_high:
            pos.reasons.append(f"市場中央値の{pos.position:.2f}倍（割高）")
        if market_soft:
            pos.reasons.append(f"競合売止率{pos.soldout_ratio:.0%}（市場に余裕）")
        if pace_behind:
            pos.reasons.append("予約ペースが遅れ")
    else:
        pos.signal = "HOLD"
        pos.reasons.append(f"シグナル不一致（上げ{raise_votes}/下げ{lower_votes}）")


def build(settings: Settings, ctx, *, target_position: float = 1.15,
          raise_pressure: float = 0.40, lower_pressure: float = 0.15,
          fixture: bool = False,
          window: tuple[date, date] | None = None) -> SurveyReport:
    days: list[DayPosition] = []
    rooms = int(settings.property["property"]["rooms"])
    season_occ = {"PEAK": .95, "HIGH": .88, "SHOULDER": .75, "LOW": .62, "DEEP_LOW": .50}

    for stay in sorted(ctx.recommendations):
        if window is not None and not (window[0] <= stay <= window[1]):
            continue
        rec = ctx.recommendations[stay]
        snap: CompSnapshot | None = ctx.comp_snapshots.get(stay)
        pace = ctx.paces.get(stay)

        pos = DayPosition(
            stay_date=stay,
            dow=settings.dow_of(stay),
            season_label=rec.season_label,
            day_class=rec.day_class,
            lead_days=rec.lead_days,
            our_current=rec.current_rate,
            our_recommended=rec.recommended_rate,
            remaining=rec.remaining,
            pace_z=getattr(pace, "z", 0.0),
        )

        if snap is not None:
            sample = [nar for _, nar, ok in snap.detail if ok and nar > 0]
            pos.comp_n = snap.sample_size
            pos.comp_p25 = snap.p25_nar
            pos.comp_median = snap.weighted_median_nar
            pos.comp_p75 = snap.p75_nar
            pos.soldout_ratio = snap.soldout_ratio
            pos.pressure = snap.pressure
            pos.percentile = _percentile_of(rec.current_rate, sample)
            if snap.weighted_median_nar > 0:
                pos.position = rec.current_rate / snap.weighted_median_nar

        pos.data_age_days = ctx.data_age.get(stay, 0)
        _classify(pos, target_position, raise_pressure, lower_pressure)

        # 市場ポジション判定とエンジン推奨が逆を向いている日は、黙って数字を並べない。
        # 「市場比では割高だが、需要イベントと予約ペースは値上げを支持する」
        # といった状況であり、人が見るべき例外そのものである。
        if pos.signal == "LOWER" and pos.our_recommended > pos.our_current * 1.02:
            pos.conflict = True
        elif pos.signal == "RAISE" and pos.our_recommended < pos.our_current * 0.98:
            pos.conflict = True

        if pos.signal == "RAISE" and pos.our_recommended > pos.our_current:
            season, _ = settings.season_of(stay)
            expected_rooms = rooms * season_occ.get(season, 0.75)
            pos.opportunity_yen = (pos.our_recommended - pos.our_current) * expected_rooms

        days.append(pos)

    positions = [d.position for d in days if d.position > 0]
    needs_review = {
        c.name: c.needs_review for c in settings.competitors.values() if c.needs_review
    }

    return SurveyReport(
        property_name=settings.property["property"]["name"],
        run_date=ctx.snapshot,
        days=days,
        comp_names={cid: c.name for cid, c in settings.competitors.items()},
        fixture=fixture,
        thin_days=sum(1 for d in days if d.signal == "DATA_THIN"),
        total_opportunity=sum(d.opportunity_yen for d in days),
        avg_position=(sum(positions) / len(positions)) if positions else 0.0,
        needs_review=needs_review,
        stale_days=sum(1 for d in days if d.comp_n and d.data_age_days > STALE_DAYS),
        max_data_age=max((d.data_age_days for d in days if d.comp_n), default=0),
    )


# ---- 出力 ---------------------------------------------------------------


def write_csv(report: SurveyReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "宿泊日", "曜日", "シーズン", "日カテゴリ", "リードタイム",
            "競合サンプル数", "競合P25", "競合中央値", "競合P75", "競合売止率",
            "自社現行", "自社推奨", "対中央値", "分布内位置", "残室", "ペースz",
            "市場判定", "根拠", "推定機会(円)", "データ経過日数", "推奨と不一致",
        ])
        for d in report.days:
            w.writerow([
                d.stay_date.isoformat(), d.dow, d.season_label, d.day_class, d.lead_days,
                d.comp_n, round(d.comp_p25), round(d.comp_median), round(d.comp_p75),
                f"{d.soldout_ratio:.0%}",
                round(d.our_current), round(d.our_recommended),
                f"{d.position:.2f}" if d.position else "",
                f"{d.percentile:.0%}" if d.comp_n else "",
                d.remaining, f"{d.pace_z:+.2f}",
                SIGNAL_LABEL[d.signal], " / ".join(d.reasons), round(d.opportunity_yen),
                d.data_age_days, "★" if d.conflict else "",
            ])


def render(report: SurveyReport, *, top: int = 12,
           request_panel: str | None = None) -> str:
    lines: list[str] = []
    bar = "=" * 78
    lines.append(bar)
    lines.append(f"  {report.property_name} — マーケットポジション調査 / ADR判断材料")
    lines.append(bar)
    if request_panel:
        lines.append(request_panel)
    else:
        lines.append(f"  基準日 {report.run_date.isoformat()} ／ 対象 {len(report.days)}日")

    if report.fixture:
        lines.append("")
        lines.append("  ██ 警告: フィクスチャ（擬似）データです。実勢価格ではありません。 ██")
        lines.append("     実データで判断するには APIキーを設定し --source serpapi で再実行してください。")

    counts: dict[str, int] = {}
    for d in report.days:
        counts[d.signal] = counts.get(d.signal, 0) + 1

    lines.append("")
    lines.append("── 総括 " + "─" * 68)
    lines.append(f"  市場に対する自社の平均ポジション : {report.avg_position:.2f} 倍（競合NAR中央値比）")
    lines.append("  判断内訳                         : " + " ／ ".join(
        f"{SIGNAL_LABEL[k]} {v}日" for k, v in sorted(counts.items())
    ))
    lines.append(f"  推定機会（値上げ余地日の合計）   : {report.total_opportunity:,.0f} 円")
    lines.append(f"  競合レートの最大経過日数         : {report.max_data_age} 日"
                 + (f"（{STALE_DAYS}日超が {report.stale_days} 日）" if report.stale_days else ""))
    if report.thin_days:
        lines.append(f"  ※ {report.thin_days}日はサンプル不足で判断不可。収集カバレッジの改善が必要。")

    conflicts = [d for d in report.days if d.conflict]
    if conflicts:
        conflicts.sort(key=lambda d: -abs(d.our_recommended - d.our_current))
        lines.append("")
        lines.append("── ★要確認: 市場判定とエンジン推奨が不一致 " + "─" * 34)
        lines.append("   市場比では割高／割安だが、需要イベントや予約ペースが逆を支持している日。")
        lines.append("   自動配信せず、人が判断すべき例外。")
        lines.append(f"  {'宿泊日':<12}{'曜':<4}{'現行':>9}{'推奨':>9}{'市場判定':>12}  内訳")
        for d in conflicts[:10]:
            lines.append(
                f"  {d.stay_date.isoformat():<12}{d.dow:<4}"
                f"{d.our_current:>9,.0f}{d.our_recommended:>9,.0f}"
                f"{SIGNAL_LABEL[d.signal]:>12}  対中央値{d.position:.2f} / {' / '.join(d.reasons)}"
            )

    for signal in ("RAISE", "LOWER"):
        subset = [d for d in report.days if d.signal == signal and not d.conflict]
        if not subset:
            continue
        subset.sort(key=lambda d: -abs(d.our_recommended - d.our_current))
        lines.append("")
        lines.append(f"── {SIGNAL_LABEL[signal]} 上位{min(top, len(subset))}日 " + "─" * 50)
        lines.append(f"  {'宿泊日':<12}{'曜':<4}{'現行':>9}{'推奨':>9}{'中央値':>10}{'対中央値':>9}{'売止':>7}  根拠")
        for d in subset[:top]:
            lines.append(
                f"  {d.stay_date.isoformat():<12}{d.dow:<4}"
                f"{d.our_current:>9,.0f}{d.our_recommended:>9,.0f}"
                f"{d.comp_median:>10,.0f}{d.position:>9.2f}{d.soldout_ratio:>7.0%}"
                f"  {' / '.join(d.reasons)}"
            )

    if report.needs_review:
        lines.append("")
        lines.append("── 要人手確認（この確定なしに本番判断してはいけない） " + "─" * 24)
        for name, flags in list(report.needs_review.items())[:12]:
            lines.append(f"  {name}: {', '.join(flags)}")

    return "\n".join(lines)
