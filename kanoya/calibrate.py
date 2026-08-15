"""レビュー投稿率のキャリブレーションと、自社実績に対する検証。

このダッシュボードの信用は一点に懸かっている。「自社の実績と突き合わせたとき、
推定稼働はどれだけ当たるのか」。当たらないなら数字は出さない。ここはその検証。
"""

from __future__ import annotations

import csv
import math
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .models import Backtest, PostingRate
from .timeline import PropertyTimeline, daterange

Z95 = 1.959964


def load_pms_actuals(path: str | Path) -> dict[date, float]:
    """自社PMSの日次販売室数。列は date, rooms_sold。"""
    path = Path(path)
    if not path.exists():
        return {}
    actuals: dict[date, float] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if not row.get("date"):
                continue
            actuals[date.fromisoformat(row["date"].strip())] = float(row["rooms_sold"])
    return actuals


def calibration_span(as_of: date, days: int, actuals: dict[date, float]) -> tuple[date, date]:
    """突合に使う期間。PMS実績のある範囲に丸める。"""
    end = as_of
    start = as_of - timedelta(days=days - 1)
    if actuals:
        start = max(start, min(actuals))
        end = min(end, max(actuals))
    return start, end


def measure_posting_rate(
    timeline: PropertyTimeline,
    actuals: dict[date, float],
    start: date,
    end: date,
    fallback: float = 0.10,
) -> PostingRate:
    """投稿率 ＝ 宿泊客由来レビュー数 ÷ 実販売室数。

    信頼区間はレビュー件数側のポアソン誤差から引く。分母（販売室数）は
    PMSの実数なので誤差を持たない。効くのは「レビューが何件出たか」の揺らぎだけ。
    """
    days = daterange(start, end)
    sold = sum(actuals.get(day, 0.0) for day in days)
    raw = timeline.raw_total(start, end)
    stay = timeline.stay_total(start, end)

    if sold <= 0 or stay <= 0:
        return PostingRate(
            rate=fallback,
            ci_low=fallback,
            ci_high=fallback,
            days=len(days),
            sold_rooms=sold,
            raw_reviews=raw,
            stay_reviews=stay,
            source="既定値（自社実績なし）",
        )

    rate = stay / sold
    margin = Z95 * math.sqrt(stay)
    return PostingRate(
        rate=rate,
        ci_low=max(0.0, (stay - margin) / sold),
        ci_high=(stay + margin) / sold,
        days=len(days),
        sold_rooms=sold,
        raw_reviews=raw,
        stay_reviews=stay,
        source="自社実績で実測",
    )


def backtest(
    config: Config,
    timeline: PropertyTimeline,
    actuals: dict[date, float],
    rate: PostingRate,
    start: date,
    end: date,
) -> Backtest:
    """突合期間を等分し、各区間で推定稼働と実稼働を比べる。"""
    days = daterange(start, end)
    n_windows = max(1, config.estimation.backtest_windows)
    if len(days) < n_windows * 7 or rate.rate <= 0:
        return Backtest(mean_abs_error_pt=float("nan"), windows=0, usable_for="検証不能")

    size = len(days) // n_windows
    rooms = timeline.property.rooms
    errors: list[float] = []
    for i in range(n_windows):
        chunk = days[i * size : (i + 1) * size]
        capacity = rooms * len(chunk)
        actual = sum(actuals.get(day, 0.0) for day in chunk) / capacity * 100.0
        estimated = (
            timeline.stay_total(chunk[0], chunk[-1]) / rate.rate / capacity * 100.0
        )
        errors.append(abs(estimated - actual))

    mae = sum(errors) / len(errors)
    rules = config.pricing
    if mae <= rules.backtest_level_max_pt:
        usable = "水準の議論に使える"
    elif mae <= rules.backtest_trend_max_pt:
        usable = "推移の議論に限る"
    else:
        usable = "方向感のみ"
    return Backtest(mean_abs_error_pt=mae, windows=len(errors), usable_for=usable)


def estimate_lag_days(
    timeline: PropertyTimeline,
    actuals: dict[date, float],
    start: date,
    end: date,
    max_lag: int = 30,
) -> int | None:
    """滞在→投稿の遅延を、自社レビューとPMS実績の相互相関から推定する。

    Places API からは滞在日が分からないので、遅延は自社でしか測れない。
    競合には同じ遅延を仮定して適用する（施設ごとに違うのは分かっているが、
    観測できない以上ここは仮定に留める）。
    """
    days = daterange(start, end)
    if len(days) < 60:
        return None
    sold = [actuals.get(day, 0.0) for day in days]
    if not any(sold):
        return None

    best_lag, best_corr = None, -2.0
    for lag in range(max_lag + 1):
        posts = [
            timeline.raw_by_post.get(day + timedelta(days=lag), 0.0) for day in days
        ]
        corr = _pearson(sold, posts)
        if corr is not None and corr > best_corr:
            best_lag, best_corr = lag, corr
    return best_lag


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)
