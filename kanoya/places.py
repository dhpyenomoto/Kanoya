"""Google Places API (New) クライアント。

取得するのは userRatingCount と rating の 2 つだけ。
OTA 画面や Google ホテル検索画面のスクレイピングは規約違反であり、
このモジュールでは一切行わない。料金は Places API から取得できないため、
レート情報は正規ライセンスのレートショッパーで別途取得すること。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date

from .store import Snapshot

ENDPOINT = "https://places.googleapis.com/v1/places/{place_id}"
FIELD_MASK = "id,displayName,rating,userRatingCount"
TIMEOUT_SECONDS = 20


class PlacesError(RuntimeError):
    """API 呼び出しが失敗したとき。"""


@dataclass(frozen=True)
class PlaceFacts:
    place_id: str
    display_name: str
    rating: float | None
    user_rating_count: int


def api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if not key:
        raise PlacesError(
            "GOOGLE_PLACES_API_KEY が未設定。環境変数に設定するか --api-key で渡す"
        )
    return key


def fetch(place_id: str, *, key: str, language: str = "ja") -> PlaceFacts:
    """1 施設の現在値を取得する。"""
    url = ENDPOINT.format(place_id=place_id) + f"?languageCode={language}"
    request = urllib.request.Request(
        url,
        headers={
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": FIELD_MASK,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise PlacesError(f"{place_id}: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise PlacesError(f"{place_id}: 接続失敗 {exc.reason}") from exc

    count = payload.get("userRatingCount")
    if count is None:
        raise PlacesError(f"{place_id}: userRatingCount が返らなかった")

    name = payload.get("displayName", {}).get("text", place_id)
    rating = payload.get("rating")
    return PlaceFacts(
        place_id=payload.get("id", place_id),
        display_name=name,
        rating=float(rating) if rating is not None else None,
        user_rating_count=int(count),
    )


def snapshot_all(
    place_ids: list[str],
    *,
    key: str,
    observed_at: date,
    language: str = "ja",
) -> tuple[list[Snapshot], list[str]]:
    """全施設を取得して Snapshot に変換する。

    1 施設の失敗で全体を落とさない。失敗した施設はエラー文字列として返し、
    呼び出し側が「今日は N 施設欠測」と記録できるようにする。
    """
    snapshots: list[Snapshot] = []
    errors: list[str] = []
    for place_id in place_ids:
        try:
            facts = fetch(place_id, key=key, language=language)
        except PlacesError as exc:
            errors.append(str(exc))
            continue
        snapshots.append(
            Snapshot(
                observed_at=observed_at,
                place_id=place_id,
                user_rating_count=facts.user_rating_count,
                rating=facts.rating,
            )
        )
    return snapshots, errors
