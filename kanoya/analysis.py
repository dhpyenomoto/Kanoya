"""スナップショット履歴から、ダッシュボード 1 枚分の分析結果を組み立てる。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .calibrate import Calibrated, calibrate, load_actuals
from .config import Config
from .decide import Signal, Verdict, build_signals, build_verdict
from .estimate import Estimate, estimate_property
from .series import ReviewSeries, area_rolling, build_series
from .store import SnapshotStore


@dataclass(frozen=True)
class Analysis:
    cfg: Config
    as_of: date
    window_start: date
    window_end: date
    ribbon_start: date
    ribbon_end: date
    estimates: list[Estimate]
    calibration: Calibrated
    verdict: Verdict
    signals: list[Signal]
    area_rolling: list[float]
    own_rolling: list[float]
    area_reviews: float
    own_reviews: float
    own_share: float | None
    prev_own_share: float | None
    own_rating: float | None
    own_actual_occupancy: float | None
    own_model_occupancy: float | None
    actuals_coverage: float
    stale_days: int | None


def _share(own: float, area: float) -> float | None:
    return (own / area) if area > 0 else None


def _holdout_check(
    actuals, series: ReviewSeries, start: date, end: date, rooms: int, rate: float
) -> tuple[float | None, float | None, float]:
    """窓内の PMS 実績と、同じ日付集合でのモデル推定を突き合わせる。

    投稿率は窓より前の期間だけで作ってあるので、これは真のホールドアウト。
    ここがずれていれば、同じ係数で出している競合の推定も同じだけずれている。
    """
    days = (end - start).days + 1
    rows = [
        a for a in actuals if start <= a.stay_date <= end and a.stay_date in series.covered
    ]
    coverage = len(rows) / days if days else 0.0
    if len(rows) < 30:
        return (None, None, coverage)
    capacity = rooms * len(rows)
    actual_occ = sum(a.rooms_sold for a in rows) / capacity
    model_reviews = sum(series.daily.get(a.stay_date, 0.0) for a in rows)
    model_occ = min((model_reviews / rate) / capacity, 1.0)
    return (actual_occ, model_occ, coverage)


def build_analysis(cfg: Config, store: SnapshotStore, as_of: date | None = None) -> Analysis:
    est_cfg = cfg.estimation
    as_of = as_of or date.today()

    # 投稿遅延の分だけ手前で切る。直近 lag 日はまだ投稿が出揃っていない。
    window_end = as_of - timedelta(days=est_cfg.review_lag_days)
    window_start = window_end - timedelta(days=est_cfg.window_days - 1)
    prev_end = window_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=est_cfg.window_days - 1)
    ribbon_end = window_end
    ribbon_start = ribbon_end - timedelta(days=est_cfg.ribbon_days - 1)

    series: dict[str, ReviewSeries] = {}
    latest: dict[str, object] = {}
    for prop in cfg.properties:
        snaps = store.history(prop.key)
        series[prop.key] = build_series(
            snaps,
            property_key=prop.key,
            review_lag_days=est_cfg.review_lag_days,
            restaurant_review_share=prop.restaurant_review_share,
        )
        latest[prop.key] = store.latest(prop.key)

    own_series = series[cfg.own.key]

    actuals_path = cfg.resolve(cfg.calibration.actuals_csv)
    actuals = load_actuals(actuals_path) if actuals_path.exists() else []
    calibration = calibrate(
        own_series,
        actuals,
        rooms=cfg.own.rooms,
        default_rate=est_cfg.default_review_rate,
        bucket_days=cfg.calibration.backtest_bucket_days,
        usable_mae_pt=cfg.calibration.usable_mae_pt,
        relative_only_mae_pt=cfg.calibration.relative_only_mae_pt,
        train_before=window_start,
    )

    estimates = [
        estimate_property(
            prop,
            series[prop.key],
            window_end=window_end,
            window_days=est_cfg.window_days,
            review_rate=calibration.rate,
            calibration_measured=calibration.measured,
            rating=getattr(latest[prop.key], "rating", None),
            review_count=getattr(latest[prop.key], "user_rating_count", None),
            high_band_pt=est_cfg.confidence_high_band_pt,
            medium_band_pt=est_cfg.confidence_medium_band_pt,
        )
        for prop in cfg.properties
    ]
    estimates.sort(key=lambda e: -e.occupancy)

    all_series = list(series.values())
    area_roll = area_rolling(all_series, ribbon_start, ribbon_end, est_cfg.rolling_days)
    own_roll = own_series.rolling(ribbon_start, ribbon_end, est_cfg.rolling_days)

    area_reviews = sum(s.total(window_start, window_end) for s in all_series)
    own_reviews = own_series.total(window_start, window_end)
    prev_area = sum(s.total(prev_start, prev_end) for s in all_series)
    prev_own = own_series.total(prev_start, prev_end)

    last_dates = [s.taken_on for s in (latest[p.key] for p in cfg.properties) if s]  # type: ignore[union-attr]
    stale_days = (as_of - max(last_dates)).days if last_dates else None
    own_latest = latest[cfg.own.key]
    own_rating = getattr(own_latest, "rating", None)

    own_share = _share(own_reviews, area_reviews)
    prev_own_share = _share(prev_own, prev_area)

    own_actual_occ, own_model_occ, actuals_coverage = _holdout_check(
        actuals, own_series, window_start, window_end, cfg.own.rooms, calibration.rate
    )

    verdict = build_verdict(estimates, cfg)
    signals = build_signals(
        estimates,
        cfg,
        as_of=as_of,
        own_actual_occupancy=own_actual_occ,
        own_model_occupancy=own_model_occ,
        own_share=own_share,
        prev_own_share=prev_own_share,
        stale_days=stale_days,
        calibration_measured=calibration.measured,
    )

    return Analysis(
        cfg=cfg,
        as_of=as_of,
        window_start=window_start,
        window_end=window_end,
        ribbon_start=ribbon_start,
        ribbon_end=ribbon_end,
        estimates=estimates,
        calibration=calibration,
        verdict=verdict,
        signals=signals,
        area_rolling=area_roll,
        own_rolling=own_roll,
        area_reviews=area_reviews,
        own_reviews=own_reviews,
        own_share=own_share,
        prev_own_share=prev_own_share,
        own_rating=own_rating,
        own_actual_occupancy=own_actual_occ,
        own_model_occupancy=own_model_occ,
        actuals_coverage=actuals_coverage,
        stale_days=stale_days,
    )
