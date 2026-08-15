"""推定稼働率。

  推定販売室数 ＝ 宿泊客由来レビュー数 ÷ 投稿率
  推定稼働率   ＝ 推定販売室数 ÷（客室数 × 日数）

絶対値を信じるのではなく、同じ係数を通した施設間の相対差とモメンタムを読む。
"""

from __future__ import annotations

from datetime import date, timedelta

from .config import Config
from .models import AreaShare, PostingRate, PropertyEstimate
from .timeline import PropertyTimeline


def window_bounds(as_of: date, days: int) -> tuple[date, date]:
    """as_of を末尾に含む days 日間。"""
    return as_of - timedelta(days=days - 1), as_of


def estimate_property(
    config: Config,
    timeline: PropertyTimeline,
    rate: PostingRate,
    as_of: date,
) -> PropertyEstimate:
    days = config.estimation.window_days
    start, end = window_bounds(as_of, days)
    prev_start, prev_end = window_bounds(start - timedelta(days=1), days)

    prop = timeline.property
    capacity = prop.rooms * days

    stay = timeline.stay_total(start, end)
    sold = stay / rate.rate if rate.rate > 0 else 0.0
    occupancy = min(100.0, sold / capacity * 100.0)

    prev_stay = timeline.stay_total(prev_start, prev_end)
    prev_sold = prev_stay / rate.rate if rate.rate > 0 else 0.0
    prev_occupancy = min(100.0, prev_sold / capacity * 100.0) if prev_stay else None

    return PropertyEstimate(
        property=prop,
        stay_reviews=stay,
        raw_reviews=timeline.raw_total(start, end),
        sold_rooms=sold,
        occupancy=occupancy,
        prev_occupancy=prev_occupancy,
        confidence=config.estimation.confidence.level(stay),
        rating=timeline.rating,
    )


def estimate_all(
    config: Config,
    timelines: dict[str, PropertyTimeline],
    rate: PostingRate,
    as_of: date,
) -> list[PropertyEstimate]:
    """稼働の高い順。並べ替えの目的は順位であって、絶対値の表彰ではない。"""
    estimates = [
        estimate_property(config, timelines[prop.key], rate, as_of)
        for prop in config.properties
        if prop.key in timelines
    ]
    return sorted(estimates, key=lambda e: e.occupancy, reverse=True)


def area_share(
    config: Config,
    timelines: dict[str, PropertyTimeline],
    as_of: date,
) -> AreaShare:
    """エリアのクチコミ総量に占める自社の比率。

    母数は追跡中の施設（自社＋コンプセット）の合計。エリア全施設ではない。
    施設を足し引きすると比率が動くので、compset を変えたら過去分と比べない。
    """
    start, end = window_bounds(as_of, config.estimation.window_days)
    total = sum(t.raw_total(start, end) for t in timelines.values())
    own = timelines[config.own.key].raw_total(start, end) if config.own.key in timelines else 0.0
    return AreaShare(total_raw_reviews=total, own_raw_reviews=own)
