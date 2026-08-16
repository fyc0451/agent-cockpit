from __future__ import annotations

import json
from pathlib import Path

from agent_cockpit import local_codex_harness as harness_mod
from agent_cockpit import private_codex_mcp as mcp


ATTACHMENT = "att_" + "c" * 32
IDENTITY = "idn_" + "d" * 32
FENCE = "sha256:" + "ab" * 32


def _cap(tmp_path: Path, *, generation: int = 1) -> Path:
    harness = harness_mod.LocalCodexHarness(capability_root=tmp_path / "caps")
    issued = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=generation,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    return issued["capability_path"]


def test_tools_only_return_claim_not_available_without_side_effects(
    tmp_path: Path, monkeypatch,
) -> None:
    cap = _cap(tmp_path)
    monkeypatch.setenv("COCKPIT_CAPABILITY_FILE", str(cap))
    target = tmp_path / "checkout" / "written.txt"
    target.parent.mkdir()
    listed = mcp.dispatch({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
    })
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert names == {
        "claim_current", "apply_patch", "run", "reply_complete", "fail",
    }
    for index, name in enumerate(names):
        reply = mcp.dispatch({
            "jsonrpc": "2.0", "id": index + 2, "method": "tools/call",
            "params": {
                "name": name,
                "arguments": {"path": str(target), "body": "should-not-write"},
            },
        })
        assert reply["result"]["isError"] is True
        assert reply["result"]["structuredContent"]["code"] == "claim_not_available"
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_old_capability_generation_is_rejected_without_writes(
    tmp_path: Path,
) -> None:
    first = _cap(tmp_path, generation=1)
    stale = first.parent / "stale.cap"
    stale.write_bytes(first.read_bytes())
    stale.chmod(0o600)
    _cap(tmp_path, generation=3)
    reply = mcp.dispatch({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "claim_current", "arguments": {}},
    }, capability_file=stale)
    assert reply["result"]["structuredContent"]["code"] == "claim_not_available"
    missing = mcp.dispatch({
        "jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "apply_patch", "arguments": {"patch": "--- a\n+++ b\n"}},
    }, capability_file=tmp_path / "gone.cap")
    assert missing["result"]["structuredContent"]["code"] == "claim_not_available"
