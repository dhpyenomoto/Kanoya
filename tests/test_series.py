import unittest
from datetime import date, timedelta

from kanoya.series import build_series
from kanoya.store import Snapshot

D = date(2026, 1, 1)


def snaps(*pairs, key="p"):
    return [Snapshot(property_key=key, taken_on=d, user_rating_count=c) for d, c in pairs]


class BuildSeriesTest(unittest.TestCase):
    def test_daily_increment_is_the_difference_of_cumulative_counts(self):
        s = build_series(
            snaps((D, 100), (D + timedelta(days=1), 103)),
            property_key="p",
            review_lag_days=0,
        )
        self.assertAlmostEqual(s.daily[D + timedelta(days=1)], 3.0)

    def test_gap_between_snapshots_is_spread_evenly(self):
        s = build_series(
            snaps((D, 100), (D + timedelta(days=4), 108)),
            property_key="p",
            review_lag_days=0,
        )
        for i in range(1, 5):
            self.assertAlmostEqual(s.daily[D + timedelta(days=i)], 2.0)
        self.assertAlmostEqual(s.total(D, D + timedelta(days=4)), 8.0)

    def test_reviews_are_shifted_back_to_the_stay_date(self):
        s = build_series(
            snaps((D, 100), (D + timedelta(days=1), 101)),
            property_key="p",
            review_lag_days=10,
        )
        self.assertIn(D + timedelta(days=1) - timedelta(days=10), s.daily)
        self.assertNotIn(D + timedelta(days=1), s.daily)

    def test_restaurant_share_is_deducted(self):
        s = build_series(
            snaps((D, 0), (D + timedelta(days=1), 10)),
            property_key="p",
            review_lag_days=0,
            restaurant_review_share=0.2,
        )
        self.assertAlmostEqual(s.daily[D + timedelta(days=1)], 8.0)

    def test_google_deleting_reviews_does_not_produce_negative_demand(self):
        s = build_series(
            snaps((D, 100), (D + timedelta(days=1), 95), (D + timedelta(days=2), 97)),
            property_key="p",
            review_lag_days=0,
        )
        self.assertAlmostEqual(s.daily[D + timedelta(days=1)], 0.0)
        self.assertAlmostEqual(s.daily[D + timedelta(days=2)], 2.0)
        self.assertEqual(s.negative_deltas, 1)

    def test_coverage_reports_the_share_of_days_backed_by_snapshots(self):
        s = build_series(
            snaps((D, 0), (D + timedelta(days=4), 4)),
            property_key="p",
            review_lag_days=0,
        )
        # 1/1 はスナップショットの起点なので裏づけがない。1/2〜1/5 の 4 日だけ。
        self.assertAlmostEqual(s.coverage(D, D + timedelta(days=4)), 0.8)

    def test_single_snapshot_yields_no_series(self):
        s = build_series(snaps((D, 100)), property_key="p")
        self.assertEqual(s.daily, {})
        self.assertAlmostEqual(s.coverage(D, D + timedelta(days=30)), 0.0)

    def test_rolling_sum_uses_a_trailing_window(self):
        s = build_series(
            snaps((D, 0), (D + timedelta(days=10), 10)),
            property_key="p",
            review_lag_days=0,
        )
        roll = s.rolling(D + timedelta(days=5), D + timedelta(days=10), 7)
        # 1日1件が10日続く。窓が完全に埋まる 1/7 以降は 7 件で一定。
        self.assertAlmostEqual(roll[0], 5.0)
        self.assertAlmostEqual(roll[1], 6.0)
        self.assertTrue(all(abs(v - 7.0) < 1e-9 for v in roll[2:]))


if __name__ == "__main__":
    unittest.main()
