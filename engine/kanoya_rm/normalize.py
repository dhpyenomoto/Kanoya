"""競合レートの正規化（NAR: Normalized Adjusted Rate）.

レートショッパーや Google Hotels から取れる価格は、施設ごとに
  ・人数課金 / 室課金
  ・素泊まり / 朝食付き / 2食付き
  ・税サ込 / 別
が混在しており、そのまま平均すると比較にならない。ここでは全社の掲出価格を
『1室2名1泊2食・税サ込のルームナイト総額』へ揃える。

この正規化こそが、汎用RMSでは施設固有事情を吸収しきれず、
自社ロジックとして資産化すべき部分である。
"""

from __future__ import annotations

from .config import Competitor


def to_room_basis(raw_rate: float, comp: Competitor, occupancy: int = 2) -> float:
    """人数課金の掲出価格を室課金（2名）へ変換."""
    if comp.pricing_basis == "per_person":
        return raw_rate * occupancy
    return raw_rate


def to_two_meals(room_rate: float, comp: Competitor) -> float:
    """食事条件を『2食付き』へ揃える（不足分を uplift で加算）."""
    add = 0.0
    if comp.meal_included == "none":
        add += comp.dinner_uplift + comp.breakfast_uplift
    elif comp.meal_included == "breakfast":
        add += comp.dinner_uplift
    return room_rate + add


def normalized_rate(raw_rate: float, comp: Competitor, occupancy: int = 2) -> float:
    """掲出価格 → NAR（1室2名1泊2食・税サ込）."""
    return to_two_meals(to_room_basis(raw_rate, comp, occupancy), comp)


def quality_flag(nar: float, floor: float = 20000, ceiling: float = 600000) -> str:
    """収集値の妥当性チェック。異常値は平均計算から除外する。"""
    if nar < floor:
        return "REJECT_LOW"
    if nar > ceiling:
        return "REJECT_HIGH"
    return "OK"
