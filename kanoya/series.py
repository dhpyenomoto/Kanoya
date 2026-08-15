"""日次系列のユーティリティ。累計→増分、投稿遅延の巻き戻し、移動合計。"""

from __future__ import annotations

import datetime as dt

Series = dict[dt.date, float]


def date_range(start: dt.date, end: dt.date) -> list[dt.date]:
    """start〜end（両端含む）。"""
    if end < start:
        return []
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def increments(counts: dict[dt.date, int]) -> Series:
    """クチコミ累計から日次増分を作る。

    - 取得欠損で日付が飛んでいる場合は、その区間に均等配分する。
    - Google 側の削除で累計が減ることがあるため、負の増分は 0 に丸める。
    - 最初のスナップショット日は「増分不明」なので系列に含めない。
    """
    out: Series = {}
    dates = sorted(counts)
    for prev, cur in zip(dates, dates[1:]):
        gap = (cur - prev).days
        if gap <= 0:
            continue
        delta = max(0, counts[cur] - counts[prev])
        per_day = delta / gap
        for i in range(gap):
            out[prev + dt.timedelta(days=i + 1)] = per_day
    return out


def shift_back(series: Series, days: int) -> Series:
    """投稿日ベースの系列を滞在日ベースに巻き戻す（既定10日）。"""
    return {d - dt.timedelta(days=days): v for d, v in series.items()}


def window_sum(series: Series, start: dt.date, end: dt.date) -> float:
    """start〜end（両端含む）の合計。"""
    return sum(v for d, v in series.items() if start <= d <= end)


def rolling_sum(series: Series, dates: list[dt.date], k: int) -> list[float]:
    """dates 各点における直近 k 日（当日含む）の合計。"""
    out: list[float] = []
    for d in dates:
        start = d - dt.timedelta(days=k - 1)
        out.append(window_sum(series, start, d))
    return out


def add(a: Series, b: Series) -> Series:
    out = dict(a)
    for d, v in b.items():
        out[d] = out.get(d, 0.0) + v
    return out
