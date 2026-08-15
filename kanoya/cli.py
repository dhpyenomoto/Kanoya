"""コマンドライン。

    python -m kanoya discover            エリアの母集団を nearby search で台帳化
    python -m kanoya collect             当日のスナップショットを1件追記（日次cron）
    python -m kanoya report              推定＋HTML生成
    python -m kanoya check               判断とシグナルを標準出力に（cron監視用）
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import config as config_mod
from . import signals as signals_mod
from .estimate import build_report
from .render import render
from .series import Series
from .store import (
    Snapshot,
    SnapshotStore,
    append_snapshots,
    load_area_places,
    load_pms,
    save_area_places,
)


def _area_place_ids(cfg: config_mod.Config) -> list[str]:
    places = load_area_places(cfg.path("area_places"))
    ids = [p["place_id"] for p in places]
    for prop in cfg.tracked:
        if prop.place_id not in ids:
            ids.append(prop.place_id)
    return ids


def _load(cfg_path: str):
    cfg = config_mod.load(cfg_path)
    store = SnapshotStore.load(cfg.path("snapshots"))
    pms = load_pms(cfg.path("pms"))
    return cfg, store, pms


def _resolve_asof(cfg, store, arg: str | None) -> dt.date:
    if arg:
        return dt.date.fromisoformat(arg)
    return store.latest_date() or dt.date.today()


def _build(cfg, store, pms, asof):
    report = build_report(cfg, store, pms, _area_place_ids(cfg), asof)
    verdict = signals_mod.decide(cfg, report)

    area: Series = {}
    for pid in _area_place_ids(cfg):
        from .estimate import raw_stay_series

        for d, v in raw_stay_series(store, pid, cfg.model.review_lag_days).items():
            area[d] = area.get(d, 0.0) + v
    from .estimate import raw_stay_series

    own = raw_stay_series(store, cfg.own.place_id, cfg.model.review_lag_days)
    sigs = signals_mod.collect(cfg, report, area, own)
    return report, verdict, sigs


def cmd_discover(args) -> int:
    from .places import PlacesClient

    cfg = config_mod.load(args.config)
    client = PlacesClient(args.api_key)
    found = client.nearby(
        cfg.area.latitude, cfg.area.longitude, cfg.area.radius_m, cfg.area.included_types
    )
    places = [
        {"place_id": p.place_id, "name": p.name, "primary_type": p.primary_type}
        for p in found
    ]
    save_area_places(cfg.path("area_places"), places)
    print(f"エリア母集団 {len(places)} 件を {cfg.paths['area_places']} に保存しました。")
    return 0


def cmd_collect(args) -> int:
    from .places import PlacesClient

    cfg = config_mod.load(args.config)
    client = PlacesClient(args.api_key)
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    snaps: list[Snapshot] = []
    for place_id in _area_place_ids(cfg):
        info = client.details(place_id)
        snaps.append(
            Snapshot(
                place_id=place_id,
                date=today,
                user_rating_count=info.user_rating_count,
                rating=info.rating,
            )
        )
    append_snapshots(cfg.path("snapshots"), snaps)
    print(f"{today} のスナップショット {len(snaps)} 件を追記しました。")
    return 0


def cmd_report(args) -> int:
    cfg, store, pms = _load(args.config)
    asof = _resolve_asof(cfg, store, args.asof)
    report, verdict, sigs = _build(cfg, store, pms, asof)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(cfg, report, verdict, sigs), encoding="utf-8")
    print(f"{out} を生成しました（{asof} 時点 / 判断: {verdict.level}）。")
    return 0


def cmd_check(args) -> int:
    cfg, store, pms = _load(args.config)
    asof = _resolve_asof(cfg, store, args.asof)
    report, verdict, sigs = _build(cfg, store, pms, asof)

    print(f"[{asof}] {verdict.level}: {verdict.headline}")
    print(f"  自社 {report.own.occupancy * 100:.0f}% / 競合中央値 {report.comp_median_occupancy * 100:.0f}%"
          f" / 投稿率 {report.calibration.rate * 100:.1f}%")
    for s in sigs:
        print(f"  - [{s.level}] {s.title}")
    return 1 if any(s.level == "要対応" for s in sigs) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kanoya", description="鹿のや レベニュー・インテリジェンス")
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="エリアの母集団を台帳化する")
    p.add_argument("--api-key")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("collect", help="当日のスナップショットを追記する")
    p.add_argument("--api-key")
    p.add_argument("--date", help="取得日を上書き（YYYY-MM-DD）")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("report", help="HTMLダッシュボードを生成する")
    p.add_argument("--out", default="dist/index.html")
    p.add_argument("--asof", help="基準日（既定はスナップショット最終日）")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("check", help="判断とシグナルを標準出力に書く")
    p.add_argument("--asof")
    p.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
