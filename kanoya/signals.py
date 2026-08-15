"""日々の判断に付随して人が確認すべき項目を機械的に洗い出す。

シグナルは判断そのものではない。ルールから外れた事象を可視化し、
人が介入すべき場面だけを絞り込むためのもの。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import Config
from .occupancy import CONFIDENCE_LOW, CONFIDENCE_NONE, MarketEstimate

LEVEL_ALERT = "要対応"
LEVEL_OPPORTUNITY = "好機"
LEVEL_WATCH = "注視"

#: 需要イベントを事前告知する日数。
EVENT_LOOKAHEAD_DAYS = 45
#: エリアシェアがこれ以上落ちたら注視対象にする（相対%）。
SHARE_DROP_PCT = 15.0
#: 競合のモメンタムがこれを超えたら注視対象にする（相対%）。
COMPETITOR_SURGE_PCT = 15.0

_LEVEL_CSS = {
    LEVEL_ALERT: "alert",
    LEVEL_OPPORTUNITY: "up",
    LEVEL_WATCH: "watch",
}


@dataclass(frozen=True)
class Signal:
    category: str
    level: str
    title: str
    body: str

    @property
    def css_class(self) -> str:
        return _LEVEL_CSS.get(self.level, "watch")


def collect_signals(
    market: MarketEstimate,
    config: Config,
    prior_share: float | None = None,
) -> list[Signal]:
    """今日確認すべきシグナルを重要度順に返す。"""
    signals: list[Signal] = []
    signals.extend(_rating_signals(market, config))
    signals.extend(_demand_signals(market, config))
    signals.extend(_share_signals(market, prior_share))
    signals.extend(_competitor_signals(market))
    signals.extend(_data_signals(market))
    signals.extend(_event_signals(config, market.as_of))

    order = {LEVEL_ALERT: 0, LEVEL_OPPORTUNITY: 1, LEVEL_WATCH: 2}
    return sorted(signals, key=lambda s: order.get(s.level, 3))


def _rating_signals(market: MarketEstimate, config: Config) -> list[Signal]:
    own = market.own
    floor = config.pricing_rules.rating_floor
    if own.rating is None or own.rating >= floor:
        return []

    return [
        Signal(
            category="品質",
            level=LEVEL_ALERT,
            title=f"評価が下限 {floor:g} を下回っている",
            body=(
                f"現在 {own.rating:.2f}。ここでの値上げは露出低下と稼働喪失を招く。"
                "スコアが回復するまで BAR の引き上げを凍結する。"
            ),
        )
    ]


def _demand_signals(market: MarketEstimate, config: Config) -> list[Signal]:
    own = market.own
    median = market.competitor_median_occupancy_pct
    if own.occupancy_pct is None or median is None:
        return []

    gap = own.occupancy_pct - median
    rules = config.pricing_rules

    if gap <= -rules.cut_gap_pt:
        return [
            Signal(
                category="需要",
                level=LEVEL_ALERT,
                title=f"競合中央値を {abs(gap):.0f}pt 下回っている",
                body=(
                    f"自社 {own.occupancy_pct:.0f}% / 競合中央値 {median:.0f}%。"
                    "レートが市場より高いか、露出が落ちている。実レートを"
                    "レートショッパーで突き合わせて原因を切り分ける。"
                ),
            )
        ]

    if gap >= rules.raise_gap_pt:
        return [
            Signal(
                category="需要",
                level=LEVEL_OPPORTUNITY,
                title=f"競合中央値を {gap:.0f}pt 上回っている",
                body=(
                    f"自社 {own.occupancy_pct:.0f}% / 競合中央値 {median:.0f}%。"
                    "残室の売り急ぎを止め、先の日付から段階的に単価を上げる。"
                ),
            )
        ]

    return []


def _share_signals(market: MarketEstimate, prior_share: float | None) -> list[Signal]:
    if not prior_share:
        return []

    change_pct = (market.own_share - prior_share) / prior_share * 100
    if change_pct > -SHARE_DROP_PCT:
        return []

    return [
        Signal(
            category="シェア",
            level=LEVEL_WATCH,
            title=f"エリア内シェアが前期比 {change_pct:.0f}% 縮小",
            body=(
                f"{prior_share * 100:.1f}% → {market.own_share * 100:.1f}%。"
                "市場が伸びているのに取り分が細っている場合、価格ではなく"
                "露出（OTA 掲載順位・写真・在庫の出し方）を先に疑う。"
            ),
        )
    ]


def _competitor_signals(market: MarketEstimate) -> list[Signal]:
    surging = [
        e
        for e in market.competitors
        if e.is_actionable
        and e.momentum_pct is not None
        and e.momentum_pct >= COMPETITOR_SURGE_PCT
    ]
    if not surging:
        return []

    top = max(surging, key=lambda e: e.momentum_pct or 0.0)
    return [
        Signal(
            category="競合",
            level=LEVEL_WATCH,
            title=f"{top.prop.name} が前期比 +{top.momentum_pct:.0f}%",
            body=(
                "急伸している施設がある。同一日程で自社が取りこぼしていないか、"
                "残室と価格の出し方を確認する。"
            ),
        )
    ]


def _data_signals(market: MarketEstimate) -> list[Signal]:
    weak = [
        e.prop.name
        for e in market.estimates
        if e.confidence in (CONFIDENCE_LOW, CONFIDENCE_NONE)
    ]
    if not weak:
        return []

    return [
        Signal(
            category="データ",
            level=LEVEL_WATCH,
            title=f"{len(weak)} 施設で推定の信頼度が低い",
            body=(
                "対象: " + " / ".join(weak) + "。"
                "レビュー母数が薄く、1 件の増減で稼働率が大きく振れる。"
                "この施設の数値を根拠に価格を動かさないこと。"
            ),
        )
    ]


def _event_signals(config: Config, as_of: date) -> list[Signal]:
    upcoming = [
        event
        for event in config.events
        if 0 <= (event.date - as_of).days <= EVENT_LOOKAHEAD_DAYS
    ]
    if not upcoming:
        return []

    nearest = min(upcoming, key=lambda e: e.date)
    days_out = (nearest.date - as_of).days
    return [
        Signal(
            category="需要暦",
            level=LEVEL_WATCH,
            title=f"{days_out} 日後に {nearest.name}",
            body=(
                f"{nearest.date.isoformat()} 前後は例年 +{nearest.lift}% の需要増。"
                "この期間だけは残室連動ルールを外し、手動で下限単価を引き上げる。"
            ),
        )
    ]
