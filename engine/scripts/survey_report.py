"""マーケットサーベイ / ADR判断レポート.

収集済みの競合価格・自社OTB・需要カレンダーを突き合わせ、
「この日のADRをどうすべきか」の判断材料を出す。

    python3 scripts/survey_report.py \
        --compset config/compset.generated.json \
        --rates data/comp_rates_collected.csv

価格・在庫・予約ペースの3シグナルのうち2つ以上が揃った日にだけ
強い判断（値上げ／値下げ）を出し、それ以外は維持とする。
5室規模では単一シグナルは1件の予約で簡単に裏切られるため、慎重側に倒している。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import survey  # noqa: E402
from kanoya_rm.cli import build_context  # noqa: E402
from kanoya_rm.config import load_csv, parse_date  # noqa: E402
from kanoya_rm.report import explain  # noqa: E402


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="マーケットポジション調査とADR判断")
    parser.add_argument("--compset", default="config/compset.generated.json")
    parser.add_argument("--rates", default="data/comp_rates_collected.csv")
    parser.add_argument("--otb", default="data/otb.csv")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--target-position", type=float, default=1.15,
                        help="競合NAR中央値に対する目標ポジション（倍）")
    parser.add_argument("--explain", default=None, help="YYYY-MM-DD の価格根拠を表示")
    parser.add_argument("--out", default="../out/market_survey.csv")
    args = parser.parse_args()

    rates_path = root / args.rates
    if not rates_path.exists():
        print(f"レートファイルがありません: {rates_path}\n"
              f"  先に scripts/collect_rates.py を実行してください。", file=sys.stderr)
        return 1

    fixture = any(r.get("is_fixture") == "1" for r in load_csv(rates_path))
    snapshot = parse_date(args.snapshot) if args.snapshot else None

    ctx = build_context(
        root, snapshot, args.days,
        compset_file=root / args.compset,
        rates_file=rates_path,
        otb_file=root / args.otb,
    )

    if not ctx.recommendations:
        print("対象日がありません。--snapshot が収集日と一致しているか確認してください。",
              file=sys.stderr)
        return 1

    report = survey.build(
        ctx.settings, ctx,
        target_position=args.target_position,
        fixture=fixture,
    )
    print(survey.render(report))

    if args.explain:
        target = date.fromisoformat(args.explain)
        rec = ctx.recommendations.get(target)
        print("\n── 価格根拠の分解 " + "─" * 59)
        if rec is None:
            print(f"  {target} は対象期間外です。")
        else:
            print(explain(rec))
            snap = ctx.comp_snapshots.get(target)
            if snap:
                print(f"\n  競合内訳（NAR = 1室2名2食・税サ込 換算）  サンプル{snap.sample_size}件")
                names = {c.id: c.name for c in ctx.settings.competitors.values()}
                for cid, nar, ok in sorted(snap.detail, key=lambda d: -d[1]):
                    label = f"{nar:>10,.0f} 円" if ok else "     売止    "
                    print(f"    {names.get(cid, cid)[:28]:<30}{label}")

    out_path = (root / args.out).resolve()
    survey.write_csv(report, out_path)
    print(f"\n出力: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
