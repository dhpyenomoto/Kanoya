from datetime import date, timedelta

from kanoya.config import PropertyConfig
from kanoya.models import Snapshot
from kanoya.timeline import (
    PropertyTimeline,
    posts_by_publish_date,
    rolling_sum,
    shift_to_stay_date,
    stay_reviews,
)

PROP = PropertyConfig(
    key="x", name="X", place_id="p", rooms=10, restaurant_review_share=0.4
)


def snaps(pairs):
    return [Snapshot("x", d, 4.5, c) for d, c in pairs]


def test_daily_delta():
    result = posts_by_publish_date(
        snaps([(date(2026, 1, 1), 100), (date(2026, 1, 2), 103)])
    )
    assert result == {date(2026, 1, 2): 3.0}


def test_gap_is_spread_evenly():
    """取得が飛んだ区間は均等配分。増分の総量は保存される。"""
    result = posts_by_publish_date(
        snaps([(date(2026, 1, 1), 100), (date(2026, 1, 5), 108)])
    )
    assert sorted(result) == [date(2026, 1, i) for i in (2, 3, 4, 5)]
    assert all(v == 2.0 for v in result.values())
    assert sum(result.values()) == 8.0


def test_deleted_reviews_do_not_create_negative_demand():
    result = posts_by_publish_date(
        snaps([(date(2026, 1, 1), 100), (date(2026, 1, 2), 96), (date(2026, 1, 3), 99)])
    )
    assert result == {date(2026, 1, 3): 3.0}


def test_same_day_duplicate_snapshot_is_ignored():
    result = posts_by_publish_date(
        snaps([(date(2026, 1, 1), 100), (date(2026, 1, 1), 101)])
    )
    assert result == {}


def test_lag_shift_moves_reviews_back_to_stay_date():
    posts = {date(2026, 5, 20): 4.0}
    assert shift_to_stay_date(posts, 10) == {date(2026, 5, 10): 4.0}


def test_restaurant_share_is_deducted():
    assert stay_reviews({date(2026, 5, 10): 10.0}, PROP) == {date(2026, 5, 10): 6.0}


def test_rolling_sum_is_trailing():
    assert rolling_sum([1, 1, 1, 1, 1, 1, 1, 1], 7) == [1, 2, 3, 4, 5, 6, 7, 7]


def test_timeline_totals_round_trip():
    start = date(2026, 1, 1)
    counts = [0, 2, 1, 0, 3, 1, 2]
    running, rows = 500, []
    rows.append((start, running))
    for i, c in enumerate(counts, start=1):
        running += c
        rows.append((start + timedelta(days=i), running))

    timeline = PropertyTimeline(PROP, snaps(rows), lag_days=10)
    first_stay = start + timedelta(days=1 - 10)
    last_stay = start + timedelta(days=len(counts) - 10)
    assert timeline.raw_total(first_stay, last_stay) == sum(counts)
    assert timeline.stay_total(first_stay, last_stay) == sum(counts) * 0.6
