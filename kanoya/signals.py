"""判断とシグナルの生成。

ダッシュボードの上段に出る「今日の判断」は、ここのルールの出力そのもの。
毎日 人が数字を眺めて決めるのではなく、ルールが決め、逸脱したときだけ
人が介入する。ルールを変えたいときは config.json の rules を変える。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .calibration import Backtest, PostingRate
from .config import Config
from .estimate import Estimate, Window, median


@dataclass(frozen=True)
class Verdict:
    level: str        # 表示ラベル: 強気 / 据え置き / 需要獲得
    tone: str         # CSS クラス: raise / hold / capture
    headline: str
    detail: str


@dataclass(frozen=True)
class Signal:
    category: str     # 品質 / 需要 / シェア / 競合 / データ
    level: str        # 要対応 / 注視 / 好機
    tone: str         # CSS クラス: alert / watch / up
    title: str
    detail: str


def _own(estimates: list[Estimate]) -> Estimate:
    for est in estimates:
        if est.prop.is_own:
            return est
    raise ValueError("自社の推定がない")


def build_verdict(
    config: Config,
    estimates: list[Estimate],
    rate_blocked: bool,
    low_confidence: bool,
) -> Verdict:
    """自社と競合中央値の差から今日の判断を決める。

    差が誤差の範囲に収まっているときに動かさないことが、このルールの要点。
    推定の相対誤差は 15% 前後あるので、小さな差を根拠に BAR を動かすと
    ノイズを追いかけることになる。
    """
    own = _own(estimates)
    comps = [e.occupancy for e in estimates if not e.prop.is_own]
    comp_median = median(comps)
    gap_pt = (own.occupancy - comp_median) * 100.0
    band = config.rules.hold_band_pt

    levels = f"自社 {own.occupancy * 100:.0f}% / 競合中央値 {comp_median * 100:.0f}%"

    if low_confidence:
        return Verdict(
            level="据え置き",
            tone="hold",
            headline="推定の精度が足りない。据え置き",
            detail=(
                f"{levels}。推定の相対誤差が許容範囲を超えており、差を根拠に"
                "できない。観測日数が積み上がるまでルール通りの残室連動のみで運用する。"
            ),
        )

    if gap_pt >= band:
        if rate_blocked:
            return Verdict(
                level="据え置き",
                tone="hold",
                headline="強気材料あり。ただし品質ゲートで凍結",
                detail=(
                    f"{levels}。差 {gap_pt:+.0f}pt は引き上げ圏だが、評価が下限を"
                    "下回っているため BAR の引き上げを見送る。露出を失う損失のほうが大きい。"
                ),
            )
        return Verdict(
            level="強気",
            tone="raise",
            headline="市場を上回る。引き上げ余地あり",
            detail=(
                f"{levels}。差 {gap_pt:+.0f}pt。残室の出方を見ながら、"
                "高需要日から段階的に BAR を引き上げる。"
            ),
        )

    if gap_pt <= -band:
        return Verdict(
            level="需要獲得",
            tone="capture",
            headline="市場を下回る。需要の取りこぼしを疑う",
            detail=(
                f"{levels}。差 {gap_pt:+.0f}pt。レートより先に、露出・写真・"
                "最低泊数の制約を点検する。値下げはその後の判断。"
            ),
        )

    return Verdict(
        level="据え置き",
        tone="hold",
        headline="市場と同水準。据え置き",
        detail=f"{levels}。差は誤差の範囲。ルール通りの残室連動のみで運用する。",
    )


def build_signals(
    config: Config,
    estimates: list[Estimate],
    rate: PostingRate,
    backtest: Backtest,
    own_rating: float | None,
    area_recent_pct: float | None,
    share_change_pct: float | None,
    as_of: dt.date,
    current: Window,
) -> list[Signal]:
    """判断に付随して確認する項目。発火したものだけを返す。"""
    rules = config.rules
    own = _own(estimates)
    out: list[Signal] = []

    # 品質ゲート。値付けより先に効く。
    if own_rating is not None and own_rating < rules.rating_floor:
        out.append(
            Signal(
                category="品質",
                level="要対応",
                tone="alert",
                title=f"評価が下限 {rules.rating_floor:g} を下回っている",
                detail=(
                    f"現在 {own_rating:.2f}。ここでの値上げは露出低下と稼働喪失を招く。"
                    "スコアが回復するまで BAR の引き上げを凍結する。"
                ),
            )
        )

    # 推定そのものが使えるかどうか。これが崩れていれば他のシグナルも読めない。
    confidence = own.confidence(
        config.estimation.confidence_high_rse, config.estimation.confidence_medium_rse
    )
    if confidence == "低":
        out.append(
            Signal(
                category="データ",
                level="要対応",
                tone="alert",
                title="自社の推定が信頼度「低」",
                detail=(
                    f"窓内レビュー {own.reviews:.0f} 件、被覆率 {own.coverage * 100:.0f}%。"
                    "この状態の推定値を値付けの根拠にしない。"
                ),
            )
        )
    if backtest.usable_range != "水準の議論に使える" and backtest.blocks:
        out.append(
            Signal(
                category="データ",
                level="注視",
                tone="watch",
                title=f"バックテスト誤差 {backtest.mean_absolute_error_pt:.1f}pt",
                detail=(
                    f"推定値の用途を「{backtest.usable_range}」に制限する。"
                    "施設間の順位とモメンタムは読めるが、稼働率の水準は主張できない。"
                ),
            )
        )

    # 自社シェアの変化。エリアが伸びているのに自社が伸びていない状態を捕まえる。
    if share_change_pct is not None:
        if share_change_pct <= -rules.share_shift_pct:
            out.append(
                Signal(
                    category="シェア",
                    level="要対応",
                    tone="alert",
                    title=f"エリア内シェアが前期比 {share_change_pct:.0f}%",
                    detail=(
                        "エリアの需要を取りこぼしている。露出面（OTA 掲載順位・"
                        "写真・レビュー返信）を先に点検する。"
                    ),
                )
            )
        elif share_change_pct >= rules.share_shift_pct:
            out.append(
                Signal(
                    category="シェア",
                    level="好機",
                    tone="up",
                    title=f"エリア内シェアが前期比 {share_change_pct:+.0f}%",
                    detail="取り分が増えている。稼働ではなく単価で回収できる局面。",
                )
            )

    # 競合のモメンタム。信頼度「高」の施設に限る。
    # 客室数の小さい施設は推定が跳ねやすく、そこを追うと誤った反応になる。
    for est in estimates:
        if est.prop.is_own:
            continue
        change = est.change_pct
        if change is None or change < rules.competitor_momentum_pct:
            continue
        if est.confidence(
            config.estimation.confidence_high_rse, config.estimation.confidence_medium_rse
        ) != "高":
            continue
        out.append(
            Signal(
                category="競合",
                level="注視",
                tone="watch",
                title=f"{est.prop.name} が前期比 {change:+.0f}%",
                detail=(
                    f"推定稼働 {est.occupancy * 100:.0f}%。同じ客層を取りに来ている可能性がある。"
                    "該当施設の在庫と最低泊数の動きを確認する。"
                ),
            )
        )

    # エリア需要の直近の締まり具合。
    if area_recent_pct is not None and abs(area_recent_pct) >= rules.compression_pct:
        if area_recent_pct > 0:
            out.append(
                Signal(
                    category="需要",
                    level="好機",
                    tone="up",
                    title=f"エリア需要が直近 14 日で {area_recent_pct:+.0f}%",
                    detail="市場全体が締まっている。残室連動の刻み幅を一段大きくする。",
                )
            )
        else:
            out.append(
                Signal(
                    category="需要",
                    level="注視",
                    tone="watch",
                    title=f"エリア需要が直近 14 日で {area_recent_pct:.0f}%",
                    detail="市場全体が緩んでいる。自社だけの問題ではないため、値下げを急がない。",
                )
            )

    # 需要イベントのリードタイム。売り止め・下限レートの設定が間に合う時期に出す。
    for event in config.events:
        lead = (event.date - as_of).days
        if 0 <= lead <= rules.event_lookahead_days:
            out.append(
                Signal(
                    category="需要",
                    level="注視",
                    tone="watch",
                    title=f"{event.name} まで {lead} 日",
                    detail=(
                        f"想定需要 +{event.lift_pct}%。この期間の下限レートと最低泊数を"
                        "先に設定し、早期の安価な在庫流出を止める。"
                    ),
                )
            )

    order = {"要対応": 0, "注視": 1, "好機": 2}
    out.sort(key=lambda s: order.get(s.level, 9))
    return out
