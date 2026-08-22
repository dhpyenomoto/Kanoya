"""公開サイト（site/）の回帰テスト.

site/ はホスティング側でビルドせず、生成済みのものをコミットする方針のため、
「コミットされている site/ が壊れていないか」を検証する。
生成そのもの（build_site.py）は markdown パッケージを要するため、
未インストール環境ではスキップする。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"

EXPECTED = {
    "index.html", "01-proposal.html", "02-data-collection.html",
    "03-architecture.html", "04-roadmap-roi.html", "05-operations.html",
    "adr-matrix-table.html", "adr-matrix.html", "engine.html",
}


class SiteTest(unittest.TestCase):
    def setUp(self) -> None:
        if not SITE.exists():
            self.skipTest("site/ 未生成（scripts/build_site.py）")
        self.pages = {p.name: p.read_text(encoding="utf-8") for p in SITE.glob("*.html")}

    def test_all_expected_pages_exist(self) -> None:
        self.assertEqual(EXPECTED - set(self.pages), set())

    def test_no_broken_internal_links(self) -> None:
        """リンク切れはサイトの主要な壊れ方なので必ず検出する."""
        broken: list[str] = []
        for name, html in self.pages.items():
            for href in re.findall(r'href="([^"#?]+\.html)[^"]*"', html):
                if href.startswith(("http://", "https://", "//")):
                    continue
                if not (SITE / href).exists():
                    broken.append(f"{name} → {href}")
        self.assertEqual(broken, [], f"リンク切れ: {broken[:8]}")

    def test_no_unconverted_markdown_links_remain(self) -> None:
        """.md へのリンクが残っていると、ブラウザでは生テキストが落ちてくる."""
        leftovers: list[str] = []
        for name, html in self.pages.items():
            for href in re.findall(r'href="([^"]+\.md)"', html):
                leftovers.append(f"{name} → {href}")
        self.assertEqual(leftovers, [], f"未変換リンク: {leftovers[:8]}")

    def test_every_page_has_navigation(self) -> None:
        for name, html in self.pages.items():
            if name == "adr-matrix.html":
                continue          # マトリクスは単体配布もするため独立したページ
            self.assertIn('class="brand"', html, f"{name} にナビが無い")

    def test_pages_are_self_contained(self) -> None:
        """外部CDN等を参照しないこと（社内ネットワークやオフラインでも開けるように）."""
        for name, html in self.pages.items():
            for src in re.findall(r'(?:src|href)="(https?://[^"]+)"', html):
                self.fail(f"{name} が外部リソースを参照している: {src}")

    def test_internal_pages_ask_not_to_be_indexed(self) -> None:
        """社内資料なので検索エンジンに載せない（アクセス制限の代わりにはならない）."""
        for name, html in self.pages.items():
            self.assertIn("noindex", html, f"{name} に noindex が無い")

    def test_wide_tables_scroll_inside_their_own_container(self) -> None:
        """表が広くてもページ全体が横スクロールしないこと."""
        for name, html in self.pages.items():
            if "<table" not in html:
                continue
            # 器の名前は問わない。横スクロールする箱に入っていればよい
            wrapped = ('class="tablewrap"><table>' in html
                       or 'class="scroll"><table>' in html)
            self.assertTrue(wrapped, f"{name} の表が横スクロール用の器に入っていない")


class VercelConfigTest(unittest.TestCase):
    """_config.yml があると Vercel が Jekyll と誤検出して失敗する（実際に踏んだ）."""

    def setUp(self) -> None:
        import json
        path = ROOT / "vercel.json"
        if not path.exists():
            self.skipTest("vercel.json なし")
        self.cfg = json.loads(path.read_text(encoding="utf-8"))

    def test_framework_detection_is_disabled(self) -> None:
        self.assertIn("framework", self.cfg)
        self.assertIsNone(self.cfg["framework"])

    def test_no_build_command_is_configured(self) -> None:
        """ホスティング側でビルドさせない方針。ビルドが無いものは壊れない."""
        self.assertNotIn("buildCommand", self.cfg)

    def test_serves_the_prebuilt_site_directory(self) -> None:
        self.assertEqual(self.cfg.get("outputDirectory"), "site")
        self.assertTrue((ROOT / "site").exists(), "site/ がコミットされていない")


if __name__ == "__main__":
    unittest.main()
