#!/usr/bin/env python3
"""デモ用の合成データを作る。

本番では data/ の中身は poll コマンドが日々積み上げたものになる。実際の
Places API を叩かずにダッシュボードの挙動を確認できるよう、同じ形式の
スナップショットと PMS 実績をここで生成する。

生成は決定的（固定シード）。同じ引数なら常に同じファイルになる。

合成の因果の向きは実際と同じにしてある。まず PMS の実売室数を作り、
そこから投稿率で間引いてレビューを生成する。逆向きに作ると、較正と
バックテストが構造的に当たってしまい検証にならない。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kanoya import config as config_module  # noqa: E402
from kanoya.series import date_range  # noqa: E402
from kanoya.store import Snapshot, SnapshotStore  # noqa: E402

# 奈良の宿泊需要の季節性。(月, 日, 標準偏差[日], 振幅)
BUMPS = [
    (3, 1, 9, 0.20),    # 修二会（お水取り）
    (4, 8, 11, 0.55),   # 桜
    (5, 4, 5, 0.28),    # 大型連休
    (8, 14, 6, 0.22),   # 盆
    (10, 28, 10, 0.45),  # 正倉院展
    (11, 22, 12, 0.40),  # 紅葉
    (12, 31, 5, 0.25),  # 年末年始
]
DIPS = [
    (1, 20, 22, -0.30),  # 厳冬の閑散
    (2, 10, 18, -0.20),
    (6, 15, 18, -0.25),  # 梅雨
    (7, 10, 12, -0.12),  # 梅雨明け前
]
WEEKDAY = {0: 0.86, 1: 0.82, 2: 0.84, 3: 0.92, 4: 1.22, 5: 1.38, 6: 0.96}

# 施設ごとの生レビュー目標（外来控除前）。
# S1 = 較正窓の前半 81 日、S2 = 前期窓 90 日、S3 = 現行窓 90 日。
TARGETS = {
    "kanoya": None,  # 自社は PMS 実績から間引いて作るので別扱い
    "comp_a": (115, 121, 114),
    "comp_b": (139, 146, 149),
    "comp_c": (35, 37, 44),
    "comp_d": (640, 671, 658),
    "comp_e": (214, 224, 226),
}
OWN_TARGETS = (52, 61, 65)
OWN_SOLD = (272, 318, 339)  # 各区間の実売室数。合計 929 室。
BASE_COUNTS = {
    "kanoya": 1180,
    "comp_a": 640,
    "comp_b": 980,
    "comp_c": 210,
    "comp_d": 4300,
    "comp_e": 1520,
}
BASE_RATINGS = {"comp_a": 4.71, "comp_b": 4.64, "comp_c": 4.48, "comp_d": 4.32, "comp_e": 4.57}


def _distance_days(day: dt.date, month: int, dom: int) -> int:
    """day から (month, dom) までの最短日数。年をまたぐ場合も扱う。"""
    best = 10**9
    for year in (day.year - 1, day.year, day.year + 1):
        try:
            ref = dt.date(year, month, dom)
        except ValueError:
            continue
        best = min(best, abs((day - ref).days))
    return best


def seasonal(day: dt.date) -> float:
    value = 1.0
    for month, dom, sigma, amp in BUMPS + DIPS:
        dist = _distance_days(day, month, dom)
        value += amp * math.exp(-((dist / sigma) ** 2) / 2.0)
    return max(0.25, value)


def shape(day: dt.date, rng: random.Random, jitter: float) -> float:
    noise = math.exp(rng.gauss(0.0, jitter)) if jitter > 0 else 1.0
    return seasonal(day) * WEEKDAY[day.weekday()] * noise


def _proportional_capped(weights: list[float], total: float, cap: float | None) -> list[float]:
    """重みに比例して total を配る。cap を超える分は他に回す（水位法）。"""
    out = [0.0] * len(weights)
    active = set(i for i, w in enumerate(weights) if w > 0)
    remaining = float(total)
    while active:
        weight_sum = sum(weights[i] for i in active)
        if weight_sum <= 0:
            break
        over = [i for i in active if cap is not None and weights[i] * remaining / weight_sum > cap]
        if not over:
            for i in active:
                out[i] = weights[i] * remaining / weight_sum
            break
        for i in over:
            out[i] = float(cap)
            remaining -= cap
            active.discard(i)
    return out


def integerize(weights: list[float], total: int, cap: int | None = None) -> list[int]:
    """重みに比例した整数列を作る。合計はちょうど total、各要素は cap 以下。"""
    n = len(weights)
    if cap is not None and cap * n < total:
        raise ValueError(f"cap={cap} × {n} 日では合計 {total} を作れない")
    if total == 0:
        return [0] * n

    floats = _proportional_capped(weights, total, cap)
    ints = [int(math.floor(v)) for v in floats]
    if cap is not None:
        ints = [min(v, cap) for v in ints]

    order = sorted(range(n), key=lambda i: floats[i] - ints[i], reverse=True)
    shortfall = total - sum(ints)
    while shortfall > 0:
        progressed = False
        for i in order:
            if shortfall <= 0:
                break
            if cap is None or ints[i] < cap:
                ints[i] += 1
                shortfall -= 1
                progressed = True
        if not progressed:
            raise ValueError("cap に阻まれて合計に届かない")
    while shortfall < 0:
        for i in reversed(order):
            if shortfall >= 0:
                break
            if ints[i] > 0:
                ints[i] -= 1
                shortfall += 1
    return ints


def multinomial(weights: list[float], total: int, rng: random.Random) -> list[int]:
    """重みに比例した確率で total 件を日に割り当てる。

    integerize（最大剰余法）はレビューのような疎な系列に使えない。1 日あたりの
    期待値が 1 を下回ると floor がすべて 0 になり、配分が「重みの大きい順に上位
    total 日」へ退化する。比例配分ではなく順位配分になり、稼働の高い日にレビューが
    集中して較正が壊れる。

    多項分布での割り当てなら期待値が重みに比例し、ゆらぎも実際の計数誤差と同じ
    大きさになる。合計は total にちょうど一致する。
    """
    n = len(weights)
    if total <= 0 or n == 0:
        return [0] * n
    if sum(weights) <= 0:
        return integerize([1.0] * n, total)
    out = [0] * n
    for index in rng.choices(range(n), weights=weights, k=total):
        out[index] += 1
    return out


def segmented(
    segments: list[list[dt.date]],
    totals: tuple[int, ...],
    rng: random.Random,
    jitter: float,
    cap: int | None = None,
    weight_of=None,
    sparse: bool = False,
) -> dict[dt.date, int]:
    """区間ごとに合計を固定したまま、日次に配分する。

    sparse=True はレビューのように 1 日あたりの件数が小さい系列向け。
    """
    out: dict[dt.date, int] = {}
    for days, total in zip(segments, totals):
        weights = [weight_of(d) if weight_of else shape(d, rng, jitter) for d in days]
        values = multinomial(weights, total, rng) if sparse else integerize(weights, total, cap)
        for day, value in zip(days, values):
            out[day] = value
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="デモ用の合成データを生成する")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--as-of", default="2026-08-15", type=dt.date.fromisoformat)
    parser.add_argument("--store", default="data/snapshots.jsonl")
    parser.add_argument("--pms", default="data/pms_actuals.csv")
    parser.add_argument("--seed", type=int, default=20260815)
    args = parser.parse_args()

    cfg = config_module.load(args.config)
    est = cfg.estimation

    end = args.as_of - dt.timedelta(days=est.review_lag_days)
    cur_start = end - dt.timedelta(days=est.window_days - 1)
    prior_end = cur_start - dt.timedelta(days=1)
    prior_start = prior_end - dt.timedelta(days=est.window_days - 1)
    cal_start = end - dt.timedelta(days=est.calibration_days - 1)

    segments = [
        date_range(cal_start, prior_start - dt.timedelta(days=1)),
        date_range(prior_start, prior_end),
        date_range(cur_start, end),
    ]
    print(f"区間: {[f'{s[0]}〜{s[-1]} ({len(s)}日)' for s in segments]}")

    # 自社の PMS 実績。5 室なので 1 日あたり 0〜5 室しか売れない。
    rng = random.Random(args.seed)
    sold = segmented(segments, OWN_SOLD, rng, jitter=0.22, cap=cfg.own.rooms)

    pms_path = Path(args.pms)
    pms_path.parent.mkdir(parents=True, exist_ok=True)
    with pms_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "rooms_sold", "rooms_available"])
        for day in date_range(cal_start, end):
            writer.writerow([day.isoformat(), sold[day], cfg.own.rooms])
    print(f"PMS 実績: {pms_path} 合計 {sum(sold.values())}室 / {len(sold)}日")

    # 自社レビューは実売室数から間引いて作る（現実と同じ因果の向き）。
    # 各泊の客が確率 p でレビューを書く、という間引きモデル。重みは実売室数
    # そのもので、追加のノイズは載せない。載せると計数誤差と区別がつかなくなり、
    # バックテストが測っているものが曖昧になる。
    review_rng = random.Random(args.seed + 1)
    stay_counts: dict[str, dict[dt.date, int]] = {
        cfg.own.key: segmented(
            segments,
            OWN_TARGETS,
            review_rng,
            jitter=0.0,
            weight_of=lambda d: float(sold[d]),
            sparse=True,
        )
    }

    # 競合は実績を持たないので、季節性から直接生成する。
    for offset, prop in enumerate(cfg.competitors, start=2):
        targets = TARGETS[prop.key]
        stay_counts[prop.key] = segmented(
            segments, targets, random.Random(args.seed + offset), jitter=0.20, sparse=True
        )

    # 滞在日ベースの件数を投稿日にずらし、累計に積み上げてスナップショットにする。
    store_path = Path(args.store)
    if store_path.exists():
        store_path.unlink()
    store = SnapshotStore(store_path)

    first = cal_start + dt.timedelta(days=est.review_lag_days - 1)
    last = end + dt.timedelta(days=est.review_lag_days)
    rows: list[Snapshot] = []
    for prop in cfg.properties:
        counts = stay_counts[prop.key]
        total = BASE_COUNTS[prop.key]
        for day in date_range(first, last):
            if day > first:
                total += counts.get(day - dt.timedelta(days=est.review_lag_days), 0)
            rows.append(
                Snapshot(
                    date=day,
                    place_id=prop.place_id,
                    user_rating_count=total,
                    rating=_rating(prop.key, day, first, last),
                )
            )
    rows.sort(key=lambda s: (s.date, s.place_id))
    store.append(rows)
    print(f"スナップショット: {store_path} {len(rows)}行 ({first}〜{last})")

    for prop in cfg.properties:
        counts = stay_counts[prop.key]
        current = sum(v for d, v in counts.items() if cur_start <= d <= end)
        print(f"  {prop.key:8} 現行窓 生レビュー {current:4d} 件 → 宿泊由来 {prop.lodging_reviews(current):7.2f}")
    return 0


def _rating(key: str, day: dt.date, first: dt.date, last: dt.date) -> float:
    """自社は直近で 4.61 → 4.53 に低下させる。品質ゲートの発火を再現するため。"""
    span = max(1, (last - first).days)
    t = (day - first).days / span
    if key == "kanoya":
        return round(4.61 - 0.08 * (t**2.2), 2)
    base = BASE_RATINGS[key]
    return round(base + 0.01 * math.sin(t * 6.0), 2)


if __name__ == "__main__":
    raise SystemExit(main())
