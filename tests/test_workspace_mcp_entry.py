"""C3 wiring: workspace_mcp_entry composition root tests."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cockpit import local_codex_harness as harness_mod
from agent_cockpit import operation_store as operation_mod
from agent_cockpit import runtime_paths
from agent_cockpit import workspace_execution_store as execution_mod
from agent_cockpit import workspace_work_store as work_mod
from agent_cockpit import workspace_mcp_entry as entry_mod
from agent_cockpit import workspace_write_tools as write_mod


ROOT = Path(__file__).resolve().parents[1]
PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
SENTINEL = "BOSS-ENTRY-SENTINEL-53c0"
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


def _world(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    work_path = tmp_path / "workspace-work.sqlite3"
    execution_path = tmp_path / "workspace-execution.sqlite3"
    operation_path = tmp_path / "operation-journal.sqlite3"
    work = work_mod.initialize(work_path)
    created = work.create_work_item(
        project_id=PROJECT, workspace_id=WORKSPACE, body=SENTINEL,
        acceptance="complete", constraints=None, idempotency_key="create",
    )
    work_id = created.item.work_item["work_item_id"]
    execution = execution_mod.initialize(execution_path)
    operation_mod.initialize(operation_path).close()
    identity = execution.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE,
        display_name="Atlas", idempotency_key="member",
    ).item
    execution.claim_prepare(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=identity.identity_id,
    )
    checkout = _repo(tmp_path / "checkout")
    source = _repo(tmp_path / "source")
    source_bytes = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*") if path.is_file()
    }
    prepared = execution.complete_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=identity.identity_id, source_head="1" * 40,
        source_tree="2" * 40, internal_path=str(checkout), operation_id=None,
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
    harness = harness_mod.LocalCodexHarness(capability_root=tmp_path / "caps")
    cap = harness.issue_capability(
        attachment_id=attachment.attachment_id,
        identity_id=identity.identity_id, generation=1, fence=fence,
        session="session-fixed", pane_id="pane-fixed",
    )["capability_path"]
    work.close()
    execution.close()
    return {
        "work_path": work_path,
        "execution_path": execution_path,
        "operation_path": operation_path,
        "cap": cap,
        "work_id": work_id,
        "checkout": checkout,
        "source": source,
        "source_bytes": source_bytes,
    }


def _build(world: dict[str, object]) -> entry_mod.McpTools:
    return entry_mod.build_tools(
        work_path=world["work_path"],
        execution_path=world["execution_path"],
        operation_path=world["operation_path"],
    )


def test_claim_adapter_maps_claim_context_and_returns_body_only_after_active(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    tools = _build(world)
    try:
        result = tools.claim_tools.claim_current(world["cap"])
        assert result["claim"]["state"] == "active"
        assert result["work_item"]["status"] == "working"
        assert result["root_message"]["body"] == SENTINEL
        execution = execution_mod.open_existing(world["execution_path"])
        try:
            prep = execution.get_preparation(
                project_id=PROJECT, workspace_id=WORKSPACE,
                work_item_id=world["work_id"],
            )
            assert prep is not None
            assert prep.lease is not None
            assert prep.lease.status == "active"
            assert prep.lease.claim_id == result["claim"]["claim_id"]
        finally:
            execution.close()
        # 重复 claim：幂等返回同一 claim，不产生第二个
        again = tools.claim_tools.claim_current(world["cap"])
        assert again["claim"]["claim_id"] == result["claim"]["claim_id"]
    finally:
        tools.close()


def test_write_adapter_kwargs_and_derived_idempotency_key(tmp_path: Path) -> None:
    world = _world(tmp_path)
    tools = _build(world)
    recorded: list[dict[str, object]] = []

    class _Recorder:
        def apply_patch(self, **kwargs: object) -> dict[str, object]:
            recorded.append(kwargs)
            return {"outcome": "applied"}

        def reply_complete(self, **kwargs: object) -> dict[str, object]:
            recorded.append(kwargs)
            return {"outcome": "completed"}

    adapter = entry_mod._WriteToolsAdapter(_Recorder())
    try:
        args = {"claim_revision": 2, "lease_revision": 2, "patch": PATCH}
        adapter.apply_patch(world["cap"], args)
        adapter.apply_patch(world["cap"], dict(args))
        assert recorded[0]["capability_path"] == Path(world["cap"])
        assert recorded[0]["claim_revision"] == 2
        assert recorded[0]["lease_revision"] == 2
        assert recorded[0]["patch"] == PATCH
        # 相同意图 → 同一派生 key；意图变化 → 换 key
        assert recorded[0]["idempotency_key"] == recorded[1]["idempotency_key"]
        adapter.apply_patch(world["cap"], {**args, "patch": PATCH + "\n"})
        assert recorded[2]["idempotency_key"] != recorded[0]["idempotency_key"]
        key = recorded[0]["idempotency_key"]
        assert isinstance(key, str) and 1 <= len(key) <= 128
        assert all(33 <= ord(char) < 127 for char in key)
        adapter.reply_complete(
            world["cap"],
            {"claim_revision": 2, "lease_revision": 2, "body": "done"},
        )
        assert recorded[3]["body"] == "done"
        # 缺键/多键/非 dict 全部 fail-closed
        for bad in (
            {},
            {"claim_revision": 2, "lease_revision": 2},
            {**args, "extra": 1},
            "not-a-dict",
        ):
            with pytest.raises(entry_mod.McpEntryError) as caught:
                adapter.apply_patch(world["cap"], bad)
            assert caught.value.code == "invalid_argument"
    finally:
        tools.close()


def test_apply_patch_and_reply_end_to_end_via_adapter(tmp_path: Path) -> None:
    world = _world(tmp_path)
    tools = _build(world)
    try:
        claimed = tools.claim_tools.claim_current(world["cap"])
        claim_revision = claimed["claim"]["revision"]
        lease_revision = claimed["lease"]["revision"]
        applied = tools.write_tools.apply_patch(
            world["cap"],
            {
                "claim_revision": claim_revision,
                "lease_revision": lease_revision,
                "patch": PATCH,
            },
        )
        assert applied["applied"] is True
        assert (world["checkout"] / "README").read_text(encoding="utf-8") == "world\n"
        after = {
            path.relative_to(world["source"]): path.read_bytes()
            for path in world["source"].rglob("*") if path.is_file()
        }
        assert after == world["source_bytes"]
        replied = tools.write_tools.reply_complete(
            world["cap"],
            {
                "claim_revision": claim_revision,
                "lease_revision": applied["lease"]["revision"],
                "body": "done locally",
            },
        )
        assert replied["lease"]["status"] == "revoked"
        assert replied["work_item"]["status"] == "completed"
        # 同参重放：同一 reply，无第二条消息
        replay = tools.write_tools.reply_complete(
            world["cap"],
            {
                "claim_revision": claim_revision,
                "lease_revision": applied["lease"]["revision"],
                "body": "done locally",
            },
        )
        assert (
            replay["reply_message"]["message_id"]
            == replied["reply_message"]["message_id"]
        )
    finally:
        tools.close()


def test_serve_lists_exactly_three_tools_and_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(tmp_path)
    tools = _build(world)
    monkeypatch.setenv("COCKPIT_CAPABILITY_FILE", str(world["cap"]))
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "claim_current", "arguments": {}},
        },
        {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "run", "arguments": {}},
        },
    ]
    stdin = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
    stdout = io.StringIO()
    try:
        assert entry_mod.serve(tools, stdin=stdin, stdout=stdout) == 0
    finally:
        tools.close()
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    by_id = {reply["id"]: reply for reply in replies}
    names = [tool["name"] for tool in by_id[2]["result"]["tools"]]
    assert sorted(names) == ["apply_patch", "claim_current", "reply_complete"]
    claimed = by_id[3]["result"]["structuredContent"]
    assert claimed["root_message"]["body"] == SENTINEL
    assert by_id[4]["result"]["isError"] is True
    assert by_id[4]["result"]["structuredContent"]["code"] == "invalid_argument"


def test_main_honors_non_default_roots_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(tmp_path / "world")
    names = {
        "workspace_work": world["work_path"],
        "workspace_execution": world["execution_path"],
        "operation_journal": world["operation_path"],
    }
    monkeypatch.setattr(
        runtime_paths, "validate_store", lambda name: names[name],
    )
    monkeypatch.setenv("COCKPIT_CAPABILITY_FILE", str(world["cap"]))
    monkeypatch.setattr(entry_mod.sys, "stdin", io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/call",'
        '"params":{"name":"claim_current","arguments":{}}}\n'
    ))
    stdout = io.StringIO()
    monkeypatch.setattr(entry_mod.sys, "stdout", stdout)
    assert entry_mod.main() == 0
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    tools = replies[0]["result"]["tools"]
    assert sorted(tool["name"] for tool in tools) == [
        "apply_patch", "claim_current", "reply_complete",
    ]
    assert replies[1]["result"]["structuredContent"]["root_message"]["body"] == SENTINEL

    # 库不存在 → fail-closed deny-all，不创建新库、不假装可用；
    # 故障态 tools/list 仍精确三项、无 run/fail
    missing = tmp_path / "empty"
    missing.mkdir()
    monkeypatch.setattr(
        runtime_paths, "validate_store",
        lambda name: missing / f"{name}.sqlite3",
    )
    monkeypatch.setattr(entry_mod.sys, "stdin", io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/call",'
        '"params":{"name":"claim_current","arguments":{}}}\n'
    ))
    stdout2 = io.StringIO()
    monkeypatch.setattr(entry_mod.sys, "stdout", stdout2)
    assert entry_mod.main() == 0
    replies2 = [json.loads(line) for line in stdout2.getvalue().splitlines()]
    names2 = [tool["name"] for tool in replies2[0]["result"]["tools"]]
    assert sorted(names2) == ["apply_patch", "claim_current", "reply_complete"]
    denied = replies2[1]
    assert denied["result"]["isError"] is True
    assert denied["result"]["structuredContent"]["code"] == (
        "workspace_work_schema_missing"
    )
    assert not (missing / "workspace_work.sqlite3").exists()


def test_main_empty_roots_subprocess_fail_closed(tmp_path: Path) -> None:
    """非默认空 roots 真实子进程：零建库、tools/list 精确三项、调用全拒。"""
    roots = tmp_path / "roots"
    for name in ("data", "config", "state", "uploads"):
        (roots / name).mkdir(parents=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["COCKPIT_DATA_DIR"] = str(roots / "data")
    env["COCKPIT_CONFIG_DIR"] = str(roots / "config")
    env["COCKPIT_STATE_DIR"] = str(roots / "state")
    env["COCKPIT_UPLOADS_DIR"] = str(roots / "uploads")
    env.pop("COCKPIT_CAPABILITY_FILE", None)
    env.pop("COCKPIT_COORDINATION_DB", None)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    for ident, tool in (
        (3, "claim_current"), (4, "apply_patch"), (5, "reply_complete"),
    ):
        lines.append({
            "jsonrpc": "2.0", "id": ident, "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
        })
    payload = "".join(json.dumps(line) + "\n" for line in lines)
    result = subprocess.run(
        [sys.executable, "-m", "agent_cockpit.workspace_mcp_entry"],
        input=payload, env=env, cwd=ROOT, text=True, capture_output=True,
        check=True,
    )
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    by_id = {reply["id"]: reply for reply in replies}
    names = [tool["name"] for tool in by_id[2]["result"]["tools"]]
    assert sorted(names) == ["apply_patch", "claim_current", "reply_complete"]
    assert "run" not in names and "fail" not in names
    for ident in (3, 4, 5):
        denied = by_id[ident]["result"]
        assert denied["isError"] is True
        assert denied["structuredContent"]["code"] == (
            "runtime_capability_invalid"
        )
    assert not any(roots.rglob("*.sqlite3"))
    assert not any(path.is_file() for path in roots.rglob("*"))
