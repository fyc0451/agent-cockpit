"""Durable, workspace-scoped Terminal Ticket v1; it never controls a PTY."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
MAX_CURSOR = 1_000_000
MAX_RECEIPTS = 32
MAX_SIGNED64 = 2**63 - 1
_STATES = frozenset({"stopped", "running", "paused", "recovery_required", "unknown"})
_REGISTRY_ID = re.compile(r"^(?:prj|ws)_[0-9a-f]{32}$")
_TICKET_ID = re.compile(r"^ttk_[0-9a-f]{32}$")
_RECEIPT_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RECEIPT_ID = re.compile(r"^[a-z][a-z0-9]*_[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA = """
CREATE TABLE terminal_tickets (
    ticket_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
    desired_state TEXT NOT NULL, observed_state TEXT NOT NULL, engine_generation INTEGER NOT NULL,
    reconnect_cursor INTEGER NOT NULL, receipt_refs_json TEXT NOT NULL, revision INTEGER NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX terminal_tickets_scope ON terminal_tickets(project_id, workspace_id, ticket_id);
CREATE TABLE terminal_ticket_idempotency (
    project_id TEXT NOT NULL, workspace_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    method TEXT NOT NULL, request_digest TEXT NOT NULL, ticket_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(project_id, workspace_id, idempotency_key)
);
CREATE TABLE terminal_ticket_schema (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_DIGEST = hashlib.sha256(_SCHEMA.encode()).hexdigest()


class TerminalTicketError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str, cause: BaseException | None = None) -> None:
    error = TerminalTicketError(code)
    if cause is None:
        raise error
    try:
        raise error from None
    except TerminalTicketError:
        error.__cause__ = error.__context__ = None
        error.__suppress_context__ = True
        raise


def _text(value: object, *, nullable: bool = False, maximum: int = 128) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 33 or ord(c) == 127 for c in value):
        _fail("invalid_argument")
    return value


def _idempotency_key(value: object, *, persisted: bool = False) -> str:
    try:
        return _text(value, maximum=128)
    except TerminalTicketError as exc:
        if persisted:
            _fail("store_corrupt", exc)
        raise


def _opaque(value: object, *, prefix: str | None = None) -> str:
    result = _text(value, maximum=36)
    if prefix not in {"prj_", "ws_"} or not _REGISTRY_ID.fullmatch(result) or not result.startswith(prefix):
        _fail("invalid_argument")
    return result


def _ticket_id(value: object) -> str:
    result = _text(value, maximum=36)
    if not _TICKET_ID.fullmatch(result):
        _fail("invalid_argument")
    return result


def _integer(value: object, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("invalid_argument")
    return value


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        _fail("invalid_argument", exc)


def _receipts(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list) or len(value) > MAX_RECEIPTS:
        _fail("invalid_argument")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"type", "id"}:
            _fail("invalid_argument")
        kind = _text(item["type"], maximum=64)
        reference = _text(item["id"], maximum=36)
        if not _RECEIPT_TYPE.fullmatch(kind) or not _RECEIPT_ID.fullmatch(reference):
            _fail("invalid_argument")
        result.append({"type": kind, "id": reference})
    return tuple(result)


def _input(value: object, *, creating: bool) -> dict[str, Any]:
    required = {"project_id", "workspace_id", "desired_state", "observed_state", "engine_generation", "reconnect_cursor", "receipt_refs"}
    if not isinstance(value, Mapping) or set(value) != required:
        _fail("invalid_argument")
    desired, observed = _text(value["desired_state"]), _text(value["observed_state"])
    if desired not in _STATES or observed not in _STATES:
        _fail("invalid_argument")
    return {
        "project_id": _opaque(value["project_id"], prefix="prj_"),
        "workspace_id": _opaque(value["workspace_id"], prefix="ws_"),
        "desired_state": desired, "observed_state": observed,
        "engine_generation": _integer(value["engine_generation"], minimum=1),
        "reconnect_cursor": _integer(value["reconnect_cursor"], maximum=MAX_CURSOR),
        "receipt_refs": _receipts(value["receipt_refs"]),
    }


def _path(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        _fail("store_unsafe")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            _fail("store_unsafe", exc)
        if stat.S_ISLNK(info.st_mode):
            _fail("store_unsafe")
    return path


def _leaf(path: Path, *, missing: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        _fail(missing)
    except OSError as exc:
        _fail("store_unsafe", exc)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        _fail("store_unsafe")


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    _leaf(path, missing="schema_missing")
    connection = sqlite3.connect(str(path) if write else path.as_uri() + "?mode=ro", uri=not write, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        if not write:
            connection.execute("PRAGMA query_only=ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _schema(connection: sqlite3.Connection) -> None:
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        digest = connection.execute("SELECT value FROM terminal_ticket_schema WHERE key='schema_digest'").fetchone()
        objects = tuple(tuple(row) for row in connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))
        memory = sqlite3.connect(":memory:")
        try:
            memory.executescript(_SCHEMA)
            expected = tuple(tuple(row) for row in memory.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))
        finally:
            memory.close()
        if version != SCHEMA_VERSION or digest is None or digest[0] != _DIGEST or objects != expected:
            _fail("schema_fingerprint_mismatch")
    except TerminalTicketError:
        raise
    except (sqlite3.Error, IndexError, KeyError, TypeError, ValueError) as exc:
        _fail("schema_fingerprint_mismatch", exc)


def _rollback(connection: sqlite3.Connection | None) -> None:
    try:
        if connection is not None and connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp(value: object) -> str:
    text = _text(value, maximum=32)
    if not text.endswith("Z"):
        _fail("store_corrupt")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        _fail("store_corrupt", exc)
    if parsed.tzinfo != UTC or parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") != text:
        _fail("store_corrupt")
    return text


@dataclass(frozen=True)
class TerminalTicket:
    ticket_id: str
    project_id: str
    workspace_id: str
    desired_state: str
    observed_state: str
    engine_generation: int
    reconnect_cursor: int
    receipt_refs: tuple[Mapping[str, str], ...]
    revision: int
    created_at: str
    updated_at: str

    def public_dict(self) -> dict[str, object]:
        return {"ticket_id": self.ticket_id, "project_id": self.project_id, "workspace_id": self.workspace_id,
                "desired_state": self.desired_state, "observed_state": self.observed_state,
                "engine_generation": self.engine_generation, "reconnect_cursor": self.reconnect_cursor,
                "receipt_refs": [dict(item) for item in self.receipt_refs], "revision": self.revision,
                "created_at": self.created_at, "updated_at": self.updated_at}


def _record(row: sqlite3.Row) -> TerminalTicket:
    try:
        required = {"ticket_id", "project_id", "workspace_id", "desired_state", "observed_state", "engine_generation", "reconnect_cursor", "receipt_refs_json", "revision", "created_at", "updated_at"}
        if set(row.keys()) != required:
            _fail("store_corrupt")
        ticket_id = row["ticket_id"]
        if not isinstance(ticket_id, str) or not _TICKET_ID.fullmatch(ticket_id):
            _fail("store_corrupt")
        project_id, workspace_id = row["project_id"], row["workspace_id"]
        if not isinstance(project_id, str) or not _REGISTRY_ID.fullmatch(project_id) or not project_id.startswith("prj_"):
            _fail("store_corrupt")
        if not isinstance(workspace_id, str) or not _REGISTRY_ID.fullmatch(workspace_id) or not workspace_id.startswith("ws_"):
            _fail("store_corrupt")
        desired, observed = row["desired_state"], row["observed_state"]
        if type(desired) is not str or type(observed) is not str or desired not in _STATES or observed not in _STATES:
            _fail("store_corrupt")
        generation = _persisted_integer(row["engine_generation"], minimum=1)
        cursor = _persisted_integer(row["reconnect_cursor"], maximum=MAX_CURSOR)
        revision = _persisted_integer(row["revision"], minimum=1)
        raw_refs = row["receipt_refs_json"]
        if not isinstance(raw_refs, str) or _canonical(json.loads(raw_refs)) != raw_refs:
            _fail("store_corrupt")
        refs = _receipts(json.loads(raw_refs))
        created, updated = _timestamp(row["created_at"]), _timestamp(row["updated_at"])
        if created > updated:
            _fail("store_corrupt")
        return TerminalTicket(ticket_id, project_id, workspace_id, desired, observed, generation, cursor, refs, revision, created, updated)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _fail("store_corrupt", exc)


def _persisted_integer(value: object, *, minimum: int = 0, maximum: int = MAX_SIGNED64) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("store_corrupt")
    return value


def _receipt(row: sqlite3.Row, *, project_id: str, workspace_id: str, key: str, digest: str) -> TerminalTicket:
    try:
        required = {"project_id", "workspace_id", "idempotency_key", "method", "request_digest", "ticket_id", "result_json"}
        if set(row.keys()) != required or row["project_id"] != project_id or row["workspace_id"] != workspace_id:
            _fail("store_corrupt")
        if row["idempotency_key"] != key or row["method"] != "create" or row["request_digest"] != digest or not _DIGEST_RE.fullmatch(str(row["request_digest"])):
            _fail("store_corrupt")
        raw = row["result_json"]
        if not isinstance(raw, str):
            _fail("store_corrupt")
        result = json.loads(raw)
        if not isinstance(result, dict) or _canonical(result) != raw:
            _fail("store_corrupt")
        ticket = _ticket_from_public(result)
        if ticket.revision != 1 or ticket.project_id != project_id or ticket.workspace_id != workspace_id or ticket.ticket_id != row["ticket_id"]:
            _fail("store_corrupt")
        original_request = {
            "project_id": ticket.project_id, "workspace_id": ticket.workspace_id,
            "desired_state": ticket.desired_state, "observed_state": ticket.observed_state,
            "engine_generation": ticket.engine_generation, "reconnect_cursor": ticket.reconnect_cursor,
            "receipt_refs": [dict(reference) for reference in ticket.receipt_refs],
        }
        if hashlib.sha256(_canonical(original_request).encode()).hexdigest() != digest:
            _fail("store_corrupt")
        return ticket
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _fail("store_corrupt", exc)
    except TerminalTicketError as exc:
        if exc.code == "store_corrupt":
            raise
        _fail("store_corrupt", exc)


def _ticket_from_public(value: object) -> TerminalTicket:
    if not isinstance(value, dict) or set(value) != {"ticket_id", "project_id", "workspace_id", "desired_state", "observed_state", "engine_generation", "reconnect_cursor", "receipt_refs", "revision", "created_at", "updated_at"}:
        _fail("store_corrupt")
    row = {key: value[key] for key in value}
    row["receipt_refs_json"] = _canonical(value["receipt_refs"])
    del row["receipt_refs"]
    class PublicRow(dict):
        def keys(self): return super().keys()
    return _record(PublicRow(row))


def _validate_persisted(connection: sqlite3.Connection) -> None:
    try:
        records: dict[str, TerminalTicket] = {}
        receipt_counts: dict[str, int] = {}
        for row in connection.execute("SELECT * FROM terminal_tickets").fetchall():
            ticket = _materialize(row)
            if ticket.ticket_id in records:
                _fail("store_corrupt")
            records[ticket.ticket_id] = ticket
        for row in connection.execute("SELECT * FROM terminal_ticket_idempotency").fetchall():
            project_id, workspace_id, key, digest = row["project_id"], row["workspace_id"], row["idempotency_key"], row["request_digest"]
            if not isinstance(project_id, str) or not isinstance(workspace_id, str) or not isinstance(digest, str):
                _fail("store_corrupt")
            key = _idempotency_key(key, persisted=True)
            receipt = _receipt(row, project_id=project_id, workspace_id=workspace_id, key=key, digest=digest)
            current = records.get(receipt.ticket_id)
            if current is None or current.project_id != receipt.project_id or current.workspace_id != receipt.workspace_id:
                _fail("store_corrupt")
            receipt_counts[receipt.ticket_id] = receipt_counts.get(receipt.ticket_id, 0) + 1
        if set(receipt_counts) != set(records) or any(count != 1 for count in receipt_counts.values()):
            _fail("store_corrupt")
    except TerminalTicketError:
        raise
    except (sqlite3.Error, IndexError, KeyError, TypeError, ValueError) as exc:
        _fail("store_corrupt", exc)


def _materialize(row: sqlite3.Row) -> TerminalTicket:
    try:
        return _record(row)
    except TerminalTicketError as exc:
        if exc.code == "store_corrupt":
            raise
        _fail("store_corrupt", exc)
    except Exception as exc:
        _fail("store_corrupt", exc)


class TerminalTicketStore:
    def __init__(self, path: Path): self.path = _path(path)
    def close(self) -> None: pass

    def create(self, value: Mapping[str, Any], *, idempotency_key: str) -> TerminalTicket:
        ticket = _input(value, creating=True); key = _idempotency_key(idempotency_key)
        digest = hashlib.sha256(_canonical(ticket).encode()).hexdigest(); connection = None
        try:
            connection = _connect(self.path, write=True); _schema(connection); connection.execute("BEGIN IMMEDIATE")
            _validate_persisted(connection)
            row = connection.execute("SELECT * FROM terminal_ticket_idempotency WHERE project_id=? AND workspace_id=? AND idempotency_key=?", (ticket["project_id"], ticket["workspace_id"], key)).fetchone()
            if row is not None:
                if row["request_digest"] != digest:
                    _fail("idempotency_conflict")
                replay = _receipt(row, project_id=ticket["project_id"], workspace_id=ticket["workspace_id"], key=key, digest=digest)
                connection.execute("COMMIT"); return replay
            ticket_id, now = "ttk_" + secrets.token_hex(16), _now()
            connection.execute("INSERT INTO terminal_tickets VALUES (?,?,?,?,?,?,?,?,?,?,?)", (ticket_id, ticket["project_id"], ticket["workspace_id"], ticket["desired_state"], ticket["observed_state"], ticket["engine_generation"], ticket["reconnect_cursor"], _canonical(ticket["receipt_refs"]), 1, now, now))
            record = connection.execute("SELECT * FROM terminal_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
            result = _materialize(record)
            connection.execute("INSERT INTO terminal_ticket_idempotency VALUES (?,?,?,?,?,?,?)", (ticket["project_id"], ticket["workspace_id"], key, "create", digest, ticket_id, _canonical(result.public_dict())))
            connection.execute("COMMIT"); return result
        except TerminalTicketError:
            _rollback(connection); raise
        except sqlite3.Error as exc:
            _rollback(connection); _fail("store_write_failed", exc)
        finally:
            if connection is not None: connection.close()

    def get(self, *, project_id: str, workspace_id: str, ticket_id: str) -> TerminalTicket | None:
        project_id, workspace_id, ticket_id = _opaque(project_id, prefix="prj_"), _opaque(workspace_id, prefix="ws_"), _ticket_id(ticket_id)
        return self._read_one("SELECT * FROM terminal_tickets WHERE project_id=? AND workspace_id=? AND ticket_id=?", (project_id, workspace_id, ticket_id))

    def list(self, *, project_id: str, workspace_id: str, after_ticket_id: str | None = None, limit: int = 50) -> tuple[tuple[TerminalTicket, ...], str | None]:
        project_id, workspace_id = _opaque(project_id, prefix="prj_"), _opaque(workspace_id, prefix="ws_")
        if after_ticket_id is not None: after_ticket_id = _ticket_id(after_ticket_id)
        _integer(limit, minimum=1, maximum=100)
        rows = self._read_many("SELECT * FROM terminal_tickets WHERE project_id=? AND workspace_id=? AND (? IS NULL OR ticket_id>?) ORDER BY ticket_id LIMIT ?", (project_id, workspace_id, after_ticket_id, after_ticket_id, limit + 1)); visible = rows[:limit]
        return visible, visible[-1].ticket_id if len(rows) > limit else None

    def update(self, *, project_id: str, workspace_id: str, ticket_id: str, expected_revision: int, value: Mapping[str, Any]) -> TerminalTicket:
        ticket = _input(value, creating=False); project_id, workspace_id, ticket_id = _opaque(project_id, prefix="prj_"), _opaque(workspace_id, prefix="ws_"), _ticket_id(ticket_id); _integer(expected_revision, minimum=1)
        if expected_revision == MAX_SIGNED64:
            _fail("revision_conflict")
        if (ticket["project_id"], ticket["workspace_id"]) != (project_id, workspace_id): _fail("invalid_argument")
        connection = None
        try:
            connection = _connect(self.path, write=True); _schema(connection); connection.execute("BEGIN IMMEDIATE"); _validate_persisted(connection)
            cursor = connection.execute("UPDATE terminal_tickets SET desired_state=?,observed_state=?,engine_generation=?,reconnect_cursor=?,receipt_refs_json=?,revision=revision+1,updated_at=? WHERE project_id=? AND workspace_id=? AND ticket_id=? AND revision=? AND engine_generation<=?", (ticket["desired_state"], ticket["observed_state"], ticket["engine_generation"], ticket["reconnect_cursor"], _canonical(ticket["receipt_refs"]), _now(), project_id, workspace_id, ticket_id, expected_revision, ticket["engine_generation"]))
            if cursor.rowcount != 1: _fail("revision_conflict")
            row = connection.execute("SELECT * FROM terminal_tickets WHERE ticket_id=?", (ticket_id,)).fetchone(); connection.execute("COMMIT"); return _materialize(row)
        except TerminalTicketError:
            _rollback(connection); raise
        except sqlite3.Error as exc:
            _rollback(connection); _fail("store_write_failed", exc)
        finally:
            if connection is not None: connection.close()

    def _read_one(self, sql: str, params: tuple[Any, ...]) -> TerminalTicket | None:
        values = self._read_many(sql, params); return values[0] if values else None
    def _read_many(self, sql: str, params: tuple[Any, ...]) -> tuple[TerminalTicket, ...]:
        connection = None
        try:
            connection = _connect(self.path, write=False); _schema(connection); connection.execute("BEGIN"); _validate_persisted(connection)
            return tuple(_materialize(row) for row in connection.execute(sql, params).fetchall())
        except TerminalTicketError: raise
        except (sqlite3.Error, IndexError, KeyError, TypeError, ValueError) as exc: _fail("store_read_failed", exc)
        finally:
            if connection is not None: connection.close()


def initialize(path: Path) -> TerminalTicketStore:
    path = _path(path)
    if path.exists(): return open_existing(path)
    if not path.parent.is_dir() or path.parent.is_symlink(): _fail("store_unsafe")
    connection = None
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd); os.chmod(path, 0o600)
        connection = _connect(path, write=True)
        connection.executescript("BEGIN IMMEDIATE;" + _SCHEMA + "INSERT INTO terminal_ticket_schema VALUES ('schema_digest','" + _DIGEST + "');PRAGMA user_version=1;COMMIT;")
    except TerminalTicketError: raise
    except (OSError, sqlite3.Error) as exc: _fail("store_write_failed", exc)
    finally:
        if connection is not None: connection.close()
    return open_existing(path)


def open_existing(path: Path) -> TerminalTicketStore:
    store = TerminalTicketStore(path); connection = None
    try:
        connection = _connect(store.path, write=False); _schema(connection); connection.execute("BEGIN"); _validate_persisted(connection)
    except TerminalTicketError: raise
    except sqlite3.Error as exc: _fail("store_read_failed", exc)
    finally:
        if connection is not None: connection.close()
    return store
