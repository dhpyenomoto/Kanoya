"""Google Places API (New) クライアント。

取得するのは 1 施設あたり id / rating / userRatingCount の 3 項目だけ。
FieldMask を絞るのは課金階層を Essentials 側に留めるためでもある。

このクライアントは意図的に「レビュー本文」を取らない。Places API が返す
レビューは最大 5 件で、時系列の再構成には使えないうえ、本文を保存すると
利用規約上のキャッシュ制限に触れる。必要なのは件数の推移だけである。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request
from typing import Iterable

from .config import Property
from .store import Snapshot

ENDPOINT = "https://places.googleapis.com/v1/places/{place_id}"
FIELD_MASK = "id,rating,userRatingCount"


class PlacesError(RuntimeError):
    pass


class PlacesClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 15.0,
        max_retries: int = 4,
        sleep=time.sleep,
    ) -> None:
        self.api_key = api_key or os.environ.get("GOOGLE_MAPS_API_KEY", "")
        if not self.api_key:
            raise PlacesError(
                "API キーがない。環境変数 GOOGLE_MAPS_API_KEY を設定すること"
            )
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep

    def _get(self, place_id: str) -> dict:
        request = urllib.request.Request(
            ENDPOINT.format(place_id=place_id),
            headers={
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": FIELD_MASK,
                "Accept": "application/json",
            },
        )
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # 4xx は投げ直しても直らない。ただし 429 は待てば通る。
                if exc.code != 429 and 400 <= exc.code < 500:
                    body = exc.read().decode("utf-8", errors="replace")[:400]
                    raise PlacesError(f"{place_id}: HTTP {exc.code}: {body}") from exc
                last = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
            if attempt < self.max_retries - 1:
                self._sleep(2.0 ** (attempt + 1))
        raise PlacesError(f"{place_id}: {self.max_retries} 回試行して取得できず: {last}")

    def snapshot(self, prop: Property, on_date: dt.date) -> Snapshot:
        payload = self._get(prop.place_id)
        if "userRatingCount" not in payload:
            raise PlacesError(
                f"{prop.name}: userRatingCount が応答にない。place_id が施設を"
                "指しているか、FieldMask が有効か確認すること"
            )
        return Snapshot(
            date=on_date,
            place_id=prop.place_id,
            user_rating_count=int(payload["userRatingCount"]),
            rating=float(payload.get("rating", 0.0)),
        )

    def snapshot_all(
        self, properties: Iterable[Property], on_date: dt.date
    ) -> tuple[list[Snapshot], list[str]]:
        """取れたものを返し、取れなかったものは理由とともに返す。

        1 施設の失敗で全体を落とさない。欠測日は series 側で日数按分される。
        """
        ok: list[Snapshot] = []
        errors: list[str] = []
        for prop in properties:
            try:
                ok.append(self.snapshot(prop, on_date))
            except PlacesError as exc:
                errors.append(f"{prop.name}: {exc}")
        return ok, errors
