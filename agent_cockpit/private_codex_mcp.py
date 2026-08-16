"""Private stdio MCP stub. C2 tools only return claim_not_available."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import local_codex_harness as harness_mod


TOOLS = (
    "claim_current", "apply_patch", "run", "reply_complete", "fail",
)
NOT_AVAILABLE = "claim_not_available"


def _denied() -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": NOT_AVAILABLE}],
        "isError": True,
        "structuredContent": {"code": NOT_AVAILABLE},
    }


def _capability_ok(path: Path | None) -> bool:
    if path is None:
        raw = os.environ.get("COCKPIT_CAPABILITY_FILE")
        if not raw:
            return False
        path = Path(raw)
    try:
        record = harness_mod._read_capability(path)
    except (harness_mod.HarnessError, OSError, TypeError):
        return False
    generation = record.get("generation")
    if type(generation) is not int or generation < 1:
        return False
    current = harness_mod.current_generation(path)
    if current is not None and current != generation:
        return False
    token = record.get("token")
    return isinstance(token, str) and len(token) == 64


def dispatch(
    message: dict[str, Any], *, capability_file: Path | None = None,
) -> dict[str, Any] | None:
    method = message.get("method")
    ident = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": ident,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cockpit-private", "version": "c2"},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": ident,
            "result": {
                "tools": [
                    {"name": name, "description": NOT_AVAILABLE, "inputSchema": {
                        "type": "object", "properties": {},
                    }}
                    for name in TOOLS
                ],
            },
        }
    if method == "tools/call":
        if not _capability_ok(capability_file):
            return {"jsonrpc": "2.0", "id": ident, "result": _denied()}
        return {"jsonrpc": "2.0", "id": ident, "result": _denied()}
    return {
        "jsonrpc": "2.0", "id": ident,
        "error": {"code": -32601, "message": NOT_AVAILABLE},
    }


def main() -> int:
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        reply = dispatch(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
