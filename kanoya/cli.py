"""コマンドライン。

  python -m kanoya collect              日次スナップショット取得（要APIキー）
  python -m kanoya report -o out.html   ダッシュボード生成
  python -m kanoya calibrate            投稿率と推定精度を確認
  python -m kanoya seed-demo            デモ用データを生成（APIキー不要）
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .config import load_config
from .store import Store


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def cmd_collect(args: argparse.Namespace) -> int:
    from .collect import collect
    from .places import PlacesClient, PlacesError

    config = load_config(args.config)
    try:
        client = PlacesClient(api_key=args.api_key)
    except PlacesError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2

    with Store(config.database_path) as store:
        result = collect(config, store, client, _parse_date(args.date))

    print(f"{result.captured_on}: 取得 {len(result.ok)}件")
    for key, reason in result.failed:
        print(f"  失敗 {key}: {reason}", file=sys.stderr)
    return 0 if result.is_complete else 1


def cmd_report(args: argparse.Namespace) -> int:
    from .render import render_html
    from .report import build_report

    config = load_config(args.config)
    with Store(config.database_path) as store:
        if store.latest_capture_date() is None:
            print(
                "エラー: スナップショットが1件もない。"
                "`python -m kanoya collect` か `seed-demo` を先に実行する。",
                file=sys.stderr,
            )
            return 2
        report = build_report(config, store, _parse_date(args.today))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(report), encoding="utf-8")

    print(f"{out} を生成（as_of {report.as_of}）")
    print(f"  判断: {report.verdict.level} — {report.verdict.headline}")
    print(
        f"  自社 {report.own.occupancy:.0f}% / 競合中央値 "
        f"{report.compset_median_occupancy:.0f}% / 投稿率 "
        f"{report.posting_rate.rate * 100:.1f}%"
    )
    for signal in report.signals:
        print(f"  [{signal.level}] {signal.title}")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from . import calibrate
    from .timeline import build_timelines

    config = load_config(args.config)
    with Store(config.database_path) as store:
        from .report import resolve_as_of

        as_of = resolve_as_of(config, store, _parse_date(args.today) or date.today())
        timelines = build_timelines(
            store, config.properties, config.estimation.review_lag_days
        )
        actuals = calibrate.load_pms_actuals(config.pms_actuals_path)
        start, end = calibrate.calibration_span(
            as_of, config.estimation.calibration_days, actuals
        )
        own = timelines[config.own.key]
        rate = calibrate.measure_posting_rate(own, actuals, start, end)
        bt = calibrate.backtest(config, own, actuals, rate, start, end)
        lag = calibrate.estimate_lag_days(own, actuals, start, end)

    print(f"突合期間      : {start} 〜 {end}（{rate.days}日）")
    print(f"販売室数(実績): {rate.sold_rooms:.0f}室")
    print(f"レビュー      : 生 {rate.raw_reviews:.1f}件 / 宿泊由来 {rate.stay_reviews:.1f}件")
    print(
        f"投稿率        : {rate.rate * 100:.2f}% "
        f"(95%CI {rate.ci_low * 100:.2f}〜{rate.ci_high * 100:.2f}%) — {rate.source}"
    )
    if bt.windows:
        print(f"平均絶対誤差  : {bt.mean_abs_error_pt:.2f}pt (n={bt.windows}) — {bt.usable_for}")
    if lag is not None:
        configured = config.estimation.review_lag_days
        mark = "" if lag == configured else f"  ← config は {configured}日"
        print(f"推定投稿遅延  : {lag}日{mark}")
    return 0


def cmd_seed_demo(args: argparse.Namespace) -> int:
    try:
        from tools.make_fixtures import build_fixtures
    except ImportError:
        print(
            "エラー: tools/make_fixtures.py が見つからない。"
            "リポジトリのルートで実行する。",
            file=sys.stderr,
        )
        return 2

    config = load_config(args.config)
    build_fixtures(config, today=_parse_date(args.today) or date(2026, 8, 15))
    print(f"デモデータを生成: {config.database_path} / {config.pms_actuals_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kanoya", description="鹿のや レベニュー・インテリジェンス"
    )
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="Google Places からスナップショットを取得")
    p.add_argument("--api-key", default=None)
    p.add_argument("--date", default=None, help="取得日を上書き（YYYY-MM-DD）")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("report", help="ダッシュボードHTMLを生成")
    p.add_argument("-o", "--out", default="dist/index.html")
    p.add_argument("--today", default=None, help="基準日を上書き（YYYY-MM-DD）")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("calibrate", help="投稿率と推定精度を表示")
    p.add_argument("--today", default=None)
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("seed-demo", help="デモ用のスナップショットとPMS実績を生成")
    p.add_argument("--today", default=None)
    p.set_defaults(func=cmd_seed_demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
