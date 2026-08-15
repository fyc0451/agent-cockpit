from __future__ import annotations

import sqlite3
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from agent_cockpit import workspace_work_store as store_module


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
OTHER_PROJECT = "prj_" + "c" * 32
OTHER_WORKSPACE = "ws_" + "d" * 32
TABLES = store_module.DOMAIN_TABLES


def _create(store, **changes):
    payload = {
        "project_id": PROJECT,
        "workspace_id": WORKSPACE,
        "body": "Save the original Boss question",
        "acceptance": None,
        "constraints": None,
        "idempotency_key": "create-1",
    }
    payload.update(changes)
    return store.create_work_item(**payload)


def _counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        return {
            table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in TABLES
        }


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / "workspace-work.sqlite3"


@pytest.fixture()
def store(path: Path):
    value = store_module.initialize(path)
    yield value
    value.close()


def test_fresh_schema_create_reopen_same_ids_and_revision(store, path: Path) -> None:
    rows = sqlite3.connect(path).execute(
        "SELECT migration_id, schema_version, schema_digest FROM schema_migrations"
    ).fetchall()
    assert rows == [(
        store_module.MIGRATION_ID, store_module.SCHEMA_VERSION,
        store_module.SCHEMA_DIGEST,
    )]
    created = _create(
        store, body="  keep original spacing  ",
        acceptance=" Must stay readable ",
        constraints=" Keep it local ",
    )
    item = created.item.public_dict()
    assert created.status_code == 201
    assert set(item) == {"thread", "root_message", "work_item"}
    assert set(item["thread"]) == {
        "thread_id", "project_id", "workspace_id", "revision", "created_at",
    }
    assert set(item["root_message"]) == {
        "message_id", "thread_id", "author_kind", "author_ref", "body",
    }
    assert set(item["work_item"]) == {
        "work_item_id", "source_message_id", "status", "acceptance", "constraints",
    }
    assert item["thread"]["project_id"] == PROJECT
    assert item["thread"]["workspace_id"] == WORKSPACE
    assert item["thread"]["revision"] == 1
    assert item["root_message"]["author_kind"] == "boss"
    assert item["root_message"]["author_ref"] is None
    assert item["root_message"]["body"] == "  keep original spacing  "
    assert item["work_item"]["source_message_id"] == item["root_message"]["message_id"]
    assert item["work_item"]["status"] == "unassigned"
    assert item["work_item"]["acceptance"] == " Must stay readable "
    assert "workspace_id" not in item["work_item"]
    assert "project_id" not in item["work_item"]
    assert "source_message" not in item
    assert _counts(path) == {
        "message_threads": 1, "messages": 1, "work_items": 1, "idempotency_records": 1,
    }
    store.close()
    reopened = store_module.open_existing(path)
    listed = reopened.list_work_items(project_id=PROJECT, workspace_id=WORKSPACE)
    assert [entry.public_dict() for entry in listed] == [item]
    assert listed[0].thread["revision"] == item["thread"]["revision"]
    reopened.close()


def test_four_fault_points_leave_zero_rows_for_intent(store, path: Path, monkeypatch) -> None:
    hooks = (
        "_after_thread_insert",
        "_after_root_message_insert",
        "_after_work_item_insert",
        "_after_receipt_insert",
    )
    for index, name in enumerate(hooks):
        def fail(_connection, injected=name):
            raise store_module.WorkspaceWorkError("store_write_failed")

        monkeypatch.setattr(store_module, name, fail)
        with pytest.raises(store_module.WorkspaceWorkError) as error:
            _create(store, idempotency_key=f"fault-{index}")
        assert error.value.code == "store_write_failed"
        assert _counts(path) == {
            "message_threads": 0, "messages": 0, "work_items": 0,
            "idempotency_records": 0,
        }
        monkeypatch.undo()
    created = _create(store, idempotency_key="after-faults")
    assert created.status_code == 201
    assert _counts(path) == {
        "message_threads": 1, "messages": 1, "work_items": 1, "idempotency_records": 1,
    }


def _assert_sanitized(info: pytest.ExceptionInfo[BaseException], code: str, *secrets: str) -> None:
    error = info.value
    assert isinstance(error, store_module.WorkspaceWorkError)
    assert error.code == code
    assert str(error) == code
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    for secret in secrets:
        assert secret not in rendered


def test_same_key_replay_and_concurrent_barrier_creates_one_aggregate(
    store, path: Path,
) -> None:
    first = _create(store, idempotency_key="same")
    replay = _create(store, idempotency_key="same")
    assert replay.status_code == first.status_code == 201
    assert replay.item.public_dict() == first.item.public_dict()
    assert _counts(path) == {
        "message_threads": 1, "messages": 1, "work_items": 1, "idempotency_records": 1,
    }

    barrier = Barrier(8)
    started = store_module.initialize(path.parent / "concurrent.sqlite3")

    def worker():
        barrier.wait()
        return _create(started, idempotency_key="race")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [future.result() for future in [pool.submit(worker) for _ in range(8)]]
    publics = [item.item.public_dict() for item in results]
    assert all(item == publics[0] for item in publics)
    assert _counts(path.parent / "concurrent.sqlite3") == {
        "message_threads": 1, "messages": 1, "work_items": 1, "idempotency_records": 1,
    }
    started.close()


def test_same_key_different_body_conflicts(store, path: Path) -> None:
    first = _create(store, idempotency_key="same")
    with pytest.raises(store_module.WorkspaceWorkError) as conflict:
        _create(store, body="A different Boss question", idempotency_key="same")
    _assert_sanitized(conflict, "idempotency_conflict")
    assert store.list_work_items(project_id=PROJECT, workspace_id=WORKSPACE) == (
        first.item,
    )
    assert _counts(path) == {
        "message_threads": 1, "messages": 1, "work_items": 1, "idempotency_records": 1,
    }


def test_list_is_stable_and_isolated(store) -> None:
    first = _create(store, body="alpha-one", idempotency_key="a1")
    second = _create(store, body="alpha-two", idempotency_key="a2")
    other = _create(
        store, project_id=OTHER_PROJECT, workspace_id=OTHER_WORKSPACE,
        body="other", idempotency_key="same",
    )
    sibling = _create(
        store, workspace_id=OTHER_WORKSPACE, body="sibling", idempotency_key="sib",
    )
    listed = store.list_work_items(project_id=PROJECT, workspace_id=WORKSPACE)
    assert [item.public_dict() for item in listed] == [
        first.item.public_dict(), second.item.public_dict(),
    ]
    assert store.list_work_items(
        project_id=PROJECT, workspace_id=OTHER_WORKSPACE,
    ) == (sibling.item,)
    assert store.list_work_items(
        project_id=OTHER_PROJECT, workspace_id=OTHER_WORKSPACE,
    ) == (other.item,)
    assert store.list_work_items(
        project_id=OTHER_PROJECT, workspace_id=WORKSPACE,
    ) == ()


def test_store_rejects_blank_control_and_overlong_fields(store, path: Path) -> None:
    with pytest.raises(store_module.WorkspaceWorkError) as blank:
        _create(store, body="   ")
    _assert_sanitized(blank, "invalid_argument")
    with pytest.raises(store_module.WorkspaceWorkError) as nul:
        _create(store, body="ok\x00no")
    _assert_sanitized(nul, "invalid_argument")
    with pytest.raises(store_module.WorkspaceWorkError) as long_body:
        _create(store, body="x" * 32769)
    _assert_sanitized(long_body, "invalid_argument")
    with pytest.raises(store_module.WorkspaceWorkError) as long_note:
        _create(store, constraints="y" * 8193)
    _assert_sanitized(long_note, "invalid_argument")
    assert _create(store, body="x" * 32768, acceptance="y" * 8192).status_code == 201
    assert _counts(path) == {
        "message_threads": 1, "messages": 1, "work_items": 1, "idempotency_records": 1,
    }


def test_busy_corrupt_and_fingerprint_are_sanitized(store, path: Path, monkeypatch) -> None:
    def fail_write(_path, *, write):
        if write:
            raise sqlite3.OperationalError("database is locked /private/write-failed")
        raise sqlite3.Error("/private/read-failed")

    monkeypatch.setattr(store_module, "_connect", fail_write)
    with pytest.raises(store_module.WorkspaceWorkError) as write_failed:
        _create(store)
    _assert_sanitized(write_failed, "store_write_failed", "/private/write-failed")
    with pytest.raises(store_module.WorkspaceWorkError) as read_failed:
        store.list_work_items(project_id=PROJECT, workspace_id=WORKSPACE)
    _assert_sanitized(read_failed, "store_read_failed", "/private/read-failed")
    monkeypatch.undo()
    assert _counts(path) == {
        "message_threads": 0, "messages": 0, "work_items": 0, "idempotency_records": 0,
    }

    damaged = path.parent / "damaged.sqlite3"
    damaged.write_bytes(b"not a database /secret/path")
    damaged.chmod(0o600)
    with pytest.raises(store_module.WorkspaceWorkError) as corrupt:
        store_module.open_existing(damaged)
    _assert_sanitized(corrupt, "store_corrupt", "/secret/path")

    drifted = path.parent / "drift.sqlite3"
    drifted_store = store_module.initialize(drifted)
    drifted_store.close()
    with sqlite3.connect(drifted) as connection:
        connection.execute("CREATE TABLE agents(id TEXT)")
        connection.commit()
    with pytest.raises(store_module.WorkspaceWorkError) as mismatch:
        store_module.open_existing(drifted)
    _assert_sanitized(mismatch, "schema_fingerprint_mismatch")

    missing = path.parent / "missing-schema.sqlite3"
    sqlite3.connect(missing).execute("CREATE TABLE messages(id TEXT)").connection.close()
    missing.chmod(0o600)
    with pytest.raises(store_module.WorkspaceWorkError) as schema_missing:
        store_module.open_existing(missing)
    _assert_sanitized(schema_missing, "workspace_work_schema_missing")

    _create(store, idempotency_key="receipt-lock")
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE idempotency_records SET request_digest='x'")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM idempotency_records")


def test_request_path_skips_full_schema_fingerprint(store, path: Path, monkeypatch) -> None:
    def boom(_connection):
        raise AssertionError("full fingerprint on request path")

    monkeypatch.setattr(store_module, "_schema_objects", boom)
    created = _create(store, idempotency_key="light-gate")
    assert created.status_code == 201
    listed = store.list_work_items(project_id=PROJECT, workspace_id=WORKSPACE)
    assert [item.public_dict() for item in listed] == [created.item.public_dict()]
    store.close()
    with pytest.raises(AssertionError, match="full fingerprint on request path"):
        store_module.open_existing(path)
    monkeypatch.undo()
    reopened = store_module.open_existing(path)
    assert [item.public_dict() for item in reopened.list_work_items(
        project_id=PROJECT, workspace_id=WORKSPACE,
    )] == [created.item.public_dict()]
    reopened.close()


def test_commit_failure_leaves_zero_rows(store, path: Path, monkeypatch) -> None:
    real_connect = store_module._connect

    class _FailCommit:
        def __init__(self, inner: sqlite3.Connection) -> None:
            object.__setattr__(self, "_inner", inner)

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().split()[0].upper() == "COMMIT":
                raise sqlite3.OperationalError("disk I/O error /private/commit")
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    def connect(target, *, write):
        return _FailCommit(real_connect(target, write=write))

    monkeypatch.setattr(store_module, "_connect", connect)
    with pytest.raises(store_module.WorkspaceWorkError) as failed:
        _create(store, idempotency_key="commit-fail")
    _assert_sanitized(failed, "store_write_failed", "/private/commit")
    monkeypatch.undo()
    assert _counts(path) == {
        "message_threads": 0, "messages": 0, "work_items": 0, "idempotency_records": 0,
    }
