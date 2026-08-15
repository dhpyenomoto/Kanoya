"""検証用サンプルデータ生成.

本番では以下に置き換わる:
  comp_rates.csv → レートショッパー（メトロエンジン / Lighthouse）または
                    Google Hotels API（SerpApi / DataForSEO）コネクタの出力
  otb.csv        → PMS / サイトコントローラーの予約明細から日次集計

ここでは決定論的な擬似データ（固定シード）を生成し、
エンジンの挙動と出力フォーマットを検証できる状態を作る。
"""

from __future__ import annotations

import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanoya_rm.config import Settings  # noqa: E402

SNAPSHOT = date(2026, 8, 15)
HORIZON = 120
SEED = 20260815


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = Settings.load(root / "config")
    rng = random.Random(SEED)
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)

    season_level = {"PEAK": 1.42, "HIGH": 1.20, "SHOULDER": 1.00, "LOW": 0.85, "DEEP_LOW": 0.74}
    dow_level = {"MON": .88, "TUE": .86, "WED": .87, "THU": .93, "FRI": 1.10, "SAT": 1.26, "SUN": .95}
    target_occ = {"PEAK": .95, "HIGH": .88, "SHOULDER": .75, "LOW": .62, "DEEP_LOW": .50}

    comp_rows = []
    otb_rows = []

    for offset in range(HORIZON):
        stay = SNAPSHOT + timedelta(days=offset)
        season, _ = settings.season_of(stay)
        dow = settings.dow_of(stay)
        event, _ = settings.event_score_of(stay)
        level = season_level[season] * dow_level[dow] * (1 + 0.35 * event)

        # --- 競合レート（各社の掲出ベースで生成し、正規化はエンジン側に委ねる） ---
        for comp in settings.competitors.values():
            anchor = {
                "cs01": 33000, "cs02": 30000, "cs03": 42000, "cs04": 38000,
                "cs05": 34000, "cs06": 62000, "cs07": 19000, "cs08": 41000,
            }[comp.id]
            noise = rng.uniform(0.93, 1.08)
            raw = anchor * level * noise
            # 直前になるほど売止が発生する
            soldout_p = min(0.85, (0.55 * event + 0.30 * (level - 1)) * max(0.0, 1 - offset / 45))
            available = 0 if rng.random() < soldout_p else 1
            comp_rows.append({
                "snapshot_date": SNAPSHOT.isoformat(),
                "stay_date": stay.isoformat(),
                "comp_id": comp.id,
                "source": "google_hotels" if comp.id in ("cs04", "cs05", "cs07") else "rate_shopper",
                "raw_rate": round(raw / 100) * 100,
                "available": available,
            })

        # --- 自社OTB（ペースカーブに沿わせつつ日ごとのブレを持たせる） ---
        lead = offset
        from kanoya_rm.pace import expected_ratio  # 遅延importで循環回避
        ratio = expected_ratio(settings, stay, lead)
        expected = 5 * target_occ[season] * ratio
        realised = expected * rng.uniform(0.45, 1.45) * (1 + 0.6 * event)
        otb = max(0, min(5, int(round(realised))))

        # --- 現行の掲出価格 = 手作業の「粗い料金表」（季節3区分 × 平日/週末の6階段） ---
        tier = "PEAK" if season in ("PEAK", "HIGH") else ("LOW" if season in ("LOW", "DEEP_LOW") else "MID")
        weekend = dow in ("FRI", "SAT") or settings.is_holiday_eve(stay)
        current = {
            ("PEAK", True): 132000, ("PEAK", False): 118000,
            ("MID", True): 98000,   ("MID", False): 88000,
            ("LOW", True): 82000,   ("LOW", False): 72000,
        }[(tier, weekend)]

        otb_rows.append({
            "stay_date": stay.isoformat(),
            "snapshot_date": SNAPSHOT.isoformat(),
            "rooms_otb": otb,
            "room_revenue_otb": round(otb * current / 1000) * 1000,
            "current_public_rate": current,
        })

    _write(data_dir / "comp_rates.csv", comp_rows)
    _write(data_dir / "otb.csv", otb_rows)
    print(f"comp_rates.csv: {len(comp_rows)} rows")
    print(f"otb.csv       : {len(otb_rows)} rows")


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
