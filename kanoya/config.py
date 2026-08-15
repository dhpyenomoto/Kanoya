"""設定の読み込みと検証。

config.json は「この宿の事実」だけを持つ。推定ロジックのパラメータは
estimation / decision に集約し、コードにハードコードしない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


class ConfigError(ValueError):
    """設定ファイルが不正なとき。"""


@dataclass(frozen=True)
class Property:
    """1施設。自社も競合も同じ型で扱う。"""

    label: str
    place_id: str
    rooms: int
    # レストラン・オーベルジュ併設施設は外来客のレビューが混ざる。
    # そのぶんを控除する比率（0.0〜1.0）。推定値であり感度分析が必要。
    restaurant_review_share: float = 0.0
    is_own: bool = False

    def __post_init__(self) -> None:
        if self.rooms <= 0:
            raise ConfigError(f"{self.label}: rooms は 1 以上である必要がある")
        if not 0.0 <= self.restaurant_review_share < 1.0:
            raise ConfigError(
                f"{self.label}: restaurant_review_share は 0.0 以上 1.0 未満"
            )
        if not self.place_id:
            raise ConfigError(f"{self.label}: place_id が空")


@dataclass(frozen=True)
class Estimation:
    window_days: int = 90
    # 滞在から投稿までの平均遅延。レビュー増分をこの日数だけ過去に引き戻す。
    review_lag_days: int = 10
    # 引き戻しの結果、直近この日数は母数が埋まりきらないので推定に使わない。
    unstable_tail_days: int = 10
    # 自社実績でキャリブレーションできないときのフォールバック投稿率。
    posting_rate_fallback: float = 0.10
    # 相対標準誤差がこの値未満なら信頼度「高」/「中」。
    confidence_high_rse: float = 0.15
    confidence_mid_rse: float = 0.25

    def __post_init__(self) -> None:
        if self.window_days < 14:
            raise ConfigError("window_days は 14 日以上（小規模施設では日次は無意味）")
        if not 0.0 < self.posting_rate_fallback < 1.0:
            raise ConfigError("posting_rate_fallback は 0.0〜1.0")


@dataclass(frozen=True)
class Decision:
    """値付け判断のしきい値。すべてポイント（百分率の差）。"""

    raise_threshold_pt: float = 6.0
    lower_threshold_pt: float = 6.0
    # この評価を下回るあいだは BAR 引き上げを凍結する。
    rating_floor: float = 4.6
    # シェア・オブ・ボイスがこのポイント数下がったら警告。
    share_drop_alert_pt: float = 1.5
    # 需要イベントがこの日数以内に迫っていたら通知。
    event_horizon_days: int = 45


@dataclass(frozen=True)
class DemandEvent:
    date: date
    label: str
    # 需要押し上げの想定（%）。判断には使わず、暦の注記として表示する。
    lift: int = 0
    days: int = 7

    @property
    def end(self) -> date:
        from datetime import timedelta

        return self.date + timedelta(days=max(self.days - 1, 0))


@dataclass(frozen=True)
class Paths:
    snapshots: Path = Path("data/snapshots.jsonl")
    pms_actuals: Path = Path("data/pms_actuals.csv")
    output: Path = Path("dist/index.html")


@dataclass(frozen=True)
class Config:
    own: Property
    compset: list[Property]
    estimation: Estimation = field(default_factory=Estimation)
    decision: Decision = field(default_factory=Decision)
    demand_events: list[DemandEvent] = field(default_factory=list)
    paths: Paths = field(default_factory=Paths)
    area_label: str = "奈良公園・春日エリア"

    @property
    def properties(self) -> list[Property]:
        """自社を先頭にした全施設。"""
        return [self.own, *self.compset]

    def by_place_id(self, place_id: str) -> Property | None:
        for prop in self.properties:
            if prop.place_id == place_id:
                return prop
        return None


def _property(raw: dict, *, is_own: bool) -> Property:
    try:
        return Property(
            label=raw["label"],
            place_id=raw["place_id"],
            rooms=int(raw["rooms"]),
            restaurant_review_share=float(raw.get("restaurant_review_share", 0.0)),
            is_own=is_own,
        )
    except KeyError as exc:
        raise ConfigError(f"施設定義に {exc} がない: {raw!r}") from exc


def load(path: str | Path) -> Config:
    """config.json を読む。"""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"設定ファイルが見つからない: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return from_dict(raw, base_dir=path.parent)


def from_dict(raw: dict, *, base_dir: Path | None = None) -> Config:
    base_dir = base_dir or Path(".")

    if "property" not in raw:
        raise ConfigError("config に property（自社施設）がない")
    own = _property(raw["property"], is_own=True)

    compset = [_property(item, is_own=False) for item in raw.get("compset", [])]
    if not compset:
        raise ConfigError("compset が空。比較対象がないと相対判断ができない")

    seen: set[str] = set()
    for prop in [own, *compset]:
        if prop.place_id in seen:
            raise ConfigError(f"place_id が重複している: {prop.place_id}")
        seen.add(prop.place_id)

    est_raw = dict(raw.get("estimation", {}))
    estimation = Estimation(**est_raw)

    dec_raw = dict(raw.get("decision", {}))
    # rating_floor は property 側に書かれることが多いので拾ってやる。
    if "rating_floor" not in dec_raw and "rating_floor" in raw["property"]:
        dec_raw["rating_floor"] = float(raw["property"]["rating_floor"])
    decision = Decision(**dec_raw)

    events = [
        DemandEvent(
            date=date.fromisoformat(item["date"]),
            label=item["label"],
            lift=int(item.get("lift", 0)),
            days=int(item.get("days", 7)),
        )
        for item in raw.get("demand_events", [])
    ]
    events.sort(key=lambda e: e.date)

    paths_raw = raw.get("paths", {})
    paths = Paths(
        snapshots=base_dir / paths_raw.get("snapshots", "data/snapshots.jsonl"),
        pms_actuals=base_dir / paths_raw.get("pms_actuals", "data/pms_actuals.csv"),
        output=base_dir / paths_raw.get("output", "dist/index.html"),
    )

    return Config(
        own=own,
        compset=compset,
        estimation=estimation,
        decision=decision,
        demand_events=events,
        paths=paths,
        area_label=raw.get("area_label", "奈良公園・春日エリア"),
    )
