"""Dormant, standalone Domain Event Journal v1."""
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
MAX_PAYLOAD_BYTES = 16_384
MAX_RECEIPTS = 32
_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "secret", "secrets", "token", "password", "credential", "authorization",
    "terminal_output", "terminal_scroll", "hidden_reasoning", "reasoning",
    "file_body", "file_content", "message_body", "full_message_body",
})
_SCHEMA = """
CREATE TABLE events (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    workspace_id TEXT,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL,
    actor_json TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    correlation_id TEXT,
    causation_id TEXT,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    receipt_refs_json TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    UNIQUE(source_type, source_event_id)
);
CREATE INDEX events_project_cursor ON events(project_id, cursor);
CREATE INDEX events_workspace_cursor ON events(workspace_id, cursor);
CREATE TABLE event_schema (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
_SCHEMA_DIGEST = hashlib.sha256(_SCHEMA.encode("utf-8")).hexdigest()


def _schema_objects(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (str(row["type"]), str(row["name"]), str(row["tbl_name"]), str(row["sql"]))
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        )
    )


def _expected_schema_objects() -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(_SCHEMA)
        return _schema_objects(connection)
    finally:
        connection.close()


_EXPECTED_SCHEMA_OBJECTS = _expected_schema_objects()


class EventStoreError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str, cause: BaseException | None = None) -> None:
    error = EventStoreError(code)
    if cause is None:
        raise error
    try:
        raise error from None
    except EventStoreError:
        error.__cause__ = None
        error.__context__ = None
        error.__suppress_context__ = True
        raise


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    event_type: str
    event_version: int
    project_id: str
    workspace_id: str | None
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    actor: Mapping[str, str]
    source_type: str
    source_event_id: str
    correlation_id: str | None
    causation_id: str | None
    occurred_at: str
    recorded_at: str
    cursor: int
    payload: Mapping[str, Any]
    receipt_refs: tuple[Mapping[str, str], ...]

    def public_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id, "event_type": self.event_type,
            "event_version": self.event_version, "project_id": self.project_id,
            "workspace_id": self.workspace_id, "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id, "aggregate_version": self.aggregate_version,
            "actor": dict(self.actor), "source": {
                "type": self.source_type, "source_event_id": self.source_event_id,
            }, "correlation_id": self.correlation_id, "causation_id": self.causation_id,
            "occurred_at": self.occurred_at, "recorded_at": self.recorded_at,
            "cursor": self.cursor, "payload": dict(self.payload),
            "receipt_refs": [dict(item) for item in self.receipt_refs],
        }


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        _fail("invalid_argument", exc)


def _text(value: object, *, maximum: int = 256, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 33 or ord(char) == 127 for char in value):
        _fail("invalid_argument")
    return value


def _timestamp(value: object) -> str:
    value = _text(value, maximum=40)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        _fail("invalid_argument", exc)
    if parsed.tzinfo is None:
        _fail("invalid_argument")
    return value


def _object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail("invalid_argument")
    return value


def _payload(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("invalid_argument")

    def inspect(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or key.lower() in _FORBIDDEN_PAYLOAD_KEYS:
                    _fail("invalid_argument")
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
        elif item is not None and type(item) not in {str, int, float, bool}:
            _fail("invalid_argument")
    inspect(value)
    encoded = _canonical(value).encode("ascii")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        _fail("invalid_argument")
    return value


def _receipt_refs(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list) or len(value) > MAX_RECEIPTS:
        _fail("invalid_argument")
    values: list[dict[str, str]] = []
    for item in value:
        item = _object(item, {"type", "id"})
        values.append({"type": _text(item["type"]), "id": _text(item["id"], maximum=512)})
    return tuple(values)


def _input(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "event_id", "event_type", "event_version", "project_id", "workspace_id",
        "aggregate_type", "aggregate_id", "aggregate_version", "actor", "source",
        "correlation_id", "causation_id", "occurred_at", "payload", "receipt_refs",
    }:
        _fail("invalid_argument")
    event_id = _text(value["event_id"])
    if not event_id.startswith("evt_"):
        _fail("invalid_argument")
    for key in ("event_type", "project_id", "aggregate_type", "aggregate_id"):
        _text(value[key], maximum=256)
    for key in ("event_version", "aggregate_version"):
        if type(value[key]) is not int or value[key] < 1:
            _fail("invalid_argument")
    actor = _object(value["actor"], {"type", "id"})
    source = _object(value["source"], {"type", "source_event_id"})
    output = dict(value)
    output["workspace_id"] = _text(value["workspace_id"], nullable=True)
    output["correlation_id"] = _text(value["correlation_id"], nullable=True)
    output["causation_id"] = _text(value["causation_id"], nullable=True)
    output["actor"] = {"type": _text(actor["type"]), "id": _text(actor["id"])}
    output["source"] = {"type": _text(source["type"]), "source_event_id": _text(source["source_event_id"], maximum=512)}
    output["occurred_at"] = _timestamp(value["occurred_at"])
    output["payload"] = _payload(value["payload"])
    output["receipt_refs"] = _receipt_refs(value["receipt_refs"])
    return output


def _path(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        _fail("store_unsafe")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            details = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            _fail("store_unsafe", exc)
        if stat.S_ISLNK(details.st_mode):
            _fail("store_unsafe")
    return path


def _leaf(path: Path, *, missing_code: str) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        _fail(missing_code)
    except OSError as exc:
        _fail("store_unsafe", exc)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600 or details.st_nlink != 1:
        _fail("store_unsafe")


def _private_parent(path: Path) -> None:
    try:
        details = path.parent.lstat()
    except OSError as exc:
        _fail("store_unsafe", exc)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) & 0o022
    ):
        _fail("store_unsafe")


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    _leaf(path, missing_code="schema_missing")
    target = str(path) if write else path.as_uri() + "?mode=ro"
    connection = sqlite3.connect(target, uri=not write, isolation_level=None, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        if not write:
            connection.execute("PRAGMA query_only=ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _validate_schema(connection: sqlite3.Connection) -> None:
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            _fail("future_schema")
        if version != SCHEMA_VERSION:
            _fail("schema_fingerprint_mismatch")
        digest = connection.execute("SELECT value FROM event_schema WHERE key='schema_digest'").fetchone()
        count = connection.execute("SELECT COUNT(*) FROM event_schema").fetchone()
        if (
            digest is None
            or count is None
            or int(count[0]) != 1
            or str(digest[0]) != _SCHEMA_DIGEST
            or _schema_objects(connection) != _EXPECTED_SCHEMA_OBJECTS
        ):
            _fail("schema_fingerprint_mismatch")
    except EventStoreError:
        raise
    except (sqlite3.Error, IndexError, KeyError, TypeError, ValueError) as exc:
        _fail("schema_fingerprint_mismatch", exc)


def _record(row: sqlite3.Row) -> EventRecord:
    try:
        return EventRecord(
            row["event_id"], row["event_type"], int(row["event_version"]), row["project_id"],
            row["workspace_id"], row["aggregate_type"], row["aggregate_id"], int(row["aggregate_version"]),
            json.loads(row["actor_json"]), row["source_type"], row["source_event_id"], row["correlation_id"],
            row["causation_id"], row["occurred_at"], row["recorded_at"], int(row["cursor"]),
            json.loads(row["payload_json"]), tuple(json.loads(row["receipt_refs_json"])),
        )
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _fail("store_corrupt", exc)


def _materialize(row: sqlite3.Row) -> EventRecord:
    try:
        return _record(row)
    except EventStoreError:
        raise
    except Exception as exc:
        _fail("store_corrupt", exc)


class EventStore:
    def __init__(self, path: Path):
        self.path = _path(path)

    def close(self) -> None:
        pass

    def append(self, value: Mapping[str, Any]) -> EventRecord:
        event = _input(value)
        digest = hashlib.sha256(_canonical(event).encode("ascii")).hexdigest()
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect(self.path, write=True)
            _validate_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM events WHERE source_type=? AND source_event_id=?",
                (event["source"]["type"], event["source"]["source_event_id"]),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != digest:
                    _fail("event_dedup_conflict")
                connection.execute("COMMIT")
                return _materialize(existing)
            recorded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            connection.execute(
                "INSERT INTO events (event_id,event_type,event_version,project_id,workspace_id,aggregate_type,aggregate_id,aggregate_version,actor_json,source_type,source_event_id,correlation_id,causation_id,occurred_at,recorded_at,payload_json,receipt_refs_json,request_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event["event_id"], event["event_type"], event["event_version"], event["project_id"], event["workspace_id"], event["aggregate_type"], event["aggregate_id"], event["aggregate_version"], _canonical(event["actor"]), event["source"]["type"], event["source"]["source_event_id"], event["correlation_id"], event["causation_id"], event["occurred_at"], recorded_at, _canonical(event["payload"]), _canonical(event["receipt_refs"]), digest),
            )
            row = connection.execute("SELECT * FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
            connection.execute("COMMIT")
            return _materialize(row)
        except EventStoreError:
            _rollback(connection)
            raise
        except sqlite3.IntegrityError as exc:
            _rollback(connection)
            _fail("event_dedup_conflict", exc)
        except sqlite3.Error as exc:
            _rollback(connection)
            _fail("store_write_failed", exc)
        finally:
            if connection is not None:
                connection.close()

    def get(self, event_id: str) -> EventRecord | None:
        _text(event_id)
        return self._read_one("SELECT * FROM events WHERE event_id=?", (event_id,))

    def list(self, *, project_id: str, workspace_id: str | None = None, after_cursor: int = 0, types: tuple[str, ...] = (), limit: int = 50) -> tuple[tuple[EventRecord, ...], int | None]:
        _text(project_id)
        _text(workspace_id, nullable=True)
        if type(after_cursor) is not int or after_cursor < 0 or type(limit) is not int or not 1 <= limit <= 100 or not isinstance(types, tuple):
            _fail("invalid_argument")
        if any(_text(value) is None for value in types) or len(set(types)) != len(types):
            _fail("invalid_argument")
        clauses = ["project_id=?", "cursor>?"]
        params: list[Any] = [project_id, after_cursor]
        if workspace_id is not None:
            clauses.append("workspace_id=?")
            params.append(workspace_id)
        if types:
            clauses.append("event_type IN (" + ",".join("?" for _ in types) + ")")
            params.extend(types)
        rows = self._read_many("SELECT * FROM events WHERE " + " AND ".join(clauses) + " ORDER BY cursor LIMIT ?", tuple(params + [limit + 1]))
        visible = rows[:limit]
        return visible, visible[-1].cursor if len(rows) > limit else None

    def _read_one(self, sql: str, params: tuple[Any, ...]) -> EventRecord | None:
        rows = self._read_many(sql, params)
        return rows[0] if rows else None

    def _read_many(self, sql: str, params: tuple[Any, ...]) -> tuple[EventRecord, ...]:
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect(self.path, write=False)
            _validate_schema(connection)
            connection.execute("BEGIN")
            return tuple(_materialize(row) for row in connection.execute(sql, params).fetchall())
        except EventStoreError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()


def initialize(path: Path) -> EventStore:
    path = _path(path)
    if path.exists():
        return open_existing(path)
    _private_parent(path)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)
        _leaf(path, missing_code="schema_missing")
        connection = _connect(path, write=True)
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;" + _SCHEMA +
                "INSERT INTO event_schema VALUES ('schema_digest', '" + _SCHEMA_DIGEST + "');"
                "PRAGMA user_version=1;COMMIT;"
            )
        finally:
            connection.close()
    except EventStoreError:
        raise
    except (OSError, sqlite3.Error) as exc:
        _fail("store_write_failed", exc)
    return open_existing(path)


def open_existing(path: Path) -> EventStore:
    store = EventStore(path)
    _private_parent(store.path)
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(store.path, write=False)
        _validate_schema(connection)
    except EventStoreError:
        raise
    except sqlite3.Error as exc:
        _fail("store_read_failed", exc)
    finally:
        if connection is not None:
            connection.close()
    return store


def _rollback(connection: sqlite3.Connection | None) -> None:
    if connection is None:
        return
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass
