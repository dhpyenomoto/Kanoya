"""ドメイン型。推定結果はここで定義した型だけを介して描画側に渡す。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import DemandEvent, PropertyConfig


@dataclass(frozen=True)
class Snapshot:
    """ある日に Google Places から取得した1施設分の状態。"""

    place_key: str
    captured_on: date
    rating: float | None
    user_rating_count: int


@dataclass(frozen=True)
class PostingRate:
    """レビュー投稿率のキャリブレーション結果。"""

    rate: float
    ci_low: float
    ci_high: float
    days: int
    sold_rooms: float
    raw_reviews: float
    stay_reviews: float
    source: str

    @property
    def is_measured(self) -> bool:
        return self.source == "自社実績で実測"


@dataclass(frozen=True)
class Backtest:
    """自社実績に対する推定精度。ここが崩れたら推定値は使わない。"""

    mean_abs_error_pt: float
    windows: int
    usable_for: str


@dataclass(frozen=True)
class PropertyEstimate:
    """1施設の直近ウィンドウ推定。"""

    property: PropertyConfig
    stay_reviews: float
    raw_reviews: float
    sold_rooms: float
    occupancy: float
    prev_occupancy: float | None
    confidence: str
    rating: float | None

    @property
    def change_pct(self) -> float | None:
        if not self.prev_occupancy:
            return None
        return (self.occupancy / self.prev_occupancy - 1.0) * 100.0


@dataclass(frozen=True)
class RibbonSeries:
    """需要リボン用の7日移動合計。"""

    dates: list[date]
    area: list[float]
    own: list[float]
    events: list[DemandEvent]


@dataclass(frozen=True)
class AreaShare:
    total_raw_reviews: float
    own_raw_reviews: float

    @property
    def share_pct(self) -> float:
        if self.total_raw_reviews <= 0:
            return 0.0
        return self.own_raw_reviews / self.total_raw_reviews * 100.0


@dataclass(frozen=True)
class Signal:
    category: str
    level: str
    title: str
    body: str
    tone: str = "info"  # info | watch | alert


@dataclass(frozen=True)
class Verdict:
    level: str
    headline: str
    body: str
    tone: str = "hold"  # hold | alert


@dataclass(frozen=True)
class Report:
    as_of: date
    generated_on: date
    window_days: int
    review_lag_days: int
    area_label: str
    own: PropertyEstimate
    estimates: list[PropertyEstimate]
    posting_rate: PostingRate
    backtest: Backtest
    share: AreaShare
    ribbon: RibbonSeries
    signals: list[Signal]
    verdict: Verdict
    rating_floor: float
    events: list[DemandEvent] = field(default_factory=list)

    @property
    def compset_median_occupancy(self) -> float:
        others = sorted(e.occupancy for e in self.estimates if not e.property.is_own)
        if not others:
            return 0.0
        mid = len(others) // 2
        if len(others) % 2:
            return others[mid]
        return (others[mid - 1] + others[mid]) / 2.0
