"""合成データを一周させる統合テスト。

demo.py は「投稿率 10.5% の世界」を作る。calibrate.py はそれを知らないまま
自社の PMS 実績から投稿率を推定し直す。両者が一致することが、
遅延の引き戻し・レストラン控除・窓の切り方が矛盾していないことの証明になる。
"""

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from kanoya import config as config_mod
from kanoya import demo, pipeline, render, store

AS_OF = date(2026, 8, 15)
ROOT = Path(__file__).resolve().parents[1]


class DemoPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        raw = config_mod.load(ROOT / "config.demo.json")
        # 一時ディレクトリに向け直して、リポジトリの data/ を汚さない。
        cls.config = config_mod.Config(
            own=raw.own,
            compset=raw.compset,
            estimation=raw.estimation,
            decision=raw.decision,
            demand_events=raw.demand_events,
            paths=config_mod.Paths(
                snapshots=cls.tmp / "snapshots.jsonl",
                pms_actuals=cls.tmp / "actuals.csv",
                output=cls.tmp / "index.html",
            ),
            area_label=raw.area_label,
        )
        snapshots, actuals = demo.generate(cls.config, as_of=AS_OF)
        store.append(cls.config.paths.snapshots, snapshots)
        demo.write_actuals(actuals, cls.config.paths.pms_actuals)
        cls.report = pipeline.build(cls.config, AS_OF)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_calibration_recovers_the_true_posting_rate(self):
        self.assertTrue(self.report.rate.measured)
        self.assertAlmostEqual(
            self.report.rate.rate, demo.TRUE_POSTING_RATE, delta=0.01
        )

    def test_true_rate_falls_inside_the_confidence_interval(self):
        self.assertLessEqual(self.report.rate.low, demo.TRUE_POSTING_RATE)
        self.assertGreaterEqual(self.report.rate.high, demo.TRUE_POSTING_RATE)

    def test_backtest_error_is_small_enough_for_level_talk(self):
        self.assertIsNotNone(self.report.backtest)
        self.assertTrue(self.report.backtest.usable_for_levels)

    def test_estimated_occupancy_tracks_own_actuals(self):
        # 自社については実績があるので、推定がどれだけ外れたか直接測れる。
        from kanoya import pms

        actuals = pms.load(self.config.paths.pms_actuals)
        actual = pms.occupancy_between(
            actuals, self.report.window.start, self.report.window.end
        )
        own = next(e for e in self.report.estimates if e.prop.is_own)
        self.assertLess(abs(own.occupancy - actual) * 100, 6.0)

    def test_every_property_gets_an_estimate(self):
        self.assertEqual(len(self.report.estimates), 6)
        for item in self.report.estimates:
            self.assertGreater(item.occupancy, 0.0)
            self.assertLessEqual(item.occupancy, 1.0)

    def test_estimates_are_sorted_by_occupancy(self):
        values = [e.occupancy for e in self.report.estimates]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_ribbon_series_covers_the_expected_span(self):
        self.assertEqual(len(self.report.series.days), 190)
        self.assertEqual(self.report.series.days[-1], date(2026, 8, 5))
        self.assertGreater(self.report.series.own_share, 0.0)
        self.assertLess(self.report.series.own_share, 1.0)

    def test_own_band_never_exceeds_the_area_total(self):
        for own, area in zip(self.report.series.own, self.report.series.area):
            self.assertLessEqual(own, area + 1e-9)

    def test_rating_floor_breach_is_reported(self):
        # demo の自社評価は 4.53、下限は 4.6。必ず要対応が立つ。
        self.assertLess(self.report.rating, self.config.decision.rating_floor)
        self.assertTrue(
            any(s.level == "要対応" and "下限" in s.title for s in self.report.signals)
        )


class RenderTest(DemoPipelineTest):
    def test_html_is_written_and_well_formed(self):
        path = render.write(self.report, self.config.paths.output)
        markup = path.read_text(encoding="utf-8")
        self.assertTrue(markup.startswith("<!DOCTYPE html>"))
        self.assertIn("</html>", markup)
        self.assertEqual(markup.count("<section"), markup.count("</section>"))
        self.assertEqual(markup.count("<svg"), markup.count("</svg>"))
        self.assertIn(self.config.own.label, markup)
        # 「需要であってレートではない」という但し書きは常に出す。
        self.assertIn("レートショッパー", markup)
        self.assertIn("スクレイピング", markup)

    def test_labels_are_escaped(self):
        hostile = config_mod.Config(
            own=config_mod.Property(
                label='<script>alert(1)</script>',
                place_id=self.config.own.place_id,
                rooms=self.config.own.rooms,
                restaurant_review_share=self.config.own.restaurant_review_share,
                is_own=True,
            ),
            compset=self.config.compset,
            estimation=self.config.estimation,
            decision=self.config.decision,
            demand_events=self.config.demand_events,
            paths=self.config.paths,
        )
        report = render.Report(
            config=hostile,
            as_of=self.report.as_of,
            window=self.report.window,
            estimates=self.report.estimates,
            verdict=self.report.verdict,
            signals=self.report.signals,
            series=self.report.series,
            rate=self.report.rate,
            backtest=self.report.backtest,
            rating=self.report.rating,
            own_review_total=self.report.own_review_total,
        )
        markup = render.render(report)
        self.assertNotIn("<script>alert(1)</script>", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_summary_names_the_verdict(self):
        lines = pipeline.summary_lines(self.report)
        self.assertTrue(any("判断:" in line for line in lines))


class EmptyLedgerTest(unittest.TestCase):
    def test_report_refuses_to_run_without_a_ledger(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            raw = config_mod.load(ROOT / "config.demo.json")
            config = config_mod.Config(
                own=raw.own,
                compset=raw.compset,
                paths=config_mod.Paths(
                    snapshots=tmp / "missing.jsonl",
                    pms_actuals=tmp / "missing.csv",
                    output=tmp / "index.html",
                ),
            )
            with self.assertRaises(RuntimeError) as ctx:
                pipeline.build(config, AS_OF)
            self.assertIn("台帳", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
