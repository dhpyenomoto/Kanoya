"""オンデマンド調査エンドポイントの回帰テスト.

この経路は**押すたびに課金される**。認証と上限が壊れていても
画面上は正常に見えるため、テストで縛る以外に気づく方法がない。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kanoya_rm import survey_api  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 8, 22)


class TokenTest(unittest.TestCase):
    def test_missing_server_token_fails_closed(self) -> None:
        """設定漏れで「誰でも叩ける」状態になってはいけない（課金経路のため）."""
        with self.assertRaises(survey_api.SurveyDenied) as cm:
            survey_api.check_token("whatever", None)
        self.assertEqual(cm.exception.status, 503)

    def test_anonymous_requires_an_explicit_opt_in(self) -> None:
        survey_api.check_token(None, None, allow_anonymous=True)   # 例外が出ないこと

    def test_wrong_token_is_rejected(self) -> None:
        with self.assertRaises(survey_api.SurveyDenied) as cm:
            survey_api.check_token("nope", "secret")
        self.assertEqual(cm.exception.status, 401)

    def test_absent_token_is_rejected(self) -> None:
        with self.assertRaises(survey_api.SurveyDenied) as cm:
            survey_api.check_token(None, "secret")
        self.assertEqual(cm.exception.status, 401)

    def test_correct_token_passes(self) -> None:
        survey_api.check_token("secret", "secret")


class RangeTest(unittest.TestCase):
    def test_span_is_capped(self) -> None:
        """1回の押下で青天井にリクエストを出させない."""
        with self.assertRaises(survey_api.SurveyDenied) as cm:
            survey_api.parse_range("2026-09-01", "2026-11-30", today=TODAY)
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("31日", cm.exception.message)

    def test_reversed_range_is_rejected(self) -> None:
        with self.assertRaises(survey_api.SurveyDenied):
            survey_api.parse_range("2026-11-20", "2026-11-10", today=TODAY)

    def test_fully_past_range_is_rejected(self) -> None:
        """過去日は Google Hotels に価格が無く、リクエストを捨てるだけ."""
        with self.assertRaises(survey_api.SurveyDenied):
            survey_api.parse_range("2020-01-01", "2020-01-03", today=TODAY)

    def test_range_starting_in_the_past_is_clipped_not_rejected(self) -> None:
        days = survey_api.parse_range("2026-08-01", "2026-08-24", today=TODAY)
        self.assertEqual(days[0], TODAY)
        self.assertEqual(days[-1], date(2026, 8, 24))

    def test_far_future_is_rejected(self) -> None:
        far = (TODAY + timedelta(days=500)).isoformat()
        with self.assertRaises(survey_api.SurveyDenied):
            survey_api.parse_range(far, far, today=TODAY)

    def test_malformed_dates_are_rejected(self) -> None:
        with self.assertRaises(survey_api.SurveyDenied) as cm:
            survey_api.parse_range("2026/11/14", "2026-11-16", today=TODAY)
        self.assertEqual(cm.exception.status, 400)

    def test_single_day_is_one_request(self) -> None:
        self.assertEqual(
            survey_api.parse_range("2026-11-14", "2026-11-14", today=TODAY),
            [date(2026, 11, 14)])


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_requests_accumulate_within_the_day(self) -> None:
        led = survey_api.Ledger(self.path, daily_max=10)
        led.reserve(4, TODAY)
        led.reserve(3, TODAY)
        self.assertEqual(led.spent_today(TODAY), 7)

    def test_exceeding_the_daily_cap_is_refused(self) -> None:
        led = survey_api.Ledger(self.path, daily_max=10)
        led.reserve(8, TODAY)
        with self.assertRaises(survey_api.SurveyDenied) as cm:
            led.reserve(5, TODAY)
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(led.spent_today(TODAY), 8, "拒否した分を数えてはいけない")

    def test_the_cap_resets_on_a_new_day(self) -> None:
        led = survey_api.Ledger(self.path, daily_max=10)
        led.reserve(10, TODAY)
        led.reserve(10, TODAY + timedelta(days=1))      # 例外が出ないこと

    def test_an_unwritable_ledger_does_not_block_the_survey(self) -> None:
        """計数できないことを理由に、業務を止めない."""
        led = survey_api.Ledger(Path("/proc/nonexistent/ledger.json"), daily_max=10)
        led.reserve(1, TODAY)


class EndToEndTest(unittest.TestCase):
    """フィクスチャ経由で、HTTP入口から表に流せる形まで通す."""

    def setUp(self) -> None:
        if not (ROOT / "data" / "fixtures").exists():
            self.skipTest("フィクスチャ未生成（scripts/make_fixtures.py）")
        self.tmp = tempfile.TemporaryDirectory()
        self.config = survey_api.Config(
            token="t0ken", allow_anonymous=False, source_name="fixture",
            max_days=31, daily_max=100,
            ledger_path=Path(self.tmp.name) / "l.json", allowed_origins=[])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, **body):
        return survey_api.handle({"token": "t0ken", **body},
                                 root=ROOT, config=self.config, today=TODAY)

    def test_returns_cells_keyed_by_comp_id(self) -> None:
        """画面側は comp_id で行に流し込む。名前で突き合わせると改名で壊れる."""
        out = self._run(**{"from": "2026-11-14", "to": "2026-11-16"})
        self.assertEqual(out["dates"],
                         ["2026-11-14", "2026-11-15", "2026-11-16"])
        self.assertTrue(out["cells"])
        for comp_id, cells in out["cells"].items():
            self.assertRegex(comp_id, r"^[a-z0-9_]+$")
            self.assertEqual(set(cells), set(out["dates"]))

    def test_sold_out_and_missing_stay_distinct(self) -> None:
        """欠測と売止を同じ値にすると市場逼迫度の読み違いが起きる."""
        out = self._run(**{"from": "2026-11-14", "to": "2026-11-16"})
        values = {v for cells in out["cells"].values() for v in cells.values()}
        self.assertTrue(values <= {None, survey_api.SOLD_OUT} |
                        {v for v in values if isinstance(v, int)})

    def test_request_count_is_one_per_stay_date(self) -> None:
        """エリア一括検索なので、施設数ではなく日数で課金される."""
        out = self._run(**{"from": "2026-11-14", "to": "2026-11-16"})
        self.assertEqual(out["requests"], 3)
        self.assertGreater(out["costUsd"], 0)

    def test_median_is_returned_for_every_date(self) -> None:
        out = self._run(**{"from": "2026-11-14", "to": "2026-11-16"})
        self.assertEqual(set(out["median"]), set(out["dates"]))

    def test_fixture_data_is_flagged(self) -> None:
        """擬似データを実勢価格と取り違えさせない."""
        self.assertTrue(self._run(**{"from": "2026-11-14",
                                     "to": "2026-11-14"})["fixture"])

    def test_bad_token_never_reaches_collection(self) -> None:
        with self.assertRaises(survey_api.SurveyDenied) as cm:
            survey_api.handle({"token": "wrong", "from": "2026-11-14",
                               "to": "2026-11-14"},
                              root=ROOT, config=self.config, today=TODAY)
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(
            survey_api.Ledger(self.config.ledger_path).spent_today(TODAY), 0,
            "認証に失敗した要求で予算を消費してはいけない")


class HttpLayerTest(unittest.TestCase):
    """api/survey.py の入出力。ロジックは持たないが、境界は守る必要がある."""

    def setUp(self) -> None:
        import importlib.util
        path = Path(__file__).resolve().parents[2] / "api" / "survey.py"
        if not path.exists():
            self.skipTest("api/survey.py なし")
        spec = importlib.util.spec_from_file_location("api_survey", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_oversized_body_is_refused_before_parsing(self) -> None:
        status, body, _ = self.mod.dispatch(b"x" * (self.mod.MAX_BODY_BYTES + 1))
        self.assertEqual(status, 413)

    def test_malformed_json_returns_400(self) -> None:
        status, body, _ = self.mod.dispatch(b"{oops")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_cors_is_not_opened_to_unlisted_origins(self) -> None:
        """`*` を返すと、どのサイトからでも課金経路を叩けてしまう."""
        headers = self.mod._cors_headers("https://evil.example", ["https://ok.example"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_cors_is_opened_to_listed_origins(self) -> None:
        headers = self.mod._cors_headers("https://ok.example", ["https://ok.example"])
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://ok.example")

    def test_origin_is_echoed_never_wildcarded(self) -> None:
        headers = self.mod._cors_headers("https://ok.example", ["*"])
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://ok.example")
        self.assertEqual(headers.get("Vary"), "Origin")


if __name__ == "__main__":
    unittest.main()
