"""Google Places API (New) クライアント。

取りに行くのは 2 つだけ。

  * userRatingCount … 日次スナップショットの差分がレビュー増分になる
  * rating          … 品質ゲート（下限割れで値上げ凍結）

個別レビュー本文・投稿日時は取得しない。Places API は最大5件しか返さず、
90日分の投稿タイムスタンプは原理的に得られないため、累計の差分で代替する。
OTA画面やGoogleホテル検索画面のスクレイピングは規約違反なので行わない。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

BASE = "https://places.googleapis.com/v1"
NEARBY_FIELDS = "places.id,places.displayName,places.userRatingCount,places.rating,places.primaryType"
DETAIL_FIELDS = "id,displayName,rating,userRatingCount"


class PlacesError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlaceInfo:
    place_id: str
    name: str
    user_rating_count: int
    rating: float | None
    primary_type: str | None = None


class PlacesClient:
    def __init__(self, api_key: str | None = None, timeout: float = 20.0, retries: int = 4):
        self.api_key = api_key or os.environ.get("GOOGLE_MAPS_API_KEY", "")
        if not self.api_key:
            raise PlacesError(
                "GOOGLE_MAPS_API_KEY が未設定です。実データ取得には Places API のキーが必要です。"
            )
        self.timeout = timeout
        self.retries = retries

    def _request(self, url: str, field_mask: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method="POST" if data else "GET",
            headers={
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": field_mask,
                "Content-Type": "application/json",
            },
        )
        delay = 2.0
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:  # 4xx は再試行しても同じ
                if exc.code < 500 and exc.code != 429:
                    raise PlacesError(f"Places API {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
                last = exc
            except urllib.error.URLError as exc:
                last = exc
            if attempt < self.retries - 1:
                time.sleep(delay)
                delay *= 2
        raise PlacesError(f"Places API への接続に失敗しました: {last}")

    def details(self, place_id: str) -> PlaceInfo:
        d = self._request(f"{BASE}/places/{place_id}", DETAIL_FIELDS)
        return PlaceInfo(
            place_id=d.get("id", place_id),
            name=(d.get("displayName") or {}).get("text", place_id),
            user_rating_count=int(d.get("userRatingCount", 0)),
            rating=d.get("rating"),
        )

    def nearby(
        self,
        latitude: float,
        longitude: float,
        radius_m: int,
        included_types: tuple[str, ...],
        max_results: int = 20,
    ) -> list[PlaceInfo]:
        d = self._request(
            f"{BASE}/places:searchNearby",
            NEARBY_FIELDS,
            {
                "includedTypes": list(included_types),
                "maxResultCount": max_results,
                "locationRestriction": {
                    "circle": {
                        "center": {"latitude": latitude, "longitude": longitude},
                        "radius": float(radius_m),
                    }
                },
            },
        )
        return [
            PlaceInfo(
                place_id=p["id"],
                name=(p.get("displayName") or {}).get("text", p["id"]),
                user_rating_count=int(p.get("userRatingCount", 0)),
                rating=p.get("rating"),
                primary_type=p.get("primaryType"),
            )
            for p in d.get("places", [])
        ]
