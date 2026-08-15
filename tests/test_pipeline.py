"""生成済みのサンプルデータに対する通しの検証。

ここが通れば、蓄積庫 → 較正 → 推定 → 判断 → HTML の経路が全部つながっている。
"""

import datetime as dt
import re
from pathlib import Path

import pytest

from kanoya import config as config_module
from kanoya import dashboard as dashboard_module
from kanoya import render as render_module
from kanoya.ribbon import nice_ceiling

ROOT = Path(__file__).resolve().parent.parent
AS_OF = dt.date(2026, 8, 15)


@pytest.fixture(scope="module")
def dash():
    cfg = config_module.load(ROOT / "config.json")
    return dashboard_module.build(
        cfg, ROOT / "data" / "snapshots.jsonl", ROOT / "data" / "pms_actuals.csv", AS_OF
    )


def test_posting_rate_matches_the_pms_ledger(dash):
    assert dash.rate.rooms_sold == 929
    assert dash.rate.reviews == pytest.approx(178.0)
    assert dash.rate.point == pytest.approx(178 / 929, rel=1e-9)
    assert dash.rate.low < dash.rate.point < dash.rate.high


def test_own_estimate_reconciles_with_the_review_count(dash):
    own = dash.own
    assert own.reviews == pytest.approx(65.0)
    assert own.rooms_sold == pytest.approx(65.0 / dash.rate.point)
    assert own.occupancy == pytest.approx(own.rooms_sold / (5 * 90))
    assert 0.74 < own.occupancy < 0.76


def test_every_row_reconciles_reviews_to_occupancy(dash):
    for est in dash.estimates:
        capacity = est.prop.rooms * dash.current.days
        assert est.occupancy == pytest.approx(est.reviews / dash.rate.point / capacity)


def test_area_total_is_the_sum_of_the_comp_set(dash):
    assert dash.area_reviews == pytest.approx(sum(e.reviews for e in dash.estimates))
    assert dash.own_reviews == pytest.approx(dash.own.reviews)


def test_rows_are_ordered_by_occupancy(dash):
    occupancies = [e.occupancy for e in dash.estimates]
    assert occupancies == sorted(occupancies, reverse=True)


def test_restaurant_share_is_actually_deducted(dash):
    # 競合D は raw 658 件、控除率 22% → 宿泊由来 513.24 件。
    comp_d = next(e for e in dash.estimates if e.prop.key == "comp_d")
    assert comp_d.prop.restaurant_review_share == pytest.approx(0.22)
    assert comp_d.reviews == pytest.approx(658 * 0.78)


def test_windows_are_contiguous_and_exclude_the_unstable_tail(dash):
    assert dash.current.end == AS_OF - dt.timedelta(days=10)
    assert dash.prior.end == dash.current.start - dt.timedelta(days=1)
    assert dash.current.days == dash.prior.days == 90


def test_the_quality_gate_fires_on_this_data(dash):
    assert dash.own_rating == pytest.approx(4.53, abs=0.005)
    assert any(s.category == "品質" and s.tone == "alert" for s in dash.signals)


def test_verdict_is_hold_against_a_comparable_market(dash):
    assert dash.verdict.level == "据え置き"


def test_ribbon_spans_both_windows_and_ends_at_the_reference_date(dash):
    ribbon = dash.ribbon
    assert len(ribbon.dates) == 181
    assert ribbon.dates[0] == dash.prior.start - dt.timedelta(days=1)
    assert ribbon.dates[-1] == dash.current.end
    assert len(ribbon.area) == len(ribbon.own) == 181
    # 自社の帯はエリア合計を超えない。
    assert all(o <= a + 1e-9 for o, a in zip(ribbon.own, ribbon.area))


def test_ribbon_events_are_clipped_to_the_visible_range(dash):
    names = {e.name for e in dash.ribbon.events}
    assert names == {"修二会（お水取り）", "桜シーズン"}


@pytest.mark.parametrize(
    "value,expected",
    [(1.0, 1.0), (7.0, 8.0), (99.0, 100.0), (100.0, 100.0), (120.0, 150.0), (0.4, 0.4)],
)
def test_nice_ceiling_never_clips_the_data(value, expected):
    assert nice_ceiling(value) == pytest.approx(expected)
    assert nice_ceiling(value) >= value


def test_axis_ticks_stay_readable_at_the_half_mark():
    for value in (3, 17, 64, 120, 480, 1900):
        assert (nice_ceiling(value) / 2) % 0.5 == 0


def test_html_renders_the_expected_structure(dash):
    html = render_module.render(dash)
    assert html.startswith("<!DOCTYPE html>")
    assert html.count("<section>") == 5
    for index in ("01", "02", "03", "04", "05"):
        assert f'<span class="ix">{index}</span>' in html
    # 表の行は自社 + 競合。
    assert html.count("<tr class=") == len(dash.estimates)
    assert '<tr class="own">' in html
    assert html.count('class="tag"') == 1
    assert "<svg viewBox" in html
    assert html.rstrip().endswith("</html>")


def test_html_reports_the_same_numbers_as_the_model(dash):
    html = render_module.render(dash)
    assert f"{dash.rate.point * 100:.1f}%" in html
    assert f"{dash.rate.rooms_sold}室" in html
    for est in dash.estimates:
        assert f"<em>{est.occupancy * 100:.0f}%</em>" in html


def test_html_escapes_markup_in_configured_names(dash):
    hostile = dash.config.own.__class__(
        key="own", name='<script>alert("x")</script>', place_id="p", rooms=5, is_own=True
    )
    patched = dash.config.__class__(
        own=hostile,
        competitors=dash.config.competitors,
        estimation=dash.config.estimation,
        rules=dash.config.rules,
        events=dash.config.events,
    )
    html = render_module.render(dash.__class__(**{**dash.__dict__, "config": patched}))
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_footer_states_the_granularity_limit(dash):
    html = render_module.render(dash)
    # 5室なら日次稼働は 20% 刻みしか取れない。この限界を明示する。
    assert "0/20/40/60/80/100%" in html
    assert re.search(r"全5室", html)
