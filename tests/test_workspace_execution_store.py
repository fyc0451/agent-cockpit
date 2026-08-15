from __future__ import annotations

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
    finished = store.finish_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
        expected_revision=view.revision, pane_id="hidden-pane",
        instance_id="hidden-instance", native_receipt="secret",
    )
    public = finished.public_dict()
    assert public["state"] == "connected_readonly"
    assert public["attachment"]["status"] == "connected_readonly"
    assert public["attachment"]["identity_verified"] is False
    assert "hidden-pane" not in str(public)
    assert "secret" not in str(public)
