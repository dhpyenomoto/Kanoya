"""デモ用の合成データ生成。

API キーなしでパイプライン全体を動かすためのもの。実在の施設のデータではない。

生成の向きは本番と逆になっている。まず「真の稼働率」を置き、そこから
レビューを発生させ、投稿遅延を与えて累計クチコミ数に積む。推定側が
この真の稼働率をどれだけ復元できるかで、手法そのものを検証できる。
"""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import Config, Property
from .store import Snapshot

#: 合成データで使う「真の」レビュー投稿率。推定側はこれを知らない。
TRUE_REVIEW_RATE = 0.105
#: 投稿遅延の平均日数と散らばり。
LAG_MEAN_DAYS = 10
LAG_SIGMA_DAYS = 5
#: 施設ごとの素性。base = 平年の稼働率、trend = 直近 90 日での相対変化。
_PROFILES: dict[str, tuple[float, float]] = {
    "own": (0.72, 0.07),
    "comp_a": (0.79, -0.06),
    "comp_b": (0.71, 0.02),
    "comp_c": (0.56, 0.20),
    "comp_d": (0.75, -0.02),
    "comp_e": (0.76, 0.01),
}
_DEFAULT_PROFILE = (0.70, 0.0)
#: 自社評価の推移。直近で下限を割る想定に置く。
OWN_RATING_START = 4.71
OWN_RATING_END = 4.53
_COMPETITOR_RATINGS = {
    "comp_a": 4.72,
    "comp_b": 4.64,
    "comp_c": 4.81,
    "comp_d": 4.35,
    "comp_e": 4.58,
}
#: 観測開始時点で各施設が既に持っているクチコミ数。
_BASE_REVIEW_COUNTS = {
    "own": 214,
    "comp_a": 388,
    "comp_b": 502,
    "comp_c": 96,
    "comp_d": 1840,
    "comp_e": 611,
}


@dataclass(frozen=True)
class DemoData:
    snapshots: list[Snapshot]
    own_actuals: dict[date, int]
    #: 検証用。推定側には渡らない真の日次販売室数。
    true_sold: dict[str, dict[date, int]]

    def true_occupancy(self, prop: Property, start: date, end: date) -> float | None:
        """指定期間の真の稼働率。推定と同じ窓で比べるために使う。"""
        sold = self.true_sold.get(prop.id)
        if not sold or end < start:
            return None
        days = (end - start).days + 1
        total = sum(
            count for day, count in sold.items() if start <= day <= end
        )
        return total / (prop.rooms * days)


def generate(
    config: Config,
    as_of: date,
    days: int = 280,
    seed: int = 20260815,
) -> DemoData:
    """観測開始から as_of までの合成スナップショットと自社実績を作る。"""
    rng = random.Random(seed)
    start = as_of - timedelta(days=days - 1)

    posted: dict[str, dict[date, int]] = {p.id: {} for p in config.properties}
    true_sold: dict[str, dict[date, int]] = {p.id: {} for p in config.properties}
    own_actuals: dict[date, int] = {}

    lifts = _event_lifts(config)

    for prop in config.properties:
        base, trend = _PROFILES.get(prop.id, _DEFAULT_PROFILE)

        for offset in range(days):
            day = start + timedelta(days=offset)
            occupancy = _true_occupancy(day, as_of, base, trend, lifts, rng)
            rooms_sold = _rooms_sold(occupancy, prop.rooms, rng)

            true_sold[prop.id][day] = rooms_sold
            if prop.is_own:
                own_actuals[day] = rooms_sold

            stay_reviews = sum(
                1 for _ in range(rooms_sold) if rng.random() < TRUE_REVIEW_RATE
            )
            total_reviews = stay_reviews + _restaurant_reviews(prop, stay_reviews, rng)

            for _ in range(total_reviews):
                post_day = day + timedelta(days=_sample_lag(rng))
                if post_day <= as_of:
                    posted[prop.id][post_day] = posted[prop.id].get(post_day, 0) + 1

    snapshots = _accumulate(config, posted, start, as_of)
    return DemoData(
        snapshots=snapshots, own_actuals=own_actuals, true_sold=true_sold
    )


def write(data: DemoData, snapshot_path: str | Path, actuals_path: str | Path) -> None:
    """生成したデモデータをストアと CSV に書き出す。"""
    from .store import SnapshotStore

    snapshot_path = Path(snapshot_path)
    if snapshot_path.exists():
        snapshot_path.unlink()
    SnapshotStore(snapshot_path).append(data.snapshots)

    actuals_path = Path(actuals_path)
    actuals_path.parent.mkdir(parents=True, exist_ok=True)
    with actuals_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "rooms_sold"])
        for day in sorted(data.own_actuals):
            writer.writerow([day.isoformat(), data.own_actuals[day]])


def _true_occupancy(
    day: date,
    as_of: date,
    base: float,
    trend: float,
    lifts: dict[date, float],
    rng: random.Random,
) -> float:
    """その日の「真の」稼働率。"""
    # 年周期の季節性。奈良は春と秋に山が来る。
    seasonal = 0.10 * math.sin(2 * math.pi * (day.timetuple().tm_yday - 60) / 365 * 2)
    # 週末の持ち上がり。
    weekend = 0.12 if day.weekday() in (4, 5) else 0.0
    # 直近 90 日でトレンドを効かせ、前期比に差を作る。
    days_out = (as_of - day).days
    ramp = max(0.0, 1.0 - days_out / 90) if days_out < 90 else 0.0
    momentum = base * trend * ramp

    occupancy = base + seasonal + weekend + momentum + lifts.get(day, 0.0)
    occupancy += rng.gauss(0, 0.08)
    return min(1.0, max(0.0, occupancy))


def _rooms_sold(occupancy: float, rooms: int, rng: random.Random) -> int:
    """稼働率を整数の販売室数に落とす。端数は確率的に丸める。"""
    exact = occupancy * rooms
    floor = int(exact)
    if rng.random() < exact - floor:
        floor += 1
    return min(rooms, floor)


def _restaurant_reviews(prop: Property, stay_reviews: int, rng: random.Random) -> int:
    """外来（レストラン利用のみ）レビューの発生数。"""
    share = prop.restaurant_review_share
    if share <= 0:
        return 0
    expected = stay_reviews * share / (1 - share)
    count = int(expected)
    if rng.random() < expected - count:
        count += 1
    return count


def _sample_lag(rng: random.Random) -> int:
    """投稿遅延日数。負にならないよう 0 で止める。"""
    return max(0, int(round(rng.gauss(LAG_MEAN_DAYS, LAG_SIGMA_DAYS))))


def _event_lifts(config: Config) -> dict[date, float]:
    """イベント前後の需要押し上げを日付ごとに展開する。"""
    lifts: dict[date, float] = {}
    for event in config.events:
        for offset in range(-3, 8):
            day = event.date + timedelta(days=offset)
            decay = 1.0 - abs(offset) / 12
            lifts[day] = lifts.get(day, 0.0) + event.lift / 100 * 0.5 * decay
    return lifts


def _accumulate(
    config: Config,
    posted: dict[str, dict[date, int]],
    start: date,
    as_of: date,
) -> list[Snapshot]:
    """日次の投稿数を累計クチコミ数のスナップショット列に変換する。"""
    snapshots: list[Snapshot] = []
    total_days = (as_of - start).days

    for prop in config.properties:
        running = _BASE_REVIEW_COUNTS.get(prop.id, 200)
        for offset in range((as_of - start).days + 1):
            day = start + timedelta(days=offset)
            running += posted[prop.id].get(day, 0)
            snapshots.append(
                Snapshot(
                    date=day,
                    property_id=prop.id,
                    place_id=prop.place_id,
                    user_rating_count=running,
                    rating=_rating(prop, offset, total_days),
                )
            )
    return snapshots


def _rating(prop: Property, offset: int, total_days: int) -> float:
    """観測日時点の評価点。自社は期間を通じて緩やかに下げる。"""
    if not prop.is_own:
        return _COMPETITOR_RATINGS.get(prop.id, 4.5)
    progress = offset / total_days if total_days else 1.0
    return round(OWN_RATING_START + (OWN_RATING_END - OWN_RATING_START) * progress, 2)
