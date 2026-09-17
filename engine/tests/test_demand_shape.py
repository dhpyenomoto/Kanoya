"""gap → z の写像（飽和関数と最小期待室数）の回帰テスト.

5室規模では、飽和点1.5室にすぐ到達して張り付く（実測で78セル中49セル）。
張り付くとOTBが何室でも同じ価格になり、満室に近づいても値上げできない。

一方、実測カーブでは長リードの期待室数が 0.05室まで下がる。実OTBは整数
しか取れないため、予約1件で価格寄与が +13% 跳ねる。これは減衰では
解決しない（離散化の問題なので係数を掛けても跳ね自体は残る）。
"""

from __future__ import annotations

import copy
import math
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import pace as pace_mod  # noqa: E402
from kanoya_rm.cli import build_context  # noqa: E402
from kanoya_rm.config import Settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class DemandShapeTest(unittest.TestCase):
    """飽和点を超えてもハードクリップしないこと."""

    def test_zero_gap_gives_zero_signal(self) -> None:
        self.assertEqual(pace_mod.demand_shape(0.0, 1.5), 0.0)

    def test_saturation_point_is_not_the_ceiling(self) -> None:
        """以前はここで ±1 に達し、その先が平らになっていた."""
        at_knee = pace_mod.demand_shape(1.5, 1.5)
        self.assertAlmostEqual(at_knee, math.tanh(1.0), places=6)
        self.assertLess(at_knee, 1.0)

    def test_it_keeps_rising_past_saturation(self) -> None:
        values = [pace_mod.demand_shape(g, 1.5) for g in (1.5, 2.0, 3.0, 4.0, 5.0)]
        for a, b in zip(values, values[1:]):
            self.assertGreater(b, a, "飽和点の先で頭打ちになっている")

    def test_it_stays_within_the_unit_range(self) -> None:
        """±1 を超えると term_clip の意味が壊れる."""
        for gap in (-50.0, -5.0, 5.0, 50.0):
            self.assertLessEqual(abs(pace_mod.demand_shape(gap, 1.5)), 1.0)

    def test_ahead_and_behind_are_treated_alike(self) -> None:
        """値上げ側だけ鈍らせない（奇関数であること）."""
        for gap in (0.5, 1.5, 3.0):
            self.assertAlmostEqual(pace_mod.demand_shape(gap, 1.5),
                                   -pace_mod.demand_shape(-gap, 1.5), places=9)

    def test_a_degenerate_saturation_does_not_explode(self) -> None:
        self.assertEqual(pace_mod.demand_shape(3.0, 0.0), 0.0)


class SettingsResolutionTest(unittest.TestCase):
    """飽和点・閾値が設定から読めること（コードに埋め込まない）."""

    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.coef = self.s.property["coefficients"]

    def test_both_keys_are_present_in_the_shipped_config(self) -> None:
        self.assertIn("pace_saturation_rooms", self.coef)
        self.assertIn("min_expected_rooms", self.coef)

    def test_changing_the_saturation_changes_the_signal(self) -> None:
        day = date(2027, 3, 15)
        as_of = day - timedelta(days=5)
        signals = {}
        for sat in (1.0, 1.5, 3.0):
            probe = copy.deepcopy(self.s)
            probe.property["coefficients"]["pace_saturation_rooms"] = sat
            signals[sat] = pace_mod.evaluate(probe, day, as_of, 5).raw_z
        # 飽和点が大きいほど、同じ gap に対するシグナルは弱くなる
        self.assertGreater(signals[1.0], signals[1.5])
        self.assertGreater(signals[1.5], signals[3.0])

    def test_ratio_form_scales_with_the_room_count(self) -> None:
        """5室と50室で同じ設定ファイルが使えること."""
        probe = copy.deepcopy(self.s)
        probe.property["coefficients"]["pace_saturation_rooms_ratio"] = 0.3
        cfg = pace_mod._demand_config(probe)
        self.assertAlmostEqual(cfg["saturation"], 0.3 * 5)

        big = copy.deepcopy(probe)
        big.property["property"]["rooms"] = 50
        self.assertAlmostEqual(pace_mod._demand_config(big)["saturation"], 15.0)

    def test_ratio_beats_the_absolute_value(self) -> None:
        probe = copy.deepcopy(self.s)
        probe.property["coefficients"]["pace_saturation_rooms"] = 1.5
        probe.property["coefficients"]["pace_saturation_rooms_ratio"] = 0.4
        self.assertAlmostEqual(pace_mod._demand_config(probe)["saturation"], 2.0)

    def test_defaults_apply_when_the_config_omits_them(self) -> None:
        probe = copy.deepcopy(self.s)
        for key in ("pace_saturation_rooms", "min_expected_rooms"):
            probe.property["coefficients"].pop(key, None)
        cfg = pace_mod._demand_config(probe)
        self.assertEqual(cfg["saturation"], pace_mod.DEFAULT_SATURATION_ROOMS)
        self.assertEqual(cfg["min_expected"], pace_mod.DEFAULT_MIN_EXPECTED_ROOMS)


class MinExpectedRoomsTest(unittest.TestCase):
    """期待室数が小さいリード帯では内部需要を使わないこと."""

    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.day = date(2027, 3, 15)
        while self.s.is_closed(self.day):
            self.day += timedelta(days=1)
        self.threshold = float(
            self.s.property["coefficients"]["min_expected_rooms"])

    def _at(self, lead: int, otb: int):
        return pace_mod.evaluate(self.s, self.day,
                                 self.day - timedelta(days=lead), otb)

    def test_thin_lead_bands_are_switched_off(self) -> None:
        far = self._at(60, 0)
        self.assertLess(far.expected_rooms, self.threshold)
        self.assertTrue(far.below_min_expected)
        self.assertEqual(far.raw_z, 0.0)
        self.assertEqual(far.z, 0.0)

    def test_one_booking_moves_nothing_in_a_thin_band(self) -> None:
        """予約1件で価格が跳ねるのが、無効化したかった症状そのもの."""
        for lead in (30, 45, 60, 90, 120):
            empty, one = self._at(lead, 0), self._at(lead, 1)
            self.assertEqual(empty.z, one.z,
                             f"リード{lead}日で予約1件がシグナルを動かしている")

    def test_the_signal_survives_where_the_data_is(self) -> None:
        """無効化しすぎて、直近まで効かなくなっていないこと."""
        near = self._at(3, 5)
        self.assertGreaterEqual(near.expected_rooms, self.threshold)
        self.assertFalse(near.below_min_expected)
        self.assertNotEqual(near.z, 0.0)

    def test_the_threshold_is_what_decides(self) -> None:
        lead = 30
        base = self._at(lead, 1)
        self.assertTrue(base.below_min_expected)

        probe = copy.deepcopy(self.s)
        probe.property["coefficients"]["min_expected_rooms"] = 0.0
        loosened = pace_mod.evaluate(probe, self.day,
                                     self.day - timedelta(days=lead), 1)
        self.assertFalse(loosened.below_min_expected)
        self.assertNotEqual(loosened.z, 0.0)

    def test_ratio_form_works_for_the_threshold_too(self) -> None:
        probe = copy.deepcopy(self.s)
        probe.property["coefficients"]["min_expected_rooms_ratio"] = 0.2
        self.assertAlmostEqual(pace_mod._demand_config(probe)["min_expected"], 1.0)


class EndToEndShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not (ROOT / "data" / "otb.csv").exists():
            raise unittest.SkipTest("収集済みデータ未生成（scripts/run_pipeline.sh）")
        cls.ctx = build_context(ROOT, None, 180)
        cls.s = cls.ctx.settings
        cls.rooms = int(cls.s.property["property"]["rooms"])

    def test_nothing_pins_at_the_clip_any_more(self) -> None:
        for day, p in self.ctx.paces.items():
            self.assertLess(abs(p.raw_z), 1.0, f"{day} でシグナルが ±1 に達している")

    def test_model_output_rises_monotonically_past_saturation(self) -> None:
        """飽和点(1.5室)を超えた 3→4→5室 でも価格が上がること."""
        from kanoya_rm import compset
        from kanoya_rm.pricing import recommend

        checked = 0
        for day in sorted(self.ctx.recommendations):
            p0 = self.ctx.paces[day]
            if p0.below_min_expected:
                continue
            snap = self.ctx.comp_snapshots.get(day)
            baseline = compset.baseline_median(self.ctx.comp_snapshots, day, self.s)
            current = float(self.ctx.otb[day]["current_public_rate"])
            prices = []
            for otb in range(self.rooms + 1):
                p = pace_mod.evaluate(self.s, day, self.ctx.snapshot, otb)
                prices.append(recommend(self.s, day, p, snap, baseline,
                                        current).raw_price)
            for i, (a, b) in enumerate(zip(prices, prices[1:])):
                self.assertGreaterEqual(b, a - 1e-6,
                                        f"{day}: OTB {i}→{i+1}室 で下がった")
            # 飽和点超え（3室以降）でも頭打ちにならないこと
            self.assertGreater(prices[-1], prices[3] + 1e-6,
                               f"{day}: 3室以降で頭打ち")
            checked += 1
        self.assertGreater(checked, 0, "有効な内部需要の日が1日も無い")

    def test_disabled_days_contribute_exactly_zero_yen(self) -> None:
        for day, rec in self.ctx.recommendations.items():
            if not self.ctx.paces[day].below_min_expected:
                continue
            demand = next(c for c in rec.contributions if c.factor == "demand")
            self.assertEqual(demand.z, 0.0)
            self.assertEqual(demand.yen, 0.0)
            self.assertIn("判断材料にならない", demand.note)


if __name__ == "__main__":
    unittest.main()
