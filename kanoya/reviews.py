"""スナップショットから「滞在日ベースの宿泊レビュー数」時系列を組み立てる。

処理は 3 段:
  1. クチコミ総数の差分 → 投稿日ベースの日次新規レビュー数
  2. 外来レビュー（レストラン利用のみ）の控除
  3. 投稿遅延ぶん日付を引き戻し、滞在日ベースに変換

3 の遅延は施設ごとに分布が異なるため、平均遅延日数による一律シフトで近似する。
この近似が効かない直近数日は :func:`window_bounds` で窓から落とす。
"""

from __future__ import annotations

from datetime import date, timedelta

from .config import Property
from .store import Snapshot


def daily_new_reviews(snapshots: list[Snapshot]) -> dict[date, float]:
    """投稿日ベースの日次新規レビュー数を返す。

    観測に穴があるときは、その区間の増分を日数で按分する。1 日ごとに観測できて
    いれば按分は起きず、そのままの日次値になる。

    クチコミ総数が減ることがある（投稿の削除、事業者側の統合）。減少ぶんは
    負の需要ではないので 0 に丸める。
    """
    ordered = sorted(snapshots, key=lambda s: s.date)
    series: dict[date, float] = {}

    for prev, curr in zip(ordered, ordered[1:]):
        gap_days = (curr.date - prev.date).days
        if gap_days <= 0:
            continue

        delta = max(0, curr.user_rating_count - prev.user_rating_count)
        per_day = delta / gap_days
        for offset in range(1, gap_days + 1):
            day = prev.date + timedelta(days=offset)
            series[day] = series.get(day, 0.0) + per_day

    return series


def stay_review_series(
    snapshots: list[Snapshot],
    prop: Property,
    review_lag_days: int,
) -> dict[date, float]:
    """滞在日ベースの宿泊レビュー数を返す。

    外来レビューを控除したうえで、投稿日を平均遅延ぶん過去にずらす。
    """
    posted = daily_new_reviews(snapshots)
    stay_ratio = prop.stay_review_ratio()
    shift = timedelta(days=review_lag_days)

    return {
        posted_day - shift: count * stay_ratio
        for posted_day, count in posted.items()
        if count > 0
    }


def window_bounds(
    as_of: date,
    window_days: int,
    review_lag_days: int,
    unstable_tail_days: int,
) -> tuple[date, date]:
    """集計に使う滞在日の窓 [start, end] を返す。

    観測できるのは投稿されたレビューだけなので、滞在日ベースで見ると
    「直近」は必ず取りこぼす。平均遅延ぶんに加えて、遅延分布のばらつきを
    吸収する猶予（unstable_tail_days）も窓の外に置く。
    """
    end = as_of - timedelta(days=review_lag_days + unstable_tail_days)
    start = end - timedelta(days=window_days - 1)
    return start, end


def sum_in_window(series: dict[date, float], start: date, end: date) -> float:
    """窓内のレビュー数を合計する。"""
    return sum(count for day, count in series.items() if start <= day <= end)


def date_range(start: date, end: date) -> list[date]:
    """start から end までの日付を昇順で返す（両端を含む）。"""
    if end < start:
        return []
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def rolling_sum(
    series: dict[date, float],
    days: list[date],
    window: int,
) -> list[float]:
    """各日について、その日を末尾とする `window` 日の移動合計を返す。"""
    if window < 1:
        raise ValueError("window は 1 以上にすること。")

    return [
        sum(
            series.get(day - timedelta(days=offset), 0.0)
            for offset in range(window)
        )
        for day in days
    ]


def observation_span(snapshots: list[Snapshot]) -> tuple[date, date] | None:
    """観測が存在する期間を返す。スナップショットが 2 件未満なら None。"""
    if len(snapshots) < 2:
        return None
    ordered = sorted(snapshots, key=lambda s: s.date)
    return ordered[0].date, ordered[-1].date


def latest_rating(snapshots: list[Snapshot]) -> float | None:
    """最新の評価点を返す。評価が一度も取れていなければ None。"""
    for snap in sorted(snapshots, key=lambda s: s.date, reverse=True):
        if snap.rating is not None:
            return snap.rating
    return None
