"""フィクスチャ再生ソース — APIキー不要でパイプライン全体を通す.

実APIのレスポンスと同じ形のJSONを読み、**実コネクタと同じパーサ**へ通す。
パーサをバイパスしないことが重要で、これにより
「オフラインでは動くが本番のレスポンス形では壊れる」という事故を防ぐ。

用途:
  ・APIキー取得前の設計検証／デモ
  ・CIでの回帰テスト（ネットワークに出ない）
  ・本番キャッシュ（data/raw/）の再生による障害再現

フィクスチャの価格は擬似データであり実勢価格ではない。
survey レポートは fixture 由来のデータに対して必ず警告バナーを表示する。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .base import PlaceRecord, RateRecord
from .places import _to_record as parse_place
from .serpapi_hotels import _to_record as parse_rate


class FixturePlacesSource:
    name = "fixture_places"

    def __init__(self, fixture_dir: Path) -> None:
        self.dir = Path(fixture_dir)

    def geocode(self, query: str, run_date: date) -> PlaceRecord | None:
        path = self.dir / "places_geocode.json"
        if not path.exists():
            return None
        places = json.loads(path.read_text(encoding="utf-8")).get("places") or []
        return parse_place(places[0], self.name) if places else None

    def nearby_lodging(self, latitude: float, longitude: float, radius_m: int,
                       run_date: date) -> list[PlaceRecord]:
        path = self.dir / "places_nearby.json"
        if not path.exists():
            raise FileNotFoundError(
                f"フィクスチャが見つかりません: {path}\n"
                f"  python3 scripts/make_fixtures.py で生成してください。"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = [parse_place(p, self.name) for p in payload.get("places") or []]
        within = [r for r in records if r.distance_km(latitude, longitude) * 1000 <= radius_m]
        return sorted(within, key=lambda r: r.distance_km(latitude, longitude))


class FixtureRatesSource:
    name = "fixture_google_hotels"

    def __init__(self, fixture_dir: Path) -> None:
        self.dir = Path(fixture_dir) / "google_hotels"

    def rates_for_date(self, query: str, stay_date: date, run_date: date, *,
                       adults: int = 2, los: int = 1) -> list[RateRecord]:
        path = self.dir / f"{stay_date.isoformat()}.json"
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        out = []
        for prop in payload.get("properties") or []:
            record = parse_rate(prop, stay_date, adults, los, self.name)
            record.is_fixture = True
            out.append(record)
        return out

    search_area = rates_for_date
