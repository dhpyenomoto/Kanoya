"""スナップショット台帳。

Places API は 1 施設あたり最大 5 件のレビュー本文しか返さない。
したがってレビュー「本文」を集めても速度は測れない。測れるのは
userRatingCount（累計レビュー件数）を毎日記録した差分だけ。
このモジュールはその台帳（JSONL）の読み書きを担う。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class Snapshot:
    observed_at: date
    place_id: str
    user_rating_count: int
    rating: float | None = None

    def to_json(self) -> str:
        payload = {
            "observed_at": self.observed_at.isoformat(),
            "place_id": self.place_id,
            "user_rating_count": self.user_rating_count,
        }
        if self.rating is not None:
            payload["rating"] = round(self.rating, 3)
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "Snapshot":
        raw = json.loads(line)
        rating = raw.get("rating")
        return cls(
            observed_at=date.fromisoformat(raw["observed_at"]),
            place_id=raw["place_id"],
            user_rating_count=int(raw["user_rating_count"]),
            rating=float(rating) if rating is not None else None,
        )


def append(path: str | Path, snapshots: list[Snapshot]) -> int:
    """台帳に追記する。既存行は書き換えない（append-only）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for snap in snapshots:
            handle.write(snap.to_json() + "\n")
    return len(snapshots)


def load(path: str | Path) -> list[Snapshot]:
    path = Path(path)
    if not path.exists():
        return []
    out: list[Snapshot] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(Snapshot.from_json(line))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise ValueError(f"{path}:{lineno} を読めない: {exc}") from exc
    return out


def series(snapshots: list[Snapshot], place_id: str) -> list[Snapshot]:
    """1 施設ぶんを日付順で返す。同日に複数あれば最後の観測を採用する。"""
    latest: dict[date, Snapshot] = {}
    for snap in snapshots:
        if snap.place_id == place_id:
            latest[snap.observed_at] = snap
    return [latest[day] for day in sorted(latest)]


def daily_reviews(
    snapshots: list[Snapshot],
    place_id: str,
    *,
    lag_days: int = 0,
) -> dict[date, float]:
    """観測日ごとのレビュー増分を、滞在日に引き戻して返す。

    観測に抜けがある区間は、その間で均等に発生したものとして按分する。
    累計件数が減った場合（Google 側の削除・統合）は 0 として扱う。
    引き戻しは lag_days ぶん単純にずらすだけで、分布は仮定しない。
    """
    ordered = series(snapshots, place_id)
    out: dict[date, float] = {}
    for prev, cur in zip(ordered, ordered[1:]):
        span = (cur.observed_at - prev.observed_at).days
        if span <= 0:
            continue
        delta = max(cur.user_rating_count - prev.user_rating_count, 0)
        if delta == 0:
            continue
        per_day = delta / span
        for step in range(span):
            observed = prev.observed_at + timedelta(days=step + 1)
            stay = observed - timedelta(days=lag_days)
            out[stay] = out.get(stay, 0.0) + per_day
    return out


def latest_rating(snapshots: list[Snapshot], place_id: str) -> float | None:
    for snap in reversed(series(snapshots, place_id)):
        if snap.rating is not None:
            return snap.rating
    return None


def latest_count(snapshots: list[Snapshot], place_id: str) -> int | None:
    ordered = series(snapshots, place_id)
    return ordered[-1].user_rating_count if ordered else None


def coverage(snapshots: list[Snapshot], place_id: str) -> tuple[date, date] | None:
    """観測が存在する期間。台帳が短ければ推定窓を張れない。"""
    ordered = series(snapshots, place_id)
    if len(ordered) < 2:
        return None
    return ordered[0].observed_at, ordered[-1].observed_at


def window_sum(daily: dict[date, float], start: date, end: date) -> float:
    """[start, end] 両端を含む合計。"""
    total = 0.0
    day = start
    while day <= end:
        total += daily.get(day, 0.0)
        day += timedelta(days=1)
    return total
