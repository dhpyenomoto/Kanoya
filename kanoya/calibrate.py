"""レビュー投稿率のキャリブレーション。

推定稼働率の全体が、この 1 つの係数に乗る。したがって「どこから来た数字か」
「実績とどれだけずれるか」を必ず添えて返す。自社 PMS 実績がなければ
config の既定値にフォールバックし、その旨を明示する。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .series import ReviewSeries


@dataclass(frozen=True)
class Actual:
    stay_date: date
    rooms_sold: float


@dataclass(frozen=True)
class Calibrated:
    rate: float
    ci_low: float | None
    ci_high: float | None
    method: str
    overlap_days: int
    rooms_sold: float
    reviews: float
    mae_pt: float | None
    buckets: int
    usability: str
    measured: bool


def wilson_interval(successes: float, trials: float, z: float = 1.96) -> tuple[float, float]:
    """比率の Wilson スコア信頼区間。件数が少ないときも区間が潰れない。"""
    if trials <= 0:
        return (0.0, 1.0)
    p = successes / trials
    denom = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return (max(0.0, center - margin), min(1.0, center + margin))


def load_actuals(path: str | Path) -> list[Actual]:
    """date,rooms_sold[,rooms_available] の CSV を読む。"""
    rows: list[Actual] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        for raw in csv.DictReader(fh):
            if not raw.get("date"):
                continue
            rows.append(
                Actual(
                    stay_date=date.fromisoformat(raw["date"].strip()),
                    rooms_sold=float(raw["rooms_sold"]),
                )
            )
    return sorted(rows, key=lambda a: a.stay_date)


def _usability(mae_pt: float | None, cfg_usable: float, cfg_relative: float) -> str:
    if mae_pt is None:
        return "方向性の議論のみ"
    if mae_pt <= cfg_usable:
        return "水準の議論に使える"
    if mae_pt <= cfg_relative:
        return "相対比較のみ"
    return "方向性の議論のみ"


def _backtest(
    buckets: list[tuple[float, float, int]],
    total_reviews: float,
    total_rooms: float,
    rooms: int,
) -> tuple[float | None, int]:
    """leave-one-out バックテスト。

    各バケットの推定には、そのバケットを除いた投稿率を使う。
    同じデータで係数を作って同じデータを当てにいかないための措置。
    """
    errors: list[float] = []
    for b_reviews, b_rooms, b_days in buckets:
        out_reviews = total_reviews - b_reviews
        out_rooms = total_rooms - b_rooms
        if out_rooms <= 0 or out_reviews <= 0 or b_days <= 0:
            continue
        loo_rate = out_reviews / out_rooms
        capacity = rooms * b_days
        if capacity <= 0:
            continue
        est_occ = (b_reviews / loo_rate) / capacity
        actual_occ = b_rooms / capacity
        errors.append(abs(est_occ - actual_occ) * 100.0)
    if not errors:
        return (None, 0)
    return (sum(errors) / len(errors), len(errors))


def calibrate(
    series: ReviewSeries,
    actuals: list[Actual],
    *,
    rooms: int,
    default_rate: float,
    bucket_days: int = 30,
    usable_mae_pt: float = 3.0,
    relative_only_mae_pt: float = 6.0,
    train_before: date | None = None,
) -> Calibrated:
    """自社の実績販売室数と自社のクチコミ系列を突合して投稿率を実測する。

    train_before を渡すと、その日より前の滞在日だけで係数を作る。今表示している
    窓を学習から外すことで、窓内の実績が投稿率ドリフトの検証用ホールドアウトになる。
    """
    usable = [
        a
        for a in actuals
        if a.stay_date in series.covered
        and a.rooms_sold > 0
        and (train_before is None or a.stay_date < train_before)
    ]

    if not usable:
        return Calibrated(
            rate=default_rate,
            ci_low=None,
            ci_high=None,
            method="config 既定値（実績突合なし）",
            overlap_days=0,
            rooms_sold=0.0,
            reviews=0.0,
            mae_pt=None,
            buckets=0,
            usability="方向性の議論のみ",
            measured=False,
        )

    total_rooms = sum(a.rooms_sold for a in usable)
    total_reviews = sum(series.daily.get(a.stay_date, 0.0) for a in usable)

    if total_reviews <= 0:
        return Calibrated(
            rate=default_rate,
            ci_low=None,
            ci_high=None,
            method="config 既定値（突合期間のクチコミが 0）",
            overlap_days=len(usable),
            rooms_sold=total_rooms,
            reviews=0.0,
            mae_pt=None,
            buckets=0,
            usability="方向性の議論のみ",
            measured=False,
        )

    rate = total_reviews / total_rooms
    lo, hi = wilson_interval(total_reviews, total_rooms)

    buckets: list[tuple[float, float, int]] = []
    start = usable[0].stay_date
    cur_reviews = cur_rooms = 0.0
    cur_days = 0
    for a in usable:
        if (a.stay_date - start).days >= bucket_days:
            buckets.append((cur_reviews, cur_rooms, cur_days))
            start = a.stay_date
            cur_reviews = cur_rooms = 0.0
            cur_days = 0
        cur_reviews += series.daily.get(a.stay_date, 0.0)
        cur_rooms += a.rooms_sold
        cur_days += 1
    if cur_days:
        buckets.append((cur_reviews, cur_rooms, cur_days))

    mae_pt, n_buckets = _backtest(buckets, total_reviews, total_rooms, rooms)

    return Calibrated(
        rate=rate,
        ci_low=lo,
        ci_high=hi,
        method="自社実績で実測",
        overlap_days=len(usable),
        rooms_sold=total_rooms,
        reviews=total_reviews,
        mae_pt=mae_pt,
        buckets=n_buckets,
        usability=_usability(mae_pt, usable_mae_pt, relative_only_mae_pt),
        measured=True,
    )
