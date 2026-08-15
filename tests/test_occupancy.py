from __future__ import annotations

from datetime import date, timedelta

import pytest

from kanoya.calibration import ReviewRate, fixed_review_rate
from kanoya.config import Config, Property
from kanoya.occupancy import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_NONE,
    estimate_market,
    estimate_property,
)
from tests.conftest import make_snapshots

AS_OF = date(2026, 8, 15)
WINDOW_START = date(2026, 4, 28)
WINDOW_END = date(2026, 7, 26)
RATE = fixed_review_rate(0.10)


def _snapshots_for(property_id: str, per_day: float, days: int = 300) -> list:
    """as_of から遡って days 日ぶん、毎日一定数のレビューが立つ観測列を作る。"""
    start = AS_OF - timedelta(days=days)
    whole = int(per_day)
    remainder = per_day - whole
    daily = []
    carry = 0.0
    for _ in range(days):
        carry += remainder
        extra = int(carry)
        carry -= extra
        daily.append(whole + extra)
    return make_snapshots(property_id, start, daily)


def test_estimate_property_inverts_the_review_rate(config: Config, own_property: Property):
    """1 日 0.5 件、投稿率 10% なら 1 日 5 室売れている＝5 室なら満室。"""
    estimate = estimate_property(
        own_property,
        _snapshots_for("own", 0.5),
        config,
        RATE,
        WINDOW_START,
        WINDOW_END,
    )

    assert estimate.occupancy_pct == pytest.approx(100.0, abs=1.0)
    assert estimate.sold_rooms == pytest.approx(450, rel=0.02)


def test_estimate_property_respects_the_occupancy_ceiling(
    config: Config, own_property: Property
):
    estimate = estimate_property(
        own_property, _snapshots_for("own", 5.0), config, RATE, WINDOW_START, WINDOW_END
    )

    assert estimate.occupancy == 1.0


def test_estimate_property_scales_by_room_count(config: Config):
    """同じレビュー数なら、客室数が多い施設ほど稼働率は低く出る。"""
    small = Property(id="s", name="小", place_id="p", rooms=5)
    large = Property(id="l", name="大", place_id="p", rooms=20)
    snapshots = _snapshots_for("x", 0.2)

    small_est = estimate_property(small, snapshots, config, RATE, WINDOW_START, WINDOW_END)
    large_est = estimate_property(large, snapshots, config, RATE, WINDOW_START, WINDOW_END)

    assert small_est.occupancy_pct == pytest.approx(large_est.occupancy_pct * 4, rel=0.01)


def test_estimate_property_declines_below_the_minimum_sample(
    config: Config, own_property: Property
):
    estimate = estimate_property(
        own_property, _snapshots_for("own", 0.01), config, RATE, WINDOW_START, WINDOW_END
    )

    assert estimate.occupancy is None
    assert estimate.confidence == CONFIDENCE_NONE
    assert not estimate.is_actionable


def test_margin_shrinks_as_reviews_accumulate(config: Config):
    """区間の幅は件数の平方根で縮む。客室数が多いほど推定は締まる。"""
    thin = estimate_property(
        Property(id="t", name="薄", place_id="p", rooms=5),
        _snapshots_for("t", 0.2),
        config,
        RATE,
        WINDOW_START,
        WINDOW_END,
    )
    thick = estimate_property(
        Property(id="k", name="厚", place_id="p", rooms=40),
        _snapshots_for("k", 1.6),
        config,
        RATE,
        WINDOW_START,
        WINDOW_END,
    )

    assert thin.occupancy_pct == pytest.approx(thick.occupancy_pct, rel=0.02)
    assert thick.relative_margin_pt < thin.relative_margin_pt


def test_review_rate_uncertainty_widens_only_the_absolute_margin(
    config: Config, own_property: Property
):
    """投稿率の誤差は全施設に共通なので、比較用の区間には効かない。"""
    snapshots = _snapshots_for("own", 0.3)
    certain = estimate_property(
        own_property, snapshots, config, fixed_review_rate(0.10),
        WINDOW_START, WINDOW_END,
    )
    uncertain = estimate_property(
        own_property,
        snapshots,
        config,
        ReviewRate(rate=0.10, ci_low=0.07, ci_high=0.13, method="calibrated"),
        WINDOW_START,
        WINDOW_END,
    )

    assert uncertain.margin_pt > certain.margin_pt
    assert uncertain.relative_margin_pt == pytest.approx(certain.relative_margin_pt)


def test_confidence_follows_the_interval_width(config: Config):
    high = estimate_property(
        Property(id="h", name="大", place_id="p", rooms=60),
        _snapshots_for("h", 3.0),
        config,
        RATE,
        WINDOW_START,
        WINDOW_END,
    )
    low = estimate_property(
        Property(id="l", name="小", place_id="p", rooms=4),
        _snapshots_for("l", 0.08),
        config,
        RATE,
        WINDOW_START,
        WINDOW_END,
    )

    assert high.confidence == CONFIDENCE_HIGH
    assert low.confidence in (CONFIDENCE_LOW, CONFIDENCE_MEDIUM)
    assert high.relative_margin_pt < low.relative_margin_pt


def test_separated_from_requires_the_gap_to_exceed_the_noise(
    config: Config, own_property: Property
):
    estimate = estimate_property(
        own_property, _snapshots_for("own", 0.3), config, RATE, WINDOW_START, WINDOW_END
    )
    own_pct = estimate.occupancy_pct

    assert not estimate.separated_from(own_pct - estimate.relative_margin_pt / 2)
    assert estimate.separated_from(own_pct - estimate.relative_margin_pt * 2)


def test_momentum_needs_observations_covering_the_prior_window(
    config: Config, own_property: Property
):
    """観測開始前の期間をレビュー 0 件とみなすと、偽の急増が出る。"""
    short = estimate_property(
        own_property,
        _snapshots_for("own", 0.3, days=120),
        config,
        RATE,
        WINDOW_START,
        WINDOW_END,
    )
    long = estimate_property(
        own_property,
        _snapshots_for("own", 0.3, days=300),
        config,
        RATE,
        WINDOW_START,
        WINDOW_END,
    )

    assert short.momentum_pct is None
    assert long.momentum_pct == pytest.approx(0.0, abs=5.0)


def test_estimate_market_computes_share_and_median(config: Config):
    snapshots = {
        "own": _snapshots_for("own", 0.2),
        "comp_a": _snapshots_for("comp_a", 0.4),
        "comp_b": _snapshots_for("comp_b", 1.4),
    }

    market = estimate_market(config, snapshots, RATE, AS_OF)

    assert market.window_days == 90
    assert market.own_share == pytest.approx(0.2 / 2.0, rel=0.05)
    # 中央値は競合 2 件のみから取り、自社は含めない。
    competitor_values = sorted(e.occupancy_pct for e in market.competitors)
    assert market.competitor_median_occupancy_pct == pytest.approx(
        sum(competitor_values) / 2
    )


def test_estimate_market_ranks_unestimated_properties_last(config: Config):
    snapshots = {
        "own": _snapshots_for("own", 0.2),
        "comp_a": _snapshots_for("comp_a", 0.4),
        "comp_b": [],
    }

    market = estimate_market(config, snapshots, RATE, AS_OF)

    assert market.ranked[-1].prop.id == "comp_b"
    assert market.ranked[-1].occupancy is None


def test_area_momentum_needs_prior_observations(config: Config):
    """前期の観測が揃っていない施設があれば、市場全体の前期比は出さない。"""
    short = {
        "own": _snapshots_for("own", 0.2, days=120),
        "comp_a": _snapshots_for("comp_a", 0.4, days=120),
        "comp_b": _snapshots_for("comp_b", 1.4, days=120),
    }

    market = estimate_market(config, short, RATE, AS_OF)

    assert market.area_momentum_pct is None


def test_market_adjusted_momentum_removes_the_common_season(config: Config):
    """全施設が同じだけ落ちた期間では、市場調整後の前期比は 0 に寄る。

    奈良は桜と紅葉で市場が大きく動く。素の前期比を competitive な良し悪しと
    読むと、季節で全施設が揃って落ちた月に全行が赤くなる。
    """
    snapshots = {
        "own": _snapshots_for("own", 0.2),
        "comp_a": _snapshots_for("comp_a", 0.4),
        "comp_b": _snapshots_for("comp_b", 1.4),
    }

    market = estimate_market(config, snapshots, RATE, AS_OF)

    assert market.area_momentum_pct == pytest.approx(0.0, abs=5.0)
    for estimate in market.estimates:
        adjusted = market.market_adjusted_momentum_pct(estimate)
        assert adjusted == pytest.approx(0.0, abs=8.0)
