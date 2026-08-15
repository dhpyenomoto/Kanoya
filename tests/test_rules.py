from dataclasses import replace
from datetime import date, timedelta

from kanoya import signals, verdict
from kanoya.config import PropertyConfig
from kanoya.models import AreaShare, Backtest, PropertyEstimate

from .helpers import demo_config

GOOD = Backtest(mean_abs_error_pt=2.2, windows=6, usable_for="水準の議論に使える")
POOR = Backtest(mean_abs_error_pt=9.0, windows=6, usable_for="方向感のみ")


def estimate(occupancy, *, rating=4.7, prev=None, is_own=True, conf="高", name="鹿のや"):
    prop = PropertyConfig(
        key="kanoya" if is_own else name, name=name, place_id="p", rooms=5, is_own=is_own
    )
    return PropertyEstimate(
        property=prop,
        stay_reviews=40.0,
        raw_reviews=70.0,
        sold_rooms=340.0,
        occupancy=occupancy,
        prev_occupancy=prev,
        confidence=conf,
        rating=rating,
    )


def test_small_gap_holds():
    result = verdict.decide(demo_config(), estimate(75.4), 73.8, GOOD)
    assert result.level == "据え置き"
    assert result.tone == "hold"


def test_large_positive_gap_suggests_a_raise():
    result = verdict.decide(demo_config(), estimate(84.0), 72.0, GOOD)
    assert result.level == "引き上げ検討"


def test_rating_floor_blocks_a_raise():
    """稼働で勝っていても評価が下限割れなら値上げは止める。露出が先に死ぬため。"""
    result = verdict.decide(demo_config(), estimate(84.0, rating=4.53), 72.0, GOOD)
    assert result.level == "据え置き"
    assert result.tone == "alert"


def test_large_negative_gap_raises_an_alert():
    result = verdict.decide(demo_config(), estimate(60.0), 75.0, GOOD)
    assert result.level == "要注意"
    assert result.tone == "alert"


def test_unusable_backtest_suspends_the_verdict():
    result = verdict.decide(demo_config(), estimate(84.0), 72.0, POOR)
    assert result.level == "判断保留"


def evaluate(
    config, own, others, *, prev_share=None, bt=GOOD, today=date(2026, 8, 15), as_of=None
):
    """既定では収集が健全な状態（as_of ＝ today − lag）で評価する。"""
    lag = timedelta(days=config.estimation.review_lag_days)
    return signals.evaluate(
        config,
        own,
        [own, *others],
        AreaShare(total_raw_reviews=1069.0, own_raw_reviews=65.0),
        prev_share,
        bt,
        as_of=as_of or today - lag,
        today=today,
    )


def test_rating_below_floor_emits_one_alert():
    config = demo_config()
    found = evaluate(config, estimate(75.0, rating=4.53), [])
    assert [s.category for s in found] == ["品質"]
    assert found[0].tone == "alert"


def test_healthy_state_is_silent():
    assert evaluate(demo_config(), estimate(75.0, rating=4.7), []) == []


def test_low_confidence_competitor_momentum_is_not_reported():
    """母数の小さい施設は数件のレビューで大きく振れる。ここを鳴らすと誤報になる。"""
    config = demo_config()
    noisy = estimate(63.0, prev=52.7, is_own=False, conf="中", name="競合C")
    assert evaluate(config, estimate(75.0), [noisy]) == []

    confident = replace(noisy, confidence="高")
    found = evaluate(config, estimate(75.0), [confident])
    assert [s.category for s in found] == ["競合"]


def test_share_drop_is_reported():
    found = evaluate(demo_config(), estimate(75.0), [], prev_share=8.0)
    assert [s.category for s in found] == ["シェア"]


def test_upcoming_event_is_reported_once_inside_the_lead_window():
    config = demo_config()
    assert evaluate(config, estimate(75.0), [], today=date(2026, 9, 1)) == []
    found = evaluate(config, estimate(75.0), [], today=date(2026, 10, 10))
    assert [s.category for s in found] == ["需要暦"]


def test_stale_collection_is_reported():
    """取得が止まったら、判断を出す前にそれを言う。"""
    found = evaluate(
        demo_config(), estimate(75.0), [], today=date(2026, 8, 25), as_of=date(2026, 8, 5)
    )
    assert "収集" in {s.category for s in found}
