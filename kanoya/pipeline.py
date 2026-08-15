"""台帳 → 推定 → 判断 → HTML の組み立て。

各段の依存はこの順で一方向。逆流させない。
"""

from __future__ import annotations

from datetime import date

from . import calibrate, estimate, pms, store, verdict
from .config import Config
from .render import Report


def build(config: Config, as_of: date) -> Report:
    snapshots = store.load(config.paths.snapshots)
    if not snapshots:
        raise RuntimeError(
            f"スナップショット台帳が空: {config.paths.snapshots}\n"
            "`kanoya snapshot` を毎日走らせて累計レビュー件数を記録すること。"
            "この手法は過去に遡って台帳を作れない。"
        )

    actuals = pms.load(config.paths.pms_actuals)
    rate = calibrate.posting_rate(config, snapshots, actuals)
    backtest = calibrate.backtest(config, snapshots, actuals, rate)

    window = estimate.analysis_window(config, as_of)
    estimates = estimate.estimate_all(config, snapshots, rate, window)
    series = estimate.area_series(config, snapshots, as_of=as_of)

    rating = store.latest_rating(snapshots, config.own.place_id)
    decision = verdict.decide(config, estimates, rating, backtest)
    signals = verdict.build_signals(
        config, estimates, series, rating, rate, backtest, as_of
    )

    return Report(
        config=config,
        as_of=as_of,
        window=window,
        estimates=estimates,
        verdict=decision,
        signals=signals,
        series=series,
        rate=rate,
        backtest=backtest,
        rating=rating,
        own_review_total=store.latest_count(snapshots, config.own.place_id),
    )


def summary_lines(report: Report) -> list[str]:
    """端末に出す 1 画面ぶんの要約。HTML を開かずに判断だけ確認する用。"""
    lines = [
        f"{report.config.own.label} / {report.as_of.isoformat()}",
        f"窓: {report.window.start} 〜 {report.window.end}（{report.window.days}日）",
        f"投稿率: {report.rate.rate * 100:.1f}%"
        f"（{report.rate.source_label}, n={report.rate.matched_rooms_sold}室）",
        "",
        f"判断: [{report.verdict.level}] {report.verdict.headline}",
        "",
        "推定稼働:",
    ]
    for item in report.estimates:
        mark = "*" if item.prop.is_own else " "
        delta = item.delta_pct
        if delta is None:
            delta_text = "  — "
        elif abs(delta) < 0.5:
            delta_text = "±0%"
        else:
            delta_text = f"{delta:+.0f}%"
        lines.append(
            f" {mark} {item.occupancy * 100:5.1f}%  {delta_text:>6}  "
            f"信頼度{item.confidence}  {item.prop.label}"
        )
    if report.signals:
        lines.append("")
        lines.append("シグナル:")
        for signal in report.signals:
            lines.append(f"  [{signal.level}] {signal.title}")
    return lines
