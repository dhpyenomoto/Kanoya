"""エンジンの回帰テスト.

    cd engine && python3 -m unittest discover -s tests -v

ガードレールと正規化は「事故を起こさないこと」の保証であり、
本番配信前に必ず緑であることを CI のゲート条件とする。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import compset, pace  # noqa: E402
from kanoya_rm.config import Competitor  # noqa: E402
from kanoya_rm.channels import direct_shift_value, evaluate as eval_channels  # noqa: E402
from kanoya_rm.cli import build  # noqa: E402
from kanoya_rm.config import Settings  # noqa: E402
from kanoya_rm.normalize import normalized_rate  # noqa: E402
from kanoya_rm.pricing import recommend  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def make_comp(cid: str, **kw) -> Competitor:
    """テスト用の競合を組み立てる（config の具体的なIDに依存しない）."""
    base = dict(name=cid, tier="PRIMARY", weight=1.0, rooms=10, distance_km=1.0,
                pricing_basis="per_room", meal_included="none",
                dinner_uplift=0.0, breakfast_uplift=0.0)
    base.update(kw)
    return Competitor(id=cid, **base)


class NormalizeTest(unittest.TestCase):
    def test_per_person_two_meals_doubles(self) -> None:
        comp = make_comp("c", pricing_basis="per_person",
                         meal_included="dinner_breakfast")
        self.assertEqual(normalized_rate(33000, comp), 66000)

    def test_room_basis_room_only_adds_both_meals(self) -> None:
        comp = make_comp("c", meal_included="none",
                         dinner_uplift=26000, breakfast_uplift=8000)
        self.assertEqual(normalized_rate(42000, comp), 76000)

    def test_breakfast_included_adds_dinner_only(self) -> None:
        comp = make_comp("c", meal_included="breakfast", dinner_uplift=26000)
        self.assertEqual(normalized_rate(38000, comp), 64000)


class CompSetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.s.competitors = {
            f"cs0{i}": make_comp(f"cs0{i}", meal_included="dinner_breakfast")
            for i in range(1, 6)
        }
        self.s.competitors["cs07"] = make_comp("cs07", tier="SECONDARY",
                                               meal_included="dinner_breakfast")

    def _rows(self, available: str = "1") -> list[dict[str, str]]:
        return [
            {"comp_id": cid, "raw_rate": str(rate), "available": available}
            for cid, rate in [("cs01", 66000), ("cs02", 60000), ("cs03", 76000),
                              ("cs04", 64000), ("cs05", 67000)]
        ]

    def test_weighted_median_within_sample_range(self) -> None:
        snap = compset.build_snapshot(self.s, date(2026, 11, 21), self._rows())
        self.assertEqual(snap.sample_size, 5)
        self.assertGreater(snap.weighted_median_nar, 55000)
        self.assertLess(snap.weighted_median_nar, 80000)

    def test_soldout_drives_pressure(self) -> None:
        snap = compset.build_snapshot(self.s, date(2026, 11, 21), self._rows(available="0"))
        self.assertEqual(snap.soldout_ratio, 1.0)
        self.assertGreater(snap.pressure, 0.5)

    def test_market_event_autodetected_without_calendar_entry(self) -> None:
        # 5社中3社が売止（60%）かつ 残り2社が平常時比 +35% → 市場イベントとして検知
        rows = [{"comp_id": c, "raw_rate": "0", "available": "0"}
                for c in ("cs01", "cs02", "cs03")]
        rows += [{"comp_id": "cs04", "raw_rate": "90000", "available": "1"},
                 {"comp_id": "cs05", "raw_rate": "84000", "available": "1"}]
        snap = compset.build_snapshot(self.s, date(2026, 6, 3), rows)
        detected, score, label = compset.detect_market_event(self.s, snap, baseline=64000)
        self.assertTrue(detected)
        self.assertGreater(score, 0.5)
        self.assertIn("市場自動検知", label)

    def test_anomalous_rate_excluded(self) -> None:
        rows = self._rows() + [{"comp_id": "cs07", "raw_rate": "9999999", "available": "1"}]
        snap = compset.build_snapshot(self.s, date(2026, 11, 21), rows)
        self.assertEqual(snap.sample_size, 5)  # 異常値は母数から除外される


class GuardrailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.s.competitors = {
            c: make_comp(c, meal_included="dinner_breakfast")
            for c in ("cs01", "cs02", "cs06")
        }
        self.day = date(2026, 9, 15)

    def _rec(self, otb: int, comp_rate: float, current: float):
        p = pace.evaluate(self.s, self.day, date(2026, 8, 15), otb)
        rows = [{"comp_id": c, "raw_rate": str(comp_rate), "available": "1"}
                for c in ("cs01", "cs02", "cs06")]
        snap = compset.build_snapshot(self.s, self.day, rows)
        return recommend(self.s, self.day, p, snap, snap.weighted_median_nar, current)

    def test_never_below_contribution_floor(self) -> None:
        rec = self._rec(otb=0, comp_rate=3000, current=59000)
        floor = self.s.property["guardrails"]["floor_room_rate"]
        self.assertGreaterEqual(rec.recommended_rate, floor)

    def test_never_above_ceiling(self) -> None:
        rec = self._rec(otb=5, comp_rate=400000, current=300000)
        self.assertLessEqual(rec.recommended_rate,
                             self.s.property["guardrails"]["ceiling_room_rate"])

    def test_daily_change_capped(self) -> None:
        rec = self._rec(otb=5, comp_rate=200000, current=90000)
        cap = self.s.property["guardrails"]["max_change_per_day_pct"]
        self.assertLessEqual(abs(rec.delta_pct), cap + 0.01)  # 丸め分の許容

    def test_rounding_unit_respected(self) -> None:
        rec = self._rec(otb=2, comp_rate=35000, current=90000)
        self.assertEqual(rec.recommended_rate % 1000, 0)

    def test_waterfall_reconciles_to_model_output(self) -> None:
        rec = self._rec(otb=2, comp_rate=35000, current=90000)
        total = rec.base_rate + sum(c.yen for c in rec.contributions)
        self.assertAlmostEqual(total, rec.raw_price, places=4)

    def test_action_classification(self) -> None:
        small = self._rec(otb=2, comp_rate=40000, current=88000)
        self.assertIn(small.action, ("AUTO_APPLY", "APPROVAL_REQUIRED"))
        auto_band = self.s.property["guardrails"]["auto_apply_band_pct"]
        if abs(small.delta_pct) <= auto_band:
            self.assertEqual(small.action, "AUTO_APPLY")


class PaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")

    def test_expected_ratio_monotonic_in_lead_time(self) -> None:
        day = date(2026, 11, 21)
        ratios = [pace.expected_ratio(self.s, day, lead) for lead in (90, 60, 30, 14, 7, 0)]
        self.assertEqual(ratios, sorted(ratios))

    def test_behind_pace_gives_negative_z(self) -> None:
        r = pace.evaluate(self.s, date(2026, 8, 20), date(2026, 8, 15), otb_rooms=0)
        self.assertLessEqual(r.z, 0.0)

    def test_ahead_of_pace_gives_positive_z(self) -> None:
        r = pace.evaluate(self.s, date(2026, 11, 15), date(2026, 8, 15), otb_rooms=5)
        self.assertGreater(r.z, 0.0)


class ChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")

    def test_direct_has_highest_net(self) -> None:
        ranked = eval_channels(self.s, 100000)
        self.assertEqual(ranked[0].channel, "direct")

    def test_point_cost_counted_as_discount(self) -> None:
        ranked = {c.channel: c for c in eval_channels(self.s, 100000)}
        # 一休は手数料11%+ポイント原資3% = 実質14%
        self.assertAlmostEqual(ranked["ikyu"].total_cost_rate, 0.14, places=6)

    def test_direct_shift_gain_is_positive(self) -> None:
        result = direct_shift_value(self.s, 83_000_000, 0.20, 0.35)
        self.assertGreater(result["annual_gain_yen"], 0)
        self.assertAlmostEqual(result["shift_points"], 0.15, places=6)


class EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        if not (ROOT / "data" / "comp_rates_collected.csv").exists():
            self.skipTest("収集済みデータ未生成（scripts/run_pipeline.sh）")

    def test_determinism(self) -> None:
        """同じ入力からは常に同じ出力 — 担当者が変わっても結果は変わらない."""
        _, first = build(ROOT, None, 60)
        _, second = build(ROOT, None, 60)
        self.assertEqual(
            [(d, r.recommended_rate, r.action, r.mlos) for d, r in sorted(first.items())],
            [(d, r.recommended_rate, r.action, r.mlos) for d, r in sorted(second.items())],
        )

    def test_every_day_has_explainable_contributions(self) -> None:
        _, recs = build(ROOT, None, 60)
        self.assertGreater(len(recs), 50)
        for rec in recs.values():
            self.assertEqual(len(rec.contributions), 5)
            self.assertGreater(rec.recommended_rate, 0)

    def test_no_recommendation_breaches_hard_stop(self) -> None:
        s = Settings.load(ROOT / "config")
        hard = s.property["guardrails"]["hard_stop_band_pct"]
        _, recs = build(ROOT, None, 120)
        for rec in recs.values():
            if rec.action != "REJECTED_ANOMALY":
                self.assertLessEqual(abs(rec.delta_pct), hard + 1e-9)


if __name__ == "__main__":
    unittest.main()
