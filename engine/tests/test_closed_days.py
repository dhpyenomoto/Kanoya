"""閉館日（定休日）の回帰テスト.

鹿のやは従業員数の都合で火曜・水曜を閉館日として運用しているが、
定休日は固定ではない。需要に応じた例外営業が通年で発生する
（2026年2〜9月の実績で閉館日64日のうち8日・12.5%）。

エンジンにこの概念が無いと、販売していない日の価格を毎日計算し、
月曜を毎週「1泊の空隙」と誤検知し、稼働率を実態より低く出す。
さらに例外営業を数えないと、稼働率の分母が実態と食い違う。
"""

from __future__ import annotations

import copy
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import restrictions  # noqa: E402
from kanoya_rm.calibrate import budget_anchor  # noqa: E402
from kanoya_rm.cli import build_context  # noqa: E402
from kanoya_rm.config import Settings  # noqa: E402
from kanoya_rm.pricing import Recommendation  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class ClosedDayResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")

    def test_configured_weekdays_are_closed(self) -> None:
        # 2026-09-01 は火曜、09-02 は水曜
        self.assertTrue(self.s.is_closed(date(2026, 9, 1)))
        self.assertTrue(self.s.is_closed(date(2026, 9, 2)))

    def test_other_weekdays_are_open(self) -> None:
        for day in (date(2026, 8, 31), date(2026, 9, 3), date(2026, 9, 4),
                    date(2026, 9, 5), date(2026, 9, 6)):
            self.assertTrue(self.s.is_open(day), f"{day} が閉館扱い")

    def test_extra_open_overrides_the_weekday_rule(self) -> None:
        """例外営業は繁忙期に限らない。曜日ルールだけでは表現できない."""
        obon_tuesday = date(2026, 8, 11)
        self.assertEqual(self.s.dow_of(obon_tuesday), "TUE")
        self.assertIn(obon_tuesday, self.s.exception_days("extra_open"))
        self.assertTrue(self.s.is_open(obon_tuesday),
                        "日付指定の営業日が曜日指定に負けている")

    def test_extra_closed_beats_an_open_weekday(self) -> None:
        probe = copy.deepcopy(self.s)
        friday = date(2026, 9, 4)
        self.assertTrue(probe.is_open(friday))
        probe.calendar["closed_days"]["extra_closed"] = [
            {"date": friday.isoformat(), "source": "planned", "note": "改装"}]
        self.assertTrue(probe.is_closed(friday), "臨時休館が効いていない")

    def test_extra_open_wins_over_extra_closed(self) -> None:
        """両方に書かれたら営業。休むほうを既定にすると売り逃す."""
        probe = copy.deepcopy(self.s)
        day = date(2026, 9, 1)          # 火曜（曜日では閉館）
        probe.calendar["closed_days"]["extra_open"] = [
            {"date": day.isoformat(), "source": "planned"}]
        probe.calendar["closed_days"]["extra_closed"] = [
            {"date": day.isoformat(), "source": "planned"}]
        self.assertTrue(probe.is_open(day))

    def test_a_property_with_no_closed_weekdays_still_works(self) -> None:
        """定休日なしの施設。曜日リストが空でも例外指定は効くこと."""
        probe = copy.deepcopy(self.s)
        probe.calendar["closed_days"]["closed_weekdays"] = []
        probe.calendar["closed_days"]["extra_open"] = []
        for offset in range(14):
            day = date(2026, 9, 1) + timedelta(days=offset)
            self.assertTrue(probe.is_open(day), f"{day} が閉館扱い")
        shutdown = date(2026, 9, 10)
        probe.calendar["closed_days"]["extra_closed"] = [
            {"date": shutdown.isoformat(), "source": "planned", "note": "貸切"}]
        self.assertTrue(probe.is_closed(shutdown))

    def test_a_property_with_one_closed_weekday_works(self) -> None:
        """週1定休の施設。曜日を1つ書くだけで済むこと."""
        probe = copy.deepcopy(self.s)
        probe.calendar["closed_days"]["closed_weekdays"] = ["WED"]
        probe.calendar["closed_days"]["extra_open"] = []
        self.assertTrue(probe.is_open(date(2026, 9, 1)))    # 火
        self.assertTrue(probe.is_closed(date(2026, 9, 2)))  # 水

    def test_closed_weekdays_are_not_hardcoded(self) -> None:
        """火・水はコードの既定値ではなく設定値であること."""
        probe = copy.deepcopy(self.s)
        probe.calendar["closed_days"]["closed_weekdays"] = ["MON"]
        probe.calendar["closed_days"]["extra_open"] = []
        self.assertTrue(probe.is_closed(date(2026, 9, 7)))   # 月
        self.assertTrue(probe.is_open(date(2026, 9, 1)))     # 火
        self.assertTrue(probe.is_open(date(2026, 9, 2)))     # 水

    def test_property_without_closed_days_is_always_open(self) -> None:
        """エンジンは施設非依存。定休日を持たない宿にもそのまま適用できること."""
        probe = copy.deepcopy(self.s)
        del probe.calendar["closed_days"]
        for offset in range(14):
            self.assertTrue(probe.is_open(date(2026, 9, 1) + timedelta(days=offset)))

    def test_open_days_counts_business_days_only(self) -> None:
        days = self.s.open_days(date(2026, 9, 7), 28)     # 月曜起点の4週間
        self.assertEqual(len(days), 20, "4週間なら営業日は20日（週5日）")
        self.assertTrue(all(self.s.is_open(d) for d in days))


class GapNightTest(unittest.TestCase):
    """閉館日が隣接する日は空隙にならない.

    空隙が問題なのは「連泊で埋められない1泊分の在庫」だから。
    翌日が閉館なら連泊自体が成立しない。
    """

    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")

    def _rec(self, day: date, remaining: int) -> Recommendation:
        return Recommendation(
            stay_date=day, day_class="", season="SHOULDER", season_label="",
            dow=self.s.dow_of(day), base_rate=100000, raw_price=100000,
            recommended_rate=100000, current_rate=100000, delta_pct=0.0,
            action="AUTO_APPLY", remaining=remaining, lead_days=10)

    def _detect(self, days: dict[date, int]) -> set[date]:
        recs = {d: self._rec(d, r) for d, r in days.items()}
        restrictions.detect_gap_nights(self.s, recs)
        return {d for d, r in recs.items() if r.gap_night}

    def test_monday_before_a_closed_tuesday_is_not_a_gap(self) -> None:
        """火・水が定休なら、月曜は構造的に毎週必ず空隙判定されてしまう."""
        sunday, monday = date(2026, 9, 6), date(2026, 9, 7)
        self.assertEqual(self.s.dow_of(monday), "MON")
        self.assertTrue(self.s.is_closed(monday + timedelta(days=1)))
        flagged = self._detect({sunday: 0, monday: 5,
                                monday + timedelta(days=1): 0})
        self.assertNotIn(monday, flagged, "月曜が空隙と誤検知されている")

    def test_a_real_gap_between_two_open_days_is_still_detected(self) -> None:
        """誤検知を消すために本当の空隙まで消してはいけない."""
        thu, fri, sat = date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 5)
        for d in (thu, fri, sat):
            self.assertTrue(self.s.is_open(d))
        flagged = self._detect({thu: 0, fri: 5, sat: 0})
        self.assertIn(fri, flagged, "営業日に挟まれた空隙が検知されない")

    def test_no_weekly_false_positives_across_the_horizon(self) -> None:
        """実データで回したときに、毎週同じ曜日が並ばないこと."""
        ctx = build_context(ROOT, None, 120)
        flagged = [d for d, r in ctx.recommendations.items() if r.gap_night]
        for day in flagged:
            self.assertTrue(self.s.is_open(day - timedelta(days=1)))
            self.assertTrue(self.s.is_open(day + timedelta(days=1)))


class RecommendationScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not (ROOT / "data" / "otb.csv").exists():
            raise unittest.SkipTest("収集済みデータ未生成（scripts/run_pipeline.sh）")
        cls.ctx = build_context(ROOT, None, 180)
        cls.s = cls.ctx.settings

    def test_closed_days_get_no_recommendation(self) -> None:
        """販売していない日の価格を出しても使い道がない."""
        for day in self.ctx.recommendations:
            self.assertTrue(self.s.is_open(day), f"{day}（閉館日）に推奨が出ている")

    def test_open_days_with_data_still_get_one(self) -> None:
        """閉館日を外すついでに営業日まで落ちていないこと."""
        covered = set(self.ctx.recommendations)
        missing = [d for d in self.ctx.otb
                   if self.s.is_open(d) and d >= self.ctx.snapshot
                   and d < self.ctx.snapshot + timedelta(days=180)
                   and d not in covered]
        self.assertEqual(missing, [], f"営業日なのに推奨が無い: {missing[:5]}")

    def test_tuesdays_and_wednesdays_are_absent(self) -> None:
        dows = {self.s.dow_of(d) for d in self.ctx.recommendations}
        self.assertNotIn("TUE", dows)
        self.assertNotIn("WED", dows)

    def test_pace_is_not_evaluated_for_closed_days(self) -> None:
        for day in self.ctx.paces:
            self.assertTrue(self.s.is_open(day))


class OccupancyDenominatorTest(unittest.TestCase):
    """稼働率・RevPAR の分母は営業日ベースであること."""

    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.start = date(2026, 9, 7)

    def test_anchor_is_computed_over_business_days(self) -> None:
        """暦日で割ると稼働率が実態より低く出て、必要ADRが過大になる."""
        open_only = budget_anchor(self.s, 62000, 0.72, self.start, days=364)

        all_days = copy.deepcopy(self.s)
        del all_days.calendar["closed_days"]        # 全日営業として計算
        calendar_based = budget_anchor(all_days, 62000, 0.72, self.start, days=364)

        self.assertNotAlmostEqual(open_only, calendar_based, delta=1.0,
                                  msg="閉館日が分母から外れていない")

    def test_business_day_count_matches_the_ratio(self) -> None:
        """将来期間は曜日ルールどおり。例外営業はまだ登録されていない.

        登録済みの例外営業は実績（source: actual）だけなので、
        将来だけを見る期間では 5/7 ちょうどになる。ここが 5/7 を
        上回っていたら、予定を実績と混ぜて数えている。
        """
        days = self.s.open_days(self.start, 364)
        self.assertAlmostEqual(len(days) / 364, 5 / 7, places=2)

    def test_a_property_with_no_open_days_fails_loudly(self) -> None:
        """全日閉館の設定で黙って0除算するより、その場で止めるほうがよい."""
        probe = copy.deepcopy(self.s)
        probe.calendar["closed_days"]["closed_weekdays"] = [
            "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        probe.calendar["closed_days"]["extra_open"] = []
        with self.assertRaises(ValueError):
            budget_anchor(probe, 62000, 0.72, self.start, days=7)


if __name__ == "__main__":
    unittest.main()
