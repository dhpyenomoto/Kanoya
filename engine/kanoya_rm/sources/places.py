"""Google Places API (New) コネクタ — 近隣宿泊施設の発見と評判データ.

【重要】Places API から実勢の宿泊料金は取得できない。
返るのは priceLevel（PRICE_LEVEL_INEXPENSIVE 〜 VERY_EXPENSIVE の4段階記号）のみで、
ADR判断には使えない。本コネクタの役割は次の2点に限られる。

  1. コンペティティブセットの機械的発見（半径N km の宿泊施設を全列挙）
  2. レビュー件数の月次増分 ＝ 宿泊需要の代理指標（"レビュー速度"）

実勢価格は serpapi_hotels / dataforseo_hotels（Google Hotels）から取得する。

API仕様上の制約と対処:
  ・searchNearby は 1リクエスト最大20件しか返さない
    → 中心 + 同心リング上の複数点へタイル分割して網羅する（_ring_centers）
  ・課金はフィールドマスクの階層（Essentials/Pro/Enterprise）で変わる
    → 必要最小限のマスクを config/sources.json で明示する
"""

from __future__ import annotations

import math
from datetime import date

from .base import PlaceRecord
from .http import HttpClient, env_credential

BASE = "https://places.googleapis.com/v1"

# rating / userRatingCount / priceLevel は Pro ティア。websiteUri は Enterprise ティア。
DEFAULT_FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.location,"
    "places.types,"
    "places.primaryType,"
    "places.rating,"
    "places.userRatingCount,"
    "places.priceLevel"
)

LODGING_TYPES = ["hotel", "resort_hotel", "inn", "japanese_inn", "bed_and_breakfast",
                 "guest_house", "lodging", "extended_stay_hotel"]


class PlacesSource:
    name = "google_places"

    def __init__(self, client: HttpClient, *, field_mask: str = DEFAULT_FIELD_MASK,
                 language: str = "ja", region: str = "jp",
                 included_types: list[str] | None = None,
                 api_key: str | None = None) -> None:
        self.client = client
        self.field_mask = field_mask
        self.language = language
        self.region = region
        self.included_types = included_types or LODGING_TYPES
        self._api_key = api_key

    # ---- 認証 -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        key = self._api_key or env_credential("GOOGLE_PLACES_API_KEY", "GOOGLE_MAPS_API_KEY")
        return {"X-Goog-Api-Key": key, "X-Goog-FieldMask": self.field_mask}

    # ---- ジオコーディング -------------------------------------------

    def geocode(self, query: str, run_date: date) -> PlaceRecord | None:
        """住所や施設名から座標を解決する（座標のハードコードを避けるため）."""
        payload = self.client.request_json(
            f"{BASE}/places:searchText",
            run_date=run_date,
            method="POST",
            headers=self._headers(),
            body={"textQuery": query, "languageCode": self.language,
                  "regionCode": self.region, "maxResultCount": 1},
            cache_key_extra=f"geocode:{query}",
        )
        places = payload.get("places") or []
        return _to_record(places[0], self.name) if places else None

    # ---- 近隣探索 ---------------------------------------------------

    def nearby_lodging(self, latitude: float, longitude: float, radius_m: int,
                       run_date: date) -> list[PlaceRecord]:
        """半径 radius_m 内の宿泊施設を、タイル分割で網羅的に列挙する."""
        found: dict[str, PlaceRecord] = {}
        for lat, lon, rad in self._ring_centers(latitude, longitude, radius_m):
            for place in self._search_nearby(lat, lon, rad, run_date):
                found.setdefault(place.place_id, place)
        return sorted(found.values(), key=lambda p: p.distance_km(latitude, longitude))

    def _search_nearby(self, lat: float, lon: float, radius_m: float,
                       run_date: date) -> list[PlaceRecord]:
        payload = self.client.request_json(
            f"{BASE}/places:searchNearby",
            run_date=run_date,
            method="POST",
            headers=self._headers(),
            body={
                "includedTypes": self.included_types,
                "maxResultCount": 20,          # API上限
                "rankPreference": "DISTANCE",
                "languageCode": self.language,
                "regionCode": self.region,
                "locationRestriction": {
                    "circle": {
                        "center": {"latitude": lat, "longitude": lon},
                        "radius": float(radius_m),
                    }
                },
            },
            cache_key_extra=f"nearby:{lat:.5f},{lon:.5f},{int(radius_m)}",
        )
        return [_to_record(p, self.name) for p in payload.get("places") or []]

    # 1リクエストあたりの探索円の半径。小さいほど「近い順20件で打ち切られる」
    # 取りこぼしが減るが、リクエスト数（＝費用）は増える。
    SUB_RADIUS_M = 900.0

    @classmethod
    def _ring_centers(cls, lat: float, lon: float,
                      radius_m: int) -> list[tuple[float, float, float]]:
        """中心＋同心リング上の探索点を返す.

        1リクエスト20件上限を回避するため、探索円を小円の集合で覆う。
        密集エリアで「近い順に20件で打ち切られ、遠方の重要な競合が落ちる」
        という取りこぼしを防ぐのが目的。

        リング上の点数は半径に応じて増やす。固定点数だと半径を広げたときに
        小円の間に隙間ができ、そこにある施設を丸ごと取りこぼす。
        """
        sub = cls.SUB_RADIUS_M
        centers: list[tuple[float, float, float]] = [(lat, lon, min(float(radius_m), sub))]
        if radius_m <= sub:
            return centers

        deg_lat = 1.0 / 110_574.0
        deg_lon = 1.0 / (111_320.0 * math.cos(math.radians(lat)) or 1.0)

        # 隣接する小円が重なるよう、間隔を sub*1.5 に抑える
        spacing = sub * 1.5
        ring_radius = sub * 1.4
        while ring_radius < radius_m + sub * 0.5:
            effective = min(ring_radius, float(radius_m))
            points = max(6, math.ceil(2 * math.pi * effective / spacing))
            for i in range(points):
                theta = 2 * math.pi * i / points
                centers.append((
                    lat + effective * math.cos(theta) * deg_lat,
                    lon + effective * math.sin(theta) * deg_lon,
                    sub,
                ))
            ring_radius += spacing

        return centers

    @classmethod
    def plan_requests(cls, radius_m: int) -> int:
        """探索に必要なリクエスト数（費用の事前提示に使う）."""
        return len(cls._ring_centers(0.0, 135.0, radius_m))


def _to_record(place: dict, source: str) -> PlaceRecord:
    loc = place.get("location") or {}
    display = place.get("displayName") or {}
    return PlaceRecord(
        place_id=place.get("id", ""),
        name=display.get("text") or place.get("name", ""),
        address=place.get("formattedAddress", ""),
        latitude=float(loc.get("latitude") or 0.0),
        longitude=float(loc.get("longitude") or 0.0),
        rating=place.get("rating"),
        review_count=int(place.get("userRatingCount") or 0),
        price_level=place.get("priceLevel", "") or "",
        types=list(place.get("types") or []),
        website=place.get("websiteUri", "") or "",
        source=source,
    )
