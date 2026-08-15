"""レビュー投稿率の実測と、その推定精度の検証。

このシステムの信頼性はすべてここに乗っている。レビュー投稿率は自社 PMS の
販売実績とレビュー発生を突合して実測し、同じ率を競合にも当てる。
競合の投稿率が自社と違えば推定は系統的にずれる。それは避けられない仮定なので、
せめて自社での当てはまりを backtest で数値化し、使ってよい範囲を示す。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .reviews import date_range, sum_in_window

#: 95% 信頼区間の z 値。
Z_95 = 1.959963985


class CalibrationError(ValueError):
    """キャリブレーションに必要なデータが足りないときに送出する。"""


@dataclass(frozen=True)
class ReviewRate:
    """レビュー投稿率（販売 1 室あたり何件のレビューが立つか）。"""

    rate: float
    ci_low: float
    ci_high: float
    method: str
    matched_days: int = 0
    matched_rooms_sold: float = 0.0
    matched_reviews: float = 0.0

    @property
    def is_measured(self) -> bool:
        return self.method == "calibrated"


@dataclass(frozen=True)
class BacktestFold:
    start: date
    end: date
    estimated_occupancy: float
    actual_occupancy: float

    @property
    def error_pt(self) -> float:
        return (self.estimated_occupancy - self.actual_occupancy) * 100


@dataclass(frozen=True)
class Backtest:
    """自社実績に対する推定の当てはまり。"""

    folds: list[BacktestFold]

    @property
    def n(self) -> int:
        return len(self.folds)

    @property
    def mean_abs_error_pt(self) -> float:
        if not self.folds:
            return float("nan")
        return sum(abs(f.error_pt) for f in self.folds) / len(self.folds)

    @property
    def usability(self) -> str:
        """推定値をどこまで使ってよいかの区分。"""
        if self.n < 3:
            return "検証不足"
        mae = self.mean_abs_error_pt
        if mae <= 5.0:
            return "水準の議論に使える"
        if mae <= 12.0:
            return "相対比較のみ"
        return "使用不可"


def load_pms_actuals(path: str | Path) -> dict[date, int]:
    """自社 PMS の日次販売室数 CSV を読む。

    想定する列は date,rooms_sold（ヘッダ必須）。
    """
    path = Path(path)
    if not path.exists():
        raise CalibrationError(f"PMS 実績ファイルが見つからない: {path}")

    actuals: dict[date, int] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "date" not in reader.fieldnames:
            raise CalibrationError(f"{path} に date 列がない。")
        if "rooms_sold" not in reader.fieldnames:
            raise CalibrationError(f"{path} に rooms_sold 列がない。")

        for row_no, row in enumerate(reader, start=2):
            if not (row.get("date") or "").strip():
                continue
            try:
                day = date.fromisoformat(row["date"].strip())
                rooms_sold = int(float(row["rooms_sold"]))
            except (ValueError, TypeError) as exc:
                raise CalibrationError(f"{path}:{row_no} が読めない: {exc}") from exc
            if rooms_sold < 0:
                raise CalibrationError(f"{path}:{row_no} の rooms_sold が負。")
            actuals[day] = rooms_sold

    if not actuals:
        raise CalibrationError(f"{path} に有効な行がない。")
    return actuals


def calibrate_review_rate(
    own_stay_series: dict[date, float],
    actuals: dict[date, int],
    observation_span: tuple[date, date] | None,
    review_lag_days: int = 0,
    unstable_tail_days: int = 0,
    min_matched_rooms: float = 50.0,
) -> ReviewRate:
    """自社実績とレビュー発生を突合して投稿率を実測する。

    突合できるのは「PMS 実績があり、かつその滞在に対するレビューが出揃うまで
    観測を続けていた」日だけ。

    ここで境界を 2 つ切る必要がある。観測開始前の日を入れるとレビュー 0 件として
    数えてしまう。観測終了の直前の日を入れると、まだ投稿されていないレビューを
    0 件として数えてしまう。どちらも投稿率を過小に見積もらせ、その過小な率で
    競合を割ると全施設の稼働が過大に出る。
    """
    if observation_span is None:
        raise CalibrationError("スナップショットが 2 件未満で、レビュー発生を追えない。")

    obs_start, obs_end = observation_span
    match_end = obs_end - timedelta(days=review_lag_days + unstable_tail_days)
    if match_end < obs_start:
        raise CalibrationError(
            f"観測期間が投稿遅延（{review_lag_days + unstable_tail_days}日）より短く、"
            "投稿率を実測できない。"
        )

    matched_days = [day for day in actuals if obs_start <= day <= match_end]
    if not matched_days:
        raise CalibrationError("PMS 実績と観測期間が重ならない。")

    rooms_sold = float(sum(actuals[day] for day in matched_days))
    reviews = sum(own_stay_series.get(day, 0.0) for day in matched_days)

    if rooms_sold < min_matched_rooms:
        raise CalibrationError(
            f"突合できた販売室数が {rooms_sold:.0f} 室しかない"
            f"（最低 {min_matched_rooms:.0f} 室必要）。"
        )

    rate = reviews / rooms_sold
    low, high = wilson_interval(reviews, rooms_sold)

    return ReviewRate(
        rate=rate,
        ci_low=low,
        ci_high=high,
        method="calibrated",
        matched_days=len(matched_days),
        matched_rooms_sold=rooms_sold,
        matched_reviews=reviews,
    )


def fixed_review_rate(rate: float) -> ReviewRate:
    """実測できないときに使う固定率。信頼区間は引かない。"""
    return ReviewRate(rate=rate, ci_low=rate, ci_high=rate, method="fixed")


def wilson_interval(successes: float, trials: float, z: float = Z_95) -> tuple[float, float]:
    """Wilson score 区間。

    投稿率は 0 に近く試行数も大きいため、正規近似より Wilson のほうが素直。
    レビュー数は外来控除で小数になりうるので float を受ける。
    """
    if trials <= 0:
        return (0.0, 0.0)

    p = successes / trials
    z2 = z * z
    denom = 1 + z2 / trials
    center = (p + z2 / (2 * trials)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials))
    return (max(0.0, center - margin), min(1.0, center + margin))


def backtest_occupancy(
    own_stay_series: dict[date, float],
    actuals: dict[date, int],
    rooms: int,
    review_rate: float,
    observation_span: tuple[date, date] | None,
    review_lag_days: int = 0,
    unstable_tail_days: int = 0,
    fold_days: int = 30,
    max_folds: int = 6,
) -> Backtest:
    """自社の実績稼働と、レビューから推定した稼働を期間ごとに突き合わせる。

    投稿率そのものを自社実績から出しているため、全期間の平均は定義上ほぼ一致する。
    意味があるのは期間ごとのばらつき — 推定が実績をどれだけ追随できるかで、
    これが競合に当てたときの誤差の目安になる。
    """
    if observation_span is None or review_rate <= 0 or rooms <= 0:
        return Backtest(folds=[])

    obs_start, obs_end = observation_span
    match_end = obs_end - timedelta(days=review_lag_days + unstable_tail_days)
    matched = sorted(day for day in actuals if obs_start <= day <= match_end)
    if len(matched) < fold_days:
        return Backtest(folds=[])

    folds: list[BacktestFold] = []
    end = matched[-1]
    earliest = matched[0]

    while len(folds) < max_folds:
        start = end - timedelta(days=fold_days - 1)
        if start < earliest:
            break

        days = date_range(start, end)
        actual_sold = sum(actuals.get(day, 0) for day in days)
        capacity = rooms * len(days)
        if capacity == 0:
            break

        reviews = sum_in_window(own_stay_series, start, end)
        estimated_sold = reviews / review_rate

        folds.append(
            BacktestFold(
                start=start,
                end=end,
                estimated_occupancy=min(1.0, estimated_sold / capacity),
                actual_occupancy=actual_sold / capacity,
            )
        )
        end = start - timedelta(days=1)

    folds.reverse()
    return Backtest(folds=folds)
