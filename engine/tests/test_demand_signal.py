"""内部需要シグナル統合（P2）の回帰テスト.

以前は「予約ペース(b_pace=0.42)」と「残室希少性(b_remain=0.34)」を別項に
持っていたが、どちらも otb_rooms の線形関数で符号も同じだった。
同一変数への二重計上で実効重みが 0.76 になり、5室の施設で予約1件が
入るだけでモデル出力が3倍動いていた。

ここで固定するのは「二度と二重計上に戻らないこと」と
「内部需要が競合ポジションより強くならないこと」である。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import compset, pace as pace_mod  # noqa: E402
from kanoya_rm.cli import build_context  # noqa: E402
from kanoya_rm.config import Settings  # noqa: E402
from kanoya_rm.pricing import base_rate, recommend  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# 水準ガードレール（価格の高さを縛るもの）と、変化率ガードレールを区別する。
# OTBを振るのは「水準」の実験なので、変化率の制限が効くのは当然であり、
# モデルの良し悪しの指標にならない。
LEVEL_GUARDRAIL_KEYWORDS = ("フロア", "天井")


def level_notes(rec) -> list[str]:
    return [n for n in rec.guardrail_notes
            if any(k in n for k in LEVEL_GUARDRAIL_KEYWORDS)]


class CoefficientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Settings.load(ROOT / "config")
        self.coef = self.s.property["coefficients"]

    def test_internal_demand_never_outweighs_the_market(self) -> None:
        """自社5室の売れ行きより、市場全体の価格の方が情報量が多い."""
        self.assertLessEqual(float(self.coef["b_demand"]),
                             float(self.coef["b_comp"]))

    def test_the_double_counted_coefficients_are_gone(self) -> None:
        """旧キーが残っていると、統合したつもりで元に戻せてしまう."""
        for dead in ("b_pace", "b_remain"):
            self.assertNotIn(dead, self.coef,
                             f"{dead} が残っている。b_demand へ統合済み")

    def test_the_formula_comment_matches_the_implementation(self) -> None:
        form = self.coef["_form"]
        self.assertIn("b_demand", form)
        self.assertNotIn("b_pace", form)
        self.assertNotIn("b_remain", form)


class LeadDampingTest(unittest.TestCase):
    """リードタイム減衰は統合シグナルが持つ（以前は残室項だけが持っていた）."""

    def test_near_dates_are_not_damped(self) -> None:
        self.assertEqual(pace_mod.lead_damping(0), 1.0)
        self.assertEqual(pace_mod.lead_damping(21), 1.0)

    def test_damping_decreases_with_lead_time(self) -> None:
        seq = [pace_mod.lead_damping(d) for d in (21, 30, 60, 90, 120, 200)]
        for a, b in zip(seq, seq[1:]):
            self.assertLessEqual(b, a)

    def test_damping_never_reaches_zero(self) -> None:
        """ゼロにすると遠い日付で満室に近づいても価格が動かず、繁忙日を取りこぼす."""
        far = pace_mod.lead_damping(400)
        self.assertGreater(far, 0.0)
        self.assertGreaterEqual(far, pace_mod.DEFAULT_FAR_FLOOR - 1e-9)

    def test_damping_is_applied_to_the_signal(self) -> None:
        s = Settings.load(ROOT / "config")
        near = pace_mod.evaluate(s, date(2026, 9, 5), date(2026, 9, 1), 5)
        far = pace_mod.evaluate(s, date(2026, 12, 20), date(2026, 9, 1), 5)
        self.assertEqual(near.damping, 1.0)
        self.assertLess(far.damping, 1.0)
        self.assertLess(abs(far.z), abs(near.z),
                        "遠い日付の予約1件が、直近と同じ強さで効いている")


class OtbSweepTest(unittest.TestCase):
    """P2 受け入れ条件を全宿泊日で検証する（感度分析と同じ振り方）."""

    @classmethod
    def setUpClass(cls) -> None:
        if not (ROOT / "data" / "otb.csv").exists() or \
           not (ROOT / "data" / "comp_rates_collected.csv").exists():
            raise unittest.SkipTest("収集済みデータ未生成（scripts/run_pipeline.sh）")
        cls.ctx = build_context(ROOT, None, 180)
        cls.s = cls.ctx.settings
        cls.rooms = int(cls.s.property["property"]["rooms"])
        floor = float(cls.s.property["guardrails"]["floor_room_rate"])

        # 基準価格そのものがフロアを下回る日は P11 で別途扱う（除外）
        cls.excluded = [d for d in sorted(cls.ctx.recommendations)
                        if base_rate(cls.s, d)[0] < floor]
        cls.days = [d for d in sorted(cls.ctx.recommendations)
                    if d not in set(cls.excluded)]
        cls.sweeps = {d: cls._sweep(d) for d in cls.days}

    @classmethod
    def _sweep(cls, day):
        snap = cls.ctx.comp_snapshots.get(day)
        baseline = compset.baseline_median(cls.ctx.comp_snapshots, day, cls.s)
        current = float(cls.ctx.otb[day]["current_public_rate"])
        out = []
        for otb in range(cls.rooms + 1):
            p = pace_mod.evaluate(cls.s, day, cls.ctx.snapshot, otb)
            out.append(recommend(cls.s, day, p, snap, baseline, current))
        return out

    def test_condition1_output_swing_stays_within_double(self) -> None:
        """予約1件で価格が3倍動くのはノイズの増幅であって需要の反映ではない."""
        worst, worst_day = 0.0, None
        for day, recs in self.sweeps.items():
            prices = [r.raw_price for r in recs if r.raw_price > 0]
            span = max(prices) / min(prices)
            if span > worst:
                worst, worst_day = span, day
        self.assertLessEqual(worst, 2.0,
                             f"{worst_day} で {worst:.2f}倍 動いている")

    def test_condition2_output_never_falls_as_bookings_rise(self) -> None:
        for day, recs in self.sweeps.items():
            prices = [r.raw_price for r in recs]
            for i, (a, b) in enumerate(zip(prices, prices[1:])):
                self.assertGreaterEqual(
                    b, a - 1e-6,
                    f"{day}: OTB {i}室→{i+1}室 で {a:,.0f}→{b:,.0f} と下がった")

    def test_condition4_level_guardrails_rarely_decide_the_price(self) -> None:
        """フロア・天井がモデル出力を上書きする行が全体の25%以下であること.

        日次変動幅は「変化率」の制限であり、current_rate は特定の1つの
        OTB水準に対応する価格である。OTBを0〜満室まで振れば価格水準そのものが
        動くので、変動幅は必ず効く。モデルの良し悪しの指標にならないため、
        ここでは水準ガードレール（フロア／天井）だけを数える。
        """
        rows = [r for recs in self.sweeps.values() for r in recs]
        over = [r for r in rows if level_notes(r)]
        self.assertLessEqual(len(over) / len(rows), 0.25,
                             f"{len(over)}/{len(rows)} 行で水準ガードレールが上書き")

    def test_condition5_level_overrides_only_on_weak_demand_days(self) -> None:
        weak = ("DEEP_LOW", "LOW")
        offenders = []
        for day, recs in self.sweeps.items():
            season, _ = self.s.season_of(day)
            for otb, rec in enumerate(recs):
                if level_notes(rec) and not (season in weak and otb <= 1):
                    offenders.append((day, season, otb))
        self.assertEqual(offenders, [], f"条件外の上書き: {offenders[:5]}")

    def test_condition6_strong_days_with_bookings_are_model_driven(self) -> None:
        strong = ("SHOULDER", "HIGH", "PEAK")
        offenders = []
        for day, recs in self.sweeps.items():
            season, _ = self.s.season_of(day)
            if season not in strong:
                continue
            for otb, rec in enumerate(recs[2:], start=2):
                if level_notes(rec):
                    offenders.append((day, season, otb))
        self.assertEqual(offenders, [], f"条件6違反: {offenders[:5]}")

    def test_single_internal_demand_term_not_two(self) -> None:
        """二重計上へ戻っていないこと。項が2本に増えたらここで落ちる."""
        for recs in self.sweeps.values():
            for rec in recs:
                factors = [c.factor for c in rec.contributions]
                self.assertEqual(factors.count("demand"), 1)
                self.assertNotIn("pace", factors)
                self.assertNotIn("remain", factors)


if __name__ == "__main__":
    unittest.main()
