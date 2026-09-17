"""フィクスチャへのペース乖離注入（--pace-divergence / PACE_SCENARIOS）の回帰テスト.

OTBをブッキングカーブ『から』生成する以上、実OTBと期待OTBの乖離は
構造的にゼロ近辺に留まる。そのためペース関連の不具合がフィクスチャでは
原理的に再現しない。実際にこれで取り逃している: 出所不明のブッキングカーブが
進捗を常に「大幅な遅れ」と判定していた件は、フィクスチャ上では症状が一切出ず、
実測相当のOTBを組み立てて初めて見えた。

ここで固定するのは「乖離を作れること」と
「作った乖離がシグナルとして機能する範囲に収まっていること」である。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import pace as pace_mod  # noqa: E402
from kanoya_rm.cli import build_context  # noqa: E402
from kanoya_rm.config import Settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _load_generator():
    path = ROOT / "scripts" / "make_fixtures.py"
    spec = importlib.util.spec_from_file_location("make_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DivergenceOptionTest(unittest.TestCase):
    """--pace-divergence が実際にOTBを動かすこと.

    ファイルは書かない。_build_otb は行を返すだけなので、
    共有のフィクスチャを壊さずに検証できる。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.gen = _load_generator()
        cls.settings = Settings.load(ROOT / "config")

    def _otb_at_run_date(self, **kwargs) -> dict[str, int]:
        rows = self.gen._build_otb(self.settings, **kwargs)
        run = self.gen.RUN_DATE.isoformat()
        return {r["stay_date"]: r["rooms_otb"]
                for r in rows if r["snapshot_date"] == run}

    def test_lower_divergence_never_books_more_rooms(self) -> None:
        """『想定の3割しか埋まっていない』が、室数を増やしては意味が通らない."""
        low = self._otb_at_run_date(divergence=0.3, scenarios=())
        base = self._otb_at_run_date(divergence=1.0, scenarios=())
        for stay, rooms in low.items():
            self.assertLessEqual(rooms, base[stay], f"{stay} で増えている")
        self.assertLess(sum(low.values()), sum(base.values()),
                        "倍率を下げてもOTBの総数が減っていない")

    def test_higher_divergence_never_books_fewer_rooms(self) -> None:
        high = self._otb_at_run_date(divergence=2.5, scenarios=())
        base = self._otb_at_run_date(divergence=1.0, scenarios=())
        for stay, rooms in high.items():
            self.assertGreaterEqual(rooms, base[stay], f"{stay} で減っている")
        self.assertGreater(sum(high.values()), sum(base.values()))

    def test_divergence_moves_the_demand_signal(self) -> None:
        """OTBが動いても z_demand が動かないなら、診断の役に立たない."""
        run = self.gen.RUN_DATE
        signals = {}
        for div in (0.3, 1.0, 2.5):
            otb = self._otb_at_run_date(divergence=div, scenarios=())
            stay = run + timedelta(days=10)
            result = pace_mod.evaluate(self.settings, stay, run,
                                       otb[stay.isoformat()])
            signals[div] = round(result.z, 4)
        self.assertLess(signals[0.3], signals[1.0])
        self.assertLess(signals[1.0], signals[2.5])

    def test_generation_is_deterministic(self) -> None:
        """実行のたびに変わると、差分が乖離由来なのか乱数由来なのか分からない."""
        first = self.gen._build_otb(self.settings, divergence=0.7)
        second = self.gen._build_otb(self.settings, divergence=0.7)
        self.assertEqual(first, second)

    def test_demand_noise_does_not_depend_on_iteration_order(self) -> None:
        """宿泊日ごとに独立したシードを使うこと.

        共有の乱数列から引くと、どこかで乱数を1つ増やしただけで
        全日のOTBがずれ、「カーブだけを変えた影響」を測れなくなる。
        """
        full = self.gen._build_otb(self.settings, scenarios=())
        run = self.gen.RUN_DATE.isoformat()
        baseline = {r["stay_date"]: r["rooms_otb"]
                    for r in full if r["snapshot_date"] == run}
        # シナリオを足しても、対象外の日のOTBは1室も変わらないはず
        with_scenarios = self.gen._build_otb(self.settings)
        after = {r["stay_date"]: r["rooms_otb"]
                 for r in with_scenarios if r["snapshot_date"] == run}
        covered = {d.isoformat()
                   for sc in self.gen.PACE_SCENARIOS
                   for d in self._dates(sc.start, sc.end)}
        for stay, rooms in baseline.items():
            if stay not in covered:
                self.assertEqual(after[stay], rooms,
                                 f"シナリオ対象外の {stay} が動いた")

    @staticmethod
    def _dates(start: date, end: date):
        day = start
        while day <= end:
            yield day
            day += timedelta(days=1)

    def test_scenario_lookup_is_pure(self) -> None:
        sc = self.gen.PACE_SCENARIOS[0]
        inside = sc.start + timedelta(days=1)
        outside = sc.end + timedelta(days=400)
        self.assertEqual(self.gen.divergence_for(inside, 1.0), sc.divergence)
        self.assertEqual(self.gen.divergence_for(outside, 1.0), 1.0)
        self.assertEqual(self.gen.divergence_for(inside, 0.5),
                         0.5 * sc.divergence)

    def test_scenarios_can_be_switched_off(self) -> None:
        self.assertEqual(self.gen.divergence_for(
            self.gen.PACE_SCENARIOS[0].start, 1.0, scenarios=()), 1.0)

    def test_scenario_windows_do_not_overlap(self) -> None:
        """重なると、どちらの倍率が効いたのか読めなくなる."""
        windows = sorted((sc.start, sc.end, sc.name)
                         for sc in self.gen.PACE_SCENARIOS)
        for (_s1, e1, n1), (s2, _e2, n2) in zip(windows, windows[1:]):
            self.assertLess(e1, s2, f"{n1} と {n2} の窓が重なっている")


class DefaultScenarioTest(unittest.TestCase):
    """既定生成に含まれる乖離シナリオが、意図した強さで出ていること."""

    @classmethod
    def setUpClass(cls) -> None:
        if not (ROOT / "data" / "otb.csv").exists() or \
           not (ROOT / "data" / "comp_rates_collected.csv").exists():
            raise unittest.SkipTest("収集済みデータ未生成（scripts/run_pipeline.sh）")
        cls.gen = _load_generator()
        cls.ctx = build_context(ROOT, None, 180)
        cls.settings = cls.ctx.settings

    def _scenario_days(self, name: str) -> list[date]:
        sc = next(s for s in self.gen.PACE_SCENARIOS if s.name == name)
        days, day = [], sc.start
        while day <= sc.end:
            if day in self.ctx.paces:
                days.append(day)
            day += timedelta(days=1)
        self.assertTrue(days, f"{name} シナリオの日が対象期間に1日も無い")
        return days

    def test_both_directions_are_present(self) -> None:
        """遅れ側だけ、先行側だけでは片側の不具合しか見つからない."""
        names = {sc.name for sc in self.gen.PACE_SCENARIOS}
        self.assertIn("lagging", names)
        self.assertIn("leading", names)

    def test_lagging_days_show_a_clear_shortfall(self) -> None:
        for day in self._scenario_days("lagging"):
            p = self.ctx.paces[day]
            self.assertLess(p.raw_z, -0.3,
                            f"{day} の遅れが弱すぎる（raw_z={p.raw_z:.2f}）")

    def test_leading_days_show_a_clear_run_up(self) -> None:
        for day in self._scenario_days("leading"):
            p = self.ctx.paces[day]
            self.assertGreater(p.raw_z, 0.2,
                               f"{day} の先行が弱すぎる（raw_z={p.raw_z:.2f}）")

    def test_scenario_days_do_not_pin_the_signal(self) -> None:
        """張り付くとOTBが何室でも同じ値になり、進捗を見ていないのと同じ.

        極端な乖離で張り付くのは正しい挙動なので、既定シナリオには入れない。
        それは --pace-divergence で明示的に作る（下のテスト参照）。
        """
        for name in ("lagging", "leading"):
            for day in self._scenario_days(name):
                p = self.ctx.paces[day]
                self.assertLess(abs(p.raw_z), 0.9999,
                                f"{day}（{name}）で z がクリップに張り付いている")

    def test_an_extreme_divergence_saturates_the_signal(self) -> None:
        """張り付き相当の状態を作れること（テストが無力でないことの確認）.

        写像を tanh にしたのでハードクリップはもう起きない（±1 に漸近する
        だけで到達しない）。代わりに「飽和の端に寄る」ことを確認する。
        """
        run = self.gen.RUN_DATE
        rows = self.gen._build_otb(self.settings, divergence=0.0, scenarios=())
        otb = {r["stay_date"]: r["rooms_otb"] for r in rows
               if r["snapshot_date"] == run.isoformat()}
        stay = run + timedelta(days=1)          # 期待室数が大きい短リード
        p = pace_mod.evaluate(self.settings, stay, run, otb[stay.isoformat()])
        self.assertFalse(p.below_min_expected)
        self.assertLess(p.raw_z, -0.95)
        self.assertGreater(p.raw_z, -1.0, "tanh なので ±1 には到達しないはず")

    def test_guardrails_hold_on_scenario_days(self) -> None:
        """乖離を入れても、ガードレールの不変条件は破れないこと."""
        guard = self.settings.property["guardrails"]
        floor = float(guard["floor_room_rate"])
        ceiling = float(guard["ceiling_room_rate"])
        band = float(guard["max_change_per_day_pct"])
        unit = float(guard["rounding_unit"])
        hard = float(guard["hard_stop_band_pct"])

        for name in ("lagging", "leading"):
            for day in self._scenario_days(name):
                rec = self.ctx.recommendations[day]
                with self.subTest(day=day, scenario=name):
                    self.assertGreaterEqual(rec.recommended_rate, floor)
                    self.assertLessEqual(rec.recommended_rate, ceiling)
                    self.assertEqual(rec.recommended_rate % unit, 0)
                    if rec.current_rate > 0 and floor < rec.recommended_rate < ceiling:
                        # フロア・天井に当たっていない限り、変動幅を越えない。
                        # ただし丸め（1,000円単位）はバンドの後に適用されるため、
                        # 丸め単位の半分までは実効的にはみ出す（設計上の順序による。
                        # 絶対境界を最後に効かせるためにこの順序を選んでいる）。
                        slack = (unit / 2) / rec.current_rate
                        self.assertLessEqual(
                            abs(rec.recommended_rate / rec.current_rate - 1),
                            band + slack + 1e-9)
                    if rec.action == "AUTO_APPLY":
                        self.assertLessEqual(
                            abs(rec.delta_pct),
                            float(guard["auto_apply_band_pct"]) + 1e-9)
                    self.assertLessEqual(abs(rec.delta_pct), hard + 1e-9)

    def test_scenarios_actually_move_the_price(self) -> None:
        """シグナルが動いても価格が動かないなら、乖離を入れた意味がない."""
        lagging = [self.ctx.recommendations[d] for d in self._scenario_days("lagging")]
        leading = [self.ctx.recommendations[d] for d in self._scenario_days("leading")]

        def demand_yen(rec):
            return next(c.yen for c in rec.contributions if c.factor == "demand")

        self.assertTrue(all(demand_yen(r) < 0 for r in lagging),
                        "遅れている日なのに内部需要項が下げ方向へ働いていない")
        self.assertTrue(all(demand_yen(r) > 0 for r in leading),
                        "先行している日なのに内部需要項が上げ方向へ働いていない")


if __name__ == "__main__":
    unittest.main()
