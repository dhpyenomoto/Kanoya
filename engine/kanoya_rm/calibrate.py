"""基準価格（アンカー）の自動校正.

『基準価格をいくらに置くか』は従来レベニューマネージャーの経験に依存してきた、
最も属人的な意思決定である。本モジュールはこれを次の2条件から機械的に解く。

  条件A（市場整合）: 標準期・平日の基準価格が、競合NAR中央値の target_position 倍
  条件B（収益整合）: 想定稼働のもとで年間 RevPAR 目標を満たす

両者の解を突き合わせ、乖離があれば「市場が許容する価格」と「事業計画が要求する価格」の
ギャップとして経営に上げる。ここを暗黙に埋めてしまうのが属人化の温床であるため、
エンジンは埋めずに可視化する。

四半期に1度、本スクリプトの出力をもって anchor_room_rate を更新する運用とする。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .compset import CompSnapshot
from .config import Settings
from .pricing import base_rate


@dataclass
class CalibrationResult:
    current_anchor: float
    market_anchor: float          # 条件A の解
    budget_anchor: float          # 条件B の解
    comp_median_overall: float
    implied_position: float
    recommended_anchor: float
    gap_pct: float
    verdict: str


def market_anchor(settings: Settings, snapshots: dict[date, CompSnapshot],
                  target_position: float) -> tuple[float, float]:
    """競合NAR中央値に対する目標ポジションから逆算したアンカー."""
    ratios: list[float] = []
    medians: list[float] = []
    for day, snap in snapshots.items():
        if snap.weighted_median_nar <= 0 or snap.sample_size < 3:
            continue
        p_base, _, _, _ = base_rate(settings, day)
        ratios.append(p_base / snap.weighted_median_nar)
        medians.append(snap.weighted_median_nar)
    if not ratios:
        return float(settings.property["base"]["anchor_room_rate"]), 0.0
    ratios.sort()
    medians.sort()
    current_ratio = ratios[len(ratios) // 2]
    anchor = float(settings.property["base"]["anchor_room_rate"])
    return anchor * (target_position / current_ratio), medians[len(medians) // 2]


def budget_anchor(settings: Settings, target_revpar: float,
                  assumed_occupancy: float, start: date, days: int = 365) -> float:
    """RevPAR目標と想定稼働から逆算したアンカー.

    年間の基準価格プロファイル（季節×曜日）の平均が、目標ADR に一致するよう解く。
    目標ADR = 目標RevPAR ÷ 想定稼働。
    """
    anchor = float(settings.property["base"]["anchor_room_rate"])
    profile = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        p_base, _, _, _ = base_rate(settings, day)
        profile.append(p_base / anchor)
    mean_profile = sum(profile) / len(profile)
    target_adr = target_revpar / max(1e-9, assumed_occupancy)
    return target_adr / mean_profile


def calibrate(settings: Settings, snapshots: dict[date, CompSnapshot], start: date,
              target_position: float = 1.15, target_revpar: float = 62000,
              assumed_occupancy: float = 0.72) -> CalibrationResult:
    current = float(settings.property["base"]["anchor_room_rate"])
    m_anchor, comp_median = market_anchor(settings, snapshots, target_position)
    b_anchor = budget_anchor(settings, target_revpar, assumed_occupancy, start)

    gap = b_anchor / m_anchor - 1.0 if m_anchor else 0.0
    if abs(gap) <= 0.05:
        verdict = "整合。市場・予算とも同水準のアンカーを支持。"
        recommended = (m_anchor + b_anchor) / 2
    elif gap > 0:
        verdict = ("予算が市場を上回る。価格だけでは目標未達。"
                   "商品価値（食事・体験・アップセル）か稼働前提の見直しが必要。")
        recommended = m_anchor  # 市場を無視した値付けはしない
    else:
        verdict = "市場が予算を上回る。値上げ余地あり。段階的な引き上げを推奨。"
        recommended = m_anchor

    recommended = round(recommended / 1000) * 1000
    return CalibrationResult(
        current_anchor=current,
        market_anchor=round(m_anchor / 1000) * 1000,
        budget_anchor=round(b_anchor / 1000) * 1000,
        comp_median_overall=comp_median,
        implied_position=(current / comp_median) if comp_median else 0.0,
        recommended_anchor=recommended,
        gap_pct=gap,
        verdict=verdict,
    )
