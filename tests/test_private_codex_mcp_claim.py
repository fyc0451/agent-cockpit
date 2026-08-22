from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_cockpit import local_codex_harness
from agent_cockpit import operation_store
from agent_cockpit import private_codex_mcp
from agent_cockpit import workspace_claim_activation
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


class _RealActivator:
    def __init__(self, value: workspace_claim_activation.ClaimActivator) -> None:
        self.value = value

    def activate(self, context, pending_claim, *, idempotency_key):
        return self.value.activate({
            "project_id": context.project_id,
            "workspace_id": context.workspace_id,
            "work_item_id": context.work_item_id,
            "expected_preparation_revision": context.preparation_revision,
            "expected_lease_revision": context.lease_revision,
            "expected_work_revision": context.work_revision,
            "attachment_id": context.attachment_id,
            "identity_id": context.identity_id,
            "generation": context.generation,
        }, pending_claim, idempotency_key=idempotency_key)


class _WriteTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path, dict[str, object]]] = []

    def apply_patch(self, path: Path, arguments: dict[str, object]):
        self.calls.append(("apply_patch", path, arguments))
        return {"outcome": "applied"}

    def reply_complete(self, path: Path, arguments: dict[str, object]):
        self.calls.append(("reply_complete", path, arguments))
        return {"outcome": "completed"}

    def submit_handoff(self, path: Path, arguments: dict[str, object]):
        self.calls.append(("submit_handoff", path, arguments))
        return {"outcome": "submitted"}


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


def _real_world(tmp_path: Path):
    work, work_id, cap, fake_tools = _world(tmp_path)
    operations = operation_store.initialize(tmp_path / "operation.sqlite3")
    tools = workspace_claim_tools.WorkspaceClaimTools(
        work=work,
        execution=fake_tools.execution,
        activator=_RealActivator(workspace_claim_activation.ClaimActivator(
            execution=fake_tools.execution, work=work, operations=operations,
        )),
    )
    return work, work_id, cap, tools, operations


def _snapshot(path: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(path) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        return tuple(
            (table, *row)
            for table in tables
            for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
        )


def _claim(tools, cap: Path) -> dict[str, object]:
    return tools.claim_current(cap)


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


def test_real_activator_replays_after_lease_is_active(tmp_path: Path) -> None:
    _work, _work_id, cap, tools, _operations = _real_world(tmp_path)
    first = _claim(tools, cap)
    assert first["lease"]["status"] == "active"
    assert _claim(tools, cap) == first


def test_real_activator_recovers_active_lease_pending_claim_window(
    tmp_path: Path,
) -> None:
    work, work_id, cap, tools, _operations = _real_world(tmp_path)
    record = json.loads(cap.read_text(encoding="utf-8"))
    digest = hashlib.sha256((record["token"] + work_id).encode()).hexdigest()
    pending = work.reserve_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=record["identity_id"], generation=record["generation"],
        expected_revision=1, idempotency_key="claim-reserve-" + digest,
    )
    preparation = tools.execution.get_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
    )
    assert preparation is not None and preparation.lease is not None
    tools.execution.activate_claim_lease(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        expected_preparation_revision=preparation.revision,
        expected_lease_revision=preparation.lease.revision,
        attachment_id=record["attachment_id"],
        identity_id=record["identity_id"], generation=record["generation"],
        claim_id=pending["claim"]["claim_id"],
        idempotency_key="claim-activate-" + digest + ":lease",
    )
    recovered = _claim(tools, cap)
    assert recovered["lease"]["status"] == "active"
    assert recovered["claim"]["claim_id"] == pending["claim"]["claim_id"]
    assert recovered["claim"]["state"] == "active"


def test_active_lease_rejects_wrong_claim_without_side_effects(
    tmp_path: Path,
) -> None:
    work, _work_id, cap, tools, operations = _real_world(tmp_path)
    _claim(tools, cap)
    with sqlite3.connect(tools.execution.path) as connection:
        connection.execute(
            "UPDATE writer_leases SET claim_id=?", ("clm_" + "9" * 32,)
        )
    before = (
        _snapshot(work.path), _snapshot(tools.execution.path),
        _snapshot(operations.path),
    )
    result = private_codex_mcp.dispatch({
        "jsonrpc": "2.0", "id": 30, "method": "tools/call",
        "params": {"name": "claim_current", "arguments": {}},
    }, capability_file=cap, claim_tools=tools)
    assert result["result"]["structuredContent"]["code"] == (
        "runtime_capability_invalid"
    )
    assert SENTINEL not in json.dumps(result)
    assert (
        _snapshot(work.path), _snapshot(tools.execution.path),
        _snapshot(operations.path),
    ) == before


def test_old_and_wrong_generation_capabilities_have_no_side_effects(
    tmp_path: Path,
) -> None:
    work, _work_id, cap, tools, operations = _real_world(tmp_path)
    original = cap.read_bytes()
    stale = cap.with_name("stale.cap")
    stale.write_bytes(original)
    stale.chmod(0o600)
    record = json.loads(original)
    cap.with_name(f'{record["attachment_id"]}.generation').write_text("2\n")
    before = (
        _snapshot(work.path), _snapshot(tools.execution.path),
        _snapshot(operations.path),
    )
    for candidate in (stale, cap):
        if candidate == cap:
            record["generation"] = 2
            cap.write_text(json.dumps(record), encoding="utf-8")
            cap.chmod(0o600)
        result = private_codex_mcp.dispatch({
            "jsonrpc": "2.0", "id": 31, "method": "tools/call",
            "params": {"name": "claim_current", "arguments": {}},
        }, capability_file=candidate, claim_tools=tools)
        assert result["result"]["isError"] is True
        assert SENTINEL not in json.dumps(result)
    assert (
        _snapshot(work.path), _snapshot(tools.execution.path),
        _snapshot(operations.path),
    ) == before


def test_non_replay_lease_states_remain_rejected(tmp_path: Path) -> None:
    for status in ("revoking", "uncertain", "revoked"):
        work, _work_id, cap, tools, operations = _real_world(tmp_path / status)
        _claim(tools, cap)
        with sqlite3.connect(tools.execution.path) as connection:
            connection.execute("UPDATE writer_leases SET status=?", (status,))
        before = (
            _snapshot(work.path), _snapshot(tools.execution.path),
            _snapshot(operations.path),
        )
        result = private_codex_mcp.dispatch({
            "jsonrpc": "2.0", "id": 32, "method": "tools/call",
            "params": {"name": "claim_current", "arguments": {}},
        }, capability_file=cap, claim_tools=tools)
        assert result["result"]["structuredContent"]["code"] == (
            "runtime_capability_invalid"
        )
        assert SENTINEL not in json.dumps(result)
        assert (
            _snapshot(work.path), _snapshot(tools.execution.path),
            _snapshot(operations.path),
        ) == before


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
        "claim_current", "apply_patch", "reply_complete", "submit_handoff",
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
