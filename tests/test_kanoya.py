"""推定パイプラインの検証。

中心は「ノイズを入れずに作った合成データから、真の稼働率を誤差なく復元できるか」。
ここが合わないと、実データでのズレがノイズなのか実装バグなのか切り分けられない。
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kanoya import config as config_mod
from kanoya import estimate, render, series, signals
from kanoya.store import PmsDay, Snapshot, SnapshotStore

ASOF = dt.date(2026, 8, 15)
OWN_ID = "own"
COMP_ID = "comp"


def make_config(**rule_overrides) -> config_mod.Config:
    own = config_mod.Property(OWN_ID, "自社 テスト宿", rooms=50, rating_floor=4.6, is_own=True)
    comp = config_mod.Property(COMP_ID, "競合テスト（レストラン併設）", rooms=50, restaurant_review_share=0.5)
    return config_mod.Config(
        own=own,
        comp_set=(comp,),
        area=config_mod.Area("テスト", 34.6, 135.8, 1000, ()),
        model=config_mod.Model(window_days=90, calibration_days=180, review_lag_days=10, unstable_tail_days=10),
        rules=config_mod.Rules(**rule_overrides),
        events=(),
        paths={},
    )


def make_store(reviews_per_day: int, first: dt.date, last: dt.date, rating: float = 4.8) -> SnapshotStore:
    """毎日きっかり N 件ずつ投稿される、ノイズのないスナップショット列。"""
    by_place: dict[str, dict[dt.date, Snapshot]] = {OWN_ID: {}, COMP_ID: {}}
    total = 1000
    d = first
    while d <= last:
        for pid in (OWN_ID, COMP_ID):
            by_place[pid][d] = Snapshot(pid, d, total, rating)
        total += reviews_per_day
        d += dt.timedelta(days=1)
    return SnapshotStore(by_place)


def make_pms(sold: int, first: dt.date, last: dt.date) -> dict[dt.date, PmsDay]:
    out = {}
    d = first
    while d <= last:
        out[d] = PmsDay(d, sold, 50)
        d += dt.timedelta(days=1)
    return out


class SeriesTest(unittest.TestCase):
    def test_increments_spread_over_gaps(self):
        counts = {dt.date(2026, 1, 1): 10, dt.date(2026, 1, 5): 18}
        inc = series.increments(counts)
        # 4日ぶんの欠測に 8 件を均等配分する
        self.assertEqual(len(inc), 4)
        self.assertAlmostEqual(sum(inc.values()), 8.0)
        self.assertAlmostEqual(inc[dt.date(2026, 1, 3)], 2.0)

    def test_negative_increments_clamped(self):
        """Google側の削除で累計が減っても、負の需要にはしない。"""
        counts = {dt.date(2026, 1, 1): 20, dt.date(2026, 1, 2): 17, dt.date(2026, 1, 3): 19}
        inc = series.increments(counts)
        self.assertEqual(inc[dt.date(2026, 1, 2)], 0.0)
        self.assertEqual(inc[dt.date(2026, 1, 3)], 2.0)

    def test_first_snapshot_has_no_increment(self):
        counts = {dt.date(2026, 1, 1): 10, dt.date(2026, 1, 2): 12}
        self.assertNotIn(dt.date(2026, 1, 1), series.increments(counts))

    def test_shift_back_moves_to_stay_date(self):
        s = {dt.date(2026, 1, 20): 3.0}
        self.assertEqual(series.shift_back(s, 10), {dt.date(2026, 1, 10): 3.0})

    def test_rolling_sum(self):
        s = {dt.date(2026, 1, i): 1.0 for i in range(1, 11)}
        got = series.rolling_sum(s, [dt.date(2026, 1, 10)], 7)
        self.assertAlmostEqual(got[0], 7.0)


class RecoveryTest(unittest.TestCase):
    """ノイズなしなら真値をぴたりと復元できること。"""

    def setUp(self):
        self.cfg = make_config()
        self.store = make_store(4, dt.date(2025, 9, 1), ASOF)
        self.pms = make_pms(40, dt.date(2025, 9, 1), ASOF)

    def test_calibration_recovers_true_rate(self):
        cal = estimate.calibrate(self.cfg, self.store, self.pms, ASOF)
        # 1日 40室・4件 → 0.10 件/室
        self.assertAlmostEqual(cal.rate, 0.10, places=6)
        self.assertEqual(cal.days, 180)
        self.assertEqual(cal.sold_rooms, 7200)
        self.assertLess(cal.ci_low, cal.rate)
        self.assertGreater(cal.ci_high, cal.rate)

    def test_backtest_error_is_zero_without_noise(self):
        cal = estimate.calibrate(self.cfg, self.store, self.pms, ASOF)
        self.assertIsNotNone(cal.mean_abs_error_pt)
        self.assertLess(cal.mean_abs_error_pt, 1e-6)
        self.assertEqual(cal.usable_for, "水準の議論に使える")

    def test_occupancy_recovered(self):
        cal = estimate.calibrate(self.cfg, self.store, self.pms, ASOF)
        win = estimate.analysis_window(self.cfg, ASOF)
        est = estimate.estimate_property(self.cfg, self.store, self.cfg.own, win, cal.rate)
        self.assertAlmostEqual(est.occupancy, 0.80, places=6)
        self.assertAlmostEqual(est.sold_rooms, 3600.0, places=3)

    def test_restaurant_share_is_deducted(self):
        """同じ投稿量でも、外来客が半分なら販売室数は半分と読む。"""
        cal = estimate.calibrate(self.cfg, self.store, self.pms, ASOF)
        win = estimate.analysis_window(self.cfg, ASOF)
        own = estimate.estimate_property(self.cfg, self.store, self.cfg.own, win, cal.rate)
        comp = estimate.estimate_property(self.cfg, self.store, self.cfg.comp_set[0], win, cal.rate)
        self.assertAlmostEqual(comp.sold_rooms, own.sold_rooms / 2, places=3)

    def test_occupancy_capped_at_one(self):
        store = make_store(40, dt.date(2025, 9, 1), ASOF)   # あり得ない投稿量
        cal = estimate.calibrate(self.cfg, self.store, self.pms, ASOF)
        win = estimate.analysis_window(self.cfg, ASOF)
        est = estimate.estimate_property(self.cfg, store, self.cfg.own, win, cal.rate)
        self.assertEqual(est.occupancy, 1.0)

    def test_unstable_tail_is_excluded(self):
        win = estimate.analysis_window(self.cfg, ASOF)
        self.assertEqual(win.end, ASOF - dt.timedelta(days=10))
        self.assertEqual(win.days, 90)


class SignificanceTest(unittest.TestCase):
    def _est(self, raw: float, raw_prev: float) -> estimate.PropertyEstimate:
        prop = config_mod.Property("x", "x", rooms=5)
        return estimate.PropertyEstimate(
            prop=prop, reviews_raw=raw, reviews_raw_prev=raw_prev, reviews_effective=raw,
            sold_rooms=0, occupancy=0.75, prev_occupancy=0.70, confidence="高",
        )

    def test_small_swing_is_not_significant(self):
        # 5室規模の典型：63件 vs 58件。見た目は +8pt でも件数の揺らぎの範囲。
        self.assertFalse(self._est(63, 58).delta_significant)

    def test_large_swing_is_significant(self):
        self.assertTrue(self._est(400, 250).delta_significant)

    def test_standard_error_shrinks_with_volume(self):
        self.assertGreater(self._est(60, 60).occupancy_se_pt, self._est(600, 600).occupancy_se_pt)

    def test_no_reviews_is_maximally_uncertain(self):
        self.assertEqual(self._est(0, 0).occupancy_se_pt, 100.0)
        self.assertFalse(self._est(0, 0).delta_significant)


class WilsonTest(unittest.TestCase):
    def test_known_interval(self):
        lo, hi = estimate.wilson_interval(50, 500)
        self.assertAlmostEqual(lo, 0.07665, places=4)
        self.assertAlmostEqual(hi, 0.12942, places=4)

    def test_zero_trials(self):
        self.assertEqual(estimate.wilson_interval(0, 0), (0.0, 0.0))


class VerdictTest(unittest.TestCase):
    def _report(self, own_occ: float, comp_occ: float, rating: float, raw: float = 4000) -> estimate.Report:
        cfg = make_config()
        own = estimate.PropertyEstimate(
            prop=cfg.own, reviews_raw=raw, reviews_raw_prev=raw, reviews_effective=raw,
            sold_rooms=0, occupancy=own_occ, prev_occupancy=own_occ, confidence="高",
        )
        comp = estimate.PropertyEstimate(
            prop=cfg.comp_set[0], reviews_raw=raw, reviews_raw_prev=raw, reviews_effective=raw,
            sold_rooms=0, occupancy=comp_occ, prev_occupancy=comp_occ, confidence="高",
        )
        win = estimate.analysis_window(cfg, ASOF)
        cal = estimate.Calibration(0.1, 0.09, 0.11, 180, 7200, 720, 720, 1.0, 6)
        return estimate.Report(
            asof=ASOF, window=win, calibration=cal, estimates=(own, comp), own=own,
            comp_median_occupancy=comp_occ, area_reviews=1000, own_reviews=raw, own_rating=rating,
            ribbon_dates=(), ribbon_area=(), ribbon_own=(),
        )

    def test_same_level_holds(self):
        v = signals.decide(make_config(), self._report(0.75, 0.74, 4.8))
        self.assertEqual(v.level, "据え置き")

    def test_strong_market_raises(self):
        v = signals.decide(make_config(), self._report(0.85, 0.70, 4.8))
        self.assertEqual(v.level, "引き上げ検討")

    def test_rating_floor_freezes_a_raise(self):
        """評価が下限割れなら、需要が上でも引き上げない。"""
        cfg = make_config()
        v = signals.decide(cfg, self._report(0.85, 0.70, 4.5))
        self.assertIn("凍結", v.level)
        self.assertEqual(v.tone, "alert")

    def test_weak_market_cuts(self):
        v = signals.decide(make_config(), self._report(0.55, 0.75, 4.8))
        self.assertEqual(v.level, "引き下げ検討")

    def test_band_widens_when_reviews_are_scarce(self):
        """件数が少ない日は帯が広がり、同じ差でも動かさない。"""
        cfg = make_config()
        self.assertEqual(signals.decide(cfg, self._report(0.85, 0.70, 4.8)).level, "引き上げ検討")
        self.assertEqual(signals.decide(cfg, self._report(0.85, 0.70, 4.8, raw=25)).level, "据え置き")

    def test_quality_signal_fires_below_floor(self):
        cfg = make_config()
        sigs = signals.collect(cfg, self._report(0.75, 0.74, 4.5), {}, {})
        self.assertTrue(any(s.category == "品質" and s.level == "要対応" for s in sigs))

    def test_no_signal_when_healthy(self):
        cfg = make_config()
        self.assertEqual(signals.collect(cfg, self._report(0.75, 0.74, 4.8), {}, {}), [])


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        store = make_store(4, dt.date(2025, 9, 1), ASOF)
        pms = make_pms(40, dt.date(2025, 9, 1), ASOF)
        self.report = estimate.build_report(self.cfg, store, pms, [OWN_ID, COMP_ID], ASOF)
        self.verdict = signals.decide(self.cfg, self.report)
        self.html = render.render(self.cfg, self.report, self.verdict, [])

    def test_sections_present(self):
        for heading in ("今日の判断", "需要リボン", "コンプセット推定稼働", "推定の土台", "シグナル", "前提と限界"):
            self.assertIn(heading, self.html)

    def test_ribbon_is_drawn(self):
        self.assertIn("<svg", self.html)
        self.assertIn("エリア需要と自社シェアの推移", self.html)

    def test_rate_limitation_is_stated(self):
        """レートではなく需要の推定である、という但し書きを落とさない。"""
        self.assertIn("推定するのは", self.html)
        self.assertIn("スクレイピング", self.html)

    def test_escapes_property_names(self):
        cfg = make_config()
        evil = config_mod.Property(OWN_ID, '<script>alert(1)</script>', rooms=50, is_own=True)
        cfg = config_mod.Config(
            own=evil, comp_set=cfg.comp_set, area=cfg.area, model=cfg.model,
            rules=cfg.rules, events=cfg.events, paths={},
        )
        html = render.render(cfg, self.report, self.verdict, [])
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_rounding_is_half_up(self):
        self.assertEqual(render._round(0.5), 1)
        self.assertEqual(render._round(1.5), 2)
        self.assertEqual(render._round(2.5), 3)


class FixtureTest(unittest.TestCase):
    """同梱フィクスチャで一気通貫に動くこと。"""

    def test_end_to_end(self):
        root = Path(__file__).resolve().parent.parent
        cfg = config_mod.load(root / "config.json")
        store = SnapshotStore.load(cfg.path("snapshots"))
        from kanoya.store import load_area_places, load_pms

        pms = load_pms(cfg.path("pms"))
        ids = [p["place_id"] for p in load_area_places(cfg.path("area_places"))]
        report = estimate.build_report(cfg, store, pms, ids, dt.date(2026, 8, 15))

        # 真値は 75.3%（tools/make_fixture.py 参照）。手法の誤差幅に収まっていること。
        self.assertAlmostEqual(report.own.occupancy, 0.753, delta=0.10)
        self.assertAlmostEqual(report.calibration.rate, 0.105, delta=0.02)
        self.assertEqual(report.calibration.days, 261)
        self.assertGreater(report.area_reviews, report.own_reviews)
        html = render.render(cfg, report, signals.decide(cfg, report), [])
        self.assertIn("鹿のや", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
