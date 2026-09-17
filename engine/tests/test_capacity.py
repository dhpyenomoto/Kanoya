"""営業日数と稼働率の分母の回帰テスト.

定休日は固定ではない。火曜・水曜を閉館日としているが、需要に応じた
例外営業が通年で発生する（2026年2〜9月の実績で閉館日64日のうち8日）。
つまり同じ期間でも営業日数は後から変わる。分母を書かない稼働率は、
前回の数字とも他施設の数字とも比較できない。

  暦日224日基準 21.2% ／ 曜日ルール160日基準 29.6% ／ 実績168日基準 28.2%

同じ実売室夜を指しているのに3つ出る。どれが正しいかではなく、
どの分母で割ったかを書かなければ意味が定まらない、というのが要点。
"""

from __future__ import annotations

import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import capacity, report  # noqa: E402
from kanoya_rm.config import (  # noqa: E402
    Settings, closed_days_migration_warning,
)

ROOT = Path(__file__).resolve().parents[1]

# 実績の算出期間。この窓の数字は kanoya-data の実績と一致する。
MEASURED_START = date(2026, 2, 6)
MEASURED_DAYS = 224          # 2026-02-06 〜 2026-09-17
MEASURED_OPEN_DAYS = 168     # 曜日ルール160日 + 例外営業8日
MEASURED_SOLD = 237          # 営業日に売れた室夜


class ProvenanceTest(unittest.TestCase):
    """例外営業の出所（実績か予定か）が区別できること."""

    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")

    def test_actual_and_planned_are_separable(self) -> None:
        every = self.s.exception_days("extra_open")
        actual = self.s.exception_days("extra_open", "actual")
        planned = self.s.exception_days("extra_open", "planned")
        self.assertEqual(actual | planned, every)
        self.assertEqual(actual & planned, set(), "同じ日が両方に数えられている")

    def test_shipped_config_registers_only_measured_days(self) -> None:
        """予定を実績に混ぜると、稼働率の分母を後から検証できなくなる."""
        actual = self.s.exception_days("extra_open", "actual")
        self.assertEqual(len(actual), 8)
        self.assertEqual(self.s.exception_days("extra_open", "planned"), set())

    def test_every_entry_states_its_source(self) -> None:
        for key in ("extra_open", "extra_closed"):
            for entry in self.s.calendar["closed_days"].get(key) or []:
                self.assertIn(entry.get("source"), ("actual", "planned"),
                              f"{key} の {entry} に source が無い")

    def test_a_missing_source_is_treated_as_planned(self) -> None:
        """実績だと言い切れないものを実績側へ入れるほうが危ない."""
        probe = copy.deepcopy(self.s)
        day = date(2026, 9, 1)
        probe.calendar["closed_days"]["extra_open"] = [{"date": day.isoformat()}]
        self.assertEqual(probe.exception_days("extra_open", "planned"), {day})
        self.assertEqual(probe.exception_days("extra_open", "actual"), set())

    def test_stay_through_days_are_not_registered_as_open(self) -> None:
        """2026-02-18(水) は在館者がいるだけで販売していない.

        登録すると営業日数が1日増え、稼働率の分母が実態より大きくなる。
        """
        stay_through = date(2026, 2, 18)
        self.assertNotIn(stay_through, self.s.exception_days("extra_open"))
        self.assertTrue(self.s.is_closed(stay_through))


class MigrationTest(unittest.TestCase):
    """旧構造のまま読んで閉館日が静かに消えないこと."""

    def setUp(self) -> None:
        self.legacy = {"closed_days": {"weekdays": ["TUE", "WED"],
                                       "open_dates": ["2026-08-11"]}}

    def test_legacy_keys_are_flagged(self) -> None:
        message = closed_days_migration_warning(self.legacy)
        self.assertIsNotNone(message)
        self.assertIn("closed_weekdays", message)

    def test_the_shipped_config_is_not_flagged(self) -> None:
        calendar = json.loads(
            (ROOT / "config" / "calendar.json").read_text(encoding="utf-8"))
        self.assertIsNone(closed_days_migration_warning(calendar))

    def test_a_legacy_config_still_closes_its_days(self) -> None:
        """移行の途中でも価格計算は回るべき。止めると何も動かせなくなる."""
        import shutil, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config"
            shutil.copytree(ROOT / "config", cfg)
            calendar = json.loads((cfg / "calendar.json").read_text(encoding="utf-8"))
            calendar["closed_days"] = dict(self.legacy["closed_days"])
            (cfg / "calendar.json").write_text(
                json.dumps(calendar, ensure_ascii=False), encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                settings = Settings.load(cfg)
            self.assertIn("旧構造", err.getvalue())
            self.assertTrue(settings.is_closed(date(2026, 9, 1)))   # 火
            self.assertTrue(settings.is_open(date(2026, 8, 11)))    # 例外営業


class SettledPeriodTest(unittest.TestCase):
    """実績が確定した期間は単一値でよい."""

    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.cap = capacity.measure(self.s, MEASURED_START, MEASURED_DAYS)

    def test_open_days_match_the_measured_period(self) -> None:
        self.assertEqual(self.cap.calendar_days, MEASURED_DAYS)
        self.assertEqual(self.cap.open_days, MEASURED_OPEN_DAYS)
        self.assertTrue(self.cap.settled)

    def test_denominator_states_days_rooms_and_room_nights(self) -> None:
        text = self.cap.denominator()
        self.assertIn("168日", text)
        self.assertIn("5室", text)
        self.assertIn("840室夜", text)

    def test_occupancy_carries_its_denominator(self) -> None:
        text = self.cap.occupancy_text(MEASURED_SOLD)
        self.assertIn("28.2%", text)
        self.assertIn("840室夜", text)
        self.assertIn("237室夜", text)

    def test_revpar_carries_its_denominator(self) -> None:
        text = self.cap.revpar_text(840 * 12_000)
        self.assertIn("840室夜", text)

    def test_calendar_basis_is_not_what_we_report(self) -> None:
        """暦日で割ると実態より低く出る（21.2%）。そちらを既定にしない."""
        low, high = self.cap.occupancy(MEASURED_SOLD)
        self.assertEqual(low, high)
        self.assertGreater(high, MEASURED_SOLD / (MEASURED_DAYS * 5))


class FutureRangeTest(unittest.TestCase):
    """将来期間は幅で出すこと."""

    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.as_of = date(2026, 9, 17)
        self.cap = capacity.measure(self.s, date(2026, 10, 1), 92,
                                    as_of=self.as_of)

    def test_future_capacity_is_a_range(self) -> None:
        self.assertFalse(self.cap.settled)
        self.assertGreater(self.cap.open_days_max, self.cap.open_days)

    def test_lower_bound_is_the_weekday_rule_alone(self) -> None:
        """例外営業ゼロが下限。将来の営業日数はここから増える."""
        weekday_only = sum(1 for d in (self.cap.start + timedelta(days=i)
                                       for i in range(self.cap.calendar_days))
                           if not self.s.closed_by_weekday(d))
        self.assertEqual(self.cap.open_days, weekday_only)

    def test_upper_bound_uses_the_configured_exception_rate(self) -> None:
        expected = self.cap.open_days + round(
            self.cap.future_closed_days * self.s.exception_rate())
        self.assertEqual(self.cap.open_days_max, expected)

    def test_occupancy_is_reported_as_a_range(self) -> None:
        text = self.cap.occupancy_text(50)
        self.assertIn("〜", text)
        low, high = self.cap.occupancy(50)
        self.assertLess(low, high, "営業日数が増えるほど稼働率は下がるはず")

    def test_zero_exception_rate_collapses_the_range(self) -> None:
        """例外営業を行わない施設では幅が出ない（不要な曖昧さを足さない）."""
        probe = copy.deepcopy(self.s)
        probe.calendar["closed_days"]["exception_rate"] = 0.0
        cap = capacity.measure(probe, date(2026, 10, 1), 92, as_of=self.as_of)
        self.assertTrue(cap.settled)
        self.assertNotIn("〜", cap.occupancy_text(50))

    def test_report_prints_the_denominator(self) -> None:
        text = report.capacity_summary(self.s, date(2026, 10, 1), 92,
                                       sold_room_nights=50, as_of=self.as_of)
        self.assertIn("稼働率の分母", text)
        self.assertIn("室夜", text)
        self.assertIn("〜", text, "将来期間なのに単一値で出ている")


if __name__ == "__main__":
    unittest.main()
