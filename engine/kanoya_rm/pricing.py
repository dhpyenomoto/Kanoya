"""価格推奨エンジン本体.

  log P = log(P_base) + b_demand·z_demand + b_comp·z_comp
                      + b_event·z_event + b_lead·z_lead

対数加法モデルを採用する理由:
  ・各要因の寄与が乗数として独立に解釈でき、円単位のウォーターフォールに分解できる
  ・係数が「寄与率」として現場に説明可能（ブラックボックスAIにしない）
  ・要因の追加・削除が既存挙動を壊さない

**内部需要シグナルの統合（2026-09 の設計変更）**

以前は「予約ペース(b_pace=0.42)」と「残室希少性(b_remain=0.34)」を
別々の項として持っていた。しかしどちらも otb_rooms の線形関数で符号も同じ、
つまり同一の変数に二重に係数がかかっていた。実効重み 0.76 は
競合ポジション(0.32)の2倍以上であり、意図した設計ではない。

実測（感度分析ツール scripts/sensitivity.py）:
  OTB 0室→満室 でモデル出力が 2.74〜3.06倍 動いていた。
  5室規模の予約1件は偶然の範囲であり、需要の反映ではなくノイズの増幅である。
  さらに60行中45行(75%)でガードレールがモデル出力を上書きしており、
  「モデルではなくガードレールが価格を決めている」状態だった。

上記2項を pace.py 側で単一の内部需要シグナル z_demand へ統合し、
実効重みを b_comp(0.32) 以下（b_demand=0.30）に抑えた。
各項の対数寄与は term_clip(±0.28) で制限されるため、
1項に統合したことで内部需要由来の振れ幅は自動的に e^0.56 = 1.75倍 が上限になる。

最終価格は必ず guardrails を通す（貢献利益フロア／天井／日次変動幅／丸め）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from .compset import CompSnapshot, detect_market_event
from .config import Settings
from .pace import PaceResult


@dataclass
class Contribution:
    factor: str
    z: float
    log_adj: float
    yen: float
    note: str = ""


@dataclass
class Recommendation:
    stay_date: date
    day_class: str
    season: str
    season_label: str
    dow: str
    base_rate: float
    raw_price: float
    recommended_rate: float
    current_rate: float
    delta_pct: float
    action: str                 # AUTO_APPLY / APPROVAL_REQUIRED / REJECTED_ANOMALY
    guardrail_notes: list[str] = field(default_factory=list)
    contributions: list[Contribution] = field(default_factory=list)
    comp_median: float = 0.0
    comp_position: float = 0.0  # 自社推奨 ÷ 競合中央値
    remaining: int = 0
    lead_days: int = 0
    event_label: str = ""
    event_score: float = 0.0
    mlos: int = 1
    gap_night: bool = False


# ---- 基準価格 ----------------------------------------------------------


def base_rate(settings: Settings, day: date) -> tuple[float, str, str, str]:
    cfg = settings.property["base"]
    season, label = settings.season_of(day)
    dow = settings.dow_of(day)
    rate = float(cfg["anchor_room_rate"])
    rate *= float(cfg["season_factor"][season])
    rate *= float(cfg["dow_factor"][dow])
    if settings.is_holiday_eve(day):
        rate *= 1.0 + float(cfg["holiday_eve_bonus"])
    return rate, season, label, dow


def lead_adjustment(settings: Settings, lead_days: int) -> float:
    for bucket in settings.property["lead_time_curve"]["buckets"]:
        if bucket["min_days"] <= lead_days <= bucket["max_days"]:
            return float(bucket["adj"])
    return 0.0


# ---- 本体 --------------------------------------------------------------


def recommend(
    settings: Settings,
    day: date,
    pace: PaceResult,
    snap: CompSnapshot | None,
    comp_baseline: float,
    current_rate: float,
) -> Recommendation:
    coef = settings.property["coefficients"]
    guard = settings.property["guardrails"]
    clip = float(coef["term_clip"])

    p_base, season, season_label, dow = base_rate(settings, day)

    # --- 各シグナルの z（−1..+1）を求める ---
    # 内部需要（予約ペース × 残室希少性）は pace.py で1本に統合済み
    z_demand = pace.z

    cal_score, cal_label = settings.event_score_of(day)
    event_score, event_label = cal_score, cal_label
    if snap is not None:
        detected, auto_score, auto_label = detect_market_event(settings, snap, comp_baseline)
        if detected and auto_score > event_score:
            event_score, event_label = auto_score, auto_label
    z_event = max(0.0, min(1.0, event_score))

    z_comp = 0.0
    comp_median = snap.weighted_median_nar if snap else 0.0
    if comp_median > 0:
        # 競合中央値に対する基準価格の位置。競合が高ければ上げ余地、安ければ下げ圧力。
        z_comp = max(-1.0, min(1.0, math.log(comp_median / p_base) / 0.30))
        if snap is not None:
            z_comp += 0.5 * snap.pressure          # 市場逼迫は上方バイアス
            z_comp = max(-1.0, min(1.0, z_comp))

    z_lead = lead_adjustment(settings, pace.lead_days) / max(1e-9, float(coef["b_lead"]))
    z_lead = max(-1.0, min(1.0, z_lead))

    terms = [
        ("demand", z_demand, float(coef["b_demand"]), "内部需要（予約ペース×残室希少性）"),
        ("comp",   z_comp,   float(coef["b_comp"]),   "競合ポジション（NAR中央値比）"),
        ("event",  z_event,  float(coef["b_event"]),  "需要イベント"),
        ("lead",   z_lead,   float(coef["b_lead"]),   "リードタイム"),
    ]

    # --- ウォーターフォール（円建て寄与）を作りながら価格を積み上げる ---
    price = p_base
    contributions: list[Contribution] = []
    for name, z, b, note in terms:
        adj = max(-clip, min(clip, b * z))
        after = price * math.exp(adj)
        contributions.append(Contribution(name, z, adj, after - price, note))
        price = after

    raw_price = price

    # --- ガードレール ---
    # 適用順序が重要: 変動幅 → 丸め → 絶対境界。
    # 絶対境界（貢献利益フロア／天井）は最後に効かせ、他のルールが越えられないようにする。
    notes: list[str] = []
    floor = float(guard["floor_room_rate"])
    ceiling = float(guard["ceiling_room_rate"])

    if current_rate > 0:
        max_change = float(guard["max_change_per_day_pct"])
        lo, hi = current_rate * (1 - max_change), current_rate * (1 + max_change)
        if price < lo:
            notes.append(f"日次変動幅 ±{max_change:.0%} で下方制限")
            price = lo
        elif price > hi:
            notes.append(f"日次変動幅 ±{max_change:.0%} で上方制限")
            price = hi

    unit = float(guard["rounding_unit"])
    price = round(price / unit) * unit

    if price < floor:
        notes.append(f"貢献利益フロア {floor:,.0f}円で下限クリップ")
        price = floor
    if price > ceiling:
        notes.append(f"天井 {ceiling:,.0f}円で上限クリップ")
        price = ceiling

    delta = (price / current_rate - 1.0) if current_rate > 0 else 0.0
    hard = float(guard["hard_stop_band_pct"])
    auto = float(guard["auto_apply_band_pct"])
    if abs(delta) > hard:
        action = "REJECTED_ANOMALY"
        notes.append(f"±{hard:.0%} 超のため自動棄却（データ異常疑い）")
    elif abs(delta) <= auto:
        action = "AUTO_APPLY"
    else:
        action = "APPROVAL_REQUIRED"

    return Recommendation(
        stay_date=day,
        day_class=settings.day_class(day),
        season=season,
        season_label=season_label,
        dow=dow,
        base_rate=p_base,
        raw_price=raw_price,
        recommended_rate=price,
        current_rate=current_rate,
        delta_pct=delta,
        action=action,
        guardrail_notes=notes,
        contributions=contributions,
        comp_median=comp_median,
        comp_position=(price / comp_median) if comp_median else 0.0,
        remaining=pace.remaining,
        lead_days=pace.lead_days,
        event_label=event_label,
        event_score=event_score,
    )
