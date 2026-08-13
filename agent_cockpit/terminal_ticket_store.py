"""Durable, workspace-scoped Terminal Ticket v1; it never controls a PTY."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
MAX_CURSOR = 1_000_000
MAX_RECEIPTS = 32
_STATES = frozenset({"stopped", "running", "paused", "recovery_required", "unknown"})
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
    request_digest TEXT NOT NULL, ticket_id TEXT NOT NULL,
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


def _opaque(value: object, *, prefix: str | None = None) -> str:
    result = _text(value, maximum=80)
    if prefix and not result.startswith(prefix):
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
        kind = _text(item["type"])
        reference = _text(item["id"], maximum=256)
        if "/" in reference or "\\" in reference:
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
        refs = json.loads(row["receipt_refs_json"])
        return TerminalTicket(row["ticket_id"], row["project_id"], row["workspace_id"], row["desired_state"], row["observed_state"], int(row["engine_generation"]), int(row["reconnect_cursor"]), tuple(refs), int(row["revision"]), row["created_at"], row["updated_at"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _fail("store_corrupt", exc)


def _materialize(row: sqlite3.Row) -> TerminalTicket:
    try:
        return _record(row)
    except TerminalTicketError:
        raise
    except Exception as exc:
        _fail("store_corrupt", exc)


class TerminalTicketStore:
    def __init__(self, path: Path): self.path = _path(path)
    def close(self) -> None: pass

    def create(self, value: Mapping[str, Any], *, idempotency_key: str) -> TerminalTicket:
        ticket = _input(value, creating=True); key = _text(idempotency_key, maximum=128)
        digest = hashlib.sha256(_canonical(ticket).encode()).hexdigest(); connection = None
        try:
            connection = _connect(self.path, write=True); _schema(connection); connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT request_digest,ticket_id FROM terminal_ticket_idempotency WHERE project_id=? AND workspace_id=? AND idempotency_key=?", (ticket["project_id"], ticket["workspace_id"], key)).fetchone()
            if row is not None:
                if row["request_digest"] != digest: _fail("idempotency_conflict")
                record = connection.execute("SELECT * FROM terminal_tickets WHERE ticket_id=?", (row["ticket_id"],)).fetchone()
                connection.execute("COMMIT"); return _materialize(record)
            ticket_id, now = "ttk_" + secrets.token_hex(16), _now()
            connection.execute("INSERT INTO terminal_tickets VALUES (?,?,?,?,?,?,?,?,?,?,?)", (ticket_id, ticket["project_id"], ticket["workspace_id"], ticket["desired_state"], ticket["observed_state"], ticket["engine_generation"], ticket["reconnect_cursor"], _canonical(ticket["receipt_refs"]), 1, now, now))
            connection.execute("INSERT INTO terminal_ticket_idempotency VALUES (?,?,?,?,?)", (ticket["project_id"], ticket["workspace_id"], key, digest, ticket_id))
            record = connection.execute("SELECT * FROM terminal_tickets WHERE ticket_id=?", (ticket_id,)).fetchone(); connection.execute("COMMIT"); return _materialize(record)
        except TerminalTicketError:
            _rollback(connection); raise
        except sqlite3.Error as exc:
            _rollback(connection); _fail("store_write_failed", exc)
        finally:
            if connection is not None: connection.close()

    def get(self, *, project_id: str, workspace_id: str, ticket_id: str) -> TerminalTicket | None:
        project_id, workspace_id, ticket_id = _opaque(project_id, prefix="prj_"), _opaque(workspace_id, prefix="ws_"), _opaque(ticket_id, prefix="ttk_")
        return self._read_one("SELECT * FROM terminal_tickets WHERE project_id=? AND workspace_id=? AND ticket_id=?", (project_id, workspace_id, ticket_id))

    def list(self, *, project_id: str, workspace_id: str, after_ticket_id: str | None = None, limit: int = 50) -> tuple[tuple[TerminalTicket, ...], str | None]:
        project_id, workspace_id = _opaque(project_id, prefix="prj_"), _opaque(workspace_id, prefix="ws_")
        if after_ticket_id is not None: after_ticket_id = _opaque(after_ticket_id, prefix="ttk_")
        _integer(limit, minimum=1, maximum=100)
        rows = self._read_many("SELECT * FROM terminal_tickets WHERE project_id=? AND workspace_id=? AND (? IS NULL OR ticket_id>?) ORDER BY ticket_id LIMIT ?", (project_id, workspace_id, after_ticket_id, after_ticket_id, limit + 1)); visible = rows[:limit]
        return visible, visible[-1].ticket_id if len(rows) > limit else None

    def update(self, *, project_id: str, workspace_id: str, ticket_id: str, expected_revision: int, value: Mapping[str, Any]) -> TerminalTicket:
        ticket = _input(value, creating=False); project_id, workspace_id, ticket_id = _opaque(project_id, prefix="prj_"), _opaque(workspace_id, prefix="ws_"), _opaque(ticket_id, prefix="ttk_"); _integer(expected_revision, minimum=1)
        if (ticket["project_id"], ticket["workspace_id"]) != (project_id, workspace_id): _fail("invalid_argument")
        connection = None
        try:
            connection = _connect(self.path, write=True); _schema(connection); connection.execute("BEGIN IMMEDIATE")
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
            connection = _connect(self.path, write=False); _schema(connection); connection.execute("BEGIN")
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
        connection = _connect(store.path, write=False); _schema(connection)
    except TerminalTicketError: raise
    except sqlite3.Error as exc: _fail("store_read_failed", exc)
    finally:
        if connection is not None: connection.close()
    return store
