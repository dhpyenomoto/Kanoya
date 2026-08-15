"""Google Places API (New) クライアント。

取得するのは施設の公開情報のみ。
- displayName / rating / userRatingCount / reviews（直近5件）

取得しないもの、および取得できないもの:
- 宿泊料金。Places API は競合施設のレートを返さない。
- OTA・Googleホテル検索の画面。スクレイピングは規約違反であり実装しない。
  実レートが要る場合は正規ライセンスのレートショッパーを別系統で使う。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

ENDPOINT = "https://places.googleapis.com/v1/places/{place_id}"
FIELD_MASK = "id,displayName,rating,userRatingCount,reviews"


class PlacesError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlaceDetails:
    place_id: str
    display_name: str
    rating: float | None
    user_rating_count: int
    reviews: list[dict]
    raw: dict


class PlacesClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 15.0,
        retries: int = 3,
        language: str = "ja",
    ):
        self.api_key = api_key or os.environ.get("GOOGLE_PLACES_API_KEY", "")
        if not self.api_key:
            raise PlacesError(
                "GOOGLE_PLACES_API_KEY が未設定。収集ジョブは API キーなしでは動かない。"
            )
        self.timeout = timeout
        self.retries = retries
        self.language = language

    def fetch(self, place_id: str) -> PlaceDetails:
        url = ENDPOINT.format(place_id=place_id) + f"?languageCode={self.language}"
        request = urllib.request.Request(
            url,
            headers={
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": FIELD_MASK,
                "Accept": "application/json",
            },
        )
        payload = self._request(request, place_id)
        return PlaceDetails(
            place_id=payload.get("id", place_id),
            display_name=(payload.get("displayName") or {}).get("text", ""),
            rating=payload.get("rating"),
            user_rating_count=int(payload.get("userRatingCount") or 0),
            reviews=list(payload.get("reviews") or []),
            raw=payload,
        )

    def _request(self, request: urllib.request.Request, place_id: str) -> dict:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:400]
                # 4xx は再試行しても同じ。設定ミスなので即座に落とす。
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise PlacesError(f"{place_id}: HTTP {exc.code} {body}") from exc
                last = PlacesError(f"{place_id}: HTTP {exc.code} {body}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = PlacesError(f"{place_id}: {exc}")
            if attempt < self.retries - 1:
                time.sleep(2**attempt)
        raise last or PlacesError(f"{place_id}: 取得失敗")
