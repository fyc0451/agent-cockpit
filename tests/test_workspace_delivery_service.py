from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_cockpit import local_codex_harness as harness_mod
from agent_cockpit import operation_store as operation_mod
from agent_cockpit import workspace_claim_activation as activate_mod
from agent_cockpit import workspace_delivery_service as service_mod
from agent_cockpit import workspace_delivery_store as delivery_mod
from agent_cockpit import workspace_execution_store as execution_mod
from agent_cockpit import workspace_work_store as work_mod
from agent_cockpit import workspace_write_tools as write_mod


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
AUTHOR = "idn_" + "1" * 32
REVIEWER = "idn_" + "2" * 32
FENCE = "sha256:" + "ab" * 32
PATCH = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-hello
+world
"""


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return result.stdout.strip()


def _world(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    (source / "README.md").write_text("hello\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    tree = _git(source, "rev-parse", "HEAD^{tree}")
    checkout = tmp_path / "checkout"
    _git(source, "worktree", "add", "--detach", str(checkout), base)

    execution = execution_mod.initialize(tmp_path / "workspace-execution.sqlite3")
    work = work_mod.initialize(tmp_path / "workspace-work.sqlite3")
    operations = operation_mod.initialize(tmp_path / "operation.sqlite3")
    member = execution.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE, display_name="Author",
        idempotency_key="member",
    ).item
    created = work.create_work_item(
        project_id=PROJECT, workspace_id=WORKSPACE, body="change README",
        acceptance="README says world", constraints="local",
        allowed_paths=["README.md"], idempotency_key="create",
    )
    work_id = created.item.work_item["work_item_id"]
    prepared = execution.complete_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=member.identity_id, source_head=base, source_tree=tree,
        internal_path=str(checkout), operation_id=None,
    )
    attaching, attachment, _ = execution.begin_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        expected_revision=prepared.revision, session_name="session",
    )
    connected = execution.finish_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        expected_revision=attaching.revision, pane_id="pane",
        instance_id="instance", native_receipt="receipt", identity_verified=True,
    )
    reserved = work.reserve_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=member.identity_id, generation=1, expected_revision=1,
        idempotency_key="reserve",
    )
    activated = activate_mod.ClaimActivator(
        execution=execution, work=work, operations=operations,
    ).activate(
        {
            "project_id": PROJECT, "workspace_id": WORKSPACE,
            "work_item_id": work_id,
            "expected_preparation_revision": connected.revision,
            "expected_lease_revision": connected.lease.revision,
            "expected_work_revision": 1,
            "attachment_id": attachment.attachment_id,
            "identity_id": member.identity_id, "generation": 1,
        },
        reserved, idempotency_key="activate",
    )
    capability = harness_mod.LocalCodexHarness(
        capability_root=tmp_path / "caps",
    ).issue_capability(
        attachment_id=attachment.attachment_id,
        identity_id=member.identity_id, generation=1, fence=FENCE,
        session="session", pane_id="pane",
    )
    delivery = delivery_mod.WorkspaceDeliveryStore.from_work_store(work)
    service = service_mod.WorkspaceDeliveryService(
        execution=execution, delivery=delivery,
        source_path_provider=lambda _project, _workspace: source,
    )
    write_tools = write_mod.WorkspaceWriteTools(
        execution=execution, work=work, operations=operations,
        delivery_service=service,
    )
    return {
        "source": source, "checkout": checkout, "base": base,
        "execution": execution, "work": work, "operations": operations,
        "member": member, "attachment": attachment, "activated": activated,
        "capability": capability, "write_tools": write_tools,
        "delivery": delivery, "service": service, "work_id": work_id,
    }


def test_managed_codex_handoff_review_apply_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(tmp_path)
    patched = world["write_tools"].apply_patch(
        capability_path=world["capability"]["capability_path"],
        claim_revision=world["activated"]["claim"]["revision"],
        lease_revision=world["activated"]["lease"]["revision"],
        patch=PATCH, idempotency_key="patch",
    )
    original_publish = delivery_mod.WorkspaceDeliveryStore.publish_handoff
    publish_attempts = 0

    def fail_first_publish(self, **kwargs):
        nonlocal publish_attempts
        publish_attempts += 1
        if publish_attempts == 1:
            raise delivery_mod.WorkspaceDeliveryError("store_write_failed")
        return original_publish(self, **kwargs)

    monkeypatch.setattr(
        delivery_mod.WorkspaceDeliveryStore, "publish_handoff",
        fail_first_publish,
    )
    with pytest.raises(write_mod.WriteToolError) as publish_failed:
        world["write_tools"].submit_handoff(
            capability_path=world["capability"]["capability_path"],
            claim_revision=world["activated"]["claim"]["revision"],
            lease_revision=patched["lease"]["revision"],
            summary="README updated", test_evidence={"tests": ["read"]},
            idempotency_key="handoff",
        )
    assert publish_failed.value.code == "store_write_failed"
    assert _git(world["checkout"], "rev-parse", "HEAD") == world["base"]
    published = world["write_tools"].submit_handoff(
        capability_path=world["capability"]["capability_path"],
        claim_revision=world["activated"]["claim"]["revision"],
        lease_revision=patched["lease"]["revision"],
        summary="README updated", test_evidence={"tests": ["read"]},
        idempotency_key="handoff",
    )
    assert published["handoff"]["base_sha"] == world["base"]
    assert published["handoff"]["head_sha"] != world["base"]
    assert published["handoff"]["changed_paths"] == ["README.md"]
    assert published["lease"]["status"] == "revoked"
    replay = world["write_tools"].submit_handoff(
        capability_path=world["capability"]["capability_path"],
        claim_revision=world["activated"]["claim"]["revision"],
        lease_revision=patched["lease"]["revision"],
        summary="README updated", test_evidence={"tests": ["read"]},
        idempotency_key="handoff",
    )
    assert replay["handoff"] == published["handoff"]
    with pytest.raises(write_mod.WriteToolError) as replay_conflict:
        world["write_tools"].submit_handoff(
            capability_path=world["capability"]["capability_path"],
            claim_revision=world["activated"]["claim"]["revision"],
            lease_revision=patched["lease"]["revision"],
            summary="different summary", test_evidence={"tests": ["read"]},
            idempotency_key="handoff",
        )
    assert replay_conflict.value.code == "idempotency_conflict"
    with pytest.raises(write_mod.WriteToolError) as revision_conflict:
        world["write_tools"].submit_handoff(
            capability_path=world["capability"]["capability_path"],
            claim_revision=world["activated"]["claim"]["revision"],
            lease_revision=patched["lease"]["revision"] + 1,
            summary="README updated", test_evidence={"tests": ["read"]},
            idempotency_key="handoff",
        )
    assert revision_conflict.value.code == "idempotency_conflict"
    assert (world["source"] / "README.md").read_text(encoding="utf-8") == "hello\n"

    reviewed = world["delivery"].review_handoff(
        project_id=PROJECT, workspace_id=WORKSPACE,
        work_item_id=world["work_id"],
        handoff_id=published["handoff"]["handoff_id"],
        reviewer_identity_id=REVIEWER, reviewer_generation=1,
        expected_handoff_revision=published["handoff"]["revision"],
        expected_delivery_revision=published["delivery_revision"],
        head_sha=published["handoff"]["head_sha"],
        diff_digest=published["handoff"]["diff_digest"], decision="accept",
        summary="reviewed exact packet", test_evidence={"approved": True},
        idempotency_key="review",
    )
    original_record_apply = delivery_mod.WorkspaceDeliveryStore.record_apply
    attempts = 0

    def fail_first_record(self, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise delivery_mod.WorkspaceDeliveryError("store_write_failed")
        return original_record_apply(self, **kwargs)

    monkeypatch.setattr(
        delivery_mod.WorkspaceDeliveryStore, "record_apply", fail_first_record,
    )
    with pytest.raises(service_mod.WorkspaceDeliveryServiceError) as unknown:
        world["service"].apply(
            project_id=PROJECT, workspace_id=WORKSPACE,
            work_item_id=world["work_id"],
            expected_delivery_revision=reviewed["delivery_revision"],
            idempotency_key="apply",
        )
    assert unknown.value.code == "apply_outcome_unknown"
    assert (world["source"] / "README.md").read_text(encoding="utf-8") == "world\n"
    applied = world["service"].apply(
        project_id=PROJECT, workspace_id=WORKSPACE,
        work_item_id=world["work_id"],
        expected_delivery_revision=reviewed["delivery_revision"],
        idempotency_key="apply",
    )
    assert applied["outcome"] == "succeeded"
    assert world["service"].apply(
        project_id=PROJECT, workspace_id=WORKSPACE,
        work_item_id=world["work_id"],
        expected_delivery_revision=reviewed["delivery_revision"],
        idempotency_key="apply",
    ) == applied
    assert (world["source"] / "README.md").read_text(encoding="utf-8") == "world\n"
    assert _git(world["source"], "status", "--porcelain") == ""
    packet = world["delivery"].get_packet(
        project_id=PROJECT, workspace_id=WORKSPACE,
        work_item_id=world["work_id"],
    )
    assert packet is not None
    assert packet["delivery_status"] == "completed"
    assert packet["apply"]["source_before_sha"] == world["base"]
    world["execution"].close()
    world["work"].close()
    world["operations"].close()
