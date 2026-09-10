"""内部需要シグナル — 自社の売れ行きを1本の指標に束ねる.

5室規模では日別の予約実績は分散が大きく、単日の前年同日比では判断できない。
シーズン×曜日の『日カテゴリ』へプールしたベンチマークカーブに対する乖離を見る。
これは小規模施設向けRMの中核的な統計処理（縮小推定の実務版）である。

**なぜ1本に束ねるのか（2026-09 の設計変更）**

以前は「予約ペース」と「残室希少性」を別々の項として持っていた。

    z_pace   = (otb_rooms − expected_rooms) / 1.5
    z_remain = (2 × otb_rooms / rooms − 1) × damp

しかしどちらも otb_rooms の線形関数で、符号も同じである。
つまり b_pace(0.42) と b_remain(0.34) が同一の変数に二重にかかっており、
内部稼働シグナルの実効重みが 0.76 と、競合ポジション(0.32)の2倍以上あった。

その結果、5室の施設で予約1件が入るだけでモデル出力が3倍動いていた
（実測: OTB 0室→満室 で 2.74〜3.06倍）。5室規模の予約1件は偶然の
範囲であり、これは需要の反映ではなくノイズの増幅である。

そこで両者を統合し、実効重みを b_comp(0.32) 以下に抑える。
統合シグナルは次の2要素を持つ:

  ① ベンチマーク比の進捗   売れ行きが速いか遅いか（残室そのものではなく）
  ② リードタイム減衰       遠い日付では効かせない

②は以前 z_remain だけが持ち z_pace が持っておらず、
「120日先の予約1件」と「明日の予約1件」を同じ強さで扱っていた。
統合にあたり、この不整合も解消する。

減衰はゼロには落とさない（far_floor）。完全にゼロにすると遠い日付で
満室に近づいても価格が1円も動かなくなり、繁忙日の取りこぼしになる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# 設定に無いときの既定値。従来の挙動と同じ飽和点・減衰開始点にしてある。
DEFAULT_SATURATION_ROOMS = 1.5
DEFAULT_FULL_EFFECT_DAYS = 21      # ここまでは減衰なし
DEFAULT_HALF_LIFE_DAYS = 30        # 以降、この日数ごとに半減
DEFAULT_FAR_FLOOR = 0.35           # 遠い日付でも残す最低限の効き


@dataclass
class PaceResult:
    lead_days: int
    otb_rooms: int
    rooms: int
    expected_ratio: float
    expected_rooms: float
    gap_rooms: float       # 実OTB − 期待OTB（室）
    raw_z: float           # 減衰前の進捗シグナル（−1..+1）
    damping: float         # 適用したリードタイム減衰（0..1）
    z: float               # 統合内部需要シグナル（−1..+1）= raw_z × damping
    remaining: int


def _demand_config(settings) -> dict:
    coef = settings.property.get("coefficients", {})
    return {
        "saturation": float(coef.get("pace_saturation_rooms",
                                     DEFAULT_SATURATION_ROOMS)),
        "full_days": float(coef.get("demand_lead_full_effect_days",
                                    DEFAULT_FULL_EFFECT_DAYS)),
        "half_life": float(coef.get("demand_lead_half_life_days",
                                    DEFAULT_HALF_LIFE_DAYS)),
        "far_floor": float(coef.get("demand_lead_far_floor",
                                    DEFAULT_FAR_FLOOR)),
    }


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


def lead_damping(lead_days: int, *, full_days: float = DEFAULT_FULL_EFFECT_DAYS,
                 half_life: float = DEFAULT_HALF_LIFE_DAYS,
                 far_floor: float = DEFAULT_FAR_FLOOR) -> float:
    """リードタイムによる内部需要シグナルの減衰（1.0 → far_floor）.

    直近は自社の売れ行きが最も確かな需要情報だが、120日先の予約1件は
    ほぼ偶然である。指数減衰で滑らかに落とす。

    far_floor でゼロ手前に留めるのは、遠い日付でも満室に近づけば
    上げられるようにするため。ゼロにすると繁忙日を取りこぼす。
    """
    over = max(0.0, lead_days - full_days)
    if over <= 0:
        return 1.0
    decay = 0.5 ** (over / max(1e-9, half_life))
    return far_floor + (1.0 - far_floor) * decay


def evaluate(settings, day: date, snapshot_date: date, otb_rooms: int) -> PaceResult:
    rooms = int(settings.property["property"]["rooms"])
    lead = (day - snapshot_date).days
    ratio = expected_ratio(settings, day, lead)
    cfg = _demand_config(settings)

    # 当該日カテゴリの想定最終稼働（シーズン別の目標稼働）
    season, _ = settings.season_of(day)
    target_occ = {"PEAK": 0.95, "HIGH": 0.88, "SHOULDER": 0.75, "LOW": 0.62, "DEEP_LOW": 0.50}[season]
    expected_rooms = rooms * target_occ * ratio
    gap = otb_rooms - expected_rooms

    # 5室では 1室=20pt。gap を室数で割らず、飽和点（既定1.5室）で標準化する。
    raw = max(-1.0, min(1.0, gap / max(1e-9, cfg["saturation"])))
    damp = lead_damping(lead, full_days=cfg["full_days"],
                        half_life=cfg["half_life"], far_floor=cfg["far_floor"])

    return PaceResult(
        lead_days=lead,
        otb_rooms=otb_rooms,
        rooms=rooms,
        expected_ratio=ratio,
        expected_rooms=expected_rooms,
        gap_rooms=gap,
        raw_z=raw,
        damping=damp,
        z=max(-1.0, min(1.0, raw * damp)),
        remaining=max(0, rooms - otb_rooms),
    )
