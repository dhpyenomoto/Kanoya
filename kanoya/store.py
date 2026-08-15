"""スナップショット（日次のクチコミ累計）と自社PMS実績の永続化。

Places API は個別レビューを最大5件しか返さないため、90日分の投稿タイムスタンプは
取得できない。そこで userRatingCount を毎日スナップショットし、その差分
（＝レビュー増分）を需要シグナルとして使う。この層はその台帳を扱う。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Snapshot:
    place_id: str
    date: dt.date
    user_rating_count: int
    rating: float | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "place_id": self.place_id,
                "date": self.date.isoformat(),
                "user_rating_count": self.user_rating_count,
                "rating": self.rating,
            },
            ensure_ascii=False,
        )


class SnapshotStore:
    """place_id -> {date: Snapshot} の読み出し専用ビュー。"""

    def __init__(self, by_place: dict[str, dict[dt.date, Snapshot]]):
        self._by_place = by_place

    @classmethod
    def load(cls, path: str | Path) -> "SnapshotStore":
        by_place: dict[str, dict[dt.date, Snapshot]] = {}
        p = Path(path)
        if not p.exists():
            return cls(by_place)
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                snap = Snapshot(
                    place_id=d["place_id"],
                    date=dt.date.fromisoformat(d["date"]),
                    user_rating_count=int(d["user_rating_count"]),
                    rating=None if d.get("rating") is None else float(d["rating"]),
                )
                # 同日に複数回取得した場合は後勝ち。
                by_place.setdefault(snap.place_id, {})[snap.date] = snap
        return cls(by_place)

    def place_ids(self) -> list[str]:
        return sorted(self._by_place)

    def counts(self, place_id: str) -> dict[dt.date, int]:
        return {d: s.user_rating_count for d, s in sorted(self._by_place.get(place_id, {}).items())}

    def latest_rating(self, place_id: str) -> float | None:
        snaps = self._by_place.get(place_id)
        if not snaps:
            return None
        for date in sorted(snaps, reverse=True):
            if snaps[date].rating is not None:
                return snaps[date].rating
        return None

    def latest_date(self) -> dt.date | None:
        dates = [d for snaps in self._by_place.values() for d in snaps]
        return max(dates) if dates else None


def append_snapshots(path: str | Path, snapshots: list[Snapshot]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for s in snapshots:
            fh.write(s.to_json() + "\n")


def write_snapshots(path: str | Path, snapshots: list[Snapshot]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for s in snapshots:
            fh.write(s.to_json() + "\n")


@dataclass(frozen=True)
class PmsDay:
    date: dt.date
    rooms_sold: int
    rooms_available: int


def load_pms(path: str | Path) -> dict[dt.date, PmsDay]:
    """自社PMSの日次実績（date,rooms_sold,rooms_available）。"""
    out: dict[dt.date, PmsDay] = {}
    p = Path(path)
    if not p.exists():
        return out
    with p.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            date = dt.date.fromisoformat(row["date"])
            out[date] = PmsDay(
                date=date,
                rooms_sold=int(row["rooms_sold"]),
                rooms_available=int(row["rooms_available"]),
            )
    return out


def load_area_places(path: str | Path) -> list[dict]:
    """エリア需要の母集団（nearby search の結果台帳）。"""
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def save_area_places(path: str | Path, places: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(places, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
