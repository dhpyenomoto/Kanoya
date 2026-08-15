"""スナップショット列 → 日次レビュー系列。

三段階の変換を行う。

1. 累計件数の差分を取り、「投稿日ベースの日次レビュー数」を作る。
   取得が飛んだ日がある場合は、その区間に均等に配分する（だから float になる）。
2. 外来客レビューを控除する（restaurant_review_share）。
3. 投稿遅延ぶん日付を巻き戻し、「滞在日ベース」に直す。
   結果として、直近 lag 日は未確定になる。この期間は as_of に含めない。
"""

from __future__ import annotations

from datetime import date, timedelta

from .config import PropertyConfig
from .models import Snapshot


def daterange(start: date, end: date) -> list[date]:
    """start〜end（両端含む）の日付列。"""
    if end < start:
        return []
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def posts_by_publish_date(snapshots: list[Snapshot]) -> dict[date, float]:
    """累計件数の差分から、投稿日ごとのレビュー数を復元する。

    差分は「前回取得の翌日から今回取得日まで」に均等配分する。
    件数が減る（レビュー削除・スパム除去）ことは実際に起きるので、
    負の増分は 0 に潰す。削除は稼働の情報を持たない。
    """
    posts: dict[date, float] = {}
    for previous, current in zip(snapshots, snapshots[1:]):
        gap = (current.captured_on - previous.captured_on).days
        if gap <= 0:
            continue
        delta = max(0, current.user_rating_count - previous.user_rating_count)
        if delta == 0:
            continue
        per_day = delta / gap
        for offset in range(gap):
            day = previous.captured_on + timedelta(days=offset + 1)
            posts[day] = posts.get(day, 0.0) + per_day
    return posts


def shift_to_stay_date(
    posts: dict[date, float], lag_days: int
) -> dict[date, float]:
    """投稿日ベースを滞在日ベースに巻き戻す。"""
    return {day - timedelta(days=lag_days): value for day, value in posts.items()}


def stay_reviews(raw: dict[date, float], prop: PropertyConfig) -> dict[date, float]:
    """宿泊客由来ぶんだけを残す。"""
    share = prop.stay_review_share
    return {day: value * share for day, value in raw.items()}


def series(values: dict[date, float], start: date, end: date) -> list[float]:
    """欠測を 0 で埋めた連続系列。"""
    return [values.get(day, 0.0) for day in daterange(start, end)]


def total(values: dict[date, float], start: date, end: date) -> float:
    return sum(v for day, v in values.items() if start <= day <= end)


def rolling_sum(values: list[float], window: int) -> list[float]:
    """後方 window 日の移動合計。先頭 window-1 点は部分和になる。"""
    out: list[float] = []
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= window:
            running -= values[i - window]
        out.append(running)
    return out


class PropertyTimeline:
    """1施設ぶんの、投稿日/滞在日それぞれの日次系列。"""

    def __init__(self, prop: PropertyConfig, snapshots: list[Snapshot], lag_days: int):
        self.property = prop
        self.snapshots = snapshots
        self.lag_days = lag_days
        self.raw_by_post = posts_by_publish_date(snapshots)
        self.raw_by_stay = shift_to_stay_date(self.raw_by_post, lag_days)
        self.stay_by_stay = stay_reviews(self.raw_by_stay, prop)

    @property
    def rating(self) -> float | None:
        return self.snapshots[-1].rating if self.snapshots else None

    @property
    def coverage(self) -> tuple[date, date] | None:
        """滞在日ベースで意味のあるデータが存在する範囲。"""
        if not self.raw_by_stay:
            return None
        days = sorted(self.raw_by_stay)
        return days[0], days[-1]

    def raw_total(self, start: date, end: date) -> float:
        return total(self.raw_by_stay, start, end)

    def stay_total(self, start: date, end: date) -> float:
        return total(self.stay_by_stay, start, end)

    def raw_series(self, start: date, end: date) -> list[float]:
        return series(self.raw_by_stay, start, end)


def build_timelines(
    store, properties, lag_days: int
) -> dict[str, PropertyTimeline]:
    return {
        prop.key: PropertyTimeline(prop, store.snapshots(prop.key), lag_days)
        for prop in properties
    }
