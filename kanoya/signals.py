"""シグナル判定。

「今日の判断」に添えて人が確認すべき項目だけを出す。平常時は空でよい。
シグナルが毎日並ぶダッシュボードは読まれなくなり、読まれない警告は無いのと同じ。
"""

from __future__ import annotations

from datetime import date

from .config import Config
from .models import AreaShare, Backtest, PropertyEstimate, Signal


def evaluate(
    config: Config,
    own: PropertyEstimate,
    estimates: list[PropertyEstimate],
    share: AreaShare,
    prev_share_pct: float | None,
    backtest: Backtest,
    as_of: date,
    today: date,
) -> list[Signal]:
    rules = config.pricing
    signals: list[Signal] = []

    # 品質。値付けより先に効く。露出が落ちれば価格の議論自体が成立しない。
    if own.rating is not None and own.rating < rules.rating_floor:
        signals.append(
            Signal(
                category="品質",
                level="要対応",
                tone="alert",
                title=f"評価が下限 {rules.rating_floor:.1f} を下回っている",
                body=(
                    f"現在 {own.rating:.2f}。ここでの値上げは露出低下と稼働喪失を招く。"
                    "スコアが回復するまでBARの引き上げを凍結する。"
                ),
            )
        )

    # 自社モメンタム。落ちているときは水準ではなく推移を見る。
    change = own.change_pct
    if change is not None and change <= -rules.momentum_alert_pct:
        signals.append(
            Signal(
                category="自社",
                level="要対応",
                tone="alert",
                title=f"自社の推定稼働が前期比 {change:+.0f}%",
                body=(
                    "エリア需要と自社の乖離を確認する。需要側が落ちていないなら"
                    "価格か在庫配分の問題であり、値付けルールの見直し対象。"
                ),
            )
        )

    # エリア内シェア。市場が伸びていても取り分が細るなら負けている。
    if prev_share_pct is not None:
        drop = prev_share_pct - share.share_pct
        if drop >= rules.share_drop_alert_pt:
            signals.append(
                Signal(
                    category="シェア",
                    level="注視",
                    tone="watch",
                    title=f"エリア内シェアが {drop:.1f}pt 低下",
                    body=(
                        f"前期 {prev_share_pct:.1f}% → 今期 {share.share_pct:.1f}%。"
                        "市場の伸びを取りこぼしている可能性がある。"
                    ),
                )
            )

    # 競合モメンタム。信頼度が高い施設だけを対象にする。
    # 母数の小さい施設は数件のレビューで大きく振れるため、ここで拾うと誤報になる。
    for est in estimates:
        if est.property.is_own or est.confidence != "高":
            continue
        est_change = est.change_pct
        if est_change is not None and est_change >= rules.momentum_alert_pct:
            signals.append(
                Signal(
                    category="競合",
                    level="注視",
                    tone="watch",
                    title=f"{est.property.name} が前期比 {est_change:+.0f}%",
                    body=(
                        f"推定稼働 {est.occupancy:.0f}%。値下げによる集客か、"
                        "販路追加かを確認する。追随の要否は実レートを見てから。"
                    ),
                )
            )

    # 需要イベント。前倒しで在庫を締める判断が要るのはこのタイミング。
    for event in config.events:
        lead = (event.date - today).days
        if 0 <= lead <= rules.event_lead_days:
            signals.append(
                Signal(
                    category="需要暦",
                    level="準備",
                    tone="watch",
                    title=f"{event.name} まで {lead} 日",
                    body=(
                        f"想定リフト +{event.lift_pct}%。ピーク日の在庫と最低泊数を"
                        "先に固定し、残室連動の下限を引き上げる。"
                    ),
                )
            )

    # 推定そのものの健全性。
    if backtest.windows and backtest.usable_for != "水準の議論に使える":
        signals.append(
            Signal(
                category="推定",
                level="注視",
                tone="watch",
                title=f"実績との平均誤差 {backtest.mean_abs_error_pt:.1f}pt",
                body=(
                    f"現在の推定は「{backtest.usable_for}」水準。"
                    "水準比較での値付け判断には使わない。"
                ),
            )
        )

    # 収集の穴。データが古ければ判断自体を止める。
    stale = (today - as_of).days - config.estimation.review_lag_days
    if stale > 3:
        signals.append(
            Signal(
                category="収集",
                level="要対応",
                tone="alert",
                title=f"スナップショットが {stale} 日分欠けている",
                body="収集ジョブを確認する。欠測期間は均等配分されており、推移は信用できない。",
            )
        )

    order = {"alert": 0, "watch": 1, "info": 2}
    return sorted(signals, key=lambda s: order.get(s.tone, 3))
