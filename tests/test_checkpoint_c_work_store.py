from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import workspace_work_api as api
from agent_cockpit import workspace_work_store as store_module


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
IDENTITY = "idn_" + "1" * 32
OTHER_IDENTITY = "idn_" + "2" * 32
STAMP = "2026-08-15T12:00:00.000000Z"


def _create(store, **changes):
    payload = {
        "project_id": PROJECT,
        "workspace_id": WORKSPACE,
        "body": "Boss root that must stay secret until activate",
        "acceptance": "done when saved",
        "constraints": "local only",
        "idempotency_key": "create-1",
    }
    payload.update(changes)
    return store.create_work_item(**payload)


def _user_version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _write_v1(path: Path, *, body: str = "legacy boss body") -> dict[str, str]:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    thread_id = "thr_" + "a" * 32
    message_id = "msg_" + "b" * 32
    work_item_id = "wrk_" + "c" * 32
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in store_module.V1_SCHEMA:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=1")
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?,?,?,?)",
            (
                store_module.V1_MIGRATION_ID, 1,
                store_module.V1_SCHEMA_DIGEST, STAMP,
            ),
        )
        connection.execute(
            "INSERT INTO message_threads VALUES (?,?,?,1,?)",
            (thread_id, PROJECT, WORKSPACE, STAMP),
        )
        connection.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?)",
            (message_id, thread_id, "boss", None, body),
        )
        connection.execute(
            "INSERT INTO work_items VALUES (?,?,?,?,?)",
            (work_item_id, message_id, "unassigned", "acc", "con"),
        )
        connection.execute(
            "INSERT INTO idempotency_records VALUES (?,?,?,?,?)",
            (
                PROJECT, WORKSPACE, "legacy-create",
                "d" * 64,
                json.dumps({"kept": True}, separators=(",", ":")),
            ),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return {
        "thread_id": thread_id,
        "message_id": message_id,
        "work_item_id": work_item_id,
        "body": body,
    }


def _write_v2(path: Path) -> str:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    work_item_id = "wrk_" + "f" * 32
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in store_module.V2_SCHEMA:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=2")
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?,?,?,?)",
            (
                store_module.V2_MIGRATION_ID, store_module.V2_SCHEMA_VERSION,
                store_module.V2_SCHEMA_DIGEST, STAMP,
            ),
        )
        connection.execute(
            "INSERT INTO message_threads VALUES (?,?,?,1,?)",
            ("thr_" + "d" * 32, PROJECT, WORKSPACE, STAMP),
        )
        connection.execute(
            "INSERT INTO messages VALUES (?,?,1,'root','boss',NULL,NULL,NULL,?,?)",
            ("msg_" + "e" * 32, "thr_" + "d" * 32, "v2 body", STAMP),
        )
        connection.execute(
            "INSERT INTO work_items VALUES (?,?,'unassigned',NULL,NULL,1,?)",
            (work_item_id, "msg_" + "e" * 32, STAMP),
        )
        connection.commit()
    finally:
        connection.close()
    return work_item_id


def test_v1_migrates_lossless_ids_and_keeps_create_list(tmp_path: Path) -> None:
    path = tmp_path / "workspace-work.sqlite3"
    legacy = _write_v1(path)
    assert _user_version(path) == 1
    store = store_module.open_existing(path)
    assert _user_version(path) == store_module.SCHEMA_VERSION
    listed = store.list_work_items(project_id=PROJECT, workspace_id=WORKSPACE)
    assert len(listed) == 1
    item = listed[0].public_dict()
    assert item["thread"]["thread_id"] == legacy["thread_id"]
    assert item["thread"]["revision"] == 1
    assert item["thread"]["created_at"] == STAMP
    assert item["root_message"]["message_id"] == legacy["message_id"]
    assert item["root_message"]["author_kind"] == "boss"
    assert item["root_message"]["body"] == legacy["body"]
    assert item["work_item"]["work_item_id"] == legacy["work_item_id"]
    assert item["work_item"]["status"] == "unassigned"
    assert item["work_item"]["acceptance"] == "acc"
    assert item["work_item"]["constraints"] == "con"
    with sqlite3.connect(path) as connection:
        message_names = [
            column[1] for column in connection.execute("PRAGMA table_info(messages)")
        ]
        row = dict(zip(
            message_names,
            connection.execute("SELECT * FROM messages").fetchone(),
        ))
        assert row["ordinal"] == 1
        assert row["message_kind"] == "root"
        assert row["author_generation"] is None
        assert row["reply_to_message_id"] is None
        assert row["created_at"] == STAMP
        work_names = [
            column[1] for column in connection.execute("PRAGMA table_info(work_items)")
        ]
        work = dict(zip(
            work_names,
            connection.execute("SELECT * FROM work_items").fetchone(),
        ))
        assert work["revision"] == 1
        scope, key = connection.execute(
            "SELECT command_scope, idempotency_key FROM idempotency_records"
        ).fetchone()
        assert scope == store_module.CREATE_SCOPE
        assert key == "legacy-create"
        migrations = [
            row[0] for row in connection.execute(
                "SELECT migration_id FROM schema_migrations ORDER BY schema_version"
            )
        ]
        assert store_module.V1_MIGRATION_ID in migrations
        assert store_module.V2_MIGRATION_ID in migrations
        assert store_module.MIGRATION_ID in migrations
    created = _create(store, idempotency_key="after-migrate")
    assert created.status_code == 201
    assert created.item.work_item["status"] == "unassigned"
    store.close()


def test_v1_migration_faults_leave_complete_v1_or_v2(tmp_path: Path, monkeypatch) -> None:
    hooks = (
        "_after_migration_copy",
        "_after_migration_swap",
        "_after_migration_receipt",
    )
    for name in hooks:
        path = tmp_path / f"{name}.sqlite3"
        legacy = _write_v1(path, body=f"keep-{name}")
        monkeypatch.setattr(
            store_module, name,
            lambda _connection: (_ for _ in ()).throw(
                store_module.WorkspaceWorkError("store_write_failed")
            ),
        )
        with pytest.raises(store_module.WorkspaceWorkError) as error:
            store_module.open_existing(path)
        assert error.value.code == "store_write_failed"
        assert _user_version(path) == 1
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT body FROM messages").fetchone()[0] == (
                f"keep-{name}"
            )
            assert connection.execute("SELECT count(*) FROM work_items").fetchone()[0] == 1
            names = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "work_item_claims" not in names
            assert "message_receipts" not in names
        monkeypatch.undo()
        store = store_module.open_existing(path)
        assert _user_version(path) == store_module.SCHEMA_VERSION
        listed = store.list_work_items(project_id=PROJECT, workspace_id=WORKSPACE)
        assert listed[0].root_message["body"] == f"keep-{name}"
        assert listed[0].work_item["work_item_id"] == legacy["work_item_id"]
        store.close()


def test_v2_migrates_to_v3_without_inventing_isolated_scope(tmp_path: Path) -> None:
    path = tmp_path / "v2.sqlite3"
    work_item_id = _write_v2(path)
    store = store_module.open_existing(path)
    assert _user_version(path) == store_module.SCHEMA_VERSION
    item = store.get_work_item(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
    )
    assert item is not None
    assert item.root_message["body"] == "v2 body"
    assert "allowed_paths" not in item.work_item
    assert store.get_allowed_paths(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
    ) is None
    store.close()


def test_v2_to_v3_fault_rolls_back_schema_and_version(
    tmp_path: Path, monkeypatch,
) -> None:
    path = tmp_path / "v2-fault.sqlite3"
    _write_v2(path)
    monkeypatch.setattr(
        store_module, "_after_v3_schema",
        lambda _connection: (_ for _ in ()).throw(
            store_module.WorkspaceWorkError("store_write_failed")
        ),
    )
    with pytest.raises(store_module.WorkspaceWorkError) as error:
        store_module.open_existing(path)
    assert error.value.code == "store_write_failed"
    assert _user_version(path) == store_module.V2_SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        names = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "work_item_deliveries" not in names


def test_drifted_v1_stops_without_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "drift-v1.sqlite3"
    _write_v1(path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE agents(id TEXT)")
        connection.commit()
    with pytest.raises(store_module.WorkspaceWorkError) as error:
        store_module.open_existing(path)
    assert error.value.code == "schema_fingerprint_mismatch"
    assert _user_version(path) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT body FROM messages").fetchone()[0] == (
            "legacy boss body"
        )


def test_reserve_omits_boss_body_and_activate_is_one_transaction(
    tmp_path: Path, monkeypatch,
) -> None:
    store = store_module.initialize(tmp_path / "workspace-work.sqlite3")
    created = _create(store)
    work_id = created.item.work_item["work_item_id"]
    reserved = store.reserve_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=IDENTITY, generation=1, expected_revision=1,
        idempotency_key="reserve-1",
    )
    dumped = json.dumps(reserved)
    assert "Boss root" not in dumped
    assert "done when saved" not in dumped
    assert reserved["claim"]["state"] == "pending_gate"
    assert reserved["claim"]["identity_id"] == IDENTITY
    assert reserved["claim"]["generation"] == 1
    assert created.item.work_item["status"] == "unassigned"
    listed = store.list_work_items(project_id=PROJECT, workspace_id=WORKSPACE)
    assert listed[0].work_item["status"] == "unassigned"
    assert listed[0].root_message["body"].startswith("Boss root")

    def fail(_connection):
        raise store_module.WorkspaceWorkError("store_write_failed")

    monkeypatch.setattr(store_module, "_after_claim_activate", fail)
    with pytest.raises(store_module.WorkspaceWorkError) as error:
        store.activate_claim(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
            claim_id=reserved["claim"]["claim_id"], identity_id=IDENTITY,
            generation=1, expected_claim_revision=1, expected_work_revision=1,
            idempotency_key="activate-fail",
        )
    assert error.value.code == "store_write_failed"
    monkeypatch.undo()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT status FROM work_items").fetchone()[0] == (
            "unassigned"
        )
        assert connection.execute("SELECT state FROM work_item_claims").fetchone()[0] == (
            "pending_gate"
        )
        assert connection.execute("SELECT count(*) FROM message_receipts").fetchone()[0] == 0

    activated = store.activate_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        claim_id=reserved["claim"]["claim_id"], identity_id=IDENTITY,
        generation=1, expected_claim_revision=1, expected_work_revision=1,
        idempotency_key="activate-1",
    )
    assert activated["claim"]["state"] == "active"
    assert activated["work_item"]["status"] == "working"
    assert activated["root_message"]["body"].startswith("Boss root")
    assert activated["work_item"]["acceptance"] == "done when saved"
    with sqlite3.connect(store.path) as connection:
        kinds = [
            row[0] for row in connection.execute(
                "SELECT kind FROM message_receipts ORDER BY created_at"
            )
        ]
        assert kinds == ["claim"]
        receipt_text = json.dumps(list(connection.execute(
            "SELECT receipt_id, kind, outcome, reason, evidence_digest "
            "FROM message_receipts"
        ).fetchall()))
        assert "Boss root" not in receipt_text
    store.close()


def test_reply_complete_is_one_transaction_and_fault_leaves_zero_half(
    tmp_path: Path, monkeypatch,
) -> None:
    store = store_module.initialize(tmp_path / "workspace-work.sqlite3")
    created = _create(store)
    work_id = created.item.work_item["work_item_id"]
    reserved = store.reserve_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=IDENTITY, generation=1, expected_revision=1,
        idempotency_key="reserve-2",
    )
    activated = store.activate_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        claim_id=reserved["claim"]["claim_id"], identity_id=IDENTITY,
        generation=1, expected_claim_revision=1, expected_work_revision=1,
        idempotency_key="activate-2",
    )

    def fail(_connection):
        raise store_module.WorkspaceWorkError("store_write_failed")

    monkeypatch.setattr(store_module, "_after_reply_complete", fail)
    with pytest.raises(store_module.WorkspaceWorkError):
        store.reply_complete(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
            claim_id=reserved["claim"]["claim_id"], identity_id=IDENTITY,
            generation=1,
            expected_claim_revision=activated["claim"]["revision"],
            expected_work_revision=activated["work_item"]["revision"],
            body="agent finished the work",
            idempotency_key="reply-fail",
        )
    monkeypatch.undo()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM work_items").fetchone()[0] == (
            "working"
        )
        assert connection.execute("SELECT state FROM work_item_claims").fetchone()[0] == (
            "active"
        )
        assert connection.execute("SELECT count(*) FROM message_receipts").fetchone()[0] == 1

    completed = store.reply_complete(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        claim_id=reserved["claim"]["claim_id"], identity_id=IDENTITY,
        generation=1,
        expected_claim_revision=activated["claim"]["revision"],
        expected_work_revision=activated["work_item"]["revision"],
        body="agent finished the work",
        idempotency_key="reply-1",
    )
    assert completed["work_item"]["status"] == "completed"
    assert completed["claim"]["state"] == "closed"
    assert completed["reply_message"]["author_kind"] == "agent"
    assert completed["reply_message"]["body"] == "agent finished the work"
    with sqlite3.connect(store.path) as connection:
        kinds = [
            row[0] for row in connection.execute(
                "SELECT kind FROM message_receipts ORDER BY created_at, receipt_id"
            )
        ]
        assert kinds == ["claim", "reply", "complete"]
        assert connection.execute(
            "SELECT count(*) FROM messages WHERE message_kind='reply'"
        ).fetchone()[0] == 1
    store.close()


def test_eight_concurrent_reserves_create_one_current_claim(tmp_path: Path) -> None:
    store = store_module.initialize(tmp_path / "workspace-work.sqlite3")
    created = _create(store)
    work_id = created.item.work_item["work_item_id"]
    barrier = Barrier(8)

    def worker(index: int):
        barrier.wait()
        try:
            return store.reserve_claim(
                project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
                identity_id=IDENTITY if index % 2 == 0 else OTHER_IDENTITY,
                generation=1, expected_revision=1,
                idempotency_key=f"race-{index}",
            )
        except store_module.WorkspaceWorkError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [future.result() for future in [
            pool.submit(worker, index) for index in range(8)
        ]]
    wins = [item for item in results if isinstance(item, dict)]
    losses = [item for item in results if item == "claim_conflict"]
    assert len(wins) == 1
    assert len(losses) == 7
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM work_item_claims").fetchone()[0] == 1
        assert connection.execute("SELECT state FROM work_item_claims").fetchone()[0] == (
            "pending_gate"
        )
    store.close()


def test_stale_revision_and_generation_are_rejected(tmp_path: Path) -> None:
    store = store_module.initialize(tmp_path / "workspace-work.sqlite3")
    created = _create(store)
    work_id = created.item.work_item["work_item_id"]
    with pytest.raises(store_module.WorkspaceWorkError) as stale:
        store.reserve_claim(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
            identity_id=IDENTITY, generation=1, expected_revision=9,
            idempotency_key="stale-rev",
        )
    assert stale.value.code == "stale_revision"
    reserved = store.reserve_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=IDENTITY, generation=1, expected_revision=1,
        idempotency_key="ok-reserve",
    )
    with pytest.raises(store_module.WorkspaceWorkError) as generation:
        store.activate_claim(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
            claim_id=reserved["claim"]["claim_id"], identity_id=IDENTITY,
            generation=2, expected_claim_revision=1, expected_work_revision=1,
            idempotency_key="stale-gen",
        )
    assert generation.value.code == "stale_generation"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT status FROM work_items").fetchone()[0] == (
            "unassigned"
        )
        assert connection.execute("SELECT state FROM work_item_claims").fetchone()[0] == (
            "pending_gate"
        )
    store.close()


def test_idempotency_is_isolated_by_command_scope(tmp_path: Path) -> None:
    store = store_module.initialize(tmp_path / "workspace-work.sqlite3")
    created = _create(store, idempotency_key="shared")
    reserved = store.reserve_claim(
        project_id=PROJECT, workspace_id=WORKSPACE,
        work_item_id=created.item.work_item["work_item_id"],
        identity_id=IDENTITY, generation=1, expected_revision=1,
        idempotency_key="shared",
    )
    replay = store.reserve_claim(
        project_id=PROJECT, workspace_id=WORKSPACE,
        work_item_id=created.item.work_item["work_item_id"],
        identity_id=IDENTITY, generation=1, expected_revision=1,
        idempotency_key="shared",
    )
    assert replay == reserved
    with pytest.raises(store_module.WorkspaceWorkError) as conflict:
        store.reserve_claim(
            project_id=PROJECT, workspace_id=WORKSPACE,
            work_item_id=created.item.work_item["work_item_id"],
            identity_id=OTHER_IDENTITY, generation=1, expected_revision=1,
            idempotency_key="shared",
        )
    assert conflict.value.code == "idempotency_conflict"
    store.close()


class _Registry:
    def get_project_by_id(self, project_id: str):
        class Project:
            lifecycle = "active"

        class Snapshot:
            project = Project()

        return Snapshot() if project_id == PROJECT else None

    def get_workspace(self, project_id: str, workspace_id: str):
        if project_id != PROJECT or workspace_id != WORKSPACE:
            return None

        class Workspace:
            pass

        item = Workspace()
        item.project_id = PROJECT
        item.workspace_id = WORKSPACE
        item.lifecycle = "active"
        return item


def test_get_detail_exposes_messages_claim_receipts_without_secret_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace-work.sqlite3"
    store = store_module.initialize(path)
    created = _create(store)
    work_id = created.item.work_item["work_item_id"]
    reserved = store.reserve_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=IDENTITY, generation=1, expected_revision=1,
        idempotency_key="detail-reserve",
    )
    store.activate_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        claim_id=reserved["claim"]["claim_id"], identity_id=IDENTITY,
        generation=1, expected_claim_revision=1, expected_work_revision=1,
        idempotency_key="detail-activate",
    )
    app = FastAPI()
    api.install(app, api.ApiService(lambda: _Registry(), lambda: store))
    http = TestClient(app)
    response = http.get(
        f"/api/projects/{PROJECT}/workspaces/{WORKSPACE}/work-items/{work_id}"
    )
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"data", "meta"}
    data = payload["data"]
    assert set(data) == {"thread", "work_item", "claim", "receipts"}
    assert data["work_item"]["status"] == "working"
    assert data["work_item"]["revision"] == 2
    assert data["claim"]["state"] == "active"
    assert data["claim"]["generation"] == 1
    assert [item["message_kind"] for item in data["thread"]["messages"]] == ["root"]
    assert data["thread"]["messages"][0]["body"].startswith("Boss root")
    assert data["receipts"][0]["kind"] == "claim"
    dumped = response.text
    assert "pane" not in dumped
    assert "argv" not in dumped
    assert "token" not in dumped
    assert "fence" not in dumped
    missing = http.get(
        f"/api/projects/{PROJECT}/workspaces/{WORKSPACE}/work-items/"
        f"wrk_{'e' * 32}"
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "work_item_not_found"
    store.close()
