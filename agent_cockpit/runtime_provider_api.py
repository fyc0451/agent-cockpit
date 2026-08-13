"""Normalized read contract for the Local Herdr Runtime Provider."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from . import runtime_provider_store as provider_store


NODE_ID = "local"
PROVIDER_ID = "local_herdr"
PROTOCOL_VERSION = 1
_SESSION_STATUS = frozenset({"running", "stopped"})
_ERRORS = {
    "invalid_node": (404, False),
    "provider_not_installed": (503, False),
    "protocol_mismatch": (503, False),
    "transport_timeout": (504, True),
    "transport_unavailable": (503, True),
    "source_malformed": (503, False),
    "store_read_failed": (503, True),
    "schema_missing": (503, False),
    "schema_mismatch": (503, False),
}


class ProviderTransportError(RuntimeError):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


class RuntimeProviderError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class LocalHerdrTransport(Protocol):
    def capabilities(self) -> object: ...
    def handshake(self) -> object: ...
    def list_sessions(self) -> object: ...
    def snapshot(self, session_name: str) -> object: ...


@dataclass(frozen=True)
class LocalHerdrProvider:
    transport: LocalHerdrTransport
    observation_store: provider_store.RuntimeProviderStore | None = None
    node_id: str = NODE_ID

    def capabilities(self) -> dict[str, object]:
        self._require_local()
        raw = self._transport("capabilities")
        value = _exact(raw, {"provider_id", "protocol", "installed", "methods"})
        if value["provider_id"] != PROVIDER_ID or type(value["installed"]) is not bool:
            _fail("source_malformed")
        if value["installed"] is False:
            _fail("provider_not_installed")
        if type(value["protocol"]) is not int or value["protocol"] != PROTOCOL_VERSION:
            _fail("protocol_mismatch")
        methods = _string_list(value["methods"])
        required = {"handshake", "list_sessions", "snapshot"}
        if not required.issubset(methods):
            _fail("protocol_mismatch")
        return _success({
            "node_id": NODE_ID,
            "provider_id": PROVIDER_ID,
            "protocol": PROTOCOL_VERSION,
            "capabilities": _capabilities(read_available=True),
        }, partial=False, source_status="available")

    def handshake(self) -> dict[str, object]:
        self._require_local()
        self.capabilities()
        raw = _exact(self._transport("handshake"), {
            "provider_id", "protocol", "runtime_identity", "epoch",
        })
        if raw["provider_id"] != PROVIDER_ID:
            _fail("source_malformed")
        if type(raw["protocol"]) is not int or raw["protocol"] != PROTOCOL_VERSION:
            _fail("protocol_mismatch")
        identity, epoch, status, watermark = self._identity(raw)
        return _success({
            "node_id": NODE_ID, "provider_id": PROVIDER_ID,
            "protocol": PROTOCOL_VERSION, "runtime_identity": identity,
            "identity_status": status, "epoch": epoch,
            "observation_watermark": watermark,
            "capabilities": _capabilities(read_available=True),
        }, partial=status != "verified", source_status="available",
           warnings=[] if status == "verified" else ["identity_unverified"])

    def list_sessions(self) -> dict[str, object]:
        self._require_local()
        handshake = self.handshake()["data"]
        raw = _exact(self._transport("list_sessions"), {"sessions"})
        rows = raw["sessions"]
        if not isinstance(rows, list):
            _fail("source_malformed")
        sessions: list[dict[str, object]] = []
        errors: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for row in rows:
            normalized = _session(row)
            session_id = str(normalized["session_id"])
            name = str(normalized["name"])
            if session_id in seen_ids or name in seen_names:
                _fail("source_malformed")
            seen_ids.add(session_id)
            seen_names.add(name)
            try:
                snap = _snapshot(self._transport("snapshot", name), normalized)
            except RuntimeProviderError as exc:
                errors.append({"session_id": normalized["session_id"], "code": exc.code})
                sessions.append({**normalized, "snapshot": None, "state": "unavailable"})
            else:
                sessions.append({**normalized, "snapshot": snap, "state": "available"})
        partial = bool(errors)
        return _success({
            "node_id": NODE_ID, "provider_id": PROVIDER_ID,
            "runtime_identity": handshake["runtime_identity"],
            "identity_status": handshake["identity_status"], "epoch": handshake["epoch"],
            "sessions": sessions, "session_errors": errors,
            "empty": not rows,
        }, partial=partial or handshake["identity_status"] != "verified",
           source_status="partial" if partial else "available",
           warnings=(["identity_unverified"] if handshake["identity_status"] != "verified" else [])
                    + (["session_snapshot_partial"] if partial else []))

    def snapshot(self) -> dict[str, object]:
        return self.list_sessions()

    def _require_local(self) -> None:
        if self.node_id != NODE_ID:
            _fail("invalid_node")

    def _transport(self, method: str, *args: object) -> object:
        try:
            return getattr(self.transport, method)(*args)
        except ProviderTransportError as exc:
            if exc.kind == "not_installed":
                _fail("provider_not_installed")
            if exc.kind == "timeout":
                _fail("transport_timeout")
            _fail("transport_unavailable")
        except (RuntimeProviderError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _fail("transport_unavailable")

    def _identity(self, raw: dict[str, Any]) -> tuple[str | None, int | None, str, int | None]:
        identity = raw["runtime_identity"]
        epoch = raw["epoch"]
        if identity is not None and not _safe_identity(identity):
            _fail("source_malformed")
        if epoch is not None and (type(epoch) is not int or epoch < 0):
            _fail("source_malformed")
        if (identity is None) != (epoch is None):
            _fail("source_malformed")
        observed = None
        if self.observation_store is not None:
            try:
                observed = self.observation_store.get_observation(
                    provider_id=PROVIDER_ID, node_id=NODE_ID,
                )
            except provider_store.RuntimeProviderStoreError as exc:
                _fail(exc.code if exc.code in _ERRORS else "store_read_failed")
        if (
            observed is not None and observed.identity_status == "verified"
            and identity == observed.runtime_identity and epoch == observed.epoch
        ):
            return identity, epoch, "verified", observed.watermark
        return None, None, "identity_unverified", observed.watermark if observed else None


def error_response(error: BaseException) -> tuple[int, dict[str, object]]:
    code = error.code if isinstance(error, RuntimeProviderError) else "transport_unavailable"
    status, retryable = _ERRORS.get(code, (500, False))
    public = code if code in _ERRORS else "transport_unavailable"
    return status, {"error": {
        "code": public,
        "message": public.replace("_", " "),
        "retryable": retryable,
        "request_id": _request_id(),
        "details": {},
    }}


def _session(value: object) -> dict[str, object]:
    row = _exact(value, {"session_id", "name", "status"})
    if not _safe_identity(row["session_id"], maximum=128):
        _fail("source_malformed")
    if not _safe_identity(row["name"], maximum=64):
        _fail("source_malformed")
    if type(row["status"]) is not str or row["status"] not in _SESSION_STATUS:
        _fail("source_malformed")
    return {"session_id": row["session_id"], "name": row["name"], "status": row["status"]}


def _snapshot(value: object, session: dict[str, object]) -> dict[str, object]:
    row = _exact(value, {"session_id", "process_state", "agent_count"})
    if row["session_id"] != session["session_id"]:
        _fail("source_malformed")
    if (
        type(row["process_state"]) is not str
        or row["process_state"] not in {"running", "stopped", "unknown"}
    ):
        _fail("source_malformed")
    if type(row["agent_count"]) is not int or row["agent_count"] < 0:
        _fail("source_malformed")
    return {
        "process_state": row["process_state"],
        "agent_count": row["agent_count"],
    }


def _capabilities(*, read_available: bool) -> dict[str, dict[str, object]]:
    return {
        "runtime.read": {"available": read_available, "reason": None if read_available else "provider_unavailable"},
        "runtime.attach": {"available": False, "reason": "operation_journal_required"},
        "runtime.terminal": {"available": False, "reason": "workspace_ticket_required"},
        "runtime.recovery": {"available": False, "reason": "operation_journal_required"},
    }


def _success(
    data: dict[str, object], *, partial: bool, source_status: str,
    warnings: list[str] | None = None,
) -> dict[str, object]:
    return {"data": data, "meta": {
        "request_id": _request_id(),
        "generated_at": datetime.now(UTC).isoformat(),
        "partial": partial,
        "sources": [{
            "name": "local_herdr", "status": source_status,
            "observed_at": None, "reason": None,
        }],
        "warnings": list(warnings or []),
        "capabilities": _capabilities(read_available=True),
    }}


def _request_id() -> str:
    return "req_" + secrets.token_hex(16)


def _exact(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail("source_malformed")
    return value


def _string_list(value: object) -> set[str]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        _fail("source_malformed")
    return set(value)


def _safe_identity(value: object, *, maximum: int = 128) -> bool:
    return (
        type(value) is str and 0 < len(value) <= maximum
        and all(char.isascii() and (char.isalnum() or char in "_-.:@") for char in value)
    )


def _fail(code: str) -> None:
    raise RuntimeProviderError(code) from None
