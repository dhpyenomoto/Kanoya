import unittest
from datetime import date

from kanoya.config import Config, Calibration, DemandEvent, Estimation, Property, Rules
from kanoya.decide import build_signals, build_verdict
from kanoya.estimate import Estimate
from pathlib import Path

AS_OF = date(2026, 8, 15)


def make_config(**rules) -> Config:
    own = Property(key="own", name="自社", place_id="", rooms=5, is_own=True)
    return Config(
        root=Path("."),
        area_name="テスト",
        own=own,
        comp_set=(),
        estimation=Estimation(),
        calibration=Calibration(),
        rules=Rules(**rules),
        demand_events=(DemandEvent(date(2026, 10, 25), "正倉院展", 30),),
    )


def est(occupancy, *, is_own=False, band_pt=2.0, key=None, **kw) -> Estimate:
    half = band_pt / 100.0
    base = dict(
        key=key or ("own" if is_own else f"c{occupancy}"),
        name="自社" if is_own else "競合",
        rooms=5,
        is_own=is_own,
        reviews=100.0,
        rooms_sold=100.0,
        occupancy=occupancy,
        occupancy_low=occupancy - half,
        occupancy_high=occupancy + half,
        prev_occupancy=None,
        delta_pct=None,
        coverage=1.0,
        confidence="高",
        capped=False,
        rating=None,
        review_count=None,
    )
    base.update(kw)
    return Estimate(**base)


class VerdictTest(unittest.TestCase):
    def test_parity_holds(self):
        v = build_verdict([est(0.75, is_own=True), est(0.74)], make_config())
        self.assertEqual(v.kind, "hold")
        self.assertEqual(v.label, "据え置き")
        self.assertFalse(v.alert)

    def test_clear_outperformance_raises(self):
        v = build_verdict([est(0.85, is_own=True), est(0.70)], make_config())
        self.assertEqual(v.kind, "raise")

    def test_clear_underperformance_cuts(self):
        v = build_verdict([est(0.55, is_own=True), est(0.75)], make_config())
        self.assertEqual(v.kind, "cut")

    def test_rating_below_floor_freezes_a_raise(self):
        v = build_verdict(
            [est(0.85, is_own=True, rating=4.4), est(0.70)],
            make_config(rating_floor=4.6),
        )
        self.assertEqual(v.kind, "hold")
        self.assertTrue(v.alert)
        self.assertIn("凍結", v.label)

    def test_rating_below_floor_does_not_block_a_cut(self):
        v = build_verdict(
            [est(0.55, is_own=True, rating=4.4), est(0.75)],
            make_config(rating_floor=4.6),
        )
        self.assertEqual(v.kind, "cut")

    def test_gap_inside_the_noise_band_does_not_move_the_rate(self):
        """5室規模の広い推定幅では、10pt の差でも根拠にならない。"""
        v = build_verdict(
            [est(0.80, is_own=True, band_pt=25.0), est(0.70, band_pt=20.0)],
            make_config(),
        )
        self.assertEqual(v.kind, "hold")
        self.assertIn("ノイズ幅", v.body)
        self.assertGreater(v.noise_pt, 10.0)

    def test_no_reviews_yet_suspends_the_decision(self):
        v = build_verdict(
            [est(0.0, is_own=True, reviews=0.0), est(0.0, reviews=0.0)], make_config()
        )
        self.assertEqual(v.label, "判断保留")
        self.assertTrue(v.alert)

    def test_missing_comp_set_suspends_the_decision(self):
        v = build_verdict([est(0.8, is_own=True)], make_config())
        self.assertEqual(v.label, "判断保留")
        self.assertTrue(v.alert)


class SignalTest(unittest.TestCase):
    def _titles(self, signals):
        return [s.title for s in signals]

    def test_rating_below_floor_raises_an_alert(self):
        sigs = build_signals(
            [est(0.75, is_own=True, rating=4.53), est(0.74)],
            make_config(rating_floor=4.6),
            as_of=AS_OF,
        )
        self.assertTrue(any(s.css == "alert" and s.category == "品質" for s in sigs))

    def test_stale_snapshots_raise_an_alert(self):
        sigs = build_signals(
            [est(0.75, is_own=True), est(0.74)],
            make_config(stale_snapshot_days=3),
            as_of=AS_OF,
            stale_days=9,
        )
        self.assertTrue(any(s.category == "データ" and s.css == "alert" for s in sigs))

    def test_competitor_momentum_is_flagged(self):
        sigs = build_signals(
            [est(0.75, is_own=True), est(0.74, delta_pct=22.0)],
            make_config(momentum_flag_pct=15.0),
            as_of=AS_OF,
        )
        self.assertTrue(any(s.category == "競合" for s in sigs))

    def test_holdout_divergence_beyond_the_band_is_an_alert(self):
        sigs = build_signals(
            [est(0.90, is_own=True, band_pt=5.0), est(0.74)],
            make_config(),
            as_of=AS_OF,
            own_actual_occupancy=0.70,
            own_model_occupancy=0.90,
        )
        self.assertTrue(any(s.category == "検証" and s.css == "alert" for s in sigs))

    def test_holdout_divergence_inside_the_band_is_not_reported(self):
        sigs = build_signals(
            [est(0.78, is_own=True, band_pt=25.0), est(0.74)],
            make_config(),
            as_of=AS_OF,
            own_actual_occupancy=0.70,
            own_model_occupancy=0.78,
        )
        self.assertFalse(any(s.category == "検証" for s in sigs))

    def test_upcoming_event_inside_the_horizon_is_surfaced(self):
        sigs = build_signals(
            [est(0.75, is_own=True), est(0.74)],
            make_config(event_horizon_days=90),
            as_of=AS_OF,
        )
        self.assertTrue(any(s.category == "需要暦" for s in sigs))

    def test_distant_event_is_not_surfaced(self):
        sigs = build_signals(
            [est(0.75, is_own=True), est(0.74)],
            make_config(event_horizon_days=10),
            as_of=AS_OF,
        )
        self.assertFalse(any(s.category == "需要暦" for s in sigs))

    def test_share_collapse_is_flagged(self):
        sigs = build_signals(
            [est(0.75, is_own=True), est(0.74)],
            make_config(),
            as_of=AS_OF,
            own_share=0.04,
            prev_own_share=0.07,
        )
        self.assertTrue(any(s.category == "シェア" for s in sigs))

    def test_quiet_day_still_says_something(self):
        sigs = build_signals(
            [est(0.75, is_own=True, rating=4.8), est(0.74)],
            make_config(event_horizon_days=1),
            as_of=AS_OF,
        )
        self.assertEqual(self._titles(sigs), ["ルール逸脱なし"])


if __name__ == "__main__":
    unittest.main()
