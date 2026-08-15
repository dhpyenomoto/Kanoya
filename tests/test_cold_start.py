"""poll を始めた直後の挙動。

導入初日にダッシュボードは出ない。窓が埋まるまでの間、システムは
「まだ分からない」と言わなければならない。黙って小さい数字を出すと、
それが実測値として読まれてしまう。
"""

import csv
import datetime as dt
import json
from pathlib import Path

import pytest

from kanoya import config as config_module
from kanoya import dashboard as dashboard_module
from kanoya import render as render_module

ROOT = Path(__file__).resolve().parent.parent
AS_OF = dt.date(2026, 8, 15)


@pytest.fixture
def young_deployment(tmp_path):
    """観測が 20 日分しかない状態を、本物のサンプルデータを切り詰めて作る。"""
    cut = dt.date(2026, 7, 26)
    store = tmp_path / "snapshots.jsonl"
    store.write_text(
        "".join(
            line
            for line in (ROOT / "data" / "snapshots.jsonl").read_text(encoding="utf-8").splitlines(
                keepends=True
            )
            if line.strip() and dt.date.fromisoformat(json.loads(line)["date"]) >= cut
        ),
        encoding="utf-8",
    )
    pms = tmp_path / "pms.csv"
    with (ROOT / "data" / "pms_actuals.csv").open(encoding="utf-8") as handle:
        rows = [r for r in csv.DictReader(handle) if dt.date.fromisoformat(r["date"]) >= cut]
    with pms.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "rooms_sold", "rooms_available"])
        writer.writeheader()
        writer.writerows(rows)
    cfg = config_module.load(ROOT / "config.json")
    return dashboard_module.build(cfg, store, pms, AS_OF)


def test_every_estimate_is_marked_low_confidence(young_deployment):
    dash = young_deployment
    for est in dash.estimates:
        assert est.coverage < 0.8
        assert est.confidence(0.15, 0.25) == "低"


def test_the_verdict_refuses_to_act(young_deployment):
    verdict = young_deployment.verdict
    assert verdict.level == "据え置き"
    assert "精度" in verdict.headline


def test_the_data_alert_names_the_coverage_problem(young_deployment):
    alerts = [s for s in young_deployment.signals if s.category == "データ"]
    assert alerts and alerts[0].tone == "alert"
    assert "被覆率" in alerts[0].detail


def test_no_spurious_demand_or_share_signal(young_deployment):
    """観測のない前期と比べて「需要が倍増した」と言ってはいけない。

    切り詰めたデータでは直近 14 日に観測があり、その前 14 日はほぼ空になる。
    素直に割ると +100% 超の急増に見えるが、これは観測開始の跡でしかない。
    """
    categories = {s.category for s in young_deployment.signals}
    assert "需要" not in categories
    assert "シェア" not in categories


def test_prior_period_change_is_reported_as_unknown(young_deployment):
    for est in young_deployment.estimates:
        assert est.change_pct is None
    html = render_module.render(young_deployment)
    # 表の前期比は空欄ではなく「—」で、欠測であることが読み取れる。
    assert html.count("—") >= len(young_deployment.estimates)


def test_the_page_still_renders(young_deployment):
    html = render_module.render(young_deployment)
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
