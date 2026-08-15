"""設定の読み込み。config.json をそのまま型付きで受け取るだけの層。"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Property:
    place_id: str
    name: str
    rooms: int
    restaurant_review_share: float = 0.0
    rating_floor: float | None = None
    is_own: bool = False

    def effective(self, raw_reviews: float) -> float:
        """外来（レストラン単独利用）クチコミを控除した実効レビュー数。"""
        return raw_reviews * (1.0 - self.restaurant_review_share)


@dataclass(frozen=True)
class Area:
    label: str
    latitude: float
    longitude: float
    radius_m: int
    included_types: tuple[str, ...]


@dataclass(frozen=True)
class Model:
    window_days: int = 90
    calibration_days: int = 261
    review_lag_days: int = 10
    unstable_tail_days: int = 10
    ribbon_days: int = 181
    rolling_days: int = 7
    confidence_reviews_high: float = 30.0
    confidence_reviews_mid: float = 10.0
    backtest_periods: int = 6
    backtest_step_days: int = 30


@dataclass(frozen=True)
class Rules:
    hold_band_pt: float = 3.0
    band_sigma: float = 1.0
    momentum_pt: float = 5.0
    share_drop_ratio: float = 0.15
    event_horizon_days: int = 45


@dataclass(frozen=True)
class Event:
    date: dt.date
    name: str
    weight: int = 0


@dataclass(frozen=True)
class Config:
    own: Property
    comp_set: tuple[Property, ...]
    area: Area
    model: Model
    rules: Rules
    events: tuple[Event, ...]
    paths: dict[str, str] = field(default_factory=dict)
    root: Path = Path(".")

    @property
    def tracked(self) -> tuple[Property, ...]:
        """自社＋コンプセット。スナップショット取得の対象。"""
        return (self.own,) + self.comp_set

    def path(self, key: str) -> Path:
        return self.root / self.paths[key]


def load(path: str | Path) -> Config:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    p = raw["property"]
    own = Property(
        place_id=p["place_id"],
        name=p["name"],
        rooms=int(p["rooms"]),
        restaurant_review_share=float(p.get("restaurant_review_share", 0.0)),
        rating_floor=float(p["rating_floor"]) if p.get("rating_floor") is not None else None,
        is_own=True,
    )
    comp_set = tuple(
        Property(
            place_id=c["place_id"],
            name=c["name"],
            rooms=int(c["rooms"]),
            restaurant_review_share=float(c.get("restaurant_review_share", 0.0)),
        )
        for c in raw["comp_set"]
    )
    a = raw["area"]
    area = Area(
        label=a.get("label", ""),
        latitude=float(a["latitude"]),
        longitude=float(a["longitude"]),
        radius_m=int(a["radius_m"]),
        included_types=tuple(a.get("included_types", ())),
    )
    events = tuple(
        Event(date=dt.date.fromisoformat(e["date"]), name=e["name"], weight=int(e.get("weight", 0)))
        for e in raw.get("demand_events", ())
    )
    return Config(
        own=own,
        comp_set=comp_set,
        area=area,
        model=Model(**raw.get("model", {})),
        rules=Rules(**raw.get("rules", {})),
        events=events,
        paths=raw.get("paths", {}),
        root=path.resolve().parent,
    )
