"""競合レートの収集 — 階層化スケジュールに沿って取得する.

    # 収集計画とコストの確認（ネットワークに出ない）
    python3 scripts/collect_rates.py --plan-only

    # フィクスチャ再生（APIキー不要）
    python3 scripts/collect_rates.py --source fixture --run-date 2026-08-15

    # 実接続
    export SERPAPI_API_KEY='...'
    python3 scripts/collect_rates.py --source serpapi

生レスポンスは data/raw/{source}/{取得日}/ へ不変保存される。
再実行してもキャッシュヒットとなり、二重課金は発生しない。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import collect, schedule  # noqa: E402
from kanoya_rm.config import Settings  # noqa: E402
from kanoya_rm.sources.dataforseo_hotels import DataForSeoHotelsSource  # noqa: E402
from kanoya_rm.sources.fixture import FixtureRatesSource  # noqa: E402
from kanoya_rm.sources.http import HttpClient, RateLimiter, SourceError  # noqa: E402
from kanoya_rm.sources.serpapi_hotels import SerpApiHotelsSource  # noqa: E402


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="競合レートの階層化収集")
    parser.add_argument("--source", choices=["serpapi", "dataforseo", "fixture"],
                        default="fixture")
    parser.add_argument("--compset", default="config/compset.generated.json")
    parser.add_argument("--run-date", default=None)
    parser.add_argument("--horizon", type=int, default=None)
    parser.add_argument("--out", default="data/comp_rates_collected.csv")
    parser.add_argument("--plan-only", action="store_true",
                        help="計画とコスト見積のみ表示し、収集はしない")
    parser.add_argument("--overwrite", action="store_true",
                        help="出力CSVを追記でなく新規作成する")
    args = parser.parse_args()

    config = json.loads((root / "config" / "sources.json").read_text(encoding="utf-8"))
    compset_path = root / args.compset
    if not compset_path.exists():
        print(f"コンペセットがありません: {compset_path}\n"
              f"  先に scripts/discover_compset.py を実行してください。", file=sys.stderr)
        return 1

    settings = Settings.load(root / "config", compset_file=compset_path)
    run_date = date.fromisoformat(args.run_date) if args.run_date else date.today()
    plan = schedule.build_plan(config, settings, run_date, args.horizon)
    estimate = schedule.estimate_monthly(
        config, settings, run_date, competitor_count=len(settings.competitors)
    )

    print("=" * 78)
    print(f"  競合レート収集 — 基準日 {run_date.isoformat()}")
    print("=" * 78)
    print(f"コンペセット   : {compset_path.name}（{len(settings.competitors)} 施設）")
    print(f"収集対象       : {plan.request_count} 宿泊日 / 先{plan.horizon_days}日")
    print("  帯別内訳     : " + " ／ ".join(f"{k} {v}日" for k, v in sorted(plan.by_tier().items())))
    print()
    print("── 月次見積（30日シミュレーション） " + "─" * 42)
    print(f"  ①施設別×毎日 : {estimate['requests_per_property']:>7,} req  "
          f"${estimate['cost_usd_per_property']:>8,.2f}   最も素朴な取得")
    print(f"  ②エリア一括  : {estimate['requests_area_daily']:>7,} req  "
          f"${estimate['cost_usd_area_daily']:>8,.2f}   1リクエストで全施設（①比 "
          f"{estimate['requests_per_property'] / max(1, estimate['requests_area_daily']):.0f}x）")
    print(f"  ③＋階層化    : {estimate['requests']:>7,} req  "
          f"${estimate['cost_usd']:>8,.2f}   本設計（②比 "
          f"{estimate['compression_vs_area']:.1f}x／①比 "
          f"{estimate['compression_vs_per_property']:.0f}x）")
    print(f"  月次上限     : {estimate['monthly_cap']:,} req "
          f"→ {'収まる' if estimate['within_cap'] else '★超過。tiers を見直すこと'}")

    if not estimate["within_cap"]:
        print("\n月次上限を超える計画のため停止します。"
              "sources.json > collection.tiers の頻度を下げてください。", file=sys.stderr)
        return 1

    if args.plan_only:
        print("\n（--plan-only のため収集は実行しませんでした）")
        return 0

    # ---- ソース選択 ----
    collection = config["collection"]
    if args.source == "fixture":
        source = FixtureRatesSource(root / "data" / "fixtures")
        label = "フィクスチャ再生（擬似データ）"
    elif args.source == "serpapi":
        client = HttpClient(
            source="serpapi_google_hotels",
            raw_dir=root / "data" / "raw",
            rate_limiter=RateLimiter(per_second=float(config["rate_limits"]["serpapi_per_second"])),
        )
        source = SerpApiHotelsSource(client, currency=collection["currency"])
        label = "SerpApi google_hotels"
    else:
        client = HttpClient(source="dataforseo_google_hotels", raw_dir=root / "data" / "raw")
        source = DataForSeoHotelsSource(client, currency=collection["currency"])
        label = "DataForSEO Google Hotels"

    print(f"\nデータ源       : {label}")

    def progress(i: int, total: int, stay: date) -> None:
        if i % 20 == 0 or i == total:
            print(f"  収集中 {i:>4}/{total}  ({stay.isoformat()})")

    try:
        outcome = collect.run(
            plan, source, settings.competitors,
            area_query=collection["area_query"],
            adults=int(collection["adults"]), los=int(collection["los"]),
            on_progress=progress,
        )
    except SourceError as exc:
        print(f"\n収集に失敗しました:\n{exc}", file=sys.stderr)
        return 1

    out_path = root / args.out
    collect.write_rows(outcome.rows, out_path, append=not args.overwrite)

    available = sum(1 for r in outcome.rows if r["available"] == 1)
    soldout = len(outcome.rows) - available
    print()
    print("── 収集結果 " + "─" * 65)
    print(f"  レコード     : {len(outcome.rows):,} 行（在庫あり {available:,} ／ 売止 {soldout:,}）")
    print(f"  ネットワーク : {outcome.network_calls} 回  ／ キャッシュヒット {outcome.cache_hits} 回")
    if outcome.fixture:
        print("  ██ フィクスチャ（擬似）データです。実勢価格ではありません。")

    if outcome.unmatched:
        names: dict[str, int] = {}
        for record in outcome.unmatched:
            names[record.property_name] = names.get(record.property_name, 0) + 1
        top = sorted(names.items(), key=lambda kv: -kv[1])[:8]
        print(f"\n  名寄せ不能 {len(outcome.unmatched):,} 行 / {len(names)} 施設:")
        for name, count in top:
            print(f"    {name}（{count}件）")
        print("    → コンペセット外の施設です。新規開業の兆候であれば discover_compset.py を再実行してください。")

    print(f"\n出力: {out_path}")
    print(f"次: python3 scripts/survey_report.py --compset {args.compset} --rates {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
