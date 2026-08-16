"""Private stdio MCP router with capability-bound workspace tools."""
from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import local_codex_harness as harness_mod


_TOOL_DEFINITIONS = {
    "claim_current": {
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
    "apply_patch": {
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
    "reply_complete": {
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
}
TOOLS = tuple(_TOOL_DEFINITIONS)
NOT_AVAILABLE = "claim_not_available"
_PUBLIC_CODES = frozenset({
    "invalid_argument", "runtime_capability_invalid", "work_item_not_found",
    "preparation_not_found", "idempotency_conflict", "stale_revision",
    "stale_generation", "delivery_conflict", "claim_conflict",
    "claim_not_active", "execution_terminal", "runtime_unavailable",
    "operation_journal_unavailable", "wakeup_outcome_unknown",
    "schema_missing", "workspace_work_schema_missing", "migration_required",
    "future_schema", "schema_fingerprint_mismatch", "store_unsafe",
    "store_corrupt", "store_read_failed", "store_write_failed",
    "patch_invalid", "lease_not_active", "fence_rejected",
    "checkout_untrusted", "reply_conflict", "reconcile_required",
    "patch_outcome_unknown",
})


def _denied(code: str = NOT_AVAILABLE) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": code}],
        "isError": True,
        "structuredContent": {"code": code},
    }


def _success(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(value, separators=(",", ":"), ensure_ascii=True),
        }],
        "isError": False,
        "structuredContent": value,
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
    try:
        current = harness_mod.current_generation(path)
    except harness_mod.HarnessError:
        return False
    if current != generation:
        return False
    token = record.get("token")
    return isinstance(token, str) and len(token) == 64


def dispatch(
    message: dict[str, Any], *, capability_file: Path | None = None,
    claim_tools: Any | None = None, write_tools: Any | None = None,
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
        if claim_tools is None and write_tools is None:
            names = TOOLS
        else:
            names = (
                *(("claim_current",) if claim_tools is not None else ()),
                *(("apply_patch", "reply_complete") if write_tools is not None else ()),
            )
        return {
            "jsonrpc": "2.0", "id": ident,
            "result": {
                "tools": [deepcopy(_TOOL_DEFINITIONS[name]) for name in names],
            },
        }
    if method == "tools/call":
        if claim_tools is None and write_tools is None:
            if not _capability_ok(capability_file):
                return {"jsonrpc": "2.0", "id": ident, "result": _denied()}
            return {"jsonrpc": "2.0", "id": ident, "result": _denied()}
        if not _capability_ok(capability_file):
            return {
                "jsonrpc": "2.0", "id": ident,
                "result": _denied("runtime_capability_invalid"),
            }
        params = message.get("params")
        if not isinstance(params, dict) or set(params) != {"name", "arguments"}:
            return {
                "jsonrpc": "2.0", "id": ident,
                "result": _denied("invalid_argument"),
            }
        name = params.get("name")
        arguments = params.get("arguments")
        if name == "claim_current":
            valid_call = claim_tools is not None and arguments == {}
        elif name in {"apply_patch", "reply_complete"}:
            valid_call = write_tools is not None and isinstance(arguments, dict)
        else:
            valid_call = False
        if not valid_call:
            return {
                "jsonrpc": "2.0", "id": ident,
                "result": _denied("invalid_argument"),
            }
        path = capability_file
        if path is None:
            raw = os.environ.get("COCKPIT_CAPABILITY_FILE")
            path = None if not raw else Path(raw)
        if path is None:
            return {
                "jsonrpc": "2.0", "id": ident,
                "result": _denied("runtime_capability_invalid"),
            }
        try:
            if name == "claim_current":
                value = claim_tools.claim_current(path)
            elif name == "apply_patch":
                value = write_tools.apply_patch(path, arguments)
            else:
                value = write_tools.reply_complete(path, arguments)
            return {"jsonrpc": "2.0", "id": ident, "result": _success(value)}
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code not in _PUBLIC_CODES:
                code = "internal_error"
            return {"jsonrpc": "2.0", "id": ident, "result": _denied(code)}
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
