import math
from datetime import date, timedelta

from kanoya.calibrate import backtest, estimate_lag_days, measure_posting_rate
from kanoya.config import PropertyConfig
from kanoya.models import Snapshot
from kanoya.timeline import PropertyTimeline

from .helpers import demo_config

OWN = PropertyConfig(
    key="kanoya", name="鹿のや", place_id="p", rooms=5,
    restaurant_review_share=0.5, is_own=True,
)


def timeline_from_daily(prop, start, counts, lag=10):
    running, rows = 0, [Snapshot(prop.key, start, 4.5, 0)]
    for i, c in enumerate(counts, start=1):
        running += c
        rows.append(Snapshot(prop.key, start + timedelta(days=i), 4.5, running))
    return PropertyTimeline(prop, rows, lag_days=lag)


def test_posting_rate_is_reviews_over_actual_rooms():
    """1日1室売って2日に1件レビューが出れば、投稿率は 50%（外来控除前は 100%）。"""
    start = date(2026, 1, 1)
    counts = [1, 0] * 50
    timeline = timeline_from_daily(OWN, start, counts)
    first_stay = start + timedelta(days=1 - 10)
    actuals = {first_stay + timedelta(days=i): 1.0 for i in range(len(counts))}

    rate = measure_posting_rate(
        timeline, actuals, first_stay, first_stay + timedelta(days=len(counts) - 1)
    )
    assert math.isclose(rate.rate, 0.25, rel_tol=1e-9)
    assert rate.ci_low < rate.rate < rate.ci_high
    assert rate.is_measured


def test_posting_rate_falls_back_without_actuals():
    timeline = timeline_from_daily(OWN, date(2026, 1, 1), [1] * 30)
    rate = measure_posting_rate(timeline, {}, date(2026, 1, 1), date(2026, 1, 30))
    assert not rate.is_measured
    assert rate.rate == 0.10


def test_confidence_thresholds():
    rule = demo_config().estimation.confidence
    assert rule.level(30) == "高"
    assert rule.level(29.9) == "中"
    assert rule.level(11.9) == "低"


def test_lag_estimation_recovers_the_injected_delay():
    """PMS実績とレビューの相互相関から、仕込んだ遅延を取り戻せること。"""
    start = date(2026, 1, 1)
    sold = [(i * 7) % 5 + 1 for i in range(180)]
    lag = 12
    timeline = timeline_from_daily(OWN, start, sold, lag=lag)
    stay_start = start + timedelta(days=1 - lag)
    actuals = {stay_start + timedelta(days=i): float(v) for i, v in enumerate(sold)}

    found = estimate_lag_days(
        timeline, actuals, stay_start, stay_start + timedelta(days=len(sold) - 1)
    )
    assert found == lag


def test_backtest_flags_a_biased_estimate():
    """後半だけレビューが倍出る施設は、水準比較に使ってはいけないと判定される。"""
    config = demo_config()
    start = date(2026, 1, 1)
    counts = [1] * 130 + [2] * 130
    timeline = timeline_from_daily(OWN, start, counts)
    stay_start = start + timedelta(days=1 - 10)
    days = [stay_start + timedelta(days=i) for i in range(len(counts))]
    actuals = {day: 3.0 for day in days}

    rate = measure_posting_rate(timeline, actuals, days[0], days[-1])
    result = backtest(config, timeline, actuals, rate, days[0], days[-1])
    assert result.windows == config.estimation.backtest_windows
    assert result.mean_abs_error_pt > config.pricing.backtest_level_max_pt
    assert result.usable_for != "水準の議論に使える"
