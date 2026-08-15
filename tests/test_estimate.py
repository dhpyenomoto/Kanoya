import unittest
from datetime import date, timedelta

from kanoya import config as config_mod
from kanoya import estimate
from kanoya.calibrate import PostingRate, wilson_interval
from kanoya.store import Snapshot

BASE = {
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


def make_config(**overrides):
    raw = {**BASE, **overrides}
    return config_mod.from_dict(raw)


def linear_snapshots(place_id: str, per_day: float, days: int, start: date):
    """1 日あたり per_day 件ずつ増える台帳。"""
    out = []
    total = 1000.0
    for step in range(days + 1):
        out.append(
            Snapshot(start + timedelta(days=step), place_id, int(total), 4.8)
        )
        total += per_day
    return out


FLAT_RATE = PostingRate(0.10, 0.08, 0.12, 100, 1000, 100.0, measured=True)


class OccupancyTest(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.as_of = date(2026, 8, 15)
        self.window = estimate.analysis_window(self.config, self.as_of)

    def test_window_excludes_the_unstable_tail(self):
        # 直近 10 日は投稿が出そろっていないので窓に含めない。
        self.assertEqual(self.window.end, date(2026, 8, 5))
        self.assertEqual(self.window.days, 90)

    def test_occupancy_is_reviews_over_rate_over_rooms(self):
        # 1 日 0.5 件 ÷ 投稿率 0.10 = 5 室/日。5 室の宿なので稼働 100%。
        snapshots = linear_snapshots("own", 0.5, 400, date(2025, 8, 1))
        result = estimate.estimate_property(
            self.config.own, self.config, snapshots, FLAT_RATE, self.window
        )
        self.assertAlmostEqual(result.occupancy, 1.0, places=2)

    def test_occupancy_is_capped_at_one(self):
        snapshots = linear_snapshots("own", 5.0, 400, date(2025, 8, 1))
        result = estimate.estimate_property(
            self.config.own, self.config, snapshots, FLAT_RATE, self.window
        )
        self.assertEqual(result.occupancy, 1.0)

    def test_restaurant_share_is_deducted(self):
        config = make_config(
            property={**BASE["property"], "restaurant_review_share": 0.5}
        )
        snapshots = linear_snapshots("own", 0.5, 400, date(2025, 8, 1))
        result = estimate.estimate_property(
            config.own, config, snapshots, FLAT_RATE, self.window
        )
        # 外来客ぶんを半分落とすので稼働も半分になる。
        self.assertAlmostEqual(result.occupancy, 0.5, places=2)

    def test_uncovered_window_is_flagged_and_downgraded(self):
        # 台帳が窓の途中からしか無い場合、増分が多くても信頼度を落とす。
        snapshots = linear_snapshots("own", 2.0, 40, date(2026, 7, 1))
        result = estimate.estimate_property(
            self.config.own, self.config, snapshots, FLAT_RATE, self.window
        )
        self.assertFalse(result.covered)
        self.assertNotEqual(result.confidence, "高")


class ConfidenceTest(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.window = estimate.analysis_window(self.config, date(2026, 8, 15))

    def _confidence_for(self, per_day: float) -> str:
        snapshots = linear_snapshots("a", per_day, 400, date(2025, 8, 1))
        result = estimate.estimate_property(
            self.config.compset[0], self.config, snapshots, FLAT_RATE, self.window
        )
        return result.confidence

    def test_many_reviews_give_high_confidence(self):
        self.assertEqual(self._confidence_for(1.0), "高")   # 90件

    def test_a_handful_of_reviews_gives_low_confidence(self):
        self.assertEqual(self._confidence_for(0.1), "低")   # 9件

    def test_no_reviews_at_all_is_low(self):
        self.assertEqual(self._confidence_for(0.0), "低")


class CompsetMedianTest(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.window = estimate.analysis_window(self.config, date(2026, 8, 15))

    def _estimates(self, rates: dict[str, float]):
        snapshots = []
        for place_id, per_day in rates.items():
            snapshots += linear_snapshots(place_id, per_day, 400, date(2025, 8, 1))
        return estimate.estimate_all(self.config, snapshots, FLAT_RATE, self.window)

    def test_own_is_excluded_from_the_median(self):
        estimates = self._estimates(
            {"own": 0.5, "a": 1.0, "b": 0.9, "c": 0.8}
        )
        median = estimate.compset_median(estimates)
        # 競合 3 件の中央値。自社の 100% は入らない。
        self.assertAlmostEqual(median, 0.9, places=2)

    def test_low_confidence_properties_do_not_set_the_baseline(self):
        # 競合C はレビューが極端に少なく信頼度「低」。中央値から外れる。
        estimates = self._estimates(
            {"own": 0.5, "a": 1.0, "b": 0.9, "c": 0.01}
        )
        median = estimate.compset_median(estimates)
        self.assertAlmostEqual(median, 0.95, places=2)

    def test_area_adjustment_removes_the_common_seasonal_move(self):
        # 全施設が同じだけ落ちたとき、エリア調整後の前期比は 0 になる。
        # 稼働率の上限に当たらず、かつ信頼度「高」が出る水準を選ぶ。
        config = make_config(
            property={**BASE["property"], "rooms": 50},
            compset=[
                {"label": "競合A", "place_id": "a", "rooms": 50},
                {"label": "競合B", "place_id": "b", "rooms": 50},
                {"label": "競合C", "place_id": "c", "rooms": 50},
            ],
        )
        snapshots = []
        for place_id in ("own", "a", "b", "c"):
            rows = []
            total = 1000.0
            start = date(2025, 8, 1)
            for step in range(401):
                day = start + timedelta(days=step)
                rows.append(Snapshot(day, place_id, int(total), 4.8))
                # 推定窓の始点（step=280 = 2026-05-08）で一律に半減させる。
                total += 2.0 if step < 280 else 1.0
            snapshots += rows
        estimates = estimate.estimate_all(
            config, snapshots, FLAT_RATE,
            estimate.analysis_window(config, date(2026, 8, 15)),
        )
        for item in estimates:
            self.assertEqual(item.confidence, "高")
            self.assertLess(item.raw_delta_pct, -20)   # 素の前期比は大きく沈む
            self.assertAlmostEqual(item.delta_pct, 0.0, places=6)


class WilsonTest(unittest.TestCase):
    def test_interval_brackets_the_point_estimate(self):
        low, high = wilson_interval(100, 1000)
        self.assertLess(low, 0.10)
        self.assertGreater(high, 0.10)

    def test_interval_never_goes_negative(self):
        low, _ = wilson_interval(1, 1000)
        self.assertGreaterEqual(low, 0.0)

    def test_more_trials_narrow_the_interval(self):
        narrow = wilson_interval(1000, 10000)
        wide = wilson_interval(10, 100)
        self.assertLess(narrow[1] - narrow[0], wide[1] - wide[0])

    def test_no_trials_returns_zero_width(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
