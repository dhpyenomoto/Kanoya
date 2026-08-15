"""デモ用の合成データ生成。

本番の履歴は毎日スナップショットを取って初めて貯まる（Places API は過去に
遡れない）。導入初日からダッシュボードの挙動を確認できるよう、同じ形の
スナップショット履歴と自社 PMS 実績を合成する。

生成物は data/snapshots.demo.db と data/pms_actuals.csv。
乱数は seed 固定なので、同じ as_of なら何度流しても同じ数字が出る。
"""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import Config, Property
from .store import Snapshot, SnapshotStore

HISTORY_DAYS = 660


@dataclass(frozen=True)
class Profile:
    base_occupancy: float
    trend_per_year: float  # 1年でこれだけ稼働が動く
    rating: float
    seed_reviews: int


PROFILES: dict[str, Profile] = {
    "kanoya": Profile(0.70, 0.05, 4.53, 210),
    "comp_a": Profile(0.73, -0.04, 4.71, 640),
    "comp_b": Profile(0.68, 0.01, 4.42, 980),
    "comp_c": Profile(0.58, 0.10, 4.66, 130),
    "comp_d": Profile(0.70, -0.02, 4.28, 3100),
    "comp_e": Profile(0.72, 0.01, 4.58, 1450),
}
DEFAULT_PROFILE = Profile(0.70, 0.0, 4.5, 500)

REVIEW_RATE = 0.105  # 実際の投稿率。推定側はこれを知らない。


def _event_lift(day: date, cfg: Config) -> float:
    lift = 0.0
    for ev in cfg.demand_events:
        gap = abs((day - ev.date).days)
        if gap <= 10:
            lift += (ev.lift_pct / 100.0) * math.exp(-((gap / 5.0) ** 2))
    return lift


def _occupancy(prop: Property, day: date, day_index: int, cfg: Config, rng: random.Random) -> float:
    pr = PROFILES.get(prop.key, DEFAULT_PROFILE)
    # 奈良は春（桜）と秋（正倉院展・紅葉）の二山。夏と冬が谷。
    seasonal = 0.09 * math.cos(2 * math.pi * (day.timetuple().tm_yday - 105) / 182.625)
    weekend = 0.12 if day.weekday() in (4, 5) else 0.0
    occ = (
        pr.base_occupancy
        + pr.trend_per_year * day_index / 365.0
        + seasonal
        + weekend
        + _event_lift(day, cfg)
        + rng.gauss(0, 0.06)
    )
    return min(1.0, max(0.0, occ))


@dataclass(frozen=True)
class Simulated:
    """1施設分の合成結果。sold が真の答え、snapshots だけが推定側から見える。"""

    key: str
    sold: dict[date, int]
    snapshots: list[Snapshot]


def simulate(cfg: Config, *, as_of: date, seed: int = 20260815) -> dict[str, Simulated]:
    start = as_of - timedelta(days=HISTORY_DAYS)
    out: dict[str, Simulated] = {}

    for prop in cfg.properties:
        rng = random.Random(f"{seed}:{prop.key}")
        pr = PROFILES.get(prop.key, DEFAULT_PROFILE)

        sold: dict[date, int] = {}
        posted: dict[date, int] = {}
        for i in range(HISTORY_DAYS + 1):
            day = start + timedelta(days=i)
            occ = _occupancy(prop, day, i, cfg, rng)
            rooms_sold = min(prop.rooms, max(0, round(prop.rooms * occ)))
            sold[day] = rooms_sold
            for _ in range(rooms_sold):
                if rng.random() >= REVIEW_RATE:
                    continue
                lag = max(1, round(rng.gauss(cfg.estimation.review_lag_days, 4)))
                on = day + timedelta(days=lag)
                posted[on] = posted.get(on, 0) + 1

        # 外来客レビュー（レストラン併設）を share に合わせて上乗せする
        share = prop.restaurant_review_share
        if share > 0:
            for day in list(posted):
                extra = posted[day] * share / (1 - share)
                whole = int(extra)
                if rng.random() < extra - whole:
                    whole += 1
                posted[day] += whole

        cumulative = pr.seed_reviews
        snaps: list[Snapshot] = []
        for i in range(HISTORY_DAYS + 1):
            day = start + timedelta(days=i)
            cumulative += posted.get(day, 0)
            if day > as_of:
                break
            snaps.append(
                Snapshot(
                    property_key=prop.key,
                    taken_on=day,
                    user_rating_count=cumulative,
                    rating=round(pr.rating + rng.gauss(0, 0.004), 2),
                    place_id=prop.place_id,
                )
            )
        out[prop.key] = Simulated(key=prop.key, sold=sold, snapshots=snaps)

    return out


def generate(
    cfg: Config,
    *,
    as_of: date | None = None,
    db_path: str | Path | None = None,
    actuals_path: str | Path | None = None,
    seed: int = 20260815,
) -> tuple[Path, Path]:
    as_of = as_of or date.today()
    db_path = Path(db_path or cfg.resolve("data/snapshots.demo.db"))
    actuals_path = Path(actuals_path or cfg.resolve(cfg.calibration.actuals_csv))
    if db_path.exists():
        db_path.unlink()

    sim = simulate(cfg, as_of=as_of, seed=seed)
    with SnapshotStore(db_path) as store:
        for s in sim.values():
            store.put_many(s.snapshots)

    actuals_path.parent.mkdir(parents=True, exist_ok=True)
    with actuals_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "rooms_sold", "rooms_available"])
        # 実績は締めのある期間だけ。直近 30 日は未確定として書き出さない。
        cutoff = as_of - timedelta(days=30)
        for day, rooms_sold in sorted(sim[cfg.own.key].sold.items()):
            if day <= cutoff:
                writer.writerow([day.isoformat(), rooms_sold, cfg.own.rooms])

    return db_path, actuals_path


def true_occupancy(cfg: Config, *, as_of: date, start: date, end: date, seed: int = 20260815):
    """合成データの真の稼働率。推定の当たり外れを測るためだけに使う。"""
    sim = simulate(cfg, as_of=as_of, seed=seed)
    days = (end - start).days + 1
    out: dict[str, float] = {}
    for prop in cfg.properties:
        sold = sum(v for d, v in sim[prop.key].sold.items() if start <= d <= end)
        out[prop.key] = sold / (prop.rooms * days) if days else 0.0
    return out

