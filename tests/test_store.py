import unittest
from datetime import date

from kanoya import store
from kanoya.store import Snapshot


def snap(day: str, count: int, place_id: str = "p", rating: float | None = None):
    return Snapshot(date.fromisoformat(day), place_id, count, rating)


class DailyReviewsTest(unittest.TestCase):
    def test_delta_is_attributed_to_the_observation_day(self):
        daily = store.daily_reviews(
            [snap("2026-01-01", 100), snap("2026-01-02", 103)], "p"
        )
        self.assertEqual(daily, {date(2026, 1, 2): 3.0})

    def test_gaps_are_spread_evenly(self):
        daily = store.daily_reviews(
            [snap("2026-01-01", 100), snap("2026-01-05", 108)], "p"
        )
        self.assertEqual(len(daily), 4)
        for value in daily.values():
            self.assertAlmostEqual(value, 2.0)

    def test_lag_shifts_deltas_back_to_the_stay_date(self):
        daily = store.daily_reviews(
            [snap("2026-01-01", 100), snap("2026-01-02", 105)], "p", lag_days=10
        )
        self.assertEqual(daily, {date(2025, 12, 23): 5.0})

    def test_count_decreases_are_treated_as_zero(self):
        # Google 側でレビューが削除・統合されると累計が減ることがある。
        daily = store.daily_reviews(
            [snap("2026-01-01", 100), snap("2026-01-02", 96)], "p"
        )
        self.assertEqual(daily, {})

    def test_same_day_duplicate_keeps_last_observation(self):
        ordered = store.series(
            [snap("2026-01-01", 100), snap("2026-01-01", 104)], "p"
        )
        self.assertEqual([s.user_rating_count for s in ordered], [104])

    def test_other_places_are_ignored(self):
        daily = store.daily_reviews(
            [snap("2026-01-01", 100, "a"), snap("2026-01-02", 200, "b")], "a"
        )
        self.assertEqual(daily, {})


class WindowSumTest(unittest.TestCase):
    def test_both_endpoints_are_included(self):
        daily = {
            date(2026, 1, 1): 1.0,
            date(2026, 1, 2): 2.0,
            date(2026, 1, 3): 4.0,
        }
        total = store.window_sum(daily, date(2026, 1, 1), date(2026, 1, 3))
        self.assertEqual(total, 7.0)

    def test_missing_days_count_as_zero(self):
        total = store.window_sum({}, date(2026, 1, 1), date(2026, 1, 10))
        self.assertEqual(total, 0.0)


class RoundTripTest(unittest.TestCase):
    def test_json_round_trip(self):
        original = snap("2026-01-01", 100, "p", 4.53)
        restored = Snapshot.from_json(original.to_json())
        self.assertEqual(original, restored)

    def test_rating_is_optional(self):
        restored = Snapshot.from_json(snap("2026-01-01", 100).to_json())
        self.assertIsNone(restored.rating)


if __name__ == "__main__":
    unittest.main()
