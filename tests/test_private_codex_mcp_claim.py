from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_cockpit import local_codex_harness
from agent_cockpit import private_codex_mcp
from agent_cockpit import workspace_claim_tools
from agent_cockpit import workspace_execution_store
from agent_cockpit import workspace_work_store


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
SENTINEL = "BOSS-CLAIM-SENTINEL-71a9"


class _Activator:
    def __init__(self, work: workspace_work_store.WorkspaceWorkStore) -> None:
        self.work = work

    def activate(self, context, pending_claim, *, idempotency_key):
        claim = pending_claim["claim"]
        return self.work.activate_claim(
            project_id=context.project_id, workspace_id=context.workspace_id,
            work_item_id=context.work_item_id, claim_id=claim["claim_id"],
            identity_id=context.identity_id, generation=context.generation,
            expected_claim_revision=claim["revision"],
            expected_work_revision=context.work_revision,
            idempotency_key=idempotency_key,
        )


class _PendingActivator:
    def activate(self, _context, pending_claim, *, idempotency_key):
        return pending_claim


class _WriteTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path, dict[str, object]]] = []

    def apply_patch(self, path: Path, arguments: dict[str, object]):
        self.calls.append(("apply_patch", path, arguments))
        return {"outcome": "applied"}

    def reply_complete(self, path: Path, arguments: dict[str, object]):
        self.calls.append(("reply_complete", path, arguments))
        return {"outcome": "completed"}


def _world(tmp_path: Path, *, pending: bool = False):
    tmp_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    work = workspace_work_store.initialize(tmp_path / "workspace-work.sqlite3")
    created = work.create_work_item(
        project_id=PROJECT, workspace_id=WORKSPACE, body=SENTINEL,
        acceptance="complete", constraints=None, idempotency_key="create",
    )
    work_id = created.item.work_item["work_item_id"]
    execution = workspace_execution_store.initialize(
        tmp_path / "workspace-execution.sqlite3"
    )
    identity = execution.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE,
        display_name="Atlas", idempotency_key="member",
    ).item
    execution.claim_prepare(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=identity.identity_id,
    )
    prepared = execution.complete_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=identity.identity_id, source_head="1" * 40,
        source_tree="2" * 40, internal_path=str(tmp_path / "checkout"),
        operation_id=None,
    )
    attaching, attachment, _checkout = execution.begin_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        expected_revision=prepared.revision, session_name="session-fixed",
    )
    execution.finish_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        expected_revision=attaching.revision, pane_id="pane-fixed",
        instance_id="i-fixed", native_receipt="sha256:" + "d" * 64,
        identity_verified=True,
    )
    with sqlite3.connect(execution.path) as connection:
        fence = connection.execute(
            "SELECT fence_digest FROM writer_leases"
        ).fetchone()[0]
    harness = local_codex_harness.LocalCodexHarness(
        capability_root=tmp_path / "caps"
    )
    cap = harness.issue_capability(
        attachment_id=attachment.attachment_id,
        identity_id=identity.identity_id, generation=1, fence=fence,
        session="session-fixed", pane_id="pane-fixed",
    )["capability_path"]
    tools = workspace_claim_tools.WorkspaceClaimTools(
        work=work, execution=execution,
        activator=_PendingActivator() if pending else _Activator(work),
    )
    return work, work_id, cap, tools


def test_claim_uses_capability_authority_and_body_only_after_active(
    tmp_path: Path,
) -> None:
    work, work_id, cap, tools = _world(tmp_path)
    listed = private_codex_mcp.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        claim_tools=tools,
    )
    assert [item["name"] for item in listed["result"]["tools"]] == [
        "claim_current"
    ]
    result = private_codex_mcp.dispatch({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "claim_current", "arguments": {}},
    }, capability_file=cap, claim_tools=tools)
    assert result["result"]["isError"] is False
    data = result["result"]["structuredContent"]
    assert data["claim"]["state"] == "active"
    assert data["root_message"]["body"] == SENTINEL
    replay = private_codex_mcp.dispatch({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "claim_current", "arguments": {}},
    }, capability_file=cap, claim_tools=tools)
    assert replay["result"]["structuredContent"] == data
    detail = work.get_work_item_detail(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
    )
    assert detail is not None and detail["work_item"]["status"] == "working"


def test_tampered_fence_fails_closed_without_boss_body(tmp_path: Path) -> None:
    work, work_id, cap, tools = _world(tmp_path)
    record = json.loads(cap.read_text())
    record["fence"] = "sha256:" + "0" * 64
    cap.write_text(json.dumps(record))
    cap.chmod(0o600)
    result = private_codex_mcp.dispatch({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "claim_current", "arguments": {}},
    }, capability_file=cap, claim_tools=tools)
    assert result["result"]["isError"] is True
    dumped = json.dumps(result)
    assert "runtime_capability_invalid" in dumped
    assert SENTINEL not in dumped
    detail = work.get_work_item_detail(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
    )
    assert detail is not None
    assert detail["work_item"]["status"] == "unassigned"
    assert detail["claim"] is None


def test_pending_gate_and_concurrent_claims_do_not_leak_early_body(
    tmp_path: Path,
) -> None:
    work, work_id, cap, pending_tools = _world(tmp_path / "pending", pending=True)
    pending = private_codex_mcp.dispatch({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "claim_current", "arguments": {}},
    }, capability_file=cap, claim_tools=pending_tools)
    assert pending["result"]["isError"] is True
    assert pending["result"]["structuredContent"]["code"] == "claim_not_active"
    assert SENTINEL not in json.dumps(pending)
    detail = work.get_work_item_detail(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
    )
    assert detail is not None
    assert detail["work_item"]["status"] == "unassigned"
    assert detail["claim"]["state"] == "pending_gate"

    _work, _work_id, active_cap, active_tools = _world(tmp_path / "active")

    def claim(index: int):
        return private_codex_mcp.dispatch({
            "jsonrpc": "2.0", "id": index, "method": "tools/call",
            "params": {"name": "claim_current", "arguments": {}},
        }, capability_file=active_cap, claim_tools=active_tools)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [future.result() for future in [
            pool.submit(claim, index) for index in range(8)
        ]]
    assert all(item["result"]["isError"] is False for item in results)
    claim_ids = {
        item["result"]["structuredContent"]["claim"]["claim_id"]
        for item in results
    }
    assert len(claim_ids) == 1


def test_explicit_router_lists_only_installed_tools(tmp_path: Path) -> None:
    _work, _work_id, cap, claim_tools = _world(tmp_path)
    write_tools = _WriteTools()
    listed = private_codex_mcp.dispatch(
        {"jsonrpc": "2.0", "id": 20, "method": "tools/list"},
        claim_tools=claim_tools, write_tools=write_tools,
    )
    assert [item["name"] for item in listed["result"]["tools"]] == [
        "claim_current", "apply_patch", "reply_complete",
    ]
    applied = private_codex_mcp.dispatch({
        "jsonrpc": "2.0", "id": 21, "method": "tools/call",
        "params": {
            "name": "apply_patch",
            "arguments": {"claim_revision": 1, "lease_revision": 1,
                          "patch": "diff"},
        },
    }, capability_file=cap, claim_tools=claim_tools, write_tools=write_tools)
    assert applied["result"]["structuredContent"] == {"outcome": "applied"}
    assert write_tools.calls == [("apply_patch", cap, {
        "claim_revision": 1, "lease_revision": 1, "patch": "diff",
    })]
