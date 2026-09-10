"""感度分析ツールの回帰テスト.

このツールは係数の議論の土台になる。ここが黙って壊れると、
「係数を変えても価格が動かない」といった診断を信じられなくなる。
"""

from __future__ import annotations

import csv as csv_mod
import importlib.util
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
TARGET = date(2026, 11, 21)


def _load_module():
    """scripts/ はパッケージではないのでファイルから直接読み込む.

    exec_module の前に sys.modules へ登録すること。@dataclass は
    cls.__module__ を辿って型を解決するため、未登録だと
    AttributeError: 'NoneType' object has no attribute '__dict__' で落ちる。
    """
    path = ROOT / "scripts" / "sensitivity.py"
    spec = importlib.util.spec_from_file_location("sensitivity", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SensitivityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not (ROOT / "data" / "otb.csv").exists() or \
           not (ROOT / "data" / "comp_rates_collected.csv").exists():
            raise unittest.SkipTest("収集済みデータ未生成（scripts/run_pipeline.sh）")
        cls.sens = _load_module()
        from kanoya_rm.cli import build_context
        cls.ctx = build_context(ROOT, None, 180)
        if TARGET not in cls.ctx.recommendations:
            raise unittest.SkipTest(f"{TARGET} が対象期間外")

    # ---- スイープが動くこと ------------------------------------------

    def test_otb_sweep_covers_empty_through_full(self) -> None:
        rows = self.sens.sweep_otb(self.ctx, TARGET)
        rooms = int(self.ctx.settings.property["property"]["rooms"])
        self.assertEqual(len(rows), rooms + 1)
        self.assertEqual([r.knob_value for r in rows],
                         [float(i) for i in range(rooms + 1)])

    def test_lead_sweep_actually_changes_lead_time(self) -> None:
        """リードタイムを振ったのに z_lead が全部同じなら、振れていない."""
        rows = self.sens.sweep_lead(self.ctx, TARGET)
        self.assertEqual(len(rows), len(self.sens.LEAD_POINTS))
        self.assertGreater(len({round(r.z["lead"], 4) for r in rows}), 1)

    def test_coef_sweep_scales_the_named_coefficient(self) -> None:
        rows = self.sens.sweep_coef(self.ctx, TARGET, "b_pace")
        self.assertEqual([r.knob_value for r in rows], self.sens.COEF_SCALES)

    def test_unknown_coefficient_fails_loudly(self) -> None:
        """名前を間違えたまま「効果なし」と結論づけられるのが最悪."""
        with self.assertRaises(SystemExit) as cm:
            self.sens.sweep_coef(self.ctx, TARGET, "b_nonexistent")
        self.assertIn("b_pace", str(cm.exception), "指定できる係数名を示すこと")

    def test_uplift_sweep_moves_the_competitor_position(self) -> None:
        """uplift は NAR 正規化に効く。z_comp が動かないなら通っていない."""
        rows = self.sens.sweep_uplift(ROOT, self.ctx, TARGET, 180)
        self.assertEqual(len(rows), len(self.sens.UPLIFT_SCALES))
        self.assertGreater(len({round(r.z["comp"], 3) for r in rows}), 1)

    def test_every_sweep_reports_all_five_factors(self) -> None:
        for row in self.sens.sweep_otb(self.ctx, TARGET):
            self.assertEqual(set(row.z), set(self.sens.FACTORS))

    # ---- ガードレール上書きの集計 ------------------------------------

    def test_override_is_judged_by_guardrail_notes_not_rounding(self) -> None:
        """丸め（1,000円単位）は設計どおりの挙動で、上書きではない."""
        Row = self.sens.Row
        rounded = Row(stay_date=TARGET, knob="x", knob_value=0.0, p_base=100000,
                      raw_price=126687, recommended=127000, notes=[])
        clipped = Row(stay_date=TARGET, knob="y", knob_value=1.0, p_base=100000,
                      raw_price=208526, recommended=152000,
                      notes=["日次変動幅 ±15% で上方制限"])
        self.assertFalse(rounded.overridden, "丸めだけの差を上書きと数えている")
        self.assertTrue(clipped.overridden)
        self.assertEqual(rounded.gap_yen, 313)

    def test_summary_counts_overridden_rows(self) -> None:
        rows = self.sens.sweep_otb(self.ctx, TARGET)
        expected = sum(1 for r in rows if r.overridden)
        self.assertIn(f"{expected}/{len(rows)} 行でガードレールがモデル出力を上書き",
                      self.sens.summarize(rows))

    # ---- 出力 --------------------------------------------------------

    def test_table_renders_without_a_crash(self) -> None:
        out = self.sens.render_table(self.sens.sweep_otb(self.ctx, TARGET),
                                     title="t")
        self.assertIn("モデル出力", out)
        self.assertIn("ガードレール", out)

    def test_empty_input_does_not_crash_the_renderer(self) -> None:
        self.assertIn("対象データがありません", self.sens.render_table([], title="t"))

    def test_multi_day_mode_folds_each_day_into_one_line(self) -> None:
        targets = sorted(self.ctx.recommendations)[:3]
        per_day = {d: self.sens.sweep_otb(self.ctx, d) for d in targets}
        out = self.sens.render_multi_day(per_day, title="t")
        for stay in targets:
            self.assertIn(stay.isoformat(), out)
        self.assertIn("最も感度が高い日", out)

    def test_csv_row_width_matches_header(self) -> None:
        rows = self.sens.sweep_otb(self.ctx, TARGET)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.csv"
            self.sens.write_csv(rows, path)
            with path.open(encoding="utf-8-sig", newline="") as fh:
                table = list(csv_mod.reader(fh))
        self.assertEqual(len(table), len(rows) + 1)
        self.assertTrue(all(len(r) == len(table[0]) for r in table))

    # ---- 読み取り専用であること --------------------------------------

    def test_sweeping_does_not_mutate_the_shared_settings(self) -> None:
        """診断のたびに本番の設定が書き換わっては、次の実行が信用できない."""
        before = float(self.ctx.settings.property["coefficients"]["b_pace"])
        self.sens.sweep_coef(self.ctx, TARGET, "b_pace")
        after = float(self.ctx.settings.property["coefficients"]["b_pace"])
        self.assertEqual(before, after)

    def test_uplift_sweep_leaves_the_compset_file_untouched(self) -> None:
        path = ROOT / "config" / "compset.json"
        before = path.read_bytes()
        self.sens.sweep_uplift(ROOT, self.ctx, TARGET, 180)
        self.assertEqual(path.read_bytes(), before, "設定ファイルを書き換えている")


if __name__ == "__main__":
    unittest.main()
