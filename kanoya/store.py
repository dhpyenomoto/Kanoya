"""スナップショット蓄積庫。

Google Places API は施設のレビュー本文を最大 5 件しか返さない。過去 90 日の
レビュー時系列を API から直接取ることはできない。

そこで本システムは userRatingCount（累計レビュー件数）を毎日記録し、その
日次差分を「その日に増えたレビュー数」として扱う。ダッシュボードが読むのは
この蓄積された差分であって、API の応答そのものではない。したがって
稼働推定は poll を始めた日からしか遡れない。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


@dataclass(frozen=True)
class Snapshot:
    date: dt.date
    place_id: str
    user_rating_count: int
    rating: float

    def to_json(self) -> str:
        return json.dumps(
            {
                "date": self.date.isoformat(),
                "place_id": self.place_id,
                "user_rating_count": self.user_rating_count,
                "rating": round(self.rating, 3),
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, line: str) -> "Snapshot":
        raw = json.loads(line)
        return cls(
            date=dt.date.fromisoformat(raw["date"]),
            place_id=raw["place_id"],
            user_rating_count=int(raw["user_rating_count"]),
            rating=float(raw["rating"]),
        )


class SnapshotStore:
    """JSONL 追記式の蓄積庫。1 行 1 スナップショット。

    追記のみで書き換えないため、過去の観測が後から改変されることがない。
    同一 (date, place_id) が重複した場合は後勝ちで解決する。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, snapshots: Iterable[Snapshot]) -> int:
        rows = list(snapshots)
        if not rows:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for snap in rows:
                handle.write(snap.to_json() + "\n")
        return len(rows)

    def __iter__(self) -> Iterator[Snapshot]:
        if not self.path.exists():
            return iter(())
        with self.path.open(encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield Snapshot.from_json(line)
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    raise ValueError(f"{self.path}:{lineno} が壊れている: {exc}") from exc

    def series(self, place_id: str) -> dict[dt.date, Snapshot]:
        """1 施設分を日付順に整えて返す。重複は後勝ち。"""
        out: dict[dt.date, Snapshot] = {}
        for snap in self:
            if snap.place_id == place_id:
                out[snap.date] = snap
        return dict(sorted(out.items()))

    def latest(self, place_id: str) -> Snapshot | None:
        series = self.series(place_id)
        if not series:
            return None
        return series[max(series)]

    def covered_dates(self, place_id: str) -> tuple[dt.date, dt.date] | None:
        series = self.series(place_id)
        if not series:
            return None
        return min(series), max(series)
