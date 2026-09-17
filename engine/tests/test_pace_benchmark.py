"""ブッキングカーブ（pace_benchmark）の回帰テスト.

このカーブは「あるべき予約進捗」を決める。実態と外れていても
エンジン内では正常値に見えるため、内部からは検知できない。

実際に事故が起きている: 擬似データ由来の想定カーブ（120日前に8%が
入っている前提）が実運用の値として入ったままになっており、
実測（0.5%）と大きく乖離していた。その結果、進捗が常に
「大幅な遅れ」と判定され、リード14日以遠で価格を平均15.6%押し下げていた。
"""

from __future__ import annotations

import copy
import io
import json
import math
import sys
import unittest
from contextlib import redirect_stderr
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import pace as pace_mod  # noqa: E402
from kanoya_rm.config import (  # noqa: E402
    Settings, benchmark_provenance_warning,
)

ROOT = Path(__file__).resolve().parents[1]

# 差し替え前の擬似データ由来カーブ（回帰の比較対象として残す）
LEGACY_CURVE = [(120, 0.08), (90, 0.15), (60, 0.28), (45, 0.38), (30, 0.52),
                (21, 0.62), (14, 0.73), (7, 0.86), (3, 0.94), (0, 1.0)]
LEGACY_MULTIPLIER = {"PEAK": 1.25, "HIGH": 1.12, "SHOULDER": 1.0,
                     "LOW": 0.88, "DEEP_LOW": 0.8}

# 実予約明細から復元した進捗（2026-02〜09）
MEASURED = {120: 0.005, 90: 0.014, 60: 0.014, 45: 0.027, 30: 0.072,
            21: 0.149, 14: 0.284, 10: 0.347, 7: 0.450, 5: 0.532,
            3: 0.635, 1: 0.856, 0: 0.995}
TARGET_OCC = {"PEAK": 0.95, "HIGH": 0.88, "SHOULDER": 0.75,
              "LOW": 0.62, "DEEP_LOW": 0.50}


class ProvenanceTest(unittest.TestCase):
    """出所不明のカーブを黙って受け入れないこと."""

    def setUp(self) -> None:
        self.calendar = json.loads(
            (ROOT / "config" / "calendar.json").read_text(encoding="utf-8"))

    def test_shipped_benchmark_states_where_it_came_from(self) -> None:
        source = self.calendar["pace_benchmark"].get("_source", "")
        self.assertTrue(source.strip(), "pace_benchmark に _source が無い")
        self.assertIsNone(benchmark_provenance_warning(self.calendar))

    def test_missing_source_is_flagged(self) -> None:
        probe = copy.deepcopy(self.calendar)
        del probe["pace_benchmark"]["_source"]
        message = benchmark_provenance_warning(probe)
        self.assertIsNotNone(message)
        self.assertIn("_source", message)

    def test_blank_source_counts_as_missing(self) -> None:
        """空文字を入れて警告だけ黙らせる、を通さない."""
        probe = copy.deepcopy(self.calendar)
        probe["pace_benchmark"]["_source"] = "   "
        self.assertIsNotNone(benchmark_provenance_warning(probe))

    def test_absent_benchmark_is_flagged(self) -> None:
        probe = copy.deepcopy(self.calendar)
        del probe["pace_benchmark"]
        self.assertIsNotNone(benchmark_provenance_warning(probe))

    def test_loading_warns_but_does_not_fail(self) -> None:
        """出所不明でも検証や試作は回せるべき。止めるとブートストラップできない."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            shutil.copytree(ROOT / "config", cfg)
            cal = json.loads((cfg / "calendar.json").read_text(encoding="utf-8"))
            del cal["pace_benchmark"]["_source"]
            (cfg / "calendar.json").write_text(
                json.dumps(cal, ensure_ascii=False), encoding="utf-8")

            err = io.StringIO()
            with redirect_stderr(err):
                settings = Settings.load(cfg)
            self.assertIn("出所不明", err.getvalue())
            self.assertTrue(settings.calendar["pace_benchmark"]["curve"])

    def test_no_warning_for_the_shipped_config(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            Settings.load(ROOT / "config")
        # 暫定フロアの警告は別件なので、ベンチマーク出所の警告だけを見る
        self.assertNotIn("出所不明", err.getvalue())


class CurveShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bench = json.loads(
            (ROOT / "config" / "calendar.json").read_text(encoding="utf-8")
        )["pace_benchmark"]

    def test_ratios_are_proportions(self) -> None:
        for point in self.bench["curve"]:
            self.assertGreaterEqual(point["ratio"], 0.0)
            self.assertLessEqual(point["ratio"], 1.0)

    def test_bookings_accumulate_as_the_stay_approaches(self) -> None:
        """リードが縮むほど進捗は増える（減ることはない）."""
        curve = sorted(self.bench["curve"], key=lambda p: -p["lead_days"])
        ratios = [p["ratio"] for p in curve]
        for a, b in zip(ratios, ratios[1:]):
            self.assertGreaterEqual(b, a, f"進捗が逆行している: {ratios}")

    def test_season_multipliers_are_documented(self) -> None:
        """サンプル不足で1.0固定にした経緯を、値と一緒に残しておく."""
        mult = self.bench["season_multiplier"]
        self.assertEqual(set(mult), set(TARGET_OCC))
        if all(v == 1.0 for v in mult.values()):
            self.assertTrue(
                self.bench.get("_season_multiplier_note", "").strip(),
                "全て1.0なら、なぜ係数化しないのかを注記に残すこと")


class RealisticOtbTest(unittest.TestCase):
    """実測どおりに予約が入った場合の挙動.

    フィクスチャのOTBはこのカーブ『から』生成されるため、定義上
    ギャップがほぼゼロになり、この種の乖離を再現できない。
    実測カーブどおりに埋まった場合のOTBを組み立てて評価する。
    """

    def setUp(self) -> None:
        self.new = Settings.load(ROOT / "config")
        self.old = copy.deepcopy(self.new)
        self.old.calendar["pace_benchmark"]["curve"] = [
            {"lead_days": l, "ratio": r} for l, r in LEGACY_CURVE]
        self.old.calendar["pace_benchmark"]["season_multiplier"] = \
            dict(LEGACY_MULTIPLIER)
        self.day = date(2027, 3, 15)            # SHOULDER の平日
        self.rooms = int(self.new.property["property"]["rooms"])
        coef = self.new.property["coefficients"]
        self.b_demand = float(coef["b_demand"])
        self.clip = float(coef["term_clip"])

    def _evaluate(self, settings, lead: int):
        season, _ = settings.season_of(self.day)
        otb = round(self.rooms * TARGET_OCC[season] * MEASURED[lead])
        return pace_mod.evaluate(settings, self.day,
                                 self.day - timedelta(days=lead), otb)

    def _price_effect(self, result) -> float:
        return math.exp(max(-self.clip,
                            min(self.clip, self.b_demand * result.z))) - 1.0

    FAR_LEADS = (14, 21, 30, 45, 60, 90, 120)

    def test_measured_curve_does_not_pin_the_signal_at_the_floor(self) -> None:
        """張り付くと、OTBが何室でも同じ価格になり進捗を見ていないのと同じ."""
        pinned = [lead for lead in MEASURED
                  if self._evaluate(self.new, lead).raw_z <= -0.9999]
        self.assertEqual(pinned, [], f"raw_z が下限に張り付くリード: {pinned}")

    def test_the_legacy_curve_drove_the_signal_to_the_floor(self) -> None:
        """比較対象が実際に壊れていたことを残す（直った証拠になる）.

        写像を tanh にしたのでハードクリップは起きない（±1 に漸近するだけで、
        実測では -0.87 止まり）。「下限に張り付く」ではなく
        「複数のリード帯で強い遅れ側へ押し込まれる」ことを見る。
        """
        strong = [lead for lead in MEASURED
                  if self._evaluate(self.old, lead).raw_z <= -0.8]
        self.assertGreaterEqual(len(strong), 3,
                                f"旧カーブで強い遅れ判定が再現しない（{strong}）。前提が変わった")

    def test_far_leads_are_no_longer_a_systematic_discount(self) -> None:
        """実測どおりに埋まっているのに値下げ方向へ働くのは、自動値下げ機."""
        effects = [self._price_effect(self._evaluate(self.new, lead))
                   for lead in self.FAR_LEADS]
        mean = sum(effects) / len(effects)
        self.assertGreater(mean, -0.03,
                           f"リード14日以遠の平均寄与が {mean:+.1%} と押し下げ側")

    def test_the_legacy_curve_discounted_far_leads_heavily(self) -> None:
        effects = [self._price_effect(self._evaluate(self.old, lead))
                   for lead in self.FAR_LEADS]
        mean = sum(effects) / len(effects)
        self.assertLess(mean, -0.10,
                        f"旧カーブの押し下げが再現しない（{mean:+.1%}）。前提が変わった")


if __name__ == "__main__":
    unittest.main()
