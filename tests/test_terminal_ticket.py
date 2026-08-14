from __future__ import annotations

import sqlite3
import traceback
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import terminal_ticket_api as api
from agent_cockpit import terminal_ticket_store as tickets


def _input(**changes):
    value = {
        "project_id": "prj_" + "a" * 32, "workspace_id": "ws_" + "b" * 32,
        "desired_state": "running", "observed_state": "unknown", "engine_generation": 1,
        "reconnect_cursor": 0, "receipt_refs": [{"type": "operation", "id": "op_" + "c" * 32}],
    }
    value.update(changes)
    return value


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / "terminal-ticket.sqlite3"


@pytest.fixture()
def store(path: Path):
    return tickets.initialize(path)


def _sanitized(info, code: str, secret: str) -> None:
    error = info.value
    assert error.code == code and str(error) == code
    assert error.__cause__ is None and error.__context__ is None
    assert secret not in "".join(traceback.format_exception(error))


def test_create_idempotent_restart_and_typed_receipts(store, path: Path):
    first = store.create(_input(), idempotency_key="create-1")
    assert first.ticket_id.startswith("ttk_") and first.revision == 1
    assert first.engine_generation == 1 and first.receipt_refs == ({"type": "operation", "id": "op_" + "c" * 32},)
    updated = store.update(project_id=first.project_id, workspace_id=first.workspace_id, ticket_id=first.ticket_id, expected_revision=1, value=_input(desired_state="paused", observed_state="paused", engine_generation=2, reconnect_cursor=2, receipt_refs=[]))
    assert updated.revision == 2
    assert store.create(_input(), idempotency_key="create-1") == first
    with pytest.raises(tickets.TerminalTicketError) as conflict:
        store.create(_input(desired_state="paused"), idempotency_key="create-1")
    assert conflict.value.code == "idempotency_conflict"
    assert tickets.open_existing(path).get(project_id=first.project_id, workspace_id=first.workspace_id, ticket_id=first.ticket_id) == updated


def test_workspace_scope_list_and_finite_cursor(store):
    first = store.create(_input(), idempotency_key="first")
    second = store.create(_input(reconnect_cursor=5), idempotency_key="second")
    other = store.create(_input(workspace_id="ws_" + "d" * 32), idempotency_key="other")
    page, cursor = store.list(project_id=first.project_id, workspace_id=first.workspace_id, limit=1)
    assert page == (min(first, second, key=lambda item: item.ticket_id),) and cursor == page[0].ticket_id
    after, cursor = store.list(project_id=first.project_id, workspace_id=first.workspace_id, after_ticket_id=cursor)
    assert after == (max(first, second, key=lambda item: item.ticket_id),) and cursor is None
    assert store.get(project_id=first.project_id, workspace_id=other.workspace_id, ticket_id=first.ticket_id) is None
    assert store.list(project_id=first.project_id, workspace_id=other.workspace_id)[0] == (other,)


def test_compare_and_swap_increments_revision_and_rejects_cross_scope(store):
    first = store.create(_input(), idempotency_key="first")
    updated = store.update(project_id=first.project_id, workspace_id=first.workspace_id, ticket_id=first.ticket_id, expected_revision=1, value=_input(desired_state="paused", observed_state="paused", engine_generation=2, reconnect_cursor=3, receipt_refs=[]))
    assert updated.revision == 2 and updated.engine_generation == 2 and updated.reconnect_cursor == 3
    with pytest.raises(tickets.TerminalTicketError) as stale:
        store.update(project_id=first.project_id, workspace_id=first.workspace_id, ticket_id=first.ticket_id, expected_revision=1, value=_input())
    assert stale.value.code == "revision_conflict"
    with pytest.raises(tickets.TerminalTicketError) as generation:
        store.update(project_id=first.project_id, workspace_id=first.workspace_id, ticket_id=first.ticket_id, expected_revision=2, value=_input(desired_state="paused", observed_state="paused", engine_generation=1, reconnect_cursor=0, receipt_refs=[]))
    assert generation.value.code == "revision_conflict"
    with pytest.raises(tickets.TerminalTicketError) as bad_scope:
        store.update(project_id=first.project_id, workspace_id=first.workspace_id, ticket_id=first.ticket_id, expected_revision=2, value=_input(workspace_id="ws_" + "d" * 32))
    assert bad_scope.value.code == "invalid_argument"


def test_signed64_revision_is_rejected_before_any_mutation(store, path: Path):
    first = store.create(_input(), idempotency_key="first")
    before = path.read_bytes()
    with pytest.raises(tickets.TerminalTicketError) as overflow:
        store.update(project_id=first.project_id, workspace_id=first.workspace_id, ticket_id=first.ticket_id, expected_revision=tickets.MAX_SIGNED64, value=_input(desired_state="paused", observed_state="paused", engine_generation=2, reconnect_cursor=1, receipt_refs=[]))
    assert overflow.value.code == "revision_conflict"
    assert path.read_bytes() == before
    assert store.get(project_id=first.project_id, workspace_id=first.workspace_id, ticket_id=first.ticket_id) == first


@pytest.mark.parametrize("change", [
    {"project_id": "not-a-project"}, {"workspace_id": True}, {"desired_state": "executing"},
    {"engine_generation": True}, {"reconnect_cursor": -1}, {"reconnect_cursor": tickets.MAX_CURSOR + 1},
    {"receipt_refs": [{"type": "x"}]}, {"receipt_refs": [{"type": "output", "id": "/absolute/path"}]},
])
def test_invalid_input_fails_before_write(store, change):
    value = _input(**change)
    with pytest.raises(tickets.TerminalTicketError) as invalid:
        store.create(value, idempotency_key="bad")
    assert invalid.value.code == "invalid_argument"
    assert store.list(project_id="prj_" + "a" * 32, workspace_id="ws_" + "b" * 32)[0] == ()


@pytest.mark.parametrize("field,prefix", [("project_id", "prj_"), ("workspace_id", "ws_")])
@pytest.mark.parametrize("suffix", ["", "a" * 31, "A" * 32, "g" * 32, "a" * 33])
def test_registry_authority_ids_are_exact(field, prefix, suffix, store):
    with pytest.raises(tickets.TerminalTicketError) as invalid:
        store.create(_input(**{field: prefix + suffix}), idempotency_key="bad-id")
    assert invalid.value.code == "invalid_argument"


def test_open_missing_does_not_create_and_schema_drift_fails_closed(path: Path):
    with pytest.raises(tickets.TerminalTicketError) as missing:
        tickets.open_existing(path)
    assert missing.value.code == "schema_missing" and not path.exists()
    tickets.initialize(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE terminal_tickets ADD COLUMN drift TEXT")
    drifted = path.read_bytes()
    with pytest.raises(tickets.TerminalTicketError) as drift:
        tickets.open_existing(path)
    assert drift.value.code == "schema_fingerprint_mismatch" and path.read_bytes() == drifted


def test_materialization_failure_is_sanitized_and_connection_closes(store, monkeypatch):
    item = store.create(_input(), idempotency_key="first")
    real_connect, tracked = tickets._connect, []
    class Tracked:
        def __init__(self, connection): self.connection, self.closed = connection, False
        def __getattr__(self, name): return getattr(self.connection, name)
        def close(self): self.connection.close(); self.closed = True
    def connect(path, *, write):
        value = Tracked(real_connect(path, write=write)); tracked.append(value); return value
    monkeypatch.setattr(tickets, "_connect", connect)
    monkeypatch.setattr(tickets, "_record", lambda _row: (_ for _ in ()).throw(IndexError("private sqlite detail")))
    with pytest.raises(tickets.TerminalTicketError) as failure:
        store.get(project_id=item.project_id, workspace_id=item.workspace_id, ticket_id=item.ticket_id)
    _sanitized(failure, "store_corrupt", "private sqlite detail")
    assert tracked[0].closed is True


@pytest.mark.parametrize("target", ["restart", "get", "list", "replay"])
def test_persisted_ticket_or_ledger_corruption_fails_closed(store, path: Path, target: str):
    item = store.create(_input(), idempotency_key="first")
    with sqlite3.connect(path) as connection:
        if target == "replay":
            connection.execute("UPDATE terminal_ticket_idempotency SET workspace_id=?", ("ws_" + "d" * 32,))
        else:
            connection.execute("UPDATE terminal_tickets SET receipt_refs_json='[1]' WHERE ticket_id=?", (item.ticket_id,))
    with pytest.raises(tickets.TerminalTicketError) as corrupt:
        if target == "restart":
            tickets.open_existing(path)
        elif target == "get":
            store.get(project_id=item.project_id, workspace_id=item.workspace_id, ticket_id=item.ticket_id)
        elif target == "list":
            store.list(project_id=item.project_id, workspace_id=item.workspace_id)
        else:
            store.create(_input(), idempotency_key="first")
    _sanitized(corrupt, "store_corrupt", "private-ledger-value")


def test_materialization_checks_all_ticket_fields(store, path: Path):
    item = store.create(_input(), idempotency_key="first")
    corruptions = [
        ("desired_state", "bad"), ("engine_generation", 0), ("reconnect_cursor", tickets.MAX_CURSOR + 1),
        ("revision", "9223372036854775808"), ("created_at", "2026-01-01T00:00:00+00:00"),
        ("updated_at", "2025-01-01T00:00:00.000000Z"), ("project_id", "prj_" + "A" * 32),
    ]
    for column, value in corruptions:
        with sqlite3.connect(path) as connection:
            connection.execute(f"UPDATE terminal_tickets SET {column}=? WHERE ticket_id=?", (value, item.ticket_id))
        with pytest.raises(tickets.TerminalTicketError) as corrupt:
            tickets.open_existing(path)
        assert corrupt.value.code == "store_corrupt"
        with sqlite3.connect(path) as connection:
            connection.execute("DELETE FROM terminal_tickets")
            connection.execute("DELETE FROM terminal_ticket_idempotency")
        item = store.create(_input(), idempotency_key="first")


@pytest.mark.parametrize("key", ["", "x" * 129, "bad key", "bad\tkey", "bad\nkey", "bad\x01key"])
@pytest.mark.parametrize("target", ["restart", "get", "list", "replay"])
def test_persisted_idempotency_key_is_fully_validated(store, path: Path, key: str, target: str):
    item = store.create(_input(), idempotency_key="first")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE terminal_ticket_idempotency SET idempotency_key=?", (key,))
    with pytest.raises(tickets.TerminalTicketError) as corrupt:
        if target == "restart":
            tickets.open_existing(path)
        elif target == "get":
            store.get(project_id=item.project_id, workspace_id=item.workspace_id, ticket_id=item.ticket_id)
        elif target == "list":
            store.list(project_id=item.project_id, workspace_id=item.workspace_id)
        else:
            store.create(_input(), idempotency_key="first")
    assert corrupt.value.code == "store_corrupt"


@pytest.mark.parametrize("kind", ["missing", "duplicate", "malformed"])
@pytest.mark.parametrize("target", ["restart", "get", "list", "replay"])
def test_ticket_receipt_cardinality_fails_closed(store, path: Path, kind: str, target: str):
    item = store.create(_input(), idempotency_key="first")
    with sqlite3.connect(path) as connection:
        if kind == "missing":
            connection.execute("DELETE FROM terminal_ticket_idempotency")
        elif kind == "duplicate":
            connection.execute("INSERT INTO terminal_ticket_idempotency SELECT project_id,workspace_id,'second','create',request_digest,ticket_id,result_json FROM terminal_ticket_idempotency")
        else:
            connection.execute("UPDATE terminal_ticket_idempotency SET result_json='{}'")
    with pytest.raises(tickets.TerminalTicketError) as corrupt:
        if target == "restart":
            tickets.open_existing(path)
        elif target == "get":
            store.get(project_id=item.project_id, workspace_id=item.workspace_id, ticket_id=item.ticket_id)
        elif target == "list":
            store.list(project_id=item.project_id, workspace_id=item.workspace_id)
        else:
            store.create(_input(), idempotency_key="first")
    assert corrupt.value.code == "store_corrupt"


def test_injected_g3_get_is_scoped(store):
    item = store.create(_input(), idempotency_key="first")
    app = FastAPI(); api.install(app, api.TerminalTicketApiService(lambda: store)); http = TestClient(app)
    body = http.get(f"/api/projects/{item.project_id}/workspaces/{item.workspace_id}/terminal-tickets/{item.ticket_id}").json()
    assert set(body) == {"data", "meta"} and body["data"]["ticket_id"] == item.ticket_id
    denied = http.get(f"/api/projects/{item.project_id}/workspaces/ws_{'d' * 32}/terminal-tickets/{item.ticket_id}")
    assert denied.status_code == 404 and set(denied.json()["error"]) == {"code", "message", "retryable", "request_id", "details"}
