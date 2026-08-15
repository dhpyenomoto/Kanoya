"""コネクタ共通のデータ構造とプロトコル."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol


@dataclass
class PlaceRecord:
    """近隣宿泊施設の1件（Places API 等から発見されたもの）."""

    place_id: str
    name: str
    address: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    rating: float | None = None
    review_count: int = 0
    price_level: str = ""          # PRICE_LEVEL_* / ""
    types: list[str] = field(default_factory=list)
    website: str = ""
    source: str = ""

    def distance_km(self, lat: float, lon: float) -> float:
        return haversine_km(self.latitude, self.longitude, lat, lon)


@dataclass
class RateRecord:
    """競合1施設・1宿泊日の掲出価格（生値）."""

    stay_date: date
    property_name: str
    property_token: str = ""
    channel: str = ""              # 掲出元（OTA名）
    raw_rate: float | None = None  # 掲出価格（税別／込は tax_included で表す）
    total_rate: float | None = None
    currency: str = "JPY"
    tax_included: bool = False
    available: bool = True
    adults: int = 2
    los: int = 1
    rating: float | None = None
    review_count: int = 0
    hotel_class: float | None = None
    latitude: float = 0.0
    longitude: float = 0.0
    source: str = ""
    is_fixture: bool = False


class PlaceSource(Protocol):
    """近隣施設を発見するデータ源."""

    name: str

    def nearby_lodging(
        self, latitude: float, longitude: float, radius_m: int, run_date: date
    ) -> list[PlaceRecord]:
        ...


class RateSource(Protocol):
    """競合の実勢価格を取得するデータ源."""

    name: str

    def rates_for_date(
        self, query: str, stay_date: date, run_date: date, *, adults: int = 2, los: int = 1
    ) -> list[RateRecord]:
        ...


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2地点間の距離（km）."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_money(value: Any) -> float | None:
    """'¥45,000' / '45000' / 45000 → 45000.0 。取れなければ None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    digits = "".join(ch for ch in str(value) if ch.isdigit() or ch == ".")
    if not digits or digits == ".":
        return None
    try:
        return float(digits)
    except ValueError:
        return None
