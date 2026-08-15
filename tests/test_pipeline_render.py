"""パイプライン全体と HTML 出力の検証。

合成データは真の稼働率を知っているので、推定手法そのものを検証できる。
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta

import pytest

from kanoya import demo as demo_module
from kanoya.config import Config, load_config
from kanoya.demo import TRUE_REVIEW_RATE
from kanoya.pipeline import build_report
from kanoya.store import Snapshot, SnapshotStore

AS_OF = date(2026, 8, 15)


@pytest.fixture(scope="module")
def shipped_config() -> Config:
    return load_config("config.json")


@pytest.fixture
def demo_report(shipped_config: Config, tmp_path):
    data = demo_module.generate(shipped_config, AS_OF, days=280, seed=7)
    actuals = tmp_path / "actuals.csv"
    snapshots = tmp_path / "snapshots.jsonl"
    demo_module.write(data, snapshots, actuals)

    config = Config(
        area_name=shipped_config.area_name,
        window_days=shipped_config.window_days,
        timezone=shipped_config.timezone,
        properties=shipped_config.properties,
        estimation=shipped_config.estimation,
        review_rate=type(shipped_config.review_rate)(
            mode="calibrated",
            fallback_rate=shipped_config.review_rate.fallback_rate,
            pms_actuals_path=str(actuals),
        ),
        pricing_rules=shipped_config.pricing_rules,
        events=shipped_config.events,
    )
    report = build_report(config, SnapshotStore(snapshots).load_by_property(), AS_OF)
    return report, data, config


# --------------------------------------------------------------------------- #
# パイプライン
# --------------------------------------------------------------------------- #


def test_pipeline_recovers_the_true_review_rate(demo_report):
    """自社実績との突合で、合成データに埋め込んだ投稿率を取り戻せる。"""
    report, _, _ = demo_report

    assert report.review_rate.is_measured
    assert report.review_rate.rate == pytest.approx(TRUE_REVIEW_RATE, abs=0.025)
    assert report.review_rate.ci_low <= TRUE_REVIEW_RATE <= report.review_rate.ci_high


def test_pipeline_estimates_stay_within_their_stated_intervals(demo_report):
    """推定が主張する区間に、真の稼働率が実際に入っているか。

    区間が名ばかりなら、この検証で落ちる。
    """
    report, data, _ = demo_report
    market = report.market

    covered = 0
    total = 0
    for estimate in market.estimates:
        true_pct = data.true_occupancy(
            estimate.prop, market.window_start, market.window_end
        )
        if true_pct is None or estimate.occupancy_pct is None:
            continue
        total += 1
        if abs(estimate.occupancy_pct - true_pct * 100) <= estimate.margin_pt:
            covered += 1

    assert total >= 5
    # 95% 区間なので、6 施設ならおおむね全数が入るはず。
    assert covered >= total - 1


def test_estimator_is_unbiased_across_seeds(shipped_config: Config, tmp_path):
    """多数の乱数種で平均すれば、推定の系統誤差はほぼ消える。

    1 回の実行では計数誤差で 10pt 以上ずれることがある。ずれがバイアスなのか
    ばらつきなのかは、平均を取らないと区別できない。
    """
    errors: list[float] = []

    for seed in range(12):
        data = demo_module.generate(shipped_config, AS_OF, days=280, seed=500 + seed)
        actuals = tmp_path / f"actuals_{seed}.csv"
        snapshots = tmp_path / f"snapshots_{seed}.jsonl"
        demo_module.write(data, snapshots, actuals)

        config = Config(
            area_name=shipped_config.area_name,
            window_days=shipped_config.window_days,
            timezone=shipped_config.timezone,
            properties=shipped_config.properties,
            estimation=shipped_config.estimation,
            review_rate=type(shipped_config.review_rate)(
                mode="calibrated",
                fallback_rate=shipped_config.review_rate.fallback_rate,
                pms_actuals_path=str(actuals),
            ),
            pricing_rules=shipped_config.pricing_rules,
            events=shipped_config.events,
        )
        report = build_report(config, SnapshotStore(snapshots).load_by_property(), AS_OF)
        market = report.market

        for estimate in market.estimates:
            true_pct = data.true_occupancy(
                estimate.prop, market.window_start, market.window_end
            )
            if true_pct is not None and estimate.occupancy_pct is not None:
                errors.append(estimate.occupancy_pct - true_pct * 100)

    assert len(errors) >= 60
    assert statistics.mean(errors) == pytest.approx(0.0, abs=6.0)


def test_pipeline_falls_back_when_actuals_are_missing(shipped_config: Config, tmp_path):
    """PMS 実績がなければ固定値に退避し、その旨を警告に残す。"""
    data = demo_module.generate(shipped_config, AS_OF, days=280, seed=7)
    snapshots = tmp_path / "snapshots.jsonl"
    SnapshotStore(snapshots).append(data.snapshots)

    config = Config(
        area_name=shipped_config.area_name,
        window_days=shipped_config.window_days,
        timezone=shipped_config.timezone,
        properties=shipped_config.properties,
        estimation=shipped_config.estimation,
        review_rate=type(shipped_config.review_rate)(
            mode="calibrated",
            fallback_rate=0.10,
            pms_actuals_path=str(tmp_path / "absent.csv"),
        ),
        pricing_rules=shipped_config.pricing_rules,
        events=shipped_config.events,
    )
    report = build_report(config, SnapshotStore(snapshots).load_by_property(), AS_OF)

    assert not report.review_rate.is_measured
    assert report.review_rate.rate == 0.10
    assert any("固定値" in w for w in report.warnings)
    assert "投稿率が未実測" in report.to_html()


def test_pipeline_warns_when_observation_starts_inside_the_window(
    shipped_config: Config, tmp_path
):
    """観測が窓の途中から始まっていると稼働が過小に出るので、警告を出す。"""
    data = demo_module.generate(shipped_config, AS_OF, days=60, seed=7)
    snapshots = tmp_path / "snapshots.jsonl"
    SnapshotStore(snapshots).append(data.snapshots)

    report = build_report(
        shipped_config, SnapshotStore(snapshots).load_by_property(), AS_OF
    )

    assert any("集計窓の先頭" in w for w in report.warnings)


def test_pipeline_handles_a_property_with_no_observations(
    shipped_config: Config, tmp_path
):
    data = demo_module.generate(shipped_config, AS_OF, days=280, seed=7)
    kept = [s for s in data.snapshots if s.property_id != "comp_c"]
    snapshots = tmp_path / "snapshots.jsonl"
    SnapshotStore(snapshots).append(kept)

    report = build_report(
        shipped_config, SnapshotStore(snapshots).load_by_property(), AS_OF
    )

    missing = next(e for e in report.market.estimates if e.prop.id == "comp_c")
    assert missing.occupancy is None
    assert any("comp_c" in w or "競合C" in w for w in report.warnings)
    assert report.to_html()


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


def test_html_contains_every_property_and_the_verdict(demo_report):
    report, _, config = demo_report
    html = report.to_html()

    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    for prop in config.properties:
        assert prop.name in html
    assert report.verdict.headline in html
    assert "レビュー投稿率" in html


def test_html_states_the_interval_next_to_every_estimate(demo_report):
    """区間を書かずに数字だけ出すと、誤差より小さい差で意思決定される。"""
    report, _, _ = demo_report
    html = report.to_html()

    for estimate in report.market.estimates:
        if estimate.occupancy_pct is not None:
            assert f"±{estimate.relative_margin_pt:.0f}" in html


def test_html_escapes_property_names(shipped_config: Config, tmp_path):
    """施設名は設定ファイル由来なので、そのまま埋め込まない。"""
    hostile = type(shipped_config.properties[0])(
        id="own",
        name='<script>alert("x")</script>',
        place_id="p",
        rooms=5,
        is_own=True,
    )
    config = Config(
        area_name=shipped_config.area_name,
        window_days=shipped_config.window_days,
        timezone=shipped_config.timezone,
        properties=[hostile] + list(shipped_config.competitors),
        estimation=shipped_config.estimation,
        review_rate=type(shipped_config.review_rate)(mode="fixed", fallback_rate=0.10),
        pricing_rules=shipped_config.pricing_rules,
        events=shipped_config.events,
    )
    data = demo_module.generate(config, AS_OF, days=280, seed=7)
    snapshots = tmp_path / "snapshots.jsonl"
    SnapshotStore(snapshots).append(data.snapshots)

    html = build_report(
        config, SnapshotStore(snapshots).load_by_property(), AS_OF
    ).to_html()

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_html_renders_the_ribbon_with_both_series(demo_report):
    report, _, _ = demo_report
    html = report.to_html()

    assert "<svg viewBox=" in html
    assert html.count("<path d=") >= 3  # エリア塗り・エリア線・自社塗り
    assert "エリア合計" in html


def test_html_marks_events_inside_the_ribbon_range(demo_report):
    report, _, config = demo_report
    html = report.to_html()

    first, last = report.ribbon.days[0], report.ribbon.days[-1]
    visible = [e for e in config.events if first <= e.date <= last]

    for event in visible:
        assert f"<title>{event.name}</title>" in html


def test_html_states_the_limits(demo_report):
    """レートを推定していないこと、スクレイピングしていないことを必ず書く。"""
    report, _, _ = demo_report
    html = report.to_html()

    assert "レート" in html
    assert "スクレイピング" in html
    assert "前提と限界" in html


def test_empty_ribbon_does_not_break_rendering(shipped_config: Config):
    report = build_report(
        shipped_config,
        {p.id: [] for p in shipped_config.properties},
        AS_OF,
    )

    assert report.to_html()
    assert report.verdict.action == "判断保留"
