"""施設 × 宿泊日 の ADR マトリクスを出力する.

    # 設定ファイル（config/survey_request.json）の期間で
    python3 scripts/adr_matrix.py

    # 調べたい日程を指定
    python3 scripts/adr_matrix.py --from 2026-11-01 --to 2026-11-30
    python3 scripts/adr_matrix.py --from 2026-11              # 11月まるごと
    python3 scripts/adr_matrix.py --from 2026-11-21 --days 1  # 単日を詳しく

出力:
    端末           一覧（千円単位）
    out/adr_matrix.csv    Excel等での再加工用
    out/adr_matrix.html   ヒートマップ付きの視覚表（ブラウザで開く）
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import matrix  # noqa: E402
from kanoya_rm import request as request_mod  # noqa: E402
from kanoya_rm.cli import build_context  # noqa: E402
from kanoya_rm.config import load_csv, parse_date  # noqa: E402


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="施設×日付のADRマトリクス")
    parser.add_argument("--compset", default="config/compset.json")
    parser.add_argument("--rates", default="data/comp_rates_collected.csv")
    parser.add_argument("--otb", default="data/otb.csv")
    request_mod.add_arguments(parser)
    parser.add_argument("--cols", type=int, default=21,
                        help="端末に表示する日数（CSV/HTMLは全期間）")
    parser.add_argument("--open", action="store_true", help="HTMLをブラウザで開く")
    parser.add_argument("--anonymize", action="store_true",
                        help="競合名を「競合A/B/…」に匿名化（外部共有・見本公開用）")
    parser.add_argument("--out-dir", default="../out")
    args = parser.parse_args()

    rates_path = root / args.rates
    if not rates_path.exists():
        print(f"レートファイルがありません: {rates_path}\n"
              f"  先に scripts/collect_rates.py を実行してください。", file=sys.stderr)
        return 1

    otb_path = root / args.otb
    if not otb_path.exists():
        print(f"自社OTBデータがありません: {otb_path}\n"
              f"  本番では PMS / サイトコントローラーから日次で取り込みます。\n"
              f"  検証用には python3 scripts/make_fixtures.py で生成できます。",
              file=sys.stderr)
        return 1

    rows = load_csv(rates_path)
    fixture = any(r.get("is_fixture") == "1" for r in rows)
    collected = sorted({parse_date(r["snapshot_date"]) for r in rows})
    survey_request = request_mod.from_args(
        args, root, today=collected[-1] if collected else None
    )

    sources = json.loads((root / "config" / "sources.json").read_text(encoding="utf-8"))

    ctx = build_context(
        root, survey_request.as_of, survey_request.max_lead + 1,
        compset_file=root / args.compset,
        rates_file=rates_path,
        otb_file=otb_path,
    )

    report = matrix.build(
        ctx.settings, ctx,
        window=(survey_request.start, survey_request.end),
        fixture=fixture,
        radius_m=int(sources["discovery"]["radius_m"]),
        anonymize=args.anonymize,
    )

    if not report.dates:
        print(request_mod.render_input_panel(survey_request), file=sys.stderr)
        print("\n対象期間に該当するデータがありません。"
              "--as-of を収集済みの基準日に合わせるか、collect_rates.py で"
              "対象期間を収集してください。", file=sys.stderr)
        return 1

    print(request_mod.render_input_panel(survey_request))
    print()
    print(matrix.render_text(report, max_cols=args.cols))

    out_dir = (root / args.out_dir).resolve()
    csv_path = out_dir / "adr_matrix.csv"
    html_path = out_dir / "adr_matrix.html"
    matrix.write_csv(report, csv_path)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    # HTML は全期間のデータを埋め込み、初期表示だけ指定期間に合わせる。
    # 画面上で日程を変えられるようにするため。
    full = matrix.build_full(
        ctx.settings, ctx,
        fixture=fixture,
        radius_m=int(sources["discovery"]["radius_m"]),
        anonymize=args.anonymize,
    )
    html_path.write_text(
        matrix.render_html(full, initial=(survey_request.start, survey_request.end)),
        encoding="utf-8")
    md_path = out_dir / "adr_matrix.md"
    md_path.write_text(matrix.render_markdown(report), encoding="utf-8")

    print(f"\n出力: {csv_path}")
    print(f"      {html_path}  ← ブラウザで開くと、画面上で日程を変えられます"
          f"（収集済み {full.dates[0]} 〜 {full.dates[-1]} の範囲で切替可）")
    print(f"      {md_path}  ← GitHub上でそのまま表示されます（社内共有向け）")

    if args.open:
        webbrowser.open(html_path.as_uri())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except request_mod.RequestError as exc:
        print(f"\n入力エラー: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
