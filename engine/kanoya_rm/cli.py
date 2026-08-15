"""CLI: 90日先までの推奨価格を算出し、承認キューと意思決定ログを出力する.

    python -m kanoya_rm.cli --days 90 --explain 2026-11-21

本番運用では Cloud Scheduler → Cloud Run Job から日次で起動し、
出力を BigQuery とサイトコントローラー連携キューへ書き出す。
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from . import channels, compset, pace, report
from .config import Settings, load_csv, parse_date
from .pricing import recommend
from .restrictions import apply_mlos, detect_gap_nights


def build(root: Path, snapshot: date | None, days: int) -> tuple[Settings, dict[date, object]]:
    settings = Settings.load(root / "config")
    comp_rows = load_csv(root / "data" / "comp_rates.csv")
    otb_rows = load_csv(root / "data" / "otb.csv")

    if snapshot is None:
        snapshot = max(parse_date(r["snapshot_date"]) for r in otb_rows)

    by_stay: dict[date, list[dict[str, str]]] = defaultdict(list)
    for row in comp_rows:
        if parse_date(row["snapshot_date"]) == snapshot:
            by_stay[parse_date(row["stay_date"])].append(row)

    otb_by_stay = {
        parse_date(r["stay_date"]): r
        for r in otb_rows
        if parse_date(r["snapshot_date"]) == snapshot
    }

    snapshots = {
        stay: compset.build_snapshot(settings, stay, rows)
        for stay, rows in by_stay.items()
    }

    recs: dict[date, object] = {}
    for offset in range(days):
        stay = snapshot + timedelta(days=offset)
        otb = otb_by_stay.get(stay)
        if otb is None:
            continue
        pace_result = pace.evaluate(settings, stay, snapshot, int(otb["rooms_otb"]))
        snap = snapshots.get(stay)
        baseline = compset.baseline_median(snapshots, stay, settings)
        recs[stay] = recommend(
            settings, stay, pace_result, snap, baseline,
            float(otb["current_public_rate"]),
        )

    apply_mlos(settings, recs)          # type: ignore[arg-type]
    detect_gap_nights(settings, recs)   # type: ignore[arg-type]
    return settings, recs


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="鹿のや レベニューマネジメント推奨エンジン")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--snapshot", type=str, default=None)
    parser.add_argument("--explain", type=str, default=None, help="YYYY-MM-DD の内訳を表示")
    parser.add_argument("--out", type=str, default="../out/recommendations.csv")
    args = parser.parse_args()

    snapshot = parse_date(args.snapshot) if args.snapshot else None
    settings, recs = build(root, snapshot, args.days)

    print("=" * 78)
    print(f"  {settings.property['property']['name']} — 価格推奨（全{settings.property['property']['rooms']}室）")
    print("=" * 78)
    print(report.summary(recs))  # type: ignore[arg-type]

    queue = [r for r in recs.values() if r.action == "APPROVAL_REQUIRED"]  # type: ignore[attr-defined]
    print("\n--- 承認キュー（自動適用帯を外れた推奨のみ人が判断する） ---")
    if not queue:
        print("なし")
    for r in sorted(queue, key=lambda x: -abs(x.delta_pct))[:12]:  # type: ignore[attr-defined]
        print(f"  {r.stay_date} ({r.dow}) {r.current_rate:>8,.0f} → {r.recommended_rate:>8,.0f} 円 "
              f"({r.delta_pct:+.1%})  残{r.remaining}室  {r.event_label or r.season_label}")

    if args.explain:
        print("\n--- 価格根拠の分解 ---")
        target = parse_date(args.explain)
        if target in recs:
            print(report.explain(recs[target]))  # type: ignore[arg-type]
            print("\n  チャネル別 Net ADR（施設手取り）")
            for ce in channels.evaluate(settings, recs[target].recommended_rate):  # type: ignore[attr-defined]
                print(f"    {ce.label:<14} 掲出 {ce.gross_rate:>8,.0f} 円 "
                      f"／ コスト {ce.total_cost_rate:>5.1%} ／ 手取り {ce.net_rate:>8,.0f} 円 "
                      f"（直販比 {ce.gap_vs_direct:>+8,.0f} 円）")
        else:
            print(f"{target} は対象期間外です。")

    out_path = (root / args.out).resolve()
    report.write_recommendations_csv(recs, out_path)  # type: ignore[arg-type]
    print(f"\n出力: {out_path}")


if __name__ == "__main__":
    main()
