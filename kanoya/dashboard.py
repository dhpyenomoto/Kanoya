"""ダッシュボード 1 枚分のデータを組み立てる。

蓄積庫と PMS 実績を入力に、投稿率の較正 → 推定 → 判断 → 描画データ、の順で
確定させる。この順序が依存関係そのもので、較正が失敗すれば以降は成立しない。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from .calibration import Backtest, PostingRate, backtest, estimate_posting_rate, load_pms_actuals
from .config import Config
from .estimate import Estimate, Window, estimate_all, windows
from .ribbon import Ribbon
from .series import combine, coverage, date_range, lodging_series, rolling_sum, window_sum
from .signals import Signal, Verdict, build_signals, build_verdict
from .store import SnapshotStore

ROLLING_DAYS = 7
RECENT_DAYS = 14
#: 期間どうしを比べるシグナルは、両方の期間がこの割合以上観測されているときだけ出す。
MIN_COMPARABLE_COVERAGE = 0.9


@dataclass(frozen=True)
class Dashboard:
    config: Config
    as_of: dt.date
    current: Window
    prior: Window
    estimates: list[Estimate]
    rate: PostingRate
    backtest: Backtest
    verdict: Verdict
    signals: list[Signal]
    ribbon: Ribbon
    area_reviews: float
    own_reviews: float
    own_rating: float | None

    @property
    def short_name(self) -> str:
        """見出し以外で使う短い呼び名。「奈良春日 鹿のや」→「鹿のや」。"""
        return self.config.own.name.split()[-1]

    @property
    def own(self) -> Estimate:
        for est in self.estimates:
            if est.prop.is_own:
                return est
        raise ValueError("自社の推定がない")


def _pct_change(current: float, prior: float) -> float | None:
    if prior <= 0:
        return None
    return (current / prior - 1.0) * 100.0


def build(
    config: Config,
    store_path: str | Path,
    pms_path: str | Path,
    as_of: dt.date,
) -> Dashboard:
    est_cfg = config.estimation
    store = SnapshotStore(store_path)
    current, prior = windows(as_of, est_cfg.window_days, est_cfg.review_lag_days)

    series_by_key = {
        prop.key: lodging_series(prop, store.series(prop.place_id), est_cfg.review_lag_days)
        for prop in config.properties
    }

    own_series = series_by_key[config.own.key]
    pms_sold = load_pms_actuals(pms_path)

    # 較正窓は現行窓の右端で終える。将来のレビューを投稿率の算定に混ぜない。
    cal_end = current.end
    cal_start = cal_end - dt.timedelta(days=est_cfg.calibration_days - 1)
    rate = estimate_posting_rate(own_series, pms_sold, cal_start, cal_end)
    checks = backtest(
        own_series,
        pms_sold,
        cal_start,
        cal_end,
        config.own.rooms,
        est_cfg.backtest_blocks,
        est_cfg.window_days,
    )

    estimates = estimate_all(config, series_by_key, rate, current, prior)

    area_series = combine(list(series_by_key.values()))
    area_current = window_sum(area_series, current.start, current.end)
    area_prior = window_sum(area_series, prior.start, prior.end)
    own_current = window_sum(own_series, current.start, current.end)
    own_prior = window_sum(own_series, prior.start, prior.end)

    # 期間比較は「観測がある期間どうし」でしか意味を持たない。poll を始めた
    # 直後は前期がほぼ空なので、素直に割ると需要が急増したように見えてしまう。
    # 被覆率が足りない比較は数字を出さず、シグナルを黙らせる。
    prior_covered = (
        coverage(area_series, prior.start, prior.end) >= MIN_COMPARABLE_COVERAGE
        and coverage(own_series, prior.start, prior.end) >= MIN_COMPARABLE_COVERAGE
    )
    share_now = (own_current / area_current) if area_current > 0 else 0.0
    share_before = (own_prior / area_prior) if area_prior > 0 else 0.0
    share_change = _pct_change(share_now, share_before) if prior_covered else None

    recent_start = current.end - dt.timedelta(days=RECENT_DAYS - 1)
    before_start = current.end - dt.timedelta(days=2 * RECENT_DAYS - 1)
    before_end = current.end - dt.timedelta(days=RECENT_DAYS)
    recent = window_sum(area_series, recent_start, current.end)
    before = window_sum(area_series, before_start, before_end)
    both_covered = (
        coverage(area_series, recent_start, current.end) >= MIN_COMPARABLE_COVERAGE
        and coverage(area_series, before_start, before_end) >= MIN_COMPARABLE_COVERAGE
    )
    area_recent = _pct_change(recent, before) if both_covered else None

    latest = store.latest(config.own.place_id)
    own_rating = latest.rating if latest else None

    # リボンは現行窓と前期窓の両方を映す。片方だけでは「細った」ことが見えない。
    ribbon_start = prior.start - dt.timedelta(days=1)
    ribbon_dates = date_range(ribbon_start, current.end)
    ribbon = Ribbon(
        dates=ribbon_dates,
        area=rolling_sum(area_series, ribbon_start, current.end, ROLLING_DAYS),
        own=rolling_sum(own_series, ribbon_start, current.end, ROLLING_DAYS),
        events=[e for e in config.events if ribbon_start <= e.date <= current.end],
    )

    own_est = next(e for e in estimates if e.prop.is_own)
    rate_blocked = own_rating is not None and own_rating < config.rules.rating_floor
    low_confidence = (
        own_est.confidence(est_cfg.confidence_high_rse, est_cfg.confidence_medium_rse) == "低"
    )

    verdict = build_verdict(config, estimates, rate_blocked, low_confidence)
    signals = build_signals(
        config,
        estimates,
        rate,
        checks,
        own_rating,
        area_recent,
        share_change,
        as_of,
        current,
    )

    return Dashboard(
        config=config,
        as_of=as_of,
        current=current,
        prior=prior,
        estimates=estimates,
        rate=rate,
        backtest=checks,
        verdict=verdict,
        signals=signals,
        ribbon=ribbon,
        area_reviews=area_current,
        own_reviews=own_current,
        own_rating=own_rating,
    )
