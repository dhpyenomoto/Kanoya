"""日次変動幅の実効値が宣言値を超えないことの回帰テスト.

ガードレールの適用順序は「変動幅 → 丸め → 絶対境界」。
丸め（1,000円単位）がバンドの後に来るため、最近接で丸めると
バンド境界から最大 unit/2 だけはみ出していた。

  2026-08-18  現行72,000 モデル46,963
    → バンド[61,200, 82,800] → 丸め後 61,000 → 実効 -15.28%

実害は 0.31ポイント／約200円と小さいが、設定に 0.15 と書いてあるものが
実効15.3%だと、将来この値を根拠に判断するときにずれる。
安全装置は宣言した範囲を超えない側に倒す。
"""

from __future__ import annotations

import copy
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import compset, pace as pace_mod  # noqa: E402
from kanoya_rm.cli import build_context  # noqa: E402
from kanoya_rm.config import Settings  # noqa: E402
from kanoya_rm.pricing import recommend  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def band_note(rec) -> str | None:
    for note in rec.guardrail_notes:
        if "日次変動幅" in note:
            return note
    return None


class EffectiveBandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not (ROOT / "data" / "otb.csv").exists():
            raise unittest.SkipTest("収集済みデータ未生成（scripts/run_pipeline.sh）")
        cls.ctx = build_context(ROOT, None, 180)
        cls.s = cls.ctx.settings
        guard = cls.s.property["guardrails"]
        cls.band = float(guard["max_change_per_day_pct"])
        cls.unit = float(guard["rounding_unit"])
        cls.floor = float(guard["floor_room_rate"])
        cls.ceiling = float(guard["ceiling_room_rate"])

    def _sweep(self, day):
        """1日について OTB を振り、バンドが効く場面を多く作る."""
        snap = self.ctx.comp_snapshots.get(day)
        baseline = compset.baseline_median(self.ctx.comp_snapshots, day, self.s)
        current = float(self.ctx.otb[day]["current_public_rate"])
        rooms = int(self.s.property["property"]["rooms"])
        out = []
        for otb in range(rooms + 1):
            p = pace_mod.evaluate(self.s, day, self.ctx.snapshot, otb)
            out.append(recommend(self.s, day, p, snap, baseline, current))
        return out

    def test_effective_band_never_exceeds_the_declared_one(self) -> None:
        """設定に 0.15 と書いたら、実効も 0.15 を超えないこと."""
        worst, worst_day = 0.0, None
        for day in sorted(self.ctx.recommendations):
            for rec in self._sweep(day):
                if rec.current_rate <= 0:
                    continue
                # 絶対境界に当たった日は、変動幅より境界が優先される（設計どおり）
                if rec.recommended_rate in (self.floor, self.ceiling):
                    continue
                delta = abs(rec.recommended_rate / rec.current_rate - 1)
                if delta > worst:
                    worst, worst_day = delta, day
        self.assertLessEqual(
            worst, self.band + 1e-9,
            f"{worst_day} で実効 {worst:.2%} > 宣言 {self.band:.0%}")

    def test_the_band_is_still_actually_binding(self) -> None:
        """はみ出しを消すために、バンド自体を効かなくしていないこと."""
        clamped = [rec for day in sorted(self.ctx.recommendations)
                   for rec in self._sweep(day) if band_note(rec)]
        self.assertGreater(len(clamped), 0, "変動幅が1件も効いていない")

    def test_rounding_stays_on_the_grid(self) -> None:
        for day in sorted(self.ctx.recommendations):
            for rec in self._sweep(day):
                self.assertEqual(rec.recommended_rate % self.unit, 0,
                                 f"{day} の推奨が丸め単位に乗っていない")


class RoundingBehaviourTest(unittest.TestCase):
    """バンドが効いていない日の丸めは、従来どおり最近接であること."""

    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.guard = self.s.property["guardrails"]
        self.unit = float(self.guard["rounding_unit"])
        self.day = date(2027, 3, 15)
        while self.s.is_closed(self.day):
            self.day += timedelta(days=1)

    def _recommend(self, current: float, otb: int = 2):
        p = pace_mod.evaluate(self.s, self.day,
                              self.day - timedelta(days=5), otb)
        return recommend(self.s, self.day, p, None, 0.0, current)

    def test_unclamped_days_round_to_nearest(self) -> None:
        """バンドが効いていない日は、切り上げ／切り捨てに変えない.

        モデル出力と推奨の差が丸め単位の半分以内であることを見る。
        片側へ寄せていれば、最大で丸め単位1つぶんずれる。
        """
        checked = 0
        for current in (80000, 90000, 100000, 110000, 120000, 130000):
            rec = self._recommend(current)
            if band_note(rec) or rec.recommended_rate in (
                    float(self.guard["floor_room_rate"]),
                    float(self.guard["ceiling_room_rate"])):
                continue
            self.assertLessEqual(
                abs(rec.recommended_rate - rec.raw_price), self.unit / 2 + 1e-6,
                f"現行{current:,.0f}円の日で最近接丸めになっていない")
            checked += 1
        self.assertGreater(checked, 0, "バンド非適用の検体が無い")

    def test_lower_clamp_rounds_up_into_the_band(self) -> None:
        # モデル出力を大きく下回らせ、下方制限を確実に効かせる
        rec = self._recommend(200000, otb=0)
        self.assertIsNotNone(band_note(rec), "下方制限が効いていない")
        if rec.recommended_rate == float(self.guard["floor_room_rate"]):
            self.skipTest("フロアが先に効いた")
        lo = 200000 * (1 - float(self.guard["max_change_per_day_pct"]))
        self.assertGreaterEqual(rec.recommended_rate, lo,
                                "バンド下限より外側へ丸められている")

    def test_upper_clamp_rounds_down_into_the_band(self) -> None:
        rec = self._recommend(62000, otb=5)
        self.assertIsNotNone(band_note(rec), "上方制限が効いていない")
        if rec.recommended_rate == float(self.guard["ceiling_room_rate"]):
            self.skipTest("天井が先に効いた")
        hi = 62000 * (1 + float(self.guard["max_change_per_day_pct"]))
        self.assertLessEqual(rec.recommended_rate, hi,
                             "バンド上限より外側へ丸められている")

    def test_absolute_bounds_still_win_over_the_band(self) -> None:
        """適用順序は変えていない。フロア・天井が最後に効くこと.

        フロアはバンド下限（現行の85%）より上に置く。下ではバンドに
        持ち上げられた時点でフロアを超えてしまい、何も検証できない。
        """
        original_floor = float(self.guard["floor_room_rate"])
        current = 200000.0
        band_lo = current * (1 - float(self.guard["max_change_per_day_pct"]))

        probe = copy.deepcopy(self.s)
        probe.property["guardrails"]["floor_room_rate"] = band_lo + 10000
        p = pace_mod.evaluate(probe, self.day,
                              self.day - timedelta(days=5), 0)
        rec = recommend(probe, self.day, p, None, 0.0, current)
        self.assertEqual(rec.recommended_rate, band_lo + 10000,
                         "フロアがバンドより先に負けている")
        self.assertEqual(original_floor, 58000,
                         "テストの前提（元のフロア）が変わった")


if __name__ == "__main__":
    unittest.main()
