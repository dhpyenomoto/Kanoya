import datetime as dt
import json
import urllib.error

import pytest

from kanoya.config import Property
from kanoya.places import PlacesClient, PlacesError
from kanoya.store import Snapshot, SnapshotStore

D = dt.date
PROP = Property(key="own", name="鹿のや", place_id="p0", rooms=5, is_own=True)


def test_store_round_trips_through_jsonl(tmp_path):
    store = SnapshotStore(tmp_path / "s.jsonl")
    rows = [Snapshot(D(2026, 1, 1), "p0", 100, 4.53), Snapshot(D(2026, 1, 2), "p0", 103, 4.54)]
    assert store.append(rows) == 2
    assert list(store) == rows


def test_store_is_append_only_across_calls(tmp_path):
    store = SnapshotStore(tmp_path / "s.jsonl")
    store.append([Snapshot(D(2026, 1, 1), "p0", 100, 4.5)])
    store.append([Snapshot(D(2026, 1, 2), "p0", 101, 4.5)])
    assert len(list(store)) == 2


def test_duplicate_observations_resolve_last_write_wins(tmp_path):
    store = SnapshotStore(tmp_path / "s.jsonl")
    store.append([Snapshot(D(2026, 1, 1), "p0", 100, 4.5)])
    store.append([Snapshot(D(2026, 1, 1), "p0", 111, 4.5)])
    assert store.series("p0")[D(2026, 1, 1)].user_rating_count == 111


def test_series_filters_by_place_and_sorts(tmp_path):
    store = SnapshotStore(tmp_path / "s.jsonl")
    store.append(
        [
            Snapshot(D(2026, 1, 3), "p1", 5, 4.0),
            Snapshot(D(2026, 1, 2), "p0", 2, 4.0),
            Snapshot(D(2026, 1, 1), "p0", 1, 4.0),
        ]
    )
    assert list(store.series("p0")) == [D(2026, 1, 1), D(2026, 1, 2)]
    assert store.covered_dates("p0") == (D(2026, 1, 1), D(2026, 1, 2))
    assert store.latest("p0").user_rating_count == 2


def test_missing_store_reads_as_empty(tmp_path):
    store = SnapshotStore(tmp_path / "absent.jsonl")
    assert list(store) == []
    assert store.latest("p0") is None
    assert store.covered_dates("p0") is None


def test_a_corrupt_line_names_its_location(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"date":"2026-01-01","place_id":"p0","user_rating_count":1,"rating":4}\n{oops\n')
    with pytest.raises(ValueError, match="s.jsonl:2"):
        list(SnapshotStore(path))


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client(monkeypatch, responses):
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        result = responses[index]
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(result)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = PlacesClient("test-key", sleep=lambda _: None)
    return client, calls


def test_snapshot_reads_count_and_rating(monkeypatch):
    client, _ = _client(monkeypatch, [{"id": "p0", "rating": 4.53, "userRatingCount": 1245}])
    snap = client.snapshot(PROP, D(2026, 8, 15))
    assert snap.user_rating_count == 1245
    assert snap.rating == 4.53
    assert snap.place_id == "p0"


def test_missing_api_key_fails_loudly(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    with pytest.raises(PlacesError, match="GOOGLE_MAPS_API_KEY"):
        PlacesClient(None)


def test_a_response_without_a_count_is_an_error(monkeypatch):
    client, _ = _client(monkeypatch, [{"id": "p0", "rating": 4.5}])
    with pytest.raises(PlacesError, match="userRatingCount"):
        client.snapshot(PROP, D(2026, 8, 15))


def test_client_errors_are_not_retried(monkeypatch):
    error = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    monkeypatch.setattr(error, "read", lambda: b"denied")
    client, calls = _client(monkeypatch, [error])
    with pytest.raises(PlacesError, match="HTTP 403"):
        client.snapshot(PROP, D(2026, 8, 15))
    assert calls["n"] == 1


def test_rate_limits_are_retried_then_succeed(monkeypatch):
    client, calls = _client(
        monkeypatch,
        [
            urllib.error.HTTPError("u", 429, "Too Many", {}, None),
            {"id": "p0", "rating": 4.5, "userRatingCount": 10},
        ],
    )
    assert client.snapshot(PROP, D(2026, 8, 15)).user_rating_count == 10
    assert calls["n"] == 2


def test_network_failures_exhaust_retries(monkeypatch):
    client, calls = _client(monkeypatch, [urllib.error.URLError("down")])
    with pytest.raises(PlacesError, match="4 回試行"):
        client.snapshot(PROP, D(2026, 8, 15))
    assert calls["n"] == 4


def test_one_bad_property_does_not_sink_the_whole_poll(monkeypatch):
    bad = Property(key="c", name="競合X", place_id="bad", rooms=9)
    responses = {
        "p0": {"id": "p0", "rating": 4.5, "userRatingCount": 10},
        "bad": urllib.error.HTTPError("u", 404, "Not Found", {}, None),
    }
    monkeypatch.setattr(responses["bad"], "read", lambda: b"missing")

    def fake_urlopen(request, timeout=None):
        result = responses["p0"] if "p0" in request.full_url else responses["bad"]
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(result)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = PlacesClient("test-key", sleep=lambda _: None)
    ok, errors = client.snapshot_all([PROP, bad], D(2026, 8, 15))
    assert len(ok) == 1 and len(errors) == 1
    assert "競合X" in errors[0]


def test_request_asks_only_for_the_fields_we_store(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["headers"] = request.headers
        seen["url"] = request.full_url
        return _FakeResponse({"id": "p0", "rating": 4.5, "userRatingCount": 1})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    PlacesClient("test-key", sleep=lambda _: None).snapshot(PROP, D(2026, 8, 15))
    # レビュー本文は要求しない（規約上の保存制限と課金階層の両方の理由から）。
    assert seen["headers"]["X-goog-fieldmask"] == "id,rating,userRatingCount"
    assert "reviews" not in seen["headers"]["X-goog-fieldmask"]
    assert seen["headers"]["X-goog-api-key"] == "test-key"
    assert seen["url"].endswith("/places/p0")
