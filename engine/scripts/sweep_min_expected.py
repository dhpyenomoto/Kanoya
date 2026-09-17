"""min_expected_rooms を振って、内部需要シグナルの効き方を測る.

    python3 scripts/sweep_min_expected.py \
        --otb ../../kanoya-data/otb.csv \
        --reservations ../../kanoya-data/reservations.csv

**このスクリプトは測定専用。設定も価格も変更しない。**
振るのはメモリ上の複製だけで、config/ は読むだけである。

**測りたいこと**

期待室数が min_expected_rooms に届かないリード帯では、内部需要シグナルを
無効化している（z_demand = 0）。実測の最終稼働へ直した結果、SHOULDER の
期待室数は最大 0.28 × 5室 = 1.40室 しかなく、有効帯はリード0〜2日に縮んだ。

ここで確定させたいのは「閾値を下げると精度が上がるのか、ノイズが増える
だけか」である。下げれば有効帯は広がるが、期待0.3室の帯で実OTBが
0室か1室かは偶然の側に寄る。1室の増減が価格を何%動かすかを帯ごとに出せば、
広がった帯が情報を運んでいるのか、丸め誤差を増幅しているのかが分かる。

**離散化の問題は閾値では消えない**

実OTBは整数しか取れない。期待0.3室の帯で観測できるのは0室か1室であり、
1室入っただけで gap が +0.7室 動く。これは「需要が強い」ではなく
「5室のうち1件入った」でしかない。閾値を下げるとこの帯を評価対象に
引き入れることになる。
"""

from __future__ import annotations

import argparse
import collections
import copy
import csv
import math
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm import pace as pace_mod                      # noqa: E402
from kanoya_rm.config import Settings, parse_date           # noqa: E402
from kanoya_rm.pricing import recommend                     # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TAX = 1.10
FORMS = ("素泊まり", "朝食のみ", "夕食のみ", "2食付き")

# 振る値。1.0 が現行の設定値。
THRESHOLDS = (0.2, 0.3, 0.5, 0.7, 1.0)
# 感度を見るリード帯。実測カーブの節目に合わせてある。
SENSITIVITY_LEADS = (0, 1, 2, 3, 5, 7, 14, 30)


def probe(settings: Settings, threshold: float) -> Settings:
    """閾値だけ差し替えた複製を返す（元の設定は触らない）."""
    copied = copy.deepcopy(settings)
    copied.property["coefficients"]["min_expected_rooms"] = threshold
    copied.property["coefficients"].pop("min_expected_rooms_ratio", None)
    return copied


def load_otb(path: Path) -> dict[tuple[date, date], dict[str, str]]:
    return {(parse_date(r["stay_date"]), parse_date(r["snapshot_date"])): r
            for r in csv.DictReader(path.open(encoding="utf-8"))}


def actual_by_stay(path: Path, settings: Settings) -> dict[date, list[float]]:
    """宿泊日ごとの実売室夜単価（部屋代・税込）."""
    out: dict[date, list[float]] = collections.defaultdict(list)
    for row in csv.DictReader(path.open(encoding="utf-8")):
        if row["product_mix"] not in FORMS:
            continue
        nights, rooms = int(row["nights"]), int(row["rooms"])
        room_nights = nights * rooms
        accommodation = float(row["accommodation"] or 0)
        if room_nights <= 0 or accommodation <= 0:
            continue
        per = accommodation / room_nights * TAX
        checkin = date(*(int(p) for p in row["checkin"].split("/")))
        for offset in range(nights):
            stay = checkin + timedelta(days=offset)
            if settings.is_open(stay):
                out[stay].extend([per] * rooms)
    return dict(out)


def active_leads(settings: Settings, day: date, limit: int = 121) -> list[int]:
    """その宿泊日で内部需要が有効なリード日数."""
    return [lead for lead in range(limit)
            if not pace_mod.evaluate(
                settings, day, day - timedelta(days=lead), 0).below_min_expected]


def _effect(settings: Settings, day: date, lead: int, otb: int) -> float:
    """内部需要項が価格を何倍にするか（1.0 を引いた寄与率）."""
    coef = settings.property["coefficients"]
    b_demand = float(coef["b_demand"])
    clip = float(coef["term_clip"])
    result = pace_mod.evaluate(settings, day, day - timedelta(days=lead), otb)
    return math.exp(max(-clip, min(clip, b_demand * result.z))) - 1.0


def first_booking_swing(settings: Settings, day: date, lead: int) -> float:
    """**予約が0室から1室になったとき**の価格寄与の変化幅.

    薄い帯で実際に起きるのはこの1歩だけである（期待0.4室の帯で観測
    できるのは0室か1室しかない）。したがってノイズの大きさを測るなら
    この値を見る。全ステップの最大値を採ると、期待室数が大きい帯でも
    tanh の傾きが最も急なところ（中ほど）を拾ってしまい、帯の薄さを
    反映しない。
    """
    return abs(_effect(settings, day, lead, 1) - _effect(settings, day, lead, 0))


def widest_swing(settings: Settings, day: date, lead: int) -> float:
    """OTBを1室ずつ動かしたときの、寄与の変化幅の最大値（参考）."""
    rooms = int(settings.property["property"]["rooms"])
    steps = [_effect(settings, day, lead, otb) for otb in range(rooms + 1)]
    return max(abs(b - a) for a, b in zip(steps, steps[1:]))


def weighted_prices(settings: Settings, otb: dict,
                    by_stay: dict[date, list[float]],
                    lead: int) -> tuple[dict[date, float], list[float], list[float]]:
    """実売室夜で重み付けした推奨部屋代と、対応する実績.

    推奨は全営業日に1つずつ出るが、実績は売れた室夜にしかない。
    重みを揃えないと、売れなかった平日を推奨側だけが数えて安く見える。
    """
    per_day: dict[date, float] = {}
    weighted: list[float] = []
    actual: list[float] = []
    for (stay, snapshot), row in sorted(otb.items()):
        if snapshot != stay - timedelta(days=lead) or not settings.is_open(stay):
            continue
        sold = by_stay.get(stay)
        if not sold:
            continue
        result = pace_mod.evaluate(settings, stay, snapshot, int(row["rooms_otb"]))
        rec = recommend(settings, stay, result, None, 0.0,
                        float(row["current_public_rate"]))
        per_day[stay] = rec.recommended_rate
        weighted.extend([rec.recommended_rate] * len(sold))
        actual.extend(sold)
    return per_day, weighted, actual


def season_reference_days(settings: Settings,
                          horizon: int = 730) -> dict[str, date]:
    """シーズンごとに代表日を1日ずつ拾う（有効帯をシーズン別に見るため）."""
    out: dict[str, date] = {}
    day = date(2026, 10, 1)
    for _ in range(horizon):
        if settings.is_open(day):
            season, _label = settings.season_of(day)
            out.setdefault(season, day)
        day += timedelta(days=1)
    order = ("PEAK", "HIGH", "SHOULDER", "LOW", "DEEP_LOW")
    return {name: out[name] for name in order if name in out}


def coverage(settings: Settings, otb: dict, lead: int) -> tuple[int, int]:
    """内部需要が有効な営業日数 / 全営業日."""
    total = active = 0
    for (stay, snapshot), row in otb.items():
        if snapshot != stay - timedelta(days=lead) or not settings.is_open(stay):
            continue
        total += 1
        result = pace_mod.evaluate(settings, stay, snapshot, int(row["rooms_otb"]))
        if not result.below_min_expected:
            active += 1
    return active, total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--otb", default="../../kanoya-data/otb.csv")
    parser.add_argument("--reservations",
                        default="../../kanoya-data/reservations.csv")
    parser.add_argument("--lead", type=int, default=7)
    parser.add_argument("--season-day", default="2027-03-15",
                        help="有効帯を測る基準の宿泊日（既定は SHOULDER の平日）")
    args = parser.parse_args()

    otb_path = (ROOT / args.otb).resolve()
    res_path = (ROOT / args.reservations).resolve()
    for path in (otb_path, res_path):
        if not path.exists():
            raise SystemExit(f"見つかりません: {path}\n"
                             "kanoya-data をクローンしてから実行してください。")

    base = Settings.load(ROOT / "config")
    shipped = float(base.property["coefficients"]["min_expected_rooms"])
    otb = load_otb(otb_path)
    by_stay = actual_by_stay(res_path, base)
    reference_day = parse_date(args.season_day)
    season, _ = base.season_of(reference_day)
    occupancy = pace_mod.expected_final_occupancy(base, season)
    rooms = int(base.property["property"]["rooms"])

    shipped_days, _shipped_prices, actual_weighted = weighted_prices(
        probe(base, shipped), otb, by_stay, args.lead)
    if not actual_weighted:
        raise SystemExit(f"リード{args.lead}日で突き合わせられる日がありません")
    actual_median = statistics.median(actual_weighted)

    print("=" * 78)
    print("  min_expected_rooms スイープ — 内部需要シグナルの効き方")
    print("=" * 78)
    print("  ※ 測定のみ。config/ は読むだけで、設定も価格も変更していない。")
    print(f"  現行の設定値          : {shipped}")
    print(f"  基準の宿泊日          : {reference_day}（{season}）")
    print(f"  そのシーズンの見込み  : {occupancy:.2f} × {rooms}室 "
          f"= 最終期待 {occupancy * rooms:.2f}室")
    print(f"  実績の中央値          : {actual_median:,.0f}円"
          f"（実売{len(actual_weighted)}室夜・部屋代・同じ日だけで突き合わせ）")
    print(f"  推奨はリード{args.lead}日時点のOTBで算出")
    print()

    print("【表1】閾値ごとの有効帯・カバレッジ・価格水準")
    print()
    print(f"  {'閾値':>5}  {'有効な最大リード':>16}  {'有効日数':>12}  "
          f"{'推奨中央値':>10}  {'実績との差':>10}  {'現行から動く日':>14}")
    for threshold in THRESHOLDS:
        settings = probe(base, threshold)
        leads = active_leads(settings, reference_day)
        max_lead = max(leads) if leads else -1
        active, total = coverage(settings, otb, args.lead)
        per_day, weighted, _ = weighted_prices(settings, otb, by_stay, args.lead)
        median = statistics.median(weighted) if weighted else 0.0
        gap = median / actual_median - 1.0 if median else 0.0
        moved = [abs(price - shipped_days[stay])
                 for stay, price in per_day.items()
                 if stay in shipped_days and price != shipped_days[stay]]
        churn = (f"{len(moved):>3}日 平均{statistics.mean(moved):>6,.0f}円"
                 if moved else f"{'—':>3}")
        mark = " ←現行" if abs(threshold - shipped) < 1e-9 else ""
        band = f"リード0〜{max_lead}日" if max_lead >= 0 else "なし"
        print(f"  {threshold:>5.1f}  {band:>16}  {active:>5} / {total:<4}日  "
              f"{median:>9,.0f}円  {gap:>+9.1%}  {churn:>14}{mark}")
    print()
    print(f"  ※ 有効な最大リードは {season} の基準日で測った値。"
          "シーズンごとに違う（表4）。")
    print(f"     有効日数は「リード{args.lead}日時点で内部需要が有効な営業日」。")
    print(f"     推奨中央値は実売{len(actual_weighted)}室夜で重み付けした値。")
    print("     『現行から動く日』は、閾値を変えたことで推奨が変わった日数と"
          "その平均変化額。")
    print()

    print("【表1b】閾値を下げて動いた日は、実績に近づいたのか離れたのか")
    print()
    print("  日ごとに『推奨 − その日の実売室夜単価の中央値』の絶対値を取り、")
    print("  現行の閾値のときと比べる。ノイズを増やしているだけなら、")
    print("  近づく日と離れる日がほぼ半々になり、誤差の平均は縮まない。")
    print()
    daily_actual = {stay: statistics.median(values)
                    for stay, values in by_stay.items()}
    print(f"  {'閾値':>5}  {'動いた日':>8}  {'近づいた':>8}  {'離れた':>8}  "
          f"{'誤差の平均変化':>14}")
    for threshold in THRESHOLDS:
        if abs(threshold - shipped) < 1e-9:
            continue
        per_day, _w, _a = weighted_prices(probe(base, threshold), otb,
                                          by_stay, args.lead)
        closer = farther = 0
        deltas: list[float] = []
        for stay, price in per_day.items():
            before = shipped_days.get(stay)
            target = daily_actual.get(stay)
            if before is None or target is None or price == before:
                continue
            change = abs(price - target) - abs(before - target)
            deltas.append(change)
            if change < 0:
                closer += 1
            else:
                farther += 1
        if not deltas:
            print(f"  {threshold:>5.1f}  {'—':>8}")
            continue
        mean = statistics.mean(deltas)
        print(f"  {threshold:>5.1f}  {len(deltas):>6}日  {closer:>6}日  "
              f"{farther:>6}日  {mean:>+12,.0f}円")
    print()
    print("  誤差の平均変化がプラスなら、閾値を下げたぶんだけ実績から離れている。")
    print()

    print("【表2】予約が0室→1室になったときの価格寄与の変化量（リード帯別）")
    print()
    header = "  ".join(f"{lead:>5}日" for lead in SENSITIVITY_LEADS)
    print(f"  {'閾値':>5}  {header}")
    for threshold in THRESHOLDS:
        settings = probe(base, threshold)
        cells = []
        for lead in SENSITIVITY_LEADS:
            result = pace_mod.evaluate(settings, reference_day,
                                       reference_day - timedelta(days=lead), 0)
            if result.below_min_expected:
                cells.append(f"{'—':>6}")
            else:
                cells.append(
                    f"{first_booking_swing(settings, reference_day, lead):>5.1%}")
        mark = " ←現行" if abs(threshold - shipped) < 1e-9 else ""
        print(f"  {threshold:>5.1f}  " + "  ".join(cells) + mark)
    print()
    print("  — は無効化されている帯（内部需要を使わない）。")
    print("  数字は予約が1件入っただけで内部需要項が動かす価格の幅。")
    print("  **リードが遠いほど大きくなる。** 期待室数が小さいほど、")
    print("  1件の有無が期待値に対して大きく見えるためで、")
    print("  需要の強さを表しているのではない。")
    print()

    print("【表3】期待室数そのもの（閾値とは独立）")
    print()
    print(f"  {'リード':>6}  {'進捗率':>8}  {'期待室数':>9}  {'実際に観測できる値':>18}")
    for lead in SENSITIVITY_LEADS:
        ratio = pace_mod.expected_ratio(base, reference_day, lead)
        expected = rooms * occupancy * ratio
        observable = "ほぼ0室 か 1室" if expected < 1.0 else f"0〜{rooms}室"
        print(f"  {lead:>4}日  {ratio:>8.3f}  {expected:>8.2f}室  {observable:>18}")
    print()
    print("  実OTBは整数しか取れない。期待室数が1室を下回る帯では、")
    print("  観測できるのが実質0室か1室しかなく、1件の有無が")
    print("  そのまま強い需要シグナルとして解釈されてしまう。")
    print("  閾値を下げてもこの離散化は消えない（表2が示すとおり）。")
    print()

    print("【表4】シーズン別の有効帯（現行の閾値 "
          f"{shipped} のとき）")
    print()
    print(f"  {'シーズン':<10} {'見込み':>7}  {'最終期待室数':>12}  {'有効な最大リード':>16}")
    for name, day in season_reference_days(base).items():
        occ = pace_mod.expected_final_occupancy(base, name)
        leads = active_leads(probe(base, shipped), day)
        max_lead = max(leads) if leads else -1
        band = f"リード0〜{max_lead}日" if max_lead >= 0 else "なし"
        print(f"  {name:<10} {occ:>7.2f}  {occ * rooms:>11.2f}室  {band:>16}")
    print()
    print("  稼働見込みが高いシーズンほど有効帯は広い。"
          "稼働が上がれば帯は自動的に広がる。")
    print()
    print("=" * 78)


if __name__ == "__main__":
    main()
