"""デモ用データ生成。

Google Places の日次スナップショットは「毎日取り続けた過去」がないと作れない。
運用開始直後や検証時に配管を通すため、合成データでストアを埋める。

生成するのは以下の2つだけで、そこから先は本番と完全に同じ経路を通る。
  - data/kanoya.sqlite3 の snapshots（累計レビュー件数の日次観測）
  - data/pms_actuals.csv（自社PMSの日次販売室数）

生成される数字は合成であり、実在の施設の稼働ではない。
"""

from __future__ import annotations

import csv
import math
from datetime import date, timedelta

from kanoya.config import Config
from kanoya.store import Store
from kanoya.timeline import daterange

# 施設ごとの、期間別クチコミ総数（滞在日ベース・外来客ぶんを含む生の件数）。
# 順に (直近90日, その前の90日, さらに前のキャリブレーション期間)。
TARGET_REVIEWS: dict[str, tuple[int, int, int]] = {
    "kanoya": (65, 61, 52),
    "comp_a": (105, 112, 96),
    "comp_b": (109, 107, 96),
    "comp_c": (24, 20, 18),
    "comp_d": (609, 621, 560),
    "comp_e": (157, 155, 140),
}

# 累計件数の開始値。差分だけが意味を持つので水準は任意。
BASE_COUNTS: dict[str, int] = {
    "kanoya": 214,
    "comp_a": 486,
    "comp_b": 731,
    "comp_c": 92,
    "comp_d": 3184,
    "comp_e": 642,
}

# 自社評価の推移。下限割れのシグナルを再現するため、期間内で下がっていく。
OWN_RATING_FROM, OWN_RATING_TO = 4.71, 4.53

RATINGS: dict[str, float] = {
    "comp_a": 4.72,
    "comp_b": 4.44,
    "comp_c": 4.81,
    "comp_d": 4.18,
    "comp_e": 4.61,
}

# 自社の実販売室数（室）。期間別に、上の TARGET_REVIEWS と整合する水準に置く。
# 合計 929室に対しレビュー 178件、うち宿泊由来 97.9件 → 投稿率 10.5%。
OWN_SEGMENT_SOLD = (271, 318, 340)

# 推定精度を意図した水準に落ち着かせるための、投稿率の緩やかな揺らぎ。
# 実データではこの揺らぎこそが誤差の正体であり、ここではそれを合成している。
ACCURACY_WOBBLE = 0.085
WOBBLE_PERIOD_DAYS = 74.0


def _season(day: date) -> float:
    """奈良の年間需要。4月（桜）と11月（正倉院展・紅葉）に山を持つ。"""
    doy = day.timetuple().tm_yday
    return (
        1.0
        + 0.13 * math.cos(2 * math.pi * (doy - 105) / 365.0)
        + 0.07 * math.cos(2 * math.pi * (doy - 320) / 365.0)
    )


def _weekday(day: date) -> float:
    """金土に寄る。7日移動合計にすると消えるが、日次の粒度を現実に寄せる。"""
    return {0: 0.90, 1: 0.88, 2: 0.90, 3: 0.95, 4: 1.24, 5: 1.30, 6: 1.02}[day.weekday()]


def _event_bump(config: Config, day: date) -> float:
    bump = 0.0
    for event in config.events:
        distance = (day - event.date).days
        if abs(distance) <= 14:
            bump += (event.lift_pct / 300.0) * math.exp(-((distance / 5.0) ** 2))
    return bump


def _jitter(day: date, salt: int) -> float:
    """日付から決まる再現可能な揺らぎ。乱数を使わないので毎回同じ結果になる。"""
    seed = (day.toordinal() * 1103515245 + salt * 12345) % 2147483647
    return 0.90 + 0.20 * (seed % 1000) / 999.0


def _demand_weight(config: Config, day: date, salt: int) -> float:
    return max(
        0.05,
        _season(day) * _weekday(day) * (1.0 + _event_bump(config, day)) * _jitter(day, salt),
    )


def _allocate(total: int, weights: list[float]) -> list[int]:
    """重みに比例して整数を時系列に配る。合計は必ず total に一致する。

    累積和を丸めて差を取る方式を使う。日次の期待値が1未満になる施設でも、
    「重みの大きい日から順に1件ずつ」ではなく期間内で均されるので、
    月単位・四半期単位で見たときの比率が崩れない。
    """
    if total <= 0 or not weights:
        return [0] * len(weights)
    scale = sum(weights)
    counts: list[int] = []
    cumulative = 0.0
    placed = 0
    for weight in weights:
        cumulative += total * weight / scale
        target = int(cumulative + 0.5)
        counts.append(target - placed)
        placed = target
    counts[-1] += total - placed
    return counts


def _allocate_capped(total: int, weights: list[float], cap: int) -> list[int]:
    """1日あたり cap（＝客室数）を超えないように配る。満室を超える販売はできない。"""
    counts = [0] * len(weights)
    remaining = total
    active = list(range(len(weights)))
    while remaining > 0 and active:
        share = _allocate(remaining, [weights[i] for i in active])
        moved = 0
        for slot, index in enumerate(active):
            room = cap - counts[index]
            take = min(room, share[slot])
            counts[index] += take
            moved += take
        remaining -= moved
        active = [i for i in active if counts[i] < cap]
        if moved == 0:
            break
    return counts


def _own_sold_rooms(
    config: Config, segments: list[tuple[date, date]]
) -> dict[date, int]:
    """自社の日次販売室数。期間ごとの合計を OWN_SEGMENT_SOLD に合わせる。"""
    sold: dict[date, int] = {}
    for (start, end), total in zip(segments, OWN_SEGMENT_SOLD):
        days = daterange(start, end)
        weights = [_demand_weight(config, day, salt=7) for day in days]
        sold.update(dict(zip(days, _allocate_capped(total, weights, config.own.rooms))))
    return sold


def _own_review_weights(
    days: list[date], sold: dict[date, int], origin: date
) -> list[float]:
    """レビューは販売室数に比例して発生する。ただし投稿率は一定ではない。

    ここで乗せている緩やかな波が、推定稼働と実稼働のズレの正体になる。
    実データでは季節や客層で投稿率が動くことに相当する。
    """
    return [
        sold.get(day, 0)
        * (
            1.0
            + ACCURACY_WOBBLE
            * math.sin(2 * math.pi * (day - origin).days / WOBBLE_PERIOD_DAYS)
        )
        for day in days
    ]


def _segments(config: Config, as_of: date) -> list[tuple[date, date]]:
    window = config.estimation.window_days
    cal_start = as_of - timedelta(days=config.estimation.calibration_days - 1)
    window_start = as_of - timedelta(days=window - 1)
    prev_start = window_start - timedelta(days=window)
    return [
        (cal_start, prev_start - timedelta(days=1)),
        (prev_start, window_start - timedelta(days=1)),
        (window_start, as_of),
    ]


def build_fixtures(config: Config, today: date) -> None:
    as_of = today - timedelta(days=config.estimation.review_lag_days)
    segments = _segments(config, as_of)
    cal_start, _ = segments[0]
    all_days = daterange(cal_start, as_of)

    sold = _own_sold_rooms(config, segments)
    _write_pms(config, all_days, [sold[day] for day in all_days])

    daily: dict[str, dict[date, int]] = {}
    for salt, prop in enumerate(config.properties):
        window_total, prev_total, pre_total = TARGET_REVIEWS[prop.key]
        counts: dict[date, int] = {}
        for (start, end), total in zip(segments, (pre_total, prev_total, window_total)):
            days = daterange(start, end)
            if prop.is_own:
                weights = _own_review_weights(days, sold, cal_start)
            else:
                weights = [_demand_weight(config, day, salt=salt + 1) for day in days]
            counts.update(dict(zip(days, _allocate(total, weights))))
        daily[prop.key] = counts

    _write_snapshots(config, daily, cal_start, today)


def _write_pms(config: Config, days: list[date], sold: list[int]) -> None:
    path = config.pms_actuals_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "rooms_sold"])
        for day, rooms in zip(days, sold):
            writer.writerow([day.isoformat(), rooms])


def _write_snapshots(
    config: Config,
    daily: dict[str, dict[date, int]],
    cal_start: date,
    today: date,
) -> None:
    """滞在日ベースの日次件数を投稿日に押し出し、累計として1日1点記録する。"""
    lag = config.estimation.review_lag_days
    first_capture = cal_start + timedelta(days=lag - 1)  # 最初の差分を取るための基準点
    captures = daterange(first_capture, today)

    path = config.database_path
    if path.exists():
        path.unlink()

    with Store(path) as store:
        for prop in config.properties:
            counts = daily[prop.key]
            cumulative = BASE_COUNTS[prop.key]
            for capture in captures:
                stay_day = capture - timedelta(days=lag)
                cumulative += counts.get(stay_day, 0)
                store.record_snapshot(
                    place_key=prop.key,
                    captured_on=capture,
                    rating=_rating(prop.key, capture, captures[0], captures[-1]),
                    user_rating_count=cumulative,
                    payload={"displayName": prop.name},
                )
                store.log_collection(prop.key, capture, "ok", "fixture")


def _rating(key: str, day: date, first: date, last: date) -> float:
    if key not in RATINGS:
        span = max(1, (last - first).days)
        progress = (day - first).days / span
        return round(OWN_RATING_FROM + (OWN_RATING_TO - OWN_RATING_FROM) * progress, 2)
    return RATINGS[key]
