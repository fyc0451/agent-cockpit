from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from agent_cockpit import operation_store as operation_mod
from agent_cockpit import workspace_claim_activation as activate_mod
from agent_cockpit import workspace_execution_store as store_module
from agent_cockpit import workspace_work_store as work_mod


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
STAMP = "2026-08-16T12:00:00.000000Z"


@pytest.fixture()
def execution(tmp_path: Path):
    value = store_module.initialize(tmp_path / "workspace-execution.sqlite3")
    yield value
    value.close()


def _member(store):
    return store.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE, display_name="Atlas",
        idempotency_key="member",
    ).item


def _prepare(store, tmp_path: Path, identity_id: str, work_item_id: str):
    dest = tmp_path / "checkout"
    dest.mkdir()
    return store.complete_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        identity_id=identity_id, source_head="a" * 40, source_tree="b" * 40,
        internal_path=str(dest), operation_id=None,
    ), dest


def _attach(store, work_item_id: str, revision: int):
    attaching, attachment, _checkout = store.begin_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_revision=revision, session_name="s",
    )
    finished = store.finish_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_revision=attaching.revision, pane_id="pane-1",
        instance_id="inst-1", native_receipt="secret", identity_verified=True,
    )
    return finished, attachment.attachment_id


def test_activate_claim_lease_binds_claim_and_hides_fence(
    execution, tmp_path: Path,
) -> None:
    work_item_id = "wrk_" + "c" * 32
    member = _member(execution)
    prepared, _dest = _prepare(execution, tmp_path, member.identity_id, work_item_id)
    connected, attachment_id = _attach(execution, work_item_id, prepared.revision)
    claim_id = "clm_" + "1" * 32
    payload = execution.activate_claim_lease(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_preparation_revision=connected.revision,
        expected_lease_revision=connected.lease.revision,
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, claim_id=claim_id, idempotency_key="act-1",
    )
    assert payload["lease"]["status"] == "active"
    assert payload["lease"]["claim_id"] == claim_id
    assert payload["lease"]["revision"] == connected.lease.revision + 1
    assert "fence" not in str(payload)
    assert "secret" not in str(payload)
    replay = execution.activate_claim_lease(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_preparation_revision=connected.revision,
        expected_lease_revision=connected.lease.revision,
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, claim_id=claim_id, idempotency_key="act-1",
    )
    assert replay == payload
    with pytest.raises(store_module.WorkspaceExecutionError) as stale:
        execution.activate_claim_lease(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
            expected_preparation_revision=connected.revision,
            expected_lease_revision=9,
            attachment_id=attachment_id, identity_id=member.identity_id,
            generation=1, claim_id=claim_id, idempotency_key="act-2",
        )
    assert stale.value.code == "stale_revision"
    with pytest.raises(store_module.WorkspaceExecutionError) as gen:
        execution.activate_claim_lease(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
            expected_preparation_revision=connected.revision,
            expected_lease_revision=connected.lease.revision,
            attachment_id=attachment_id, identity_id=member.identity_id,
            generation=2, claim_id=claim_id, idempotency_key="act-3",
        )
    assert gen.value.code == "stale_generation"


def test_tool_and_reply_lease_states(execution, tmp_path: Path) -> None:
    work_item_id = "wrk_" + "c" * 32
    member = _member(execution)
    prepared, _dest = _prepare(execution, tmp_path, member.identity_id, work_item_id)
    connected, attachment_id = _attach(execution, work_item_id, prepared.revision)
    claim_id = "clm_" + "1" * 32
    active = execution.activate_claim_lease(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_preparation_revision=connected.revision,
        expected_lease_revision=connected.lease.revision,
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, claim_id=claim_id, idempotency_key="act",
    )
    op = "op_" + "a" * 32
    begun = execution.begin_tool(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_preparation_revision=connected.revision,
        expected_lease_revision=active["lease"]["revision"],
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, claim_id=claim_id, operation_id=op,
        operation_digest="sha256:" + "ab" * 32, idempotency_key="tool-b",
    )
    other = "op_" + "b" * 32
    with pytest.raises(store_module.WorkspaceExecutionError) as busy:
        execution.begin_tool(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
            expected_preparation_revision=connected.revision,
            expected_lease_revision=begun["lease"]["revision"],
            attachment_id=attachment_id, identity_id=member.identity_id,
            generation=1, claim_id=claim_id, operation_id=other,
            operation_digest="sha256:" + "cd" * 32, idempotency_key="tool-x",
        )
    assert busy.value.code == "reconcile_required"
    unknown = execution.finish_tool(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_preparation_revision=connected.revision,
        expected_lease_revision=begun["lease"]["revision"],
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, claim_id=claim_id, operation_id=op,
        outcome="outcome_unknown", idempotency_key="tool-u",
    )
    assert unknown["lease"]["status"] == "uncertain"
    with pytest.raises(store_module.WorkspaceExecutionError) as blocked:
        execution.begin_reply(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
            expected_preparation_revision=connected.revision,
            expected_lease_revision=unknown["lease"]["revision"],
            attachment_id=attachment_id, identity_id=member.identity_id,
            generation=1, claim_id=claim_id, idempotency_key="rep-bad",
        )
    assert blocked.value.code == "reconcile_required"
    recovered = execution.finish_tool(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_preparation_revision=connected.revision,
        expected_lease_revision=unknown["lease"]["revision"],
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, claim_id=claim_id, operation_id=op,
        outcome="failed", idempotency_key="tool-f",
    )
    assert recovered["lease"]["status"] == "active"
    revoking = execution.begin_reply(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_preparation_revision=connected.revision,
        expected_lease_revision=recovered["lease"]["revision"],
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, claim_id=claim_id, idempotency_key="rep-b",
    )
    assert revoking["lease"]["status"] == "revoking"
    with pytest.raises(store_module.WorkspaceExecutionError) as frozen:
        execution.begin_tool(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
            expected_preparation_revision=connected.revision,
            expected_lease_revision=revoking["lease"]["revision"],
            attachment_id=attachment_id, identity_id=member.identity_id,
            generation=1, claim_id=claim_id, operation_id="op_" + "c" * 32,
            operation_digest="sha256:" + "ef" * 32, idempotency_key="tool-z",
        )
    assert frozen.value.code == "lease_not_active"
    done = execution.finish_reply(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_preparation_revision=connected.revision,
        expected_lease_revision=revoking["lease"]["revision"],
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, claim_id=claim_id, idempotency_key="rep-f",
    )
    assert done["lease"]["status"] == "revoked"
    again = execution.finish_reply(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_preparation_revision=connected.revision,
        expected_lease_revision=revoking["lease"]["revision"],
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, claim_id=claim_id, idempotency_key="rep-f",
    )
    assert again == done


def test_v1_execution_store_migrates_reserved_lease(tmp_path: Path) -> None:
    path = tmp_path / "workspace-execution.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
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
        connection.execute("COMMIT")
    finally:
        connection.close()
    store = store_module.open_existing(path)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 2
    cols = [
        row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(writer_leases)")
    ]
    assert "claim_id" in cols
    assert "updated_at" in cols
    store.close()


def test_activator_returns_boss_body_only_after_active(tmp_path: Path) -> None:
    execution = store_module.initialize(tmp_path / "workspace-execution.sqlite3")
    work = work_mod.initialize(tmp_path / "workspace-work.sqlite3")
    operations = operation_mod.initialize(tmp_path / "operation.sqlite3")
    member = _member(execution)
    created = work.create_work_item(
        project_id=PROJECT, workspace_id=WORKSPACE, body="SECRET-SENTINEL",
        acceptance="done", constraints="local", idempotency_key="create",
    )
    work_item_id = created.item.work_item["work_item_id"]
    prepared, _dest = _prepare(execution, tmp_path, member.identity_id, work_item_id)
    connected, attachment_id = _attach(execution, work_item_id, prepared.revision)
    reserved = work.reserve_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        identity_id=member.identity_id, generation=1,
        expected_revision=1,
        idempotency_key="reserve",
    )
    activated = activate_mod.ClaimActivator(
        execution=execution, work=work, operations=operations,
    ).activate(
        {
            "project_id": PROJECT,
            "workspace_id": WORKSPACE,
            "work_item_id": work_item_id,
            "expected_preparation_revision": connected.revision,
            "expected_lease_revision": connected.lease.revision,
            "expected_work_revision": 1,
            "attachment_id": attachment_id,
            "identity_id": member.identity_id,
            "generation": 1,
        },
        reserved,
        idempotency_key="activate",
    )
    assert activated["lease"]["status"] == "active"
    assert activated["claim"]["state"] == "active"
    assert activated["root_message"]["body"] == "SECRET-SENTINEL"
    replay = activate_mod.ClaimActivator(
        execution=execution, work=work, operations=operations,
    ).activate(
        {
            "project_id": PROJECT,
            "workspace_id": WORKSPACE,
            "work_item_id": work_item_id,
            "expected_preparation_revision": connected.revision,
            "expected_lease_revision": connected.lease.revision,
            "expected_work_revision": 1,
            "attachment_id": attachment_id,
            "identity_id": member.identity_id,
            "generation": 1,
        },
        reserved,
        idempotency_key="activate",
    )
    assert replay["claim"]["claim_id"] == activated["claim"]["claim_id"]
    execution.close()
    work.close()
    operations.close()
