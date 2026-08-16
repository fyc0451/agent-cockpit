from __future__ import annotations

from pathlib import Path

import pytest

from agent_cockpit import local_codex_harness as harness_mod
from agent_cockpit import workspace_execution_store as store_module
from agent_cockpit import workspace_write_gate as gate_mod


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
WORK = "wrk_" + "c" * 32
FENCE = "sha256:" + "ab" * 32


def _connected(tmp_path: Path):
    store = store_module.initialize(tmp_path / "workspace-execution.sqlite3")
    member = store.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE, display_name="Atlas",
        idempotency_key="m",
    ).item
    dest = tmp_path / "checkout"
    dest.mkdir()
    prepared = store.complete_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
        identity_id=member.identity_id, source_head="a" * 40,
        source_tree="b" * 40, internal_path=str(dest), operation_id=None,
    )
    attaching, attachment, _checkout = store.begin_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
        expected_revision=prepared.revision, session_name="s",
    )
    connected = store.finish_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
        expected_revision=attaching.revision, pane_id="pane-1",
        instance_id="inst-1", native_receipt="secret", identity_verified=True,
    )
    return store, member, dest, connected, attachment.attachment_id


def test_reserved_lease_stays_fail_closed(tmp_path: Path) -> None:
    store, member, dest, connected, attachment_id = _connected(tmp_path)
    issued = harness_mod.LocalCodexHarness(
        capability_root=tmp_path / "caps",
    ).issue_capability(
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, fence=FENCE, session="s", pane_id="pane-1",
    )
    with pytest.raises(gate_mod.WriteGateError) as error:
        gate_mod.WorkspaceWriteGate().authorize(
            attachment_id=attachment_id, identity_id=member.identity_id,
            generation=1, fence=FENCE,
            capability_path=issued["capability_path"], checkout_path=dest,
            execution=store, project_id=PROJECT, workspace_id=WORKSPACE,
            work_item_id=WORK, expected_lease_revision=connected.lease.revision,
        )
    assert error.value.code == "lease_not_active"
    store.close()


def test_active_lease_authorizes_matching_capability(tmp_path: Path) -> None:
    store, member, dest, connected, attachment_id = _connected(tmp_path)
    claim_id = "clm_" + "1" * 32
    active = store.activate_claim_lease(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=WORK,
        expected_preparation_revision=connected.revision,
        expected_lease_revision=connected.lease.revision,
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, claim_id=claim_id, idempotency_key="act",
    )
    issued = harness_mod.LocalCodexHarness(
        capability_root=tmp_path / "caps",
    ).issue_capability(
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, fence=FENCE, session="s", pane_id="pane-1",
    )
    gate_mod.WorkspaceWriteGate().authorize(
        attachment_id=attachment_id, identity_id=member.identity_id,
        generation=1, fence=FENCE, capability_path=issued["capability_path"],
        checkout_path=dest, execution=store, project_id=PROJECT,
        workspace_id=WORKSPACE, work_item_id=WORK,
        expected_lease_revision=active["lease"]["revision"], claim_id=claim_id,
    )
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(gate_mod.WriteGateError) as untrusted:
        gate_mod.WorkspaceWriteGate().authorize(
            attachment_id=attachment_id, identity_id=member.identity_id,
            generation=1, fence=FENCE, capability_path=issued["capability_path"],
            checkout_path=other, execution=store, project_id=PROJECT,
            workspace_id=WORKSPACE, work_item_id=WORK,
            expected_lease_revision=active["lease"]["revision"], claim_id=claim_id,
        )
    assert untrusted.value.code == "checkout_untrusted"
    with pytest.raises(gate_mod.WriteGateError) as fence:
        gate_mod.WorkspaceWriteGate().authorize(
            attachment_id=attachment_id, identity_id=member.identity_id,
            generation=1, fence="sha256:" + "00" * 32,
            capability_path=issued["capability_path"], checkout_path=dest,
            execution=store, project_id=PROJECT, workspace_id=WORKSPACE,
            work_item_id=WORK,
            expected_lease_revision=active["lease"]["revision"], claim_id=claim_id,
        )
    assert fence.value.code == "fence_rejected"
    store.close()
