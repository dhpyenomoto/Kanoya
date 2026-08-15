"""config.json の読み込みと検証。

設定はすべてこのモジュール経由で読む。推定ロジック側に定数を直書きしないこと
（値付けルールの閾値はレベニュー担当が触る場所であり、コードではない）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class PropertyConfig:
    """1施設分の設定。"""

    key: str
    name: str
    place_id: str
    rooms: int
    restaurant_review_share: float = 0.0
    is_own: bool = False
    note: str = ""

    @property
    def stay_review_share(self) -> float:
        """クチコミのうち宿泊客由来とみなす比率。"""
        return 1.0 - self.restaurant_review_share


@dataclass(frozen=True)
class ConfidenceRule:
    high_min_reviews: float
    medium_min_reviews: float

    def level(self, reviews: float) -> str:
        if reviews >= self.high_min_reviews:
            return "高"
        if reviews >= self.medium_min_reviews:
            return "中"
        return "低"


@dataclass(frozen=True)
class EstimationConfig:
    window_days: int
    ribbon_days: int
    review_lag_days: int
    calibration_days: int
    backtest_windows: int
    confidence: ConfidenceRule


@dataclass(frozen=True)
class PricingRules:
    rating_floor: float
    raise_gap_pt: float
    cut_gap_pt: float
    momentum_alert_pct: float
    share_drop_alert_pt: float
    event_lead_days: int
    change_highlight_pct: float
    backtest_level_max_pt: float
    backtest_trend_max_pt: float


@dataclass(frozen=True)
class DemandEvent:
    date: date
    name: str
    lift_pct: int


@dataclass(frozen=True)
class Config:
    root: Path
    area_label: str
    database: str
    pms_actuals_csv: str
    own: PropertyConfig
    compset: tuple[PropertyConfig, ...]
    estimation: EstimationConfig
    pricing: PricingRules
    events: tuple[DemandEvent, ...]

    @property
    def properties(self) -> tuple[PropertyConfig, ...]:
        """自社＋コンプセット。エリア需要の母数もこの集合で定義する。"""
        return (self.own, *self.compset)

    def property_by_key(self, key: str) -> PropertyConfig:
        for prop in self.properties:
            if prop.key == key:
                return prop
        raise KeyError(f"未登録の施設キー: {key}")

    @property
    def database_path(self) -> Path:
        return self._resolve(self.database)

    @property
    def pms_actuals_path(self) -> Path:
        return self._resolve(self.pms_actuals_csv)

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root / path


def _property(raw: dict, *, is_own: bool) -> PropertyConfig:
    rooms = int(raw["rooms"])
    if rooms <= 0:
        raise ValueError(f"{raw.get('key')}: rooms は 1 以上である必要がある")
    share = float(raw.get("restaurant_review_share", 0.0))
    if not 0.0 <= share < 1.0:
        raise ValueError(
            f"{raw.get('key')}: restaurant_review_share は 0 以上 1 未満（現在 {share}）"
        )
    return PropertyConfig(
        key=raw["key"],
        name=raw["name"],
        place_id=raw["place_id"],
        rooms=rooms,
        restaurant_review_share=share,
        is_own=is_own,
        note=raw.get("note", ""),
    )


def load_config(path: str | Path = "config.json") -> Config:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    est = raw["estimation"]
    rules = raw["pricing_rules"]

    config = Config(
        root=path.resolve().parent,
        area_label=raw.get("area_label", ""),
        database=raw.get("database", "data/kanoya.sqlite3"),
        pms_actuals_csv=raw.get("pms_actuals_csv", "data/pms_actuals.csv"),
        own=_property(raw["own"], is_own=True),
        compset=tuple(_property(c, is_own=False) for c in raw["compset"]),
        estimation=EstimationConfig(
            window_days=int(est["window_days"]),
            ribbon_days=int(est["ribbon_days"]),
            review_lag_days=int(est["review_lag_days"]),
            calibration_days=int(est["calibration_days"]),
            backtest_windows=int(est["backtest_windows"]),
            confidence=ConfidenceRule(
                high_min_reviews=float(est["confidence"]["high_min_reviews"]),
                medium_min_reviews=float(est["confidence"]["medium_min_reviews"]),
            ),
        ),
        pricing=PricingRules(
            rating_floor=float(rules["rating_floor"]),
            raise_gap_pt=float(rules["raise_gap_pt"]),
            cut_gap_pt=float(rules["cut_gap_pt"]),
            momentum_alert_pct=float(rules["momentum_alert_pct"]),
            share_drop_alert_pt=float(rules["share_drop_alert_pt"]),
            event_lead_days=int(rules["event_lead_days"]),
            change_highlight_pct=float(rules["change_highlight_pct"]),
            backtest_level_max_pt=float(rules["backtest_level_max_pt"]),
            backtest_trend_max_pt=float(rules["backtest_trend_max_pt"]),
        ),
        events=tuple(
            DemandEvent(
                date=date.fromisoformat(e["date"]),
                name=e["name"],
                lift_pct=int(e.get("lift_pct", 0)),
            )
            for e in sorted(raw.get("demand_events", []), key=lambda e: e["date"])
        ),
    )

    keys = [p.key for p in config.properties]
    if len(keys) != len(set(keys)):
        raise ValueError("施設キーが重複している")
    if config.estimation.window_days < 30:
        raise ValueError("window_days が短すぎる。本手法は90日窓を前提にしている")
    return config
