"""コマンドラインインターフェース。

    kanoya poll     … 今日のクチコミ件数を観測してストアに追記する（日次 cron 用）
    kanoya report   … ストアからダッシュボード HTML を生成する
    kanoya demo     … 合成データを作り、そのままレポートまで通す
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import demo as demo_module
from .config import Config, ConfigError, load_config
from .pipeline import build_report
from .places import PlacesClient, PlacesError, poll as poll_places
from .store import SnapshotStore

DEFAULT_CONFIG = "config.json"
DEFAULT_STORE = "data/snapshots.jsonl"
DEFAULT_OUTPUT = "dist/index.html"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 2

    handlers = {"poll": _cmd_poll, "report": _cmd_report, "demo": _cmd_demo}
    return handlers[args.command](args, config)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kanoya",
        description="鹿のや レベニュー・インテリジェンス",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="設定ファイル")
    parser.add_argument("--store", default=DEFAULT_STORE, help="スナップショット JSONL")
    sub = parser.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="クチコミ件数を観測して追記する")
    poll.add_argument("--on", default=None, help="観測日（既定は今日）")

    report = sub.add_parser("report", help="ダッシュボードを生成する")
    report.add_argument("--out", default=DEFAULT_OUTPUT, help="出力先 HTML")
    report.add_argument("--as-of", default=None, help="基準日（既定は今日）")

    demo = sub.add_parser("demo", help="合成データを作ってレポートまで通す")
    demo.add_argument("--out", default=DEFAULT_OUTPUT, help="出力先 HTML")
    demo.add_argument("--as-of", default=None, help="基準日（既定は今日）")
    demo.add_argument("--days", type=int, default=280, help="生成する観測日数")
    demo.add_argument("--seed", type=int, default=20260815, help="乱数シード")

    return parser


def _cmd_poll(args: argparse.Namespace, config: Config) -> int:
    on = _parse_date(args.on)

    try:
        client = PlacesClient()
    except PlacesError as exc:
        print(f"観測できない: {exc}", file=sys.stderr)
        return 1

    snapshots, errors = poll_places(client, config.properties, on)
    SnapshotStore(args.store).append(snapshots)

    print(f"{on.isoformat()}: {len(snapshots)}/{len(config.properties)} 施設を観測した。")
    for error in errors:
        print(f"  失敗: {error}", file=sys.stderr)

    # 全滅したときだけ異常終了にする。一部欠測は後段が按分で吸収する。
    return 1 if snapshots == [] else 0


def _cmd_report(args: argparse.Namespace, config: Config) -> int:
    snapshots = SnapshotStore(args.store).load_by_property()
    if not snapshots:
        print(
            f"スナップショットがない: {args.store}\n"
            "kanoya poll を毎日走らせて観測を積むか、kanoya demo を試すこと。",
            file=sys.stderr,
        )
        return 1

    report = build_report(config, snapshots, _parse_date(args.as_of))
    _write_html(Path(args.out), report.to_html())

    print(f"生成した: {args.out}")
    _print_summary(report)
    return 0


def _cmd_demo(args: argparse.Namespace, config: Config) -> int:
    as_of = _parse_date(args.as_of)
    data = demo_module.generate(config, as_of, days=args.days, seed=args.seed)
    demo_module.write(data, args.store, config.review_rate.pms_actuals_path)
    print(
        f"合成データを書き出した: {args.store} / {config.review_rate.pms_actuals_path} "
        f"({args.days}日ぶん)"
    )

    report = build_report(config, SnapshotStore(args.store).load_by_property(), as_of)
    _write_html(Path(args.out), report.to_html())
    print(f"生成した: {args.out}")

    _print_summary(report)
    _print_recovery(report, data)
    return 0


def _write_html(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def _print_summary(report) -> None:
    market = report.market
    own = market.own
    median = market.competitor_median_occupancy_pct

    print(f"  判断: {report.verdict.action} — {report.verdict.headline}")
    print(
        f"  投稿率: {report.review_rate.rate * 100:.1f}% "
        f"({'実測' if report.review_rate.is_measured else '固定値'})"
    )
    if report.backtest.n:
        print(
            f"  実績との平均誤差: {report.backtest.mean_abs_error_pt:.1f}pt "
            f"(n={report.backtest.n}, {report.backtest.usability})"
        )
    if own.occupancy_pct is not None:
        median_text = f"{median:.0f}%" if median is not None else "—"
        print(f"  自社 {own.occupancy_pct:.0f}% / 競合中央値 {median_text}")

    for warning in report.warnings:
        print(f"  警告: {warning}", file=sys.stderr)


def _print_recovery(report, data: demo_module.DemoData) -> None:
    """合成データの真値をどれだけ復元できたかを表示する。

    比較は推定と同じ滞在日の窓で行う。窓がずれていると、季節性やトレンドの差を
    推定誤差と取り違える。
    """
    market = report.market
    print(
        f"  真値との比較（滞在日 {market.window_start} 〜 {market.window_end}）:"
    )
    errors = []
    for estimate in market.ranked:
        true_occupancy = data.true_occupancy(
            estimate.prop, market.window_start, market.window_end
        )
        if true_occupancy is None or estimate.occupancy_pct is None:
            continue
        error = estimate.occupancy_pct - true_occupancy * 100
        errors.append(abs(error))
        print(
            f"    {estimate.prop.name:<28} 推定 {estimate.occupancy_pct:5.1f}%  "
            f"真値 {true_occupancy * 100:5.1f}%  差 {error:+5.1f}pt"
        )
    if errors:
        print(f"    平均絶対誤差: {sum(errors) / len(errors):.1f}pt")


def _parse_date(value: str | None) -> date:
    if value is None:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"日付の書式が不正（YYYY-MM-DD で指定すること）: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
