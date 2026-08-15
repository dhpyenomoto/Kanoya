import unittest
from datetime import date, timedelta

from kanoya.calibrate import Actual, calibrate, load_actuals, wilson_interval
from kanoya.series import build_series
from kanoya.store import Snapshot

D = date(2026, 1, 1)


def constant_series(days: int, per_day: int, *, lag: int = 0):
    """毎日 per_day 件ずつ増えるスナップショット列。"""
    snaps = [
        Snapshot(property_key="own", taken_on=D + timedelta(days=i), user_rating_count=i * per_day)
        for i in range(days + 1)
    ]
    return build_series(snaps, property_key="own", review_lag_days=lag)


class WilsonTest(unittest.TestCase):
    def test_interval_brackets_the_point_estimate(self):
        lo, hi = wilson_interval(105, 1000)
        self.assertLess(lo, 0.105)
        self.assertGreater(hi, 0.105)

    def test_fewer_trials_widen_the_interval(self):
        narrow = wilson_interval(105, 1000)
        wide = wilson_interval(10.5, 100)
        self.assertGreater(wide[1] - wide[0], narrow[1] - narrow[0])

    def test_no_trials_gives_no_information(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))


class CalibrateTest(unittest.TestCase):
    def test_rate_is_reviews_over_rooms_sold(self):
        series = constant_series(200, 1)
        actuals = [Actual(D + timedelta(days=i), 10) for i in range(1, 201)]
        c = calibrate(series, actuals, rooms=5, default_rate=0.2)
        self.assertTrue(c.measured)
        self.assertAlmostEqual(c.rate, 0.1, places=6)
        self.assertEqual(c.overlap_days, 200)

    def test_falls_back_to_the_configured_rate_without_actuals(self):
        c = calibrate(constant_series(50, 1), [], rooms=5, default_rate=0.123)
        self.assertFalse(c.measured)
        self.assertAlmostEqual(c.rate, 0.123)
        self.assertIsNone(c.ci_low)
        self.assertEqual(c.usability, "方向性の議論のみ")

    def test_days_without_snapshot_backing_are_ignored(self):
        series = constant_series(50, 1)
        actuals = [Actual(D + timedelta(days=i), 10) for i in range(1, 400)]
        c = calibrate(series, actuals, rooms=5, default_rate=0.2)
        self.assertEqual(c.overlap_days, 50)

    def test_train_before_excludes_the_current_window(self):
        series = constant_series(200, 1)
        actuals = [Actual(D + timedelta(days=i), 10) for i in range(1, 201)]
        c = calibrate(
            series, actuals, rooms=5, default_rate=0.2, train_before=D + timedelta(days=101)
        )
        self.assertEqual(c.overlap_days, 100)

    def test_backtest_reports_error_in_points(self):
        series = constant_series(360, 1)
        actuals = [Actual(D + timedelta(days=i), 10) for i in range(1, 361)]
        c = calibrate(series, actuals, rooms=5, default_rate=0.2, bucket_days=90)
        self.assertIsNotNone(c.mae_pt)
        self.assertGreaterEqual(c.buckets, 3)
        # 完全に一定の需要なら leave-one-out でもほぼ誤差なく当たる。
        self.assertLess(c.mae_pt, 1.0)
        self.assertEqual(c.usability, "水準の議論に使える")


class LoadActualsTest(unittest.TestCase):
    def test_reads_date_and_rooms_sold(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.csv"
            path.write_text(
                "date,rooms_sold,rooms_available\n2026-01-02,4,5\n2026-01-01,3,5\n",
                encoding="utf-8",
            )
            rows = load_actuals(path)
        self.assertEqual([r.stay_date.day for r in rows], [1, 2])
        self.assertEqual(rows[0].rooms_sold, 3.0)


if __name__ == "__main__":
    unittest.main()
