"""クチコミ件数スナップショットの保管庫。

Google Places API が返すのは「その時点の累計クチコミ件数」だけであり、
過去の日次推移は返さない。したがって日次の増分は、こちらで毎日
スナップショットを取り続けて差分を取る以外に得る方法がない。
このモジュールはその履歴を SQLite に貯める。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    property_key      TEXT NOT NULL,
    taken_on          TEXT NOT NULL,
    user_rating_count INTEGER NOT NULL,
    rating            REAL,
    place_id          TEXT,
    PRIMARY KEY (property_key, taken_on)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_key_date ON snapshots (property_key, taken_on);
"""


@dataclass(frozen=True)
class Snapshot:
    property_key: str
    taken_on: date
    user_rating_count: int
    rating: float | None = None
    place_id: str | None = None


class SnapshotStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SnapshotStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def put(self, snap: Snapshot) -> None:
        """同一日の再取得は上書き（最後に取れた値を採用）。"""
        self.conn.execute(
            "INSERT INTO snapshots (property_key, taken_on, user_rating_count, rating, place_id) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(property_key, taken_on) DO UPDATE SET "
            "user_rating_count=excluded.user_rating_count, "
            "rating=excluded.rating, place_id=excluded.place_id",
            (
                snap.property_key,
                snap.taken_on.isoformat(),
                int(snap.user_rating_count),
                snap.rating,
                snap.place_id,
            ),
        )
        self.conn.commit()

    def put_many(self, snaps: list[Snapshot]) -> None:
        for s in snaps:
            self.put(s)

    def history(
        self, property_key: str, start: date | None = None, end: date | None = None
    ) -> list[Snapshot]:
        sql = "SELECT * FROM snapshots WHERE property_key = ?"
        args: list = [property_key]
        if start:
            sql += " AND taken_on >= ?"
            args.append(start.isoformat())
        if end:
            sql += " AND taken_on <= ?"
            args.append(end.isoformat())
        sql += " ORDER BY taken_on"
        rows = self.conn.execute(sql, args).fetchall()
        return [
            Snapshot(
                property_key=r["property_key"],
                taken_on=date.fromisoformat(r["taken_on"]),
                user_rating_count=r["user_rating_count"],
                rating=r["rating"],
                place_id=r["place_id"],
            )
            for r in rows
        ]

    def latest(self, property_key: str) -> Snapshot | None:
        hist = self.conn.execute(
            "SELECT * FROM snapshots WHERE property_key = ? ORDER BY taken_on DESC LIMIT 1",
            (property_key,),
        ).fetchone()
        if hist is None:
            return None
        return Snapshot(
            property_key=hist["property_key"],
            taken_on=date.fromisoformat(hist["taken_on"]),
            user_rating_count=hist["user_rating_count"],
            rating=hist["rating"],
            place_id=hist["place_id"],
        )

    def clear(self, property_key: str | None = None) -> None:
        if property_key:
            self.conn.execute("DELETE FROM snapshots WHERE property_key = ?", (property_key,))
        else:
            self.conn.execute("DELETE FROM snapshots")
        self.conn.commit()
