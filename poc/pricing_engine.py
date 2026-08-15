"""
奈良春日 鹿のや — 価格決定エンジン（試作実装 / Proof of Concept）

別冊03『価格決定エンジン仕様』の算定ロジックを、そのまま動く形にしたもの。

設計方針:
  1. 説明可能性を精度に優先する  … 全推奨価格を乗数分解して返す
  2. 純ADR（手数料控除後）で最適化する
  3. 小標本を前提に縮小推定する  … データが薄い日は基準価格へ引き戻す

本モジュールは副作用を持たない純粋関数の集合として実装する。
外部I/O（収集・配信）は呼び出し側の責務とし、テスト容易性を確保する。
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Sequence

Availability = Literal["available", "sold_out", "restricted", "not_offered"]
Segment = Literal["weekday_low", "weekday_high", "weekend_low", "weekend_high", "peak"]


# ---------------------------------------------------------------------------
# 入力データ構造
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RateObservation:
    """競合1施設・1宿泊日あたりの観測（正規化済み: 2名1室・朝食別・税込・LOS=1・返金可）."""

    property_id: str
    stay_date: dt.date
    price_normalized: float | None  # 在庫なしの場合 None
    availability: Availability
    source: str = "rakuten_api"
    collected_hours_ago: float = 6.0


@dataclass(frozen=True)
class CompsetSnapshot:
    """あるstay_dateに対するコンプセットの状態。"""

    stay_date: dt.date
    observations: Sequence[RateObservation]
    primary_size: int  # 一次コンプセットの登録施設数（網羅率の分母）
    price_trend_7d: float = 0.0  # 直近7日でのコンプセット中央値の変化率

    @property
    def priced(self) -> list[float]:
        return [o.price_normalized for o in self.observations if o.price_normalized is not None]

    @property
    def median(self) -> float | None:
        return statistics.median(self.priced) if self.priced else None

    @property
    def sold_out_ratio(self) -> float:
        if not self.observations:
            return 0.0
        sold = sum(1 for o in self.observations if o.availability == "sold_out")
        return sold / len(self.observations)

    @property
    def coverage(self) -> float:
        if self.primary_size <= 0:
            return 0.0
        return min(1.0, len(self.observations) / self.primary_size)

    @property
    def freshness_hours(self) -> float:
        if not self.observations:
            return math.inf
        return max(o.collected_hours_ago for o in self.observations)


@dataclass(frozen=True)
class OwnState:
    """自社の当該宿泊日における状態。"""

    stay_date: dt.date
    as_of: dt.date
    rooms_sold: int
    current_rate: float
    expected_final_rooms: float  # 過去実績から推定した最終販売室数
    booking_curve: float  # 同一seg のブッキングカーブ B_seg(τ) ∈ (0,1]
    market_index_z: float = 0.0  # 市場季節性（前年同週比などの標準化値）

    @property
    def lead_days(self) -> int:
        return (self.stay_date - self.as_of).days


@dataclass
class Recommendation:
    """推奨結果。根拠を必ず伴う（根拠のない推奨は配信しない）。"""

    stay_date: dt.date
    segment: Segment
    current_price: float
    recommended_price: float
    min_los: int
    factors: dict[str, float]
    evidence: dict[str, Any]
    dpi: float
    auto_publish: bool
    warnings: list[str] = field(default_factory=list)
    channel_gating: dict[str, str] = field(default_factory=dict)

    @property
    def change_ratio(self) -> float:
        if self.current_price <= 0:
            return 0.0
        return self.recommended_price / self.current_price - 1.0

    def explain(self) -> str:
        """承認カードに載せる根拠テキストを生成する。"""
        f = self.factors
        lines = [
            f"【{self.stay_date:%Y-%m-%d (%a)}／{self.segment}】",
            f"  現在 {self.current_price:,.0f}円 → 推奨 {self.recommended_price:,.0f}円 "
            f"({self.change_ratio:+.1%})",
            f"  基準 {f['base']:,.0f}円 × ペース{f['pace']:.3f} × 競合{f['comp']:.3f} "
            f"× イベント{f['event']:.3f} × 在庫{f['inventory']:.3f} × リード{f['lead']:.3f}",
            f"  縮小推定 κ={f['shrinkage']:.2f}／需要圧力 DPI={self.dpi:+.2f}",
            f"  コンプセット中央値 {self.evidence['compset_median']}／"
            f"完売率 {self.evidence['sold_out_ratio']:.0%}／網羅率 {self.evidence['coverage']:.0%}",
            f"  自社 残室 {self.evidence['rooms_left']}/{self.evidence['total_rooms']}／"
            f"ペース {self.evidence['pace']:.2f}／リード {self.evidence['lead_days']}日",
        ]
        if self.min_los > 1:
            lines.append(f"  最低宿泊日数: {self.min_los}泊")
        if self.warnings:
            lines.append("  ⚠ " + " / ".join(self.warnings))
        lines.append("  → " + ("自動配信" if self.auto_publish else "要承認"))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 補助関数
# ---------------------------------------------------------------------------
def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def round_to_ladder(price: float, ladder: int) -> float:
    """価格ラダーへの丸め（心理価格の維持）."""
    return float(round(price / ladder) * ladder)


def classify_segment(
    day: dt.date,
    *,
    events: Iterable[str] = (),
    holidays: frozenset[dt.date] = frozenset(),
    high_season_months: frozenset[int] = frozenset({3, 4, 10, 11}),
) -> Segment:
    """日区分の判定。イベント日は peak、それ以外は曜日×季節で4分類。"""
    if list(events):
        return "peak"
    is_weekend = day.weekday() >= 4 or day in holidays  # 金土 or 祝日
    is_high = day.month in high_season_months
    if is_weekend:
        return "weekend_high" if is_high else "weekend_low"
    return "weekday_high" if is_high else "weekday_low"


def event_score(event_ids: Iterable[str], policy: dict[str, Any]) -> tuple[float, list[str]]:
    """イベントリフトの合成。Σ lift_e × relevance_e を返す。"""
    table = {e["id"]: e for e in policy.get("events", [])}
    total, names = 0.0, []
    for eid in event_ids:
        e = table.get(eid)
        if not e:
            continue
        total += float(e["lift"]) * float(e.get("relevance", 1.0))
        names.append(str(e["name"]))
    return total, names


def _z(value: float, center: float, scale: float) -> float:
    """簡易標準化。本番では同一seg内のロバストz（中央値・MAD）に置き換える。"""
    if scale <= 0:
        return 0.0
    return clip((value - center) / scale, -3.0, 3.0)


def pace_index(own: OwnState) -> float:
    """ペース指数 = 現在OTB / (最終予測 × ブッキングカーブ)。
    τ>180 では分母が不安定なため 1.0 固定とする。"""
    if own.lead_days > 180:
        return 1.0
    denom = max(own.expected_final_rooms * own.booking_curve, 1e-6)
    return clip(own.rooms_sold / denom, 0.0, 3.0)


def uncertainty(comp: CompsetSnapshot, own: OwnState, event_present: bool) -> float:
    """不確実性スコア σ。大きいほど推奨価格を基準価格へ引き戻す。"""
    sigma = 0.0
    sigma += (1.0 - comp.coverage) * 1.2  # 網羅率が低い
    sigma += 0.5 if comp.freshness_hours > 24 else 0.0  # データが古い
    sigma += 0.4 if own.lead_days > 120 else 0.0  # 超長期
    sigma += 0.3 if event_present else 0.0  # イベント係数は未検証度が高い
    if not comp.priced:
        sigma += 1.5  # 価格が1件も取れていない
    return sigma


def net_adr(price: float, channel: str, policy: dict[str, Any]) -> float:
    """純ADR（手数料控除後）。全ての意思決定はこの値で行う。"""
    fee = float(policy["channels"][channel]["fee"])
    return price * (1.0 - fee)


# ---------------------------------------------------------------------------
# 中核: 推奨価格の算定
# ---------------------------------------------------------------------------
def recommend_rate(
    comp: CompsetSnapshot,
    own: OwnState,
    policy: dict[str, Any],
    *,
    event_ids: Sequence[str] = (),
    holidays: frozenset[dt.date] = frozenset(),
    adjacent_demand_ratio: float = 1.0,
) -> Recommendation:
    """1宿泊日ぶんの推奨ADR・最低宿泊日数・チャネル制御を算出する。"""
    total_rooms = int(policy["property"]["total_rooms"])
    seg = classify_segment(own.stay_date, events=event_ids, holidays=holidays)
    base = float(policy["bar_base"][seg])
    sens = policy["sensitivity"]
    guard = policy["guardrails"]
    warnings: list[str] = []

    rooms_left = total_rooms - own.rooms_sold
    pace = pace_index(own)
    ev_lift, ev_names = event_score(event_ids, policy)

    # --- 需要圧力指数 DPI -------------------------------------------------
    w = policy["dpi_weights"]
    dpi = (
        w["so"] * _z(comp.sold_out_ratio, 0.35, 0.25)
        + w["dp"] * _z(comp.price_trend_7d, 0.0, 0.08)
        + w["pace"] * _z(pace, 1.0, 0.35)
        + w["ev"] * _z(ev_lift, 0.0, 0.20)
        + w["mkt"] * own.market_index_z
    )

    # --- 乗数分解 ---------------------------------------------------------
    f_pace = clip(1 + float(sens["alpha_pace"]) * (pace - 1.0), 0.88, 1.35)

    if comp.median is not None:
        target = comp.median * float(policy["objectives"]["target_ari"]) / 100.0
        f_comp = clip(1 + float(sens["beta_comp"]) * (target / base - 1.0), 0.90, 1.25)
    else:
        f_comp = 1.0
        warnings.append("競合価格が1件も取得できていません")

    f_event = clip(1.0 + ev_lift, 1.00, 1.60)

    ladder = {int(k): float(v) for k, v in policy["inventory_ladder"].items()}
    f_inv = ladder.get(rooms_left, 1.0) if pace >= 0.9 else 1.0

    f_lead = _lead_factor(own.lead_days, pace)

    raw = base * f_pace * f_comp * f_event * f_inv * f_lead

    # --- 縮小推定 ---------------------------------------------------------
    sigma = uncertainty(comp, own, bool(event_ids))
    kappa = 1.0 / (1.0 + float(sens["lambda_shrink"]) * sigma)
    price = base + kappa * (raw - base)

    # --- ガードレール -----------------------------------------------------
    floor = max(float(policy["floors"]["brand_floor"]), float(policy["floors"]["variable_cost"]))
    ceil = float(policy["ceilings"][seg])
    price = clip(price, floor, ceil)

    max_chg = float(guard["max_daily_change"])
    price = clip(price, own.current_rate * (1 - max_chg), own.current_rate * (1 + max_chg))

    price = _apply_rate_integrity_rule(price, own, rooms_left, pace, policy, warnings)
    price = clip(price, floor, ceil)
    price = round_to_ladder(price, int(guard["rate_ladder"]))

    # --- サニティチェック -------------------------------------------------
    if comp.coverage < float(guard["min_compset_coverage"]):
        warnings.append(f"コンプセット網羅率不足 ({comp.coverage:.0%})")
    if comp.freshness_hours > float(guard["max_data_age_hours"]):
        warnings.append(f"データ鮮度超過 ({comp.freshness_hours:.0f}h)")
    change = abs(price / own.current_rate - 1.0) if own.current_rate > 0 else 1.0
    if change > 0.50:
        warnings.append("前日比±50%超 — 収集バグの可能性。配信をブロック")

    auto = (
        change <= float(guard["auto_publish_band"])
        and not warnings
    )

    # --- 在庫制限とチャネル制御 -------------------------------------------
    min_los = _decide_min_los(own, comp, policy, adjacent_demand_ratio, dpi)
    gating = _channel_gating(dpi, policy)

    return Recommendation(
        stay_date=own.stay_date,
        segment=seg,
        current_price=own.current_rate,
        recommended_price=price,
        min_los=min_los,
        factors={
            "base": base,
            "pace": f_pace,
            "comp": f_comp,
            "event": f_event,
            "inventory": f_inv,
            "lead": f_lead,
            "shrinkage": kappa,
        },
        evidence={
            "compset_median": f"{comp.median:,.0f}円" if comp.median else "取得なし",
            "sold_out_ratio": comp.sold_out_ratio,
            "coverage": comp.coverage,
            "rooms_left": rooms_left,
            "total_rooms": total_rooms,
            "pace": pace,
            "lead_days": own.lead_days,
            "events": ev_names,
            "net_adr_direct": net_adr(price, "direct", policy),
            "net_adr_ota_high": net_adr(price, "expedia", policy),
        },
        dpi=dpi,
        auto_publish=auto,
        warnings=warnings,
        channel_gating=gating,
    )


def _lead_factor(lead_days: int, pace: float) -> float:
    """リードタイム係数。長期は中立、直前は変動を抑制する。
    直前の安易な値下げはブッキングカーブを恒久的に後ろ倒しにするため行わない。"""
    if lead_days > 60:
        return 1.00
    if lead_days > 21:
        return 1.00 if pace >= 0.9 else 0.98
    if pace >= 1.2:
        return 1.05  # 直前で好調 = 希少性が実証されている
    return 1.00


def _apply_rate_integrity_rule(
    price: float,
    own: OwnState,
    rooms_left: int,
    pace: float,
    policy: dict[str, Any],
    warnings: list[str],
) -> float:
    """値下げ禁止則。直前の値下げは条件をすべて満たす場合のみ、限定幅で許容する。"""
    ri = policy["rate_integrity"]
    if own.lead_days > int(ri["no_discount_within_days"]):
        return price
    if price >= own.current_rate:
        return price

    allowed = (
        rooms_left >= int(ri["emergency_discount_min_rooms_left"])
        and pace < float(ri["emergency_discount_max_pace"])
    )
    if not allowed:
        warnings.append("値下げ禁止則により据置（価値提供で対応）")
        return own.current_rate

    floor_by_rule = own.current_rate * (1 - float(ri["emergency_discount_max"]))
    return max(price, floor_by_rule)


def _decide_min_los(
    own: OwnState,
    comp: CompsetSnapshot,
    policy: dict[str, Any],
    adjacent_demand_ratio: float,
    dpi: float,
) -> int:
    """逸失分析に基づく MinLOS 判定。
    需要過多の日に1泊客を入れて前後日を空室固定させないための制御。"""
    cfg = policy["min_los"]
    if own.lead_days < int(cfg["min_lead_days"]):
        return 1
    demand_ratio = 1.0 + dpi + comp.sold_out_ratio
    if (
        demand_ratio >= float(cfg["demand_ratio_trigger"])
        and adjacent_demand_ratio <= float(cfg["adjacent_soft_max"])
    ):
        return 2
    return 1


def _channel_gating(dpi: float, policy: dict[str, Any]) -> dict[str, str]:
    """需要が逼迫した日は高手数料チャネルを絞り、純RevPARを高める。"""
    threshold = float(policy.get("high_demand_dpi_threshold", 0.8))
    gating: dict[str, str] = {}
    for name, cfg in policy["channels"].items():
        if dpi >= threshold and float(cfg["fee"]) >= 0.15:
            gating[name] = "premium_only"  # 上位料金のみ提供／在庫を絞る
        else:
            gating[name] = "open"
    return gating


# ---------------------------------------------------------------------------
# 事後分析
# ---------------------------------------------------------------------------
def detect_early_sellout(stay_date: dt.date, sold_out_on: dt.date, threshold_days: int = 45) -> bool:
    """早期完売＝安売りの検知。翌年同条件日の基準価格引き上げ候補に上程する。"""
    return (stay_date - sold_out_on).days >= threshold_days


def is_holdout_date(day: dt.date, ratio: float = 0.09, salt: int = 20260801) -> bool:
    """効果検証用のホールドアウト日（ポリシー凍結日）を決定的に選ぶ。
    最繁忙日は呼び出し側で除外すること（機会損失が大きすぎるため）。"""
    h = (day.toordinal() * 2654435761 + salt) % 1000
    return h < ratio * 1000
