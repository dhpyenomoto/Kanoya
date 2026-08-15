"""今日の判断。

自社の推定稼働と競合中央値の差だけで決める。差が閾値内なら「据え置き」と
はっきり言う。毎日なにか動かす理由を探すダッシュボードは、レベニュー管理では
害になる。
"""

from __future__ import annotations

from .config import Config
from .models import Backtest, PropertyEstimate, Verdict


def decide(
    config: Config,
    own: PropertyEstimate,
    compset_median: float,
    backtest: Backtest,
) -> Verdict:
    rules = config.pricing
    gap = own.occupancy - compset_median
    levels = f"自社 {own.occupancy:.0f}% / 競合中央値 {compset_median:.0f}%。"

    # 推定が水準比較に耐えないなら、水準比較を根拠にした判断は出さない。
    if backtest.windows and backtest.usable_for == "方向感のみ":
        return Verdict(
            level="判断保留",
            headline="推定精度が不足。値付け判断には使わない",
            body=(
                f"{levels}実績との平均誤差 {backtest.mean_abs_error_pt:.1f}pt。"
                "キャリブレーションを取り直すまで、この数字を根拠に動かさない。"
            ),
            tone="alert",
        )

    rating_blocked = (
        own.rating is not None and own.rating < rules.rating_floor
    )

    if gap <= rules.cut_gap_pt:
        return Verdict(
            level="要注意",
            headline="市場に対して稼働を落としている",
            body=(
                f"{levels}差 {gap:+.0f}pt。価格が原因かを実レートで確認し、"
                "原因が価格なら残室連動の下限を下げる。"
            ),
            tone="alert",
        )

    if gap >= rules.raise_gap_pt:
        if rating_blocked:
            return Verdict(
                level="据え置き",
                headline="値上げ余地はあるが、評価が下限割れのため凍結",
                body=(
                    f"{levels}差 {gap:+.0f}pt は引き上げ水準だが、"
                    f"評価 {own.rating:.2f} が下限 {rules.rating_floor:.1f} を下回る。"
                    "スコアが戻るまでBARは動かさない。"
                ),
                tone="alert",
            )
        return Verdict(
            level="引き上げ検討",
            headline="市場を上回る稼働。BAR引き上げの余地",
            body=(
                f"{levels}差 {gap:+.0f}pt。実レートを確認したうえで、"
                "残室の少ない日から段階的に上げる。"
            ),
            tone="hold",
        )

    return Verdict(
        level="据え置き",
        headline="市場と同水準。据え置き",
        body=(
            f"{levels}差は誤差の範囲。ルール通りの残室連動のみで運用する。"
        ),
        tone="hold",
    )
