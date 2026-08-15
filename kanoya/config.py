"""config.json の読み込みと正規化。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Property:
    key: str
    name: str
    place_id: str
    rooms: int
    restaurant_review_share: float = 0.0
    is_own: bool = False


@dataclass(frozen=True)
class DemandEvent:
    date: date
    name: str
    lift_pct: float


@dataclass(frozen=True)
class Estimation:
    window_days: int = 90
    ribbon_days: int = 181
    review_lag_days: int = 10
    rolling_days: int = 7
    default_review_rate: float = 0.105
    confidence_high_band_pt: float = 8.0
    confidence_medium_band_pt: float = 18.0


@dataclass(frozen=True)
class Calibration:
    actuals_csv: str = "data/pms_actuals.csv"
    backtest_bucket_days: int = 30
    usable_mae_pt: float = 3.0
    relative_only_mae_pt: float = 6.0


@dataclass(frozen=True)
class Rules:
    rating_floor: float = 4.6
    parity_band_pt: float = 3.0
    raise_threshold_pt: float = 5.0
    cut_threshold_pt: float = -5.0
    momentum_flag_pct: float = 15.0
    event_horizon_days: int = 45
    stale_snapshot_days: int = 3
    min_coverage: float = 0.7


@dataclass(frozen=True)
class Config:
    root: Path
    area_name: str
    own: Property
    comp_set: tuple[Property, ...]
    estimation: Estimation
    calibration: Calibration
    rules: Rules
    demand_events: tuple[DemandEvent, ...] = field(default=())

    @property
    def properties(self) -> tuple[Property, ...]:
        """自社を先頭にした全施設。"""
        return (self.own,) + self.comp_set

    def property_by_key(self, key: str) -> Property | None:
        for p in self.properties:
            if p.key == key:
                return p
        return None

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else self.root / p


def _property(raw: dict, *, is_own: bool) -> Property:
    return Property(
        key=raw["key"],
        name=raw["name"],
        place_id=raw.get("place_id", ""),
        rooms=int(raw["rooms"]),
        restaurant_review_share=float(raw.get("restaurant_review_share", 0.0)),
        is_own=is_own,
    )


def load_config(path: str | Path = "config.json") -> Config:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    own = _property(raw["property"], is_own=True)
    comps = tuple(_property(c, is_own=False) for c in raw.get("comp_set", []))

    keys = [p.key for p in (own,) + comps]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise ValueError(f"config.json: key が重複しています: {sorted(dupes)}")

    for p in (own,) + comps:
        if p.rooms <= 0:
            raise ValueError(f"config.json: {p.key} の rooms は 1 以上である必要があります")
        if not 0.0 <= p.restaurant_review_share < 1.0:
            raise ValueError(
                f"config.json: {p.key} の restaurant_review_share は 0 以上 1 未満です"
            )

    events = tuple(
        DemandEvent(
            date=date.fromisoformat(e["date"]),
            name=e["name"],
            lift_pct=float(e.get("lift_pct", 0)),
        )
        for e in raw.get("demand_events", [])
    )

    return Config(
        root=path.resolve().parent,
        area_name=raw.get("area", {}).get("name", ""),
        own=own,
        comp_set=comps,
        estimation=Estimation(**raw.get("estimation", {})),
        calibration=Calibration(**raw.get("calibration", {})),
        rules=Rules(**raw.get("rules", {})),
        demand_events=tuple(sorted(events, key=lambda e: e.date)),
    )
