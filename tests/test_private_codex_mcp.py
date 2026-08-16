from __future__ import annotations

import json
from pathlib import Path

from agent_cockpit import local_codex_harness as harness_mod
from agent_cockpit import private_codex_mcp as mcp


ATTACHMENT = "att_" + "c" * 32
IDENTITY = "idn_" + "d" * 32
FENCE = "sha256:" + "ab" * 32
SENTINEL = "BOSS-CONTRACT-SENTINEL-9df1"
EXPECTED_TOOLS = [
    {
        "name": "claim_current",
        "description": (
            "Call first after COCKPIT_WAKEUP_V1 with {}. Claims the dispatched work "
            "and returns root_message.body, claim.revision, and lease.revision."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "apply_patch",
        "description": (
            "Call after claim_current to write the managed checkout. Pass "
            "claim_revision=claim.revision and lease_revision=lease.revision from "
            "claim_current, plus patch as a unified diff. Returns lease.revision for "
            "reply_complete."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "claim_revision": {"type": "integer"},
                "lease_revision": {"type": "integer"},
                "patch": {"type": "string"},
            },
            "required": ["claim_revision", "lease_revision", "patch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reply_complete",
        "description": (
            "Call after apply_patch to finish the work. Pass the same claim_revision, "
            "lease_revision=lease.revision returned by apply_patch, and body as the "
            "completion summary."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "claim_revision": {"type": "integer"},
                "lease_revision": {"type": "integer"},
                "body": {"type": "string"},
            },
            "required": ["claim_revision", "lease_revision", "body"],
            "additionalProperties": False,
        },
    },
]


def _cap(tmp_path: Path, *, generation: int = 1) -> Path:
    harness = harness_mod.LocalCodexHarness(capability_root=tmp_path / "caps")
    issued = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=generation,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    return issued["capability_path"]


def test_tools_publish_exact_contract_and_deny_without_side_effects(
    tmp_path: Path, monkeypatch,
) -> None:
    cap = _cap(tmp_path)
    monkeypatch.setenv("COCKPIT_CAPABILITY_FILE", str(cap))
    target = tmp_path / "checkout" / "written.txt"
    target.parent.mkdir()
    listed = mcp.dispatch({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
    })
    assert listed["result"]["tools"] == EXPECTED_TOOLS
    wired = mcp.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        claim_tools=object(), write_tools=object(),
    )
    assert wired["result"]["tools"] == EXPECTED_TOOLS
    metadata = json.dumps(listed["result"]["tools"], sort_keys=True)
    token = json.loads(cap.read_text(encoding="utf-8"))["token"]
    for forbidden in (
        "claim_not_available", "\"run\"", "\"fail\"", SENTINEL, token, FENCE,
    ):
        assert forbidden not in metadata
    arguments = {
        "claim_current": {},
        "apply_patch": {
            "claim_revision": 2, "lease_revision": 2,
            "patch": "diff --git a/a b/a\n",
        },
        "reply_complete": {
            "claim_revision": 2, "lease_revision": 3, "body": "done",
        },
    }
    for index, name in enumerate(arguments):
        reply = mcp.dispatch({
            "jsonrpc": "2.0", "id": index + 3, "method": "tools/call",
            "params": {
                "name": name, "arguments": arguments[name],
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


def test_tools_call_accepts_only_optional_object_meta(tmp_path: Path) -> None:
    cap = _cap(tmp_path)

    class ClaimTools:
        def __init__(self) -> None:
            self.calls: list[Path] = []

        def claim_current(self, capability_file: Path) -> dict[str, str]:
            self.calls.append(capability_file)
            return {"outcome": "claimed"}

    tools = ClaimTools()
    accepted = mcp.dispatch({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {
            "name": "claim_current", "arguments": {},
            "_meta": {
                "progressToken": 7,
                "x-codex-turn-metadata": {"turn_id": "turn-1"},
            },
        },
    }, capability_file=cap, claim_tools=tools)
    assert accepted["result"]["isError"] is False
    assert accepted["result"]["structuredContent"] == {"outcome": "claimed"}
    assert tools.calls == [cap]

    invalid_params = (
        {"name": "claim_current", "arguments": {}, "_meta": {}, "extra": True},
        {"name": "claim_current", "arguments": {}, "_meta": "not-an-object"},
        {"name": 1, "arguments": {}, "_meta": {}},
        {"name": "claim_current", "arguments": [], "_meta": {}},
    )
    for index, params in enumerate(invalid_params, start=10):
        denied = mcp.dispatch({
            "jsonrpc": "2.0", "id": index, "method": "tools/call",
            "params": params,
        }, capability_file=cap, claim_tools=tools)
        assert denied["result"]["isError"] is True
        assert denied["result"]["structuredContent"]["code"] == "invalid_argument"
    assert tools.calls == [cap]
