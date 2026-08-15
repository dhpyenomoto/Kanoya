"""コマンドライン。

    kanoya snapshot   毎日 1 回。累計レビュー件数を台帳に追記する。
    kanoya report     台帳から推定してダッシュボードを出す。
    kanoya demo       合成台帳を作る（本番データが貯まるまでの検証用）。
    kanoya check      設定と台帳の健全性だけ見る。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import config as config_mod
from . import demo as demo_mod
from . import pipeline, places, pms, render, store


def _as_of(value: str | None) -> date:
    return date.fromisoformat(value) if value else date.today()


def _load(args) -> config_mod.Config:
    return config_mod.load(args.config)


def cmd_snapshot(args) -> int:
    config = _load(args)
    if demo_mod.is_demo(config) and not args.force:
        print(
            "place_id が demo- で始まっている。実 API を叩く前に config.json の "
            "place_id を実際の値に差し替えること（強行するなら --force）。",
            file=sys.stderr,
        )
        return 2

    key = places.api_key(args.api_key)
    observed_at = _as_of(args.date)
    place_ids = [prop.place_id for prop in config.properties]

    existing = store.load(config.paths.snapshots)
    already = {
        snap.place_id
        for snap in existing
        if snap.observed_at == observed_at
    }
    targets = [pid for pid in place_ids if pid not in already]
    if not targets:
        print(f"{observed_at}: 全 {len(place_ids)} 施設とも記録済み。何もしない。")
        return 0

    snapshots, errors = places.snapshot_all(targets, key=key, observed_at=observed_at)
    store.append(config.paths.snapshots, snapshots)
    print(f"{observed_at}: {len(snapshots)}/{len(targets)} 施設を記録 → {config.paths.snapshots}")
    for error in errors:
        print(f"  欠測: {error}", file=sys.stderr)
    # 欠測があっても 0 を返す。日次ジョブを 1 施設の失敗で止めない。
    return 0


def cmd_report(args) -> int:
    config = _load(args)
    as_of = _as_of(args.date)
    try:
        report = pipeline.build(config, as_of)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    output = Path(args.output) if args.output else config.paths.output
    render.write(report, output)
    print("\n".join(pipeline.summary_lines(report)))
    print(f"\n→ {output}")
    if demo_mod.is_demo(config):
        print("※ 合成データによる出力。実データではない。", file=sys.stderr)
    return 0


def cmd_demo(args) -> int:
    config = _load(args)
    as_of = _as_of(args.date)
    if not demo_mod.is_demo(config) and not args.force:
        print(
            "config の place_id が実データを指している。合成台帳で上書きすると"
            "本物の観測履歴が汚れる（強行するなら --force）。",
            file=sys.stderr,
        )
        return 2

    snapshots, actuals = demo_mod.generate(config, as_of=as_of, seed=args.seed)
    snapshot_path = Path(config.paths.snapshots)
    if snapshot_path.exists():
        snapshot_path.unlink()
    store.append(snapshot_path, snapshots)
    demo_mod.write_actuals(actuals, config.paths.pms_actuals)
    print(f"合成スナップショット {len(snapshots)} 行 → {snapshot_path}")
    print(f"合成 PMS 実績 {len(actuals)} 行 → {config.paths.pms_actuals}")
    return 0


def cmd_check(args) -> int:
    config = _load(args)
    as_of = _as_of(args.date)
    snapshots = store.load(config.paths.snapshots)
    actuals = pms.load(config.paths.pms_actuals)

    print(f"設定: {args.config}")
    print(f"自社: {config.own.label}（{config.own.rooms}室）")
    print(f"競合: {len(config.compset)}件")
    print(f"台帳: {len(snapshots)}行 / {config.paths.snapshots}")
    print(f"実績: {len(actuals)}日 / {config.paths.pms_actuals}")

    problems = 0
    for prop in config.properties:
        span = store.coverage(snapshots, prop.place_id)
        if span is None:
            print(f"  [欠] {prop.label}: 観測が 2 日ぶん未満")
            problems += 1
            continue
        first, last = span
        days = (last - first).days
        stale = (as_of - last).days
        flag = "  " if days >= config.estimation.window_days else "[短]"
        print(f"  {flag} {prop.label}: {first}〜{last}（{days}日, 最終観測 {stale}日前）")
        if days < config.estimation.window_days:
            problems += 1
        if stale > 3:
            print(f"       最終観測が {stale} 日前。日次ジョブが止まっている可能性。")
            problems += 1

    if not actuals:
        print("  [欠] PMS 実績が無い。投稿率が実測できず全推定がフォールバック。")
        problems += 1

    print(f"\n要確認 {problems} 件" if problems else "\n問題なし")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kanoya",
        description="鹿のや レベニュー・インテリジェンス（需要推定。レートは推定しない）",
    )
    parser.add_argument("--config", default="config.json", help="設定ファイル")
    parser.add_argument("--date", help="基準日 YYYY-MM-DD（既定: 今日）")
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="累計レビュー件数を台帳に追記")
    snap.add_argument("--api-key", help="既定は環境変数 GOOGLE_PLACES_API_KEY")
    snap.add_argument("--force", action="store_true", help="demo place_id でも実行")
    snap.set_defaults(func=cmd_snapshot)

    report = sub.add_parser("report", help="ダッシュボードを出力")
    report.add_argument("--output", help="出力先 HTML")
    report.set_defaults(func=cmd_report)

    demo = sub.add_parser("demo", help="合成台帳を生成（検証用）")
    demo.add_argument("--seed", type=int, default=20260815)
    demo.add_argument("--force", action="store_true", help="実 place_id でも実行")
    demo.set_defaults(func=cmd_demo)

    check = sub.add_parser("check", help="設定と台帳の健全性を確認")
    check.set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (config_mod.ConfigError, pms.ActualsError, places.PlacesError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
