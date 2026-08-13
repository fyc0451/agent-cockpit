from __future__ import annotations

import sqlite3
import traceback
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import event_api, event_store


def _event(index: int = 1, **changes):
    value = {
        "event_id": f"evt_{index:032x}", "event_type": "workspace.created",
        "event_version": 1, "project_id": "prj_" + "a" * 32,
        "workspace_id": "ws_" + "b" * 32, "aggregate_type": "workspace",
        "aggregate_id": "ws_" + "b" * 32, "aggregate_version": index,
        "actor": {"type": "system", "id": "system-local"},
        "source": {"type": "operation", "source_event_id": f"op-{index}"},
        "correlation_id": "op_" + "c" * 32, "causation_id": None,
        "occurred_at": "2026-08-14T00:00:00Z", "payload": {"kind": "created"},
        "receipt_refs": [{"type": "operation", "id": "op_" + "c" * 32}],
    }
    value.update(changes)
    return value


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / "event-journal.sqlite3"


@pytest.fixture()
def store(path: Path):
    return event_store.initialize(path)


def _sanitized(exc_info, code: str, secret: str) -> None:
    error = exc_info.value
    assert error.code == code
    assert str(error) == code
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in "".join(traceback.format_exception(error))


def test_append_envelope_replay_conflict_and_cursor_restart(store, path: Path):
    first = store.append(_event())
    assert first.cursor == 1
    assert first.recorded_at.endswith("Z")
    assert first.public_dict()["source"] == {"type": "operation", "source_event_id": "op-1"}
    assert store.append(_event()) == first
    with pytest.raises(event_store.EventStoreError) as conflict:
        store.append(_event(payload={"kind": "changed"}))
    assert conflict.value.code == "event_dedup_conflict"
    assert event_store.open_existing(path).append(_event(2)).cursor == 2


def test_project_workspace_type_cursor_filter_and_pagination(store):
    alpha = store.append(_event(1, event_type="workspace.created"))
    beta = store.append(_event(2, event_type="agent.runtime.observed"))
    other = store.append(_event(3, project_id="prj_" + "d" * 32))
    items, next_cursor = store.list(project_id=alpha.project_id, limit=1)
    assert items == (alpha,)
    assert next_cursor == alpha.cursor
    page, next_cursor = store.list(project_id=alpha.project_id, after_cursor=next_cursor, types=("agent.runtime.observed",), limit=2)
    assert page == (beta,)
    assert next_cursor is None
    assert store.list(project_id=alpha.project_id, workspace_id="ws_" + "0" * 32)[0] == ()
    assert store.get(other.event_id) == other
    assert store.get("evt_" + "f" * 32) is None


@pytest.mark.parametrize("change", [
    {"event_version": True}, {"aggregate_version": 0}, {"actor": {"type": "system"}},
    {"source": {"type": "operation", "source_event_id": ""}}, {"occurred_at": "not-time"},
    {"payload": {"secret": "never"}}, {"payload": {"terminal_output": "never"}},
    {"payload": {"nested": {"hidden_reasoning": "never"}}}, {"receipt_refs": [{"type": "x"}]},
])
def test_malformed_or_forbidden_envelopes_fail_before_write(store, change):
    value = _event()
    value.update(change)
    with pytest.raises(event_store.EventStoreError) as invalid:
        store.append(value)
    assert invalid.value.code == "invalid_argument"
    assert store.list(project_id="prj_" + "a" * 32)[0] == ()


def test_payload_size_and_invalid_queries_fail_closed(store):
    with pytest.raises(event_store.EventStoreError) as payload:
        store.append(_event(payload={"summary": "x" * (event_store.MAX_PAYLOAD_BYTES + 1)}))
    assert payload.value.code == "invalid_argument"
    with pytest.raises(event_store.EventStoreError) as query:
        store.list(project_id="prj_" + "a" * 32, after_cursor=True)
    assert query.value.code == "invalid_argument"
    with pytest.raises(event_store.EventStoreError) as duplicate_types:
        store.list(project_id="prj_" + "a" * 32, types=("workspace.created", "workspace.created"))
    assert duplicate_types.value.code == "invalid_argument"


def test_read_never_initializes_and_schema_drift_fails_closed(path: Path):
    with pytest.raises(event_store.EventStoreError) as missing:
        event_store.open_existing(path)
    assert missing.value.code == "schema_missing"
    assert not path.exists()

    event_store.initialize(path).close()
    before = path.read_bytes()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE events ADD COLUMN drift TEXT")
    drifted = path.read_bytes()
    with pytest.raises(event_store.EventStoreError) as drift:
        event_store.open_existing(path)
    assert drift.value.code == "schema_fingerprint_mismatch"
    assert drifted != before
    assert path.read_bytes() == drifted


def test_store_errors_are_sanitized_and_connection_is_closed(store, monkeypatch):
    real_connect = event_store._connect
    captured = []

    class Tracked:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.connection.close()
            self.closed = True

    def tracked(path, *, write):
        value = Tracked(real_connect(path, write=write))
        captured.append(value)
        return value

    monkeypatch.setattr(event_store, "_connect", tracked)
    monkeypatch.setattr(event_store, "_record", lambda _row: (_ for _ in ()).throw(IndexError("private sqlite detail")))
    with pytest.raises(event_store.EventStoreError) as failure:
        store.append(_event())
    _sanitized(failure, "store_corrupt", "private sqlite detail")
    assert captured[0].closed is True


def test_read_uses_query_only_and_does_not_write_sidecars(store, monkeypatch, path: Path):
    store.append(_event())
    real_connect = event_store._connect
    observed = []

    def checked(path, *, write):
        connection = real_connect(path, write=write)
        if not write:
            observed.append(connection.execute("PRAGMA query_only").fetchone()[0])
        return connection

    monkeypatch.setattr(event_store, "_connect", checked)
    before = path.read_bytes()
    assert store.get(_event()["event_id"]) is not None
    assert observed == [1]
    assert path.read_bytes() == before
    assert not path.with_name(path.name + "-wal").exists()


def test_event_id_collision_is_a_sanitized_conflict(store):
    store.append(_event())
    with pytest.raises(event_store.EventStoreError) as conflict:
        store.append(_event(2, event_id=_event()["event_id"], source={"type": "operation", "source_event_id": "op-other"}))
    assert conflict.value.code == "event_dedup_conflict"


def test_g3_get_by_id_is_injected_and_does_not_install_server_routes(store):
    record = store.append(_event())
    app = FastAPI()
    event_api.install(app, event_api.EventApiService(lambda: store))
    client = TestClient(app)
    response = client.get(f"/api/events/{record.event_id}")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data", "meta"}
    assert body["data"]["event_id"] == record.event_id
    assert body["meta"]["capabilities"]["eventJournal.write"]["available"] is False
    missing = client.get("/api/events/evt_" + "f" * 32)
    assert missing.status_code == 404
    assert set(missing.json()["error"]) == {"code", "message", "retryable", "request_id", "details"}
