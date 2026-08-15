import datetime as dt

import pytest

from kanoya.calibration import PostingRate
from kanoya.config import Property
from kanoya.estimate import estimate_property, median, windows

D = dt.date

RATE = PostingRate(point=0.2, low=0.18, high=0.22, days=261, rooms_sold=929, reviews=185.8)


def _prop(rooms=5, share=0.0, own=True):
    return Property(key="k", name="K", place_id="p", rooms=rooms, restaurant_review_share=share, is_own=own)


def test_windows_end_before_the_unstable_tail():
    # 基準日は「今日」ではなく「今日 − 投稿遅延」。
    current, prior = windows(D(2026, 8, 15), window_days=90, lag_days=10)
    assert current.end == D(2026, 8, 5)
    assert current.start == D(2026, 5, 8)
    assert current.days == 90
    assert prior.end == D(2026, 5, 7)
    assert prior.start == D(2026, 2, 7)


def test_occupancy_is_reviews_over_rate_over_capacity():
    current, prior = windows(D(2026, 8, 15), 90, 10)
    series = {current.start + dt.timedelta(days=i): 1.0 for i in range(90)}
    est = estimate_property(_prop(rooms=5), series, RATE, current, prior)
    assert est.reviews == pytest.approx(90.0)
    assert est.rooms_sold == pytest.approx(450.0)
    assert est.occupancy == pytest.approx(1.0)


def test_prior_period_change_is_relative():
    current, prior = windows(D(2026, 8, 15), 90, 10)
    series = {current.start + dt.timedelta(days=i): 1.0 for i in range(90)}
    series.update({prior.start + dt.timedelta(days=i): 0.5 for i in range(90)})
    est = estimate_property(_prop(), series, RATE, current, prior)
    assert est.change_pct == pytest.approx(100.0)


def test_change_is_undefined_when_the_prior_window_is_unobserved():
    current, prior = windows(D(2026, 8, 15), 90, 10)
    series = {current.start + dt.timedelta(days=i): 1.0 for i in range(90)}
    est = estimate_property(_prop(), series, RATE, current, prior)
    assert est.prior_occupancy is None
    assert est.change_pct is None


def test_more_reviews_narrow_the_relative_error():
    current, prior = windows(D(2026, 8, 15), 90, 10)
    thin = {current.start + dt.timedelta(days=i): 0.2 for i in range(90)}
    thick = {current.start + dt.timedelta(days=i): 8.0 for i in range(90)}
    assert (
        estimate_property(_prop(), thick, RATE, current, prior).relative_se
        < estimate_property(_prop(), thin, RATE, current, prior).relative_se
    )


def test_relative_error_never_falls_below_the_posting_rate_error():
    # 投稿率の誤差は全施設に共通して残る。件数を増やしても消えない下限。
    current, prior = windows(D(2026, 8, 15), 90, 10)
    huge = {current.start + dt.timedelta(days=i): 5000.0 for i in range(90)}
    est = estimate_property(_prop(), huge, RATE, current, prior)
    assert est.relative_se >= RATE.relative_se


def test_partial_coverage_forces_low_confidence():
    current, prior = windows(D(2026, 8, 15), 90, 10)
    # 窓の 3 割しか観測がない。件数が多くても水準は主張できない。
    series = {current.start + dt.timedelta(days=i): 20.0 for i in range(27)}
    est = estimate_property(_prop(), series, RATE, current, prior)
    assert est.coverage < 0.8
    assert est.confidence(0.15, 0.25) == "低"


def test_confidence_tiers_follow_the_thresholds():
    current, prior = windows(D(2026, 8, 15), 90, 10)

    def conf(per_day):
        series = {current.start + dt.timedelta(days=i): per_day for i in range(90)}
        return estimate_property(_prop(), series, RATE, current, prior).confidence(0.15, 0.25)

    assert conf(6.0) == "高"
    assert conf(0.35) == "中"
    assert conf(0.06) == "低"


def test_median_of_even_and_odd_counts():
    assert median([1.0, 2.0, 3.0]) == 2.0
    assert median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert median([]) == 0.0
