"""実績からシーズン別の最終稼働を算出する.

    python3 scripts/build_expected_occupancy.py \
        --otb ../../kanoya-data/otb.csv \
        --reservations ../../kanoya-data/reservations.csv

**この値は経営目標ではない**

pace.py は expected_rooms = 客室数 × expected_final_occupancy × 進捗率 として
「あるべきOTB」を作り、実OTBとの差を内部需要シグナルにする。つまりこれは
予測変数であって、目標変数ではない。目標を入れると、実勢どおりに埋まった
日でも常に進捗不足と判定され、全日が値下げ方向へ押される。

実際にこれで事故が起きている。経営目標
（PEAK 0.95 / HIGH 0.88 / SHOULDER 0.75 / LOW 0.62 / DEEP_LOW 0.50）が
コードに直書きされており、実績（順に 49.1 / 29.1 / 27.5 / 22.0 / 10.0%）から
全シーズンで40〜59ポイント上振れしていた。z_demand の中央値は -0.73、
価格寄与は約 -20% だった。

**サンプル不足のシーズンは値を出さない**

5室規模では1室夜で稼働率が4ポイント動く。営業数日の平均はシグナルではなく
偶然である。既定では営業30日未満のシーズンは値を出さず、隣接シーズンの
流用を促す（実績では DEEP_LOW が営業4日・実売2室夜しかない）。

**OTBの網羅性について**

otb.csv は閉館日の宿泊日を機械的に除外して作られていることがある。その場合
例外営業の日がOTBに現れず、営業日数だけが増えて稼働率が下振れする。
実績では8日（PEAK 3日・HIGH 2日を含む）が欠け、PEAK が 49.1% ではなく
52.6%、HIGH が 29.1% ではなく 24.0% になった。欠けを検知したら警告し、
--reservations が渡されていればそちらで補う。
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import capacity                              # noqa: E402
from kanoya_rm.config import Settings, parse_date           # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SEASONS = ("PEAK", "HIGH", "SHOULDER", "LOW", "DEEP_LOW")

# 値を出さないシーズンに、どのシーズンを流用させるか。
# 需要の強さが隣り合う側へ倒す（弱いほうへ倒すと値下げ方向に働くため、
# 強弱の順序に沿った素直な隣接を使う）。
NEIGHBOUR = {"PEAK": "HIGH", "HIGH": "PEAK", "SHOULDER": "LOW",
             "LOW": "SHOULDER", "DEEP_LOW": "LOW"}


def final_otb(path: Path) -> dict[date, int]:
    """宿泊日ごとの最終OTB（リード0日の観測）."""
    out: dict[date, int] = {}
    for row in csv.DictReader(path.open(encoding="utf-8")):
        if row["snapshot_date"] == row["stay_date"]:
            out[parse_date(row["stay_date"])] = int(row["rooms_otb"])
    return out


def sold_from_reservations(path: Path) -> dict[date, int]:
    """予約明細を室夜へ展開する（OTBの欠けを埋めるための補助）."""
    out: collections.Counter[date] = collections.Counter()
    for row in csv.DictReader(path.open(encoding="utf-8")):
        checkin = date(*(int(p) for p in row["checkin"].split("/")))
        nights, rooms = int(row["nights"]), int(row["rooms"])
        for offset in range(nights):
            out[checkin + timedelta(days=offset)] += rooms
    return dict(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--otb", default="../../kanoya-data/otb.csv")
    parser.add_argument("--reservations", default="",
                        help="OTBに欠けがある場合の補完元（予約明細）")
    parser.add_argument("--from", dest="start", default="2026-02-06")
    parser.add_argument("--to", dest="end", default="2026-09-17")
    parser.add_argument("--min-days", type=int, default=0,
                        help="この営業日数未満のシーズンは値を出さない"
                             "（既定は property.json の _min_sample_days）")
    args = parser.parse_args()

    settings = Settings.load(ROOT / "config")
    rooms = int(settings.property["property"]["rooms"])
    configured = settings.property.get("expected_final_occupancy") or {}
    min_days = args.min_days or int(configured.get("_min_sample_days", 30))

    otb_path = (ROOT / args.otb).resolve()
    if not otb_path.exists():
        raise SystemExit(f"見つかりません: {otb_path}")
    observed = final_otb(otb_path)

    start, end = parse_date(args.start), parse_date(args.end)
    span = (end - start).days + 1
    open_days = settings.open_days(start, span)
    if not open_days:
        raise SystemExit("対象期間に営業日が1日もありません。closed_days を確認してください。")

    # OTBの網羅性。欠けたまま割ると分母だけ増えて稼働率が下振れする。
    missing = [d for d in open_days if d not in observed]
    filled: dict[date, int] = {}
    if missing:
        print(f"⚠️  OTBに営業日の欠けがあります（{len(missing)}日）: "
              f"{', '.join(d.isoformat() for d in missing[:8])}"
              f"{' 他' if len(missing) > 8 else ''}", file=sys.stderr)
        if args.reservations:
            res_path = (ROOT / args.reservations).resolve()
            if not res_path.exists():
                raise SystemExit(f"見つかりません: {res_path}")
            sold = sold_from_reservations(res_path)
            filled = {d: sold.get(d, 0) for d in missing}
            print(f"   予約明細から補完しました（{sum(filled.values())}室夜）。",
                  file=sys.stderr)
        else:
            print("   --reservations を渡すと補completeできます。"
                  "補完しない場合、欠けた日は母数から外します。", file=sys.stderr)

    sold_by: collections.Counter[str] = collections.Counter()
    days_by: collections.Counter[str] = collections.Counter()
    for day in open_days:
        rooms_otb = observed.get(day, filled.get(day))
        if rooms_otb is None:
            continue            # データの無い日は分母にも入れない
        season, _ = settings.season_of(day)
        days_by[season] += 1
        sold_by[season] += rooms_otb

    cap = capacity.measure(settings, start, span)
    total_days = sum(days_by.values())
    total_sold = sum(sold_by.values())

    print("=" * 74)
    print("  シーズン別の最終稼働（実績）")
    print("=" * 74)
    print(f"  対象期間   {start} 〜 {end}（暦日{span}日）")
    print(f"  分母       {cap.denominator()}")
    print(f"  集計対象   営業{total_days}日 × {rooms}室 = {total_days * rooms:,}室夜"
          f"（データのある日のみ）")
    print(f"  実売       {total_sold:,}室夜  全体稼働 "
          f"{total_sold / max(1, total_days * rooms):.1%}")
    print()
    print(f"  {'シーズン':<10} {'営業日':>6} {'実売':>6} {'稼働':>8}   判定")

    measured: dict[str, float] = {}
    thin: list[str] = []
    for season in SEASONS:
        days, sold = days_by[season], sold_by[season]
        if not days:
            print(f"  {season:<10} {0:>6} {0:>6} {'—':>8}   対象期間に営業日なし")
            thin.append(season)
            continue
        occ = sold / (days * rooms)
        if days < min_days:
            print(f"  {season:<10} {days:>6} {sold:>6} {occ:>7.1%}   "
                  f"サンプル不足（営業{min_days}日未満）— 値を採らない")
            thin.append(season)
        else:
            measured[season] = round(occ, 2)
            print(f"  {season:<10} {days:>6} {sold:>6} {occ:>7.1%}   採用 {occ:.2f}")

    out = collections.OrderedDict()
    out["_source"] = (
        f"実績から算出（{start}〜{end}）。{cap.denominator()}、"
        f"実売{total_sold}室夜、全体稼働 {total_sold / max(1, total_days * rooms):.1%}。"
        + "シーズン別の営業日数は "
        + " / ".join(f"{s} {days_by[s]}" for s in SEASONS) + " 日。"
    )
    for season in SEASONS:
        if season in measured:
            out[season] = measured[season]
            continue
        donor = NEIGHBOUR.get(season)
        value = measured.get(donor)
        if value is None:
            value = next((measured[s] for s in SEASONS if s in measured), None)
            donor = next((s for s in SEASONS if s in measured), "")
        if value is None:
            continue
        out[season] = value
        out[f"_{season.lower()}_note"] = (
            f"営業{days_by[season]}日・実売{sold_by[season]}室夜しかなく、"
            f"実測値は信頼できない。{donor} と同値を流用している。"
            "1年分の実績が貯まったら再算出すること。")

    if thin:
        print()
        print(f"⚠️  サンプル不足のシーズン: {', '.join(thin)}", file=sys.stderr)
        print("   5室規模では1室夜で稼働率が4ポイント動きます。"
              "少数日の平均はシグナルではなく偶然です。", file=sys.stderr)
        print("   隣接シーズンの値を流用しました。"
              "実績が貯まってから再算出してください。", file=sys.stderr)

    print()
    print("  property.json の expected_final_occupancy に貼る内容:")
    print()
    for line in json.dumps(out, ensure_ascii=False, indent=2).splitlines():
        print(f"    {line}")


if __name__ == "__main__":
    main()
