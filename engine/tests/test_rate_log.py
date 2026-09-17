"""提示価格の日次記録（rate_log）の回帰テスト.

実績には成約価格しか残っていない。いくらで出して売れなかったのかが
分からないため、値付けの良否が測れない。実際にこれで損をしている:
2026年6〜7月の値上げ実験は、提示価格が無かったため定量評価できなかった。

もう一つ、承認区分が機能しない。delta_pct は推奨と現行掲出価格の比なので、
current_public_rate が0だと全日が AUTO_APPLY に落ちる。実データ検証で
自動配信100%と出たのは較正が良いからではなく、比べる相手が無いからだった。
"""

from __future__ import annotations

import ast
import csv
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import rate_log  # noqa: E402
from kanoya_rm.cli import build_context  # noqa: E402
from kanoya_rm.config import Settings  # noqa: E402
from kanoya_rm.products import load as load_products  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def entry(stay: str, snapshot: str, rate: float, form: str = "room_only",
          channel: str = "") -> rate_log.Entry:
    return rate_log.Entry(stay_date=date.fromisoformat(stay),
                          snapshot_date=date.fromisoformat(snapshot),
                          product_type=form, posted_rate=rate, channel=channel)


class ChangeLogTest(unittest.TestCase):
    """記録は変更履歴。1行はより新しい行に上書きされるまで有効."""

    def setUp(self) -> None:
        self.settings = Settings.load(ROOT / "config")
        self.products = load_products(self.settings)
        self.entries = [
            entry("2026-11-21", "2026-11-01", 60000),
            entry("2026-11-21", "2026-11-10", 66000),
            entry("2026-11-22", "2026-11-01", 50000),
        ]

    def test_the_latest_entry_at_or_before_the_snapshot_wins(self) -> None:
        self.assertEqual(
            rate_log.posted_as_of(self.entries, date(2026, 11, 21),
                                  date(2026, 11, 15))["room_only"], 66000)

    def test_a_later_entry_does_not_leak_backwards(self) -> None:
        """基準日より後の記録を使うと、当時の判断を後知恵で塗り替えてしまう."""
        self.assertEqual(
            rate_log.posted_as_of(self.entries, date(2026, 11, 21),
                                  date(2026, 11, 5))["room_only"], 60000)

    def test_an_unchanged_day_needs_no_new_row(self) -> None:
        """毎日書き写させない。書き写す運用は続かない."""
        for offset in range(30):
            self.assertEqual(
                rate_log.posted_as_of(self.entries, date(2026, 11, 21),
                                      date(2026, 11, 10) + timedelta(days=offset)
                                      )["room_only"], 66000)

    def test_no_record_is_an_empty_result_not_a_crash(self) -> None:
        self.assertEqual(
            rate_log.posted_as_of(self.entries, date(2027, 1, 1),
                                  date(2026, 12, 1)), {})

    def test_missing_file_reads_as_empty(self) -> None:
        """新規導入時はファイルが無い。それが正常な初期状態."""
        self.assertEqual(rate_log.read(Path("/nonexistent/rate_log.csv")), [])

    def test_a_broken_row_does_not_stop_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rate_log.csv"
            path.write_text(
                "stay_date,snapshot_date,product_type,posted_rate,channel,note\n"
                "2026-11-21,2026-11-01,room_only,60000,,\n"
                "こわれた行,,,,,\n"
                "2026-11-22,2026-11-01,room_only,50000,,\n",
                encoding="utf-8")
            self.assertEqual(len(rate_log.read(path)), 2)


class RoomRateConversionTest(unittest.TestCase):
    """エンジンが比べるのは部屋代。食事付きの記録は食事加算を引いて戻す."""

    def setUp(self) -> None:
        self.settings = Settings.load(ROOT / "config")
        self.products = load_products(self.settings)

    def test_room_only_is_used_as_is(self) -> None:
        got = rate_log.room_rate_as_of(
            [entry("2026-11-21", "2026-11-01", 60000)],
            date(2026, 11, 21), date(2026, 11, 10), self.products)
        self.assertEqual(got, 60000)

    def test_a_two_meal_record_is_converted_back(self) -> None:
        total = 60000 + self.products.meal_add("two_meals")
        got = rate_log.room_rate_as_of(
            [entry("2026-11-21", "2026-11-01", total, form="two_meals")],
            date(2026, 11, 21), date(2026, 11, 10), self.products)
        self.assertAlmostEqual(got, 60000)

    def test_room_only_wins_when_both_are_recorded(self) -> None:
        """引き算より実測。素泊まりがあればそれを使う."""
        entries = [
            entry("2026-11-21", "2026-11-01", 61000),
            entry("2026-11-21", "2026-11-01",
                  60000 + self.products.meal_add("two_meals"), form="two_meals"),
        ]
        self.assertEqual(
            rate_log.room_rate_as_of(entries, date(2026, 11, 21),
                                     date(2026, 11, 10), self.products), 61000)

    def test_an_unknown_form_is_not_guessed_at(self) -> None:
        """勝手に食事分を引くと部屋代が安く見え、値下げ方向へ働く."""
        got = rate_log.room_rate_as_of(
            [entry("2026-11-21", "2026-11-01", 55000, form="謎のプラン")],
            date(2026, 11, 21), date(2026, 11, 10), self.products)
        self.assertEqual(got, 55000)

    def test_a_channel_filter_selects_one_source(self) -> None:
        entries = [entry("2026-11-21", "2026-11-01", 60000, channel="direct"),
                   entry("2026-11-21", "2026-11-02", 70000, channel="ota")]
        self.assertEqual(
            rate_log.room_rate_as_of(entries, date(2026, 11, 21),
                                     date(2026, 11, 10), self.products,
                                     channel="direct"), 60000)


class EngineIntegrationTest(unittest.TestCase):
    """current_public_rate が rate_log から解決されること."""

    @classmethod
    def setUpClass(cls) -> None:
        if not (ROOT / "data" / "otb.csv").exists():
            raise unittest.SkipTest("収集済みデータ未生成（scripts/run_pipeline.sh）")

    def _context_with(self, entries: list[rate_log.Entry]):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "engine"
        (root / "data").mkdir(parents=True)
        shutil.copytree(ROOT / "config", root / "config")
        for name in ("otb.csv", "comp_rates_collected.csv"):
            source = ROOT / "data" / name
            if source.exists():
                shutil.copy(source, root / "data" / name)
        log = root / "rate_log.csv"
        rate_log.write(log, entries)
        sources = json.loads(
            (root / "config" / "sources.json").read_text(encoding="utf-8"))
        sources["rate_log"]["path"] = str(log)
        sources["rate_log"]["fallback_paths"] = []
        (root / "config" / "sources.json").write_text(
            json.dumps(sources, ensure_ascii=False), encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            ctx = build_context(root, None, 30)
        return ctx, err.getvalue()

    def test_the_log_overrides_the_otb_column(self) -> None:
        baseline, _err = self._context_with([])
        day = sorted(baseline.recommendations)[0]
        marker = 47_000.0
        self.assertNotEqual(baseline.recommendations[day].current_rate, marker)

        ctx, _err = self._context_with([
            rate_log.Entry(stay_date=day, snapshot_date=baseline.snapshot,
                           product_type="room_only", posted_rate=marker)])
        self.assertEqual(ctx.recommendations[day].current_rate, marker)
        self.assertEqual(ctx.posted[day], marker)

    def test_days_without_a_record_fall_back_to_the_otb_column(self) -> None:
        baseline, _err = self._context_with([])
        days = sorted(baseline.recommendations)
        ctx, _err = self._context_with([
            rate_log.Entry(stay_date=days[0], snapshot_date=baseline.snapshot,
                           product_type="room_only", posted_rate=47_000.0)])
        self.assertEqual(ctx.recommendations[days[1]].current_rate,
                         baseline.recommendations[days[1]].current_rate)

    def test_an_empty_log_changes_nothing(self) -> None:
        """記録が無い状態で価格が動いたら、それは副作用."""
        with_log, _err = self._context_with([])
        plain = build_context(ROOT, None, 30)
        self.assertEqual(
            {d: r.recommended_rate for d, r in with_log.recommendations.items()},
            {d: r.recommended_rate for d, r in plain.recommendations.items()})

    def test_a_missing_period_warns_without_stopping(self) -> None:
        ctx, err = self._context_with([])
        self.assertTrue(ctx.recommendations, "警告を出すために処理を止めている")
        self.assertIn("提示価格の記録", err)
        self.assertGreater(ctx.rate_log_coverage.missing, 0)

    def test_the_warning_explains_what_cannot_be_judged(self) -> None:
        """『記録がないから判定できない』が画面で分かること."""
        _ctx, err = self._context_with([])
        self.assertIn("承認区分", err)
        self.assertIn("日次変動幅", err)

    def test_a_stale_log_is_flagged(self) -> None:
        """変更履歴なので、止まっているのか変わっていないのかは日付でしか分からない."""
        baseline, _err = self._context_with([])
        old = baseline.snapshot - timedelta(days=30)
        _ctx, err = self._context_with([
            rate_log.Entry(stay_date=day, snapshot_date=old,
                           product_type="room_only", posted_rate=47_000.0)
            for day in sorted(baseline.recommendations)])
        self.assertIn("止まっています", err)


class NoNetworkTest(unittest.TestCase):
    """log_rates.py が外部通信をしないこと.

    提示価格は運用者が手で入れる。ここでネットワークを触る理由は無く、
    触れるようにしておくと、いつか誰かがOTAのスクレイピングを足す。
    """

    SCRIPT = ROOT / "scripts" / "log_rates.py"
    FORBIDDEN = {"socket", "urllib", "urllib.request", "http", "http.client",
                 "requests", "ftplib", "telnetlib", "smtplib", "asyncio",
                 "xmlrpc", "webbrowser"}

    def test_no_network_module_is_imported(self) -> None:
        tree = ast.parse(self.SCRIPT.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & self.FORBIDDEN, set(),
                         f"ネットワーク系モジュールを読み込んでいる: "
                         f"{sorted(imported & self.FORBIDDEN)}")

    def test_it_runs_with_sockets_disabled(self) -> None:
        """import を見るだけでは足りない。実際に socket を封じて回す."""
        with tempfile.TemporaryDirectory() as tmp:
            guard = (
                "import socket, sys\n"
                "def _blocked(*a, **k):\n"
                "    raise AssertionError('log_rates.py がネットワークへ出ようとした')\n"
                "socket.socket = _blocked\n"
                "socket.create_connection = _blocked\n"
                "sys.argv = ['log_rates.py', '--log', %r, '--as-of', '2026-08-15',\n"
                "            '--from', '2026-08-15', '--days', '30',\n"
                "            '--set', '8/15-8/31=64000']\n"
                "src = open(%r, encoding='utf-8').read()\n"
                "exec(compile(src, %r, 'exec'), {'__name__': '__main__',\n"
                "                                '__file__': %r})\n"
                % (str(Path(tmp) / "rate_log.csv"), str(self.SCRIPT),
                   str(self.SCRIPT), str(self.SCRIPT))
            )
            result = subprocess.run([sys.executable, "-c", guard],
                                    cwd=str(ROOT), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0,
                             f"socket を封じたら落ちた:\n{result.stderr}")
            self.assertIn("行を追記しました", result.stdout)


class InputHelperTest(unittest.TestCase):
    """差分だけを入れる設計であること（運用が続くかどうかの分かれ目）."""

    SCRIPT = ROOT / "scripts" / "log_rates.py"

    def _run(self, *args, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              cwd=str(ROOT), input=stdin,
                              capture_output=True, text=True)

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = Path(tmp.name) / "rate_log.csv"
        self.base = ["--log", str(self.log), "--as-of", "2026-08-15",
                     "--from", "2026-08-15", "--days", "60"]

    def test_a_span_sets_every_open_day_in_it(self) -> None:
        result = self._run(*self.base, "--set", "8/15-8/31=64000")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = list(csv.DictReader(self.log.open(encoding="utf-8")))
        self.assertTrue(rows)
        self.assertTrue(all(r["posted_rate"] == "64000" for r in rows))

    def test_closed_days_are_skipped(self) -> None:
        """販売していない日の提示価格を記録しても使い道がない."""
        self._run(*self.base, "--set", "8/15-8/31=64000")
        settings = Settings.load(ROOT / "config")
        for row in csv.DictReader(self.log.open(encoding="utf-8")):
            day = date.fromisoformat(row["stay_date"])
            self.assertTrue(settings.is_open(day), f"{day} は閉館日")

    def test_an_unchanged_day_writes_nothing(self) -> None:
        self._run(*self.base, "--set", "8/15-8/31=64000")
        before = self.log.read_text(encoding="utf-8")
        result = self._run("--log", str(self.log), "--as-of", "2026-08-16",
                           "--from", "2026-08-16", "--days", "60", stdin="\n")
        self.assertIn("変更なし", result.stdout)
        self.assertEqual(self.log.read_text(encoding="utf-8"), before)

    def test_the_display_collapses_into_spans(self) -> None:
        """120日を120行出したら読まれない。区間にまとめる."""
        self._run(*self.base, "--set", "8/15-10/13=64000")
        result = self._run(*self.base, "--show")
        spans = [line for line in result.stdout.splitlines()
                 if "円" in line and "-" in line]
        self.assertLessEqual(len(spans), 3,
                             f"同じ値の期間がまとまっていない:\n{result.stdout}")

    def test_a_csv_can_be_imported(self) -> None:
        """将来のサイトコントローラー連携の入口."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "export.csv"
            source.write_text(
                "stay_date,posted_rate\n2026-11-20,71000\n2026-11-21,73000\n",
                encoding="utf-8")
            result = self._run("--log", str(self.log), "--as-of", "2026-08-15",
                               "--import", str(source))
            self.assertEqual(result.returncode, 0, result.stderr)
        rates = {r["stay_date"]: r["posted_rate"]
                 for r in csv.DictReader(self.log.open(encoding="utf-8"))}
        self.assertEqual(rates["2026-11-21"], "73000")

    def test_a_malformed_import_row_is_reported_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "export.csv"
            source.write_text("stay_date,posted_rate\nよくわからない,abc\n",
                              encoding="utf-8")
            result = self._run("--log", str(self.log), "--as-of", "2026-08-15",
                               "--import", str(source))
        self.assertIn("読めなかった行", result.stderr)


if __name__ == "__main__":
    unittest.main()
