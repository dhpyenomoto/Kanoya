"""レビュー増分から推定稼働率を出す。

    販売室数 ≒ 滞在日ベースのレビュー数 ÷ レビュー投稿率
    稼働率   ≒ 販売室数 ÷ （客室数 × 日数）

絶対値は投稿率の仮定にそのまま比例する。読むべきは施設間の相対差と、
前期からのモメンタム。

この推定の精度はレビュー件数だけで決まる。90 日で 30 件しかレビューが立たない
施設なら、稼働率の 95% 区間は ±12pt 前後にしかならない。信頼度はこの区間の
幅から機械的に決める。件数の閾値で決めると、5 室の施設に「高」が付いて
誤差より小さい差で価格を動かすことになる。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from .calibration import Z_95, ReviewRate
from .config import Config, Property
from .reviews import stay_review_series, sum_in_window, window_bounds
from .store import Snapshot

CONFIDENCE_HIGH = "高"
CONFIDENCE_MEDIUM = "中"
CONFIDENCE_LOW = "低"
CONFIDENCE_NONE = "不可"

#: 推定できたと言える最低レビュー件数。これを割ると区間が意味を持たない。
MIN_REVIEWS = 5


@dataclass(frozen=True)
class PropertyEstimate:
    """1 施設ぶんの推定結果。"""

    prop: Property
    occupancy: float | None
    sold_rooms: float
    reviews_in_window: float
    prior_occupancy: float | None
    confidence: str
    #: 推定稼働率そのものの 95% 区間の半幅（ポイント）。投稿率の誤差を含む。
    margin_pt: float
    #: 施設間比較に使う 95% 区間の半幅（ポイント）。投稿率の誤差を含まない。
    #: 投稿率は全施設に同じ値を当てているため、その誤差は比較では相殺される。
    relative_margin_pt: float
    rating: float | None = None

    @property
    def occupancy_pct(self) -> float | None:
        return None if self.occupancy is None else self.occupancy * 100

    @property
    def occupancy_range_pct(self) -> tuple[float, float] | None:
        """推定稼働率の 95% 区間。"""
        if self.occupancy_pct is None:
            return None
        return (
            max(0.0, self.occupancy_pct - self.margin_pt),
            min(100.0, self.occupancy_pct + self.margin_pt),
        )

    def separated_from(self, other_pct: float) -> bool:
        """他施設の水準との差が、計数誤差で説明できない大きさか。

        比較なので投稿率の誤差は効かない。相対区間のほうで判定する。
        """
        if self.occupancy_pct is None:
            return False
        return abs(self.occupancy_pct - other_pct) > self.relative_margin_pt

    @property
    def momentum_pct(self) -> float | None:
        """前期比（相対変化率、%）。前期が 0 なら判定不能。"""
        if self.occupancy is None or not self.prior_occupancy:
            return None
        return (self.occupancy - self.prior_occupancy) / self.prior_occupancy * 100

    @property
    def is_actionable(self) -> bool:
        return self.occupancy is not None and self.confidence in (
            CONFIDENCE_HIGH,
            CONFIDENCE_MEDIUM,
        )


@dataclass(frozen=True)
class MarketEstimate:
    """コンプセット全体の推定結果。"""

    as_of: date
    window_start: date
    window_end: date
    estimates: list[PropertyEstimate]
    own_share: float
    area_reviews: float
    own_reviews: float
    prior_area_reviews: float = 0.0

    @property
    def area_momentum_pct(self) -> float | None:
        """エリア全体のレビュー量の前期比（相対%）。

        施設ごとの前期比は季節性をまるごと含む。奈良なら桜と紅葉で需要が倍近く
        動くので、施設単独の前期比を competitive な良し悪しと読むと必ず間違える。
        エリア全体の動きを並べて出し、差し引いて読めるようにする。
        """
        if not self.prior_area_reviews:
            return None
        return (
            (self.area_reviews - self.prior_area_reviews) / self.prior_area_reviews * 100
        )

    def market_adjusted_momentum_pct(self, estimate: PropertyEstimate) -> float | None:
        """市場の動きを差し引いた前期比。シェアを取ったか失ったかを表す。"""
        area = self.area_momentum_pct
        if area is None or estimate.momentum_pct is None:
            return None
        return estimate.momentum_pct - area

    @property
    def own(self) -> PropertyEstimate:
        return next(e for e in self.estimates if e.prop.is_own)

    @property
    def competitors(self) -> list[PropertyEstimate]:
        return [e for e in self.estimates if not e.prop.is_own]

    @property
    def window_days(self) -> int:
        return (self.window_end - self.window_start).days + 1

    @property
    def competitor_median_occupancy_pct(self) -> float | None:
        """競合の推定稼働率の中央値。自社は含めない。"""
        values = [
            e.occupancy_pct
            for e in self.competitors
            if e.occupancy_pct is not None and e.is_actionable
        ]
        return statistics.median(values) if values else None

    @property
    def ranked(self) -> list[PropertyEstimate]:
        """推定稼働率の降順。推定できなかった施設は末尾に置く。"""
        return sorted(
            self.estimates,
            key=lambda e: (e.occupancy is None, -(e.occupancy or 0.0)),
        )


def estimate_property(
    prop: Property,
    snapshots: list[Snapshot],
    config: Config,
    review_rate: ReviewRate,
    window_start: date,
    window_end: date,
) -> PropertyEstimate:
    """1 施設の稼働率を推定する。"""
    est = config.estimation
    series = stay_review_series(snapshots, prop, est.review_lag_days)

    window_days = (window_end - window_start).days + 1
    reviews = sum_in_window(series, window_start, window_end)

    prior_end = window_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=window_days - 1)
    prior_reviews = sum_in_window(series, prior_start, prior_end)

    rating = _latest_rating(snapshots)

    if review_rate.rate <= 0 or reviews < MIN_REVIEWS:
        return PropertyEstimate(
            prop=prop,
            occupancy=None,
            sold_rooms=0.0,
            reviews_in_window=reviews,
            prior_occupancy=None,
            confidence=CONFIDENCE_NONE,
            margin_pt=float("inf"),
            relative_margin_pt=float("inf"),
            rating=rating,
        )

    capacity = prop.rooms * window_days
    sold_rooms = reviews / review_rate.rate
    occupancy = min(est.occupancy_ceiling, sold_rooms / capacity)

    prior_occupancy = None
    if _has_observations(snapshots, prior_start, prior_end):
        prior_occupancy = min(
            est.occupancy_ceiling, (prior_reviews / review_rate.rate) / capacity
        )

    margin_pt, relative_margin_pt = _margins_pt(reviews, occupancy, review_rate)

    return PropertyEstimate(
        prop=prop,
        occupancy=occupancy,
        sold_rooms=sold_rooms,
        reviews_in_window=reviews,
        prior_occupancy=prior_occupancy,
        confidence=_confidence(relative_margin_pt, config),
        margin_pt=margin_pt,
        relative_margin_pt=relative_margin_pt,
        rating=rating,
    )


def _margins_pt(
    reviews: float, occupancy: float, review_rate: ReviewRate
) -> tuple[float, float]:
    """推定稼働率の 95% 区間の半幅（絶対用・比較用）をポイントで返す。

    誤差は 2 つの独立な源から来る。

    1. レビュー件数そのものの計数誤差。件数は Poisson とみなし、相対標準誤差は
       1/√件数。90 日で 30 件なら 18% で、これが誤差の主成分になる。
    2. 投稿率の推定誤差。自社実績の突合から来る区間をそのまま伝播させる。

    重要なのは 2 が全施設に共通の系統誤差だということ。投稿率を 1 割高く
    見積もれば全施設の稼働が揃って 1 割低く出るので、施設間の比較では消える。
    したがって水準を語るときは 1+2、順位や差を語るときは 1 だけを使う。
    """
    if reviews <= 0:
        return float("inf"), float("inf")

    count_rel_se = math.sqrt(1.0 / reviews)
    relative_margin = Z_95 * count_rel_se * occupancy * 100

    rate_rel_var = 0.0
    if review_rate.is_measured and review_rate.rate > 0:
        rate_se = (review_rate.ci_high - review_rate.ci_low) / (2 * Z_95)
        rate_rel_var = (rate_se / review_rate.rate) ** 2

    absolute_rel_se = math.sqrt(1.0 / reviews + rate_rel_var)
    absolute_margin = Z_95 * absolute_rel_se * occupancy * 100

    return absolute_margin, relative_margin


def estimate_market(
    config: Config,
    snapshots_by_property: dict[str, list[Snapshot]],
    review_rate: ReviewRate,
    as_of: date,
) -> MarketEstimate:
    """コンプセット全体を推定する。"""
    est = config.estimation
    window_start, window_end = window_bounds(
        as_of, config.window_days, est.review_lag_days, est.unstable_tail_days
    )

    estimates = [
        estimate_property(
            prop,
            snapshots_by_property.get(prop.id, []),
            config,
            review_rate,
            window_start,
            window_end,
        )
        for prop in config.properties
    ]

    area_reviews = sum(e.reviews_in_window for e in estimates)
    own_reviews = next(e.reviews_in_window for e in estimates if e.prop.is_own)
    own_share = own_reviews / area_reviews if area_reviews > 0 else 0.0

    prior_area_reviews = _prior_area_reviews(
        config, snapshots_by_property, window_start, window_end
    )

    return MarketEstimate(
        as_of=as_of,
        window_start=window_start,
        window_end=window_end,
        estimates=estimates,
        own_share=own_share,
        area_reviews=area_reviews,
        own_reviews=own_reviews,
        prior_area_reviews=prior_area_reviews,
    )


def _prior_area_reviews(
    config: Config,
    snapshots_by_property: dict[str, list[Snapshot]],
    window_start: date,
    window_end: date,
) -> float:
    """1 窓ぶん前のエリア全体のレビュー量。

    前期の観測が揃っていない施設は 0 件として数えず、エリア全体の前期比が
    見かけ上の急増にならないようにする。
    """
    window_days = (window_end - window_start).days + 1
    prior_end = window_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=window_days - 1)

    total = 0.0
    for prop in config.properties:
        snapshots = snapshots_by_property.get(prop.id, [])
        if not _has_observations(snapshots, prior_start, prior_end):
            return 0.0
        series = stay_review_series(snapshots, prop, config.estimation.review_lag_days)
        total += sum_in_window(series, prior_start, prior_end)
    return total


def _confidence(relative_margin_pt: float, config: Config) -> str:
    """推定の信頼度。

    比較用の区間の半幅で決める。信頼度が答えるのは「この施設を他と並べて
    論じてよいか」であって、「稼働率の水準を言い当てられるか」ではない。
    後者はレビューという signal では小規模施設では原理的に届かない。
    """
    est = config.estimation
    if not math.isfinite(relative_margin_pt):
        return CONFIDENCE_NONE
    if relative_margin_pt <= est.high_confidence_margin_pt:
        return CONFIDENCE_HIGH
    if relative_margin_pt <= est.medium_confidence_margin_pt:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def _has_observations(snapshots: list[Snapshot], start: date, end: date) -> bool:
    """指定期間に観測が届いているか。

    観測開始前の期間をレビュー 0 件として扱うと、前期比が実態のない急増に見える。
    """
    if len(snapshots) < 2:
        return False
    ordered = sorted(snapshots, key=lambda s: s.date)
    return ordered[0].date <= start and ordered[-1].date >= end


def _latest_rating(snapshots: list[Snapshot]) -> float | None:
    for snap in sorted(snapshots, key=lambda s: s.date, reverse=True):
        if snap.rating is not None:
            return snap.rating
    return None
