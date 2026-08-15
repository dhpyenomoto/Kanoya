import unittest
from datetime import date, timedelta

from kanoya.config import Property
from kanoya.estimate import (
    Estimate,
    comp_median,
    counting_band,
    estimate_property,
    noise_floor_pt,
)
from kanoya.series import build_series
from kanoya.store import Snapshot

D = date(2026, 1, 1)
END = D + timedelta(days=200)


def series_with(total_reviews: int, days: int = 201, lag: int = 0):
    """days 日かけて total_reviews 件が均等に積み上がる系列。"""
    snaps = [
        Snapshot(
            property_key="p",
            taken_on=D + timedelta(days=i),
            user_rating_count=round(i * total_reviews / (days - 1)),
        )
        for i in range(days)
    ]
    return build_series(snaps, property_key="p", review_lag_days=lag)


def make_estimate(**kw) -> Estimate:
    base = dict(
        key="x",
        name="x",
        rooms=10,
        is_own=False,
        reviews=100.0,
        rooms_sold=900.0,
        occupancy=0.7,
        occupancy_low=0.6,
        occupancy_high=0.8,
        prev_occupancy=None,
        delta_pct=None,
        coverage=1.0,
        confidence="中",
        capped=False,
        rating=None,
        review_count=None,
    )
    base.update(kw)
    return Estimate(**base)


class OccupancyMathTest(unittest.TestCase):
    def test_occupancy_is_reviews_over_rate_over_capacity(self):
        prop = Property(key="p", name="p", place_id="", rooms=5, is_own=True)
        # 窓 90日、5室 → 定員 450室。投稿率 10% で 36 件なら 360室 = 80%。
        est = estimate_property(
            prop,
            series_with(80),  # 200日で80件 → 90日窓では約36件
            window_end=END,
            window_days=90,
            review_rate=0.1,
        )
        self.assertAlmostEqual(est.reviews, 36.0, places=1)
        self.assertAlmostEqual(est.rooms_sold, 360.0, places=0)
        self.assertAlmostEqual(est.occupancy, 0.8, places=2)

    def test_occupancy_is_capped_at_full_and_flagged(self):
        prop = Property(key="p", name="p", place_id="", rooms=1, is_own=True)
        est = estimate_property(
            prop, series_with(400), window_end=END, window_days=90, review_rate=0.1
        )
        self.assertEqual(est.occupancy, 1.0)
        self.assertTrue(est.capped)

    def test_zero_rate_is_rejected(self):
        prop = Property(key="p", name="p", place_id="", rooms=5)
        with self.assertRaises(ValueError):
            estimate_property(
                prop, series_with(10), window_end=END, window_days=90, review_rate=0.0
            )


class BandTest(unittest.TestCase):
    def test_band_narrows_as_review_count_grows(self):
        few = counting_band(0.7, 25)
        many = counting_band(0.7, 2500)
        self.assertGreater(few[1] - few[0], many[1] - many[0])

    def test_no_reviews_means_no_information(self):
        self.assertEqual(counting_band(0.0, 0), (0.0, 1.0))

    def test_five_room_property_carries_a_wide_band(self):
        """5室・90日窓では 40 件前後しか集まらず、±20pt 級の幅になる。"""
        prop = Property(key="p", name="p", place_id="", rooms=5, is_own=True)
        est = estimate_property(
            prop, series_with(90), window_end=END, window_days=90, review_rate=0.105
        )
        self.assertGreater(est.band_pt, 15.0)
        self.assertEqual(est.confidence, "低")


class ConfidenceTest(unittest.TestCase):
    def test_narrow_band_and_full_coverage_gives_high(self):
        prop = Property(key="p", name="p", place_id="", rooms=200, is_own=False)
        est = estimate_property(
            prop, series_with(9000), window_end=END, window_days=90, review_rate=0.105
        )
        self.assertLess(est.band_pt, 8.0)
        self.assertEqual(est.confidence, "高")

    def test_unmeasured_calibration_downgrades_high_to_medium(self):
        prop = Property(key="p", name="p", place_id="", rooms=200, is_own=False)
        est = estimate_property(
            prop,
            series_with(9000),
            window_end=END,
            window_days=90,
            review_rate=0.105,
            calibration_measured=False,
        )
        self.assertEqual(est.confidence, "中")


class CompSetTest(unittest.TestCase):
    def test_median_excludes_own_property(self):
        ests = [
            make_estimate(key="own", is_own=True, occupancy=0.99),
            make_estimate(key="a", occupancy=0.6),
            make_estimate(key="b", occupancy=0.7),
            make_estimate(key="c", occupancy=0.8),
        ]
        self.assertAlmostEqual(comp_median(ests), 0.7)

    def test_median_of_even_comp_set_averages_the_middle_two(self):
        ests = [
            make_estimate(key="own", is_own=True),
            make_estimate(key="a", occupancy=0.6),
            make_estimate(key="b", occupancy=0.8),
        ]
        self.assertAlmostEqual(comp_median(ests), 0.7)

    def test_no_comps_means_no_median(self):
        self.assertIsNone(comp_median([make_estimate(is_own=True)]))

    def test_noise_floor_combines_own_and_comp_bands(self):
        ests = [
            make_estimate(key="own", is_own=True, occupancy_low=0.6, occupancy_high=0.8),
            make_estimate(key="a", occupancy=0.7, occupancy_low=0.65, occupancy_high=0.75),
        ]
        # 自社 ±10pt と競合 ±5pt の二乗和 → 約 11pt
        self.assertAlmostEqual(noise_floor_pt(ests), 11.18, places=1)


if __name__ == "__main__":
    unittest.main()
