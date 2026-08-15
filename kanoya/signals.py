"""判断ルール。ここだけが「で、どうするか」を決める。

方針：人が毎日眺めて考えるのではなく、ルールを逸脱したときだけ人が介入する。
したがって出力は「今日の判断（1つ）」＋「確認すべきシグナル（0件以上）」。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .config import Config
from .estimate import Report
from .series import Series, window_sum


@dataclass(frozen=True)
class Verdict:
    level: str      # 据え置き / 引き上げ検討 / 引き下げ検討
    headline: str
    detail: str
    tone: str       # hold / up / alert


@dataclass(frozen=True)
class Signal:
    category: str   # 品質 / 需要 / 競合 / 暦
    level: str      # 要対応 / 監視 / 好機
    title: str
    detail: str
    tone: str       # alert / watch / up


def _pct(x: float) -> str:
    return f"{round(x * 100):.0f}%"


def _pt(x: float) -> str:
    return f"{x:+.0f}pt"


def rating_blocked(cfg: Config, report: Report) -> bool:
    """評価が下限割れなら値上げを凍結する。

    露出（Googleの並び）と成約率はレビュースコアに効く。スコアが落ちている局面で
    BARを上げると、稼働を落としたうえにスコア回復も遅れる。
    """
    floor = cfg.own.rating_floor
    return floor is not None and report.own_rating is not None and report.own_rating < floor


def decide(cfg: Config, report: Report) -> Verdict:
    gap = report.gap_pt
    own = _pct(report.own.occupancy)
    med = _pct(report.comp_median_occupancy)
    blocked = rating_blocked(cfg, report)

    # 据え置き帯は固定値ではなく、その日の推定精度で決める。5室規模では
    # クチコミ件数が少ないぶん誤差が大きく、小さな差を動かす理由にしてはいけない。
    #
    # 幅は 1σ（約68%）を既定とする。95%（1.96σ）にすると帯が ±18pt まで開き、
    # 5室ではどんな差でも「据え置き」しか出なくなる。値付けは仮説検定ではなく、
    # 外したときに戻せる可逆な意思決定なので、1σの証拠で動かしてよい。
    band = max(cfg.rules.hold_band_pt, cfg.rules.band_sigma * report.own.occupancy_se_pt)
    ci95 = 1.96 * report.own.occupancy_se_pt

    if abs(gap) <= band:
        return Verdict(
            level="据え置き",
            headline="市場と同水準。据え置き",
            detail=f"自社 {own} / 競合中央値 {med}。差 {gap:+.0f}pt は誤差の範囲"
            f"（据え置き帯 ±{band:.0f}pt、95%区間 ±{ci95:.0f}pt）。"
            "ルール通りの残室連動のみで運用する。",
            tone="hold",
        )

    if gap > band:
        if blocked:
            return Verdict(
                level="据え置き（引き上げ凍結）",
                headline="需要は上だが、評価が下限割れ。据え置き",
                detail=f"自社 {own} / 競合中央値 {med}（{_pt(gap)}）。通常なら引き上げ局面だが、"
                f"評価 {report.own_rating:.2f} が下限 {cfg.own.rating_floor:.1f} を下回っている。"
                "スコアが戻るまでBARは据え置く。",
                tone="alert",
            )
        return Verdict(
            level="引き上げ検討",
            headline="市場より強い。引き上げを検討",
            detail=f"自社 {own} / 競合中央値 {med}（{_pt(gap)}、据え置き帯 ±{band:.0f}pt）。"
            "残室の多い日から段階的に上げ、稼働の落ち方を見て刻む。",
            tone="up",
        )

    return Verdict(
        level="引き下げ検討",
        headline="市場に負けている。取り込みを優先",
        detail=f"自社 {own} / 競合中央値 {med}（{_pt(gap)}）。"
        "直近の空室日を対象に価格と露出を見直す。全期間の一律値下げはしない。",
        tone="alert",
    )


def collect(cfg: Config, report: Report, area_series: Series, own_series: Series) -> list[Signal]:
    out: list[Signal] = []

    # 1. 品質ゲート。値付けより先に効く。
    if rating_blocked(cfg, report):
        out.append(
            Signal(
                category="品質",
                level="要対応",
                title=f"評価が下限 {cfg.own.rating_floor:.1f} を下回っている",
                detail=f"現在 {report.own_rating:.2f}。ここでの値上げは露出低下と稼働喪失を招く。"
                "スコアが回復するまでBARの引き上げを凍結する。",
                tone="alert",
            )
        )

    # 2. エリア内シェアの縮小。市場が伸びていても取り分を落としていれば問題。
    win = report.window
    half = cfg.model.window_days // 2
    mid = win.end - dt.timedelta(days=half - 1)
    recent_area = window_sum(area_series, mid, win.end)
    recent_own = window_sum(own_series, mid, win.end)
    prior_area = window_sum(area_series, win.start, mid - dt.timedelta(days=1))
    prior_own = window_sum(own_series, win.start, mid - dt.timedelta(days=1))
    if recent_area > 0 and prior_area > 0 and prior_own > 0:
        recent_share = recent_own / recent_area
        prior_share = prior_own / prior_area
        drop = (prior_share - recent_share) / prior_share
        if drop >= cfg.rules.share_drop_ratio:
            out.append(
                Signal(
                    category="需要",
                    level="監視",
                    title="エリア内シェアが縮小している",
                    detail=f"直近{half}日 {recent_share * 100:.1f}% ／ その前 {prior_share * 100:.1f}%"
                    f"（{drop * 100:.0f}% 縮小）。市場ではなく自社側の要因を疑う。",
                    tone="watch",
                )
            )

    # 3. 競合モメンタム。自社以上の水準で伸びている先だけを拾う。
    for est in report.estimates:
        if est.prop.is_own:
            continue
        if not est.delta_significant:
            continue  # 件数の揺らぎで説明できる増減は拾わない
        if est.delta_pt >= cfg.rules.momentum_pt and est.occupancy >= report.own.occupancy:
            out.append(
                Signal(
                    category="競合",
                    level="監視",
                    title=f"{est.prop.name} が上位で伸びている",
                    detail=f"推定稼働 {_pct(est.occupancy)}（前期比 {_pt(est.delta_pt)}）。"
                    "自社より上の水準での伸びは、価格帯かチャネルの見直しを示唆する。",
                    tone="watch",
                )
            )

    # 4. 需要暦。窓に入った催事は在庫の締め方を先に決める。
    for ev in cfg.events:
        days_out = (ev.date - report.asof).days
        if 0 <= days_out <= cfg.rules.event_horizon_days:
            out.append(
                Signal(
                    category="暦",
                    level="好機",
                    title=f"{ev.name} まで {days_out} 日",
                    detail="需要が張り付く期間。最低宿泊日数と早期の在庫クローズを先に決める。",
                    tone="up",
                )
            )

    return out
