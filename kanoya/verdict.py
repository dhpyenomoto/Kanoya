"""今日の判断と、それに付随するシグナル。

ここが唯一「で、どうするのか」を書く場所。
ダッシュボードの他の節はすべてこの判断の根拠でしかない。
ルールは明示的に書く。ルールから外れたときだけ人が介入する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .calibrate import BacktestResult, PostingRate
from .config import Config
from .estimate import AreaSeries, PropertyEstimate, compset_median


@dataclass(frozen=True)
class Verdict:
    level: str      # 引き上げ / 据え置き / 引き下げ検討 / 判断保留
    headline: str
    detail: str
    tone: str       # hold / alert / up


@dataclass(frozen=True)
class Signal:
    category: str   # 品質 / 需要 / 競合 / データ
    level: str      # 要対応 / 注視 / 好機
    title: str
    body: str
    tone: str       # alert / watch / up


def _own(estimates: list[PropertyEstimate]) -> PropertyEstimate | None:
    for estimate in estimates:
        if estimate.prop.is_own:
            return estimate
    return None


def decide(
    config: Config,
    estimates: list[PropertyEstimate],
    rating: float | None,
    backtest: BacktestResult | None,
) -> Verdict:
    own = _own(estimates)
    median = compset_median(estimates)
    floor = config.decision.rating_floor

    if own is None or median is None:
        return Verdict(
            level="判断保留",
            headline="比較できる競合が足りない",
            detail="信頼度が確保できた競合が無いため、相対判断を行わない。"
            "コンプセットの place_id と客室数を確認する。",
            tone="alert",
        )

    if own.confidence == "低":
        return Verdict(
            level="判断保留",
            headline="自社の推定が不安定。今日は動かさない",
            detail=f"直近窓のレビュー増分が {own.raw_reviews:.0f} 件しかなく、"
            "推定稼働率の誤差が大きい。台帳が貯まるまで既存のレートを維持する。",
            tone="alert",
        )

    own_pct = own.occupancy * 100
    median_pct = median * 100
    gap = own_pct - median_pct
    rating_ok = rating is None or rating >= floor
    base = f"自社 {own_pct:.0f}% / 競合中央値 {median_pct:.0f}%。"

    if backtest is not None and not backtest.usable_for_levels:
        # 水準そのものが信用できないので、差分ではなくモメンタムだけで判断する。
        own_delta = own.delta_pct
        if own_delta is not None and own_delta <= -10:
            return Verdict(
                level="引き下げ検討",
                headline="水準は測れないが、自社のモメンタムが落ちている",
                detail=base + f"後方検証の平均誤差が {backtest.mean_abs_error_pt:.1f}pt "
                "あり水準の比較には使えない。前期比 "
                f"{own_delta:+.0f}% の失速のみを根拠に、残室の多い日から緩める。",
                tone="alert",
            )
        return Verdict(
            level="据え置き",
            headline="推定精度が水準の議論に届かない。据え置き",
            detail=base + f"後方検証の平均誤差 {backtest.mean_abs_error_pt:.1f}pt。"
            "絶対水準の比較には使わず、モメンタムのみ監視する。",
            tone="hold",
        )

    if gap >= config.decision.raise_threshold_pt:
        if not rating_ok:
            return Verdict(
                level="据え置き",
                headline="引き上げ余地はあるが、評価が下限を割っている",
                detail=base + f"差は +{gap:.0f}pt で通常なら引き上げ局面。"
                f"ただし評価 {rating:.2f} が下限 {floor:.1f} を下回っており、"
                "ここでの値上げは露出低下と稼働喪失を招く。スコア回復まで凍結する。",
                tone="alert",
            )
        return Verdict(
            level="引き上げ",
            headline=f"競合中央値を {gap:.0f}pt 上回っている",
            detail=base + "残室の少ない日から BAR を引き上げる。"
            "引き上げ後 14 日はレビュー増分とキャンセル率を追い、"
            "モメンタムが反転したら戻す。",
            tone="up",
        )

    if gap <= -config.decision.lower_threshold_pt:
        return Verdict(
            level="引き下げ検討",
            headline=f"競合中央値を {abs(gap):.0f}pt 下回っている",
            detail=base + "エリアは埋まっていて自社だけ空いている状態。"
            "レートより先に、写真・プラン・掲載面を疑う。"
            "それでも説明がつかなければ残室の多い日から緩める。",
            tone="alert",
        )

    return Verdict(
        level="据え置き",
        headline="市場と同水準。据え置き",
        detail=base + "差は誤差の範囲。ルール通りの残室連動のみで運用する。",
        tone="hold",
    )


def build_signals(
    config: Config,
    estimates: list[PropertyEstimate],
    series: AreaSeries,
    rating: float | None,
    rate: PostingRate,
    backtest: BacktestResult | None,
    as_of: date,
) -> list[Signal]:
    signals: list[Signal] = []
    own = _own(estimates)
    floor = config.decision.rating_floor

    if rating is not None and rating < floor:
        signals.append(
            Signal(
                category="品質",
                level="要対応",
                title=f"評価が下限 {floor:.1f} を下回っている",
                body=f"現在 {rating:.2f}。ここでの値上げは露出低下と稼働喪失を招く。"
                "スコアが回復するまで BAR の引き上げを凍結する。",
                tone="alert",
            )
        )

    # シェア・オブ・ボイス：直近 30 日と、その前 30 日を比べる。
    if len(series.days) >= 60:
        recent = series.days[-30:]
        prior = series.days[-60:-30]
        recent_share = _share_over(series, recent)
        prior_share = _share_over(series, prior)
        if recent_share is not None and prior_share is not None:
            drop_pt = (prior_share - recent_share) * 100
            if drop_pt >= config.decision.share_drop_alert_pt:
                signals.append(
                    Signal(
                        category="競合",
                        level="要対応",
                        title=f"エリア内シェアが {drop_pt:.1f}pt 縮んでいる",
                        body=f"クチコミ総量に占める自社比率が {prior_share * 100:.1f}% → "
                        f"{recent_share * 100:.1f}%。エリアは動いているのに"
                        "自社が取れていない。掲載面と在庫開放を先に確認する。",
                        tone="alert",
                    )
                )

    # 競合のモメンタム。信頼度が確保できている施設だけ見る。
    for estimate in estimates:
        if estimate.prop.is_own or estimate.confidence == "低":
            continue
        delta = estimate.delta_pct
        if delta is not None and delta >= 15:
            signals.append(
                Signal(
                    category="競合",
                    level="注視",
                    title=f"{estimate.prop.label} が前期比 {delta:+.0f}%",
                    body=f"推定稼働 {estimate.occupancy * 100:.0f}%。"
                    "在庫を絞ったのか、値下げで取りに来たのかはこの指標では分からない。"
                    "レートショッパー側で当該施設の実レートを確認する。",
                    tone="watch",
                )
            )

    # 需要暦。判断そのものではなく、仕込みの期限として出す。
    horizon = config.decision.event_horizon_days
    for event in config.demand_events:
        days_out = (event.date - as_of).days
        if 0 <= days_out <= horizon:
            signals.append(
                Signal(
                    category="需要",
                    level="好機",
                    title=f"{event.label} まで {days_out} 日",
                    body=f"{event.date.isoformat()} 起点で {event.days} 日間。"
                    f"想定需要押し上げ +{event.lift}%。"
                    "この期間のレートと最低宿泊日数は手動で確定させる。",
                    tone="up",
                )
            )

    # データ品質。ここが崩れていたら上の判断は全部読めない。
    if not rate.measured:
        signals.append(
            Signal(
                category="データ",
                level="要対応",
                title="投稿率が未実測。全推定がフォールバック値の上に乗っている",
                body=f"仮置きの {rate.rate * 100:.1f}% を使用中。"
                "自社 PMS の日次実績（date, rooms_sold, rooms_available）を"
                "投入するまで、稼働率の絶対値は読まないこと。",
                tone="alert",
            )
        )

    if backtest is None:
        signals.append(
            Signal(
                category="データ",
                level="注視",
                title="後方検証がまだ走っていない",
                body="推定手法を自社実績に当てた誤差が測れていない。"
                "台帳と PMS 実績が 90 日ぶん揃うまでは相対順位のみ読む。",
                tone="watch",
            )
        )
    elif not backtest.usable_for_levels:
        signals.append(
            Signal(
                category="データ",
                level="要対応",
                title=f"後方検証の平均誤差が {backtest.mean_abs_error_pt:.1f}pt",
                body=f"n={backtest.windows} 窓、最大 {backtest.worst_abs_error_pt:.1f}pt。"
                "推定稼働率の絶対水準は議論に使えない。前期比のみ使う。",
                tone="alert",
            )
        )

    weak = [e.prop.label for e in estimates if e.confidence == "低"]
    if weak:
        signals.append(
            Signal(
                category="データ",
                level="注視",
                title=f"信頼度が確保できない施設が {len(weak)} 件",
                body="、".join(weak) + "。レビュー増分が少なく推定が不安定。"
                "中央値の算出からは除外している。",
                tone="watch",
            )
        )

    if own is not None and not own.covered:
        signals.append(
            Signal(
                category="データ",
                level="注視",
                title="自社の台帳が推定窓を覆っていない",
                body="スナップショットの開始日が窓の始点より後。"
                "窓の前半は増分が観測できておらず、稼働率を過小評価している。",
                tone="watch",
            )
        )

    return signals


def _share_over(series: AreaSeries, days: list[date]) -> float | None:
    index = {day: i for i, day in enumerate(series.days)}
    area_total = 0.0
    own_total = 0.0
    for day in days:
        i = index.get(day)
        if i is None:
            continue
        area_total += series.area[i]
        own_total += series.own[i]
    if area_total <= 0:
        return None
    return own_total / area_total


def next_events(config: Config, as_of: date, limit: int = 4) -> list:
    """as_of 以降の需要イベント。過ぎたものは落とす。"""
    upcoming = [e for e in config.demand_events if e.end >= as_of - timedelta(days=180)]
    upcoming.sort(key=lambda e: e.date)
    return upcoming[:limit]
