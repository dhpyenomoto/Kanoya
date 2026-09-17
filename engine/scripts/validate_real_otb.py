"""実データ（Kanoya-data）で推奨価格の分布を実績と突き合わせる.

    python3 scripts/validate_real_otb.py \
        --otb ../../kanoya-data/otb.csv \
        --reservations ../../kanoya-data/reservations.csv

**このスクリプトが確かめること**

較正が合っているかどうかは、フィクスチャでは判定できない。フィクスチャの
OTBはブッキングカーブ『から』生成されるため、進捗は定義上ほぼ想定どおりに
なり、価格も基準価格の周りに集まる。実績と比べて初めて「単位が商品と
噛み合っているか」が見える。

2026-09 に最適化単位を「1室2名1泊2食の総額」から「1室2名1泊の部屋代」へ
移したのは、まさにこの突き合わせで分かったことによる。総額基準では
2食付きの実績（中央値80,340円）とはよく合っていたが、それは実売の12%で
しかなく、主力の素泊まり（実績中央値41,300円前後）とは1.8倍ずれていた。

**読むときの重大な注意**

実データなのは **OTB（予約進捗）だけ** である。競合レート
（comp_rates_collected.csv）は今もフィクスチャであり、実勢ではない。
そのためこのスクリプトは競合項を意図的に無効化して走る（z_comp = 0）。
出力の推奨価格は「内部需要・イベント・リードタイムだけで決めた部屋代」で
あって、市場ポジションを織り込んだ値ではない。
"""

from __future__ import annotations

import argparse
import collections
import csv
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import pace as pace_mod                      # noqa: E402
from kanoya_rm.config import Settings, parse_date           # noqa: E402
from kanoya_rm.products import FORMS, LABELS, load as load_products  # noqa: E402
from kanoya_rm.pricing import recommend                     # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# 実測の食事単価（税込・1名あたり）。実績を「1室2名」へ揃えるために使う。
# property.json の rate_components と同じ値。ここで再掲するのは、
# 実績側の換算であってエンジンの設定ではないことを明示するため。
MEAL_PER_ROOM = {"room_only": 0, "breakfast": 11_000,
                 "dinner": 33_000, "two_meals": 44_000}
FORM_OF_MIX = {"素泊まり": "room_only", "朝食のみ": "breakfast",
               "夕食のみ": "dinner", "2食付き": "two_meals"}
TAX = 1.10


def _quantiles(values: list[float]) -> tuple[float, float, float]:
    ordered = sorted(values)
    if len(ordered) < 4:
        mid = statistics.median(ordered)
        return ordered[0], mid, ordered[-1]
    q1, _med, q3 = statistics.quantiles(ordered, n=4)
    return q1, statistics.median(ordered), q3


def actual_room_rates(path: Path, settings: Settings) -> dict[str, list[float]]:
    """実績の室夜単価（部屋代・税込・1室あたり）を商品形態ごとに集める.

    予約明細は1予約1行なので、宿泊日ごとの室夜へ展開する。
    accommodation 列は滞在全体の税抜部屋代なので、室夜数で割って税を乗せる。
    食事は別列にあり部屋代には入っていない（形態別の加算として別に扱う）。
    """
    out: dict[str, list[float]] = collections.defaultdict(list)
    for row in csv.DictReader(path.open(encoding="utf-8")):
        form = FORM_OF_MIX.get(row["product_mix"])
        if form is None:
            continue
        nights, rooms = int(row["nights"]), int(row["rooms"])
        room_nights = nights * rooms
        accommodation = float(row["accommodation"] or 0)
        if room_nights <= 0 or accommodation <= 0:
            continue        # 招待・従業員利用など、単価の議論に入れられない行
        per_room_night = accommodation / room_nights * TAX
        checkin = date(*(int(p) for p in row["checkin"].split("/")))
        for offset in range(nights):
            stay = checkin + timedelta(days=offset)
            if not settings.is_open(stay):
                continue    # 閉館日（連泊がまたいだ分）は実勢の母数に入れない
            out[form].extend([per_room_night] * rooms)
    return out


def final_occupancy(path: Path, settings: Settings) -> dict[date, int]:
    """宿泊日ごとの最終OTB（リード0日の観測）を返す."""
    out: dict[date, int] = {}
    for row in csv.DictReader(path.open(encoding="utf-8")):
        stay = parse_date(row["stay_date"])
        if row["snapshot_date"] != row["stay_date"] or not settings.is_open(stay):
            continue
        out[stay] = int(row["rooms_otb"])
    return out


def recommended_room_rates(otb_path: Path, settings: Settings,
                           lead: int) -> tuple[dict, dict]:
    """各宿泊日について、リード lead 日時点のOTBから推奨部屋代を出す.

    競合スナップショットは渡さない。実データに競合レートが無いためで、
    フィクスチャを混ぜると「実データで検証した」と言えなくなる。
    """
    by_key: dict[tuple[date, date], dict[str, str]] = {}
    for row in csv.DictReader(otb_path.open(encoding="utf-8")):
        by_key[(parse_date(row["stay_date"]),
                parse_date(row["snapshot_date"]))] = row

    out: dict[date, object] = {}
    paces: dict[date, object] = {}
    for stay, snapshot in sorted(by_key):
        if snapshot != stay - timedelta(days=lead):
            continue
        if not settings.is_open(stay):
            continue
        row = by_key[(stay, snapshot)]
        result = pace_mod.evaluate(settings, stay, snapshot,
                                   int(row["rooms_otb"]))
        paces[stay] = result
        out[stay] = recommend(settings, stay, result, None, 0.0,
                              float(row["current_public_rate"]))
    return out, paces


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--otb", default="../../kanoya-data/otb.csv")
    parser.add_argument("--reservations",
                        default="../../kanoya-data/reservations.csv")
    parser.add_argument("--lead", type=int, default=7,
                        help="推奨を出す時点のリード日数（既定7＝実測の中央値帯）")
    parser.add_argument("--also-leads", default="30,14,0",
                        help="参考として並べるリード（カンマ区切り）")
    args = parser.parse_args()

    otb_path = (ROOT / args.otb).resolve()
    res_path = (ROOT / args.reservations).resolve()
    for path in (otb_path, res_path):
        if not path.exists():
            raise SystemExit(f"見つかりません: {path}\n"
                             "Kanoya-data をクローンしてから実行してください。")

    settings = Settings.load(ROOT / "config")
    products = load_products(settings)

    print("=" * 78)
    print("  実データ検証 — 商品形態別の推奨価格と実績の突き合わせ")
    print("=" * 78)
    print("【データの出所】")
    print(f"  OTB（実データ）      : {otb_path}")
    print(f"  実績明細（実データ） : {res_path}")
    print("  競合レート           : **未取得**。comp_rates_collected.csv は")
    print("                         今もフィクスチャなので、この検証では使わない。")
    print("                         したがって下の推奨価格に市場ポジションは")
    print("                         入っていない（z_comp = 0 で走らせている）。")
    print()

    actual = actual_room_rates(res_path, settings)
    recs, paces = recommended_room_rates(otb_path, settings, args.lead)
    if not recs:
        raise SystemExit(f"リード{args.lead}日のOTBが1件も無い")

    print(f"【推奨（リード{args.lead}日時点のOTB・{len(recs)}営業日）と実績の比較】")
    print("  部屋代（1室2名1泊・税サ込）")
    print()
    print(f"  {'形態':<8} {'推奨 中央値':>12} {'推奨 四分位':>19} "
          f"{'実績 中央値':>12} {'実績 四分位':>19} {'室夜':>5} {'差':>7}")
    for form in FORMS:
        rec_values = [r.recommended_rate for r in recs.values()]
        rq1, rmed, rq3 = _quantiles(rec_values)
        got = actual.get(form, [])
        if got:
            aq1, amed, aq3 = _quantiles(got)
            gap = f"{rmed / amed - 1:+.0%}"
            actual_cells = (f"{amed:>12,.0f} "
                            f"{f'{aq1:,.0f}–{aq3:,.0f}':>19} {len(got):>5}")
        else:
            actual_cells = f"{'—':>12} {'—':>19} {0:>5}"
            gap = "—"
        # 部屋代は形態によらず同じ。形態差は食事加算にのみ出る。
        print(f"  {LABELS[form]:<8} {rmed:>12,.0f} "
              f"{f'{rq1:,.0f}–{rq3:,.0f}':>19} {actual_cells} {gap:>7}")
    print()
    print("  ※ 推奨部屋代は形態によらず同一（食事は固定加算のため）。")
    print("     実績側の差は、どの形態がどの日に売れたかの違いによる。")
    print()

    every = [v for values in actual.values() for v in values]
    anchor = products.anchor_room_rate
    print("【アンカーそのものの位置】")
    print(f"  設定の部屋代アンカー          {anchor:>9,.0f}円"
          f"（{products.room_per_person:,.0f}円/名 × {products.occupancy}名）")
    print(f"  実績の部屋代 中央値（全形態） {statistics.median(every):>9,.0f}円"
          f"（{len(every)}室夜）")
    print(f"  ずれ {anchor / statistics.median(every) - 1:+.0%}")
    print("  ※ 18,500円/名 は実測の部屋代ではなく、従来の2食付きアンカー")
    print("     81,000円から実測の食事単価を差し引いて逆算した値である。")
    print("     実勢の部屋代を測って置いた値ではないので、アンカー自体の")
    print("     較正は別途必要（P15の範囲外）。")
    print()

    print("【販売価格（部屋代＋食事加算）の推奨分布】")
    for form in FORMS:
        values = [r.product_prices[form] for r in recs.values()]
        q1, med, q3 = _quantiles(values)
        print(f"  {LABELS[form]:<8} 中央値 {med:>9,.0f}  "
              f"四分位 {q1:,.0f}–{q3:,.0f}  "
              f"（部屋代＋{MEAL_PER_ROOM[form]:,}円）")
    print()

    others = [int(x) for x in args.also_leads.split(",") if x.strip()]
    if others:
        print("【参考：リード別の推奨部屋代 中央値】")
        for lead in [args.lead] + others:
            got, _ = recommended_room_rates(otb_path, settings, lead)
            if not got:
                continue
            values = [r.recommended_rate for r in got.values()]
            print(f"  リード{lead:>3}日  中央値 {statistics.median(values):>9,.0f}  "
                  f"（{len(got)}営業日）")
        print()

    floor, floor_form, provisional = products.room_floor()
    hits = [r for r in recs.values() if r.floor_form]
    print("【フロアの当たり方】")
    print(f"  部屋代フロア {floor:,.0f}円"
          f"（{LABELS.get(floor_form, floor_form)}が拘束"
          f"{'・暫定値' if provisional else ''}）")
    print(f"  フロアに当たった日: {len(hits)} / {len(recs)}")
    if provisional:
        print("  ※ このフロアは暫定値（2食付きフロアから食事の売価を引いただけ）。")
        print("     食事に乗っている利益を戻していないため、真の下限より低い。")
    print()

    print("【差はどこから来ているか — 推奨部屋代の分解】")
    base = [r.base_rate for r in recs.values()]
    print(f"  基準価格（アンカー×曜日×季節）の中央値 {statistics.median(base):>9,.0f}円")
    for factor in ("demand", "comp", "event", "lead"):
        yen = [next(c.yen for c in r.contributions if c.factor == factor)
               for r in recs.values()]
        print(f"    {factor:<7} 中央値 {statistics.median(yen):>+9,.0f}円  "
              f"平均 {statistics.mean(yen):>+9,.0f}円")
    z_demand = [p.z for p in paces.values()]
    print(f"  z_demand の中央値 {statistics.median(z_demand):+.2f}"
          f"（平均 {statistics.mean(z_demand):+.2f}）")
    print()

    # 実績の稼働率と、内部需要が前提にしている目標稼働のずれ。
    # ここが噛み合っていないと、実績どおりに埋まっている日でも
    # 「大幅な遅れ」と判定され、全日が値下げ側へ押される。
    final = final_occupancy(otb_path, settings)
    if final:
        rooms = int(settings.property["property"]["rooms"])
        realized = statistics.mean(final.values()) / rooms
        print("【内部需要が前提にしている稼働と、実績の稼働】")
        print(f"  実績（営業日・最終OTB）の平均稼働率 {realized:.1%}"
              f"（{len(final)}営業日）")
        configured = " / ".join(
            f"{s} {pace_mod.expected_final_occupancy(settings, s):.0%}"
            for s in ("PEAK", "HIGH", "SHOULDER", "LOW", "DEEP_LOW"))
        print(f"  設定の最終稼働見込み  {configured}")
        print()

    # 内部需要シグナルが実際に効いている日数。無効化された日は z=0 に
    # なるが、出力上は「寄与0円」としか出ない。想定どおりだったのか、
    # そもそも見ていないのかが区別できないので、ここで数える。
    off = sum(1 for p in paces.values() if p.below_min_expected)
    print("【内部需要シグナルが効いている日数】")
    print(f"  有効 {len(paces) - off} / {len(paces)} 日"
          f"（{off}日は期待室数が min_expected_rooms 未満のため不使用）")
    if off:
        print("  ※ 無効化された日は競合・イベント・曜日季節だけで価格が決まる。")
        print("     最終稼働の見込みを実測へ下げると期待室数が小さくなり、")
        print("     min_expected_rooms に届くリード帯が狭まる。")
        print("     価格が上がったとしても、それは遅れ判定が消えたためであって、")
        print("     進捗を正しく測れるようになったためではない。")
    print()

    actions = collections.Counter(r.action for r in recs.values())
    print("【アクション区分】")
    for name, count in actions.most_common():
        print(f"  {name:<20} {count:>4}日")
    if all(float(r.current_rate) == 0 for r in recs.values()):
        print("  ※ 実データに提示価格（current_public_rate）が無いため、")
        print("     日次変動幅ガードが働かず全日が自動適用帯に入る。")
        print("     この区分は較正の良さではなく、記録の欠落を表している（P17）。")
    print()
    print("=" * 78)
    print("  結論を読むときの注意: 実データはOTBのみ。競合レートはフィクスチャ。")
    print("  ここでの推奨価格と実勢の差は、市場ポジションの差ではない。")
    print("=" * 78)


if __name__ == "__main__":
    main()
