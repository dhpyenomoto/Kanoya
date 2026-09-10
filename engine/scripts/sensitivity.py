"""感度分析 — 係数設計の妥当性を数値で確かめる読み取り専用の診断ツール.

係数を変えたときに推奨価格がどう動くかを見る手段がないと、
キャリブレーションは「たぶんこのくらい」の議論にしかならない。
このスクリプトは入力を1つずつ振り、各段階の値を並べて出す。

    # OTB（予約済室数）を 0室〜満室 まで振る
    python3 scripts/sensitivity.py --sweep otb --date 2026-11-21

    # 全競合の食事uplift を 0.5〜2.0倍
    python3 scripts/sensitivity.py --sweep uplift --date 2026-11-21

    # 特定の係数を 0.5〜2.0倍
    python3 scripts/sensitivity.py --sweep coef=b_demand --date 2026-11-21

    # リードタイムを 0〜120日
    python3 scripts/sensitivity.py --sweep lead --date 2026-11-21

    # 複数日を一括評価してサマリだけ出す
    python3 scripts/sensitivity.py --sweep otb --days 30

    # CSVにも落とす
    python3 scripts/sensitivity.py --sweep otb --date 2026-11-21 --csv ../out/sens.csv

**エンジン本体は変更しない。** 設定を複製して差し替え、公開されている
build_context / recommend を呼ぶだけ。診断のために本番経路へ分岐を
足すと、その分岐自体が次の不具合になる。

「ガードレールが価格を決めている割合」について:
  モデル出力と推奨価格の差には、丸め（1,000円単位）による差も混ざる。
  丸めは設計どおりの挙動でありガードレールの上書きではないため、
  判定には recommend() が残す guardrail_notes を使う。
  変動幅・フロア・天井が効いたときだけ notes が入る。
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import compset, pace as pace_mod  # noqa: E402
from kanoya_rm.cli import build_context  # noqa: E402
from kanoya_rm.config import parse_date  # noqa: E402
from kanoya_rm.pricing import recommend  # noqa: E402

FACTORS = ["demand", "comp", "event", "lead"]

UPLIFT_SCALES = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
COEF_SCALES = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
LEAD_POINTS = [0, 3, 7, 14, 21, 30, 45, 60, 90, 120]


@dataclass
class Row:
    """スイープ1点分の観測。1行が表の1行になる."""

    stay_date: date
    knob: str                       # 振った変数の値（表示用）
    knob_value: float
    p_base: float
    z: dict[str, float] = field(default_factory=dict)
    raw_price: float = 0.0          # ガードレール適用前のモデル出力
    recommended: float = 0.0
    current_rate: float = 0.0
    action: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def overridden(self) -> bool:
        """ガードレールがモデル出力を上書きしたか（丸めは含めない）."""
        return bool(self.notes)

    @property
    def gap_yen(self) -> float:
        return self.recommended - self.raw_price


def _row_from(rec, stay: date, knob: str, knob_value: float) -> Row:
    return Row(
        stay_date=stay,
        knob=knob,
        knob_value=knob_value,
        p_base=rec.base_rate,
        z={c.factor: c.z for c in rec.contributions},
        raw_price=rec.raw_price,
        recommended=rec.recommended_rate,
        current_rate=rec.current_rate,
        action=rec.action,
        notes=list(rec.guardrail_notes),
    )


# ---- スイープ本体 -------------------------------------------------------
#
# otb / coef / lead は、pace と競合スナップショットを組み直すだけでよいので
# recommend() を直接呼ぶ。uplift は NAR 正規化そのものが変わるため、
# 競合設定を差し替えて build_context をやり直す（正規化の実経路を通す）。


def sweep_otb(ctx, stay: date) -> list[Row]:
    settings = ctx.settings
    rooms = int(settings.property["property"]["rooms"])
    snap = ctx.comp_snapshots.get(stay)
    baseline = compset.baseline_median(ctx.comp_snapshots, stay, settings)
    current = float(ctx.otb[stay]["current_public_rate"])

    rows = []
    for otb in range(0, rooms + 1):
        p = pace_mod.evaluate(settings, stay, ctx.snapshot, otb)
        rec = recommend(settings, stay, p, snap, baseline, current)
        rows.append(_row_from(rec, stay, f"OTB {otb}室", float(otb)))
    return rows


def sweep_lead(ctx, stay: date) -> list[Row]:
    """リードタイムだけを振る.

    宿泊日を固定したまま基準日を後ろへずらす。競合価格は当該宿泊日の
    ものを据え置き、リードタイム由来の変化だけを見る。
    """
    settings = ctx.settings
    snap = ctx.comp_snapshots.get(stay)
    baseline = compset.baseline_median(ctx.comp_snapshots, stay, settings)
    otb_rooms = int(ctx.otb[stay]["rooms_otb"])
    current = float(ctx.otb[stay]["current_public_rate"])

    rows = []
    for lead in LEAD_POINTS:
        as_of = stay - timedelta(days=lead)
        p = pace_mod.evaluate(settings, stay, as_of, otb_rooms)
        rec = recommend(settings, stay, p, snap, baseline, current)
        rows.append(_row_from(rec, stay, f"リード {lead}日", float(lead)))
    return rows


def sweep_coef(ctx, stay: date, name: str) -> list[Row]:
    settings = ctx.settings
    coefficients = settings.property["coefficients"]
    if name not in coefficients or not isinstance(coefficients[name], (int, float)):
        available = [k for k, v in coefficients.items()
                     if isinstance(v, (int, float)) and not k.startswith("_")]
        raise SystemExit(f"係数 '{name}' がありません。指定できるのは: {', '.join(available)}")

    base_value = float(coefficients[name])
    p = ctx.paces[stay]
    snap = ctx.comp_snapshots.get(stay)
    baseline = compset.baseline_median(ctx.comp_snapshots, stay, settings)
    current = float(ctx.otb[stay]["current_public_rate"])

    rows = []
    for scale in COEF_SCALES:
        probe = copy.deepcopy(settings)
        probe.property["coefficients"][name] = base_value * scale
        rec = recommend(probe, stay, p, snap, baseline, current)
        rows.append(_row_from(rec, stay, f"{name}×{scale:g}", scale))
    return rows


def sweep_uplift(root: Path, ctx, stay: date, days: int) -> list[Row]:
    """全競合の食事uplift を一斉に倍率で振る.

    uplift は NAR 正規化に効くため、スナップショットから組み直す必要がある。
    競合設定を書き出して build_context を通し直すことで、
    ここに正規化の別実装を持たないようにする。
    """
    compset_path = root / "config" / "compset.json"
    original = json.loads(compset_path.read_text(encoding="utf-8"))

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for scale in UPLIFT_SCALES:
            probe_cfg = copy.deepcopy(original)
            for comp in probe_cfg["competitors"]:
                for key in ("dinner_uplift", "breakfast_uplift"):
                    comp[key] = float(comp.get(key, 0.0)) * scale
            probe_path = Path(tmp) / f"compset_{scale}.json"
            probe_path.write_text(json.dumps(probe_cfg, ensure_ascii=False),
                                  encoding="utf-8")

            probe_ctx = build_context(root, ctx.snapshot, days,
                                      compset_file=probe_path)
            rec = probe_ctx.recommendations.get(stay)
            if rec is None:
                continue
            rows.append(_row_from(rec, stay, f"uplift×{scale:g}", scale))
    return rows


def run_sweep(root: Path, ctx, stay: date, spec: str, days: int) -> list[Row]:
    if spec == "otb":
        return sweep_otb(ctx, stay)
    if spec == "lead":
        return sweep_lead(ctx, stay)
    if spec == "uplift":
        return sweep_uplift(root, ctx, stay, days)
    if spec.startswith("coef="):
        return sweep_coef(ctx, stay, spec.split("=", 1)[1])
    raise SystemExit(f"未知のスイープ: {spec}\n"
                     f"  otb / uplift / lead / coef=<係数名> のいずれかを指定してください。")


# ---- 出力 ---------------------------------------------------------------


def render_table(rows: list[Row], *, title: str) -> str:
    if not rows:
        return "（対象データがありません）"

    head = (f"{'振った値':<14}{'基準価格':>10}"
            + "".join(f"{'z_' + f:>9}" for f in FACTORS)
            + f"{'モデル出力':>12}{'推奨':>10}{'差':>10}  {'判定':<18}ガードレール")
    lines = ["=" * len(head), f"  {title}", "=" * len(head), "", head,
             "-" * len(head)]

    for r in rows:
        mark = "▲" if r.overridden else " "
        lines.append(
            f"{r.knob:<14}{r.p_base:>10,.0f}"
            + "".join(f"{r.z.get(f, 0.0):>9.2f}" for f in FACTORS)
            + f"{r.raw_price:>12,.0f}{r.recommended:>10,.0f}"
            + f"{r.gap_yen:>+10,.0f}{mark} {r.action:<18}"
            + ("; ".join(r.notes) if r.notes else "—")
        )

    lines.append("-" * len(head))
    lines.append(summarize(rows))
    return "\n".join(lines)


def summarize(rows: list[Row]) -> str:
    overridden = [r for r in rows if r.overridden]
    priced = [r.raw_price for r in rows if r.raw_price > 0]
    out = [f"  {len(overridden)}/{len(rows)} 行でガードレールがモデル出力を上書き"]
    if overridden:
        reasons: dict[str, int] = {}
        for r in overridden:
            for n in r.notes:
                reasons[n] = reasons.get(n, 0) + 1
        for note, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            out.append(f"      {count:>3}件  {note}")
    if priced:
        lo, hi = min(priced), max(priced)
        span = (hi / lo) if lo > 0 else 0.0
        out.append(f"  モデル出力の振れ幅: {lo:,.0f} 〜 {hi:,.0f} 円"
                   f"（{span:.2f}倍）")
    return "\n".join(out)


def render_multi_day(per_day: dict[date, list[Row]], *, title: str) -> str:
    """複数日の簡易モード。1日1行に畳む."""
    head = (f"{'宿泊日':<12}{'モデル最小':>12}{'最大':>12}{'倍率':>8}"
            f"{'上書き':>10}{'推奨最小':>12}{'最大':>12}")
    lines = ["=" * len(head), f"  {title}", "=" * len(head), "", head,
             "-" * len(head)]

    total_rows = total_over = 0
    worst: tuple[float, date] | None = None
    for stay, rows in sorted(per_day.items()):
        priced = [r.raw_price for r in rows if r.raw_price > 0]
        if not priced:
            continue
        lo, hi = min(priced), max(priced)
        span = (hi / lo) if lo > 0 else 0.0
        over = sum(1 for r in rows if r.overridden)
        total_rows += len(rows)
        total_over += over
        if worst is None or span > worst[0]:
            worst = (span, stay)
        recs = [r.recommended for r in rows]
        lines.append(
            f"{stay.isoformat():<12}{lo:>12,.0f}{hi:>12,.0f}{span:>8.2f}"
            f"{over:>7}/{len(rows):<3}{min(recs):>12,.0f}{max(recs):>12,.0f}"
        )

    lines.append("-" * len(head))
    lines.append(f"  {total_over}/{total_rows} 行でガードレールがモデル出力を上書き")
    if worst:
        lines.append(f"  最も感度が高い日: {worst[1]}（{worst[0]:.2f}倍）")
    return "\n".join(lines)


def write_csv(rows: list[Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["宿泊日", "振った値", "数値", "基準価格"]
                   + [f"z_{f}" for f in FACTORS]
                   + ["モデル出力", "推奨価格", "現行", "差", "ガードレール上書き",
                      "判定", "ガードレール内容"])
        for r in rows:
            w.writerow([r.stay_date.isoformat(), r.knob, r.knob_value,
                        round(r.p_base)]
                       + [round(r.z.get(f, 0.0), 4) for f in FACTORS]
                       + [round(r.raw_price), round(r.recommended),
                          round(r.current_rate), round(r.gap_yen),
                          1 if r.overridden else 0, r.action,
                          "; ".join(r.notes)])


# ---- エントリポイント ---------------------------------------------------


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="係数設計の感度分析（読み取り専用の診断ツール）")
    parser.add_argument("--sweep", required=True,
                        help="otb / uplift / lead / coef=<係数名>")
    parser.add_argument("--date", help="対象の宿泊日 YYYY-MM-DD")
    parser.add_argument("--days", type=int,
                        help="基準日から N 日を一括評価しサマリのみ出す")
    parser.add_argument("--as-of", dest="as_of", help="基準日 YYYY-MM-DD")
    parser.add_argument("--horizon", type=int, default=180,
                        help="コンテキストを組む日数（既定180）")
    parser.add_argument("--csv", help="CSV出力先")
    args = parser.parse_args()

    if not args.date and not args.days:
        parser.error("--date か --days のどちらかを指定してください。")

    as_of = parse_date(args.as_of) if args.as_of else None
    ctx = build_context(root, as_of, args.horizon)
    if not ctx.recommendations:
        print("推奨がありません。先に ./scripts/run_pipeline.sh を実行してください。",
              file=sys.stderr)
        return 1

    print(f"基準日 {ctx.snapshot.isoformat()} ／ スイープ {args.sweep}")

    if args.date:
        stay = parse_date(args.date)
        if stay not in ctx.recommendations:
            available = sorted(ctx.recommendations)
            print(f"{stay} は対象期間外です。"
                  f"収集済みは {available[0]} 〜 {available[-1]}。", file=sys.stderr)
            return 1
        rows = run_sweep(root, ctx, stay, args.sweep, args.horizon)
        print()
        print(render_table(rows, title=f"感度分析 {stay.isoformat()} — {args.sweep}"))
    else:
        targets = sorted(ctx.recommendations)[: args.days]
        per_day = {stay: run_sweep(root, ctx, stay, args.sweep, args.horizon)
                   for stay in targets}
        rows = [r for rs in per_day.values() for r in rs]
        print()
        print(render_multi_day(per_day,
                               title=f"感度分析 {len(targets)}日 — {args.sweep}"))

    if args.csv:
        out = (root / args.csv).resolve() if not Path(args.csv).is_absolute() \
            else Path(args.csv)
        write_csv(rows, out)
        print(f"\n出力: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
