"""スナップショット保管庫（SQLite）。

Google Places API は「この施設の各日のレビュー件数」を返さない。返すのは
現時点の累計件数（userRatingCount）と、直近5件のレビュー本文だけである。
したがって日次の増分は「毎日取得して差分を取る」以外に得る方法がない。
このストアはその毎日の観測を貯める場所であり、システムの唯一の一次記録。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from .models import Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    place_key         TEXT    NOT NULL,
    captured_on       TEXT    NOT NULL,
    rating            REAL,
    user_rating_count INTEGER NOT NULL,
    payload           TEXT,
    PRIMARY KEY (place_key, captured_on)
);

CREATE TABLE IF NOT EXISTS reviews (
    place_key     TEXT NOT NULL,
    review_name   TEXT NOT NULL,
    publish_time  TEXT,
    rating        REAL,
    first_seen_on TEXT NOT NULL,
    PRIMARY KEY (place_key, review_name)
);

CREATE TABLE IF NOT EXISTS collection_log (
    place_key   TEXT NOT NULL,
    captured_on TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT,
    PRIMARY KEY (place_key, captured_on)
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------ 書き込み

    def record_snapshot(
        self,
        place_key: str,
        captured_on: date,
        rating: float | None,
        user_rating_count: int,
        payload: dict | None = None,
    ) -> None:
        """同日再取得は上書きする（1日1点に正規化する）。"""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO snapshots (place_key, captured_on, rating, user_rating_count, payload) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(place_key, captured_on) DO UPDATE SET "
                "rating=excluded.rating, user_rating_count=excluded.user_rating_count, "
                "payload=excluded.payload",
                (
                    place_key,
                    captured_on.isoformat(),
                    rating,
                    int(user_rating_count),
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                ),
            )

    def record_reviews(
        self, place_key: str, reviews: Iterable[dict], seen_on: date
    ) -> int:
        """直近5件のレビューを控える。投稿遅延の実測に使う補助データ。"""
        rows = [
            (
                place_key,
                r.get("name") or r.get("id") or "",
                r.get("publishTime"),
                r.get("rating"),
                seen_on.isoformat(),
            )
            for r in reviews
        ]
        rows = [r for r in rows if r[1]]
        if not rows:
            return 0
        with self._tx() as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO reviews "
                "(place_key, review_name, publish_time, rating, first_seen_on) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            return cur.rowcount

    def log_collection(
        self, place_key: str, captured_on: date, status: str, detail: str = ""
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO collection_log (place_key, captured_on, status, detail) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(place_key, captured_on) DO UPDATE SET "
                "status=excluded.status, detail=excluded.detail",
                (place_key, captured_on.isoformat(), status, detail),
            )

    # ------------------------------------------------------------------ 読み出し

    def snapshots(self, place_key: str) -> list[Snapshot]:
        rows = self._conn.execute(
            "SELECT place_key, captured_on, rating, user_rating_count FROM snapshots "
            "WHERE place_key = ? ORDER BY captured_on",
            (place_key,),
        ).fetchall()
        return [
            Snapshot(
                place_key=r["place_key"],
                captured_on=date.fromisoformat(r["captured_on"]),
                rating=r["rating"],
                user_rating_count=r["user_rating_count"],
            )
            for r in rows
        ]

    def latest_snapshot(self, place_key: str) -> Snapshot | None:
        snaps = self.snapshots(place_key)
        return snaps[-1] if snaps else None

    def latest_capture_date(self) -> date | None:
        row = self._conn.execute("SELECT MAX(captured_on) AS d FROM snapshots").fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    def observed_lags(self, place_key: str) -> list[int]:
        """publishTime と初回観測日の差（＝検知遅れ）。収集頻度の健全性チェック用。

        注意: これは「滞在→投稿」の遅延ではない。滞在からの遅延は Places からは
        観測できないため、calibrate.estimate_lag_days() で自社PMSと突き合わせる。
        """
        rows = self._conn.execute(
            "SELECT publish_time, first_seen_on FROM reviews "
            "WHERE place_key = ? AND publish_time IS NOT NULL",
            (place_key,),
        ).fetchall()
        lags = []
        for r in rows:
            try:
                published = date.fromisoformat(r["publish_time"][:10])
            except (TypeError, ValueError):
                continue
            lags.append((date.fromisoformat(r["first_seen_on"]) - published).days)
        return lags
