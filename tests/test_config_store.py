from __future__ import annotations

import json
from datetime import date

import pytest

from kanoya.config import ConfigError, load_config
from kanoya.store import Snapshot, SnapshotStore

MINIMAL = {
    "area_name": "奈良春日",
    "window_days": 90,
    "properties": [
        {"id": "own", "name": "鹿のや", "place_id": "p", "rooms": 5, "is_own": True},
        {"id": "a", "name": "競合A", "place_id": "q", "rooms": 8},
    ],
}


def _write(tmp_path, payload) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_loads_a_minimal_config(tmp_path):
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.own.id == "own"
    assert [p.id for p in config.competitors] == ["a"]
    assert config.by_id("a").rooms == 8


def test_rejects_multiple_own_properties(tmp_path):
    payload = json.loads(json.dumps(MINIMAL))
    payload["properties"][1]["is_own"] = True

    with pytest.raises(ConfigError, match="ちょうど 1 件"):
        load_config(_write(tmp_path, payload))


def test_rejects_no_own_property(tmp_path):
    payload = json.loads(json.dumps(MINIMAL))
    payload["properties"][0]["is_own"] = False

    with pytest.raises(ConfigError, match="ちょうど 1 件"):
        load_config(_write(tmp_path, payload))


def test_rejects_duplicate_ids(tmp_path):
    payload = json.loads(json.dumps(MINIMAL))
    payload["properties"][1]["id"] = "own"

    with pytest.raises(ConfigError, match="重複"):
        load_config(_write(tmp_path, payload))


def test_rejects_a_restaurant_share_of_one(tmp_path):
    payload = json.loads(json.dumps(MINIMAL))
    payload["properties"][0]["restaurant_review_share"] = 1.0

    with pytest.raises(ConfigError, match="restaurant_review_share"):
        load_config(_write(tmp_path, payload))


def test_rejects_a_too_short_window(tmp_path):
    payload = json.loads(json.dumps(MINIMAL))
    payload["window_days"] = 7

    with pytest.raises(ConfigError, match="window_days"):
        load_config(_write(tmp_path, payload))


def test_rejects_inverted_confidence_thresholds(tmp_path):
    payload = json.loads(json.dumps(MINIMAL))
    payload["estimation"] = {
        "high_confidence_margin_pt": 30.0,
        "medium_confidence_margin_pt": 10.0,
    }

    with pytest.raises(ConfigError, match="high_confidence_margin_pt"):
        load_config(_write(tmp_path, payload))


def test_rejects_a_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="見つからない"):
        load_config(tmp_path / "absent.json")


def test_rejects_broken_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigError, match="JSON"):
        load_config(path)


def test_the_shipped_config_is_valid():
    config = load_config("config.json")

    assert config.own.rooms == 5
    assert len(config.competitors) == 5
    assert config.events


# --------------------------------------------------------------------------- #
# スナップショットストア
# --------------------------------------------------------------------------- #


def test_store_round_trips(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots.jsonl")
    snapshots = [
        Snapshot(date(2026, 1, 1), "own", "p", 100, 4.7),
        Snapshot(date(2026, 1, 2), "own", "p", 103, 4.7),
    ]

    store.append(snapshots)

    assert store.load() == snapshots


def test_store_prefers_the_later_write_for_a_repeated_day(tmp_path):
    """観測をやり直しても壊れず、後から書いたほうが残る。"""
    store = SnapshotStore(tmp_path / "snapshots.jsonl")
    store.append([Snapshot(date(2026, 1, 1), "own", "p", 100, 4.7)])
    store.append([Snapshot(date(2026, 1, 1), "own", "p", 105, 4.8)])

    loaded = store.load()

    assert len(loaded) == 1
    assert loaded[0].user_rating_count == 105


def test_store_groups_by_property(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots.jsonl")
    store.append(
        [
            Snapshot(date(2026, 1, 1), "own", "p", 100),
            Snapshot(date(2026, 1, 1), "comp", "q", 200),
            Snapshot(date(2026, 1, 2), "own", "p", 101),
        ]
    )

    grouped = store.load_by_property()

    assert set(grouped) == {"own", "comp"}
    assert [s.user_rating_count for s in grouped["own"]] == [100, 101]


def test_store_is_empty_before_any_observation(tmp_path):
    assert SnapshotStore(tmp_path / "missing.jsonl").load() == []


def test_store_reports_the_line_of_a_corrupt_record(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    path.write_text(
        '{"date":"2026-01-01","property_id":"own","user_rating_count":1}\n'
        "not json\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=":2"):
        SnapshotStore(path).load()


def test_store_skips_blank_lines(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    path.write_text(
        '{"date":"2026-01-01","property_id":"own","user_rating_count":1}\n\n',
        encoding="utf-8",
    )

    assert len(SnapshotStore(path).load()) == 1
