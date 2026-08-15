"""レビュー投稿率のキャリブレーションと、推定手法の後方検証。

この 2 つが無ければ、推定稼働率はただの掛け算の結果でしかない。
「自社では実際に何%の宿泊がレビューになったか」を実測し、
「その率で自社の過去を推定したら実績とどれだけずれたか」を測る。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from . import pms, store
from .config import Config
from .store import Snapshot


@dataclass(frozen=True)
class PostingRate:
    """レビュー投稿率（1 販売室あたり何件のレビューが立つか）。"""

    rate: float
    low: float
    high: float
    matched_days: int
    matched_rooms_sold: int
    matched_reviews: float
    measured: bool  # False = 自社実績が無く、フォールバック値を使っている

    @property
    def source_label(self) -> str:
        return "自社実績で実測" if self.measured else "フォールバック（未実測）"


def wilson_interval(successes: float, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson スコア区間。

    投稿率は「販売室あたり投稿されたか」の二項過程として扱う。
    正規近似だと率が小さいとき下限が負に振れるので Wilson を使う。
    """
    if trials <= 0:
        return (0.0, 0.0)
    p = successes / trials
    p = min(max(p, 0.0), 1.0)
    denom = 1 + z * z / trials
    center = p + z * z / (2 * trials)
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return (max((center - margin) / denom, 0.0), min((center + margin) / denom, 1.0))


def posting_rate(
    config: Config,
    snapshots: list[Snapshot],
    actuals: dict[date, pms.DayActual],
) -> PostingRate:
    """自社の PMS 実績とレビュー増分を突合して投稿率を実測する。

    突合対象は「実績があり、かつスナップショット台帳がカバーしている日」に限る。
    レビュー増分は滞在日に引き戻したうえで、レストラン外来分を控除する。
    """
    est = config.estimation
    fallback = PostingRate(
        rate=est.posting_rate_fallback,
        low=est.posting_rate_fallback * 0.7,
        high=est.posting_rate_fallback * 1.3,
        matched_days=0,
        matched_rooms_sold=0,
        matched_reviews=0.0,
        measured=False,
    )
    if not actuals:
        return fallback

    span = store.coverage(snapshots, config.own.place_id)
    if span is None:
        return fallback
    first_observed, last_observed = span

    daily = store.daily_reviews(
        snapshots, config.own.place_id, lag_days=est.review_lag_days
    )
    # 引き戻しの結果、台帳の末尾 lag 日ぶんは滞在日側が埋まらない。
    # さらに unstable_tail_days ぶん切って、母数の欠けた区間を突合から外す。
    start = first_observed
    end = last_observed - timedelta(days=est.review_lag_days + est.unstable_tail_days)
    if end <= start:
        return fallback

    matched_days = 0
    rooms_sold = 0
    day = start
    while day <= end:
        actual = actuals.get(day)
        if actual is not None:
            matched_days += 1
            rooms_sold += actual.rooms_sold
        day += timedelta(days=1)

    if rooms_sold <= 0 or matched_days == 0:
        return fallback

    raw_reviews = store.window_sum(daily, start, end)
    reviews = raw_reviews * (1 - config.own.restaurant_review_share)
    if reviews <= 0:
        return fallback

    rate = reviews / rooms_sold
    low, high = wilson_interval(reviews, rooms_sold)
    return PostingRate(
        rate=rate,
        low=low,
        high=high,
        matched_days=matched_days,
        matched_rooms_sold=rooms_sold,
        matched_reviews=reviews,
        measured=True,
    )


@dataclass(frozen=True)
class BacktestResult:
    """推定手法を自社実績に当てて測った誤差。"""

    windows: int
    mean_abs_error_pt: float
    worst_abs_error_pt: float
    samples: list[tuple[date, float, float]]  # (窓の終端, 推定, 実績)

    @property
    def usable_for_levels(self) -> bool:
        """絶対水準の議論に使ってよいか。

        平均誤差 5pt 以内なら「うちは競合より高い / 低い」という
        水準の話に使える。それ以上ずれるならモメンタム（前期比）だけ見る。
        """
        return self.windows >= 3 and self.mean_abs_error_pt <= 5.0

    @property
    def usage_label(self) -> str:
        if self.windows < 3:
            return "検証不足（モメンタムのみ）"
        return "水準の議論に使える" if self.usable_for_levels else "モメンタムのみ"


def backtest(
    config: Config,
    snapshots: list[Snapshot],
    actuals: dict[date, pms.DayActual],
    rate: PostingRate,
    *,
    step_days: int = 30,
    max_windows: int = 6,
) -> BacktestResult | None:
    """自社について、推定稼働率と実績稼働率をずらしながら突き合わせる。

    投稿率そのものを自社で実測している以上、全期間平均は定義上ほぼ一致する。
    意味があるのは「窓を切ったときに月ごとにどれだけ振れるか」であり、
    その振れ幅がそのまま競合の推定値に乗る誤差の下限になる。
    """
    est = config.estimation
    span = store.coverage(snapshots, config.own.place_id)
    if span is None or not actuals or rate.rate <= 0:
        return None
    first_observed, last_observed = span

    daily = store.daily_reviews(
        snapshots, config.own.place_id, lag_days=est.review_lag_days
    )
    horizon = last_observed - timedelta(days=est.review_lag_days + est.unstable_tail_days)

    samples: list[tuple[date, float, float]] = []
    end = horizon
    for _ in range(max_windows):
        start = end - timedelta(days=est.window_days - 1)
        if start < first_observed:
            break
        actual_occ = pms.occupancy_between(actuals, start, end)
        if actual_occ is None:
            break
        raw = store.window_sum(daily, start, end)
        adjusted = raw * (1 - config.own.restaurant_review_share)
        estimated_sold = adjusted / rate.rate
        capacity = config.own.rooms * est.window_days
        estimated_occ = min(estimated_sold / capacity, 1.0) if capacity else 0.0
        samples.append((end, estimated_occ, actual_occ))
        end = end - timedelta(days=step_days)

    if not samples:
        return None

    errors = [abs(est_occ - act) * 100 for _, est_occ, act in samples]
    return BacktestResult(
        windows=len(samples),
        mean_abs_error_pt=sum(errors) / len(errors),
        worst_abs_error_pt=max(errors),
        samples=list(reversed(samples)),
    )
