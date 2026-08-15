"""コンプセット推定稼働。

レビュー増分 ÷ 投稿率 ÷ 客室数。それだけ。
複雑にしても入力の粗さは消えないので、式は読める形のまま置く。
読むべきは絶対値ではなく、施設間の相対差とモメンタム。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, timedelta

from . import store
from .calibrate import PostingRate
from .config import Config, Property
from .store import Snapshot

CONFIDENCE_ORDER = {"高": 0, "中": 1, "低": 2}


@dataclass(frozen=True)
class PropertyEstimate:
    prop: Property
    raw_reviews: float          # 窓内のレビュー増分（生）
    adjusted_reviews: float     # レストラン外来分を控除した後
    rooms_sold: float           # 推定販売室数
    occupancy: float            # 推定稼働率 0.0〜1.0
    prior_occupancy: float | None
    confidence: str             # 高 / 中 / 低
    rse: float                  # 相対標準誤差
    covered: bool               # 台帳が窓全体をカバーしているか
    area_delta_pct: float | None = None  # エリア全体の前期比（調整の基準線）

    @property
    def raw_delta_pct(self) -> float | None:
        """素の前期比（相対変化率, %）。季節性を丸ごと含む。"""
        if self.prior_occupancy is None or self.prior_occupancy <= 0:
            return None
        return (self.occupancy - self.prior_occupancy) / self.prior_occupancy * 100

    @property
    def delta_pct(self) -> float | None:
        """エリア調整済みの前期比。

        奈良は季節差が大きく、90日窓を 90日ずらすと桜と真夏を比べることになる。
        素の前期比は毎年夏に全施設が二桁マイナスになり、判断に使えない。
        エリア全体の変化を引いて「市場に対して取れているか」だけを残す。
        """
        raw = self.raw_delta_pct
        if raw is None:
            return None
        if self.area_delta_pct is None:
            return raw
        return raw - self.area_delta_pct

    @property
    def confidence_rank(self) -> int:
        return CONFIDENCE_ORDER[self.confidence]


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def shifted_back(self, days: int) -> "Window":
        return Window(self.start - timedelta(days=days), self.end - timedelta(days=days))


def analysis_window(config: Config, as_of: date) -> Window:
    """as_of から見て、推定に使える最新の窓。

    レビューは滞在から遅れて投稿されるので、末尾の unstable_tail_days は
    まだ増分が出そろっていない。そこを切ったところを窓の終端にする。
    """
    est = config.estimation
    end = as_of - timedelta(days=est.unstable_tail_days)
    start = end - timedelta(days=est.window_days - 1)
    return Window(start, end)


def _confidence(raw_reviews: float, config: Config, covered: bool) -> tuple[str, float]:
    """増分の少なさから来る不確かさを 3 段階に丸める。

    件数 k のカウント過程の相対標準誤差は 1/sqrt(k)。
    月に数件しかレビューが立たない施設の推定値は、
    数字が出ていても読んではいけない、ということを明示する。
    """
    if raw_reviews <= 0:
        return "低", float("inf")
    rse = 1 / math.sqrt(raw_reviews)
    est = config.estimation
    if rse < est.confidence_high_rse:
        level = "高"
    elif rse < est.confidence_mid_rse:
        level = "中"
    else:
        level = "低"
    if not covered and level != "低":
        # 台帳が窓を覆っていないなら、増分が多くても一段落とす。
        level = "中" if level == "高" else "低"
    return level, rse


def _occupancy_for(
    prop: Property,
    daily: dict[date, float],
    window: Window,
    rate: PostingRate,
) -> tuple[float, float, float, float]:
    raw = store.window_sum(daily, window.start, window.end)
    adjusted = raw * (1 - prop.restaurant_review_share)
    rooms_sold = adjusted / rate.rate if rate.rate > 0 else 0.0
    capacity = prop.rooms * window.days
    occupancy = min(rooms_sold / capacity, 1.0) if capacity else 0.0
    return raw, adjusted, rooms_sold, occupancy


def estimate_property(
    prop: Property,
    config: Config,
    snapshots: list[Snapshot],
    rate: PostingRate,
    window: Window,
) -> PropertyEstimate:
    est = config.estimation
    daily = store.daily_reviews(snapshots, prop.place_id, lag_days=est.review_lag_days)

    raw, adjusted, rooms_sold, occupancy = _occupancy_for(prop, daily, window, rate)

    prior_window = Window(
        window.start - timedelta(days=window.days),
        window.start - timedelta(days=1),
    )
    span = store.coverage(snapshots, prop.place_id)
    covered = span is not None and span[0] <= window.start
    has_prior = span is not None and span[0] <= prior_window.start

    prior_occupancy = None
    if has_prior:
        _, _, _, prior_occupancy = _occupancy_for(prop, daily, prior_window, rate)
        if prior_occupancy <= 0:
            prior_occupancy = None

    confidence, rse = _confidence(raw, config, covered)
    return PropertyEstimate(
        prop=prop,
        raw_reviews=raw,
        adjusted_reviews=adjusted,
        rooms_sold=rooms_sold,
        occupancy=occupancy,
        prior_occupancy=prior_occupancy,
        confidence=confidence,
        rse=rse,
        covered=covered,
    )


def estimate_all(
    config: Config,
    snapshots: list[Snapshot],
    rate: PostingRate,
    window: Window,
) -> list[PropertyEstimate]:
    """全施設。稼働率の降順で返す。

    前期比はエリア全体の変化を差し引いた値にする。基準線には信頼度が
    確保できた施設の素の前期比の中央値を使う（自社も含む——市場の一部だから）。
    """
    estimates = [
        estimate_property(prop, config, snapshots, rate, window)
        for prop in config.properties
    ]

    baseline = _median(
        [
            e.raw_delta_pct
            for e in estimates
            if e.confidence != "低" and e.raw_delta_pct is not None
        ]
    )
    if baseline is not None:
        estimates = [replace(e, area_delta_pct=baseline) for e in estimates]

    estimates.sort(key=lambda e: e.occupancy, reverse=True)
    return estimates


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def compset_median(estimates: list[PropertyEstimate]) -> float | None:
    """自社を除いた競合の稼働率中央値。

    信頼度「低」の施設は中央値から外す。数件のレビューから出た
    ノイズを、自社の値付け判断の基準線にしてはいけない。
    """
    return _median(
        [e.occupancy for e in estimates if not e.prop.is_own and e.confidence != "低"]
    )


@dataclass(frozen=True)
class AreaSeries:
    """需要リボン用の時系列。"""

    days: list[date]
    area: list[float]  # エリア全体の 7 日移動合計
    own: list[float]   # うち自社ぶん

    @property
    def own_share(self) -> float:
        total = sum(self.area)
        return sum(self.own) / total if total > 0 else 0.0

    def share_at(self, index: int) -> float:
        if not 0 <= index < len(self.area) or self.area[index] <= 0:
            return 0.0
        return self.own[index] / self.area[index]


def area_series(
    config: Config,
    snapshots: list[Snapshot],
    *,
    as_of: date,
    span_days: int = 190,
    rolling: int = 7,
) -> AreaSeries:
    """エリア全体と自社のレビュー発生量（移動合計）。

    ここは推定を挟まない生の観測量。稼働率に変換する前の
    「エリアにどれだけ人が来ているか」を、そのまま面積で見せる。
    """
    est = config.estimation
    end = as_of - timedelta(days=est.unstable_tail_days)
    start = end - timedelta(days=span_days - 1)

    per_prop = {
        prop.place_id: store.daily_reviews(
            snapshots, prop.place_id, lag_days=est.review_lag_days
        )
        for prop in config.properties
    }

    days: list[date] = []
    area: list[float] = []
    own: list[float] = []
    day = start
    while day <= end:
        window_start = day - timedelta(days=rolling - 1)
        area_total = 0.0
        for place_id, daily in per_prop.items():
            area_total += store.window_sum(daily, window_start, day)
        own_total = store.window_sum(per_prop[config.own.place_id], window_start, day)
        days.append(day)
        area.append(area_total)
        own.append(own_total)
        day += timedelta(days=1)

    return AreaSeries(days=days, area=area, own=own)
