"""推定稼働率の算出。

    推定販売室数 = 窓内クチコミ件数 ÷ レビュー投稿率
    推定稼働率   = 推定販売室数 ÷ (客室数 × 窓日数)

絶対値そのものより、施設間の相対差と前期比のモメンタムを読むための数字。

推定の幅について。窓内のクチコミ件数は計数値なので、推定稼働率には
おおむね ±1.96/√(件数) の相対誤差が乗る。5室・90日窓なら件数は 40 件程度、
つまり ±25pt 前後の幅がある。この幅を持たない稼働率は読めない数字なので、
Estimate は必ず区間とセットで返す。

投稿率そのものの不確かさ（Calibrated.ci_low/ci_high）は全施設に同じ倍率で
効くため、施設間の比較には影響せず、水準の議論にだけ影響する。したがって
ここでは計数ノイズだけを区間に入れている。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from .config import Property
from .series import ReviewSeries

Z = 1.96


@dataclass(frozen=True)
class Estimate:
    key: str
    name: str
    rooms: int
    is_own: bool
    reviews: float
    rooms_sold: float
    occupancy: float
    occupancy_low: float
    occupancy_high: float
    prev_occupancy: float | None
    delta_pct: float | None
    coverage: float
    confidence: str
    capped: bool
    rating: float | None
    review_count: int | None

    @property
    def band_pt(self) -> float:
        """推定稼働率の 95% 区間の半幅（パーセントポイント）。"""
        return (self.occupancy_high - self.occupancy_low) / 2 * 100.0


def counting_band(occupancy: float, reviews: float) -> tuple[float, float]:
    """計数ノイズによる 95% 区間。件数が 0 なら区間は張れない。"""
    if reviews <= 0:
        return (0.0, 1.0)
    rel = Z / math.sqrt(reviews)
    return (max(0.0, occupancy * (1 - rel)), min(1.0, occupancy * (1 + rel)))


def _confidence(
    band_pt: float,
    coverage: float,
    restaurant_share: float,
    reviews: float,
    *,
    calibration_measured: bool,
    high_band_pt: float = 8.0,
    medium_band_pt: float = 18.0,
) -> str:
    """信頼度は「区間の狭さ」で決める。件数の多寡そのものではない。"""
    if reviews <= 0:
        return "低"
    if band_pt <= high_band_pt and coverage >= 0.9 and restaurant_share <= 0.2:
        level = "高"
    elif band_pt <= medium_band_pt and coverage >= 0.6:
        level = "中"
    else:
        level = "低"
    if not calibration_measured and level == "高":
        level = "中"
    return level


def estimate_property(
    prop: Property,
    series: ReviewSeries,
    *,
    window_end: date,
    window_days: int,
    review_rate: float,
    calibration_measured: bool = True,
    rating: float | None = None,
    review_count: int | None = None,
    high_band_pt: float = 8.0,
    medium_band_pt: float = 18.0,
) -> Estimate:
    if review_rate <= 0:
        raise ValueError("review_rate は 0 より大きい必要があります")

    window_start = window_end - timedelta(days=window_days - 1)
    prev_end = window_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=window_days - 1)

    reviews = series.total(window_start, window_end)
    capacity = prop.rooms * window_days
    rooms_sold = reviews / review_rate
    raw_occ = rooms_sold / capacity if capacity else 0.0
    capped = raw_occ > 1.0
    occupancy = min(raw_occ, 1.0)
    occ_low, occ_high = counting_band(occupancy, reviews)

    prev_coverage = series.coverage(prev_start, prev_end)
    prev_occ: float | None = None
    delta_pct: float | None = None
    if prev_coverage >= 0.6:
        prev_reviews = series.total(prev_start, prev_end)
        prev_occ = min((prev_reviews / review_rate) / capacity, 1.0) if capacity else 0.0
        if prev_occ > 0:
            delta_pct = (occupancy - prev_occ) / prev_occ * 100.0

    coverage = series.coverage(window_start, window_end)
    band_pt = (occ_high - occ_low) / 2 * 100.0
    return Estimate(
        key=prop.key,
        name=prop.name,
        rooms=prop.rooms,
        is_own=prop.is_own,
        reviews=reviews,
        rooms_sold=rooms_sold,
        occupancy=occupancy,
        occupancy_low=occ_low,
        occupancy_high=occ_high,
        prev_occupancy=prev_occ,
        delta_pct=delta_pct,
        coverage=coverage,
        confidence=_confidence(
            band_pt,
            coverage,
            prop.restaurant_review_share,
            reviews,
            calibration_measured=calibration_measured,
            high_band_pt=high_band_pt,
            medium_band_pt=medium_band_pt,
        ),
        capped=capped,
        rating=rating,
        review_count=review_count,
    )


def comp_median(estimates: list[Estimate]) -> float | None:
    """競合のみの推定稼働率の中央値（自社は除く）。"""
    vals = sorted(e.occupancy for e in estimates if not e.is_own)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def comp_median_band_pt(estimates: list[Estimate]) -> float:
    """中央値まわりのノイズ幅。中央値に効く 1〜2 施設の幅で代表させる。"""
    comps = sorted((e for e in estimates if not e.is_own), key=lambda e: e.occupancy)
    if not comps:
        return 0.0
    mid = len(comps) // 2
    if len(comps) % 2:
        return comps[mid].band_pt
    return (comps[mid - 1].band_pt + comps[mid].band_pt) / 2


def noise_floor_pt(estimates: list[Estimate]) -> float:
    """自社と競合中央値の差が、ノイズだけで動きうる幅（95%）。"""
    own = next((e for e in estimates if e.is_own), None)
    if own is None:
        return 0.0
    return math.hypot(own.band_pt, comp_median_band_pt(estimates))
