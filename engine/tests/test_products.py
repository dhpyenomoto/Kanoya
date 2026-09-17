"""商品形態別の価格導出とフロアの回帰テスト.

最適化単位を総額から部屋代へ移した（2026-09）。総額基準の出力は実売の
12%（2食付き）にしか当たらず、主力の素泊まり（55%）とは1.8倍乖離していた。
またOTA管理画面に打ち込むのは部屋代なので、総額では現場で使えない。
"""

from __future__ import annotations

import copy
import csv
import io
import json
import statistics
import sys
import unittest
from contextlib import redirect_stderr
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import pace as pace_mod, products  # noqa: E402
from kanoya_rm.cli import build_context  # noqa: E402
from kanoya_rm.config import Settings  # noqa: E402
from kanoya_rm.pricing import recommend  # noqa: E402
from kanoya_rm.products import load as load_products  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class DerivedPriceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.p = products.load(self.s)

    def test_all_four_forms_are_priced(self) -> None:
        prices = self.p.prices(40000)
        self.assertEqual(set(prices), set(products.FORMS))
        self.assertEqual(len(prices), 4)

    def test_meals_are_added_per_person(self) -> None:
        room = 40000.0
        occ = self.p.occupancy
        prices = self.p.prices(room)
        self.assertEqual(prices["room_only"], room)
        self.assertEqual(prices["breakfast"], room + self.p.breakfast_per_person * occ)
        self.assertEqual(prices["dinner"], room + self.p.dinner_per_person * occ)
        self.assertEqual(prices["two_meals"],
                         room + (self.p.dinner_per_person
                                 + self.p.breakfast_per_person) * occ)

    def test_forms_are_ordered_cheapest_first(self) -> None:
        prices = self.p.prices(40000)
        self.assertLess(prices["room_only"], prices["breakfast"])
        self.assertLess(prices["breakfast"], prices["dinner"])
        self.assertLess(prices["dinner"], prices["two_meals"])

    def test_room_anchor_reproduces_the_legacy_two_meal_total(self) -> None:
        """部屋代アンカーから、移行前の総額アンカーが概ね再現できること.

        完全一致は求めない。移行時点では 37,000 + 44,000 = 81,000 で
        ぴったり一致していたが、2026-09 にアンカーを実勢へ合わせて
        39,000円にしたため +2.5% ずれた。ここで見たいのは
        「単位の取り違えで桁やスケールが飛んでいないか」であって、
        アンカーを動かせなくすることではない。許容は ±5%。
        """
        ok, converted, legacy = self.p.anchor_consistency()
        self.assertTrue(ok, f"{converted:,.0f} vs {legacy:,.0f}")
        self.assertLessEqual(abs(converted / legacy - 1), 0.05)

    def test_anchor_matches_the_room_component(self) -> None:
        self.assertAlmostEqual(
            self.p.room_per_person * self.p.occupancy,
            self.p.anchor_room_rate, delta=1.0)

    def test_meal_rates_match_the_measured_values(self) -> None:
        """実測（税込）: 夕食16,500円/名・朝食5,500円/名."""
        self.assertEqual(self.p.dinner_per_person, 16500)
        self.assertEqual(self.p.breakfast_per_person, 5500)

    def test_two_meal_total_is_what_competitors_compare_against(self) -> None:
        """競合NARは『1室2名2食』基準。素の部屋代と直接比べてはいけない."""
        self.assertEqual(self.p.two_meal_total(40000),
                         self.p.prices(40000)["two_meals"])


class FloorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.p = products.load(self.s)

    def test_room_floor_satisfies_every_form(self) -> None:
        floor, _form, _prov = self.p.room_floor()
        for form in products.FORMS:
            self.assertGreaterEqual(
                self.p.price(form, floor), self.p.floors[form].value - 1e-6,
                f"{form} のフロアを割っている")

    def test_provisional_floors_are_flagged(self) -> None:
        provisional = self.p.provisional_forms()
        self.assertIn("room_only", provisional)
        self.assertIn("breakfast", provisional)
        self.assertIn("dinner", provisional)
        self.assertNotIn("two_meals", provisional,
                         "2食付きのフロアだけが変動費から算出された確定値")

    def test_a_tie_reports_the_provisional_side(self) -> None:
        """暫定フロアが等しく効いているのに、確定値の形態を返して隠さないこと.

        現在の暫定値は『2食付きフロア − 含まれない食事の売価』で置いてあるため、
        4形態すべてが同じ部屋代下限に帰着する。
        """
        self.assertEqual(len(self.p.binding_forms()), 4,
                         "暫定値の作り方が変わった。前提を見直すこと")
        _floor, form, provisional = self.p.room_floor()
        self.assertTrue(provisional)
        self.assertIn(form, self.p.provisional_forms())

    def test_startup_warns_about_provisional_floors(self) -> None:
        messages = "\n".join(products.warnings_for(self.p))
        self.assertIn("暫定フロア", messages)
        self.assertIn("変動費", messages)

    def test_startup_warns_when_the_anchor_drifts(self) -> None:
        probe = copy.deepcopy(self.p)
        probe.anchor_room_rate = probe.anchor_room_rate * 1.5
        messages = "\n".join(products.warnings_for(probe))
        self.assertIn("整合しません", messages)

    def test_no_anchor_warning_for_the_shipped_config(self) -> None:
        messages = "\n".join(products.warnings_for(self.p))
        self.assertNotIn("整合しません", messages)


class RecommendationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.p = products.load(self.s)
        self.day = date(2027, 3, 15)
        while self.s.is_closed(self.day):
            self.day += timedelta(days=1)

    def _rec(self, otb: int = 2, current: float = 0.0, **kw):
        pace = pace_mod.evaluate(self.s, self.day,
                                 self.day - timedelta(days=5), otb)
        return recommend(self.s, self.day, pace, None, 0.0, current, **kw)

    def test_recommendation_carries_all_four_prices(self) -> None:
        rec = self._rec()
        self.assertEqual(set(rec.product_prices), set(products.FORMS))
        for form, price in rec.product_prices.items():
            self.assertEqual(price, self.p.price(form, rec.recommended_rate))

    def test_recommended_rate_is_the_room_rate(self) -> None:
        """recommended_rate は部屋代。素泊まり価格と一致する."""
        rec = self._rec()
        self.assertEqual(rec.product_prices["room_only"], rec.recommended_rate)

    def test_two_meal_total_is_exposed_for_comparison(self) -> None:
        rec = self._rec()
        self.assertEqual(rec.two_meal_total,
                         self.p.two_meal_total(rec.recommended_rate))
        self.assertGreater(rec.two_meal_total, rec.recommended_rate)

    def test_provisional_floor_is_stated_when_it_binds(self) -> None:
        """仮値のフロアが効いた日は、そうと分かるようにする."""
        probe = copy.deepcopy(self.s)
        # フロアを高くして確実に当てる
        probe.property["guardrails"]["floors"] = {
            "room_only": {"value": 999000, "provisional": True},
            "two_meals": {"value": 10000, "provisional": False},
        }
        pace = pace_mod.evaluate(probe, self.day,
                                 self.day - timedelta(days=5), 0)
        rec = recommend(probe, self.day, pace, None, 0.0, 0.0)
        self.assertTrue(rec.floor_provisional)
        self.assertEqual(rec.floor_form, "room_only")
        notes = " / ".join(rec.guardrail_notes)
        self.assertIn("暫定フロア適用", notes)
        self.assertIn("素泊まり", notes)

    def test_confirmed_floor_is_not_marked_provisional(self) -> None:
        probe = copy.deepcopy(self.s)
        probe.property["guardrails"]["floors"] = {
            "two_meals": {"value": 999000, "provisional": False}}
        pace = pace_mod.evaluate(probe, self.day,
                                 self.day - timedelta(days=5), 0)
        rec = recommend(probe, self.day, pace, None, 0.0, 0.0)
        self.assertFalse(rec.floor_provisional)
        self.assertNotIn("暫定フロア適用", " / ".join(rec.guardrail_notes))

    def test_no_floor_note_when_it_does_not_bind(self) -> None:
        rec = self._rec(otb=5)
        self.assertEqual(rec.floor_form, "")
        self.assertFalse(rec.floor_provisional)

    def test_competitor_comparison_uses_the_two_meal_basis(self) -> None:
        """部屋代とNARを直接比べると z_comp が +1 に張り付いて動かなくなる."""
        from kanoya_rm import compset
        from kanoya_rm.config import Competitor

        s = copy.deepcopy(self.s)
        s.competitors = {
            f"c{i}": Competitor(id=f"c{i}", name=f"c{i}", tier="PRIMARY",
                                weight=1.0, rooms=10, distance_km=1.0,
                                pricing_basis="per_room",
                                meal_included="dinner_breakfast",
                                dinner_uplift=0.0, breakfast_uplift=0.0)
            for i in range(1, 4)
        }
        # 競合NARを自社の2食付き相当と同水準に置く → z_comp はほぼ0のはず
        own_two_meal = self.p.two_meal_total(
            self.p.anchor_room_rate)
        rows = [{"comp_id": c, "raw_rate": str(int(own_two_meal)), "available": "1"}
                for c in s.competitors]
        snap = compset.build_snapshot(s, self.day, rows)
        pace = pace_mod.evaluate(s, self.day, self.day - timedelta(days=5), 2)
        rec = recommend(s, self.day, pace, snap, snap.weighted_median_nar, 0.0)
        z_comp = next(c.z for c in rec.contributions if c.factor == "comp")
        self.assertLess(abs(z_comp), 1.0,
                        "z_comp が飽和している。部屋代とNARを直接比べていないか")


class PostedRateUnitTest(unittest.TestCase):
    """otb.csv の current_public_rate が部屋代基準であること.

    実際にここで事故を起こしている。2026-09 に最適化単位を部屋代へ移した際、
    フィクスチャの現行掲出価格は「1室2名2食の総額」のままだった。
    日次変動幅ガードは推奨と現行を直接比べるので、部屋代の推奨が総額の
    バンド下限（現行の85%）へ持ち上げられ、2026-11 の推奨が全日ほぼ同額の
    145,000〜157,000円（2食付き換算）に張り付いた。delta_pct も
    別単位どうしの比になっており、承認区分の判断材料として成立していなかった。

    単位の食い違いは値が大きくずれて初めて気づくため、比率で固定する。
    """

    @classmethod
    def setUpClass(cls) -> None:
        path = ROOT / "data" / "otb.csv"
        if not path.exists():
            raise unittest.SkipTest("収集済みデータ未生成（scripts/run_pipeline.sh）")
        cls.settings = Settings.load(ROOT / "config")
        cls.p = load_products(cls.settings)
        with path.open(encoding="utf-8") as fh:
            cls.rates = sorted({float(r["current_public_rate"])
                                for r in csv.DictReader(fh)})

    def test_posted_rates_are_room_rates_not_two_meal_totals(self) -> None:
        median = statistics.median(self.rates)
        room = abs(median / self.p.anchor_room_rate - 1)
        total = abs(median / self.p.two_meal_total(self.p.anchor_room_rate) - 1)
        self.assertLess(room, total,
                        f"現行掲出価格の中央値 {median:,.0f}円 が部屋代アンカー "
                        f"{self.p.anchor_room_rate:,.0f}円 より2食付き総額に近い。"
                        "単位が総額のままではないか")

    def test_model_output_and_posted_rate_are_the_same_unit(self) -> None:
        """モデル出力と現行掲出価格が同じ単位で並んでいること.

        delta_pct と日次変動幅ガードは、この2つを直接比べる。単位が
        食い違っていても値は出てしまうので、「食事を足したほうが近い」
        状態になっていないかで見る。総額のままなら食事を足した側が近くなる。

        バンドで挟んだ後の recommended_rate ではなく、挟む前の raw_price を
        見る。バンドは必ず現行の±15%へ収めるので、挟んだ後の値では
        単位が食い違っていても差が消えてしまう。
        """
        ctx = build_context(ROOT, None, 120)
        priced = [r for r in ctx.recommendations.values() if r.current_rate > 0]
        self.assertTrue(priced, "現行価格のある日が無く、検証できない")
        meals = self.p.all_meals
        as_room = statistics.median(
            abs(r.raw_price / r.current_rate - 1) for r in priced)
        as_total = statistics.median(
            abs(r.raw_price / (r.current_rate + meals) - 1) for r in priced)
        self.assertLess(as_room, as_total,
                        f"食事{meals:,.0f}円を足したほうがモデル出力に近い"
                        f"（部屋代基準 {as_room:.1%} / 総額基準 {as_total:.1%}）。"
                        "current_public_rate が総額のままではないか")


if __name__ == "__main__":
    unittest.main()
