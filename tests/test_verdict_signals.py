from __future__ import annotations

from datetime import date, timedelta

from kanoya.config import Config
from kanoya.occupancy import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    MarketEstimate,
    PropertyEstimate,
)
from kanoya.signals import (
    LEVEL_ALERT,
    LEVEL_OPPORTUNITY,
    LEVEL_WATCH,
    collect_signals,
)
from kanoya.verdict import (
    ACTION_CUT,
    ACTION_DEFER,
    ACTION_HOLD,
    ACTION_RAISE,
    decide,
)

AS_OF = date(2026, 8, 15)


def _estimate(
    prop,
    occupancy_pct: float,
    *,
    confidence: str = CONFIDENCE_HIGH,
    margin_pt: float = 3.0,
    rating: float | None = 4.7,
    prior_pct: float | None = None,
) -> PropertyEstimate:
    return PropertyEstimate(
        prop=prop,
        occupancy=occupancy_pct / 100,
        sold_rooms=100.0,
        reviews_in_window=200.0,
        prior_occupancy=None if prior_pct is None else prior_pct / 100,
        confidence=confidence,
        margin_pt=margin_pt,
        relative_margin_pt=margin_pt,
        rating=rating,
    )


def _market(config: Config, own_pct: float, comp_pcts: list[float], **kwargs) -> MarketEstimate:
    estimates = [_estimate(config.own, own_pct, **kwargs)]
    estimates += [
        _estimate(prop, pct) for prop, pct in zip(config.competitors, comp_pcts)
    ]
    return MarketEstimate(
        as_of=AS_OF,
        window_start=AS_OF - timedelta(days=109),
        window_end=AS_OF - timedelta(days=20),
        estimates=estimates,
        own_share=0.06,
        area_reviews=1000.0,
        own_reviews=60.0,
    )


# --------------------------------------------------------------------------- #
# 判断
# --------------------------------------------------------------------------- #


def test_holds_when_level_matches_the_market(config: Config):
    verdict = decide(_market(config, 75, [74, 76]), config)

    assert verdict.action == ACTION_HOLD
    assert "同水準" in verdict.headline


def test_raises_when_clearly_above_the_market(config: Config):
    verdict = decide(_market(config, 88, [70, 72]), config)

    assert verdict.action == ACTION_RAISE
    assert verdict.css_class == "up"


def test_freezes_a_raise_while_the_rating_is_below_the_floor(config: Config):
    """値上げ余地があっても、評価が下限割れなら引き上げない。"""
    verdict = decide(_market(config, 88, [70, 72], rating=4.4), config)

    assert verdict.action == ACTION_HOLD
    assert "凍結" in verdict.body


def test_calls_for_analysis_when_clearly_below_the_market(config: Config):
    verdict = decide(_market(config, 55, [74, 76]), config)

    assert verdict.action == ACTION_CUT
    assert verdict.css_class == "alert"


def test_holds_when_the_gap_is_inside_the_noise(config: Config):
    """閾値を超える差でも、その差が計数誤差より小さければ動かさない。"""
    verdict = decide(_market(config, 88, [70, 72], margin_pt=30.0), config)

    assert verdict.action == ACTION_HOLD
    assert "誤差と区別できない" in verdict.headline


def test_defers_when_own_estimate_is_unreliable(config: Config):
    verdict = decide(_market(config, 75, [74, 76], confidence=CONFIDENCE_LOW), config)

    assert verdict.action == ACTION_DEFER


def test_defers_without_comparable_competitors(config: Config):
    market = MarketEstimate(
        as_of=AS_OF,
        window_start=AS_OF - timedelta(days=109),
        window_end=AS_OF - timedelta(days=20),
        estimates=[_estimate(config.own, 75)],
        own_share=1.0,
        area_reviews=60.0,
        own_reviews=60.0,
    )

    verdict = decide(market, config)

    assert verdict.action == ACTION_DEFER
    assert "競合" in verdict.headline


def test_holds_inside_the_dead_band_between_thresholds(config: Config):
    """据え置き帯は超えたが判断閾値には届かない差は、推移を見るだけ。"""
    verdict = decide(_market(config, 79, [74, 75]), config)

    assert verdict.action == ACTION_HOLD
    assert "推移" in verdict.body


# --------------------------------------------------------------------------- #
# シグナル
# --------------------------------------------------------------------------- #


def test_rating_below_floor_raises_an_alert(config: Config):
    signals = collect_signals(_market(config, 75, [74, 76], rating=4.53), config)
    rating_signals = [s for s in signals if s.category == "品質"]

    assert len(rating_signals) == 1
    assert rating_signals[0].level == LEVEL_ALERT
    assert "4.53" in rating_signals[0].body


def test_no_rating_signal_when_above_the_floor(config: Config):
    signals = collect_signals(_market(config, 75, [74, 76], rating=4.7), config)

    assert not [s for s in signals if s.category == "品質"]


def test_demand_gap_raises_an_alert(config: Config):
    signals = collect_signals(_market(config, 55, [74, 76]), config)
    demand = [s for s in signals if s.category == "需要"]

    assert demand and demand[0].level == LEVEL_ALERT


def test_demand_surplus_is_an_opportunity(config: Config):
    signals = collect_signals(_market(config, 90, [70, 72]), config)
    demand = [s for s in signals if s.category == "需要"]

    assert demand and demand[0].level == LEVEL_OPPORTUNITY


def test_share_contraction_is_flagged(config: Config):
    signals = collect_signals(_market(config, 75, [74, 76]), config, prior_share=0.09)
    share = [s for s in signals if s.category == "シェア"]

    assert share and share[0].level == LEVEL_WATCH


def test_stable_share_is_not_flagged(config: Config):
    signals = collect_signals(_market(config, 75, [74, 76]), config, prior_share=0.061)

    assert not [s for s in signals if s.category == "シェア"]


def test_low_confidence_properties_are_flagged(config: Config):
    signals = collect_signals(
        _market(config, 75, [74, 76], confidence=CONFIDENCE_LOW), config
    )
    data = [s for s in signals if s.category == "データ"]

    assert data and "鹿のや" in data[0].body


def test_upcoming_event_is_announced(config: Config):
    """正倉院展は 2026-10-25。71 日先なので既定の予告窓の外。"""
    market = _market(config, 75, [74, 76])
    near = MarketEstimate(
        as_of=date(2026, 10, 1),
        window_start=market.window_start,
        window_end=market.window_end,
        estimates=market.estimates,
        own_share=market.own_share,
        area_reviews=market.area_reviews,
        own_reviews=market.own_reviews,
    )

    assert not [s for s in collect_signals(market, config) if s.category == "需要暦"]

    event_signals = [s for s in collect_signals(near, config) if s.category == "需要暦"]
    assert event_signals and "正倉院展" in event_signals[0].title


def test_alerts_sort_ahead_of_watches(config: Config):
    signals = collect_signals(
        _market(config, 55, [74, 76], rating=4.4), config, prior_share=0.09
    )

    levels = [s.level for s in signals]
    assert levels == sorted(levels, key=lambda l: {LEVEL_ALERT: 0, LEVEL_OPPORTUNITY: 1}.get(l, 2))
    assert signals[0].level == LEVEL_ALERT
