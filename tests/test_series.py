import datetime as dt

import pytest

from kanoya.config import Property
from kanoya.series import (
    coverage,
    daily_increments,
    lodging_series,
    rolling_sum,
    shift_to_stay_dates,
    window_sum,
)
from kanoya.store import Snapshot

D = dt.date


def snaps(pairs, place_id="p"):
    return {d: Snapshot(date=d, place_id=place_id, user_rating_count=c, rating=4.5) for d, c in pairs}


def test_increments_drop_the_first_day():
    # 初日は前日がないので増分を定義できない。0 と偽らずに落とす。
    series = snaps([(D(2026, 1, 1), 100), (D(2026, 1, 2), 103)])
    assert daily_increments(series) == {D(2026, 1, 2): 3}


def test_increments_spread_across_observation_gaps():
    # poll が 3 日落ちた区間。増えた 7 件を 3 日に按分し、端数は後ろへ寄せる。
    series = snaps([(D(2026, 1, 1), 100), (D(2026, 1, 4), 107)])
    out = daily_increments(series)
    assert out == {D(2026, 1, 2): 2, D(2026, 1, 3): 2, D(2026, 1, 4): 3}
    assert sum(out.values()) == 7


def test_deleted_reviews_do_not_become_negative_demand():
    series = snaps([(D(2026, 1, 1), 100), (D(2026, 1, 2), 97)])
    assert daily_increments(series) == {D(2026, 1, 2): 0}


def test_shift_moves_posts_back_to_stay_dates():
    posted = {D(2026, 5, 20): 4}
    assert shift_to_stay_dates(posted, 10) == {D(2026, 5, 10): 4}


def test_restaurant_share_is_deducted_from_lodging_reviews():
    prop = Property(key="d", name="D", place_id="p", rooms=40, restaurant_review_share=0.25)
    series = snaps([(D(2026, 1, 1), 100), (D(2026, 1, 2), 108)])
    out = lodging_series(prop, series, lag_days=10)
    assert out == {D(2025, 12, 23): 6.0}


def test_window_sum_includes_both_ends():
    series = {D(2026, 1, d): 1.0 for d in range(1, 11)}
    assert window_sum(series, D(2026, 1, 3), D(2026, 1, 5)) == 3.0


def test_coverage_reports_observed_fraction_of_the_window():
    series = {D(2026, 1, 1): 1.0, D(2026, 1, 3): 1.0}
    assert coverage(series, D(2026, 1, 1), D(2026, 1, 4)) == 0.5


def test_rolling_sum_is_backward_looking():
    series = {D(2026, 1, d): float(d) for d in range(1, 11)}
    out = rolling_sum(series, D(2026, 1, 5), D(2026, 1, 6), window=3)
    assert out == [3 + 4 + 5, 4 + 5 + 6]


@pytest.mark.parametrize("gap", [1, 2, 5, 30])
def test_gap_filling_conserves_the_total(gap):
    start, end = D(2026, 1, 1), D(2026, 1, 1) + dt.timedelta(days=gap)
    out = daily_increments(snaps([(start, 500), (end, 517)]))
    assert sum(out.values()) == 17
    assert len(out) == gap
