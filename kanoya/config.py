"""設定ファイルの読み込みと検証。

config.json の構造をデータクラスに写す。値の妥当性はここで一度だけ検査し、
下流のモジュールは検証済みの前提で書く。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


class ConfigError(ValueError):
    """config.json の内容が不正なときに送出する。"""


@dataclass(frozen=True)
class Property:
    """コンプセットに含まれる 1 施設。"""

    id: str
    name: str
    place_id: str
    rooms: int
    is_own: bool = False
    #: レビューのうち外来（レストラン利用のみ）の推定比率。宿泊需要から控除する。
    restaurant_review_share: float = 0.0

    def stay_review_ratio(self) -> float:
        """レビュー総数のうち宿泊者由来とみなす比率。"""
        return 1.0 - self.restaurant_review_share


@dataclass(frozen=True)
class Estimation:
    #: 滞在日からレビュー投稿までの平均遅延日数。レビューをこの日数だけ過去に引き戻す。
    review_lag_days: int = 10
    #: 直近この日数はレビューが出揃っておらず推定が不安定なため、窓から除外する。
    unstable_tail_days: int = 10
    #: 推定稼働率の 95% 区間の半幅がこれ以内なら信頼度「高」（ポイント）。
    high_confidence_margin_pt: float = 8.0
    #: 同じく「中」。これを超えると「低」となり、価格判断には使わない。
    medium_confidence_margin_pt: float = 25.0
    occupancy_ceiling: float = 1.0


@dataclass(frozen=True)
class ReviewRateConfig:
    #: "calibrated" = 自社実績から実測、"fixed" = fallback_rate をそのまま使う。
    mode: str = "calibrated"
    fallback_rate: float = 0.10
    pms_actuals_path: str = "data/pms_actuals.csv"


@dataclass(frozen=True)
class PricingRules:
    #: この評価点を下回る間は BAR の引き上げを凍結する。
    rating_floor: float = 4.6
    #: 競合中央値との差がこの範囲内なら「同水準」とみなす（ポイント）。
    hold_band_pt: float = 3.0
    raise_gap_pt: float = 5.0
    cut_gap_pt: float = 5.0
    min_confidence_for_action: str = "中"


@dataclass(frozen=True)
class Event:
    date: date
    name: str
    lift: int = 0


@dataclass(frozen=True)
class Config:
    area_name: str
    window_days: int
    timezone: str
    properties: list[Property]
    estimation: Estimation = field(default_factory=Estimation)
    review_rate: ReviewRateConfig = field(default_factory=ReviewRateConfig)
    pricing_rules: PricingRules = field(default_factory=PricingRules)
    events: list[Event] = field(default_factory=list)

    @property
    def own(self) -> Property:
        return next(p for p in self.properties if p.is_own)

    @property
    def competitors(self) -> list[Property]:
        return [p for p in self.properties if not p.is_own]

    def by_id(self, property_id: str) -> Property:
        for p in self.properties:
            if p.id == property_id:
                return p
        raise KeyError(property_id)


def load_config(path: str | Path) -> Config:
    """config.json を読んで検証済みの Config を返す。"""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"設定ファイルが見つからない: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"設定ファイルの JSON が壊れている: {path}: {exc}") from exc

    properties = [_parse_property(item, i) for i, item in enumerate(raw.get("properties", []))]
    if not properties:
        raise ConfigError("properties が空。最低でも自社 1 件が必要。")

    own_count = sum(1 for p in properties if p.is_own)
    if own_count != 1:
        raise ConfigError(f"is_own の施設はちょうど 1 件必要（現在 {own_count} 件）。")

    ids = [p.id for p in properties]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"properties の id が重複している: {ids}")

    window_days = int(raw.get("window_days", 90))
    if window_days < 14:
        raise ConfigError("window_days は 14 日以上にすること。短い窓では推定が成立しない。")

    config = Config(
        area_name=raw.get("area_name", ""),
        window_days=window_days,
        timezone=raw.get("timezone", "Asia/Tokyo"),
        properties=properties,
        estimation=_parse_estimation(raw.get("estimation", {})),
        review_rate=_parse_review_rate(raw.get("review_rate", {})),
        pricing_rules=_parse_pricing_rules(raw.get("pricing_rules", {})),
        events=[_parse_event(item) for item in raw.get("events", [])],
    )
    return config


def _parse_property(item: dict, index: int) -> Property:
    for key in ("id", "name", "place_id", "rooms"):
        if key not in item:
            raise ConfigError(f"properties[{index}] に {key} がない。")

    rooms = int(item["rooms"])
    if rooms <= 0:
        raise ConfigError(f"properties[{index}] の rooms は 1 以上にすること。")

    share = float(item.get("restaurant_review_share", 0.0))
    if not 0.0 <= share < 1.0:
        raise ConfigError(
            f"properties[{index}] の restaurant_review_share は 0 以上 1 未満: {share}"
        )

    return Property(
        id=str(item["id"]),
        name=str(item["name"]),
        place_id=str(item["place_id"]),
        rooms=rooms,
        is_own=bool(item.get("is_own", False)),
        restaurant_review_share=share,
    )


def _parse_estimation(raw: dict) -> Estimation:
    est = Estimation(
        review_lag_days=int(raw.get("review_lag_days", 10)),
        unstable_tail_days=int(raw.get("unstable_tail_days", 10)),
        high_confidence_margin_pt=float(raw.get("high_confidence_margin_pt", 8.0)),
        medium_confidence_margin_pt=float(raw.get("medium_confidence_margin_pt", 25.0)),
        occupancy_ceiling=float(raw.get("occupancy_ceiling", 1.0)),
    )
    if est.review_lag_days < 0:
        raise ConfigError("review_lag_days は 0 以上。")
    if est.unstable_tail_days < 0:
        raise ConfigError("unstable_tail_days は 0 以上。")
    if est.high_confidence_margin_pt > est.medium_confidence_margin_pt:
        raise ConfigError(
            "high_confidence_margin_pt は medium_confidence_margin_pt 以下にすること。"
        )
    if not 0.0 < est.occupancy_ceiling <= 1.0:
        raise ConfigError("occupancy_ceiling は 0 より大きく 1 以下。")
    return est


def _parse_review_rate(raw: dict) -> ReviewRateConfig:
    cfg = ReviewRateConfig(
        mode=str(raw.get("mode", "calibrated")),
        fallback_rate=float(raw.get("fallback_rate", 0.10)),
        pms_actuals_path=str(raw.get("pms_actuals_path", "data/pms_actuals.csv")),
    )
    if cfg.mode not in ("calibrated", "fixed"):
        raise ConfigError(f"review_rate.mode は calibrated か fixed: {cfg.mode}")
    if not 0.0 < cfg.fallback_rate < 1.0:
        raise ConfigError("review_rate.fallback_rate は 0 と 1 の間。")
    return cfg


def _parse_pricing_rules(raw: dict) -> PricingRules:
    rules = PricingRules(
        rating_floor=float(raw.get("rating_floor", 4.6)),
        hold_band_pt=float(raw.get("hold_band_pt", 3.0)),
        raise_gap_pt=float(raw.get("raise_gap_pt", 5.0)),
        cut_gap_pt=float(raw.get("cut_gap_pt", 5.0)),
        min_confidence_for_action=str(raw.get("min_confidence_for_action", "中")),
    )
    if not 0.0 <= rules.rating_floor <= 5.0:
        raise ConfigError("rating_floor は 0〜5 の範囲。")
    if rules.hold_band_pt < 0:
        raise ConfigError("hold_band_pt は 0 以上。")
    return rules


def _parse_event(item: dict) -> Event:
    try:
        event_date = date.fromisoformat(str(item["date"]))
    except (KeyError, ValueError) as exc:
        raise ConfigError(f"events の date が不正: {item!r}") from exc
    return Event(date=event_date, name=str(item.get("name", "")), lift=int(item.get("lift", 0)))
