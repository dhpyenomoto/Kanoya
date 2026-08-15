"""レポート組み立て。収集済みデータ → Report。描画は render.py が担当する。"""

from __future__ import annotations

from datetime import date, timedelta

from . import calibrate, occupancy, signals, verdict
from .config import Config
from .models import AreaShare, Report, RibbonSeries
from .store import Store
from .timeline import build_timelines, daterange, rolling_sum

ROLLING_WINDOW = 7


def resolve_as_of(config: Config, store: Store, today: date) -> date:
    """判断に使ってよい最終日。

    レビューは滞在から平均 lag 日遅れて投稿される。滞在日に巻き戻した系列の
    末尾 lag 日ぶんは、まだ投稿されていない滞在を取りこぼしており必ず過小に出る。
    したがって as_of は「最終取得日 − lag」に置く。
    """
    latest = store.latest_capture_date() or today
    return min(latest, today) - timedelta(days=config.estimation.review_lag_days)


def build_ribbon(config: Config, timelines, as_of: date) -> RibbonSeries:
    start = as_of - timedelta(days=config.estimation.ribbon_days)
    padded_start = start - timedelta(days=ROLLING_WINDOW - 1)
    days = daterange(padded_start, as_of)
    keep = len(days) - (ROLLING_WINDOW - 1)

    area_daily = [
        sum(t.raw_by_stay.get(day, 0.0) for t in timelines.values()) for day in days
    ]
    own_timeline = timelines.get(config.own.key)
    own_daily = [
        own_timeline.raw_by_stay.get(day, 0.0) if own_timeline else 0.0 for day in days
    ]

    return RibbonSeries(
        dates=days[-keep:],
        area=rolling_sum(area_daily, ROLLING_WINDOW)[-keep:],
        own=rolling_sum(own_daily, ROLLING_WINDOW)[-keep:],
        events=list(config.events),
    )


def build_report(config: Config, store: Store, today: date | None = None) -> Report:
    today = today or date.today()
    as_of = resolve_as_of(config, store, today)

    timelines = build_timelines(
        store, config.properties, config.estimation.review_lag_days
    )

    actuals = calibrate.load_pms_actuals(config.pms_actuals_path)
    cal_start, cal_end = calibrate.calibration_span(
        as_of, config.estimation.calibration_days, actuals
    )
    own_timeline = timelines[config.own.key]
    rate = calibrate.measure_posting_rate(own_timeline, actuals, cal_start, cal_end)
    accuracy = calibrate.backtest(
        config, own_timeline, actuals, rate, cal_start, cal_end
    )

    estimates = occupancy.estimate_all(config, timelines, rate, as_of)
    own = next(e for e in estimates if e.property.is_own)

    share = occupancy.area_share(config, timelines, as_of)
    prev_as_of = as_of - timedelta(days=config.estimation.window_days)
    prev_share: AreaShare = occupancy.area_share(config, timelines, prev_as_of)
    prev_share_pct = prev_share.share_pct if prev_share.total_raw_reviews else None

    ribbon = build_ribbon(config, timelines, as_of)

    median = _median(
        [e.occupancy for e in estimates if not e.property.is_own]
    )
    decision = verdict.decide(config, own, median, accuracy)
    flags = signals.evaluate(
        config, own, estimates, share, prev_share_pct, accuracy, as_of, today
    )

    return Report(
        as_of=as_of,
        generated_on=today,
        window_days=config.estimation.window_days,
        review_lag_days=config.estimation.review_lag_days,
        area_label=config.area_label,
        own=own,
        estimates=estimates,
        posting_rate=rate,
        backtest=accuracy,
        share=share,
        ribbon=ribbon,
        signals=flags,
        verdict=decision,
        rating_floor=config.pricing.rating_floor,
        events=list(config.events),
    )


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0
