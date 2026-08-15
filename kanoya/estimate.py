"""推定エンジン。

    推定販売室数 = 実効レビュー増分 ÷ レビュー投稿率
    推定稼働率   = 推定販売室数 ÷（客室数 × 日数）

投稿率は自社PMS実績で実測し、同率を競合にも適用する。したがってこの数字は
「施設間の相対差とモメンタム」を読むためのものであって、絶対値ではない。
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from .config import Config, Property
from .series import Series, increments, shift_back, window_sum
from .store import PmsDay, SnapshotStore

Z95 = 1.959963984540054


@dataclass(frozen=True)
class Window:
    start: dt.date
    end: dt.date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def shifted(self, days: int) -> "Window":
        return Window(self.start - dt.timedelta(days=days), self.end - dt.timedelta(days=days))


@dataclass(frozen=True)
class Calibration:
    rate: float
    ci_low: float
    ci_high: float
    days: int
    sold_rooms: int
    reviews_raw: float
    reviews_effective: float
    mean_abs_error_pt: float | None = None
    backtest_n: int = 0

    @property
    def usable_for(self) -> str:
        """このキャリブレーション精度で何を語ってよいか。"""
        e = self.mean_abs_error_pt
        if e is None:
            return "検証前（相対比較のみ）"
        if e < 3.0:
            return "水準の議論に使える"
        if e < 6.0:
            return "方向性の議論のみ"
        return "参考値（順位のみ）"


@dataclass(frozen=True)
class PropertyEstimate:
    prop: Property
    reviews_raw: float
    reviews_raw_prev: float
    reviews_effective: float
    sold_rooms: float
    occupancy: float
    prev_occupancy: float
    confidence: str

    @property
    def delta_pt(self) -> float:
        return (self.occupancy - self.prev_occupancy) * 100.0

    @property
    def occupancy_se_pt(self) -> float:
        """稼働推定の標準誤差（pt）。

        レビューの発生はポアソン過程とみなせるので、相対標準誤差は 1/√件数。
        5室規模では 90日で 60件程度しか出ないため、これは ±9pt 前後になる。
        """
        if self.reviews_raw <= 0:
            return 100.0
        return self.occupancy * 100.0 / math.sqrt(self.reviews_raw)

    @property
    def delta_significant(self) -> bool:
        """前期比が件数の揺らぎで説明できないか（2標本ポアソン検定, 両側5%）。

        窓の長さが同じ＝露出が同じなので、差の分散は件数の和で近似できる。
        ここが False の増減を読みにいくと、毎回ノイズに反応することになる。
        """
        n1, n2 = self.reviews_raw, self.reviews_raw_prev
        if n1 + n2 <= 0:
            return False
        return abs(n1 - n2) / math.sqrt(n1 + n2) >= Z95


@dataclass(frozen=True)
class Report:
    asof: dt.date
    window: Window
    calibration: Calibration
    estimates: tuple[PropertyEstimate, ...]
    own: PropertyEstimate
    comp_median_occupancy: float
    area_reviews: float
    own_reviews: float
    own_rating: float | None
    ribbon_dates: tuple[dt.date, ...]
    ribbon_area: tuple[float, ...]
    ribbon_own: tuple[float, ...]

    @property
    def area_share(self) -> float:
        return self.own_reviews / self.area_reviews if self.area_reviews else 0.0

    @property
    def gap_pt(self) -> float:
        return (self.own.occupancy - self.comp_median_occupancy) * 100.0


def wilson_interval(successes: float, trials: float, z: float = Z95) -> tuple[float, float]:
    """二項比率の Wilson 信頼区間。successes は控除後の実数でも受ける。"""
    if trials <= 0:
        return (0.0, 0.0)
    p = successes / trials
    p = min(max(p, 0.0), 1.0)
    denom = 1.0 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    half = z / denom * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return (max(0.0, center - half), min(1.0, center + half))


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def stay_series(store: SnapshotStore, prop: Property, lag_days: int) -> Series:
    """滞在日ベースの実効レビュー増分。"""
    raw = shift_back(increments(store.counts(prop.place_id)), lag_days)
    return {d: prop.effective(v) for d, v in raw.items()}


def raw_stay_series(store: SnapshotStore, place_id: str, lag_days: int) -> Series:
    """控除前・滞在日ベースの増分（エリア需要リボン用）。"""
    return shift_back(increments(store.counts(place_id)), lag_days)


def analysis_window(cfg: Config, asof: dt.date) -> Window:
    """直近の不安定な尾（投稿遅延で未確定な期間）を落とした集計窓。"""
    end = asof - dt.timedelta(days=cfg.model.unstable_tail_days)
    return Window(end - dt.timedelta(days=cfg.model.window_days - 1), end)


def calibrate(
    cfg: Config,
    store: SnapshotStore,
    pms: dict[dt.date, PmsDay],
    asof: dt.date,
) -> Calibration:
    """自社PMS実績とレビュー増分を突合し、投稿率（1販売室あたり）を実測する。"""
    win = analysis_window(cfg, asof)
    start = win.end - dt.timedelta(days=cfg.model.calibration_days - 1)

    raw = raw_stay_series(store, cfg.own.place_id, cfg.model.review_lag_days)
    days = [d for d in sorted(pms) if start <= d <= win.end and d in raw]
    sold = sum(pms[d].rooms_sold for d in days)
    reviews_raw = sum(raw[d] for d in days)
    reviews_eff = cfg.own.effective(reviews_raw)

    rate = reviews_eff / sold if sold else 0.0
    lo, hi = wilson_interval(reviews_eff, sold)
    mae, n = _backtest(cfg, store, pms, asof, rate)
    return Calibration(
        rate=rate,
        ci_low=lo,
        ci_high=hi,
        days=len(days),
        sold_rooms=sold,
        reviews_raw=reviews_raw,
        reviews_effective=reviews_eff,
        mean_abs_error_pt=mae,
        backtest_n=n,
    )


def _backtest(
    cfg: Config,
    store: SnapshotStore,
    pms: dict[dt.date, PmsDay],
    asof: dt.date,
    rate: float,
) -> tuple[float | None, int]:
    """過去窓で「推定した自社稼働」と「PMS実績」を突き合わせ、平均絶対誤差を出す。

    投稿率は全期間で実測した1つの値なので、この検証はイン・サンプル。
    手法の上限精度であって、将来の誤差保証ではない。
    """
    if rate <= 0:
        return (None, 0)
    eff = stay_series(store, cfg.own, cfg.model.review_lag_days)
    base = analysis_window(cfg, asof)
    errors: list[float] = []
    for k in range(cfg.model.backtest_periods):
        win = base.shifted(k * cfg.model.backtest_step_days)
        days = [d for d in sorted(pms) if win.start <= d <= win.end]
        if len(days) < win.days * 0.9:
            continue
        available = sum(pms[d].rooms_available for d in days)
        if not available:
            continue
        actual = sum(pms[d].rooms_sold for d in days) / available
        estimated = window_sum(eff, win.start, win.end) / rate / available
        errors.append(abs(estimated - actual) * 100.0)
    if not errors:
        return (None, 0)
    return (sum(errors) / len(errors), len(errors))


def _confidence(cfg: Config, reviews_effective: float) -> str:
    if reviews_effective >= cfg.model.confidence_reviews_high:
        return "高"
    if reviews_effective >= cfg.model.confidence_reviews_mid:
        return "中"
    return "低"


def estimate_property(
    cfg: Config,
    store: SnapshotStore,
    prop: Property,
    win: Window,
    rate: float,
) -> PropertyEstimate:
    eff = stay_series(store, prop, cfg.model.review_lag_days)
    prev = win.shifted(cfg.model.window_days)

    raw_series = raw_stay_series(store, prop.place_id, cfg.model.review_lag_days)
    reviews_eff = window_sum(eff, win.start, win.end)
    raw = window_sum(raw_series, win.start, win.end)
    raw_prev = window_sum(raw_series, prev.start, prev.end)
    capacity = prop.rooms * win.days

    sold = reviews_eff / rate if rate else 0.0
    occ = min(sold / capacity, 1.0) if capacity else 0.0

    prev_sold = window_sum(eff, prev.start, prev.end) / rate if rate else 0.0
    prev_occ = min(prev_sold / (prop.rooms * prev.days), 1.0) if capacity else 0.0

    return PropertyEstimate(
        prop=prop,
        reviews_raw=raw,
        reviews_raw_prev=raw_prev,
        reviews_effective=reviews_eff,
        sold_rooms=sold,
        occupancy=occ,
        prev_occupancy=prev_occ,
        confidence=_confidence(cfg, reviews_eff),
    )


def build_report(
    cfg: Config,
    store: SnapshotStore,
    pms: dict[dt.date, PmsDay],
    area_place_ids: list[str],
    asof: dt.date,
) -> Report:
    win = analysis_window(cfg, asof)
    cal = calibrate(cfg, store, pms, asof)

    estimates = [estimate_property(cfg, store, p, win, cal.rate) for p in cfg.tracked]
    estimates.sort(key=lambda e: e.occupancy, reverse=True)
    own = next(e for e in estimates if e.prop.is_own)
    comp_median = median([e.occupancy for e in estimates if not e.prop.is_own])

    # エリア需要は控除前のクチコミ総量。母集団は nearby search の台帳。
    area_series: Series = {}
    for pid in area_place_ids or [p.place_id for p in cfg.tracked]:
        for d, v in raw_stay_series(store, pid, cfg.model.review_lag_days).items():
            area_series[d] = area_series.get(d, 0.0) + v
    own_series = raw_stay_series(store, cfg.own.place_id, cfg.model.review_lag_days)

    from .series import date_range, rolling_sum

    ribbon_end = win.end
    ribbon_dates = date_range(ribbon_end - dt.timedelta(days=cfg.model.ribbon_days - 1), ribbon_end)
    k = cfg.model.rolling_days
    return Report(
        asof=asof,
        window=win,
        calibration=cal,
        estimates=tuple(estimates),
        own=own,
        comp_median_occupancy=comp_median,
        area_reviews=window_sum(area_series, win.start, win.end),
        own_reviews=window_sum(own_series, win.start, win.end),
        own_rating=store.latest_rating(cfg.own.place_id),
        ribbon_dates=tuple(ribbon_dates),
        ribbon_area=tuple(rolling_sum(area_series, ribbon_dates, k)),
        ribbon_own=tuple(rolling_sum(own_series, ribbon_dates, k)),
    )
