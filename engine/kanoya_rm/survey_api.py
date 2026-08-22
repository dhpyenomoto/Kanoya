"""オンデマンド調査 — 画面の「調査」ボタンから競合の実勢価格を取りに行く.

夜間バッチ（collect_rates.py）とは目的が違う。バッチは全期間を安く広く集める。
こちらは「いま見ている数日だけ、今すぐ最新にしたい」に応える。

同じ収集・正規化コードを通す。ここで別実装を書くと、
バッチとボタンで違う数字が出て、どちらが正しいか誰にも分からなくなる。

**この関数は課金される。** 1宿泊日 = SerpApi 1リクエスト = 約2.3円。
URLさえ知っていれば誰でも押せてしまうため、認証と上限は
「付けたほうがよい」ではなく、動作の前提条件として組み込む。

  ① トークン照合   env RM_SURVEY_TOKEN と一致しなければ実行しない
  ② 期間の上限     1回の要求で取得できる宿泊日数を制限（既定31日）
  ③ 日次の上限     1日に発行できるリクエスト総数を制限（既定200）

②③を通っても、最終的な歯止めは SerpApi 契約プランの検索上限である。
サーバーレスではインスタンスごとにファイルが分かれるため、
③は厳密なグローバル上限にはならない（後述の LEDGER 参照）。
"""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import collect
from .compset import build_snapshot
from .config import Settings
from .schedule import CollectionTask, Plan

SOLD_OUT = "SO"

DEFAULT_MAX_DAYS = 31
DEFAULT_DAILY_MAX = 200


class SurveyDenied(Exception):
    """要求を実行してはいけない。HTTPステータスと日本語の理由を持つ."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---- 入力の検証 ---------------------------------------------------------

def check_token(provided: str | None, expected: str | None, *,
                allow_anonymous: bool = False) -> None:
    """トークンを定数時間で照合する.

    expected が未設定のときは**拒否する**（フェイルクローズ）。
    「キーを入れ忘れたら誰でも叩ける」は、課金される経路では事故そのもの。
    検証用に開けたい場合だけ allow_anonymous を明示する。
    """
    if not expected:
        if allow_anonymous:
            return
        raise SurveyDenied(503, "調査サーバーのトークン（RM_SURVEY_TOKEN）が"
                                "設定されていません。管理者に連絡してください。")
    if not provided:
        raise SurveyDenied(401, "アクセストークンが必要です。")
    if not hmac.compare_digest(provided, expected):
        raise SurveyDenied(401, "アクセストークンが違います。")


def parse_range(raw_from: str, raw_to: str, *, today: date,
                max_days: int = DEFAULT_MAX_DAYS,
                max_lead_days: int = 400) -> list[date]:
    """要求された期間を宿泊日のリストにする.

    過去日は Google Hotels に価格が無く、リクエストを捨てるだけなので弾く。
    """
    try:
        start = date.fromisoformat(raw_from)
        end = date.fromisoformat(raw_to)
    except (TypeError, ValueError):
        raise SurveyDenied(400, "日付は YYYY-MM-DD 形式で指定してください。") from None

    if start > end:
        raise SurveyDenied(400, "開始日が終了日より後になっています。")
    if end < today:
        raise SurveyDenied(400, "過去の宿泊日は取得できません。")
    start = max(start, today)

    span = (end - start).days + 1
    if span > max_days:
        raise SurveyDenied(
            400, f"一度に調査できるのは{max_days}日までです"
                 f"（{span}日が要求されました）。期間を分けてください。")
    if (end - today).days > max_lead_days:
        raise SurveyDenied(400, f"{max_lead_days}日より先の宿泊日は調査できません。")
    return [start + timedelta(days=i) for i in range(span)]


# ---- 日次上限 -----------------------------------------------------------

@dataclass
class Ledger:
    """その日に発行したリクエスト数を数える.

    サーバーレスでは実行インスタンスごとにファイルシステムが分かれるため、
    これは**厳密なグローバル上限ではない**（インスタンスが3つ立てば
    最悪3倍まで通る）。それでも暴走ループや連打による青天井は止まる。
    厳密に締めたい場合は SerpApi 側のプラン上限か、外部KVを使うこと。
    """

    path: Path
    daily_max: int = DEFAULT_DAILY_MAX

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def spent_today(self, today: date) -> int:
        data = self._read()
        return int(data.get(today.isoformat(), 0))

    def reserve(self, count: int, today: date) -> None:
        spent = self.spent_today(today)
        if spent + count > self.daily_max:
            raise SurveyDenied(
                429, f"本日の調査上限（{self.daily_max}リクエスト）に達しました。"
                     f"本日の使用 {spent}／要求 {count}。明日再度お試しください。")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 当日分だけ残す。古い日付を溜めても使い道がない
            self.path.write_text(
                json.dumps({today.isoformat(): spent + count}), encoding="utf-8")
        except OSError:
            pass          # 計数できなくても調査自体は続行する


# ---- 調査の実行 ---------------------------------------------------------

@dataclass
class SurveyResult:
    as_of: date
    dates: list[date]
    cells: dict[str, dict[str, float | str | None]] = field(default_factory=dict)
    median: dict[str, float] = field(default_factory=dict)
    pressure: dict[str, float] = field(default_factory=dict)
    sample: dict[str, int] = field(default_factory=dict)
    soldout_ratio: dict[str, float] = field(default_factory=dict)
    requests: int = 0
    fixture: bool = False
    unmatched: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "asOf": self.as_of.isoformat(),
            "dates": [d.isoformat() for d in self.dates],
            "cells": self.cells,
            "median": self.median,
            "pressure": self.pressure,
            "sample": self.sample,
            "soldoutRatio": self.soldout_ratio,
            "requests": self.requests,
            "fixture": self.fixture,
            "unmatched": self.unmatched,
        }


def run_survey(settings: Settings, rate_source, dates: list[date], *,
               as_of: date, area_query: str, adults: int = 2,
               los: int = 1) -> SurveyResult:
    """指定された宿泊日について競合価格を取得し、NAR正規化して返す.

    バッチと同じ collect.run / build_snapshot を通す。
    ここを別実装にすると、ボタンと夜間バッチで違う数字が出る。
    """
    plan = Plan(
        run_date=as_of,
        tasks=[CollectionTask(stay_date=d, lead_days=(d - as_of).days,
                              tier="ondemand", reason="ondemand")
               for d in dates],
        horizon_days=(dates[-1] - as_of).days if dates else 0,
    )
    outcome = collect.run(plan, rate_source, settings.competitors,
                          area_query=area_query, adults=adults, los=los)

    by_stay: dict[str, list[dict]] = {}
    for row in outcome.rows:
        by_stay.setdefault(row["stay_date"], []).append(row)

    result = SurveyResult(as_of=as_of, dates=dates,
                          requests=len(plan.tasks), fixture=outcome.fixture,
                          unmatched=sorted({r.property_name for r in outcome.unmatched}))
    for comp_id in settings.competitors:
        result.cells[comp_id] = {}

    for day in dates:
        iso = day.isoformat()
        rows = by_stay.get(iso, [])
        # build_snapshot は文字列キーのCSV行を前提とする。型を合わせる
        snap = build_snapshot(settings, day,
                              [{**r, "raw_rate": str(r["raw_rate"]),
                                "available": str(r["available"])} for r in rows])
        priced = {cid: (nar, avail) for cid, nar, avail in snap.detail}
        for comp_id in settings.competitors:
            if comp_id not in priced:
                result.cells[comp_id][iso] = None          # データなし
            elif not priced[comp_id][1]:
                result.cells[comp_id][iso] = SOLD_OUT      # 売止
            else:
                result.cells[comp_id][iso] = round(priced[comp_id][0])
        result.median[iso] = round(snap.weighted_median_nar)
        result.pressure[iso] = round(snap.pressure, 3)
        result.sample[iso] = snap.sample_size
        result.soldout_ratio[iso] = round(snap.soldout_ratio, 3)

    return result


# ---- 環境からの組み立て -------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Config:
    """環境変数から読む運用パラメータ（コードにも設定ファイルにも書かない）."""

    token: str | None
    allow_anonymous: bool
    source_name: str
    max_days: int
    daily_max: int
    ledger_path: Path
    allowed_origins: list[str]

    @classmethod
    def from_env(cls) -> "Config":
        origins = [o.strip() for o in
                   (os.environ.get("RM_SURVEY_ALLOWED_ORIGINS") or "").split(",")
                   if o.strip()]
        return cls(
            token=os.environ.get("RM_SURVEY_TOKEN") or None,
            allow_anonymous=os.environ.get("RM_SURVEY_ALLOW_ANONYMOUS") == "1",
            source_name=(os.environ.get("RM_SURVEY_SOURCE") or "serpapi").lower(),
            max_days=_env_int("RM_SURVEY_MAX_DAYS", DEFAULT_MAX_DAYS),
            daily_max=_env_int("RM_SURVEY_DAILY_MAX", DEFAULT_DAILY_MAX),
            ledger_path=Path(os.environ.get("RM_SURVEY_LEDGER")
                             or "/tmp/kanoya-survey-ledger.json"),
            allowed_origins=origins,
        )


def build_source(root: Path, config: Config, sources_config: dict):
    """データ源を組み立てる.

    fixture を選べるようにしてあるのは、APIキーが無い段階でも
    ボタンからサーバーまでの経路を丸ごと検証できるようにするため。
    """
    if config.source_name == "fixture":
        from .sources.fixture import FixtureRatesSource
        return FixtureRatesSource(root / "data" / "fixtures"), "フィクスチャ（擬似データ）"

    from .sources.http import HttpClient, RateLimiter
    from .sources.serpapi_hotels import SerpApiHotelsSource
    limits = sources_config.get("rate_limits", {})
    client = HttpClient(
        source="serpapi_google_hotels",
        # サーバーレスでは書ける場所が /tmp だけ。生データは実行後に消える
        raw_dir=Path(os.environ.get("RM_SURVEY_RAW_DIR") or "/tmp/kanoya-raw"),
        rate_limiter=RateLimiter(
            per_second=float(limits.get("serpapi_per_second", 2.0))),
    )
    currency = sources_config.get("collection", {}).get("currency", "JPY")
    return SerpApiHotelsSource(client, currency=currency), "SerpApi google_hotels"


def handle(body: dict, *, root: Path, config: Config,
           today: date | None = None) -> dict:
    """HTTPの外側から呼ばれる本体。例外は SurveyDenied に統一する."""
    today = today or datetime.now(timezone.utc).astimezone().date()

    check_token(body.get("token"), config.token,
                allow_anonymous=config.allow_anonymous)

    dates = parse_range(body.get("from", ""), body.get("to", ""),
                        today=today, max_days=config.max_days)

    settings = Settings.load(root / "config")
    sources_config = json.loads(
        (root / "config" / "sources.json").read_text(encoding="utf-8"))
    collection = sources_config.get("collection", {})

    Ledger(config.ledger_path, config.daily_max).reserve(len(dates), today)

    source, label = build_source(root, config, sources_config)
    try:
        result = run_survey(
            settings, source, dates, as_of=today,
            area_query=collection.get("area_query", ""),
            adults=int(collection.get("adults", 2)),
            los=int(collection.get("los", 1)),
        )
    except Exception as exc:                     # 収集経路の失敗
        # 例外文字列にAPIキーが混ざる経路があり得るため、そのまま返さない
        raise SurveyDenied(502, f"競合データの取得に失敗しました（{type(exc).__name__}）。"
                                f"時間をおいて再試行してください。") from exc

    cost = sources_config.get("budget", {}).get("serpapi_cost_per_search_usd", 0.0)
    payload = result.to_json()
    payload["source"] = label
    payload["costUsd"] = round(result.requests * float(cost), 4)
    return payload
