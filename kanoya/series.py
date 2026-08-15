"""スナップショット列 → 滞在日ベースの日次クチコミ系列。

手順:
  1. 連続する 2 スナップショットの累計件数の差を取る（増分）。
  2. 増分は、その間隔の各日に均等に配分する（間隔が 1 日なら実測そのもの）。
  3. 投稿日を review_lag_days だけ引き戻して滞在日に割り当てる。
  4. restaurant_review_share の分を控除する（外来客レビューの混入分）。

Google 側のレビュー削除で累計が減ることがある。減少分は 0 に切り捨て、
data_quality に記録して信頼度判定に回す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .store import Snapshot


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


@dataclass
class ReviewSeries:
    """滞在日 -> その日の滞在に帰属するクチコミ件数（小数）。"""

    property_key: str
    daily: dict[date, float] = field(default_factory=dict)
    covered: set[date] = field(default_factory=set)
    negative_deltas: int = 0
    first_snapshot: date | None = None
    last_snapshot: date | None = None

    def total(self, start: date, end: date) -> float:
        return sum(v for d, v in self.daily.items() if start <= d <= end)

    def coverage(self, start: date, end: date) -> float:
        """区間のうち、スナップショット間隔で裏づけられている日の割合。"""
        days = (end - start).days + 1
        if days <= 0:
            return 0.0
        hit = sum(1 for d in daterange(start, end) if d in self.covered)
        return hit / days

    def values(self, start: date, end: date) -> list[float]:
        return [self.daily.get(d, 0.0) for d in daterange(start, end)]

    def rolling(self, start: date, end: date, window: int) -> list[float]:
        """区間内の各日について、その日を末尾とする window 日移動合計。"""
        out = []
        for d in daterange(start, end):
            lo = d - timedelta(days=window - 1)
            out.append(sum(self.daily.get(x, 0.0) for x in daterange(lo, d)))
        return out


def build_series(
    snapshots: list[Snapshot],
    *,
    property_key: str,
    review_lag_days: int = 10,
    restaurant_review_share: float = 0.0,
) -> ReviewSeries:
    snaps = sorted(snapshots, key=lambda s: s.taken_on)
    series = ReviewSeries(property_key=property_key)
    if not snaps:
        return series

    series.first_snapshot = snaps[0].taken_on
    series.last_snapshot = snaps[-1].taken_on
    keep = 1.0 - restaurant_review_share
    lag = timedelta(days=review_lag_days)

    for prev, cur in zip(snaps, snaps[1:]):
        gap = (cur.taken_on - prev.taken_on).days
        if gap <= 0:
            continue
        delta = cur.user_rating_count - prev.user_rating_count
        if delta < 0:
            series.negative_deltas += 1
            delta = 0
        per_day = (delta * keep) / gap
        for i in range(1, gap + 1):
            posted_on = prev.taken_on + timedelta(days=i)
            stay_on = posted_on - lag
            series.daily[stay_on] = series.daily.get(stay_on, 0.0) + per_day
            series.covered.add(stay_on)

    return series


def area_rolling(
    series_list: list[ReviewSeries], start: date, end: date, window: int
) -> list[float]:
    out = []
    for d in daterange(start, end):
        lo = d - timedelta(days=window - 1)
        out.append(
            sum(s.daily.get(x, 0.0) for s in series_list for x in daterange(lo, d))
        )
    return out
