from __future__ import annotations

from datetime import date, timedelta

import pytest

from kanoya.config import (
    Config,
    Estimation,
    Event,
    PricingRules,
    Property,
    ReviewRateConfig,
)
from kanoya.store import Snapshot

AS_OF = date(2026, 8, 15)


@pytest.fixture
def own_property() -> Property:
    return Property(
        id="own", name="鹿のや", place_id="p_own", rooms=5, is_own=True,
        restaurant_review_share=0.0,
    )


@pytest.fixture
def config(own_property: Property) -> Config:
    return Config(
        area_name="奈良春日",
        window_days=90,
        timezone="Asia/Tokyo",
        properties=[
            own_property,
            Property(id="comp_a", name="競合A", place_id="p_a", rooms=10),
            Property(id="comp_b", name="競合B", place_id="p_b", rooms=20),
        ],
        estimation=Estimation(review_lag_days=10, unstable_tail_days=10),
        review_rate=ReviewRateConfig(mode="fixed", fallback_rate=0.10),
        pricing_rules=PricingRules(),
        events=[Event(date=date(2026, 10, 25), name="正倉院展", lift=30)],
    )


def make_snapshots(
    property_id: str,
    start: date,
    daily_new: list[int],
    base_count: int = 100,
    rating: float | None = 4.7,
    place_id: str = "p",
) -> list[Snapshot]:
    """日次の新規レビュー数から累計スナップショット列を作る。

    daily_new[i] は start+i+1 日に増えた件数として扱う（最初の1件は基準点）。
    """
    snapshots = [
        Snapshot(
            date=start,
            property_id=property_id,
            place_id=place_id,
            user_rating_count=base_count,
            rating=rating,
        )
    ]
    running = base_count
    for offset, new in enumerate(daily_new, start=1):
        running += new
        snapshots.append(
            Snapshot(
                date=start + timedelta(days=offset),
                property_id=property_id,
                place_id=place_id,
                user_rating_count=running,
                rating=rating,
            )
        )
    return snapshots


def constant_snapshots(
    property_id: str,
    start: date,
    days: int,
    per_day: int,
    rooms_unused: int = 0,
    rating: float | None = 4.7,
) -> list[Snapshot]:
    return make_snapshots(property_id, start, [per_day] * days, rating=rating)
