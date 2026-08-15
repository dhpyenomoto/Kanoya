"""DataForSEO Business Data コネクタ — 競合の実勢価格（代替経路）.

SerpApi と同じ Google Hotels のデータに別経路でアクセスする。
単一ベンダー依存を避けるためのセカンダリであり、
①SerpApi 障害時のフェイルオーバー、②月次のクロスチェック（データ品質監査）
に用いる。両方を常時使う必要はない。
"""

from __future__ import annotations

import base64
from datetime import date, timedelta

from .base import RateRecord, parse_money
from .http import HttpClient, env_credential

ENDPOINT = "https://api.dataforseo.com/v3/business_data/google/hotel_searches/live/advanced"


class DataForSeoHotelsSource:
    name = "dataforseo_google_hotels"

    def __init__(self, client: HttpClient, *, location_name: str = "Nara,Nara Prefecture,Japan",
                 language_code: str = "ja", currency: str = "JPY",
                 credentials: tuple[str, str] | None = None) -> None:
        self.client = client
        self.location_name = location_name
        self.language_code = language_code
        self.currency = currency
        self._credentials = credentials

    def _auth_header(self) -> dict[str, str]:
        if self._credentials:
            login, password = self._credentials
        else:
            login = env_credential("DATAFORSEO_LOGIN")
            password = env_credential("DATAFORSEO_PASSWORD")
        token = base64.b64encode(f"{login}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def rates_for_date(self, query: str, stay_date: date, run_date: date, *,
                       adults: int = 2, los: int = 1) -> list[RateRecord]:
        payload = self.client.request_json(
            ENDPOINT,
            run_date=run_date,
            method="POST",
            headers=self._auth_header(),
            body=[{
                "keyword": query,
                "location_name": self.location_name,
                "language_code": self.language_code,
                "check_in": stay_date.isoformat(),
                "check_out": (stay_date + timedelta(days=los)).isoformat(),
                "adults": adults,
                "currency": self.currency,
            }],
            cache_key_extra=f"dfs:{query}:{stay_date}:{los}:{adults}",
        )
        return list(_parse(payload, stay_date, adults, los, self.name))


def _parse(payload: dict, stay_date: date, adults: int, los: int, source: str):
    for task in payload.get("tasks") or []:
        if task.get("status_code") not in (20000, None):
            raise RuntimeError(f"DataForSEO: {task.get('status_message')}")
        for result in task.get("result") or []:
            for item in result.get("items") or []:
                price = item.get("price") or {}
                yield RateRecord(
                    stay_date=stay_date,
                    property_name=item.get("title", "") or "",
                    property_token=item.get("hotel_identifier", "") or "",
                    channel=item.get("domain", "") or "dataforseo",
                    raw_rate=parse_money(price.get("current") or item.get("price_current")),
                    currency=price.get("currency", "JPY") or "JPY",
                    available=bool(price.get("current") or item.get("price_current")),
                    adults=adults, los=los,
                    rating=(item.get("rating") or {}).get("value"),
                    review_count=int((item.get("rating") or {}).get("votes_count") or 0),
                    hotel_class=item.get("hotel_class"),
                    latitude=float(item.get("latitude") or 0.0),
                    longitude=float(item.get("longitude") or 0.0),
                    source=source,
                )
