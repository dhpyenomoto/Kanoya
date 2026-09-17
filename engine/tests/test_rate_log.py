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

from kanoya_rm import rate_log, report  # noqa: E402
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
    """エンジンが比べるのは部屋代。食事付きの記録は食事加算を引いて戻す.

    ここで見たいのは換算だけなので、実行記録の裏付け（max_unconfirmed_days）は
    外して呼ぶ。裏付けの側は ConfirmationTest で見る。
    """

    def setUp(self) -> None:
        self.settings = Settings.load(ROOT / "config")
        self.products = load_products(self.settings)

    def _room_rate(self, entries, stay, as_of, **kwargs) -> float:
        return rate_log.room_rate_as_of(entries, stay, as_of, self.products,
                                        max_unconfirmed_days=-1, **kwargs)

    def test_room_only_is_used_as_is(self) -> None:
        got = self._room_rate([entry("2026-11-21", "2026-11-01", 60000)],
                              date(2026, 11, 21), date(2026, 11, 10))
        self.assertEqual(got, 60000)

    def test_a_two_meal_record_is_converted_back(self) -> None:
        total = 60000 + self.products.meal_add("two_meals")
        got = self._room_rate(
            [entry("2026-11-21", "2026-11-01", total, form="two_meals")],
            date(2026, 11, 21), date(2026, 11, 10))
        self.assertAlmostEqual(got, 60000)

    def test_room_only_wins_when_both_are_recorded(self) -> None:
        """引き算より実測。素泊まりがあればそれを使う."""
        entries = [
            entry("2026-11-21", "2026-11-01", 61000),
            entry("2026-11-21", "2026-11-01",
                  60000 + self.products.meal_add("two_meals"), form="two_meals"),
        ]
        self.assertEqual(
            self._room_rate(entries, date(2026, 11, 21), date(2026, 11, 10)),
            61000)

    def test_an_unknown_form_is_not_guessed_at(self) -> None:
        """勝手に食事分を引くと部屋代が安く見え、値下げ方向へ働く."""
        got = self._room_rate(
            [entry("2026-11-21", "2026-11-01", 55000, form="謎のプラン")],
            date(2026, 11, 21), date(2026, 11, 10))
        self.assertEqual(got, 55000)

    def test_a_channel_filter_selects_one_source(self) -> None:
        entries = [entry("2026-11-21", "2026-11-01", 60000, channel="direct"),
                   entry("2026-11-21", "2026-11-02", 70000, channel="ota")]
        self.assertEqual(
            self._room_rate(entries, date(2026, 11, 21), date(2026, 11, 10),
                            channel="direct"), 60000)


class EngineIntegrationTest(unittest.TestCase):
    """current_public_rate が rate_log から解決されること."""

    @classmethod
    def setUpClass(cls) -> None:
        if not (ROOT / "data" / "otb.csv").exists():
            raise unittest.SkipTest("収集済みデータ未生成（scripts/run_pipeline.sh）")

    def _context_with(self, entries: list[rate_log.Entry],
                      runs: list[rate_log.Run] | None = None):
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
        rate_log.write_runs(rate_log.runs_path_for(log), runs or [])
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

        ctx, _err = self._context_with(
            [rate_log.Entry(stay_date=day, snapshot_date=baseline.snapshot,
                            product_type="room_only", posted_rate=marker)],
            [rate_log.Run(run_date=baseline.snapshot, stay_from=day,
                          stay_to=day, product_type="room_only")])
        self.assertEqual(ctx.recommendations[day].current_rate, marker)
        self.assertEqual(ctx.posted[day], marker)

    def test_days_without_a_record_fall_back_to_the_otb_column(self) -> None:
        baseline, _err = self._context_with([])
        days = sorted(baseline.recommendations)
        ctx, _err = self._context_with(
            [rate_log.Entry(stay_date=days[0], snapshot_date=baseline.snapshot,
                            product_type="room_only", posted_rate=47_000.0)],
            [rate_log.Run(run_date=baseline.snapshot, stay_from=days[0],
                          stay_to=days[0], product_type="room_only")])
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
        self.assertIn("提示価格", err)
        self.assertGreater(ctx.rate_log_coverage.missing, 0)

    def test_the_warning_explains_what_cannot_be_judged(self) -> None:
        """『記録がないから判定できない』が画面で分かること."""
        _ctx, err = self._context_with([])
        self.assertIn("承認区分", err)
        self.assertIn("日次変動幅", err)

    def test_a_long_gap_since_the_last_run_is_flagged(self) -> None:
        """『変わっていない』のか『誰も見ていない』のかは実行記録でしか分からない."""
        baseline, _err = self._context_with([])
        old = baseline.snapshot - timedelta(days=30)
        days = sorted(baseline.recommendations)
        _ctx, err = self._context_with(
            [rate_log.Entry(stay_date=day, snapshot_date=old,
                            product_type="room_only", posted_rate=47_000.0)
             for day in days],
            [rate_log.Run(run_date=old, stay_from=days[0], stay_to=days[-1],
                          product_type="room_only")])
        self.assertIn("未実行です", err)


class ConfirmationTest(unittest.TestCase):
    """「確認して変わっていない」と「誰も見ていない」を区別する.

    変更履歴だけを持つと、行が無い日が両方を意味してしまう。後者を前者と
    読むと、**確認していない日に「価格を据え置いた」という事実でない記録**が
    残る。しかもその誤りは、あとから区別する手がかりが無い。

    そこで実行そのものを別ファイルに残し、裏付けのある値だけを使う。
    """

    def setUp(self) -> None:
        self.settings = Settings.load(ROOT / "config")
        self.products = load_products(self.settings)
        self.stay = date(2026, 11, 21)
        self.entries = [entry("2026-11-21", "2026-11-01", 60000)]

    def _rate(self, runs, as_of, limit=3) -> float:
        return rate_log.room_rate_as_of(self.entries, self.stay, as_of,
                                        self.products, runs=runs,
                                        max_unconfirmed_days=limit)

    def _run(self, on: str) -> rate_log.Run:
        return rate_log.Run(run_date=date.fromisoformat(on),
                            stay_from=date(2026, 11, 1),
                            stay_to=date(2026, 12, 31))

    # ---- 未実行と変更なしの区別 ------------------------------------
    def test_a_confirmed_day_resolves(self) -> None:
        """人が確認していれば、値が変わっていなくても使える."""
        self.assertEqual(self._rate([self._run("2026-11-10")],
                                    date(2026, 11, 10)), 60000)

    def test_confirmation_carries_for_a_few_days(self) -> None:
        """毎日実行できないことはある。数日は裏付けとして認める（設定値）."""
        self.assertEqual(self._rate([self._run("2026-11-10")],
                                    date(2026, 11, 13)), 60000)

    def test_an_unrun_period_does_not_resolve(self) -> None:
        """据え置きを仮定しない。ここが仮定に変わると、事実でない記録が残る."""
        self.assertEqual(self._rate([self._run("2026-11-10")],
                                    date(2026, 11, 20)), 0.0)

    def test_never_run_does_not_resolve(self) -> None:
        self.assertEqual(self._rate([], date(2026, 11, 20)), 0.0)

    def test_a_run_outside_the_stay_range_does_not_confirm_it(self) -> None:
        """別の期間を見ただけでは、この宿泊日を確認したことにならない."""
        elsewhere = rate_log.Run(run_date=date(2026, 11, 20),
                                 stay_from=date(2027, 1, 1),
                                 stay_to=date(2027, 1, 31))
        self.assertEqual(self._rate([elsewhere], date(2026, 11, 20)), 0.0)

    def test_a_later_run_revives_an_old_entry(self) -> None:
        """値は変わっていないが、確認し直したので使える."""
        runs = [self._run("2026-11-10"), self._run("2026-11-20")]
        self.assertEqual(self._rate(runs, date(2026, 11, 20)), 60000)

    def test_a_run_does_not_confirm_a_future_price_change(self) -> None:
        """実行日より後の変更を、その実行が裏付けたことにはできない."""
        entries = self.entries + [entry("2026-11-21", "2026-11-15", 70000)]
        got = rate_log.room_rate_as_of(entries, self.stay, date(2026, 11, 16),
                                       self.products,
                                       runs=[self._run("2026-11-14")],
                                       max_unconfirmed_days=3)
        self.assertEqual(got, 60000, "実行日より後の記録を裏付け済みにしている")

    def test_the_migration_escape_hatch_still_works(self) -> None:
        """-1 は裏付けを問わない（導入前データ用・常用しない）."""
        self.assertEqual(self._rate([], date(2026, 11, 20), limit=-1), 60000)

    # ---- 集計 ------------------------------------------------------
    def test_coverage_separates_unconfirmed_from_never_recorded(self) -> None:
        days = [date(2026, 11, 21), date(2026, 11, 22)]
        got = rate_log.coverage(self.entries, days, date(2026, 11, 20),
                                self.products, runs=[self._run("2026-11-10")],
                                max_unconfirmed_days=3)
        self.assertEqual(got.covered, 0)
        self.assertEqual(got.unconfirmed, 1, "記録はあるが未確認の日")
        self.assertEqual(got.never_recorded, 1, "一度も記録の無い日")
        self.assertEqual(got.missing, got.unconfirmed + got.never_recorded)

    def test_coverage_reports_the_gap_since_the_last_run(self) -> None:
        got = rate_log.coverage(self.entries, [self.stay], date(2026, 11, 20),
                                self.products, runs=[self._run("2026-11-10")])
        self.assertEqual(got.last_run, date(2026, 11, 10))
        self.assertEqual(got.days_since_run, 10)

    def test_unconfirmed_days_is_none_when_never_run(self) -> None:
        self.assertIsNone(rate_log.unconfirmed_days([], date(2026, 11, 20)))

    # ---- 保存形式 --------------------------------------------------
    def test_runs_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rate_log_runs.csv"
            rate_log.write_runs(path, [self._run("2026-11-10")])
            self.assertEqual(rate_log.read_runs(path),
                             [self._run("2026-11-10")])

    def test_the_runs_file_sits_next_to_the_price_log(self) -> None:
        self.assertEqual(
            rate_log.runs_path_for(Path("/x/rate_log.csv")).name,
            "rate_log_runs.csv")

    def test_a_missing_runs_file_reads_as_empty(self) -> None:
        self.assertEqual(rate_log.read_runs(Path("/nonexistent/r.csv")), [])


class EngineConfirmationTest(unittest.TestCase):
    """未実行期間の current_public_rate が解決されないこと（エンジン側）."""

    @classmethod
    def setUpClass(cls) -> None:
        if not (ROOT / "data" / "otb.csv").exists():
            raise unittest.SkipTest("収集済みデータ未生成（scripts/run_pipeline.sh）")

    def _context(self, runs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "engine"
        (root / "data").mkdir(parents=True)
        shutil.copytree(ROOT / "config", root / "config")
        for name in ("otb.csv", "comp_rates_collected.csv"):
            source = ROOT / "data" / name
            if source.exists():
                shutil.copy(source, root / "data" / name)
        probe = build_context(ROOT, None, 30)
        snapshot = probe.snapshot
        days = sorted(probe.recommendations)
        log = root / "rate_log.csv"
        rate_log.write(log, [
            rate_log.Entry(stay_date=day, snapshot_date=snapshot - timedelta(days=20),
                           product_type="room_only", posted_rate=47_000.0)
            for day in days])
        rate_log.write_runs(rate_log.runs_path_for(log),
                            [r(snapshot, days) for r in runs])
        sources = json.loads(
            (root / "config" / "sources.json").read_text(encoding="utf-8"))
        sources["rate_log"]["path"] = str(log)
        sources["rate_log"]["fallback_paths"] = []
        (root / "config" / "sources.json").write_text(
            json.dumps(sources, ensure_ascii=False), encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            ctx = build_context(root, None, 30)
        return ctx, err.getvalue(), days

    @staticmethod
    def _recent(snapshot, days):
        return rate_log.Run(run_date=snapshot, stay_from=days[0],
                            stay_to=days[-1], product_type="room_only")

    @staticmethod
    def _old(snapshot, days):
        return rate_log.Run(run_date=snapshot - timedelta(days=20),
                            stay_from=days[0], stay_to=days[-1],
                            product_type="room_only")

    def test_a_recent_run_resolves_the_posted_rate(self) -> None:
        ctx, _err, days = self._context([self._recent])
        self.assertEqual(ctx.recommendations[days[0]].current_rate, 47_000.0)

    def test_an_unrun_period_leaves_it_unresolved(self) -> None:
        """据え置きを仮定しない。otb.csv 側の値へ落ちる."""
        ctx, err, days = self._context([self._old])
        self.assertNotEqual(ctx.recommendations[days[0]].current_rate, 47_000.0)
        self.assertEqual(ctx.posted, {})
        self.assertIn("未実行です", err)

    def test_the_unresolved_days_are_counted_as_unconfirmed(self) -> None:
        ctx, _err, _days = self._context([self._old])
        coverage = ctx.rate_log_coverage
        self.assertEqual(coverage.covered, 0)
        self.assertEqual(coverage.never_recorded, 0,
                         "記録はあるのに『一度も記録なし』に数えている")
        self.assertEqual(coverage.unconfirmed, coverage.total)

    def test_the_summary_line_separates_the_two_states(self) -> None:
        ctx, _err, _days = self._context([self._old])
        line = report.rate_log_line(ctx.rate_log_coverage)
        self.assertIn("記録はあるが未確認", line)
        self.assertNotIn("一度も記録なし", line)


class ConfirmedRangeTest(unittest.TestCase):
    """「どこを確認したか」を、入力の仕方より広く言い切らないこと.

    広く言い切ると、見ていない宿泊日まで「確認済み」になり、まさに
    避けたかった『事実でない記録』が実行記録の側にできあがる。
    """

    SCRIPT = ROOT / "scripts" / "log_rates.py"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.log = self.dir / "rate_log.csv"

    def _run(self, *args, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.SCRIPT),
                               "--log", str(self.log), *args],
                              cwd=str(ROOT), input=stdin,
                              capture_output=True, text=True)

    def _runs(self) -> list[rate_log.Run]:
        return rate_log.read_runs(rate_log.runs_path_for(self.log))

    def test_set_confirms_only_the_named_spans(self) -> None:
        self._run("--as-of", "2026-08-15", "--from", "2026-08-15",
                  "--days", "120", "--set", "9/1-9/10=59000")
        run = self._runs()[-1]
        self.assertEqual(run.stay_from, date(2026, 9, 3))   # 9/1,9/2 は閉館日
        self.assertEqual(run.stay_to, date(2026, 9, 10))

    def test_import_confirms_only_the_dates_in_the_file(self) -> None:
        source = self.dir / "export.csv"
        source.write_text("stay_date,posted_rate\n2026-08-20,71000\n",
                          encoding="utf-8")
        self._run("--as-of", "2026-08-15", "--from", "2026-08-15",
                  "--days", "120", "--import", str(source))
        run = self._runs()[-1]
        self.assertEqual((run.stay_from, run.stay_to),
                         (date(2026, 8, 20), date(2026, 8, 20)))

    def test_an_interactive_session_confirms_the_whole_window(self) -> None:
        """画面に出した期間は人が見ている。そこは確認済みでよい."""
        self._run("--as-of", "2026-08-15", "--from", "2026-08-15",
                  "--days", "30", stdin="\n")
        run = self._runs()[-1]
        self.assertEqual(run.stay_from, date(2026, 8, 15))
        self.assertGreater(run.stay_to, date(2026, 9, 1))

    def test_show_confirms_nothing(self) -> None:
        """読むだけの画面で確認済みにしない."""
        self._run("--as-of", "2026-08-15", "--from", "2026-08-15",
                  "--days", "30", "--show")
        self.assertEqual(self._runs(), [])

    def test_dry_run_confirms_nothing(self) -> None:
        self._run("--as-of", "2026-08-15", "--from", "2026-08-15",
                  "--days", "30", "--set", "8/15-8/31=64000", "--dry-run")
        self.assertEqual(self._runs(), [])
        self.assertFalse(self.log.exists())


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

    def test_an_unchanged_day_writes_no_price_row(self) -> None:
        self._run(*self.base, "--set", "8/15-8/31=64000")
        before = self.log.read_text(encoding="utf-8")
        result = self._run("--log", str(self.log), "--as-of", "2026-08-16",
                           "--from", "2026-08-16", "--days", "60", stdin="\n")
        self.assertIn("価格の変更はありません", result.stdout)
        self.assertEqual(self.log.read_text(encoding="utf-8"), before)

    def test_an_unchanged_day_still_records_the_run(self) -> None:
        """『確認して変わっていない』を残さないと、未実行と区別できない."""
        self._run(*self.base, "--set", "8/15-8/31=64000")
        self._run("--log", str(self.log), "--as-of", "2026-08-16",
                  "--from", "2026-08-16", "--days", "60", stdin="\n")
        runs = rate_log.read_runs(rate_log.runs_path_for(self.log))
        self.assertEqual([r.run_date for r in runs],
                         [date(2026, 8, 15), date(2026, 8, 16)])
        self.assertIn("変更なしを確認", runs[-1].note)

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
