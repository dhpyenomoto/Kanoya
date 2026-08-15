"""近隣宿泊施設の発見とコンペティティブセット候補の生成.

    # APIキー不要（フィクスチャ再生）
    python3 scripts/discover_compset.py --source fixture

    # 実接続
    export GOOGLE_PLACES_API_KEY='...'
    python3 scripts/discover_compset.py --source places --radius 2500 --as-of today

    # 候補を確定して compset.json へ昇格（四半期レビューで人が実行する）
    python3 scripts/discover_compset.py --source fixture --write

生成物はそのまま本番投入しない。食事形態・uplift・客室数は
config/compset_overrides.json で人が確定させたうえで昇格させる。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import request as request_mod  # noqa: E402
from kanoya_rm.discovery import (  # noqa: E402
    assign_tiers, diff_against_existing, score_candidates, to_compset_config,
)
from kanoya_rm.sources.fixture import FixturePlacesSource  # noqa: E402
from kanoya_rm.sources.http import HttpClient, RateLimiter, SourceError  # noqa: E402
from kanoya_rm.sources.places import PlacesSource  # noqa: E402

TIER_ORDER = {"PRIMARY": 0, "SECONDARY": 1, "ASPIRATIONAL": 2, "EXCLUDED": 3}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="コンペセット候補の機械的発見")
    parser.add_argument("--source", choices=["places", "fixture"], default="fixture")
    parser.add_argument("--radius", type=int, default=None, help="探索半径（m）")
    request_mod.add_arguments(parser, include_window=False)
    parser.add_argument("--write", action="store_true",
                        help="config/compset.json へ昇格（四半期レビュー承認後に実行）")
    parser.add_argument("--out", default="config/compset.generated.json")
    args = parser.parse_args()

    config = json.loads((root / "config" / "sources.json").read_text(encoding="utf-8"))
    overrides = json.loads(
        (root / "config" / "compset_overrides.json").read_text(encoding="utf-8")
    ).get("overrides", {})

    prop = config["property"]
    radius = args.radius or int(config["discovery"]["radius_m"])
    survey_request = request_mod.from_args(args, root)
    run_date = survey_request.as_of

    # ---- ソース選択 ----
    if args.source == "fixture":
        source = FixturePlacesSource(root / "data" / "fixtures")
        source_label = "フィクスチャ再生（擬似データ）"
    else:
        client = HttpClient(
            source="google_places",
            raw_dir=root / "data" / "raw",
            rate_limiter=RateLimiter(per_second=float(config["rate_limits"]["places_per_second"])),
        )
        source = PlacesSource(client)
        source_label = "Google Places API (New)"

    # ---- 起点座標の解決（ハードコードより実測を優先） ----
    origin = (float(prop["latitude"]), float(prop["longitude"]))
    origin_note = "sources.json のフォールバック座標"
    try:
        geo = source.geocode(prop["geocode_query"], run_date)
        if geo and geo.latitude and geo.longitude:
            origin = (geo.latitude, geo.longitude)
            origin_note = f"{source_label} のジオコーディング結果"
    except (SourceError, AttributeError, FileNotFoundError) as exc:
        origin_note += f"（ジオコーディング不可: {type(exc).__name__}）"

    # ---- 近隣探索 ----
    try:
        places = source.nearby_lodging(origin[0], origin[1], radius, run_date)
    except (SourceError, FileNotFoundError) as exc:
        print(f"探索に失敗しました: {exc}", file=sys.stderr)
        return 1

    # 人が確定済みの客室数はスコアリング（規模の近さ）にも効かせる
    rooms_override = {
        name: int(ov["rooms"]) for name, ov in overrides.items() if ov.get("rooms")
    }
    candidates = assign_tiers(
        score_candidates(places, origin, config, rooms_override), config
    )

    print("=" * 88)
    print(f"  コンペティティブセット候補 — {prop['name']}")
    print("=" * 88)
    print(request_mod.render_input_panel(survey_request))
    print()
    print(f"データ源   : {source_label}")
    print(f"起点座標   : {origin[0]:.5f}, {origin[1]:.5f}（{origin_note}）")
    print(f"探索半径   : {radius:,} m ／ 発見 {len(places)} 件 → フィルタ後 {len(candidates)} 件")

    # 1リクエスト20件上限をタイル分割で回避するため、リクエスト数は半径の二乗で増える。
    # 四半期に1回とはいえ、実行前に費用を見せる。
    tiles = PlacesSource.plan_requests(radius)
    unit = float(config["budget"].get("places_nearby_cost_per_request_usd", 0.0))
    print(f"探索リクエスト: {tiles} 回（タイル分割）"
          + (f" ／ 概算 ${tiles * unit:,.2f}" if unit and args.source == "places" else ""))
    print()
    print(f"  {'ティア':<13}{'施設名':<26}{'距離':>7}{'評点':>6}{'口コミ':>7}{'価格帯':>6}{'スコア':>7}  要確認")
    print("  " + "-" * 86)
    for c in sorted(candidates, key=lambda x: (TIER_ORDER[x.tier], -x.total)):
        level = c.place.price_level.replace("PRICE_LEVEL_", "")[:4] or "—"
        flags = ",".join(f.replace("_unknown", "?") for f in c.flags) or "—"
        print(f"  {c.tier:<13}{c.name[:24]:<26}{c.distance_km:>6.1f}km"
              f"{(c.place.rating or 0):>6.1f}{c.place.review_count:>7,}{level:>6}"
              f"{c.total:>7.3f}  {flags}")

    excluded_by_filter = len(places) - len(candidates)
    if excluded_by_filter > 0:
        print(f"\n  ※ {excluded_by_filter} 件は業態・口コミ数フィルタで除外（設定: sources.json > discovery）")

    # ---- 生成 ----
    generated = to_compset_config(candidates, origin, {
        "source": source_label,
        "generated_on": run_date.isoformat(),
        "radius_m": radius,
        "places_found": len(places),
        "origin_note": origin_note,
    }, overrides=overrides)

    out_path = root / args.out
    out_path.write_text(json.dumps(generated, ensure_ascii=False, indent=2), encoding="utf-8")

    diff = diff_against_existing(generated, root / "config" / "compset.json")
    print(f"\n出力: {out_path}")
    print(f"  採用 {len(generated['competitors'])} 件"
          f"（PRIMARY {sum(1 for c in generated['competitors'] if c['tier'] == 'PRIMARY')} / "
          f"SECONDARY {sum(1 for c in generated['competitors'] if c['tier'] == 'SECONDARY')} / "
          f"ASPIRATIONAL {sum(1 for c in generated['competitors'] if c['tier'] == 'ASPIRATIONAL')}）")

    if diff["added"]:
        print(f"  ＋新規候補: {', '.join(diff['added'][:8])}"
              + (" ほか" if len(diff["added"]) > 8 else ""))
    if diff["removed"]:
        print(f"  −消滅/圏外: {', '.join(diff['removed'][:8])}")

    unconfirmed = [c["name"] for c in generated["competitors"]
                   if any("unconfirmed" in f or "unset" in f for f in c["needs_review"])]
    if unconfirmed:
        print(f"\n  ★ 食事条件が未確定の施設 {len(unconfirmed)} 件: {', '.join(unconfirmed[:6])}"
              + (" ほか" if len(unconfirmed) > 6 else ""))
        print("     config/compset_overrides.json で確定し confirmed:true にしてください。")
        print("     未確定のまま本番判断すると NAR 正規化が体系的に歪みます。")

    if args.write:
        target = root / "config" / "compset.json"
        target.write_text(json.dumps(generated, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n▶ {target} へ昇格しました。")
    else:
        print("\n（--write で config/compset.json へ昇格。四半期レビューでの承認後に実行してください）")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except request_mod.RequestError as exc:
        # 入力の矛盾はスタックトレースではなく、直せる指示として見せる
        print(f"\n入力エラー: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
