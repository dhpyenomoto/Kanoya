"""CLI: 90日先までの推奨価格を算出し、承認キューと意思決定ログを出力する.

    python -m kanoya_rm.cli --days 90 --explain 2026-11-21

本番運用では Cloud Scheduler → Cloud Run Job から日次で起動し、
出力を BigQuery とサイトコントローラー連携キューへ書き出す。
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import channels, compset, pace, report
from .config import Settings, load_csv, parse_date
from .pricing import recommend
from .restrictions import apply_mlos, detect_gap_nights


@dataclass
class Context:
    """価格推奨と、その根拠となった中間成果物一式."""

    settings: Settings
    snapshot: date
    recommendations: dict
    comp_snapshots: dict
    paces: dict
    otb: dict
    data_age: dict          # 宿泊日 → 競合レートの最大経過日数（鮮度）


def build_context(root: Path, snapshot: date | None, days: int, *,
                  compset_file: str | Path | None = None,
                  rates_file: str | Path | None = None,
                  otb_file: str | Path | None = None) -> Context:
    settings = Settings.load(root / "config", compset_file=compset_file)
    comp_rows = load_csv(Path(rates_file) if rates_file else root / "data" / "comp_rates_collected.csv")
    otb_rows = load_csv(Path(otb_file) if otb_file else root / "data" / "otb.csv")

    if snapshot is None:
        snapshot = max(parse_date(r["snapshot_date"]) for r in otb_rows)

    # 階層化収集では、遠い宿泊日は毎日は更新されない。
    # 「今日取得した行だけ」を見ると大半の日がデータ無しになるため、
    # (宿泊日, 競合) ごとに基準日以前で最新の観測を採用する。
    latest: dict[tuple[date, str], tuple[date, dict[str, str]]] = {}
    for row in comp_rows:
        taken = parse_date(row["snapshot_date"])
        if taken > snapshot:
            continue
        stay = parse_date(row["stay_date"])
        key = (stay, row["comp_id"])
        current = latest.get(key)
        if current is None or taken > current[0]:
            latest[key] = (taken, row)

    by_stay: dict[date, list[dict[str, str]]] = defaultdict(list)
    data_age: dict[date, int] = {}
    for (stay, _comp_id), (taken, row) in latest.items():
        by_stay[stay].append(row)
        age = (snapshot - taken).days
        data_age[stay] = max(data_age.get(stay, 0), age)

    otb_latest: dict[date, tuple[date, dict[str, str]]] = {}
    for row in otb_rows:
        taken = parse_date(row["snapshot_date"])
        if taken > snapshot:
            continue
        stay = parse_date(row["stay_date"])
        current = otb_latest.get(stay)
        if current is None or taken > current[0]:
            otb_latest[stay] = (taken, row)
    otb_by_stay = {stay: row for stay, (_taken, row) in otb_latest.items()}

    snapshots = {
        stay: compset.build_snapshot(settings, stay, rows)
        for stay, rows in by_stay.items()
    }

    recs: dict[date, object] = {}
    paces: dict[date, object] = {}
    for offset in range(days):
        stay = snapshot + timedelta(days=offset)
        otb = otb_by_stay.get(stay)
        if otb is None:
            continue
        pace_result = pace.evaluate(settings, stay, snapshot, int(otb["rooms_otb"]))
        paces[stay] = pace_result
        snap = snapshots.get(stay)
        baseline = compset.baseline_median(snapshots, stay, settings)
        recs[stay] = recommend(
            settings, stay, pace_result, snap, baseline,
            float(otb["current_public_rate"]),
        )

    apply_mlos(settings, recs)          # type: ignore[arg-type]
    detect_gap_nights(settings, recs)   # type: ignore[arg-type]
    return Context(settings=settings, snapshot=snapshot, recommendations=recs,
                   comp_snapshots=snapshots, paces=paces, otb=otb_by_stay,
                   data_age=data_age)


def build(root: Path, snapshot: date | None, days: int) -> tuple[Settings, dict[date, object]]:
    ctx = build_context(root, snapshot, days)
    return ctx.settings, ctx.recommendations


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
