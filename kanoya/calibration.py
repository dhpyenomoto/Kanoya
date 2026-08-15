"""レビュー投稿率の較正とバックテスト。

このシステム全体の信頼性は、ここで求める投稿率 p に集約される。
p = 自社の突合レビュー数 ÷ 自社の実売室数。競合にも同じ p を当てる。

p の推定を自社実績に対して検証しないかぎり、競合の推定稼働率は
「それらしい数字」でしかない。バックテストはそのための手続きであり、
その平均誤差が推定値の使ってよい範囲を決める。
"""

from __future__ import annotations

import csv
import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .series import date_range, window_sum

Z95 = 1.959963984540054


def wilson_interval(successes: float, trials: float, z: float = Z95) -> tuple[float, float]:
    """二項比率の Wilson スコア区間。

    正規近似（Wald）は p が小さく n が中規模のとき下限が負に振れるため使わない。
    successes は外来控除で非整数になりうるので float を受ける。
    """
    if trials <= 0:
        return (0.0, 0.0)
    p = successes / trials
    denom = 1.0 + z * z / trials
    centre = (p + z * z / (2.0 * trials)) / denom
    margin = (z / denom) * math.sqrt(p * (1.0 - p) / trials + z * z / (4.0 * trials * trials))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True)
class PostingRate:
    """自社実績で実測したレビュー投稿率。"""

    point: float
    low: float
    high: float
    days: int
    rooms_sold: int
    reviews: float

    @property
    def relative_se(self) -> float:
        """点推定に対する相対標準誤差。推定稼働率の誤差伝播に使う。"""
        if self.point <= 0:
            return float("inf")
        return ((self.high - self.low) / 2.0 / Z95) / self.point


@dataclass(frozen=True)
class BacktestBlock:
    start: dt.date
    end: dt.date
    actual_occupancy: float
    estimated_occupancy: float

    @property
    def error_pt(self) -> float:
        return (self.estimated_occupancy - self.actual_occupancy) * 100.0


@dataclass(frozen=True)
class Backtest:
    blocks: tuple[BacktestBlock, ...]

    @property
    def mean_absolute_error_pt(self) -> float:
        if not self.blocks:
            return float("inf")
        return sum(abs(b.error_pt) for b in self.blocks) / len(self.blocks)

    @property
    def bias_pt(self) -> float:
        """符号付き平均誤差。系統的に上振れ/下振れしていないかを見る。"""
        if not self.blocks:
            return 0.0
        return sum(b.error_pt for b in self.blocks) / len(self.blocks)

    @property
    def usable_range(self) -> str:
        """推定値をどこまで使ってよいかの判定。

        水準そのものを議論できるのは誤差が小さいときだけ。誤差が開くにつれ、
        施設間の順位づけ → 方向感のみ、と用途を落とす。
        """
        mae = self.mean_absolute_error_pt
        if mae <= 3.0:
            return "水準の議論に使える"
        if mae <= 7.0:
            return "相対比較のみ"
        return "方向感のみ"


def load_pms_actuals(path: str | Path) -> dict[dt.date, int]:
    """自社 PMS の日次実売室数を読む。列は date,rooms_sold[,rooms_available]。"""
    path = Path(path)
    out: dict[dt.date, int] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if not row.get("date"):
                continue
            out[dt.date.fromisoformat(row["date"].strip())] = int(row["rooms_sold"])
    return out


def estimate_posting_rate(
    own_reviews: Mapping[dt.date, float],
    pms_sold: Mapping[dt.date, int],
    start: dt.date,
    end: dt.date,
) -> PostingRate:
    """突合窓での投稿率を実測する。

    レビュー側は滞在日ベースに引き戻し済みであることが前提。PMS と同じ
    日付軸に載っていないと分母と分子がずれる。
    """
    days = [d for d in date_range(start, end) if d in pms_sold]
    sold = sum(pms_sold[d] for d in days)
    reviews = window_sum(own_reviews, start, end)
    if sold <= 0:
        raise ValueError("突合窓に自社の実売室数がない。PMS 実績の期間を確認すること")
    point = reviews / sold
    low, high = wilson_interval(reviews, sold)
    return PostingRate(
        point=point,
        low=low,
        high=high,
        days=len(days),
        rooms_sold=sold,
        reviews=reviews,
    )


def backtest(
    own_reviews: Mapping[dt.date, float],
    pms_sold: Mapping[dt.date, int],
    start: dt.date,
    end: dt.date,
    rooms: int,
    blocks: int,
    block_days: int,
) -> Backtest:
    """突合窓の中で推定稼働率と実績を突き合わせる。

    検証区間の長さは本番の集計窓と同じ block_days にする。ここを短く取ると、
    ダッシュボードが一度も使わない粒度で誤差を測ることになり、数字が実際より
    悪く出る。レビュー件数の計数誤差は区間が短いほど効くためである。

    突合窓に block_days の区間を非重複で並べると 2〜3 本しか取れないので、
    等間隔にずらした重複区間を使う。重複しているぶん各区間は独立ではなく、
    n はそのまま自由度として扱えない（誤差の大きさの目安として読む）。

    各区間の投稿率は leave-one-out で求める。その区間自身のレビューを投稿率の
    算定から外さないと、自分で自分を予測することになり誤差が構造的に小さく出る。
    """
    days = [d for d in date_range(start, end) if d in pms_sold]
    if not days or blocks < 2 or block_days < 1:
        return Backtest(blocks=())
    if len(days) < block_days:
        block_days = len(days)

    total_sold = sum(pms_sold[d] for d in days)
    total_reviews = window_sum(own_reviews, start, end)

    span = len(days) - block_days
    stride = span / (blocks - 1) if blocks > 1 and span > 0 else 0.0
    out: list[BacktestBlock] = []
    for i in range(blocks):
        offset = int(round(i * stride))
        chunk = days[offset : offset + block_days]
        if not chunk:
            continue
        b_start, b_end = chunk[0], chunk[-1]
        b_sold = sum(pms_sold[d] for d in chunk)
        b_reviews = window_sum(own_reviews, b_start, b_end)
        if b_sold <= 0:
            continue

        loo_sold = total_sold - b_sold
        loo_reviews = total_reviews - b_reviews
        if loo_sold <= 0 or loo_reviews <= 0:
            continue
        loo_rate = loo_reviews / loo_sold

        capacity = rooms * len(chunk)
        out.append(
            BacktestBlock(
                start=b_start,
                end=b_end,
                actual_occupancy=b_sold / capacity,
                estimated_occupancy=(b_reviews / loo_rate) / capacity,
            )
        )
    return Backtest(blocks=tuple(out))
