"""ブッキングペース（予約進捗）の評価.

5室規模では日別の予約実績は分散が大きく、単日の前年同日比では判断できない。
シーズン×曜日の『日カテゴリ』へプールしたベンチマークカーブに対する乖離を見る。
これは小規模施設向けRMの中核的な統計処理（縮小推定の実務版）である。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class PaceResult:
    lead_days: int
    otb_rooms: int
    rooms: int
    expected_ratio: float
    expected_rooms: float
    gap_rooms: float       # 実OTB − 期待OTB（室）
    z: float               # 標準化した乖離（−1..+1）
    remaining: int


def expected_ratio(settings, day: date, lead_days: int) -> float:
    """当該日カテゴリ・リードタイムにおける『あるべきOTB比率』."""
    bench = settings.calendar["pace_benchmark"]
    curve = sorted(bench["curve"], key=lambda p: p["lead_days"])
    lead = max(0, lead_days)

    if lead >= curve[-1]["lead_days"]:
        ratio = curve[-1]["ratio"]
    else:
        ratio = curve[0]["ratio"]
        for lo, hi in zip(curve, curve[1:]):
            if lo["lead_days"] <= lead <= hi["lead_days"]:
                span = hi["lead_days"] - lo["lead_days"]
                t = 0.0 if span == 0 else (lead - lo["lead_days"]) / span
                ratio = lo["ratio"] + t * (hi["ratio"] - lo["ratio"])
                break

    season, _ = settings.season_of(day)
    mult = float(bench["season_multiplier"].get(season, 1.0))
    return min(1.0, ratio * mult)


def evaluate(settings, day: date, snapshot_date: date, otb_rooms: int) -> PaceResult:
    rooms = int(settings.property["property"]["rooms"])
    lead = (day - snapshot_date).days
    ratio = expected_ratio(settings, day, lead)

    # 当該日カテゴリの想定最終稼働（シーズン別の目標稼働）
    season, _ = settings.season_of(day)
    target_occ = {"PEAK": 0.95, "HIGH": 0.88, "SHOULDER": 0.75, "LOW": 0.62, "DEEP_LOW": 0.50}[season]
    expected_rooms = rooms * target_occ * ratio
    gap = otb_rooms - expected_rooms

    # 5室では 1室=20pt。gap を室数で割らず「1.5室」を飽和点として標準化する。
    z = max(-1.0, min(1.0, gap / 1.5))

    return PaceResult(
        lead_days=lead,
        otb_rooms=otb_rooms,
        rooms=rooms,
        expected_ratio=ratio,
        expected_rooms=expected_rooms,
        gap_rooms=gap,
        z=z,
        remaining=max(0, rooms - otb_rooms),
    )


def remaining_z(pace: PaceResult) -> float:
    """残室の希少性。売り切れ間際は上げ、大量に余っていれば下げる方向へ."""
    if pace.rooms == 0:
        return 0.0
    sold_through = pace.otb_rooms / pace.rooms
    # 0室売れ=-1、満室=+1 の線形。ただしリードタイムが長いうちは効かせない。
    raw = 2.0 * sold_through - 1.0
    damp = 1.0 if pace.lead_days <= 21 else max(0.0, 1.0 - (pace.lead_days - 21) / 60.0)
    return max(-1.0, min(1.0, raw * damp))
