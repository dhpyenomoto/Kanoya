"""コンプセットの推定稼働率。

推定販売室数 = 宿泊レビュー数 ÷ 投稿率
推定稼働率   = 推定販売室数 ÷ (客室数 × 窓日数)

絶対値は投稿率の推定誤差をそのまま引きずる。読むべきは施設間の相対差と
前期比のモメンタムであって、稼働率そのものではない。
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Mapping

from .calibration import PostingRate
from .config import Config, Property
from .series import coverage, window_sum


@dataclass(frozen=True)
class Window:
    start: dt.date
    end: dt.date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass(frozen=True)
class Estimate:
    prop: Property
    reviews: float
    rooms_sold: float
    occupancy: float
    prior_occupancy: float | None
    relative_se: float
    coverage: float

    @property
    def change_pct(self) -> float | None:
        """前期比（相対%）。前期の稼働がゼロなら定義しない。"""
        if self.prior_occupancy is None or self.prior_occupancy <= 0:
            return None
        return (self.occupancy / self.prior_occupancy - 1.0) * 100.0

    def confidence(self, high_rse: float, medium_rse: float) -> str:
        if self.coverage < 0.8:
            return "低"
        if self.relative_se <= high_rse:
            return "高"
        if self.relative_se <= medium_rse:
            return "中"
        return "低"


def windows(as_of: dt.date, window_days: int, lag_days: int) -> tuple[Window, Window]:
    """基準日から現行窓と前期窓を切り出す。

    基準日は「今日」ではなく「今日 − 投稿遅延」。直近の滞在分はレビューが
    まだ出揃っていないので、そこを窓に含めると全施設が一律に低く出る。
    """
    end = as_of - dt.timedelta(days=lag_days)
    current = Window(start=end - dt.timedelta(days=window_days - 1), end=end)
    prior_end = current.start - dt.timedelta(days=1)
    prior = Window(start=prior_end - dt.timedelta(days=window_days - 1), end=prior_end)
    return current, prior


def estimate_property(
    prop: Property,
    series: Mapping[dt.date, float],
    rate: PostingRate,
    current: Window,
    prior: Window,
) -> Estimate:
    reviews = window_sum(series, current.start, current.end)
    capacity = prop.rooms * current.days
    rooms_sold = reviews / rate.point if rate.point > 0 else 0.0

    prior_reviews = window_sum(series, prior.start, prior.end)
    prior_cov = coverage(series, prior.start, prior.end)
    prior_occ: float | None = None
    if prior_cov >= 0.8 and rate.point > 0:
        prior_occ = (prior_reviews / rate.point) / (prop.rooms * prior.days)

    # 誤差は 2 つの独立な源から来る。レビュー件数の計数ゆらぎ（ポアソン）と、
    # 投稿率そのものの推定誤差。後者は全施設に共通して効くので、施設間の
    # 順位づけには影響しないが、水準の議論には効く。
    count_rse = 1.0 / math.sqrt(reviews) if reviews > 0 else float("inf")
    rel_se = math.hypot(count_rse, rate.relative_se)

    return Estimate(
        prop=prop,
        reviews=reviews,
        rooms_sold=rooms_sold,
        occupancy=rooms_sold / capacity if capacity else 0.0,
        prior_occupancy=prior_occ,
        relative_se=rel_se,
        coverage=coverage(series, current.start, current.end),
    )


def estimate_all(
    config: Config,
    series_by_key: Mapping[str, Mapping[dt.date, float]],
    rate: PostingRate,
    current: Window,
    prior: Window,
) -> list[Estimate]:
    """推定稼働率の降順。自社もこの並びの中に置き、位置で読む。"""
    out = [
        estimate_property(prop, series_by_key.get(prop.key, {}), rate, current, prior)
        for prop in config.properties
    ]
    out.sort(key=lambda e: e.occupancy, reverse=True)
    return out


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0
