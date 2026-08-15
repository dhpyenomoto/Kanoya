"""コマンドライン。

    poll       Places API を叩いて当日のスナップショットを追記する（日次 cron）
    build      ダッシュボード HTML を生成する
    calibrate  投稿率とバックテストだけを表示する
    report     判断とシグナルを端末に出す

poll は毎日必ず走らせる必要がある。1 日飛ばすとその日の増分が失われ、
series 側で日数按分される（精度が落ちる）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import config as config_module
from . import dashboard as dashboard_module
from . import render as render_module
from .places import PlacesClient, PlacesError
from .store import SnapshotStore

DEFAULT_CONFIG = "config.json"
DEFAULT_STORE = "data/snapshots.jsonl"
DEFAULT_PMS = "data/pms_actuals.csv"
DEFAULT_OUT = "dist/index.html"


def _today() -> dt.date:
    return dt.date.today()


def cmd_poll(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    on_date = args.date or _today()
    store = SnapshotStore(args.store)

    existing = {s.place_id for s in store if s.date == on_date}
    todo = [p for p in cfg.properties if p.place_id not in existing]
    if not todo:
        print(f"{on_date}: 取得済み。何もしない")
        return 0

    try:
        client = PlacesClient(args.api_key)
    except PlacesError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2

    snapshots, errors = client.snapshot_all(todo, on_date)
    store.append(snapshots)
    for snap in snapshots:
        print(f"{on_date} {snap.place_id} count={snap.user_rating_count} rating={snap.rating}")
    for message in errors:
        print(f"警告: {message}", file=sys.stderr)

    # 一部でも取れていれば蓄積は前に進むので成功扱い。全滅なら失敗。
    return 0 if snapshots else 1


def cmd_calibrate(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    dash = dashboard_module.build(cfg, args.store, args.pms, args.date or _today())
    rate, checks = dash.rate, dash.backtest

    print(f"投稿率      {rate.point * 100:.2f}%  (95%CI {rate.low * 100:.2f}〜{rate.high * 100:.2f}%)")
    print(f"突合        {rate.days}日 / {rate.rooms_sold}室 / レビュー {rate.reviews:.1f}件")
    print(f"平均誤差    {checks.mean_absolute_error_pt:.2f}pt  (n={len(checks.blocks)})")
    print(f"偏り        {checks.bias_pt:+.2f}pt")
    print(f"使える範囲  {checks.usable_range}")
    print()
    for block in checks.blocks:
        print(
            f"  {block.start}〜{block.end}  実績 {block.actual_occupancy * 100:5.1f}%  "
            f"推定 {block.estimated_occupancy * 100:5.1f}%  誤差 {block.error_pt:+5.1f}pt"
        )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    dash = dashboard_module.build(cfg, args.store, args.pms, args.date or _today())

    print(f"■ {cfg.own.name}  {dash.as_of}")
    print(f"  窓 {dash.current.start}〜{dash.current.end}（{dash.current.days}日）")
    print()
    print(f"【{dash.verdict.level}】{dash.verdict.headline}")
    print(f"  {dash.verdict.detail}")
    print()
    for est in dash.estimates:
        conf = est.confidence(
            cfg.estimation.confidence_high_rse, cfg.estimation.confidence_medium_rse
        )
        change = est.change_pct
        change_text = f"{change:+5.1f}%" if change is not None else "    —"
        mark = "*" if est.prop.is_own else " "
        print(
            f" {mark} {est.occupancy * 100:5.1f}%  {change_text}  [{conf}]  "
            f"{est.rooms_sold:6.0f}室  {est.prop.name}"
        )
    print()
    if not dash.signals:
        print("シグナルなし")
    for sig in dash.signals:
        print(f"[{sig.level}] {sig.category}: {sig.title}")
        print(f"          {sig.detail}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    dash = dashboard_module.build(cfg, args.store, args.pms, args.date or _today())
    html = render_module.render(dash)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"書き出し: {out}  ({len(html):,} bytes)")
    return 0


def _date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kanoya", description="鹿のや レベニュー・インテリジェンス"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--store", default=DEFAULT_STORE)
    parser.add_argument("--date", type=_date, help="基準日（既定: 今日）")
    sub = parser.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="Places API のスナップショットを追記")
    poll.add_argument("--api-key", default=None)
    poll.set_defaults(func=cmd_poll)

    for name, func, helptext in (
        ("build", cmd_build, "ダッシュボード HTML を生成"),
        ("calibrate", cmd_calibrate, "投稿率とバックテストを表示"),
        ("report", cmd_report, "判断とシグナルを端末に表示"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--pms", default=DEFAULT_PMS)
        if name == "build":
            sp.add_argument("--out", default=DEFAULT_OUT)
        sp.set_defaults(func=func)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (config_module.ConfigError, ValueError, FileNotFoundError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        # `| head` などで読み手が先に閉じた場合。異常ではないので黙って終わる。
        try:
            sys.stdout.close()
        finally:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
