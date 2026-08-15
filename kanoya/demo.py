"""デモ用の合成台帳。

この手法は過去に遡れない——Places API は「今の累計レビュー件数」しか返さず、
昨日の値は昨日記録していなければ永久に手に入らない。
つまり本番では台帳が貯まるまでダッシュボードは動かない。
その間にレイアウトと判断ロジックを詰めるための、明示的に合成されたデータ。

生成される数字は実在の施設のものではない。
config.example.json の place_id が demo- で始まっている限り、
このモジュールの出力が使われていると考えてよい。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .store import Snapshot

DEMO_PREFIX = "demo-"

# 真の投稿率。demo が生成する世界の定数であり、
# calibrate.py はこれを知らないまま自社実績から推定し直す。
TRUE_POSTING_RATE = 0.105


@dataclass(frozen=True)
class Fixture:
    """1 施設ぶんの合成パラメータ。"""

    place_id: str
    base_occupancy: float   # 年間平均の稼働率
    trend: float            # 観測期間を通じた相対的な伸び（+0.10 = 10%増）
    base_reviews: int       # 台帳開始時点の累計レビュー件数
    rating: float


FIXTURES: dict[str, Fixture] = {
    "demo-kanoya": Fixture("demo-kanoya", 0.735, +0.10, 1180, 4.53),
    "demo-comp-a": Fixture("demo-comp-a", 0.795, -0.07, 640, 4.71),
    "demo-comp-b": Fixture("demo-comp-b", 0.745, +0.03, 910, 4.44),
    "demo-comp-c": Fixture("demo-comp-c", 0.575, +0.24, 205, 4.62),
    "demo-comp-d": Fixture("demo-comp-d", 0.765, -0.02, 3120, 4.21),
    "demo-comp-e": Fixture("demo-comp-e", 0.845, +0.01, 1460, 4.58),
}


def is_demo(config: Config) -> bool:
    return any(prop.place_id.startswith(DEMO_PREFIX) for prop in config.properties)


def _bump(doy: int, center: int, width: float) -> float:
    """年内の距離で測ったガウス山。年末年始をまたいでも切れないようにする。"""
    distance = min(abs(doy - center), 365 - abs(doy - center))
    return math.exp(-((distance / width) ** 2))


def _seasonal(day: date, config: Config) -> float:
    """奈良の需要季節性。桜と紅葉に山、梅雨と真冬に谷。"""
    doy = day.timetuple().tm_yday
    factor = 1.0
    factor += 0.20 * _bump(doy, 105, 26)   # 桜
    factor += 0.17 * _bump(doy, 320, 24)   # 紅葉・正倉院展
    factor -= 0.13 * _bump(doy, 172, 34)   # 梅雨
    factor -= 0.10 * _bump(doy, 20, 26)    # 厳冬
    for event in config.demand_events:
        if event.date <= day <= event.end:
            factor *= 1 + event.lift / 100 * 0.35
    # 週末は埋まる。
    if day.weekday() in (4, 5):
        factor *= 1.14
    return factor


def generate(
    config: Config,
    *,
    as_of: date,
    history_days: int = 320,
    seed: int = 20260815,
) -> tuple[list[Snapshot], list[tuple[date, int, int]]]:
    """スナップショット台帳と、自社 PMS 実績を生成する。

    レビューは滞在日に発生し、review_lag_days 後に投稿されるものとして
    累計件数に積む。つまり生成側は「遅延がある世界」を作り、
    推定側は estimation.review_lag_days でそれを引き戻そうとする。
    """
    rng = random.Random(seed)
    start = as_of - timedelta(days=history_days)
    lag = config.estimation.review_lag_days

    # 投稿日 -> その日に立つレビュー件数（施設別）
    posted: dict[str, dict[date, float]] = {
        prop.place_id: {} for prop in config.properties
    }
    actuals: list[tuple[date, int, int]] = []

    for prop in config.properties:
        fixture = FIXTURES.get(prop.place_id)
        if fixture is None:
            # config に demo 以外の施設が混ざっている場合は平凡な値で埋める。
            fixture = Fixture(prop.place_id, 0.72, 0.0, 500, 4.4)

        for step in range(history_days + 1):
            day = start + timedelta(days=step)
            progress = step / history_days
            occupancy = fixture.base_occupancy * _seasonal(day, config)
            occupancy *= 1 + fixture.trend * (progress - 0.5)
            occupancy *= 1 + rng.gauss(0, 0.07)
            occupancy = min(max(occupancy, 0.15), 1.0)

            # 客室数が少ないほど日次は粗い格子にしか乗らない。そこも再現する。
            rooms_sold = min(round(occupancy * prop.rooms), prop.rooms)
            if prop.is_own:
                actuals.append((day, rooms_sold, prop.rooms))

            guest_reviews = rooms_sold * TRUE_POSTING_RATE
            # レストラン併設施設は外来客のぶんだけレビューが水増しされる。
            raw = guest_reviews / (1 - prop.restaurant_review_share)
            post_day = day + timedelta(days=lag + rng.randint(-3, 3))
            posted[prop.place_id][post_day] = (
                posted[prop.place_id].get(post_day, 0.0) + raw
            )

    snapshots: list[Snapshot] = []
    for prop in config.properties:
        fixture = FIXTURES.get(prop.place_id)
        total = float(fixture.base_reviews if fixture else 500)
        emitted = 0
        for step in range(history_days + 1):
            day = start + timedelta(days=step)
            total += posted[prop.place_id].get(day, 0.0)
            count = int(total)
            if count < emitted:
                count = emitted
            emitted = count
            rating = (fixture.rating if fixture else 4.4) + rng.gauss(0, 0.004)
            snapshots.append(
                Snapshot(
                    observed_at=day,
                    place_id=prop.place_id,
                    user_rating_count=count,
                    rating=round(rating, 2),
                )
            )

    snapshots.sort(key=lambda s: (s.observed_at, s.place_id))
    actuals.sort(key=lambda row: row[0])
    return snapshots, actuals


def write_actuals(rows: list[tuple[date, int, int]], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["date,rooms_sold,rooms_available"]
    lines += [f"{day.isoformat()},{sold},{available}" for day, sold, available in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
