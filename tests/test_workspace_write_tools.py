from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_cockpit import local_codex_harness as harness_mod
from agent_cockpit import operation_store as operation_mod
from agent_cockpit import workspace_claim_activation as activate_mod
from agent_cockpit import workspace_execution_store as exec_mod
from agent_cockpit import workspace_work_store as work_mod
from agent_cockpit import workspace_write_tools as tools_mod


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
FENCE = "sha256:" + "ab" * 32
PATCH = """diff --git a/README b/README
--- a/README
+++ b/README
@@ -1 +1 @@
-hello
+world
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "t@t.test")
    _git(path, "config", "user.name", "t")
    (path / "README").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README")
    _git(path, "commit", "-m", "init")
    return path


def _world(tmp_path: Path):
    execution = exec_mod.initialize(tmp_path / "workspace-execution.sqlite3")
    work = work_mod.initialize(tmp_path / "workspace-work.sqlite3")
    operations = operation_mod.initialize(tmp_path / "operation.sqlite3")
    member = execution.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE, display_name="Atlas",
        idempotency_key="m",
    ).item
    created = work.create_work_item(
        project_id=PROJECT, workspace_id=WORKSPACE, body="SECRET-SENTINEL",
        acceptance="done", constraints="local", idempotency_key="create",
    )
    work_item_id = created.item.work_item["work_item_id"]
    dest = _repo(tmp_path / "checkout")
    source = _repo(tmp_path / "source")
    source_bytes = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*") if path.is_file()
    }
    prepared = execution.complete_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        identity_id=member.identity_id, source_head="a" * 40,
        source_tree="b" * 40, internal_path=str(dest), operation_id=None,
    )
    attaching, attachment, _checkout = execution.begin_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_revision=prepared.revision, session_name="s",
    )
    connected = execution.finish_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_revision=attaching.revision, pane_id="pane-1",
        instance_id="inst-1", native_receipt="secret", identity_verified=True,
    )
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
            "attachment_id": attachment.attachment_id,
            "identity_id": member.identity_id,
            "generation": 1,
        },
        reserved,
        idempotency_key="activate",
    )
    issued = harness_mod.LocalCodexHarness(
        capability_root=tmp_path / "caps",
    ).issue_capability(
        attachment_id=attachment.attachment_id, identity_id=member.identity_id,
        generation=1, fence=FENCE, session="s", pane_id="pane-1",
    )
    tools = tools_mod.WorkspaceWriteTools(
        execution=execution, work=work, operations=operations,
    )
    return {
        "execution": execution,
        "work": work,
        "operations": operations,
        "tools": tools,
        "issued": issued,
        "activated": activated,
        "dest": dest,
        "source": source,
        "source_bytes": source_bytes,
        "work_item_id": work_item_id,
        "attachment_id": attachment.attachment_id,
        "member": member,
    }


def test_apply_patch_writes_checkout_only(tmp_path: Path) -> None:
    world = _world(tmp_path)
    applied = world["tools"].apply_patch(
        capability_path=world["issued"]["capability_path"],
        claim_revision=world["activated"]["claim"]["revision"],
        lease_revision=world["activated"]["lease"]["revision"],
        patch=PATCH, idempotency_key="patch-1",
    )
    assert applied["applied"] is True
    assert applied["lease"]["status"] == "active"
    assert (world["dest"] / "README").read_text(encoding="utf-8") == "world\n"
    after = {
        path.relative_to(world["source"]): path.read_bytes()
        for path in world["source"].rglob("*") if path.is_file()
    }
    assert after == world["source_bytes"]
    with pytest.raises(tools_mod.WriteToolError) as absolute:
        world["tools"].apply_patch(
            capability_path=world["issued"]["capability_path"],
            claim_revision=world["activated"]["claim"]["revision"],
            lease_revision=applied["lease"]["revision"],
            patch="diff --git a/README b/README\n--- a/README\n+++ b/../../etc/passwd\n",
            idempotency_key="patch-abs",
        )
    assert absolute.value.code == "patch_invalid"
    with pytest.raises(tools_mod.WriteToolError) as rename:
        world["tools"].apply_patch(
            capability_path=world["issued"]["capability_path"],
            claim_revision=world["activated"]["claim"]["revision"],
            lease_revision=applied["lease"]["revision"],
            patch="diff --git a/README b/OTHER\nrename from README\nrename to OTHER\n",
            idempotency_key="patch-ren",
        )
    assert rename.value.code == "patch_invalid"
    world["execution"].close()
    world["work"].close()
    world["operations"].close()


def test_old_token_generation_and_reply_all_or_nothing(tmp_path: Path) -> None:
    world = _world(tmp_path)
    old_cap = harness_mod.LocalCodexHarness(
        capability_root=tmp_path / "old-caps",
    ).issue_capability(
        attachment_id=world["attachment_id"], identity_id=world["member"].identity_id,
        generation=2, fence=FENCE, session="s", pane_id="pane-1",
    )
    with pytest.raises(tools_mod.WriteToolError) as stale:
        world["tools"].apply_patch(
            capability_path=old_cap["capability_path"],
            claim_revision=world["activated"]["claim"]["revision"],
            lease_revision=world["activated"]["lease"]["revision"],
            patch=PATCH, idempotency_key="patch-old",
        )
    assert stale.value.code == "stale_generation"
    assert (world["dest"] / "README").read_text(encoding="utf-8") == "hello\n"
    replied = world["tools"].reply_complete(
        capability_path=world["issued"]["capability_path"],
        claim_revision=world["activated"]["claim"]["revision"],
        lease_revision=world["activated"]["lease"]["revision"],
        body="done locally", idempotency_key="reply-1",
    )
    assert replied["lease"]["status"] == "revoked"
    assert replied["claim"]["state"] == "closed"
    assert replied["work_item"]["status"] == "completed"
    assert replied["reply_message"]["body"] == "done locally"
    replay = world["tools"].reply_complete(
        capability_path=world["issued"]["capability_path"],
        claim_revision=world["activated"]["claim"]["revision"],
        lease_revision=world["activated"]["lease"]["revision"],
        body="done locally", idempotency_key="reply-1",
    )
    assert replay["reply_message"]["message_id"] == replied["reply_message"]["message_id"]
    detail = world["work"].get_work_item_detail(
        project_id=PROJECT, workspace_id=WORKSPACE,
        work_item_id=world["work_item_id"],
    )
    kinds = [row["kind"] for row in detail["receipts"]]
    assert "reply" in kinds and "complete" in kinds
    world["execution"].close()
    world["work"].close()
    world["operations"].close()
