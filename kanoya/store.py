"""クチコミ件数スナップショットの永続化。

Places API は 1 施設あたり最大 5 件のレビューしか返さない。したがって
「過去 90 日に何件レビューが増えたか」を 1 回の呼び出しで得ることはできない。
毎日 userRatingCount を記録し、その差分を需要の代理指標として使う。
つまりこの JSONL がシステムの唯一の時系列データ源であり、
観測を始めた日より前には遡れない。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Snapshot:
    """ある日ある施設について観測したクチコミ総数と評価。"""

    date: date
    property_id: str
    place_id: str
    user_rating_count: int
    rating: float | None = None

    def to_json(self) -> dict:
        data = asdict(self)
        data["date"] = self.date.isoformat()
        return data

    @classmethod
    def from_json(cls, data: dict) -> Snapshot:
        return cls(
            date=date.fromisoformat(data["date"]),
            property_id=data["property_id"],
            place_id=data.get("place_id", ""),
            user_rating_count=int(data["user_rating_count"]),
            rating=None if data.get("rating") is None else float(data["rating"]),
        )


class SnapshotStore:
    """スナップショットの追記専用ストア（JSONL）。

    同じ (date, property_id) が複数回書かれた場合、後から書かれたものを採用する。
    観測をやり直しても壊れないようにするため、更新ではなく追記で扱う。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, snapshots: list[Snapshot]) -> None:
        if not snapshots:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for snap in snapshots:
                fh.write(json.dumps(snap.to_json(), ensure_ascii=False) + "\n")

    def load(self) -> list[Snapshot]:
        """全スナップショットを日付順で返す。重複は後勝ちで解決する。"""
        if not self.path.exists():
            return []

        latest: dict[tuple[date, str], Snapshot] = {}
        with self.path.open(encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    snap = Snapshot.from_json(json.loads(line))
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    raise ValueError(f"{self.path}:{line_no} が読めない: {exc}") from exc
                latest[(snap.date, snap.property_id)] = snap

        return sorted(latest.values(), key=lambda s: (s.date, s.property_id))

    def load_by_property(self) -> dict[str, list[Snapshot]]:
        """施設 ID をキーに、日付順のスナップショット列を返す。"""
        grouped: dict[str, list[Snapshot]] = {}
        for snap in self.load():
            grouped.setdefault(snap.property_id, []).append(snap)
        return grouped
