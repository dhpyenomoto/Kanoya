"""ADRマトリクスと、探索半径まわりの回帰テスト."""

from __future__ import annotations

import json
import math
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import discovery, matrix  # noqa: E402
from kanoya_rm.cli import build_context  # noqa: E402
from kanoya_rm.sources.base import PlaceRecord, haversine_km  # noqa: E402
from kanoya_rm.sources.places import PlacesSource  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUN_DATE = date(2026, 8, 15)
ORIGIN = (34.6815, 135.8462)


class RadiusTilingTest(unittest.TestCase):
    """半径を広げても小円の隙間に施設を取りこぼさないこと."""

    def _coverage_gap(self, radius_m: int) -> float:
        """探索点のいずれからも遠い地点までの最大距離を測る（隙間の指標）."""
        centers = PlacesSource._ring_centers(ORIGIN[0], ORIGIN[1], radius_m)
        sub = PlacesSource.SUB_RADIUS_M
        worst = 0.0
        # 探索円内を粗くサンプリングし、最寄りの探索点までの距離を見る
        for ring in range(1, 11):
            r = radius_m * ring / 10.0
            for i in range(36):
                theta = 2 * math.pi * i / 36
                lat = ORIGIN[0] + r * math.cos(theta) / 110_574.0
                lon = ORIGIN[1] + r * math.sin(theta) / (
                    111_320.0 * math.cos(math.radians(ORIGIN[0])))
                nearest = min(
                    haversine_km(lat, lon, c[0], c[1]) * 1000 for c in centers
                )
                worst = max(worst, nearest - sub)
        return worst

    def test_no_coverage_gap_at_5km(self) -> None:
        self.assertLessEqual(self._coverage_gap(5000), 0.0,
                             "5km圏に探索円が届かない領域がある（取りこぼしが起きる）")

    def test_no_coverage_gap_at_2500m(self) -> None:
        self.assertLessEqual(self._coverage_gap(2500), 0.0)

    def test_small_radius_is_a_single_request(self) -> None:
        self.assertEqual(PlacesSource.plan_requests(800), 1)

    def test_request_count_grows_with_area_not_linearly(self) -> None:
        """費用は半径の二乗で効く。事前提示できるよう見積もれること."""
        n2500 = PlacesSource.plan_requests(2500)
        n5000 = PlacesSource.plan_requests(5000)
        self.assertGreater(n5000, n2500)
        self.assertGreater(n5000, 20)


class DistanceScoreTest(unittest.TestCase):
    """探索半径を変えても既存施設のスコアが動かないこと（実際に踏んだ設計不良）."""

    def setUp(self) -> None:
        self.config = json.loads(
            (ROOT / "config" / "sources.json").read_text(encoding="utf-8")
        )

    def _place(self, name, lat, lon, level="PRICE_LEVEL_EXPENSIVE",
               rating=4.5, reviews=200) -> PlaceRecord:
        return PlaceRecord(place_id=name, name=name, latitude=lat, longitude=lon,
                           price_level=level, rating=rating, review_count=reviews)

    def test_score_is_independent_of_search_radius(self) -> None:
        places = [self._place("近隣宿", 34.6900, 135.8500)]
        narrow = dict(self.config)
        narrow["discovery"] = {**self.config["discovery"], "radius_m": 2500}
        wide = dict(self.config)
        wide["discovery"] = {**self.config["discovery"], "radius_m": 5000}

        a = discovery.score_candidates(places, ORIGIN, narrow)[0].total
        b = discovery.score_candidates(places, ORIGIN, wide)[0].total
        self.assertAlmostEqual(a, b, places=6,
                               msg="探索半径を変えるとスコアが動いている")

    def test_distance_decay_halves_at_half_distance(self) -> None:
        half = float(self.config["scoring"]["distance_half_km"])
        self.assertAlmostEqual(discovery._distance_score(0.0, half), 1.0)
        self.assertAlmostEqual(discovery._distance_score(half, half), 0.5)
        self.assertAlmostEqual(discovery._distance_score(half * 2, half), 0.25)

    def test_similar_but_distant_beats_dissimilar_but_close(self) -> None:
        """5km圏に広げる目的は、遠くても『似ている』宿を拾うこと."""
        places = [
            self._place("遠方の小規模高級宿", 34.7100, 135.8100,
                        "PRICE_LEVEL_VERY_EXPENSIVE", 4.6, 200),
            self._place("近隣の大型格安ホテル", 34.6820, 135.8470,
                        "PRICE_LEVEL_INEXPENSIVE", 3.9, 2000),
        ]
        rooms = {"遠方の小規模高級宿": 8, "近隣の大型格安ホテル": 300}
        ranked = discovery.score_candidates(places, ORIGIN, self.config, rooms)
        self.assertEqual(ranked[0].name, "遠方の小規模高級宿")


class MatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rates = ROOT / "data" / "comp_rates_collected.csv"
        self.compset = ROOT / "config" / "compset.json"
        if not self.rates.exists() or not self.compset.exists():
            self.skipTest("収集済みデータ未生成（run_pipeline.sh）")
        self.window = (date(2026, 11, 14), date(2026, 11, 30))
        self.ctx = build_context(ROOT, RUN_DATE, 120,
                                 compset_file=self.compset, rates_file=self.rates)
        self.report = matrix.build(self.ctx.settings, self.ctx, self.window,
                                   fixture=True, radius_m=5000)

    def test_window_bounds_are_respected(self) -> None:
        self.assertTrue(self.report.dates)
        self.assertTrue(all(self.window[0] <= d <= self.window[1]
                            for d in self.report.dates))

    def test_own_property_is_pinned_at_top(self) -> None:
        self.assertTrue(self.report.rows[0].is_self)
        self.assertTrue(self.report.rows[1].is_self)
        self.assertTrue(self.report.rows[2].is_summary)

    def test_competitors_are_sorted_by_similarity(self) -> None:
        weights = [r.weight for r in self.report.competitor_rows]
        self.assertEqual(weights, sorted(weights, reverse=True))

    def test_sold_out_and_missing_are_distinct(self) -> None:
        """欠測と売止を同じ記号にすると市場逼迫度の読み違いが起きる."""
        row = self.report.competitor_rows[0]
        row.cells[self.report.dates[0]] = matrix.SOLD_OUT
        row.cells[self.report.dates[1]] = matrix.MISSING
        text = matrix.render_text(self.report)
        self.assertIn("満", text)
        self.assertIn("·", text)
        # HTMLはJSで描画するため、静的マークアップではなく
        # 埋め込みデータと、それを描くCSS/JSの存在を検証する
        html_out = matrix.render_html(self.report)
        payload = matrix._payload(self.report, None)
        cells = payload["rows"][3]["cells"]
        self.assertEqual(cells[self.report.dates[0].isoformat()], "SO")
        self.assertIsNone(cells[self.report.dates[1].isoformat()])
        self.assertIn("td.so{", html_out.replace(" ", ""))
        self.assertIn("td.na{", html_out.replace(" ", ""))

    def test_sold_out_is_excluded_from_average(self) -> None:
        row = matrix.MatrixRow(comp_id="x", name="x", tier="PRIMARY", weight=1.0,
                               rooms=5, distance_km=1.0)
        row.cells = {date(2026, 11, 1): 100000.0,
                     date(2026, 11, 2): matrix.SOLD_OUT,
                     date(2026, 11, 3): matrix.MISSING}
        self.assertEqual(row.average, 100000.0)
        self.assertEqual(row.coverage, 1)
        self.assertEqual(row.soldout_count, 1)

    def test_html_is_a_complete_document_declaring_utf8(self) -> None:
        """単体配布するファイルなので、文書として自己完結している必要がある.

        charset が無いとブラウザが文字コードを推測して日本語が化ける。
        """
        out = matrix.render_html(self.report)
        self.assertTrue(out.lstrip().lower().startswith("<!doctype html>"))
        head = out.encode("utf-8")[:1024].lower()
        self.assertIn(b'<meta charset="utf-8">', head)
        for tag in ("<html", "<head>", "</head>", "<body>", "</body>", "</html>"):
            self.assertIn(tag, out, f"{tag} が無い")

    def test_html_is_self_contained_and_labels_fixture(self) -> None:
        out = matrix.render_html(self.report)
        self.assertIn("<title>", out)
        self.assertIn("フィクスチャ", out)
        self.assertNotIn("http://", out)
        self.assertNotIn("https://", out)   # CSPで外部参照は使えない

    def test_html_defines_colors_outside_media_queries(self) -> None:
        """ダークテーマ指定なしの既定状態でも色が解決すること."""
        out = matrix.render_html(self.report)
        root_block = out.split(":root {", 1)[1].split("}", 1)[0]
        for token in ("--ground", "--surface", "--ink", "--line"):
            self.assertIn(token, root_block)

    def test_ramp_darkens_as_value_rises(self) -> None:
        """価格が高いほど濃くなること（単色ランプの前提）。実装はJS側にある."""
        out = matrix.render_html(self.report)
        self.assertIn("96 - t * 46", out)   # 明度は t の増加に対し単調減少

    def test_empty_window_renders_without_crashing(self) -> None:
        empty = matrix.build(self.ctx.settings, self.ctx,
                             (date(2030, 1, 1), date(2030, 1, 5)))
        self.assertEqual(empty.dates, [])
        self.assertIn("データがありません", matrix.render_text(empty))

    def test_html_embeds_all_collected_dates_not_just_the_window(self) -> None:
        """期間切替をブラウザ側で行うため、HTMLは全期間分のデータを持つ必要がある."""
        full = matrix.build_full(self.ctx.settings, self.ctx, fixture=True)
        self.assertGreater(len(full.dates), len(self.report.dates))
        out = matrix.render_html(full, initial=self.window)
        for day in (full.dates[0], full.dates[-1]):
            self.assertIn(day.isoformat(), out)

    def test_html_initial_selection_matches_requested_window(self) -> None:
        full = matrix.build_full(self.ctx.settings, self.ctx, fixture=True)
        out = matrix.render_html(full, initial=self.window)
        self.assertIn(f'"from": "{self.window[0].isoformat()}"', out)
        self.assertIn(f'"to": "{self.window[1].isoformat()}"', out)

    def test_embedded_json_cannot_break_out_of_the_script_tag(self) -> None:
        """施設名等に </script> が混ざってもページが壊れないこと."""
        full = matrix.build_full(self.ctx.settings, self.ctx, fixture=True)
        full.rows[-1].name = 'X</script><script>alert(1)</script>'
        out = matrix.render_html(full, initial=self.window)
        self.assertNotIn("</script><script>alert", out)
        self.assertIn("<\\/script>", out)

    def test_markdown_renders_a_github_table(self) -> None:
        """非公開リポジトリを社内共有する運用では、GitHub上でそのまま読めることが要件."""
        md = matrix.render_markdown(self.report)
        header_rows = [l for l in md.splitlines() if l.startswith("|")]
        self.assertGreater(len(header_rows), 5)
        widths = {l.count("|") for l in header_rows}
        self.assertEqual(len(widths), 1, "行ごとに列数が違うと表が崩れる")
        self.assertIn(self.ctx.settings.property["property"]["name"], md)

    def test_markdown_keeps_real_names_by_default(self) -> None:
        md = matrix.render_markdown(self.report)
        self.assertNotIn("競合A", md)

    def test_anonymize_hides_competitor_names(self) -> None:
        """外部共有・見本公開時に、実在施設へ擬似価格を紐づけない."""
        anon = matrix.build(self.ctx.settings, self.ctx, self.window,
                            fixture=True, anonymize=True)
        real_names = {c.name for c in self.ctx.settings.competitors.values()}
        labels = [r.name for r in anon.competitor_rows]
        self.assertFalse(set(labels) & real_names, "実名が残っている")
        self.assertEqual(labels[:3], ["競合A", "競合B", "競合C"])
        # 自社名は自分のものなので匿名化しない
        self.assertEqual(anon.rows[0].name, self.ctx.settings.property["property"]["name"])
        self.assertNotIn("ふふ奈良", matrix.render_html(anon))

    def test_anon_label_rolls_over_past_z(self) -> None:
        self.assertEqual(matrix._anon_label(0), "競合A")
        self.assertEqual(matrix._anon_label(25), "競合Z")
        self.assertEqual(matrix._anon_label(26), "競合AA")

    def test_csv_row_width_matches_header(self) -> None:
        import csv as csv_mod
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.csv"
            matrix.write_csv(self.report, path)
            with path.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv_mod.reader(fh))
        self.assertGreater(len(rows), 3)
        self.assertTrue(all(len(r) == len(rows[0]) for r in rows))


if __name__ == "__main__":
    unittest.main()
