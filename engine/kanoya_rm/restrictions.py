"""在庫制約（MLOS）と空隙（gap night）最適化.

5室規模では、価格変更よりも滞在制約の設計のほうが収益インパクトが大きい局面がある。
  ・繁忙日に1泊予約で埋まると、前後日に売れ残りが生じる（連泊需要の取りこぼし）
  ・逆に予約カレンダー上に1泊だけの空隙が残ると、そこは連泊客では埋まらない

前者は MLOS（最低宿泊数）で、後者は gap night 検知＋非価格特典で対処する。
"""

from __future__ import annotations

from datetime import date, timedelta

from .config import Settings
from .pricing import Recommendation


def apply_mlos(settings: Settings, recs: dict[date, Recommendation]) -> None:
    """需要が強い日に最低宿泊数を設定する（在庫の断片化を防ぐ）."""
    cfg = settings.property["restrictions"]
    trig_event = float(cfg["mlos_trigger_event_score"])
    trig_press = float(cfg["mlos_trigger_comp_pressure"])
    nights = int(cfg["mlos_nights"])

    for day, rec in recs.items():
        strong_event = rec.event_score >= trig_event
        strong_market = rec.comp_position > 0 and rec.comp_position >= 1.15
        tight = rec.remaining <= 2 and rec.lead_days >= 7
        if (strong_event or strong_market) and tight:
            rec.mlos = nights
            rec.guardrail_notes.append(f"MLOS {nights}泊を設定（在庫断片化の防止）")


def detect_gap_nights(settings: Settings, recs: dict[date, Recommendation]) -> None:
    """前後日が満室に近く、当日だけ空いている『1泊の空隙』を検知する."""
    rooms = int(settings.property["property"]["rooms"])
    for day, rec in recs.items():
        prev_rec = recs.get(day - timedelta(days=1))
        next_rec = recs.get(day + timedelta(days=1))
        if prev_rec is None or next_rec is None:
            continue
        neighbours_tight = (
            prev_rec.remaining <= max(1, rooms // 5)
            and next_rec.remaining <= max(1, rooms // 5)
        )
        if neighbours_tight and rec.remaining >= 2 and rec.lead_days <= 21:
            rec.gap_night = True
            rec.mlos = 1
            rec.guardrail_notes.append(
                "gap night: MLOS解除＋非価格特典（貸切風呂・アップグレード）で充当"
            )
