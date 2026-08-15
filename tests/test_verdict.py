import unittest
from datetime import date

from kanoya import config as config_mod
from kanoya import verdict
from kanoya.calibrate import BacktestResult, PostingRate
from kanoya.config import Property
from kanoya.estimate import AreaSeries, PropertyEstimate

RATE = PostingRate(0.10, 0.08, 0.12, 200, 2000, 200.0, measured=True)
GOOD_BACKTEST = BacktestResult(6, 2.3, 4.0, [])
BAD_BACKTEST = BacktestResult(6, 9.1, 14.0, [])


def make_config():
    return config_mod.from_dict(
        {
            "property": {
                "label": "自社",
                "place_id": "own",
                "rooms": 5,
                "rating_floor": 4.6,
            },
            "compset": [
                {"label": "競合A", "place_id": "a", "rooms": 10},
                {"label": "競合B", "place_id": "b", "rooms": 10},
                {"label": "競合C", "place_id": "c", "rooms": 10},
            ],
        }
    )


def estimate_for(
    place_id: str,
    occupancy: float,
    *,
    is_own: bool = False,
    confidence: str = "高",
    delta: float | None = None,
) -> PropertyEstimate:
    prop = Property(label=place_id, place_id=place_id, rooms=10, is_own=is_own)
    prior = None if delta is None else occupancy / (1 + delta / 100)
    return PropertyEstimate(
        prop=prop,
        raw_reviews=100.0,
        adjusted_reviews=100.0,
        rooms_sold=1000.0,
        occupancy=occupancy,
        prior_occupancy=prior,
        confidence=confidence,
        rse=0.1,
        covered=True,
        area_delta_pct=0.0,
    )


def scenario(own_occ: float, comp_occs: list[float], **own_kwargs):
    return [
        estimate_for("own", own_occ, is_own=True, **own_kwargs),
        *[
            estimate_for(chr(ord("a") + i), occ)
            for i, occ in enumerate(comp_occs)
        ],
    ]


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.config = make_config()

    def test_matching_the_market_holds(self):
        result = verdict.decide(
            self.config, scenario(0.75, [0.77, 0.74, 0.72]), 4.8, GOOD_BACKTEST
        )
        self.assertEqual(result.level, "据え置き")
        self.assertEqual(result.tone, "hold")

    def test_clearly_ahead_of_the_market_raises(self):
        result = verdict.decide(
            self.config, scenario(0.88, [0.72, 0.70, 0.68]), 4.8, GOOD_BACKTEST
        )
        self.assertEqual(result.level, "引き上げ")
        self.assertEqual(result.tone, "up")

    def test_clearly_behind_the_market_considers_lowering(self):
        result = verdict.decide(
            self.config, scenario(0.55, [0.78, 0.76, 0.74]), 4.8, GOOD_BACKTEST
        )
        self.assertEqual(result.level, "引き下げ検討")

    def test_rating_below_the_floor_freezes_a_raise(self):
        # 引き上げ条件を満たしていても、評価が下限割れなら上げない。
        result = verdict.decide(
            self.config, scenario(0.88, [0.72, 0.70, 0.68]), 4.53, GOOD_BACKTEST
        )
        self.assertEqual(result.level, "据え置き")
        self.assertEqual(result.tone, "alert")
        self.assertIn("4.53", result.detail)

    def test_rating_below_the_floor_does_not_block_lowering(self):
        # 凍結するのは引き上げだけ。値下げ判断まで止めてはいけない。
        result = verdict.decide(
            self.config, scenario(0.55, [0.78, 0.76, 0.74]), 4.53, GOOD_BACKTEST
        )
        self.assertEqual(result.level, "引き下げ検討")

    def test_low_confidence_on_own_suspends_the_call(self):
        estimates = scenario(0.88, [0.72, 0.70, 0.68], confidence="低")
        result = verdict.decide(self.config, estimates, 4.8, GOOD_BACKTEST)
        self.assertEqual(result.level, "判断保留")

    def test_no_usable_comps_suspends_the_call(self):
        estimates = [
            estimate_for("own", 0.75, is_own=True),
            *[estimate_for(p, 0.7, confidence="低") for p in ("a", "b", "c")],
        ]
        result = verdict.decide(self.config, estimates, 4.8, GOOD_BACKTEST)
        self.assertEqual(result.level, "判断保留")

    def test_poor_backtest_blocks_level_based_raises(self):
        # 水準が信用できないなら、+16pt 開いていても引き上げに使わない。
        result = verdict.decide(
            self.config, scenario(0.88, [0.72, 0.70, 0.68]), 4.8, BAD_BACKTEST
        )
        self.assertEqual(result.level, "据え置き")
        self.assertIn("9.1pt", result.detail)

    def test_poor_backtest_still_reacts_to_momentum(self):
        estimates = scenario(0.60, [0.72, 0.70, 0.68], delta=-25)
        result = verdict.decide(self.config, estimates, 4.8, BAD_BACKTEST)
        self.assertEqual(result.level, "引き下げ検討")

    def test_missing_rating_does_not_block_a_raise(self):
        result = verdict.decide(
            self.config, scenario(0.88, [0.72, 0.70, 0.68]), None, GOOD_BACKTEST
        )
        self.assertEqual(result.level, "引き上げ")


def flat_series(days: int, area: float, own: float) -> AreaSeries:
    start = date(2026, 1, 1)
    from datetime import timedelta

    return AreaSeries(
        days=[start + timedelta(days=i) for i in range(days)],
        area=[area] * days,
        own=[own] * days,
    )


class SignalTest(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.as_of = date(2026, 8, 15)

    def _titles(self, signals):
        return [s.title for s in signals]

    def test_rating_below_floor_raises_a_signal(self):
        signals = verdict.build_signals(
            self.config, scenario(0.75, [0.74]), flat_series(90, 100, 6),
            4.53, RATE, GOOD_BACKTEST, self.as_of,
        )
        self.assertTrue(any("下限" in t for t in self._titles(signals)))

    def test_unmeasured_posting_rate_raises_a_signal(self):
        unmeasured = PostingRate(0.10, 0.07, 0.13, 0, 0, 0.0, measured=False)
        signals = verdict.build_signals(
            self.config, scenario(0.75, [0.74]), flat_series(90, 100, 6),
            4.8, unmeasured, GOOD_BACKTEST, self.as_of,
        )
        self.assertTrue(any("投稿率が未実測" in t for t in self._titles(signals)))

    def test_share_of_voice_drop_raises_a_signal(self):
        series = flat_series(90, 100, 8)
        # 直近 30 日だけシェアを半分に落とす。
        series = AreaSeries(
            days=series.days,
            area=series.area,
            own=series.own[:-30] + [3.0] * 30,
        )
        signals = verdict.build_signals(
            self.config, scenario(0.75, [0.74]), series,
            4.8, RATE, GOOD_BACKTEST, self.as_of,
        )
        self.assertTrue(any("シェア" in t for t in self._titles(signals)))

    def test_stable_share_raises_no_share_signal(self):
        signals = verdict.build_signals(
            self.config, scenario(0.75, [0.74]), flat_series(90, 100, 6),
            4.8, RATE, GOOD_BACKTEST, self.as_of,
        )
        self.assertFalse(any("シェア" in t for t in self._titles(signals)))

    def test_upcoming_demand_event_is_surfaced(self):
        config = config_mod.from_dict(
            {
                "property": {"label": "自社", "place_id": "own", "rooms": 5},
                "compset": [{"label": "競合A", "place_id": "a", "rooms": 10}],
                "demand_events": [
                    {"date": "2026-09-01", "label": "正倉院展", "lift": 30}
                ],
            }
        )
        signals = verdict.build_signals(
            config, scenario(0.75, [0.74]), flat_series(90, 100, 6),
            4.8, RATE, GOOD_BACKTEST, self.as_of,
        )
        self.assertTrue(any("正倉院展" in t for t in self._titles(signals)))

    def test_distant_demand_event_is_not_surfaced(self):
        config = config_mod.from_dict(
            {
                "property": {"label": "自社", "place_id": "own", "rooms": 5},
                "compset": [{"label": "競合A", "place_id": "a", "rooms": 10}],
                "demand_events": [
                    {"date": "2026-12-01", "label": "遠い行事", "lift": 30}
                ],
            }
        )
        signals = verdict.build_signals(
            config, scenario(0.75, [0.74]), flat_series(90, 100, 6),
            4.8, RATE, GOOD_BACKTEST, self.as_of,
        )
        self.assertFalse(any("遠い行事" in t for t in self._titles(signals)))


if __name__ == "__main__":
    unittest.main()
