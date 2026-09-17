"""設定ファイルの読み込みとカレンダー解決."""

from __future__ import annotations

import csv
import json
import sys
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


def benchmark_provenance_warning(calendar: dict[str, Any]) -> str | None:
    """ブッキングカーブの出所が不明なら警告文を返す（問題なければ None）.

    擬似データ由来の想定値と実測値が、設定ファイル上で区別できなかった。
    実際にこれで事故が起きている: 擬似データの想定カーブ（120日前に8%が
    入っている前提）を実運用の値と取り違えたまま動かしており、
    実測（0.5%）とかけ離れていたため進捗が常に「大幅な遅れ」と判定され、
    リード14日以遠のほぼ全日で価格を押し下げていた。
    値が妥当に見えるぶん、内部からは気づけない種類の誤りである。

    そのため出所（_source）の記載を必須とし、無ければ起動時に警告する。
    エラーにはしない。出所不明でも検証や試作は回せるべきで、
    ここで止めるとブートストラップができなくなる。
    """
    bench = calendar.get("pace_benchmark")
    if not isinstance(bench, dict):
        return "pace_benchmark がありません。予約進捗の評価ができません。"
    source = str(bench.get("_source") or "").strip()
    if not source:
        return (
            "pace_benchmark に _source がありません（出所不明のベンチマーク）。\n"
            "  擬似データ由来の想定値と実測値が区別できない状態です。\n"
            "  実測から復元した値であれば、期間・母数・除外条件を _source に書いてください。\n"
            "  例: \"実予約明細から復元（2026-02〜09）。火曜・水曜は閉館日のため除外。\"\n"
            "  出所不明のカーブは、進捗判定を系統的に狂わせたまま正常値に見えます。"
        )
    return None


def warn_if_benchmark_provenance_unknown(calendar: dict[str, Any], *,
                                         source: str = "") -> None:
    message = benchmark_provenance_warning(calendar)
    if message:
        where = f"（{source}）" if source else ""
        print(f"⚠️  出所不明のベンチマーク{where}\n  {message}", file=sys.stderr)


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
    place_id: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    needs_review: list[str] = field(default_factory=list)


@dataclass
class Settings:
    root: Path
    property: dict[str, Any]
    compset: dict[str, Any]
    calendar: dict[str, Any]
    competitors: dict[str, Competitor] = field(default_factory=dict)

    @classmethod
    def load(cls, config_dir: str | Path, *,
             compset_file: str | Path | None = None) -> "Settings":
        """設定を読み込む.

        compset_file を指定すると、既定の compset.json ではなく任意の
        コンペセット定義（discover_compset.py の生成物など）を使用する。
        """
        root = Path(config_dir)
        prop = _load_json(root / "property.json")
        compset_path = Path(compset_file) if compset_file else root / "compset.json"
        if not compset_path.is_absolute() and compset_file:
            compset_path = Path(compset_file)
        if compset_path.exists():
            compset = _load_json(compset_path)
        else:
            # コンペセットはまだ存在しないことがある（初回の発見前、
            # あるいはカレンダーだけを使う検証データ生成時）。
            # ここで落とすとブートストラップができなくなるため、空で続行する。
            compset = {"competitors": [], "tiers": {}, "_missing": str(compset_path)}
        calendar = _load_json(root / "calendar.json")
        warn_if_benchmark_provenance_unknown(calendar, source=str(root / "calendar.json"))
        competitors = {
            c["id"]: Competitor(
                id=c["id"],
                name=c["name"],
                tier=c["tier"],
                weight=float(c["weight"]),
                rooms=int(c.get("rooms") or 0),
                distance_km=float(c.get("distance_km") or 0.0),
                pricing_basis=c["pricing_basis"],
                meal_included=c["meal_included"],
                dinner_uplift=float(c.get("dinner_uplift", 0)),
                breakfast_uplift=float(c.get("breakfast_uplift", 0)),
                place_id=c.get("place_id", ""),
                latitude=float(c.get("latitude") or 0.0),
                longitude=float(c.get("longitude") or 0.0),
                needs_review=list(c.get("needs_review") or []),
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
