"""Google Places API (New) の薄いクライアント。

取得するのは公式に返るフィールドだけ:
    id / displayName / rating / userRatingCount

宿泊料金は Places API では返らない。OTA 画面や Google ホテル検索画面の
スクレイピングは規約違反であり、このモジュールでは一切行わない。
実レートが必要な場合は正規ライセンスのレートショッパーを別系統で用意する。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

ENDPOINT = "https://places.googleapis.com/v1/places/{place_id}"
FIELD_MASK = "id,displayName,rating,userRatingCount"
API_KEY_ENV = "GOOGLE_PLACES_API_KEY"


class PlacesError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlaceFacts:
    place_id: str
    display_name: str
    rating: float | None
    user_rating_count: int


def _api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get(API_KEY_ENV)
    if not key:
        raise PlacesError(
            f"環境変数 {API_KEY_ENV} が設定されていません（--api-key でも指定できます）"
        )
    return key


def fetch_place(place_id: str, *, api_key: str | None = None, timeout: float = 15.0) -> PlaceFacts:
    if not place_id or place_id.startswith("REPLACE_WITH"):
        raise PlacesError(f"place_id が未設定です: {place_id!r}")

    req = urllib.request.Request(
        ENDPOINT.format(place_id=place_id) + "?languageCode=ja",
        headers={
            "X-Goog-Api-Key": _api_key(api_key),
            "X-Goog-FieldMask": FIELD_MASK,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - ネットワーク経路
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise PlacesError(f"Places API {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - ネットワーク経路
        raise PlacesError(f"Places API に到達できません: {exc.reason}") from exc

    return parse_place(payload, fallback_id=place_id)


def parse_place(payload: dict, *, fallback_id: str = "") -> PlaceFacts:
    name = payload.get("displayName") or {}
    rating = payload.get("rating")
    return PlaceFacts(
        place_id=payload.get("id") or fallback_id,
        display_name=name.get("text", "") if isinstance(name, dict) else str(name),
        rating=float(rating) if rating is not None else None,
        user_rating_count=int(payload.get("userRatingCount") or 0),
    )
