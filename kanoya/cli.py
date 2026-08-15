"""コマンドラインインターフェース。

    python -m kanoya demo                 デモ用のスナップショット履歴を合成
    python -m kanoya snapshot             Places API から今日の値を1回取得して保存
    python -m kanoya calibrate            レビュー投稿率を実績と突合して表示
    python -m kanoya estimate             コンプセットの推定稼働を表示
    python -m kanoya render -o out.html   ダッシュボードを書き出す
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import render as render_mod
from .analysis import build_analysis
from .demo import generate as generate_demo
from .config import Config, load_config
from .places import PlacesError, fetch_place
from .store import Snapshot, SnapshotStore

DEFAULT_DB = "data/snapshots.db"
DEMO_DB = "data/snapshots.demo.db"


def _resolve_db(cfg: Config, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    live = cfg.resolve(DEFAULT_DB)
    demo = cfg.resolve(DEMO_DB)
    if not live.exists() and demo.exists():
        print(f"[info] {live} が無いのでデモ DB を使う: {demo}", file=sys.stderr)
        return demo
    return live


def _as_of(value: str | None) -> date:
    return date.fromisoformat(value) if value else date.today()


def cmd_demo(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    db, actuals = generate_demo(
        cfg, as_of=_as_of(args.as_of), db_path=args.db or cfg.resolve(DEMO_DB)
    )
    print(f"スナップショット履歴: {db}")
    print(f"自社 PMS 実績:        {actuals}")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    taken_on = _as_of(args.date)
    failures = 0
    with SnapshotStore(_resolve_db(cfg, args.db)) as store:
        for prop in cfg.properties:
            try:
                facts = fetch_place(prop.place_id, api_key=args.api_key)
            except PlacesError as exc:
                print(f"[warn] {prop.name}: {exc}", file=sys.stderr)
                failures += 1
                continue
            store.put(
                Snapshot(
                    property_key=prop.key,
                    taken_on=taken_on,
                    user_rating_count=facts.user_rating_count,
                    rating=facts.rating,
                    place_id=facts.place_id,
                )
            )
            print(f"{taken_on} {prop.key:8s} count={facts.user_rating_count} rating={facts.rating}")
    return 1 if failures == len(cfg.properties) else 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    with SnapshotStore(_resolve_db(cfg, args.db)) as store:
        an = build_analysis(cfg, store, as_of=_as_of(args.as_of))
    c = an.calibration
    print(
        json.dumps(
            {
                "rate": round(c.rate, 5),
                "ci": [round(c.ci_low, 5), round(c.ci_high, 5)] if c.ci_low else None,
                "method": c.method,
                "overlap_days": c.overlap_days,
                "rooms_sold": round(c.rooms_sold, 1),
                "reviews": round(c.reviews, 1),
                "backtest_mae_pt": round(c.mae_pt, 2) if c.mae_pt is not None else None,
                "buckets": c.buckets,
                "usability": c.usability,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    with SnapshotStore(_resolve_db(cfg, args.db)) as store:
        an = build_analysis(cfg, store, as_of=_as_of(args.as_of))

    if args.json:
        print(
            json.dumps(
                {
                    "as_of": an.as_of.isoformat(),
                    "window": [an.window_start.isoformat(), an.window_end.isoformat()],
                    "review_rate": round(an.calibration.rate, 5),
                    "verdict": {
                        "kind": an.verdict.kind,
                        "label": an.verdict.label,
                        "headline": an.verdict.headline,
                        "gap_pt": round(an.verdict.gap_pt, 2) if an.verdict.gap_pt else None,
                    },
                    "estimates": [
                        {
                            "key": e.key,
                            "name": e.name,
                            "rooms": e.rooms,
                            "is_own": e.is_own,
                            "reviews": round(e.reviews, 1),
                            "rooms_sold": round(e.rooms_sold),
                            "occupancy": round(e.occupancy, 4),
                            "delta_pct": round(e.delta_pct, 1) if e.delta_pct is not None else None,
                            "confidence": e.confidence,
                            "coverage": round(e.coverage, 3),
                        }
                        for e in an.estimates
                    ],
                    "signals": [
                        {"category": s.category, "level": s.level, "title": s.title}
                        for s in an.signals
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"{an.as_of} 時点 / 窓 {an.window_start}〜{an.window_end}")
    print(f"投稿率 {an.calibration.rate:.1%}（{an.calibration.method}）\n")
    print(f"{'施設':28s}{'室数':>5s}{'稼働':>7s}{'販売室数':>9s}{'前期比':>8s}{'信頼度':>7s}")
    for e in an.estimates:
        delta = f"{e.delta_pct:+.0f}%" if e.delta_pct is not None else "—"
        mark = "*" if e.is_own else " "
        print(
            f"{mark}{e.name[:26]:26s}{e.rooms:>5d}{e.occupancy:>7.0%}"
            f"{e.rooms_sold:>9.0f}{delta:>8s}{e.confidence:>7s}"
        )
    print(f"\n判断: {an.verdict.label} — {an.verdict.headline}")
    for s in an.signals:
        print(f"  [{s.level}] {s.category}: {s.title}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    with SnapshotStore(_resolve_db(cfg, args.db)) as store:
        an = build_analysis(cfg, store, as_of=_as_of(args.as_of))
    out = render_mod.write(an, args.out)
    print(f"書き出し: {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kanoya", description="鹿のや レベニュー・インテリジェンス")
    parser.add_argument("--config", default="config.json", help="設定ファイル（既定: config.json）")
    parser.add_argument("--db", default=None, help="スナップショット DB のパス")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help="デモ用のスナップショット履歴と PMS 実績を合成する")
    p.add_argument("--as-of", default=None)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("snapshot", help="Places API から現在値を取得して保存する")
    p.add_argument("--date", default=None, help="保存する日付（既定: 今日）")
    p.add_argument("--api-key", default=None, help="GOOGLE_PLACES_API_KEY の代わりに指定")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("calibrate", help="レビュー投稿率を実績と突合する")
    p.add_argument("--as-of", default=None)
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("estimate", help="コンプセットの推定稼働を出す")
    p.add_argument("--as-of", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("render", help="ダッシュボード HTML を書き出す")
    p.add_argument("--as-of", default=None)
    p.add_argument("-o", "--out", default="dist/index.html")
    p.set_defaults(func=cmd_render)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
