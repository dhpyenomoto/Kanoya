"""Google Places API (New) クライアント。

取得するのは施設ごとの `rating` と `userRatingCount` だけ。
これは Places API が正規に返すフィールドである。

意図的に実装していないこと:
  - OTA 画面 / Google ホテル検索画面のスクレイピング（規約違反）
  - 競合の宿泊料金の取得（Places API は返さない。レートは別系統で取る）
"""

from __future__ import annotations

import os
from datetime import date
from typing import Protocol

import requests

from .config import Property
from .store import Snapshot

PLACES_ENDPOINT = "https://places.googleapis.com/v1/places/{place_id}"
FIELD_MASK = "id,displayName,rating,userRatingCount"
DEFAULT_TIMEOUT = 15.0


class PlacesError(RuntimeError):
    """Places API の呼び出しに失敗したときに送出する。"""


class PlacesSource(Protocol):
    """観測元のインターフェース。実 API とデモ用ソースを差し替えるために使う。"""

    def fetch(self, prop: Property, on: date) -> Snapshot: ...


class PlacesClient:
    """Places API から 1 施設ぶんのクチコミ総数と評価を取得する。"""

    def __init__(
        self,
        api_key: str | None = None,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key or os.environ.get("GOOGLE_PLACES_API_KEY", "")
        if not self.api_key:
            raise PlacesError(
                "API キーがない。環境変数 GOOGLE_PLACES_API_KEY を設定するか、"
                "デモデータで実行すること（kanoya demo）。"
            )
        self.session = session or requests.Session()
        self.timeout = timeout

    def fetch(self, prop: Property, on: date) -> Snapshot:
        url = PLACES_ENDPOINT.format(place_id=prop.place_id)
        headers = {
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        }

        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise PlacesError(f"{prop.name}: Places API に到達できない: {exc}") from exc

        if response.status_code != 200:
            raise PlacesError(
                f"{prop.name}: Places API が {response.status_code} を返した: "
                f"{response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PlacesError(f"{prop.name}: Places API の応答が JSON でない") from exc

        if "userRatingCount" not in payload:
            raise PlacesError(
                f"{prop.name}: 応答に userRatingCount がない。place_id を確認すること: "
                f"{prop.place_id}"
            )

        return Snapshot(
            date=on,
            property_id=prop.id,
            place_id=prop.place_id,
            user_rating_count=int(payload["userRatingCount"]),
            rating=None if payload.get("rating") is None else float(payload["rating"]),
        )


def poll(
    source: PlacesSource,
    properties: list[Property],
    on: date,
) -> tuple[list[Snapshot], list[str]]:
    """全施設を 1 回観測する。

    1 施設の失敗で観測全体を落とさない。取れたぶんを返し、失敗は文言として返す。
    欠測日は後段の :func:`kanoya.reviews.daily_new_reviews` が線形に按分する。
    """
    snapshots: list[Snapshot] = []
    errors: list[str] = []
    for prop in properties:
        try:
            snapshots.append(source.fetch(prop, on))
        except PlacesError as exc:
            errors.append(str(exc))
    return snapshots, errors
