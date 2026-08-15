"""設定の読み込みと検証。

config.json はこのシステムの唯一の入力仕様であり、施設定義・推定パラメータ・
値付けルール・需要暦をすべて含む。数値は暗黙のデフォルトに頼らず明示する。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """config.json が不正なときに送出する。"""


@dataclass(frozen=True)
class Property:
    """コンプセット上の 1 施設。"""

    key: str
    name: str
    place_id: str
    rooms: int
    #: レビューのうち外来（レストラン・カフェ・宴会）由来と見なす比率。
    #: 宿泊レビューだけを残すため (1 - share) を掛けて控除する。
    restaurant_review_share: float = 0.0
    is_own: bool = False

    def lodging_reviews(self, raw: float) -> float:
        """外来レビューを控除した宿泊由来レビュー数。"""
        return raw * (1.0 - self.restaurant_review_share)


@dataclass(frozen=True)
class Estimation:
    window_days: int = 90
    #: 滞在日から投稿日までの平均遅延。この日数だけ投稿日を巻き戻して滞在日に直す。
    review_lag_days: int = 10
    #: 直近この日数の推定は遅延投稿が出揃わず不安定。基準日から切り落とす。
    unstable_tail_days: int = 10
    #: 較正窓の長さ（自社 PMS 実績と突合する日数）。
    calibration_days: int = 261
    #: バックテストの分割数。
    backtest_blocks: int = 6
    #: 推定稼働率の相対標準誤差が この値以下なら信頼度「高」/「中」。
    confidence_high_rse: float = 0.19
    confidence_medium_rse: float = 0.32

    def __post_init__(self) -> None:
        if self.window_days < 14:
            raise ConfigError("window_days は 14 日以上にすること（本手法は短期窓では意味を持たない）")
        if self.calibration_days < self.window_days:
            raise ConfigError("calibration_days は window_days 以上にすること")
        if self.backtest_blocks < 2:
            raise ConfigError("backtest_blocks は 2 以上にすること")


@dataclass(frozen=True)
class Rules:
    """値付け判断のルール。人が介入するのはこの逸脱時のみ。"""

    #: 自社と競合中央値の差がこの pt 未満なら「据え置き」。
    hold_band_pt: float = 8.0
    #: 評価がこの値を下回ったら BAR 引き上げを凍結する。
    rating_floor: float = 4.6
    #: 競合のモメンタムを注視シグナルに上げる閾値（相対%）。
    competitor_momentum_pct: float = 15.0
    #: 自社シェアの増減をシグナルに上げる閾値（相対%）。
    share_shift_pct: float = 15.0
    #: 直近 14 日のエリア需要が前 14 日比でこの%以上動いたらシグナル。
    compression_pct: float = 20.0
    #: 需要イベントをリードタイム対応のシグナルに上げる日数。
    event_lookahead_days: int = 45


@dataclass(frozen=True)
class DemandEvent:
    date: dt.date
    name: str
    #: 需要押し上げの想定幅（%）。表示と発注リードタイム判断に使う。
    lift_pct: int


@dataclass(frozen=True)
class Config:
    own: Property
    competitors: tuple[Property, ...]
    estimation: Estimation
    rules: Rules
    events: tuple[DemandEvent, ...]
    area_label: str = "奈良・春日エリア"
    group_label: str = "dhp都市開発グループ"

    @property
    def properties(self) -> tuple[Property, ...]:
        """自社を含むコンプセット全体。"""
        return (self.own,) + self.competitors

    def by_key(self, key: str) -> Property:
        for prop in self.properties:
            if prop.key == key:
                return prop
        raise KeyError(key)


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where} に必須項目 '{key}' がない")
    return mapping[key]


def _property(raw: dict[str, Any], *, is_own: bool) -> Property:
    where = "property" if is_own else "comp_set[]"
    share = float(raw.get("restaurant_review_share", 0.0))
    if not 0.0 <= share < 1.0:
        raise ConfigError(f"{where}.restaurant_review_share は 0 以上 1 未満: {share}")
    rooms = int(_require(raw, "rooms", where))
    if rooms < 1:
        raise ConfigError(f"{where}.rooms は 1 以上: {rooms}")
    return Property(
        key=str(_require(raw, "key", where)),
        name=str(_require(raw, "name", where)),
        place_id=str(_require(raw, "place_id", where)),
        rooms=rooms,
        restaurant_review_share=share,
        is_own=is_own,
    )


def load(path: str | Path) -> Config:
    """config.json を読み、検証済みの Config を返す。"""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"設定ファイルがない: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"設定ファイルが JSON として不正: {path}: {exc}") from exc

    own = _property(_require(raw, "property", "config"), is_own=True)
    comps = tuple(_property(c, is_own=False) for c in raw.get("comp_set", []))
    if not comps:
        raise ConfigError("comp_set が空。相対比較ができないため推定に意味がない")

    keys = [p.key for p in (own,) + comps]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise ConfigError(f"key が重複している: {sorted(dupes)}")

    estimation = Estimation(**raw.get("estimation", {}))
    rules = Rules(**raw.get("rules", {}))

    events = []
    for item in raw.get("demand_events", []):
        events.append(
            DemandEvent(
                date=dt.date.fromisoformat(str(_require(item, "date", "demand_events[]"))),
                name=str(_require(item, "name", "demand_events[]")),
                lift_pct=int(item.get("lift_pct", 0)),
            )
        )
    events.sort(key=lambda e: e.date)

    return Config(
        own=own,
        competitors=comps,
        estimation=estimation,
        rules=rules,
        events=tuple(events),
        area_label=str(raw.get("area_label", "奈良・春日エリア")),
        group_label=str(raw.get("group_label", "dhp都市開発グループ")),
    )
