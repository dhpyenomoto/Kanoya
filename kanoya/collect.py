"""日次収集ジョブ。

毎日1回、全施設のスナップショットを1点ずつ取る。これを止めると増分が復元できず、
欠測期間は均等配分になって推移が鈍る。cron で回し、失敗は collection_log に残す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import Config
from .places import PlacesClient, PlacesError
from .store import Store


@dataclass
class CollectionResult:
    captured_on: date
    ok: list[str]
    failed: list[tuple[str, str]]

    @property
    def is_complete(self) -> bool:
        return not self.failed


def collect(
    config: Config,
    store: Store,
    client: PlacesClient,
    captured_on: date | None = None,
) -> CollectionResult:
    captured_on = captured_on or date.today()
    ok: list[str] = []
    failed: list[tuple[str, str]] = []

    for prop in config.properties:
        try:
            details = client.fetch(prop.place_id)
        except PlacesError as exc:
            store.log_collection(prop.key, captured_on, "error", str(exc))
            failed.append((prop.key, str(exc)))
            continue

        store.record_snapshot(
            place_key=prop.key,
            captured_on=captured_on,
            rating=details.rating,
            user_rating_count=details.user_rating_count,
            payload={"displayName": details.display_name},
        )
        store.record_reviews(prop.key, details.reviews, captured_on)
        store.log_collection(
            prop.key, captured_on, "ok", f"count={details.user_rating_count}"
        )
        ok.append(prop.key)

    return CollectionResult(captured_on=captured_on, ok=ok, failed=failed)
