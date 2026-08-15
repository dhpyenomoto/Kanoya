from __future__ import annotations

from datetime import date, timedelta

import pytest

from kanoya.config import Property
from kanoya.reviews import (
    daily_new_reviews,
    date_range,
    latest_rating,
    observation_span,
    rolling_sum,
    stay_review_series,
    sum_in_window,
    window_bounds,
)
from kanoya.store import Snapshot
from tests.conftest import make_snapshots

START = date(2026, 1, 1)


def test_daily_new_reviews_takes_consecutive_deltas():
    snapshots = make_snapshots("own", START, [2, 0, 5])
    series = daily_new_reviews(snapshots)

    assert series[START + timedelta(days=1)] == 2
    assert series[START + timedelta(days=2)] == 0
    assert series[START + timedelta(days=3)] == 5
    # 最初の観測日には差分が定義できないので値を持たない。
    assert START not in series


def test_daily_new_reviews_spreads_gaps_evenly():
    """観測が飛んだ区間は、その増分を日数で按分する。"""
    snapshots = [
        Snapshot(date=START, property_id="own", place_id="p", user_rating_count=100),
        Snapshot(
            date=START + timedelta(days=4),
            property_id="own",
            place_id="p",
            user_rating_count=112,
        ),
    ]
    series = daily_new_reviews(snapshots)

    assert len(series) == 4
    assert all(value == pytest.approx(3.0) for value in series.values())
    assert sum(series.values()) == pytest.approx(12.0)


def test_daily_new_reviews_clamps_deletions_to_zero():
    """クチコミが削除されて総数が減っても、負の需要にはしない。"""
    snapshots = make_snapshots("own", START, [5])
    snapshots.append(
        Snapshot(
            date=START + timedelta(days=2),
            property_id="own",
            place_id="p",
            user_rating_count=90,
        )
    )
    series = daily_new_reviews(snapshots)

    assert series[START + timedelta(days=2)] == 0
    assert min(series.values()) >= 0


def test_stay_review_series_shifts_back_by_lag():
    snapshots = make_snapshots("own", START, [0, 7, 0])
    prop = Property(id="own", name="own", place_id="p", rooms=5)

    series = stay_review_series(snapshots, prop, review_lag_days=10)

    posted_on = START + timedelta(days=2)
    assert series[posted_on - timedelta(days=10)] == pytest.approx(7.0)


def test_stay_review_series_deducts_restaurant_share():
    snapshots = make_snapshots("own", START, [10])
    prop = Property(
        id="own", name="own", place_id="p", rooms=5, restaurant_review_share=0.3
    )

    series = stay_review_series(snapshots, prop, review_lag_days=0)

    assert series[START + timedelta(days=1)] == pytest.approx(7.0)


def test_window_bounds_excludes_the_unsettled_tail():
    """遅延ぶんと猶予ぶんを as_of から差し引いた位置に窓の右端が来る。"""
    start, end = window_bounds(
        date(2026, 8, 15), window_days=90, review_lag_days=10, unstable_tail_days=10
    )

    assert end == date(2026, 7, 26)
    assert (end - start).days + 1 == 90


def test_rolling_sum_counts_the_trailing_window():
    series = {START + timedelta(days=i): float(i) for i in range(5)}
    days = date_range(START, START + timedelta(days=4))

    result = rolling_sum(series, days, window=3)

    assert result[0] == pytest.approx(0.0)
    assert result[4] == pytest.approx(2 + 3 + 4)


def test_rolling_sum_rejects_nonpositive_window():
    with pytest.raises(ValueError):
        rolling_sum({}, [START], window=0)


def test_sum_in_window_is_inclusive_of_both_ends():
    series = {START + timedelta(days=i): 1.0 for i in range(5)}

    assert sum_in_window(series, START, START + timedelta(days=4)) == 5.0
    assert sum_in_window(series, START + timedelta(days=1), START + timedelta(days=2)) == 2.0


def test_observation_span_needs_two_points():
    assert observation_span([]) is None
    assert observation_span(make_snapshots("own", START, [])) is None
    assert observation_span(make_snapshots("own", START, [1, 1])) == (
        START,
        START + timedelta(days=2),
    )


def test_latest_rating_skips_missing_values():
    snapshots = make_snapshots("own", START, [1, 1], rating=None)
    snapshots[0] = Snapshot(
        date=START, property_id="own", place_id="p", user_rating_count=100, rating=4.4
    )

    assert latest_rating(snapshots) == 4.4
    assert latest_rating([]) is None


def test_date_range_handles_reversed_bounds():
    assert date_range(START + timedelta(days=1), START) == []
