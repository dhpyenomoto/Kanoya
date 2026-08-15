import datetime as dt

import pytest

from kanoya.calibration import Backtest, BacktestBlock, PostingRate
from kanoya.config import Config, DemandEvent, Estimation, Property, Rules
from kanoya.estimate import Estimate, windows
from kanoya.signals import build_signals, build_verdict

D = dt.date
AS_OF = D(2026, 8, 15)
CURRENT, PRIOR = windows(AS_OF, 90, 10)
RATE = PostingRate(point=0.1916, low=0.1676, high=0.2182, days=261, rooms_sold=929, reviews=178.0)
CLEAN = Backtest(blocks=(BacktestBlock(CURRENT.start, CURRENT.end, 0.75, 0.752),))


def _config(events=(), **rules):
    own = Property(key="own", name="鹿のや", place_id="p0", rooms=5, is_own=True)
    comps = tuple(
        Property(key=f"c{i}", name=f"競合{i}", place_id=f"p{i}", rooms=10) for i in range(1, 4)
    )
    return Config(
        own=own,
        competitors=comps,
        estimation=Estimation(confidence_high_rse=0.15, confidence_medium_rse=0.25),
        rules=Rules(**rules),
        events=tuple(events),
    )


def _est(prop, occupancy, change=None, reviews=200.0, rse=0.10):
    prior = None if change is None else occupancy / (1 + change / 100.0)
    return Estimate(
        prop=prop,
        reviews=reviews,
        rooms_sold=occupancy * prop.rooms * 90,
        occupancy=occupancy,
        prior_occupancy=prior,
        relative_se=rse,
        coverage=1.0,
    )


def _set(own_occ, comp_occs, **kwargs):
    cfg = _config(**kwargs)
    estimates = [_est(cfg.own, own_occ)]
    estimates += [_est(p, occ) for p, occ in zip(cfg.competitors, comp_occs)]
    return cfg, estimates


def test_small_gaps_hold_because_they_are_inside_the_noise():
    cfg, estimates = _set(0.75, [0.77, 0.74, 0.72])
    verdict = build_verdict(cfg, estimates, rate_blocked=False, low_confidence=False)
    assert verdict.level == "据え置き"
    assert verdict.tone == "hold"


def test_a_clear_lead_opens_the_raise():
    cfg, estimates = _set(0.88, [0.72, 0.70, 0.68])
    assert build_verdict(cfg, estimates, False, False).level == "強気"


def test_a_clear_lag_calls_for_demand_capture_not_a_price_cut():
    cfg, estimates = _set(0.55, [0.75, 0.74, 0.72])
    verdict = build_verdict(cfg, estimates, False, False)
    assert verdict.level == "需要獲得"
    assert "レートより先に" in verdict.detail


def test_the_quality_gate_outranks_a_raise():
    cfg, estimates = _set(0.88, [0.72, 0.70, 0.68])
    verdict = build_verdict(cfg, estimates, rate_blocked=True, low_confidence=False)
    assert verdict.level == "据え置き"
    assert "凍結" in verdict.headline


def test_low_confidence_blocks_any_move():
    cfg, estimates = _set(0.88, [0.72, 0.70, 0.68])
    verdict = build_verdict(cfg, estimates, rate_blocked=False, low_confidence=True)
    assert verdict.level == "据え置き"
    assert "精度" in verdict.headline


def _signals(cfg, estimates, *, rating=4.7, area=None, share=None, checks=CLEAN):
    return build_signals(cfg, estimates, RATE, checks, rating, area, share, AS_OF, CURRENT)


def test_rating_below_the_floor_raises_an_alert():
    cfg, estimates = _set(0.75, [0.74, 0.73, 0.72])
    out = _signals(cfg, estimates, rating=4.53)
    assert any(s.category == "品質" and s.tone == "alert" for s in out)


def test_rating_at_the_floor_is_not_an_alert():
    cfg, estimates = _set(0.75, [0.74, 0.73, 0.72])
    assert not any(s.category == "品質" for s in _signals(cfg, estimates, rating=4.6))


def test_competitor_momentum_is_ignored_when_that_estimate_is_not_solid():
    # 小規模施設は推定が跳ねやすい。信頼度「高」でなければ反応しない。
    cfg = _config()
    estimates = [_est(cfg.own, 0.75)]
    estimates.append(_est(cfg.competitors[0], 0.64, change=19.0, reviews=44.0, rse=0.165))
    out = _signals(cfg, estimates)
    assert not any(s.category == "競合" for s in out)

    estimates[1] = _est(cfg.competitors[0], 0.64, change=19.0, reviews=300.0, rse=0.09)
    assert any(s.category == "競合" for s in _signals(cfg, estimates))


def test_share_loss_and_share_gain_are_distinguished():
    cfg, estimates = _set(0.75, [0.74, 0.73, 0.72])
    loss = _signals(cfg, estimates, share=-22.0)
    assert any(s.category == "シェア" and s.tone == "alert" for s in loss)
    gain = _signals(cfg, estimates, share=+22.0)
    assert any(s.category == "シェア" and s.tone == "up" for s in gain)
    assert not any(s.category == "シェア" for s in _signals(cfg, estimates, share=5.0))


def test_area_wide_softening_argues_against_cutting_price():
    cfg, estimates = _set(0.75, [0.74, 0.73, 0.72])
    out = _signals(cfg, estimates, area=-30.0)
    demand = [s for s in out if s.category == "需要"]
    assert demand and "値下げを急がない" in demand[0].detail


def test_events_surface_only_inside_the_lead_time():
    near = DemandEvent(date=AS_OF + dt.timedelta(days=20), name="正倉院展", lift_pct=30)
    far = DemandEvent(date=AS_OF + dt.timedelta(days=200), name="来春の桜", lift_pct=20)
    past = DemandEvent(date=AS_OF - dt.timedelta(days=5), name="済んだ祭", lift_pct=10)
    cfg, estimates = _set(0.75, [0.74, 0.73, 0.72], **{})
    cfg = Config(
        own=cfg.own,
        competitors=cfg.competitors,
        estimation=cfg.estimation,
        rules=cfg.rules,
        events=(past, near, far),
    )
    titles = [s.title for s in _signals(cfg, estimates)]
    assert any("正倉院展" in t for t in titles)
    assert not any("来春の桜" in t or "済んだ祭" in t for t in titles)


def test_a_weak_backtest_restricts_how_the_numbers_may_be_used():
    cfg, estimates = _set(0.75, [0.74, 0.73, 0.72])
    weak = Backtest(blocks=(BacktestBlock(CURRENT.start, CURRENT.end, 0.75, 0.61),))
    out = _signals(cfg, estimates, checks=weak)
    data = [s for s in out if s.category == "データ"]
    assert data and "方向感のみ" in data[0].detail


def test_alerts_sort_above_watches():
    cfg, estimates = _set(0.75, [0.74, 0.73, 0.72])
    out = _signals(cfg, estimates, rating=4.4, area=+40.0)
    assert [s.level for s in out] == sorted(
        [s.level for s in out], key=lambda x: {"要対応": 0, "注視": 1, "好機": 2}[x]
    )
