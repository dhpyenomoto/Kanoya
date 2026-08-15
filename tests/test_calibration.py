import datetime as dt

import pytest

from kanoya.calibration import backtest, estimate_posting_rate, wilson_interval

D = dt.date


def test_wilson_brackets_the_point_estimate():
    low, high = wilson_interval(178, 929)
    assert low < 178 / 929 < high
    assert 0.16 < low < 0.18
    assert 0.21 < high < 0.23


def test_wilson_stays_inside_zero_and_one_for_rare_events():
    # Wald 近似ならここで下限が負に振れる。Wilson を使う理由。
    low, high = wilson_interval(1, 400)
    assert low >= 0.0
    assert high <= 1.0


def test_wilson_accepts_fractional_successes():
    # 外来控除でレビュー数は非整数になる。
    low, high = wilson_interval(104.88, 547.0)
    assert low < 104.88 / 547.0 < high


def test_wilson_with_no_trials_is_empty():
    assert wilson_interval(0, 0) == (0.0, 0.0)


def _flat(start, days, per_day):
    return {start + dt.timedelta(days=i): per_day for i in range(days)}


def test_posting_rate_is_reviews_over_rooms_sold():
    start = D(2026, 1, 1)
    rate = estimate_posting_rate(
        _flat(start, 100, 1.0), _flat(start, 100, 5), start, start + dt.timedelta(days=99)
    )
    assert rate.rooms_sold == 500
    assert rate.reviews == pytest.approx(100.0)
    assert rate.point == pytest.approx(0.2)
    assert rate.days == 100


def test_posting_rate_needs_pms_actuals_in_the_window():
    start = D(2026, 1, 1)
    with pytest.raises(ValueError):
        estimate_posting_rate(_flat(start, 10, 1.0), {}, start, start + dt.timedelta(days=9))


def test_backtest_recovers_occupancy_when_reviews_track_stays_exactly():
    # 5室・180日、稼働 60%、投稿率 20% をノイズなしで作る。誤差はゼロに近づく。
    start = D(2026, 1, 1)
    sold = _flat(start, 180, 3)
    reviews = {d: v * 0.2 for d, v in sold.items()}
    result = backtest(reviews, sold, start, start + dt.timedelta(days=179), 5, 6, 90)
    assert len(result.blocks) == 6
    assert result.mean_absolute_error_pt == pytest.approx(0.0, abs=1e-9)
    assert result.usable_range == "水準の議論に使える"
    for block in result.blocks:
        assert block.actual_occupancy == pytest.approx(0.6)


def test_backtest_blocks_are_window_length():
    start = D(2026, 1, 1)
    sold = _flat(start, 261, 3)
    reviews = {d: v * 0.2 for d, v in sold.items()}
    result = backtest(reviews, sold, start, start + dt.timedelta(days=260), 5, 6, 90)
    # 本番の集計窓と同じ長さで検証する。短い区間で測ると誤差が過大に出る。
    for block in result.blocks:
        assert (block.end - block.start).days + 1 == 90
    assert result.blocks[0].start == start
    assert result.blocks[-1].end == start + dt.timedelta(days=260)


def test_backtest_detects_a_systematic_over_estimate():
    # 後半だけレビューが 1.5 倍出る施設。稼働は一定なので推定は上振れするはず。
    start = D(2026, 1, 1)
    sold = _flat(start, 260, 3)
    reviews = {}
    for i, (day, value) in enumerate(sorted(sold.items())):
        reviews[day] = value * (0.2 if i < 130 else 0.3)
    result = backtest(reviews, sold, start, start + dt.timedelta(days=259), 5, 6, 90)
    assert result.mean_absolute_error_pt > 5.0
    assert result.usable_range != "水準の議論に使える"


def test_backtest_returns_nothing_without_data():
    start = D(2026, 1, 1)
    assert backtest({}, {}, start, start + dt.timedelta(days=10), 5, 6, 90).blocks == ()
