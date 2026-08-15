"""今日の値付け判断を 1 つに畳む。

判断は自社と競合中央値の差だけで決める。差が誤差の範囲なら動かさない。
「動かさない」を既定にしておかないと、推定誤差にレートが振り回される。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .occupancy import MarketEstimate

ACTION_HOLD = "据え置き"
ACTION_RAISE = "値上げ検討"
ACTION_CUT = "要因分析"
ACTION_DEFER = "判断保留"

_ACTION_CSS = {
    ACTION_HOLD: "hold",
    ACTION_RAISE: "up",
    ACTION_CUT: "alert",
    ACTION_DEFER: "alert",
}


@dataclass(frozen=True)
class Verdict:
    action: str
    headline: str
    body: str

    @property
    def css_class(self) -> str:
        return _ACTION_CSS.get(self.action, "hold")


def decide(market: MarketEstimate, config: Config) -> Verdict:
    """今日の判断を返す。"""
    own = market.own
    rules = config.pricing_rules

    if not own.is_actionable or own.occupancy_pct is None:
        return Verdict(
            action=ACTION_DEFER,
            headline="推定の幅が広すぎる。判断を保留する",
            body=(
                f"直近 {market.window_days} 日の自社レビューは "
                f"{own.reviews_in_window:.0f} 件（信頼度 {own.confidence}）。"
                "この母数では稼働率の 95% 区間が判断の閾値より広く、"
                "どちらに動かすべきかを推定から決められない。"
                "窓を延ばすか、観測日数が積み上がるまで、"
                "従来どおり残室と実績で判断する。"
            ),
        )

    median = market.competitor_median_occupancy_pct
    if median is None:
        return Verdict(
            action=ACTION_DEFER,
            headline="比較できる競合がない。判断を保留する",
            body=(
                "信頼度の足りる競合施設が 1 件もなく、相対比較が成立しない。"
                "コンプセットの構成と place_id を見直すこと。"
            ),
        )

    gap = own.occupancy_pct - median
    context = f"自社 {own.occupancy_pct:.0f}% / 競合中央値 {median:.0f}%。"

    # 閾値を超えていても、その差が計数誤差より小さければ動かさない。
    # 誤差でレートを動かすのが、この種の推定でいちばんやりやすい失敗。
    if abs(gap) >= min(rules.raise_gap_pt, rules.cut_gap_pt) and not own.separated_from(median):
        return Verdict(
            action=ACTION_HOLD,
            headline=f"差 {abs(gap):.0f}pt は誤差と区別できない。据え置き",
            body=(
                context + f"自社の推定は 95% 区間で ±{own.relative_margin_pt:.0f}pt。"
                f"差の {abs(gap):.0f}pt はその内側にあり、実際の優劣は判定できない。"
                "窓を延ばすか、観測日数が積み上がるのを待つ。"
            ),
        )

    if gap >= rules.raise_gap_pt:
        return _raise_or_freeze(market, config, gap, context)

    if gap <= -rules.cut_gap_pt:
        return Verdict(
            action=ACTION_CUT,
            headline=f"競合中央値を {abs(gap):.0f}pt 下回る。要因を切り分ける",
            body=(
                context + "値下げの前に、実レート・OTA 掲載順位・写真の"
                "どれが効いているかを確認する。レートはこのダッシュボードでは"
                "分からない。レートショッパー側の数字と突き合わせること。"
            ),
        )

    if abs(gap) <= rules.hold_band_pt:
        return Verdict(
            action=ACTION_HOLD,
            headline="市場と同水準。据え置き",
            body=context + "差は誤差の範囲。ルール通りの残室連動のみで運用する。",
        )

    direction = "上回る" if gap > 0 else "下回る"
    return Verdict(
        action=ACTION_HOLD,
        headline=f"競合中央値を {abs(gap):.0f}pt {direction}。据え置き",
        body=(
            context + f"差は据え置き帯（±{rules.hold_band_pt:g}pt）を出たが、"
            f"判断を変える閾値（{rules.raise_gap_pt:g}pt）には届かない。"
            "推移を見る。"
        ),
    )


def _raise_or_freeze(
    market: MarketEstimate,
    config: Config,
    gap: float,
    context: str,
) -> Verdict:
    """値上げ余地があるときに、品質ゲートで止めるかどうかを判定する。"""
    own = market.own
    floor = config.pricing_rules.rating_floor

    if own.rating is not None and own.rating < floor:
        return Verdict(
            action=ACTION_HOLD,
            headline="値上げ余地はあるが、評価が下限を割っている。据え置き",
            body=(
                context + f"評価 {own.rating:.2f}（下限 {floor:g}）。"
                "低評価のまま単価を上げると露出と稼働の両方を失う。"
                "スコアが回復するまで引き上げを凍結する。"
            ),
        )

    return Verdict(
        action=ACTION_RAISE,
        headline=f"競合中央値を {gap:.0f}pt 上回る。値上げ余地あり",
        body=(
            context + "先の日付から段階的に BAR を引き上げ、"
            "直近の残室は現行レートのまま売り切る。"
        ),
    )
