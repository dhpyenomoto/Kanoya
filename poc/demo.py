"""
価格決定エンジンのデモ実行（合成データ）

    python3 poc/demo.py            # 90日分の推奨価格を出力
    python3 poc/demo.py --detail 7 # 先頭7日は承認カード形式で詳細表示

実データ接続前でも、ロジックの挙動と要因分解を検証できるようにしたもの。
合成データは奈良の実際の行事日程（正倉院展・紅葉・万燈籠 等）を模した配置とする。
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from dataclasses import replace
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pricing_engine import (  # noqa: E402
    CompsetSnapshot,
    OwnState,
    RateObservation,
    is_holdout_date,
    net_adr,
    recommend_rate,
)

POLICY_PATH = Path(__file__).resolve().parent / "policy.yaml"

# 合成データ用のコンプセット（実装時は Phase 0 で確定した実施設に置換する）
COMPSET = ["comp_a", "comp_b", "comp_c", "comp_d", "comp_e"]

TODAY = dt.date(2026, 9, 1)

# 奈良の主要行事を模した配置（実装時は event_master テーブルから取得）
EVENT_CALENDAR: dict[dt.date, list[str]] = {}
for d in range(24, 31):  # 正倉院展 会期
    EVENT_CALENDAR[dt.date(2026, 10, d)] = ["shosoin"]
for d in range(1, 9):
    EVENT_CALENDAR[dt.date(2026, 11, d)] = ["shosoin"]
for d in range(14, 25):  # 紅葉ピーク
    EVENT_CALENDAR.setdefault(dt.date(2026, 11, d), []).append("koyo")
for d in (17, 18):  # 鹿の角きり
    EVENT_CALENDAR.setdefault(dt.date(2026, 10, d), []).append("tsunokiri")

HOLIDAYS = frozenset(
    {
        dt.date(2026, 9, 21),
        dt.date(2026, 9, 22),
        dt.date(2026, 9, 23),
        dt.date(2026, 10, 12),
        dt.date(2026, 11, 3),
        dt.date(2026, 11, 23),
    }
)


def synth_day(day: dt.date, rng: random.Random, policy: dict) -> tuple[CompsetSnapshot, OwnState, list[str]]:
    """1宿泊日ぶんの合成データを生成する。"""
    events = EVENT_CALENDAR.get(day, [])
    lead = (day - TODAY).days
    is_weekend = day.weekday() >= 4 or day in HOLIDAYS

    # --- 市場の強さ（イベント・曜日・季節） -------------------------------
    strength = 0.45
    strength += 0.20 if is_weekend else 0.0
    strength += 0.28 * len(events)
    strength += 0.10 if day.month == 11 else 0.0
    strength -= 0.15 if lead > 70 else 0.0  # 先の日付ほどまだ埋まっていない
    strength = max(0.05, min(0.98, strength + rng.uniform(-0.08, 0.08)))

    # --- コンプセット観測 --------------------------------------------------
    obs = []
    base_price = 78000 * (1 + 0.45 * strength)
    for pid in COMPSET:
        if rng.random() < 0.06:  # 収集欠損
            continue
        sold_out = rng.random() < strength * 0.75
        price = None if sold_out else base_price * rng.uniform(0.82, 1.22)
        obs.append(
            RateObservation(
                property_id=pid,
                stay_date=day,
                price_normalized=price,
                availability="sold_out" if sold_out else "available",
                collected_hours_ago=rng.uniform(3, 20),
            )
        )
    comp = CompsetSnapshot(
        stay_date=day,
        observations=obs,
        primary_size=len(COMPSET),
        price_trend_7d=rng.uniform(-0.05, 0.12) * strength,
    )

    # --- 自社の状態 --------------------------------------------------------
    curve = min(1.0, max(0.05, 1.0 - lead / 150.0))  # 簡易ブッキングカーブ
    expected_final = 5 * min(1.0, 0.55 + 0.5 * strength)
    sold = int(round(expected_final * curve * rng.uniform(0.6, 1.35)))
    sold = max(0, min(5, sold))

    seg_default = {
        True: 118000 if day.month in (10, 11) else 88000,
        False: 88000 if day.month in (10, 11) else 68000,
    }[is_weekend]
    current = seg_default * rng.uniform(0.95, 1.05)

    own = OwnState(
        stay_date=day,
        as_of=TODAY,
        rooms_sold=sold,
        current_rate=round(current / 1000) * 1000,
        expected_final_rooms=expected_final,
        booking_curve=curve,
        market_index_z=(strength - 0.5) * 2,
    )
    return comp, own, events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--detail", type=int, default=5, help="承認カード形式で詳細表示する日数")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--converge",
        type=int,
        default=7,
        help="連続する意思決定日を何日ぶん模擬するか（日次変動上限により価格は段階的に収束する）",
    )
    args = ap.parse_args()

    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    rng = random.Random(args.seed)

    days = [TODAY + dt.timedelta(days=i + 1) for i in range(args.days)]
    synth = {d: synth_day(d, rng, policy) for d in days}

    # 隣接日の需要（MinLOS判定に使用）
    def adjacent_ratio(day: dt.date) -> float:
        vals = []
        for delta in (-1, 1):
            nb = day + dt.timedelta(days=delta)
            if nb in synth:
                c, o, _ = synth[nb]
                vals.append(c.sold_out_ratio + o.rooms_sold / 5.0)
        return min(vals) if vals else 1.0

    # 日次変動上限（±15%）があるため、初回は多くの日が上限に張り付き「要承認」になる。
    # 連続する意思決定日を模擬し、価格が収束した状態（＝定常運用時）を評価する。
    recs, first_pass_auto, orig_rate = [], 0, {}
    for day in days:
        comp, own, events = synth[day]
        if own.rooms_sold >= int(policy["property"]["total_rooms"]):
            continue  # 完売日は価格算定の対象外
        orig_rate[day] = own.current_rate
        state, rec = own, None
        iterations = max(1, min(args.converge, own.lead_days))
        for i in range(iterations):
            rec = recommend_rate(
                comp,
                state,
                policy,
                event_ids=events,
                holidays=HOLIDAYS,
                adjacent_demand_ratio=adjacent_ratio(day),
            )
            if i == 0:
                first_pass_auto += int(rec.auto_publish)
            if abs(rec.change_ratio) < 1e-9:
                break
            state = replace(state, current_rate=rec.recommended_price)
        assert rec is not None
        recs.append(rec)

    # --- 一覧出力 ---------------------------------------------------------
    print("=" * 108)
    print(f" 奈良春日 鹿のや — 推奨レート（算定基準日 {TODAY:%Y-%m-%d} / {args.days}日分・合成データ）")
    print("=" * 108)
    print(
        f"{'宿泊日':<12}{'区分':<14}{'残室':>4}{'現行':>10}{'推奨':>10}{'変動':>8}"
        f"{'純ADR(直販)':>12}{'DPI':>7}{'LOS':>5}  {'定常時':<8}{'行事/警告'}"
    )
    print("-" * 108)

    auto_n = 0
    for r in recs:
        auto_n += int(r.auto_publish)
        note = "/".join(r.evidence["events"]) if r.evidence["events"] else ""
        if r.warnings:
            note = (note + " ⚠" + r.warnings[0]) if note else "⚠" + r.warnings[0]
        if is_holdout_date(r.stay_date) and r.segment != "peak":
            note = (note + " [対照日]") if note else "[対照日]"
        print(
            f"{r.stay_date:%m/%d(%a)}  {r.segment:<14}"
            f"{r.evidence['rooms_left']:>4}"
            f"{orig_rate[r.stay_date]:>10,.0f}{r.recommended_price:>10,.0f}"
            f"{r.recommended_price / orig_rate[r.stay_date] - 1:>+8.1%}"
            f"{r.evidence['net_adr_direct']:>12,.0f}"
            f"{r.dpi:>+7.2f}{r.min_los:>5}  "
            f"{'自動' if r.auto_publish else '要承認':<8}{note}"
        )

    # --- サマリ -----------------------------------------------------------
    cur = sum(orig_rate[r.stay_date] for r in recs)
    new = sum(r.recommended_price for r in recs)
    net_cur = sum(net_adr(orig_rate[r.stay_date], "ikyu", policy) for r in recs)
    net_new = sum(
        net_adr(r.recommended_price, "direct" if r.dpi >= 0.8 else "ikyu", policy) for r in recs
    )
    print("-" * 108)
    print(
        f" 自動配信率  初回移行時 {first_pass_auto/len(recs):.0%}"
        f"  →  定常運用時 {auto_n/len(recs):.0%}（要承認 {len(recs)-auto_n} 件）"
    )
    print(f" 対象日数 {len(recs)}日（うち完売により対象外: {args.days - len(recs)}日）")
    print(
        f" 平均ADR   合成データ上の現行 {cur/len(recs):,.0f}円 → 推奨 {new/len(recs):,.0f}円 "
        f"({new/cur-1:+.1%})  ※合成データ上の比較であり効果推定ではない"
    )
    print(
        f" 平均純ADR 手数料控除後 {net_cur/len(recs):,.0f}円 → {net_new/len(recs):,.0f}円 "
        f"({net_new/net_cur-1:+.1%}／高需要日のチャネル制御を含む)"
    )
    print(f" MinLOS=2 発動: {sum(1 for r in recs if r.min_los > 1)}日")
    print(f" 高手数料チャネル絞り込み: {sum(1 for r in recs if r.channel_gating.get('expedia') != 'open')}日")

    # --- 承認カードの例 ---------------------------------------------------
    if args.detail:
        print()
        print("=" * 108)
        print(" 承認カード（先頭 %d 件） — これが「経験」の代替になる" % args.detail)
        print("=" * 108)
        for r in recs[: args.detail]:
            print(r.explain())
            print()


if __name__ == "__main__":
    main()
