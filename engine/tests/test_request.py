"""調査リクエスト（入力枠）の回帰テスト."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import request as req  # noqa: E402
from kanoya_rm import schedule  # noqa: E402
from kanoya_rm.config import Settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 8, 15)


class TokenTest(unittest.TestCase):
    def test_today_words(self) -> None:
        for token in ("today", "TODAY", "本日", "今日"):
            self.assertEqual(req.resolve_token(token, TODAY), TODAY)

    def test_absolute_date(self) -> None:
        self.assertEqual(req.resolve_token("2026-11-01", TODAY), date(2026, 11, 1))

    def test_month_token_start_and_end(self) -> None:
        self.assertEqual(req.resolve_token("2026-11", TODAY), date(2026, 11, 1))
        self.assertEqual(req.resolve_token("2026-11", TODAY, month_end=True),
                         date(2026, 11, 30))
        self.assertEqual(req.resolve_token("2026-02", TODAY, month_end=True),
                         date(2026, 2, 28))

    def test_relative_units(self) -> None:
        self.assertEqual(req.resolve_token("+7d", TODAY), date(2026, 8, 22))
        self.assertEqual(req.resolve_token("+2w", TODAY), date(2026, 8, 29))
        self.assertEqual(req.resolve_token("+3m", TODAY), date(2026, 11, 15))
        self.assertEqual(req.resolve_token("+1y", TODAY), date(2027, 8, 15))
        self.assertEqual(req.resolve_token("-7d", TODAY), date(2026, 8, 8))

    def test_month_arithmetic_clamps_day(self) -> None:
        # 1/31 の1か月後は 2/28（存在しない 2/31 を作らない）
        self.assertEqual(req.resolve_token("+1m", date(2026, 1, 31)), date(2026, 2, 28))

    def test_unparseable_token_explains_valid_formats(self) -> None:
        with self.assertRaises(req.RequestError) as ctx:
            req.resolve_token("らいげつ", TODAY)
        self.assertIn("+90d", str(ctx.exception))


class LoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "survey_request.json"
        self.path.write_text(json.dumps({
            "as_of": "today",
            "target": {"from": "today", "to": "+120d"},
            "conditions": {"adults": 2, "los": 1, "target_position": 1.15},
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_defaults_from_config(self) -> None:
        r = req.load(self.path, today=TODAY)
        self.assertEqual(r.as_of, TODAY)
        self.assertEqual(r.start, TODAY)
        self.assertEqual(r.end, date(2026, 12, 13))
        self.assertEqual(r.days, 121)

    def test_cli_month_shorthand_beats_config_end(self) -> None:
        """--from 2026-11 だけ指定したとき、設定ファイルの to='+120d' に負けてはいけない."""
        r = req.load(self.path, start="2026-11", today=TODAY)
        self.assertEqual(r.start, date(2026, 11, 1))
        self.assertEqual(r.end, date(2026, 11, 30))
        self.assertEqual(r.days, 30)

    def test_explicit_to_beats_month_shorthand(self) -> None:
        r = req.load(self.path, start="2026-11", end="2026-12-15", today=TODAY)
        self.assertEqual(r.end, date(2026, 12, 15))

    def test_days_is_inclusive_of_start(self) -> None:
        r = req.load(self.path, start="2026-11-01", days=30, today=TODAY)
        self.assertEqual(r.end, date(2026, 11, 30))
        self.assertEqual(r.days, 30)

    def test_as_of_and_window_are_independent(self) -> None:
        """『今日の時点で、11月だけを調べる』は正当な要求."""
        r = req.load(self.path, as_of="2026-08-15", start="2026-11-01",
                     end="2026-11-30", today=TODAY)
        self.assertEqual(r.as_of, TODAY)
        self.assertEqual(r.min_lead, 78)
        self.assertEqual(r.max_lead, 107)

    def test_past_start_is_clipped_with_warning(self) -> None:
        r = req.load(self.path, start="2026-07-01", end="2026-09-30", today=TODAY)
        self.assertEqual(r.start, TODAY)
        self.assertTrue(any("過去" in w for w in r.warnings))

    def test_entirely_past_window_is_an_error(self) -> None:
        with self.assertRaises(req.RequestError):
            req.load(self.path, start="2026-01-01", end="2026-02-01", today=TODAY)

    def test_reversed_window_is_an_error(self) -> None:
        with self.assertRaises(req.RequestError):
            req.load(self.path, start="2026-11-30", end="2026-11-01", today=TODAY)

    def test_beyond_horizon_warns_rather_than_silently_returning_nothing(self) -> None:
        r = req.load(self.path, start="2027-06-01", end="2027-06-30",
                     today=TODAY, max_horizon=120)
        self.assertTrue(any("ホライズン" in w for w in r.warnings))

    def test_missing_config_falls_back_to_defaults(self) -> None:
        r = req.load(Path("/nonexistent/survey_request.json"), today=TODAY)
        self.assertEqual(r.as_of, TODAY)
        self.assertEqual(r.days, 121)

    def test_relative_as_of_reproduces_past_snapshot(self) -> None:
        r = req.load(self.path, as_of="-7d", start="-7d", end="+30d", today=TODAY)
        self.assertEqual(r.as_of, date(2026, 8, 8))
        self.assertEqual(r.start, date(2026, 8, 8))

    def test_panel_shows_both_concepts(self) -> None:
        panel = req.render_input_panel(req.load(self.path, start="2026-11", today=TODAY))
        self.assertIn("調査基準日", panel)
        self.assertIn("調査対象期間", panel)
        self.assertIn("2026-11-30", panel)


class WindowedPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings.load(ROOT / "config")
        self.config = json.loads(
            (ROOT / "config" / "sources.json").read_text(encoding="utf-8")
        )

    def test_window_restricts_collection_to_requested_dates(self) -> None:
        window = (date(2026, 11, 1), date(2026, 11, 30))
        plan = schedule.build_plan(self.config, self.settings, TODAY, window=window)
        self.assertTrue(plan.tasks)
        self.assertTrue(all(window[0] <= t.stay_date <= window[1] for t in plan.tasks))

    def test_narrow_window_gets_full_coverage(self) -> None:
        """『11月を調べたい』に週1スロットで4日しか返さないのは要求に応えていない."""
        window = (date(2026, 11, 1), date(2026, 11, 30))
        plan = schedule.build_plan(self.config, self.settings, TODAY, window=window)
        self.assertEqual(plan.request_count, 30)

    def test_broad_window_stays_tiered(self) -> None:
        window = (TODAY, date(2026, 12, 13))
        plan = schedule.build_plan(self.config, self.settings, TODAY, window=window)
        self.assertLess(plan.request_count, 121)

    def test_full_flag_overrides_auto_decision(self) -> None:
        window = (TODAY, date(2026, 12, 13))
        plan = schedule.build_plan(self.config, self.settings, TODAY,
                                   window=window, full=True)
        self.assertEqual(plan.request_count, 121)

    def test_tiered_flag_overrides_narrow_window(self) -> None:
        window = (date(2026, 11, 1), date(2026, 11, 30))
        plan = schedule.build_plan(self.config, self.settings, TODAY,
                                   window=window, full=False)
        self.assertLess(plan.request_count, 30)

    def test_window_beyond_default_horizon_is_still_planned(self) -> None:
        """ホライズン既定値の外でも、明示指定されたら計画に載せる（警告は request 側）."""
        window = (date(2027, 1, 1), date(2027, 1, 31))
        plan = schedule.build_plan(self.config, self.settings, TODAY, window=window)
        self.assertTrue(plan.tasks)


if __name__ == "__main__":
    unittest.main()
