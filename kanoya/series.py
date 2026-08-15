"""日次レビュー系列の組み立て。

スナップショットの累計件数 → 日次増分 → 滞在日への引き戻し → 外来控除、
という順で処理する。この順序には意味がある。増分を取る前に控除すると
累計の段差が壊れるし、控除を滞在日引き戻しの後に回しても結果は同じだが
窓合計の検算がしにくくなる。
"""

from __future__ import annotations

import datetime as dt
from typing import Mapping

from .config import Property
from .store import Snapshot


def date_range(start: dt.date, end: dt.date) -> list[dt.date]:
    """start から end までの両端を含む日付列。"""
    if end < start:
        return []
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def daily_increments(series: Mapping[dt.date, Snapshot]) -> dict[dt.date, int]:
    """累計件数の日次差分を取る。

    最初のスナップショットには前日がないため増分を定義できず、除外する。
    観測が飛んだ区間（例: poll 障害で 3 日空いた）は、その間に増えた分を
    日数で等分して配る。レビューは特定日に偏る性質が弱いのでこの近似で足りる。
    差分が負になるのはレビュー削除。0 に丸める（削除分を「マイナスの需要」として
    扱ってしまうのを防ぐ）。
    """
    dates = sorted(series)
    out: dict[dt.date, int] = {}
    for prev, cur in zip(dates, dates[1:]):
        delta = series[cur].user_rating_count - series[prev].user_rating_count
        if delta < 0:
            delta = 0
        gap = (cur - prev).days
        if gap == 1:
            out[cur] = delta
            continue
        # 欠測区間を等分。端数は後ろの日に寄せる。
        base, extra = divmod(delta, gap)
        for i in range(gap):
            day = prev + dt.timedelta(days=i + 1)
            out[day] = base + (1 if i >= gap - extra else 0)
    return out


def shift_to_stay_dates(posted: Mapping[dt.date, int], lag_days: int) -> dict[dt.date, int]:
    """投稿日ベースの系列を滞在日ベースに引き戻す。

    平均遅延 lag_days で一律にずらす。実際の遅延は分布を持つので、これは
    分布の中心だけを合わせる近似。窓合計を取る用途では十分だが、日次では
    使えない（README の限界事項を参照）。
    """
    return {day - dt.timedelta(days=lag_days): count for day, count in posted.items()}


def lodging_series(
    prop: Property,
    snapshots: Mapping[dt.date, Snapshot],
    lag_days: int,
) -> dict[dt.date, float]:
    """1 施設の、滞在日ベース・外来控除済みレビュー系列。"""
    stay = shift_to_stay_dates(daily_increments(snapshots), lag_days)
    return {day: prop.lodging_reviews(count) for day, count in stay.items()}


def window_sum(series: Mapping[dt.date, float], start: dt.date, end: dt.date) -> float:
    """[start, end] 両端含む区間の合計。"""
    return sum(v for day, v in series.items() if start <= day <= end)


def coverage(series: Mapping[dt.date, float], start: dt.date, end: dt.date) -> float:
    """区間のうち実際に観測がある日の比率。

    poll を始めたばかりの施設は窓を埋めきれない。合計だけ見ると需要が
    低いのか観測が無いのか区別できないため、被覆率を別に持つ。
    """
    span = (end - start).days + 1
    if span <= 0:
        return 0.0
    observed = sum(1 for day in series if start <= day <= end)
    return observed / span


def rolling_sum(
    series: Mapping[dt.date, float],
    start: dt.date,
    end: dt.date,
    window: int,
) -> list[float]:
    """[start, end] の各日について、その日を末尾とする window 日の後方移動合計。"""
    out: list[float] = []
    for day in date_range(start, end):
        left = day - dt.timedelta(days=window - 1)
        out.append(window_sum(series, left, day))
    return out


def combine(series_list: list[Mapping[dt.date, float]]) -> dict[dt.date, float]:
    """複数施設の系列を日付ごとに足し合わせ、エリア合計を作る。"""
    out: dict[dt.date, float] = {}
    for series in series_list:
        for day, value in series.items():
            out[day] = out.get(day, 0.0) + value
    return dict(sorted(out.items()))
