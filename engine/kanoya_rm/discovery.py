"""コンペティティブセットの機械的発見とティア分類.

「なんとなくあそこが競合」という感覚を排除し、
距離・価格帯・規模・評判・レビュー量の5軸スコアで候補を生成する。

ただし**完全自動にはしない**。競合認定は事業判断を含むため、
本モジュールは候補と根拠を提示するところまでを担い、
最終確定は四半期レビューで人が承認する（--write で昇格）。

Places API から取得できない情報（客室数・食事形態・課金方式）は
needs_review フラグを立てて明示する。ここを黙って推測で埋めると、
NAR正規化が体系的に狂い、しかもエンジン内では正常値に見えてしまう。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .sources.base import PlaceRecord

PRICE_LEVEL_ORDINAL = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}


@dataclass
class Candidate:
    place: PlaceRecord
    distance_km: float
    scores: dict[str, float] = field(default_factory=dict)
    total: float = 0.0
    tier: str = ""
    flags: list[str] = field(default_factory=list)
    rooms: int | None = None

    @property
    def name(self) -> str:
        return self.place.name


def _price_score(level: str, reference: str) -> tuple[float, bool]:
    """価格帯の近さ。priceLevel 未提供なら中立値＋要確認フラグ."""
    if not level or level not in PRICE_LEVEL_ORDINAL:
        return 0.5, True
    diff = abs(PRICE_LEVEL_ORDINAL[level] - PRICE_LEVEL_ORDINAL.get(reference, 4))
    return max(0.0, 1.0 - diff / 4.0), False


def _reputation_score(rating: float | None, reference: float) -> tuple[float, bool]:
    if rating is None:
        return 0.5, True
    return max(0.0, 1.0 - abs(rating - reference) / 1.5), False


def _review_volume_score(count: int) -> float:
    """レビュー量の対数スケール。多すぎ／少なすぎの両方を減点する."""
    if count <= 0:
        return 0.0
    # 300件前後を基準に、対数距離で減衰
    return max(0.0, 1.0 - abs(math.log10(count) - math.log10(300)) / 1.6)


def _scale_score(rooms: int | None, reference_rooms: int) -> tuple[float, bool]:
    if rooms is None:
        return 0.5, True
    ratio = math.log10(max(1, rooms)) - math.log10(max(1, reference_rooms))
    return max(0.0, 1.0 - abs(ratio) / 1.5), False


def score_candidates(
    places: list[PlaceRecord],
    origin: tuple[float, float],
    config: dict,
    rooms_override: dict[str, int] | None = None,
) -> list[Candidate]:
    disc = config["discovery"]
    scoring = config["scoring"]
    weights = scoring["weights"]
    radius_km = disc["radius_m"] / 1000.0
    rooms_override = rooms_override or {}

    excluded = [p.lower() for p in disc.get("exclude_name_patterns", [])]
    out: list[Candidate] = []

    for place in places:
        name_l = place.name.lower()
        if any(pat in name_l for pat in excluded):
            continue
        if place.review_count < int(disc.get("min_review_count", 0)):
            continue

        distance = place.distance_km(*origin)
        rooms = rooms_override.get(place.place_id) or rooms_override.get(place.name)

        s_dist = max(0.0, 1.0 - distance / max(radius_km, 1e-9))
        s_price, price_unknown = _price_score(place.price_level, scoring["reference_price_level"])
        s_scale, scale_unknown = _scale_score(rooms, int(scoring["reference_rooms"]))
        s_rep, rating_unknown = _reputation_score(place.rating, float(scoring["reference_rating"]))
        s_vol = _review_volume_score(place.review_count)

        scores = {
            "distance": s_dist, "price_band": s_price, "scale": s_scale,
            "reputation": s_rep, "review_volume": s_vol,
        }
        total = sum(scores[k] * float(weights[k]) for k in weights)

        flags: list[str] = []
        if price_unknown:
            flags.append("price_level_unknown")
        if scale_unknown:
            flags.append("rooms_unknown")
        if rating_unknown:
            flags.append("rating_unknown")

        out.append(Candidate(place=place, distance_km=distance, scores=scores,
                             total=total, flags=flags, rooms=rooms))

    out.sort(key=lambda c: -c.total)
    return out[: int(disc.get("max_candidates", 40))]


def assign_tiers(candidates: list[Candidate], config: dict) -> list[Candidate]:
    scoring = config["scoring"]
    th = scoring["tier_thresholds"]
    aspirational_levels = set(scoring.get("aspirational_price_levels", []))

    for c in candidates:
        if c.total >= float(th["PRIMARY"]):
            c.tier = "PRIMARY"
        elif c.total >= float(th["SECONDARY"]):
            c.tier = "SECONDARY"
        elif c.place.price_level in aspirational_levels:
            # スコアは低いが価格帯が明確に上位 → ADR天井の参照点として残す
            c.tier = "ASPIRATIONAL"
        else:
            c.tier = "EXCLUDED"
    return candidates


def to_compset_config(candidates: list[Candidate], origin: tuple[float, float],
                      provenance: dict, overrides: dict | None = None) -> dict:
    """engine が読める compset 形式へ変換する（要確認項目を明示）.

    overrides には人が確定済みの施設属性（客室数・課金方式・食事形態・uplift）を渡す。
    再発見のたびに人手の知見が消えないよう、生成物へマージする。
    """
    overrides = overrides or {}
    selected = [c for c in candidates if c.tier != "EXCLUDED"]
    max_score = max((c.total for c in selected), default=1.0) or 1.0

    competitors = []
    for i, c in enumerate(selected, start=1):
        ov = overrides.get(c.name, {})
        confirmed = bool(ov.get("confirmed"))

        needs_review = list(c.flags)
        if not ov:
            needs_review.append("meal_and_uplift_unset")
        elif not confirmed:
            needs_review.append("overrides_present_but_unconfirmed")

        rooms = ov.get("rooms", c.rooms)
        competitors.append({
            "id": f"auto{i:02d}",
            "name": c.name,
            "place_id": c.place.place_id,
            "tier": c.tier,
            "weight": round(c.total / max_score, 3),
            "rooms": int(rooms) if rooms else 0,
            "distance_km": round(c.distance_km, 2),
            "latitude": c.place.latitude,
            "longitude": c.place.longitude,
            "pricing_basis": ov.get("pricing_basis", "per_room"),
            "_pricing_basis_note": "Google Hotels は室単価で返るため既定は per_room。人数課金の宿は要確認。",
            "meal_included": ov.get("meal_included", "none"),
            "dinner_uplift": float(ov.get("dinner_uplift", 0)),
            "breakfast_uplift": float(ov.get("breakfast_uplift", 0)),
            "_meal_note": "★人手確定項目。誤ると NAR 正規化が体系的に狂い、エンジン内では正常値に見える。",
            "rating": c.place.rating,
            "review_count": c.place.review_count,
            "price_level": c.place.price_level,
            "score_breakdown": {k: round(v, 3) for k, v in c.scores.items()},
            "needs_review": needs_review,
        })

    return {
        "_generated": True,
        "_provenance": provenance,
        "_warning": (
            "自動生成された候補です。そのまま本番投入しないでください。"
            "meal_included / dinner_uplift / breakfast_uplift / rooms を人が確定し、"
            "compset.json へ昇格させてから使用します。"
        ),
        "_normalization": (
            "各社の掲出価格を『1室2名1泊2食・税サ込』に補正するためのパラメータ。"
            "meal_included=none の施設には dinner_uplift/breakfast_uplift を加算し、"
            "pricing_basis=per_person の施設は ×2 する。"
        ),
        "origin": {"latitude": origin[0], "longitude": origin[1]},
        "tiers": {
            "PRIMARY": {"description": "直接競合", "tier_weight": 1.0},
            "SECONDARY": {"description": "価格帯代替", "tier_weight": 0.5},
            "ASPIRATIONAL": {"description": "目標ポジション（ADR天井の参照点）", "tier_weight": 0.3},
        },
        "competitors": competitors,
    }


def diff_against_existing(generated: dict, existing_path: Path) -> dict[str, list[str]]:
    """既存コンペセットとの差分（新規開業・消滅の検知）."""
    if not existing_path.exists():
        return {"added": [c["name"] for c in generated["competitors"]], "removed": [], "kept": []}
    existing = json.loads(existing_path.read_text(encoding="utf-8"))
    old = {c["name"] for c in existing.get("competitors", [])}
    new = {c["name"] for c in generated["competitors"]}
    return {
        "added": sorted(new - old),
        "removed": sorted(old - new),
        "kept": sorted(new & old),
    }
