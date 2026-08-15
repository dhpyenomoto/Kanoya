"""観測データからダッシュボードまでの一本道。

CLI とテストの両方がここを通る。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .calibration import (
    Backtest,
    CalibrationError,
    ReviewRate,
    backtest_occupancy,
    calibrate_review_rate,
    fixed_review_rate,
    load_pms_actuals,
)
from .config import Config
from .occupancy import MarketEstimate, estimate_market
from .render import RibbonSeries, build_ribbon, render_dashboard
from .reviews import observation_span, stay_review_series
from .signals import Signal, collect_signals
from .store import Snapshot
from .verdict import Verdict, decide

#: 需要リボンに描く期間（日）。窓より長く取り、季節の山谷が見えるようにする。
RIBBON_DAYS = 180


@dataclass(frozen=True)
class Report:
    """1 回ぶんの分析結果一式。"""

    config: Config
    market: MarketEstimate
    review_rate: ReviewRate
    backtest: Backtest
    signals: list[Signal]
    verdict: Verdict
    ribbon: RibbonSeries
    warnings: list[str]

    def to_html(self) -> str:
        return render_dashboard(
            config=self.config,
            market=self.market,
            review_rate=self.review_rate,
            backtest=self.backtest,
            signals=self.signals,
            verdict=self.verdict,
            ribbon=self.ribbon,
        )


def build_report(
    config: Config,
    snapshots_by_property: dict[str, list[Snapshot]],
    as_of: date,
) -> Report:
    """スナップショットから分析結果一式を組み立てる。"""
    warnings: list[str] = []
    est = config.estimation
    own = config.own

    own_snapshots = snapshots_by_property.get(own.id, [])
    own_series = stay_review_series(own_snapshots, own, est.review_lag_days)
    own_span = observation_span(own_snapshots)

    review_rate, backtest = _resolve_review_rate(
        config, own_series, own_span, warnings
    )

    market = estimate_market(config, snapshots_by_property, review_rate, as_of)
    prior_share = _prior_share(config, snapshots_by_property, review_rate, as_of)

    signals = collect_signals(market, config, prior_share)
    verdict = decide(market, config)
    ribbon = _build_ribbon(config, snapshots_by_property, market)

    warnings.extend(_coverage_warnings(config, snapshots_by_property, market))

    return Report(
        config=config,
        market=market,
        review_rate=review_rate,
        backtest=backtest,
        signals=signals,
        verdict=verdict,
        ribbon=ribbon,
        warnings=warnings,
    )


def _resolve_review_rate(
    config: Config,
    own_series: dict[date, float],
    own_span: tuple[date, date] | None,
    warnings: list[str],
) -> tuple[ReviewRate, Backtest]:
    """投稿率を実測する。できなければ固定値に落として警告を積む。"""
    cfg = config.review_rate
    est = config.estimation

    if cfg.mode == "fixed":
        warnings.append(
            "投稿率は config の固定値。自社実績で実測していないため、"
            "推定稼働率の水準は保証されない。"
        )
        return fixed_review_rate(cfg.fallback_rate), Backtest(folds=[])

    try:
        actuals = load_pms_actuals(cfg.pms_actuals_path)
        review_rate = calibrate_review_rate(
            own_series,
            actuals,
            own_span,
            review_lag_days=est.review_lag_days,
            unstable_tail_days=est.unstable_tail_days,
        )
    except CalibrationError as exc:
        warnings.append(f"投稿率を実測できず固定値に退避した: {exc}")
        return fixed_review_rate(cfg.fallback_rate), Backtest(folds=[])

    backtest = backtest_occupancy(
        own_series,
        actuals,
        config.own.rooms,
        review_rate.rate,
        own_span,
        review_lag_days=est.review_lag_days,
        unstable_tail_days=est.unstable_tail_days,
        # ダッシュボードが報告するのと同じ窓長で検証する。窓長が違うと、
        # 報告している数字とは別の精度を測ってしまう。
        fold_days=config.window_days,
    )
    if backtest.usability == "使用不可":
        warnings.append(
            f"自社実績との平均誤差が {backtest.mean_abs_error_pt:.1f}pt ある。"
            "この推定値で価格を動かさないこと。"
        )
    return review_rate, backtest


def _prior_share(
    config: Config,
    snapshots_by_property: dict[str, list[Snapshot]],
    review_rate: ReviewRate,
    as_of: date,
) -> float | None:
    """1 窓ぶん前のエリア内シェア。前期と比較できないときは None。"""
    prior_as_of = as_of - timedelta(days=config.window_days)
    prior = estimate_market(config, snapshots_by_property, review_rate, prior_as_of)
    if prior.area_reviews <= 0:
        return None
    return prior.own_share


def _build_ribbon(
    config: Config,
    snapshots_by_property: dict[str, list[Snapshot]],
    market: MarketEstimate,
) -> RibbonSeries:
    est = config.estimation
    area_series: dict[date, float] = {}
    own_series: dict[date, float] = {}

    for prop in config.properties:
        series = stay_review_series(
            snapshots_by_property.get(prop.id, []), prop, est.review_lag_days
        )
        for day, count in series.items():
            area_series[day] = area_series.get(day, 0.0) + count
        if prop.is_own:
            own_series = series

    end = market.window_end
    start = end - timedelta(days=RIBBON_DAYS - 1)
    return build_ribbon(area_series, own_series, start, end)


def _coverage_warnings(
    config: Config,
    snapshots_by_property: dict[str, list[Snapshot]],
    market: MarketEstimate,
) -> list[str]:
    """観測が足りていない施設を洗い出す。"""
    warnings: list[str] = []
    for prop in config.properties:
        span = observation_span(snapshots_by_property.get(prop.id, []))
        if span is None:
            warnings.append(f"{prop.name}: 観測が 2 日ぶん未満。推定できない。")
        elif span[0] > market.window_start:
            missing = (span[0] - market.window_start).days
            warnings.append(
                f"{prop.name}: 集計窓の先頭 {missing} 日ぶんの観測がない。"
                "この施設の稼働とモメンタムは過小に出る。"
            )
    return warnings
