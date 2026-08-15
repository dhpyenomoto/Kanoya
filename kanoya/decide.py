"""今日の判断とシグナルの生成。

ルールは config の rules に置き、ここでは判定だけを行う。
人が介入するのはルールを逸脱するときだけ、という運用を前提にしている。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import Config, DemandEvent
from .estimate import Estimate, comp_median, noise_floor_pt


@dataclass(frozen=True)
class Verdict:
    kind: str  # hold / raise / cut
    label: str  # 据え置き / 引き上げ / 引き下げ
    headline: str
    body: str
    alert: bool
    gap_pt: float | None
    comp_median: float | None
    noise_pt: float = 0.0


@dataclass(frozen=True)
class Signal:
    category: str
    level: str  # 要対応 / 注視 / 好機
    css: str  # alert / watch / up / ""
    title: str
    body: str


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def build_verdict(estimates: list[Estimate], cfg: Config) -> Verdict:
    own = next((e for e in estimates if e.is_own), None)
    median = comp_median(estimates)
    rules = cfg.rules

    if own is None or median is None:
        return Verdict(
            kind="hold",
            label="判断保留",
            headline="比較対象が揃っていない",
            body="自社またはコンプセットの推定が出ていない。スナップショットの取得状況を確認する。",
            alert=True,
            gap_pt=None,
            comp_median=median,
        )

    if own.reviews <= 0 or all(e.reviews <= 0 for e in estimates if not e.is_own):
        return Verdict(
            kind="hold",
            label="判断保留",
            headline="推定に足るクチコミがまだ無い",
            body=(
                "窓内のクチコミ増分が 0。Places API は過去に遡れないので、"
                "日次スナップショットを取り始めてから窓の日数分が貯まるまで判断は出せない。"
            ),
            alert=True,
            gap_pt=None,
            comp_median=median,
        )

    gap_pt = (own.occupancy - median) * 100.0
    noise = noise_floor_pt(estimates)
    floor_breach = own.rating is not None and own.rating < rules.rating_floor
    base = f"自社 {_pct(own.occupancy)} / 競合中央値 {_pct(median)}。"

    # 閾値はノイズ幅より下には設定できない。5室規模では計数ノイズが
    # 支配的で、閾値だけを見ると毎週のように「引き上げ」が出てしまう。
    raise_at = max(rules.raise_threshold_pt, noise)
    cut_at = min(rules.cut_threshold_pt, -noise)
    noisy = abs(gap_pt) > rules.parity_band_pt and abs(gap_pt) <= noise

    if gap_pt >= raise_at:
        if floor_breach:
            return Verdict(
                kind="hold",
                label="据え置き（引き上げ凍結）",
                headline="引き上げ条件を満たすが、評価下限割れで凍結",
                body=(
                    base
                    + f"需要は競合を {gap_pt:+.0f}pt 上回るが、評価 {own.rating:.2f} が下限 "
                    f"{rules.rating_floor} を割っている。露出低下の方が値上げ益より高くつく。"
                ),
                alert=True,
                gap_pt=gap_pt,
                comp_median=median,
                noise_pt=noise,
            )
        return Verdict(
            kind="raise",
            label="引き上げ",
            headline="需要が市場を上回る。BAR を引き上げる",
            body=(
                base
                + f"差は {gap_pt:+.0f}pt で、推定のノイズ幅 ±{noise:.0f}pt を超えている。"
                "残室の多い日から段階的に反映する。"
            ),
            alert=False,
            gap_pt=gap_pt,
            comp_median=median,
            noise_pt=noise,
        )

    if gap_pt <= cut_at:
        return Verdict(
            kind="cut",
            label="引き下げ",
            headline="需要が市場に劣後。取り込みを優先する",
            body=(
                base
                + f"差は {gap_pt:+.0f}pt でノイズ幅 ±{noise:.0f}pt を超える。"
                "直近の空室日から下げ、リードタイムの長い日は据え置く。"
            ),
            alert=False,
            gap_pt=gap_pt,
            comp_median=median,
            noise_pt=noise,
        )

    if noisy:
        detail = (
            f"差は {gap_pt:+.0f}pt あるが、推定のノイズ幅 ±{noise:.0f}pt の内側にある。"
            "この差で値を動かすと、実需ではなく投稿件数のばらつきに反応することになる。"
        )
    elif abs(gap_pt) <= rules.parity_band_pt:
        detail = "差は誤差の範囲。"
    else:
        detail = f"差は {gap_pt:+.0f}pt で判断閾値未満。"

    return Verdict(
        kind="hold",
        label="据え置き",
        headline="市場と同水準。据え置き",
        body=base + detail + "ルール通りの残室連動のみで運用する。",
        alert=False,
        gap_pt=gap_pt,
        comp_median=median,
        noise_pt=noise,
    )


def upcoming_events(events: tuple[DemandEvent, ...], as_of: date, horizon: int):
    return [e for e in events if 0 <= (e.date - as_of).days <= horizon]


def build_signals(
    estimates: list[Estimate],
    cfg: Config,
    *,
    as_of: date,
    own_share: float | None = None,
    prev_own_share: float | None = None,
    stale_days: int | None = None,
    calibration_measured: bool = True,
    own_actual_occupancy: float | None = None,
    own_model_occupancy: float | None = None,
) -> list[Signal]:
    rules = cfg.rules
    own = next((e for e in estimates if e.is_own), None)
    out: list[Signal] = []

    if own is not None and own.rating is not None and own.rating < rules.rating_floor:
        out.append(
            Signal(
                category="品質",
                level="要対応",
                css="alert",
                title=f"評価が下限 {rules.rating_floor} を下回っている",
                body=(
                    f"現在 {own.rating:.2f}。ここでの値上げは露出低下と稼働喪失を招く。"
                    "スコアが回復するまでBARの引き上げを凍結する。"
                ),
            )
        )

    if stale_days is not None and stale_days > rules.stale_snapshot_days:
        out.append(
            Signal(
                category="データ",
                level="要対応",
                css="alert",
                title=f"スナップショットが {stale_days} 日途切れている",
                body="累計件数の差分が取れないため、直近の推定は伸びしろを取りこぼしている。取得ジョブを確認する。",
            )
        )

    low_cov = [e for e in estimates if e.coverage < rules.min_coverage]
    if low_cov:
        names = "、".join(e.name for e in low_cov[:3])
        out.append(
            Signal(
                category="データ",
                level="注視",
                css="watch",
                title="観測が薄い施設がある",
                body=f"{names} は窓内のスナップショット被覆が {rules.min_coverage:.0%} 未満。相対比較から外して読む。",
            )
        )

    if not calibration_measured:
        out.append(
            Signal(
                category="前提",
                level="注視",
                css="watch",
                title="投稿率が実測されていない",
                body="config の既定値で全施設を割り戻している。自社 PMS 実績を突合するまで、水準の議論には使えない。",
            )
        )

    movers = [
        e
        for e in estimates
        if not e.is_own and e.delta_pct is not None and e.delta_pct >= rules.momentum_flag_pct
    ]
    for m in sorted(movers, key=lambda e: -(e.delta_pct or 0))[:2]:
        out.append(
            Signal(
                category="競合",
                level="注視",
                css="watch",
                title=f"{m.name} が前期比 {m.delta_pct:+.0f}%",
                body=(
                    f"推定稼働 {_pct(m.occupancy)}（信頼度 {m.confidence}）。"
                    "取り込み方が変わった可能性がある。該当日の残室と価格を照合する。"
                ),
            )
        )

    if own is not None and own_actual_occupancy is not None and own_model_occupancy is not None:
        # 自社だけは PMS の実数がある。投稿率は窓より前の期間で作ってあるので、
        # ここは真のホールドアウト。外していれば競合の推定も同じだけ外れている。
        diff_pt = (own_model_occupancy - own_actual_occupancy) * 100.0
        if abs(diff_pt) > max(own.band_pt, 5.0):
            out.append(
                Signal(
                    category="検証",
                    level="要対応",
                    css="alert",
                    title=f"自社の推定が実績を {diff_pt:+.0f}pt 外している",
                    body=(
                        f"同じ日付でレビュー由来 {_pct(own_model_occupancy)} に対し PMS 実績 "
                        f"{_pct(own_actual_occupancy)}。ノイズ幅 ±{own.band_pt:.0f}pt を超える乖離。"
                        "投稿率が動いたか、レビューの帰属日がずれている。競合の推定も同じだけ疑う。"
                    ),
                )
            )

    if own is not None and own.capped:
        out.append(
            Signal(
                category="在庫",
                level="好機",
                css="up",
                title="推定が上限に張り付いている",
                body="推定販売室数が総室数を超えた。投稿率が実態より低く見積もられているか、実質満室。価格の上振れ余地を確認する。",
            )
        )

    if own_share is not None and prev_own_share and prev_own_share > 0:
        drop = (own_share - prev_own_share) / prev_own_share * 100.0
        if drop <= -20.0:
            out.append(
                Signal(
                    category="シェア",
                    level="注視",
                    css="watch",
                    title=f"エリア内シェアが前期比 {drop:+.0f}%",
                    body=(
                        f"クチコミ総量に占める自社比率が {prev_own_share:.1%} → {own_share:.1%}。"
                        "市場が伸びる局面で取り分を落としている。"
                    ),
                )
            )

    for e in upcoming_events(cfg.demand_events, as_of, rules.event_horizon_days):
        days = (e.date - as_of).days
        out.append(
            Signal(
                category="需要暦",
                level="好機",
                css="up",
                title=f"{e.name} まで {days} 日",
                body=f"想定押し上げ +{e.lift_pct:.0f}%。閉栓日と最低泊数の設定を前倒しで確認する。",
            )
        )

    if not out:
        out.append(
            Signal(
                category="運用",
                level="情報",
                css="",
                title="ルール逸脱なし",
                body="いずれの閾値にも触れていない。人の介入は不要。",
            )
        )
    return out
