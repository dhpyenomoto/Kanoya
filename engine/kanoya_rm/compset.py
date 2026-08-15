"""コンペティティブセットの集計と市場圧力指標."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import Settings
from .normalize import normalized_rate, quality_flag


@dataclass
class CompSnapshot:
    stay_date: date
    sample_size: int
    weighted_median_nar: float
    p25_nar: float
    p75_nar: float
    soldout_ratio: float          # 売止（在庫なし）施設の重み付き比率
    pressure: float               # 0..1 の市場逼迫度
    detail: list[tuple[str, float, bool]]  # (comp_id, NAR, available)


def _weighted_quantile(pairs: list[tuple[float, float]], q: float) -> float:
    """(値, 重み) のリストから重み付き分位点を返す."""
    if not pairs:
        return 0.0
    pairs = sorted(pairs, key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        return pairs[len(pairs) // 2][0]
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= q * total:
            return value
    return pairs[-1][0]


def build_snapshot(
    settings: Settings,
    stay_date: date,
    rows: list[dict[str, str]],
) -> CompSnapshot:
    """当該滞在日の競合レート行から集計スナップショットを作る.

    rows: comp_id / raw_rate / available を持つ最新スナップショットの行。
    """
    tiers = settings.compset["tiers"]
    priced: list[tuple[float, float]] = []
    detail: list[tuple[str, float, bool]] = []
    weight_total = 0.0
    soldout_weight = 0.0

    for row in rows:
        comp = settings.competitors.get(row["comp_id"])
        if comp is None:
            continue
        tier_weight = float(tiers[comp.tier]["tier_weight"])
        weight = comp.weight * tier_weight
        available = row.get("available", "1") == "1"
        weight_total += weight
        if not available:
            soldout_weight += weight
            detail.append((comp.id, 0.0, False))
            continue

        nar = normalized_rate(float(row["raw_rate"]), comp)
        if quality_flag(nar) != "OK":
            continue
        priced.append((nar, weight))
        detail.append((comp.id, nar, True))

    soldout_ratio = (soldout_weight / weight_total) if weight_total else 0.0
    median = _weighted_quantile(priced, 0.5)
    p25 = _weighted_quantile(priced, 0.25)
    p75 = _weighted_quantile(priced, 0.75)

    # 市場逼迫度: 売止比率と価格分布の上方シフトを合成（0..1）
    spread = (p75 / median - 1.0) if median else 0.0
    pressure = min(1.0, max(0.0, 0.75 * soldout_ratio + 0.9 * spread))

    return CompSnapshot(
        stay_date=stay_date,
        sample_size=len(priced),
        weighted_median_nar=median,
        p25_nar=p25,
        p75_nar=p75,
        soldout_ratio=soldout_ratio,
        pressure=pressure,
        detail=detail,
    )


def baseline_median(snapshots: dict[date, CompSnapshot], day: date,
                    settings: Settings, window: int = 28) -> float:
    """同一『日カテゴリ』の平常時中央値。イベント自動検知の基準線となる."""
    target_class = settings.day_class(day)
    values = [
        s.weighted_median_nar
        for d, s in snapshots.items()
        if settings.day_class(d) == target_class
        and s.weighted_median_nar > 0
        and abs((d - day).days) <= window
    ]
    if not values:
        values = [s.weighted_median_nar for s in snapshots.values() if s.weighted_median_nar > 0]
    if not values:
        return 0.0
    values.sort()
    return values[len(values) // 2]


def detect_market_event(settings: Settings, snap: CompSnapshot, baseline: float) -> tuple[bool, float, str]:
    """カレンダー未登録の需要イベントを競合の挙動から逆算検知する.

    『イベント表の記入漏れ＝取りこぼし』を構造的に潰すための機構であり、
    属人性排除の要の一つ。
    """
    cfg = settings.property["auto_event_detection"]
    if baseline <= 0:
        return False, 0.0, ""
    spike = snap.weighted_median_nar / baseline - 1.0
    soldout_hit = snap.soldout_ratio >= float(cfg["comp_soldout_ratio_threshold"])
    spike_hit = spike >= float(cfg["comp_rate_spike_threshold"])
    if soldout_hit and spike_hit:
        score = min(1.0, 0.5 + spike)
        return True, score, f"市場自動検知（売止{snap.soldout_ratio:.0%}／レート+{spike:.0%}）"
    return False, 0.0, ""
