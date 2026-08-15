"""収集オーケストレーション — 計画に沿って取得し、コンペセットへ突合する.

Google Hotels が返す施設名と、こちらのコンペセット定義の施設名は一致しない
（表記ゆれ・ブランド名・サブタイトル）。名寄せに失敗すると「競合が売止だった」
のか「名寄せに失敗した」のか区別できず、市場逼迫度の判定が壊れる。

そのため:
  ・正規化した名称の類似度 と 座標距離 の両方で突合する
  ・突合できなかったレコードは捨てずに unmatched として記録する
    （新規開業の兆候であり、コンペセット見直しの入力になる）
"""

from __future__ import annotations

import csv
import difflib
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Competitor
from .schedule import Plan
from .sources.base import RateRecord, haversine_km

# 区切り記号・空白のみを落とす。
# 「ホテル」「奈良」等の一般語まで落とすと『奈良ホテル』が空文字になり、
# どの施設とも突合できなくなる（実際に踏んだ不具合）。業態語や地名は
# 施設の識別に効く情報なので残す。
NAME_NOISE = ("（", "）", "(", ")", "〜", "~", "-", "‐", "―", "ー", "・",
              "／", "/", "＆", "&", "、", ",", ".", "。", " ", "　")


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).lower()
    for token in NAME_NOISE:
        text = text.replace(token, "")
    return text or unicodedata.normalize("NFKC", name).lower()


@dataclass
class MatchResult:
    matched: list[tuple[str, RateRecord]] = field(default_factory=list)
    unmatched: list[RateRecord] = field(default_factory=list)


def match_records(records: list[RateRecord], competitors: dict[str, Competitor],
                  *, name_threshold: float = 0.72,
                  max_distance_km: float = 0.6) -> MatchResult:
    """レートレコードをコンペセットへ突合する（名称類似度＋座標距離）."""
    result = MatchResult()
    norm_comp = {cid: normalize_name(c.name) for cid, c in competitors.items()}
    comp_coords = {
        cid: (getattr(c, "latitude", 0.0), getattr(c, "longitude", 0.0))
        for cid, c in competitors.items()
    }

    for record in records:
        target = normalize_name(record.property_name)
        best_id, best_score = "", 0.0

        for cid, cname in norm_comp.items():
            if not cname or not target:
                continue
            score = difflib.SequenceMatcher(None, target, cname).ratio()
            # 部分一致は強いシグナル（サブタイトル付きの表記ゆれを拾う）
            if cname in target or target in cname:
                score = max(score, 0.85)
            # 座標が十分近ければ加点（同名別施設との取り違えを防ぐ）
            lat, lon = comp_coords.get(cid, (0.0, 0.0))
            if lat and lon and record.latitude and record.longitude:
                if haversine_km(lat, lon, record.latitude, record.longitude) <= max_distance_km:
                    score += 0.15
            if score > best_score:
                best_id, best_score = cid, score

        if best_id and best_score >= name_threshold:
            result.matched.append((best_id, record))
        else:
            result.unmatched.append(record)

    return result


@dataclass
class CollectionOutcome:
    rows: list[dict]
    unmatched: list[RateRecord]
    stay_dates: int
    network_calls: int
    cache_hits: int
    fixture: bool


def run(plan: Plan, rate_source, competitors: dict[str, Competitor], *,
        area_query: str, adults: int = 2, los: int = 1,
        on_progress=None) -> CollectionOutcome:
    rows: list[dict] = []
    unmatched: list[RateRecord] = []
    fixture = False

    for i, task in enumerate(plan.tasks, start=1):
        records = rate_source.rates_for_date(
            area_query, task.stay_date, plan.run_date, adults=adults, los=los
        )
        fixture = fixture or any(r.is_fixture for r in records)
        match = match_records(records, competitors)
        unmatched.extend(match.unmatched)

        seen: set[str] = set()
        for comp_id, record in match.matched:
            if comp_id in seen:      # 同一施設の複数掲出は最安のみ採用
                continue
            seen.add(comp_id)
            rows.append({
                "snapshot_date": plan.run_date.isoformat(),
                "stay_date": task.stay_date.isoformat(),
                "comp_id": comp_id,
                "source": record.source,
                "channel": record.channel,
                "raw_rate": int(record.raw_rate) if record.raw_rate else 0,
                "total_rate": int(record.total_rate) if record.total_rate else 0,
                "currency": record.currency,
                "available": 1 if (record.available and record.raw_rate) else 0,
                "adults": record.adults,
                "los": record.los,
                "rating": record.rating if record.rating is not None else "",
                "review_count": record.review_count,
                "lead_days": task.lead_days,
                "collection_tier": task.reason,
                "property_name": record.property_name,
                "is_fixture": 1 if record.is_fixture else 0,
            })

        # 計画に含まれるが結果に現れなかった施設は「売止」として明示的に記録する。
        # 欠測と売止を区別しないと、市場逼迫度の判定が壊れる。
        for comp_id in competitors:
            if comp_id not in seen:
                rows.append({
                    "snapshot_date": plan.run_date.isoformat(),
                    "stay_date": task.stay_date.isoformat(),
                    "comp_id": comp_id,
                    "source": getattr(rate_source, "name", "unknown"),
                    "channel": "",
                    "raw_rate": 0, "total_rate": 0, "currency": "JPY",
                    "available": 0,
                    "adults": adults, "los": los,
                    "rating": "", "review_count": 0,
                    "lead_days": task.lead_days,
                    "collection_tier": task.reason,
                    "property_name": competitors[comp_id].name,
                    "is_fixture": 1 if fixture else 0,
                })

        if on_progress:
            on_progress(i, len(plan.tasks), task.stay_date)

    client = getattr(rate_source, "client", None)
    return CollectionOutcome(
        rows=rows,
        unmatched=unmatched,
        stay_dates=len(plan.tasks),
        network_calls=getattr(client, "network_calls", 0),
        cache_hits=getattr(client, "cache_hits", 0),
        fixture=fixture,
    )


FIELDNAMES = [
    "snapshot_date", "stay_date", "comp_id", "source", "channel",
    "raw_rate", "total_rate", "currency", "available", "adults", "los",
    "rating", "review_count", "lead_days", "collection_tier",
    "property_name", "is_fixture",
]


def write_rows(rows: list[dict], path: Path, *, append: bool = True) -> None:
    """fact_comp_rate 相当のCSVへ追記する（生データは上書きしない）."""
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    mode = "a" if (append and exists) else "w"
    with path.open(mode, encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        if mode == "w":
            writer.writeheader()
        writer.writerows(rows)
