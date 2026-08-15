"""検証データ生成 — 実APIと同じ形のレスポンスと、自社OTBを作る.

目的は「APIキーが無くてもパイプライン全体を通せること」であって、
実勢価格の再現ではない。価格は擬似データであり、survey レポートは
フィクスチャ由来のデータに対して必ず警告バナーを表示する。

施設名・所在は公開情報に基づく実在の施設だが、**価格は完全な擬似値**である。
実データで判断するには APIキーを設定して実接続で収集すること。

出力:
  data/fixtures/places_nearby.json     Places API (New) 形式
  data/fixtures/places_geocode.json    同上
  data/fixtures/google_hotels/*.json   SerpApi google_hotels 形式（宿泊日ごと）
  data/otb.csv                         自社OTBスナップショット

OTBは本番では PMS / サイトコントローラーから日次で取り込む。
検証用にはブッキングカーブに沿った擬似値を生成する（宿泊日ごとに需要係数を
1度だけ引くことで、取得日が宿泊日へ近づくにつれ単調増加する形にしている）。
"""

from __future__ import annotations

import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm.config import Settings  # noqa: E402

RUN_DATE = date(2026, 8, 15)
HORIZON = 121
BACKFILL = 14      # 基準日より前の実行日を再現できるよう、過去分の宿泊日も生成する
SEED = 20260815

# 公開情報に基づく近隣宿泊施設。座標は概算、価格アンカーは擬似値。
# rate_anchor は「1室2名・素泊まりまたは掲出上の最安」を想定した擬似値（税別）。
FACILITIES = [
    # name, lat, lon, priceLevel, rating, reviews, class, rate_anchor
    ("ふふ奈良",                   34.6790, 135.8395, "PRICE_LEVEL_VERY_EXPENSIVE", 4.6, 420,  5, 90000),
    ("古都の宿 むさし野",           34.6890, 135.8420, "PRICE_LEVEL_EXPENSIVE",      4.5, 260,  4, 66000),
    ("ANDO HOTEL 奈良若草山",       34.6883, 135.8480, "PRICE_LEVEL_EXPENSIVE",      4.4, 610,  4, 63000),
    ("江戸三",                     34.6800, 135.8420, "PRICE_LEVEL_EXPENSIVE",      4.5, 180,  4, 60000),
    ("NIPPONIA HOTEL 奈良ならまち", 34.6790, 135.8290, "PRICE_LEVEL_VERY_EXPENSIVE", 4.4, 210,  4, 48000),
    ("奈良ホテル",                 34.6790, 135.8318, "PRICE_LEVEL_VERY_EXPENSIVE", 4.4, 2400, 5, 42000),
    ("静観荘",                     34.6795, 135.8360, "PRICE_LEVEL_EXPENSIVE",      4.3, 150,  4, 44000),
    ("春日ホテル",                 34.6840, 135.8290, "PRICE_LEVEL_EXPENSIVE",      4.2, 520,  4, 40000),
    ("セトレ ならまち",             34.6770, 135.8280, "PRICE_LEVEL_EXPENSIVE",      4.3, 340,  4, 38000),
    ("紀寺の家",                   34.6740, 135.8290, "PRICE_LEVEL_EXPENSIVE",      4.6, 120,  4, 36000),
    ("奈良万葉若草の宿 三笠",       34.6960, 135.8500, "PRICE_LEVEL_MODERATE",       4.1, 780,  4, 36000),
    ("飛鳥荘",                     34.6800, 135.8350, "PRICE_LEVEL_MODERATE",       4.0, 690,  3, 34000),
    ("ホテルニューわかさ",          34.6830, 135.8300, "PRICE_LEVEL_MODERATE",       4.0, 830,  3, 30000),
    ("奈良倶楽部",                 34.6930, 135.8330, "PRICE_LEVEL_MODERATE",       4.4, 95,   3, 26000),
    ("月日亭",                     34.6872, 135.8545, "PRICE_LEVEL_VERY_EXPENSIVE", 4.5, 140,  5, 58000),
    ("コンフォートホテル奈良",      34.6790, 135.8230, "PRICE_LEVEL_INEXPENSIVE",    4.0, 1500, 3, 14000),
    ("東横INN近鉄奈良駅前",         34.6830, 135.8270, "PRICE_LEVEL_INEXPENSIVE",    3.9, 1900, 3, 12000),
    # 半径2.5kmでは入らないが5kmでは入る帯（半径拡大の効果を確認するための対照）
    ("スーパーホテルLohas JR奈良駅", 34.6820, 135.8195, "PRICE_LEVEL_INEXPENSIVE",   4.1, 2200, 3, 13000),
    ("ホテル日航奈良",              34.6795, 135.8180, "PRICE_LEVEL_MODERATE",       4.2, 2100, 4, 26000),
    ("奈良ロイヤルホテル",           34.6960, 135.7960, "PRICE_LEVEL_MODERATE",       4.1, 1400, 4, 24000),
    # 5km圏外。半径フィルタで落ちることの確認用
    ("奈良パークホテル",            34.7040, 135.7860, "PRICE_LEVEL_MODERATE",       3.9, 900,  3, 22000),
    # 以下は discovery のフィルタで落ちることを確認するための対照
    ("ゲストハウス奈良小町",        34.6800, 135.8250, "PRICE_LEVEL_INEXPENSIVE",    4.5, 210,  2, 7000),
    ("奈良ならまち カプセルイン",   34.6780, 135.8240, "PRICE_LEVEL_INEXPENSIVE",    3.8, 60,   1, 4500),
    ("春日野レビュー僅少ロッジ",    34.6850, 135.8450, "",                           4.8, 4,    0, 25000),
]


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = Settings.load(root / "config")
    data_dir = root / "data"
    fixtures = data_dir / "fixtures"
    (fixtures / "google_hotels").mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    # ---- Places API (New) 形式 ----
    places = {
        "places": [
            {
                "id": f"fixture_place_{i:03d}",
                "displayName": {"text": name, "languageCode": "ja"},
                "formattedAddress": f"日本、〒630-82XX 奈良県奈良市（{name} 所在地）",
                "location": {"latitude": lat, "longitude": lon},
                "types": ["lodging", "hotel", "point_of_interest"],
                "primaryType": "hotel",
                "rating": rating,
                "userRatingCount": reviews,
                **({"priceLevel": level} if level else {}),
            }
            for i, (name, lat, lon, level, rating, reviews, _cls, _anchor) in enumerate(FACILITIES)
        ]
    }
    _dump(fixtures / "places_nearby.json", places)

    prop = json.loads((root / "config" / "sources.json").read_text(encoding="utf-8"))["property"]
    _dump(fixtures / "places_geocode.json", {"places": [{
        "id": "fixture_place_self",
        "displayName": {"text": prop["name"], "languageCode": "ja"},
        "formattedAddress": prop["address"],
        "location": {"latitude": prop["latitude"], "longitude": prop["longitude"]},
        "types": ["lodging", "hotel"],
        "rating": 4.8, "userRatingCount": 60,
        "priceLevel": "PRICE_LEVEL_VERY_EXPENSIVE",
    }]})

    # ---- SerpApi google_hotels 形式（宿泊日ごと） ----
    season_level = {"PEAK": 1.45, "HIGH": 1.22, "SHOULDER": 1.00, "LOW": 0.86, "DEEP_LOW": 0.75}
    dow_level = {"MON": .88, "TUE": .86, "WED": .87, "THU": .93, "FRI": 1.12, "SAT": 1.28, "SUN": .95}

    for offset in range(-BACKFILL, HORIZON):
        stay = RUN_DATE + timedelta(days=offset)
        season, _ = settings.season_of(stay)
        event, _ = settings.event_score_of(stay)
        level = season_level[season] * dow_level[settings.dow_of(stay)] * (1 + 0.30 * event)

        properties = []
        for i, (name, lat, lon, _lvl, rating, reviews, hclass, anchor) in enumerate(FACILITIES):
            # 需要が強い日ほど売止が出る（＝レスポンスから消える）
            soldout_p = min(0.8, (0.55 * event + 0.35 * max(0.0, level - 1.05))
                            * max(0.0, 1 - max(0, offset) / 50))
            if rng.random() < soldout_p:
                continue
            nightly = round(anchor * level * rng.uniform(0.94, 1.07) / 100) * 100
            properties.append({
                "type": "hotel",
                "name": name,
                "property_token": f"fixture_token_{i:03d}",
                "gps_coordinates": {"latitude": lat, "longitude": lon},
                "check_in_time": "15:00", "check_out_time": "11:00",
                "rate_per_night": {
                    "lowest": f"¥{nightly:,}",
                    "extracted_lowest": nightly,
                    "before_taxes_fees": f"¥{round(nightly / 1.1):,}",
                    "extracted_before_taxes_fees": round(nightly / 1.1),
                },
                "total_rate": {
                    "lowest": f"¥{nightly:,}",
                    "extracted_lowest": nightly,
                },
                "overall_rating": rating,
                "reviews": reviews,
                "hotel_class": f"{hclass}-star hotel" if hclass else "",
                "extracted_hotel_class": hclass or None,
            })

        _dump(fixtures / "google_hotels" / f"{stay.isoformat()}.json", {
            "search_metadata": {"status": "Success", "_fixture": True},
            "search_parameters": {
                "engine": "google_hotels",
                "check_in_date": stay.isoformat(),
                "check_out_date": (stay + timedelta(days=1)).isoformat(),
                "adults": 2, "currency": "JPY", "gl": "jp", "hl": "ja",
            },
            "properties": properties,
        })

    # ---- 自社OTB（本番では PMS / サイトコントローラーから取り込む） ----
    otb_rows = _build_otb(settings, rng, season_level, dow_level)
    _write_csv(data_dir / "otb.csv", otb_rows)

    print(f"places_nearby.json      : {len(FACILITIES)} 施設")
    print(f"google_hotels/*.json    : {HORIZON + BACKFILL} 日分")
    print(f"otb.csv                 : {len(otb_rows)} 行"
          f"（取得日 {BACKFILL + 1} 日分 × 宿泊日）")
    print(f"出力先                  : {fixtures.parent}")
    print("\n※ 施設名・所在は公開情報ベースの実在施設。価格は擬似データであり実勢価格ではない。")


def _build_otb(settings, rng, season_level, dow_level) -> list[dict]:
    """OTBスナップショットを生成する.

    宿泊日ごとに需要係数を1度だけ引き、取得日はブッキングカーブの進捗のみで
    決める。こうすると取得日が宿泊日へ近づくにつれ OTB が単調増加し、
    実際の予約の積み上がり方と整合する（取得日ごとに乱数を引くと減ることがある）。
    """
    from kanoya_rm.pace import expected_ratio  # 遅延importで循環回避

    rooms = int(settings.property["property"]["rooms"])
    target_occ = {"PEAK": .95, "HIGH": .88, "SHOULDER": .75, "LOW": .62, "DEEP_LOW": .50}

    demand: dict[date, float] = {}
    for offset in range(-BACKFILL, HORIZON):
        stay = RUN_DATE + timedelta(days=offset)
        event, _ = settings.event_score_of(stay)
        demand[stay] = rng.uniform(0.55, 1.35) * (1 + 0.5 * event)

    rows: list[dict] = []
    for back in range(BACKFILL, -1, -1):
        snapshot = RUN_DATE - timedelta(days=back)
        for offset in range(0, HORIZON):
            stay = snapshot + timedelta(days=offset)
            if stay not in demand:
                continue
            season, _ = settings.season_of(stay)
            ratio = expected_ratio(settings, stay, offset)
            otb = max(0, min(rooms, round(
                rooms * target_occ[season] * ratio * demand[stay]
            )))

            # 現行の掲出価格＝手作業の「粗い料金表」（季節3区分 × 平日/週末の6階段）
            tier = ("PEAK" if season in ("PEAK", "HIGH")
                    else "LOW" if season in ("LOW", "DEEP_LOW") else "MID")
            weekend = (settings.dow_of(stay) in ("FRI", "SAT")
                       or settings.is_holiday_eve(stay))
            current = {
                ("PEAK", True): 132000, ("PEAK", False): 118000,
                ("MID", True): 98000,   ("MID", False): 88000,
                ("LOW", True): 82000,   ("LOW", False): 72000,
            }[(tier, weekend)]

            rows.append({
                "stay_date": stay.isoformat(),
                "snapshot_date": snapshot.isoformat(),
                "rooms_otb": otb,
                "room_revenue_otb": otb * current,
                "current_public_rate": current,
            })
    return rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
