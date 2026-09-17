"""最終稼働の見込み（expected_final_occupancy）の回帰テスト.

これは**予測値**であって経営目標ではない。pace.py が
expected_rooms = 客室数 × この値 × 進捗率 として「あるべきOTB」を作り、
実OTBとの差を内部需要シグナルにする。

実際に取り違えていた。経営目標
（PEAK 0.95 / HIGH 0.88 / SHOULDER 0.75 / LOW 0.62 / DEEP_LOW 0.50）が
コードに直書きされており、実績（順に 49.1 / 29.1 / 27.5 / 22.0 / 10.0%）から
全シーズンで40〜59ポイント上振れしていた。その結果、実勢どおりに埋まった日でも
常に進捗不足と判定され、z_demand の中央値が -0.73、価格寄与が約 -20% だった。
値付けを押し下げる自動装置になっていた。

値そのものはどちらでも妥当に見えるため、内部からは区別がつかない。
そのため出所（_source）の記載を必須とする。
"""

from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import pace as pace_mod  # noqa: E402
from kanoya_rm.config import (  # noqa: E402
    Settings, expected_occupancy_provenance_warning,
)

ROOT = Path(__file__).resolve().parents[1]
SEASONS = ("PEAK", "HIGH", "SHOULDER", "LOW", "DEEP_LOW")

# 実績（2026-02-06〜09-17・営業168日）。property.json の値の出どころ。
MEASURED = {"PEAK": 0.49, "HIGH": 0.29, "SHOULDER": 0.28,
            "LOW": 0.22, "DEEP_LOW": 0.22}
# 取り違えられていた経営目標。回帰の比較対象として残す。
LEGACY_TARGET = {"PEAK": 0.95, "HIGH": 0.88, "SHOULDER": 0.75,
                 "LOW": 0.62, "DEEP_LOW": 0.50}


class ConfigurationTest(unittest.TestCase):
    """コードではなく property.json から読むこと."""

    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")

    def test_every_season_is_configured(self) -> None:
        table = self.s.property["expected_final_occupancy"]
        for season in SEASONS:
            self.assertIn(season, table)

    def test_values_match_the_measured_period(self) -> None:
        for season, expected in MEASURED.items():
            self.assertAlmostEqual(
                pace_mod.expected_final_occupancy(self.s, season), expected,
                places=4, msg=f"{season} が実績値と違う")

    def test_management_targets_are_not_in_here(self) -> None:
        """目標を入れると全日が進捗不足と判定され、価格が押し下がる."""
        for season, target in LEGACY_TARGET.items():
            value = pace_mod.expected_final_occupancy(self.s, season)
            self.assertLess(value, target - 0.1,
                            f"{season} に経営目標が入っていないか（{value}）")

    def test_the_config_drives_the_expectation(self) -> None:
        probe = copy.deepcopy(self.s)
        probe.property["expected_final_occupancy"]["SHOULDER"] = 0.60
        day = date(2027, 3, 15)
        before = pace_mod.evaluate(self.s, day, day, 0).expected_rooms
        after = pace_mod.evaluate(probe, day, day, 0).expected_rooms
        self.assertGreater(after, before)

    def test_an_absent_table_falls_back_without_crashing(self) -> None:
        """設定が無い施設でもエンジンは回ること（施設非依存の方針）."""
        probe = copy.deepcopy(self.s)
        del probe.property["expected_final_occupancy"]
        value = pace_mod.expected_final_occupancy(probe, "SHOULDER")
        self.assertGreater(value, 0.0)
        self.assertLess(value, 1.0)

    def test_out_of_range_values_are_clamped(self) -> None:
        probe = copy.deepcopy(self.s)
        probe.property["expected_final_occupancy"]["SHOULDER"] = 1.8
        self.assertEqual(
            pace_mod.expected_final_occupancy(probe, "SHOULDER"), 1.0)


class ProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")

    def test_the_shipped_config_states_where_it_came_from(self) -> None:
        table = self.s.property["expected_final_occupancy"]
        self.assertTrue(str(table.get("_source") or "").strip())
        self.assertIsNone(
            expected_occupancy_provenance_warning(self.s.property))

    def test_missing_source_is_flagged(self) -> None:
        probe = copy.deepcopy(self.s.property)
        del probe["expected_final_occupancy"]["_source"]
        message = expected_occupancy_provenance_warning(probe)
        self.assertIsNotNone(message)
        self.assertIn("_source", message)

    def test_blank_source_counts_as_missing(self) -> None:
        """空文字を入れて警告だけ黙らせる、を通さない."""
        probe = copy.deepcopy(self.s.property)
        probe["expected_final_occupancy"]["_source"] = "   "
        self.assertIsNotNone(expected_occupancy_provenance_warning(probe))

    def test_absent_table_is_flagged(self) -> None:
        probe = copy.deepcopy(self.s.property)
        del probe["expected_final_occupancy"]
        self.assertIsNotNone(expected_occupancy_provenance_warning(probe))

    def test_loading_warns_but_does_not_fail(self) -> None:
        import shutil, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            shutil.copytree(ROOT / "config", cfg)
            prop = json.loads((cfg / "property.json").read_text(encoding="utf-8"))
            del prop["expected_final_occupancy"]["_source"]
            (cfg / "property.json").write_text(
                json.dumps(prop, ensure_ascii=False), encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                settings = Settings.load(cfg)
            self.assertIn("出所不明の稼働見込み", err.getvalue())
            self.assertGreater(
                pace_mod.expected_final_occupancy(settings, "SHOULDER"), 0.0)

    def test_no_warning_for_the_shipped_config(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            Settings.load(ROOT / "config")
        self.assertNotIn("出所不明の稼働見込み", err.getvalue())


class ActiveBandTest(unittest.TestCase):
    """実測値へ下げたことで、内部需要の有効帯が縮んだことを固定する.

    期待室数が min_expected_rooms(1.0室) に届かないリード帯は無効化される。
    最終稼働の見込みを 0.75 → 0.28 にすると期待室数は2.7分の1になり、
    有効帯はリード0〜9日から0〜2日へ縮む。

    これは「進捗を見なくなった」ことを意味する。価格が上がるのは
    遅れ判定が消えたからであって、進捗を正しく測れるようになったからではない。
    取り違えると、min_expected_rooms を放置したまま較正できたと誤認する。
    """

    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.day = date(2027, 3, 15)        # SHOULDER の平日

    def _active(self, settings) -> list[int]:
        return [lead for lead in range(0, 31)
                if not pace_mod.evaluate(
                    settings, self.day,
                    self.day - timedelta(days=lead), 0).below_min_expected]

    def test_the_measured_values_narrow_the_active_band(self) -> None:
        legacy = copy.deepcopy(self.s)
        legacy.property["expected_final_occupancy"] = dict(LEGACY_TARGET)
        self.assertLess(len(self._active(self.s)), len(self._active(legacy)),
                        "実測値へ下げても有効帯が縮んでいない。前提が変わった")

    def test_the_active_band_is_recorded(self) -> None:
        self.assertEqual(self._active(self.s), [0, 1, 2],
                         "SHOULDER の有効帯が変わった。"
                         "min_expected_rooms と稼働見込みはセットで決まる")


class BuildScriptTest(unittest.TestCase):
    """再算出スクリプトが、サンプル不足を黙って通さないこと."""

    SCRIPT = ROOT / "scripts" / "build_expected_occupancy.py"
    OTB = ROOT / ".." / ".." / "kanoya-data" / "otb.csv"

    @classmethod
    def setUpClass(cls) -> None:
        if not cls.OTB.resolve().exists():
            raise unittest.SkipTest("実データ未取得（kanoya-data をクローンしてください）")

    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *extra],
            cwd=str(ROOT), capture_output=True, text=True, check=True)

    def test_thin_seasons_are_warned_about(self) -> None:
        """DEEP_LOW は営業4日・実売2室夜しかない。平均を採ってはいけない."""
        result = self._run("--min-days", "30")
        self.assertIn("サンプル不足", result.stderr)
        self.assertIn("DEEP_LOW", result.stderr)

    def test_thin_seasons_get_a_neighbour_value_with_a_note(self) -> None:
        result = self._run("--min-days", "30")
        self.assertIn("流用", result.stdout)
        self.assertIn("_deep_low_note", result.stdout)

    def test_output_states_its_denominator(self) -> None:
        result = self._run()
        self.assertIn("室夜", result.stdout)
        self.assertIn("_source", result.stdout)

    def test_missing_open_days_in_otb_are_flagged(self) -> None:
        """otb.csv は閉館日を機械的に除外して作られており、例外営業が欠ける.

        黙って進むと分母だけ増えて稼働率が下振れする（実績では
        PEAK が 49.1% ではなく 52.6% になった）。
        """
        result = self._run()
        self.assertIn("OTBに営業日の欠けがあります", result.stderr)

    def test_reservations_fill_the_gap(self) -> None:
        result = self._run("--reservations", "../../kanoya-data/reservations.csv")
        self.assertIn("予約明細から補完しました", result.stderr)
        self.assertIn("営業168日", result.stdout)


if __name__ == "__main__":
    unittest.main()
