"""合成データを真の答えとして、パイプライン全体の当たり外れを測る。

ここが通らなければ、ダッシュボードに出る数字は読んではいけない。
"""

import dataclasses
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path

from kanoya.analysis import build_analysis
from kanoya.config import load_config
from kanoya.demo import generate, true_occupancy
from kanoya.render import render
from kanoya.store import SnapshotStore

AS_OF = date(2026, 8, 15)
CONFIG = Path(__file__).resolve().parent.parent / "config.json"


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cfg = dataclasses.replace(load_config(CONFIG), root=root)
        db, _ = generate(cfg, as_of=AS_OF)
        cls.cfg = cfg
        cls.db = db
        with SnapshotStore(db) as store:
            cls.analysis = build_analysis(cfg, store, as_of=AS_OF)
        cls.truth = true_occupancy(
            cfg, as_of=AS_OF, start=cls.analysis.window_start, end=cls.analysis.window_end
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_window_ends_before_the_unsettled_review_lag(self):
        an = self.analysis
        self.assertEqual((AS_OF - an.window_end).days, self.cfg.estimation.review_lag_days)
        self.assertEqual((an.window_end - an.window_start).days + 1, 90)

    def test_every_property_is_estimated(self):
        keys = {e.key for e in self.analysis.estimates}
        self.assertEqual(keys, {p.key for p in self.cfg.properties})

    def test_measured_rate_recovers_the_true_posting_rate(self):
        from kanoya.demo import REVIEW_RATE

        c = self.analysis.calibration
        self.assertTrue(c.measured)
        self.assertLess(abs(c.rate - REVIEW_RATE), 0.02)
        self.assertLessEqual(c.ci_low, REVIEW_RATE)
        self.assertGreaterEqual(c.ci_high, REVIEW_RATE)

    def test_estimates_are_not_systematically_biased(self):
        errors = [
            (e.occupancy - self.truth[e.key]) * 100 for e in self.analysis.estimates
        ]
        mean_error = sum(errors) / len(errors)
        self.assertLess(abs(mean_error), 8.0, f"平均誤差が大きすぎる: {mean_error:.1f}pt")

    def test_reported_band_actually_contains_the_truth(self):
        """区間が名ばかりでないことの確認。6施設中5施設以上が区間内に入る。"""
        inside = [
            e
            for e in self.analysis.estimates
            if e.occupancy_low <= self.truth[e.key] <= e.occupancy_high
        ]
        self.assertGreaterEqual(len(inside), len(self.analysis.estimates) - 1)

    def test_the_largest_property_is_estimated_tightly(self):
        """クチコミ件数が多い施設ほど誤差は小さい。40室の競合Dで検証する。"""
        big = max(self.analysis.estimates, key=lambda e: e.reviews)
        self.assertEqual(big.rooms, 40)
        self.assertLess(abs(big.occupancy - self.truth[big.key]) * 100, 6.0)
        self.assertLess(big.band_pt, 10.0)

    def test_small_property_is_honestly_labelled_low_confidence(self):
        own = next(e for e in self.analysis.estimates if e.is_own)
        self.assertIn(own.confidence, ("中", "低"))
        self.assertGreater(own.band_pt, 8.0)

    def test_area_share_is_between_zero_and_one(self):
        self.assertIsNotNone(self.analysis.own_share)
        self.assertTrue(0 < self.analysis.own_share < 1)
        self.assertGreater(self.analysis.area_reviews, self.analysis.own_reviews)

    def test_ribbon_has_one_point_per_day(self):
        an = self.analysis
        expected = (an.ribbon_end - an.ribbon_start).days + 1
        self.assertEqual(len(an.area_rolling), expected)
        self.assertEqual(len(an.own_rolling), expected)
        self.assertTrue(all(a >= o for a, o in zip(an.area_rolling, an.own_rolling)))

    def test_render_leaves_no_placeholder_behind(self):
        html = render(self.analysis)
        self.assertEqual(re.findall(r"\{\{[A-Z_]+\}\}", html), [])
        self.assertIn("奈良春日 鹿のや", html)
        self.assertIn("<svg", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))

    def test_render_is_deterministic(self):
        self.assertEqual(render(self.analysis), render(self.analysis))


class EmptyStoreTest(unittest.TestCase):
    """スナップショットが 1 件も無くても落ちない。導入初日の状態。"""

    def test_analysis_and_render_survive_an_empty_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dataclasses.replace(load_config(CONFIG), root=Path(tmp))
            with SnapshotStore(Path(tmp) / "empty.db") as store:
                an = build_analysis(cfg, store, as_of=AS_OF)
            self.assertEqual(an.calibration.measured, False)
            self.assertTrue(all(e.reviews == 0 for e in an.estimates))
            self.assertEqual(an.verdict.label, "判断保留")
            html = render(an)
            self.assertEqual(re.findall(r"\{\{[A-Z_]+\}\}", html), [])


if __name__ == "__main__":
    unittest.main()
