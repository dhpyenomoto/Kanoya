"""設定ファイルの読み込みとカレンダー解決."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

DOW = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


@dataclass
class Competitor:
    id: str
    name: str
    tier: str
    weight: float
    rooms: int
    distance_km: float
    pricing_basis: str
    meal_included: str
    dinner_uplift: float
    breakfast_uplift: float


@dataclass
class Settings:
    root: Path
    property: dict[str, Any]
    compset: dict[str, Any]
    calendar: dict[str, Any]
    competitors: dict[str, Competitor] = field(default_factory=dict)

    @classmethod
    def load(cls, config_dir: str | Path) -> "Settings":
        root = Path(config_dir)
        prop = _load_json(root / "property.json")
        compset = _load_json(root / "compset.json")
        calendar = _load_json(root / "calendar.json")
        competitors = {
            c["id"]: Competitor(
                id=c["id"],
                name=c["name"],
                tier=c["tier"],
                weight=float(c["weight"]),
                rooms=int(c["rooms"]),
                distance_km=float(c["distance_km"]),
                pricing_basis=c["pricing_basis"],
                meal_included=c["meal_included"],
                dinner_uplift=float(c.get("dinner_uplift", 0)),
                breakfast_uplift=float(c.get("breakfast_uplift", 0)),
            )
            for c in compset["competitors"]
        }
        return cls(root=root, property=prop, compset=compset, calendar=calendar,
                   competitors=competitors)

    # ---- カレンダー解決 -------------------------------------------------

    def season_of(self, day: date) -> tuple[str, str]:
        """該当日のシーズン区分とラベルを返す（年跨ぎのレンジにも対応）."""
        key = day.strftime("%m-%d")
        for entry in self.calendar["seasons"]:
            lo, hi = entry["from"], entry["to"]
            hit = lo <= key <= hi if lo <= hi else (key >= lo or key <= hi)
            if hit:
                return entry["season"], entry["label"]
        return self.calendar.get("default_season", "SHOULDER"), "標準期"

    def event_score_of(self, day: date) -> tuple[float, str]:
        """カレンダー登録済みイベントのスコア（最大値）とラベル."""
        best, label = 0.0, ""
        for entry in self.calendar.get("events", []):
            if parse_date(entry["from"]) <= day <= parse_date(entry["to"]):
                if float(entry["score"]) > best:
                    best, label = float(entry["score"]), entry["label"]
        return best, label

    def is_holiday(self, day: date) -> bool:
        return day.strftime("%Y-%m-%d") in set(self.calendar.get("holidays_2026", []))

    def is_holiday_eve(self, day: date) -> bool:
        """翌日が休日（祝日 or 土日）で、当日が平日扱いなら『連休前夜』."""
        nxt = day + timedelta(days=1)
        next_is_off = self.is_holiday(nxt) or nxt.weekday() >= 5
        today_is_weekday = day.weekday() <= 3 and not self.is_holiday(day)
        return next_is_off and today_is_weekday

    def dow_of(self, day: date) -> str:
        return DOW[day.weekday()]

    def day_class(self, day: date) -> str:
        """シーズン×曜日タイプの『日カテゴリ』。5室規模での統計プーリング単位."""
        season, _ = self.season_of(day)
        dow = self.dow_of(day)
        if dow in ("FRI", "SAT") or self.is_holiday_eve(day):
            bucket = "WEEKEND"
        elif dow == "SUN":
            bucket = "SUNDAY"
        else:
            bucket = "WEEKDAY"
        return f"{season}:{bucket}"
