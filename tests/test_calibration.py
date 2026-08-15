from __future__ import annotations

from datetime import date, timedelta

import pytest

from kanoya.calibration import (
    CalibrationError,
    backtest_occupancy,
    calibrate_review_rate,
    fixed_review_rate,
    load_pms_actuals,
    wilson_interval,
)

START = date(2026, 1, 1)
SPAN = (START, START + timedelta(days=299))


def _flat_series(days: int, per_day: float, start: date = START) -> dict[date, float]:
    return {start + timedelta(days=i): per_day for i in range(days)}


def _flat_actuals(days: int, rooms_sold: int, start: date = START) -> dict[date, int]:
    return {start + timedelta(days=i): rooms_sold for i in range(days)}


def test_calibrate_recovers_the_underlying_rate():
    """1 日 4 室売って 0.4 件のレビューが立つなら投稿率は 10%。"""
    rate = calibrate_review_rate(
        _flat_series(300, 0.4), _flat_actuals(300, 4), SPAN
    )

    assert rate.rate == pytest.approx(0.10)
    assert rate.is_measured
    assert rate.ci_low < rate.rate < rate.ci_high


def test_calibrate_excludes_the_unsettled_tail():
    """まだレビューが出揃っていない直近の滞在日を分母に入れない。

    入れてしまうと販売室数だけが積み上がり、投稿率が過小に出る。
    """
    series = _flat_series(280, 0.4)  # 直近 20 日はレビューがまだ立っていない
    actuals = _flat_actuals(300, 4)

    naive = calibrate_review_rate(series, actuals, SPAN)
    corrected = calibrate_review_rate(
        series, actuals, SPAN, review_lag_days=10, unstable_tail_days=10
    )

    assert naive.rate < 0.10
    assert corrected.rate == pytest.approx(0.10)


def test_calibrate_rejects_a_span_shorter_than_the_lag():
    span = (START, START + timedelta(days=5))
    with pytest.raises(CalibrationError, match="投稿遅延"):
        calibrate_review_rate(
            _flat_series(6, 1), _flat_actuals(6, 4), span,
            review_lag_days=10, unstable_tail_days=10,
        )


def test_calibrate_requires_a_minimum_sample():
    span = (START, START + timedelta(days=9))
    with pytest.raises(CalibrationError, match="販売室数"):
        calibrate_review_rate(_flat_series(10, 0.1), _flat_actuals(10, 1), span)


def test_calibrate_requires_observations():
    with pytest.raises(CalibrationError, match="スナップショット"):
        calibrate_review_rate(_flat_series(300, 0.4), _flat_actuals(300, 4), None)


def test_calibrate_rejects_disjoint_actuals():
    far_future = {date(2030, 1, 1) + timedelta(days=i): 4 for i in range(300)}
    with pytest.raises(CalibrationError, match="重ならない"):
        calibrate_review_rate(_flat_series(300, 0.4), far_future, SPAN)


def test_wilson_interval_brackets_the_point_estimate():
    low, high = wilson_interval(100, 1000)

    assert low < 0.10 < high
    assert 0.08 < low and high < 0.13


def test_wilson_interval_narrows_with_more_data():
    narrow = wilson_interval(1000, 10000)
    wide = wilson_interval(10, 100)

    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_wilson_interval_handles_no_trials():
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_fixed_rate_carries_no_interval():
    rate = fixed_review_rate(0.10)

    assert rate.rate == 0.10
    assert not rate.is_measured
    assert rate.ci_low == rate.ci_high == 0.10


def test_backtest_matches_actuals_when_the_rate_is_right():
    backtest = backtest_occupancy(
        _flat_series(300, 0.4),
        _flat_actuals(300, 4),
        rooms=8,
        review_rate=0.10,
        observation_span=SPAN,
        fold_days=90,
    )

    assert backtest.n >= 3
    assert backtest.mean_abs_error_pt == pytest.approx(0.0, abs=0.5)
    assert backtest.usability == "水準の議論に使える"


def test_backtest_flags_a_biased_rate():
    """投稿率を倍に取り違えると、推定稼働は実績の半分になる。"""
    backtest = backtest_occupancy(
        _flat_series(300, 0.4),
        _flat_actuals(300, 4),
        rooms=8,
        review_rate=0.20,
        observation_span=SPAN,
        fold_days=90,
    )

    assert backtest.mean_abs_error_pt == pytest.approx(25.0, abs=1.0)
    assert backtest.usability == "使用不可"


def test_backtest_returns_nothing_without_enough_history():
    backtest = backtest_occupancy(
        _flat_series(10, 0.4),
        _flat_actuals(10, 4),
        rooms=8,
        review_rate=0.10,
        observation_span=(START, START + timedelta(days=9)),
        fold_days=90,
    )

    assert backtest.n == 0
    assert backtest.usability == "検証不足"


def test_load_pms_actuals_reads_a_csv(tmp_path):
    path = tmp_path / "actuals.csv"
    path.write_text("date,rooms_sold\n2026-01-01,3\n2026-01-02,5\n", encoding="utf-8")

    actuals = load_pms_actuals(path)

    assert actuals == {date(2026, 1, 1): 3, date(2026, 1, 2): 5}


def test_load_pms_actuals_reports_a_bad_row(tmp_path):
    path = tmp_path / "actuals.csv"
    path.write_text("date,rooms_sold\n2026-01-01,three\n", encoding="utf-8")

    with pytest.raises(CalibrationError, match=":2"):
        load_pms_actuals(path)


def test_load_pms_actuals_requires_the_expected_columns(tmp_path):
    path = tmp_path / "actuals.csv"
    path.write_text("date,sold\n2026-01-01,3\n", encoding="utf-8")

    with pytest.raises(CalibrationError, match="rooms_sold"):
        load_pms_actuals(path)


def test_load_pms_actuals_reports_a_missing_file(tmp_path):
    with pytest.raises(CalibrationError, match="見つからない"):
        load_pms_actuals(tmp_path / "nope.csv")
