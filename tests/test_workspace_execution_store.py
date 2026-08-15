from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_cockpit import workspace_execution_store as store_module


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
WORK = "wrk_" + "c" * 32
OTHER_WORK = "wrk_" + "d" * 32


@pytest.fixture()
def store(tmp_path: Path):
    value = store_module.initialize(tmp_path / "workspace-execution.sqlite3")
    yield value
    value.close()


def test_members_are_idempotent_and_same_name_is_not_same_identity(store) -> None:
    first = store.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE, display_name="Atlas",
        idempotency_key="member-1",
    )
    replay = store.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE, display_name="Atlas",
        idempotency_key="member-1",
    )
    sibling = store.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE, display_name="Atlas",
        idempotency_key="member-2",
    )
    assert first.status_code == replay.status_code == 201
    assert first.item.public_dict() == replay.item.public_dict()
    assert sibling.item.identity_id != first.item.identity_id
    assert sibling.item.display_name == first.item.display_name
    with pytest.raises(store_module.WorkspaceExecutionError) as conflict:
        store.create_identity(
            project_id=PROJECT, workspace_id=WORKSPACE, display_name="Other",
            idempotency_key="member-1",
        )
    assert conflict.value.code == "idempotency_conflict"
    listed = store.list_identities(project_id=PROJECT, workspace_id=WORKSPACE)
    assert {item.identity_id for item in listed} == {
        first.item.identity_id, sibling.item.identity_id,
    }
    public = first.item.public_dict()
    assert set(public) == {
        "identity_id", "display_name", "role", "lifecycle", "revision",
    }
    assert "fence_digest" not in public


def test_preparation_persists_reserved_lease_and_hides_fence(store, tmp_path: Path) -> None:
    member = store.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE, display_name="Atlas",
        idempotency_key="m",
    )
    dest = tmp_path / "checkout"
    dest.mkdir()
    view = store.complete_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
        identity_id=member.item.identity_id, source_head="a" * 40,
        source_tree="b" * 40, internal_path=str(dest), operation_id=None,
    )
    payload = view.public_dict()
    assert payload["state"] == "prepared"
    assert payload["work_item_status"] == "unassigned"
    assert payload["lease"]["status"] == "reserved"
    assert payload["lease"]["generation"] == 1
    assert payload["checkout"]["source_head"] == "a" * 40
    assert "fence_digest" not in str(payload)
    assert "internal_path" not in str(payload)
    assert view.principal["generation"] == 1
    other = store.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE, display_name="Nova",
        idempotency_key="n",
    )
    with pytest.raises(store_module.WorkspaceExecutionError) as conflict:
        store.complete_preparation(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
            identity_id=other.item.identity_id, source_head="a" * 40,
            source_tree="b" * 40, internal_path=str(dest), operation_id=None,
        )
    assert conflict.value.code == "checkout_conflict"
    again = store.complete_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
        identity_id=member.item.identity_id, source_head="a" * 40,
        source_tree="b" * 40, internal_path=str(dest), operation_id=None,
    )
    assert again.public_dict() == payload
    assert store.get_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=OTHER_WORK,
    ) is None


def test_stale_revision_rejects_attach(store, tmp_path: Path) -> None:
    member = store.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE, display_name="Atlas",
        idempotency_key="m",
    )
    dest = tmp_path / "checkout"
    dest.mkdir()
    store.complete_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
        identity_id=member.item.identity_id, source_head="a" * 40,
        source_tree="b" * 40, internal_path=str(dest), operation_id=None,
    )
    with pytest.raises(store_module.WorkspaceExecutionError) as stale:
        store.begin_attach(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
            expected_revision=9, session_name="s",
        )
    assert stale.value.code == "stale_revision"
    view, attachment, checkout = store.begin_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
        expected_revision=1, session_name="s",
    )
    assert view.state == "attaching"
    assert attachment.status == "attaching"
    assert checkout.internal_path == str(dest)
    with pytest.raises(store_module.WorkspaceExecutionError) as unverified:
        store.finish_attach(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
            expected_revision=view.revision, pane_id="hidden-pane",
            instance_id="hidden-instance", native_receipt="secret",
            identity_verified=False,
        )
    assert unverified.value.code == "runtime_identity_unverified"
    finished = store.finish_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
        expected_revision=view.revision, pane_id="hidden-pane",
        instance_id="hidden-instance", native_receipt="secret",
        identity_verified=True,
    )
    public = finished.public_dict()
    assert public["state"] == "connected_readonly"
    assert public["attachment"]["status"] == "connected_readonly"
    assert public["attachment"]["identity_verified"] is True
    assert "hidden-pane" not in str(public)
    assert "secret" not in str(public)


def _schema_sql(path: Path, table: str) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_schema_check_allows_detached_and_connected_must_be_verified(
    store, tmp_path: Path,
) -> None:
    db = tmp_path / "workspace-execution.sqlite3"
    ddl = _schema_sql(db, "work_item_preparations")
    assert "detached" in ddl
    assert "connected_readonly" in ddl
    member = store.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE, display_name="Atlas",
        idempotency_key="m2",
    )
    dest = tmp_path / "checkout-2"
    dest.mkdir()
    store.complete_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=OTHER_WORK,
        identity_id=member.item.identity_id, source_head="a" * 40,
        source_tree="b" * 40, internal_path=str(dest), operation_id=None,
    )
    attaching, _attachment, _checkout = store.begin_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=OTHER_WORK,
        expected_revision=1, session_name="s",
    )
    connected = store.finish_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=OTHER_WORK,
        expected_revision=attaching.revision, pane_id="hidden-pane",
        instance_id="hidden-instance", native_receipt="secret",
        identity_verified=True,
    )
    assert connected.state == "connected_readonly"
    assert connected.attachment is not None
    assert connected.attachment.identity_verified is True
    detaching, _live, _chk = store.begin_detach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=OTHER_WORK,
        expected_revision=connected.revision,
    )
    assert detaching.state == "detaching"
    detached = store.finish_detach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=OTHER_WORK,
        expected_revision=detaching.revision,
    )
    assert detached.state == "detached"
    assert detached.lease is not None
    assert detached.lease.status == "revoked"
    assert detached.checkout is not None
    assert detached.identity.identity_id == member.item.identity_id
    store.close()
    reopened = store_module.open_existing(db)
    loaded = reopened.get_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=OTHER_WORK,
    )
    assert loaded is not None
    assert loaded.state == "detached"
    reopened.close()
