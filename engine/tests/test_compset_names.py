"""競合の識別情報を Private 側へ分離したことの回帰テスト.

本体リポジトリ（Kanoya）は Public のため、競合施設の実名・place_id・座標・
距離・評価は Kanoya-data（Private）の compset_names.json に置いてある。
**削除ではなく移動**であり、情報は失われていない。

ここで固定するのは3つ。

1. 対応表が無くてもエンジンが動くこと。無いと落ちるなら、
   Public 側だけをクローンした人が何もできない。
2. 対応表に無い comp_id は、その施設だけ匿名表示になること。
   発見で競合が増えたとき、対応表の追記漏れで全体が壊れないように。
3. 突き合わせが comp_id であること。表示名を鍵にすると、施設が改名した
   瞬間に静かに外れ、その施設だけ匿名へ戻るうえ誰も気づかない。
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import matrix  # noqa: E402
from kanoya_rm.config import (  # noqa: E402
    IDENTITY_FIELDS, Settings, load_competitor_names,
)

ROOT = Path(__file__).resolve().parents[1]


class SplitTest(unittest.TestCase):
    """compset.json 側に識別情報が残っていないこと."""

    def setUp(self) -> None:
        self.compset = json.loads(
            (ROOT / "config" / "compset.json").read_text(encoding="utf-8"))

    def test_no_identifying_fields_remain(self) -> None:
        for comp in self.compset["competitors"]:
            for field_name in IDENTITY_FIELDS + ("score_breakdown",):
                self.assertNotIn(field_name, comp,
                                 f"{comp['id']} に {field_name} が残っている")

    def test_pricing_attributes_are_still_there(self) -> None:
        """価格計算に必要な属性は本体側に残すこと（Private が無くても値は出る）."""
        needed = ("id", "tier", "weight", "pricing_basis", "meal_included",
                  "dinner_uplift", "breakfast_uplift")
        for comp in self.compset["competitors"]:
            for field_name in needed:
                self.assertIn(field_name, comp,
                              f"{comp['id']} に {field_name} が無い")


class WithoutTheTableTest(unittest.TestCase):
    """対応表が無い環境でも動くこと."""

    def _settings_without_table(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name) / "config"
        shutil.copytree(ROOT / "config", cfg)
        sources = json.loads((cfg / "sources.json").read_text(encoding="utf-8"))
        sources["compset_names"]["path"] = "/nonexistent/compset_names.json"
        sources["compset_names"]["fallback_paths"] = []
        (cfg / "sources.json").write_text(
            json.dumps(sources, ensure_ascii=False), encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            settings = Settings.load(cfg)
        return settings, err.getvalue()

    def test_the_engine_still_loads(self) -> None:
        settings, _err = self._settings_without_table()
        self.assertTrue(settings.competitors)

    def test_names_fall_back_to_the_comp_id(self) -> None:
        settings, _err = self._settings_without_table()
        for comp_id, comp in settings.competitors.items():
            self.assertEqual(comp.name, comp_id)

    def test_it_says_so_instead_of_failing_silently(self) -> None:
        _settings, err = self._settings_without_table()
        self.assertIn("対応表が見つかりません", err)

    def test_pricing_attributes_survive(self) -> None:
        """匿名でも NAR 正規化に必要な属性は揃っていること."""
        settings, _err = self._settings_without_table()
        for comp in settings.competitors.values():
            self.assertIn(comp.pricing_basis, ("per_room", "per_person"))
            self.assertIn(comp.meal_included,
                          ("none", "breakfast", "dinner", "dinner_breakfast"))


class PartialTableTest(unittest.TestCase):
    """対応表に無い comp_id だけが匿名になること."""

    def setUp(self) -> None:
        self.table, self.path = load_competitor_names(
            ROOT / "config",
            json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8")))
        if not self.table:
            self.skipTest("対応表が未取得（Kanoya-data をクローンしてください）")

    def _settings_missing(self, dropped: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name) / "config"
        shutil.copytree(ROOT / "config", cfg)
        table = json.loads(self.path.read_text(encoding="utf-8"))
        table["competitors"].pop(dropped)
        names = Path(tmp.name) / "names.json"
        names.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        sources = json.loads((cfg / "sources.json").read_text(encoding="utf-8"))
        sources["compset_names"]["path"] = str(names)
        sources["compset_names"]["fallback_paths"] = []
        (cfg / "sources.json").write_text(
            json.dumps(sources, ensure_ascii=False), encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            settings = Settings.load(cfg)
        return settings, err.getvalue()

    def test_only_the_missing_one_is_anonymous(self) -> None:
        dropped = sorted(self.table)[1]
        settings, _err = self._settings_missing(dropped)
        self.assertEqual(settings.competitors[dropped].name, dropped)
        for comp_id, comp in settings.competitors.items():
            if comp_id != dropped:
                self.assertNotEqual(comp.name, comp_id,
                                    f"{comp_id} まで匿名になっている")

    def test_it_says_which_ones_are_missing(self) -> None:
        dropped = sorted(self.table)[1]
        _settings, err = self._settings_missing(dropped)
        self.assertIn("対応表に無い競合", err)
        self.assertIn(dropped, err)


class JoinKeyTest(unittest.TestCase):
    """突き合わせが comp_id であること（表示名ではない）."""

    def setUp(self) -> None:
        self.table, self.path = load_competitor_names(
            ROOT / "config",
            json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8")))
        if not self.table:
            self.skipTest("対応表が未取得（Kanoya-data をクローンしてください）")

    def test_renaming_a_facility_does_not_break_the_join(self) -> None:
        """施設が改名しても、comp_id が同じなら解決し続けること."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name) / "config"
        shutil.copytree(ROOT / "config", cfg)
        table = json.loads(self.path.read_text(encoding="utf-8"))
        target = sorted(table["competitors"])[0]
        table["competitors"][target]["name"] = "改名後の宿"
        names = Path(tmp.name) / "names.json"
        names.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        sources = json.loads((cfg / "sources.json").read_text(encoding="utf-8"))
        sources["compset_names"]["path"] = str(names)
        sources["compset_names"]["fallback_paths"] = []
        (cfg / "sources.json").write_text(
            json.dumps(sources, ensure_ascii=False), encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            settings = Settings.load(cfg)
        self.assertEqual(settings.competitors[target].name, "改名後の宿")
        self.assertNotIn("対応表に無い競合", err,
                         "改名しただけで突合が外れている（名前で突き合わせていないか）")

    def test_the_table_is_keyed_by_comp_id(self) -> None:
        compset = json.loads(
            (ROOT / "config" / "compset.json").read_text(encoding="utf-8"))
        ids = {c["id"] for c in compset["competitors"]}
        self.assertTrue(set(self.table) >= ids,
                        f"対応表に無い comp_id: {sorted(ids - set(self.table))}")


class NoRealNamesInTheRepoTest(unittest.TestCase):
    """追跡対象のファイルに競合の実名が残っていないこと.

    実名の一覧は Private 側にしか無いので、それを持っている環境でだけ走る。
    生成物（docs/sample-adr-matrix.* と site/）は再生成のたびに実名が
    紛れ込みうるため、ここで止める。
    """

    @classmethod
    def setUpClass(cls) -> None:
        table, _path = load_competitor_names(
            ROOT / "config",
            json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8")))
        cls.names = sorted(
            {str(v.get("name") or "").strip() for v in table.values()} - {""})
        if not cls.names:
            raise unittest.SkipTest("対応表が未取得（Kanoya-data をクローンしてください）")
        cls.repo = ROOT.parent

    def _tracked_files(self) -> list[Path]:
        import subprocess
        out = subprocess.run(["git", "ls-files"], cwd=str(self.repo),
                             capture_output=True, text=True, check=True)
        return [self.repo / line for line in out.stdout.splitlines() if line]

    def test_no_competitor_name_appears_anywhere(self) -> None:
        hits: list[str] = []
        for path in self._tracked_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
                continue
            for name in self.names:
                if name in text:
                    hits.append(f"{path.relative_to(self.repo)} :: {name}")
        self.assertEqual(hits, [], "競合の実名が Public 側に残っている:\n  "
                                   + "\n  ".join(hits[:20]))


if __name__ == "__main__":
    unittest.main()
