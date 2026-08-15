"""SerpApi google_hotels コネクタ — 競合の実勢価格.

Google Hotels は施設 × チェックイン日の実勢価格と空室状況を保持しており、
これが競合レートの実質的な取得先になる。直接のパブリックAPIは提供されていないため、
SerpApi 経由で取得する。

2つの取得モードを持つ:
  search_area()   エリア一括検索。1リクエストで複数施設の価格が返るため単価が安い
  rates_for_property()  property_token 指定。OTA別の内訳（prices[]）まで取れる

エリア一括を主に使い、PRIMARY施設だけ個別取得でOTA別内訳を補う運用が
リクエスト数の面で最も効率が良い。
"""

from __future__ import annotations

from datetime import date, timedelta

from .base import RateRecord, parse_money
from .http import HttpClient, env_credential

ENDPOINT = "https://serpapi.com/search.json"


class SerpApiHotelsSource:
    name = "serpapi_google_hotels"

    def __init__(self, client: HttpClient, *, currency: str = "JPY",
                 gl: str = "jp", hl: str = "ja", api_key: str | None = None) -> None:
        self.client = client
        self.currency = currency
        self.gl = gl
        self.hl = hl
        self._api_key = api_key

    def _key(self) -> str:
        return self._api_key or env_credential("SERPAPI_API_KEY", "SERPAPI_KEY")

    def _params(self, query: str, stay_date: date, los: int, adults: int) -> dict:
        return {
            "engine": "google_hotels",
            "q": query,
            "check_in_date": stay_date.isoformat(),
            "check_out_date": (stay_date + timedelta(days=los)).isoformat(),
            "adults": adults,
            "currency": self.currency,
            "gl": self.gl,
            "hl": self.hl,
            "api_key": self._key(),
        }

    # ---- エリア一括検索 ---------------------------------------------

    def search_area(self, query: str, stay_date: date, run_date: date, *,
                    adults: int = 2, los: int = 1) -> list[RateRecord]:
        """1リクエストでエリア内の複数施設の価格を取得する（単価効率が最も良い）."""
        payload = self.client.request_json(
            ENDPOINT,
            run_date=run_date,
            params=self._params(query, stay_date, los, adults),
            cache_key_extra=f"area:{query}:{stay_date}:{los}:{adults}",
        )
        if payload.get("error"):
            raise RuntimeError(f"SerpApi: {payload['error']}")
        return [
            _to_record(p, stay_date, adults, los, self.name)
            for p in payload.get("properties") or []
        ]

    # RateSource プロトコル準拠
    def rates_for_date(self, query: str, stay_date: date, run_date: date, *,
                       adults: int = 2, los: int = 1) -> list[RateRecord]:
        return self.search_area(query, stay_date, run_date, adults=adults, los=los)

    # ---- 施設個別（OTA別内訳つき） -----------------------------------

    def rates_for_property(self, property_token: str, query: str, stay_date: date,
                           run_date: date, *, adults: int = 2, los: int = 1) -> list[RateRecord]:
        """property_token 指定で、OTA別の掲出価格（prices[]）まで取得する."""
        params = self._params(query, stay_date, los, adults)
        params["property_token"] = property_token
        payload = self.client.request_json(
            ENDPOINT,
            run_date=run_date,
            params=params,
            cache_key_extra=f"prop:{property_token}:{stay_date}:{los}:{adults}",
        )
        if payload.get("error"):
            raise RuntimeError(f"SerpApi: {payload['error']}")

        base = _to_record(payload, stay_date, adults, los, self.name)
        out: list[RateRecord] = []
        for offer in payload.get("prices") or []:
            rate = offer.get("rate_per_night") or {}
            total = offer.get("total_rate") or {}
            out.append(RateRecord(
                stay_date=stay_date,
                property_name=base.property_name,
                property_token=property_token,
                channel=offer.get("source", "") or "",
                raw_rate=parse_money(rate.get("extracted_lowest") or rate.get("lowest")),
                total_rate=parse_money(total.get("extracted_lowest") or total.get("lowest")),
                currency=self.currency,
                tax_included=False,   # rate_per_night.lowest は税サ別のことが多い
                available=bool(rate),
                adults=adults, los=los,
                rating=base.rating, review_count=base.review_count,
                hotel_class=base.hotel_class,
                latitude=base.latitude, longitude=base.longitude,
                source=self.name,
            ))
        return out or [base]


def _to_record(prop: dict, stay_date: date, adults: int, los: int,
               source: str) -> RateRecord:
    rate = prop.get("rate_per_night") or {}
    total = prop.get("total_rate") or {}
    gps = prop.get("gps_coordinates") or {}
    raw = parse_money(rate.get("extracted_lowest") or rate.get("lowest"))
    before_tax = parse_money(
        rate.get("extracted_before_taxes_fees") or rate.get("before_taxes_fees")
    )
    return RateRecord(
        stay_date=stay_date,
        property_name=prop.get("name", "") or "",
        property_token=prop.get("property_token", "") or "",
        channel="google_hotels_lowest",
        raw_rate=raw if raw is not None else before_tax,
        total_rate=parse_money(total.get("extracted_lowest") or total.get("lowest")),
        currency=prop.get("currency", "JPY") or "JPY",
        tax_included=raw is not None and before_tax is not None and raw > before_tax,
        available=raw is not None or before_tax is not None,
        adults=adults, los=los,
        rating=prop.get("overall_rating"),
        review_count=int(prop.get("reviews") or 0),
        hotel_class=prop.get("extracted_hotel_class"),
        latitude=float(gps.get("latitude") or 0.0),
        longitude=float(gps.get("longitude") or 0.0),
        source=source,
    )
