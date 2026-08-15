import json

import pytest

from kanoya import config as config_module
from kanoya.config import ConfigError

BASE = {
    "property": {"key": "own", "name": "鹿のや", "place_id": "p0", "rooms": 5},
    "comp_set": [{"key": "c1", "name": "競合A", "place_id": "p1", "rooms": 8}],
}


def _write(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_loads_a_minimal_config(tmp_path):
    cfg = config_module.load(_write(tmp_path, BASE))
    assert cfg.own.is_own is True
    assert cfg.competitors[0].is_own is False
    assert len(cfg.properties) == 2
    assert cfg.by_key("c1").rooms == 8


def test_rejects_a_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="設定ファイルがない"):
        config_module.load(tmp_path / "nope.json")


def test_rejects_malformed_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON として不正"):
        config_module.load(path)


def test_rejects_an_empty_comp_set(tmp_path):
    with pytest.raises(ConfigError, match="comp_set が空"):
        config_module.load(_write(tmp_path, {**BASE, "comp_set": []}))


def test_rejects_duplicate_keys(tmp_path):
    data = {**BASE, "comp_set": [{"key": "own", "name": "X", "place_id": "p1", "rooms": 8}]}
    with pytest.raises(ConfigError, match="重複"):
        config_module.load(_write(tmp_path, data))


def test_rejects_a_restaurant_share_outside_the_unit_interval(tmp_path):
    data = json.loads(json.dumps(BASE))
    data["comp_set"][0]["restaurant_review_share"] = 1.0
    with pytest.raises(ConfigError, match="restaurant_review_share"):
        config_module.load(_write(tmp_path, data))


def test_rejects_a_property_without_rooms(tmp_path):
    data = json.loads(json.dumps(BASE))
    del data["property"]["rooms"]
    with pytest.raises(ConfigError, match="rooms"):
        config_module.load(_write(tmp_path, data))


def test_rejects_a_window_too_short_for_the_method(tmp_path):
    data = {**BASE, "estimation": {"window_days": 7}}
    with pytest.raises(ConfigError, match="window_days"):
        config_module.load(_write(tmp_path, data))


def test_rejects_a_calibration_shorter_than_the_window(tmp_path):
    data = {**BASE, "estimation": {"window_days": 90, "calibration_days": 30}}
    with pytest.raises(ConfigError, match="calibration_days"):
        config_module.load(_write(tmp_path, data))


def test_events_are_sorted_by_date(tmp_path):
    data = {
        **BASE,
        "demand_events": [
            {"date": "2026-11-20", "name": "紅葉", "lift_pct": 25},
            {"date": "2026-03-01", "name": "修二会", "lift_pct": 25},
        ],
    }
    cfg = config_module.load(_write(tmp_path, data))
    assert [e.name for e in cfg.events] == ["修二会", "紅葉"]


def test_lodging_reviews_deducts_the_restaurant_share():
    prop = config_module.Property(
        key="d", name="D", place_id="p", rooms=40, restaurant_review_share=0.22
    )
    assert prop.lodging_reviews(658) == pytest.approx(513.24)


def test_the_shipped_config_is_valid():
    cfg = config_module.load("config.json")
    assert cfg.own.rooms == 5
    assert len(cfg.competitors) == 5
    assert cfg.rules.rating_floor == 4.6
