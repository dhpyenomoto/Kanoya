"""デモデータを一巡させ、配管全体が壊れていないことを確認する。

個々の数字を固定すると合成データを触るたびに落ちるので、ここで縛るのは
「経路が通ること」と「壊れたら判断を誤る性質」だけにしている。
"""

from dataclasses import replace
from datetime import date

import pytest

from kanoya.render import render_html
from kanoya.report import build_report
from kanoya.ribbon import render_ribbon
from kanoya.store import Store

from .helpers import demo_config

TODAY = date(2026, 8, 15)


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    from tools.make_fixtures import build_fixtures

    root = tmp_path_factory.mktemp("kanoya")
    config = replace(
        demo_config(),
        database=str(root / "test.sqlite3"),
        pms_actuals_csv=str(root / "pms.csv"),
    )
    build_fixtures(config, today=TODAY)
    with Store(config.database_path) as store:
        return build_report(config, store, TODAY)


def test_as_of_excludes_the_unsettled_tail(report):
    """直近 lag 日は投稿が出そろっていない。判断に含めてはいけない。"""
    assert report.as_of == date(2026, 8, 5)
    assert (TODAY - report.as_of).days == report.review_lag_days


def test_every_property_is_estimated_and_sorted(report):
    assert len(report.estimates) == len(demo_config().properties)
    occupancies = [e.occupancy for e in report.estimates]
    assert occupancies == sorted(occupancies, reverse=True)
    assert all(0 < e.occupancy <= 100 for e in report.estimates)


def test_posting_rate_is_measured_against_own_actuals(report):
    rate = report.posting_rate
    assert rate.is_measured
    assert 0.08 < rate.rate < 0.13
    assert rate.ci_low < rate.rate < rate.ci_high


def test_estimate_is_calibrated_well_enough_to_compare_levels(report):
    assert report.backtest.windows == demo_config().estimation.backtest_windows
    assert report.backtest.usable_for == "水準の議論に使える"


def test_share_is_own_over_tracked_area(report):
    total = sum(e.raw_reviews for e in report.estimates)
    assert report.share.total_raw_reviews == pytest.approx(total)
    assert report.share.own_raw_reviews == pytest.approx(report.own.raw_reviews)


def test_ribbon_spans_the_configured_window(report):
    est = demo_config().estimation
    assert len(report.ribbon.dates) == est.ribbon_days + 1
    assert report.ribbon.dates[-1] == report.as_of
    assert len(report.ribbon.area) == len(report.ribbon.own)
    # 自社ぶんはエリア合計の内数。上回ることはありえない。
    assert all(o <= a + 1e-9 for o, a in zip(report.ribbon.own, report.ribbon.area))


def test_rating_below_floor_produces_the_quality_alert(report):
    assert report.own.rating is not None
    assert report.own.rating < report.rating_floor
    assert any(s.category == "品質" for s in report.signals)


def test_verdict_matches_the_gap(report):
    gap = report.own.occupancy - report.compset_median_occupancy
    rules = demo_config().pricing
    assert rules.cut_gap_pt < gap < rules.raise_gap_pt
    assert report.verdict.level == "据え置き"


def test_html_is_self_contained(report):
    html = render_html(report)
    assert html.startswith("<!DOCTYPE html>")
    assert "<style>" in html and "</html>" in html
    # 外部リソースを一切参照しない（社内配布でオフラインでも開けること）
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html
    for est in report.estimates:
        assert est.property.name in html


def test_ribbon_svg_geometry_stays_inside_the_plot_area():
    svg = render_ribbon(report_ribbon())
    assert svg.count("<path") == 3  # エリア塗り・エリア輪郭・自社
    assert 'viewBox="0 0 900 190"' in svg


def report_ribbon():
    from kanoya.models import RibbonSeries

    days = [date(2026, 2, 6)]
    return RibbonSeries(dates=days * 2, area=[10.0, 20.0], own=[1.0, 2.0], events=[])
