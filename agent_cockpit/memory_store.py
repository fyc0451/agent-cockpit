"""Dormant, project-scoped Memory Store v1.

The store owns durable facts, candidates, decision receipts, and a local audit
timeline. It deliberately owns no HTTP authentication, Project registry,
Runtime, Operation, Domain Event, Checkpoint, or Context Pack behavior.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
MAX_JSON_BYTES = 16_384
MAX_TEXT = 2_048
MAX_IDENTIFIER = 256
MAX_SQLITE_INTEGER = 2**63 - 1

FACT_KINDS = frozenset({
    "state", "decision", "constraint", "risk", "runbook", "preference",
})
FACT_STATUSES = frozenset({"current", "stale", "conflict", "retired"})
CANDIDATE_STATUSES = frozenset({"pending", "approved", "rejected", "merged"})
DECISIONS = frozenset({"approve", "reject", "merge"})
_FORBIDDEN_JSON_KEYS = frozenset({
    "api_key", "authorization", "cookie", "credential", "credentials",
    "env", "environment", "file_body", "file_content", "hidden_reasoning",
    "message_body", "password", "passwords", "private_key", "reasoning",
    "scrollback", "secret", "secrets", "terminal_output", "terminal_scroll",
    "token", "tokens",
})
_CANONICAL_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{6})?Z"
)

_SCHEMA = """
CREATE TABLE memory_projects (
    project_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE memory_facts (
    project_id TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('state','decision','constraint','risk','runbook','preference')),
    status TEXT NOT NULL CHECK (status IN ('current','stale','conflict','retired')),
    current_version INTEGER NOT NULL CHECK (current_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, fact_key)
);
CREATE TABLE memory_fact_revisions (
    project_id TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    kind TEXT NOT NULL CHECK (kind IN ('state','decision','constraint','risk','runbook','preference')),
    status TEXT NOT NULL CHECK (status IN ('current','stale','conflict','retired')),
    summary TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_json TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    verified_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, fact_key, version)
);
CREATE TABLE memory_candidates (
    candidate_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    target_fact_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('state','decision','constraint','risk','runbook','preference')),
    summary TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_json TEXT NOT NULL,
    proposer_json TEXT NOT NULL,
    confidence REAL,
    status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','merged')),
    expected_fact_version INTEGER NOT NULL CHECK (expected_fact_version >= 0),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    request_digest TEXT NOT NULL,
    decision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX memory_candidates_project_status_id
    ON memory_candidates(project_id, status, candidate_id);
CREATE TABLE memory_candidate_decisions (
    decision_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    candidate_revision INTEGER NOT NULL CHECK (candidate_revision >= 2),
    decision TEXT NOT NULL CHECK (decision IN ('approve','reject','merge')),
    decided_by_json TEXT NOT NULL,
    expected_fact_version INTEGER NOT NULL CHECK (expected_fact_version >= 0),
    result_fact_key TEXT,
    result_fact_version INTEGER,
    result_value_digest TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES memory_candidates(candidate_id)
);
CREATE TABLE memory_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_event_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_version INTEGER NOT NULL CHECK (entity_version >= 1),
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, project_revision)
);
CREATE INDEX memory_events_project_seq ON memory_events(project_id, seq);
CREATE TABLE memory_schema (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TRIGGER memory_fact_revisions_no_update
BEFORE UPDATE ON memory_fact_revisions BEGIN
    SELECT RAISE(ABORT, 'append_only');
END;
CREATE TRIGGER memory_fact_revisions_no_delete
BEFORE DELETE ON memory_fact_revisions BEGIN
    SELECT RAISE(ABORT, 'append_only');
END;
CREATE TRIGGER memory_candidate_decisions_no_update
BEFORE UPDATE ON memory_candidate_decisions BEGIN
    SELECT RAISE(ABORT, 'append_only');
END;
CREATE TRIGGER memory_candidate_decisions_no_delete
BEFORE DELETE ON memory_candidate_decisions BEGIN
    SELECT RAISE(ABORT, 'append_only');
END;
CREATE TRIGGER memory_events_no_update
BEFORE UPDATE ON memory_events BEGIN
    SELECT RAISE(ABORT, 'append_only');
END;
CREATE TRIGGER memory_events_no_delete
BEFORE DELETE ON memory_events BEGIN
    SELECT RAISE(ABORT, 'append_only');
END;
"""
_SCHEMA_DIGEST = hashlib.sha256(_SCHEMA.encode("utf-8")).hexdigest()
_TABLES = (
    "memory_candidate_decisions", "memory_candidates", "memory_events",
    "memory_fact_revisions", "memory_facts", "memory_projects", "memory_schema",
)


class MemoryStoreError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    error = MemoryStoreError(code)
    try:
        raise error from None
    except MemoryStoreError:
        error.__cause__ = None
        error.__context__ = None
        error.__suppress_context__ = True
        raise


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _identifier(value: object, *, maximum: int = MAX_IDENTIFIER) -> str:
    if (
        type(value) is not str or not value or len(value) > maximum
        or not value.isascii()
        or any(not (char.isalnum() or char in "._-:@/") for char in value)
    ):
        _fail("invalid_argument")
    return value


def _text(value: object, *, maximum: int = MAX_TEXT) -> str:
    if (
        type(value) is not str or not value.strip() or len(value) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _fail("invalid_argument")
    return value


def _integer(value: object, *, minimum: int) -> int:
    if type(value) is not int or not minimum <= value <= MAX_SQLITE_INTEGER:
        _fail("invalid_argument")
    return value


def _timestamp(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    value = _text(value, maximum=40)
    if _CANONICAL_UTC_TIMESTAMP.fullmatch(value) is None:
        _fail("invalid_argument")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("invalid_argument")
    if parsed.tzinfo != UTC or parsed.isoformat().replace("+00:00", "Z") != value:
        _fail("invalid_argument")
    return value


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        _fail("invalid_argument")


def _json_object(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        _fail("invalid_argument")

    def inspect(item: object) -> None:
        if type(item) is dict:
            for key, child in item.items():
                if (
                    type(key) is not str or not key
                    or key.lower() in _FORBIDDEN_JSON_KEYS
                    or any(ord(char) < 32 or ord(char) == 127 for char in key)
                ):
                    _fail("invalid_argument")
                inspect(child)
        elif type(item) is list:
            for child in item:
                inspect(child)
        elif item is not None and type(item) not in {str, int, float, bool}:
            _fail("invalid_argument")
        elif type(item) is float and not math.isfinite(item):
            _fail("invalid_argument")

    inspect(value)
    encoded = _canonical(value).encode("ascii")
    if len(encoded) > MAX_JSON_BYTES:
        _fail("invalid_argument")
    return json.loads(encoded)


def _typed_ref(value: object) -> dict[str, str]:
    if type(value) is not dict or set(value) != {"type", "id"}:
        _fail("invalid_argument")
    return {
        "type": _identifier(value["type"], maximum=64),
        "id": _identifier(value["id"], maximum=512),
    }


def _kind(value: object) -> str:
    if type(value) is not str or value not in FACT_KINDS:
        _fail("invalid_argument")
    return value


def _fact_status(value: object) -> str:
    if type(value) is not str or value not in FACT_STATUSES:
        _fail("invalid_argument")
    return value


def _confidence(value: object) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float} or type(value) is bool:
        _fail("invalid_argument")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        _fail("invalid_argument")
    return result


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def _stored_digest(value: object) -> str:
    if (
        type(value) is not str or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        _fail("invalid_argument")
    return value


@dataclass(frozen=True)
class FactRecord:
    project_id: str
    fact_key: str
    kind: str
    status: str
    version: int
    summary: str
    value: Mapping[str, Any]
    source: Mapping[str, str]
    actor: Mapping[str, str]
    verified_at: str | None
    created_at: str
    updated_at: str

    def public_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id, "fact_key": self.fact_key,
            "kind": self.kind, "status": self.status, "version": self.version,
            "summary": self.summary, "value": dict(self.value),
            "source": dict(self.source), "actor": dict(self.actor),
            "verified_at": self.verified_at, "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class CandidateDecision:
    decision_id: str
    candidate_id: str
    project_id: str
    candidate_revision: int
    decision: str
    decided_by: Mapping[str, str]
    expected_fact_version: int
    result_fact_key: str | None
    result_fact_version: int | None
    result_value_digest: str | None
    created_at: str

    def public_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id, "candidate_id": self.candidate_id,
            "project_id": self.project_id,
            "candidate_revision": self.candidate_revision,
            "decision": self.decision, "decided_by": dict(self.decided_by),
            "expected_fact_version": self.expected_fact_version,
            "result_fact_key": self.result_fact_key,
            "result_fact_version": self.result_fact_version,
            "result_value_digest": self.result_value_digest,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    project_id: str
    target_fact_key: str
    kind: str
    summary: str
    value: Mapping[str, Any]
    source: Mapping[str, str]
    proposer: Mapping[str, str]
    confidence: float | None
    status: str
    expected_fact_version: int
    revision: int
    decision: CandidateDecision | None
    created_at: str
    updated_at: str

    def public_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id, "project_id": self.project_id,
            "target_fact_key": self.target_fact_key, "kind": self.kind,
            "summary": self.summary, "value": dict(self.value),
            "source": dict(self.source), "proposer": dict(self.proposer),
            "confidence": self.confidence, "status": self.status,
            "expected_fact_version": self.expected_fact_version,
            "revision": self.revision,
            "decision": self.decision.public_dict() if self.decision else None,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class MemoryEvent:
    seq: int
    memory_event_id: str
    project_id: str
    project_revision: int
    event_type: str
    entity_type: str
    entity_id: str
    entity_version: int
    summary: Mapping[str, Any]
    created_at: str

    def public_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq, "memory_event_id": self.memory_event_id,
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "event_type": self.event_type, "entity_type": self.entity_type,
            "entity_id": self.entity_id, "entity_version": self.entity_version,
            "summary": dict(self.summary), "created_at": self.created_at,
        }


@dataclass(frozen=True)
class MemorySummary:
    project_id: str
    revision: int
    current_facts: int
    stale_facts: int
    conflicts: int
    retired_facts: int
    pending_candidates: int

    def public_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id, "revision": self.revision,
            "current_facts": self.current_facts,
            "stale_facts": self.stale_facts, "conflicts": self.conflicts,
            "retired_facts": self.retired_facts,
            "pending_candidates": self.pending_candidates,
        }


def _canonical_sql(value: str | None) -> str:
    return " ".join((value or "").split()).lower()


def _schema_fingerprint(connection: sqlite3.Connection) -> tuple[object, ...]:
    objects = tuple(
        (str(kind), str(name), str(table), _canonical_sql(sql))
        for kind, name, table, sql in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
    )
    tables: list[tuple[object, ...]] = []
    for table in _TABLES:
        columns = tuple(
            tuple(row)
            for row in connection.execute(f"PRAGMA table_xinfo({table})")
        )
        indexes = tuple(
            tuple(row)
            for row in connection.execute(f"PRAGMA index_list({table})")
        )
        index_columns = tuple(
            (
                str(row[1]),
                tuple(
                    tuple(column)
                    for column in connection.execute(
                        f"PRAGMA index_xinfo({row[1]})"
                    )
                ),
            )
            for row in indexes
        )
        foreign_keys = tuple(
            tuple(row)
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        )
        tables.append((table, columns, indexes, index_columns, foreign_keys))
    return objects, tuple(tables)


def _expected_schema_fingerprint() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(_SCHEMA)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


_EXPECTED_SCHEMA_FINGERPRINT = _expected_schema_fingerprint()


def _absolute(path: Path) -> Path:
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
        except OSError:
            _fail("store_unsafe")
        if stat.S_ISLNK(details.st_mode):
            _fail("store_unsafe")
    return path


def _private_parent(path: Path) -> None:
    try:
        details = path.parent.lstat()
    except OSError:
        _fail("store_unsafe")
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) & 0o022
    ):
        _fail("store_unsafe")


def _leaf_signature(path: Path) -> tuple[int, int, int, int, int]:
    try:
        details = path.lstat()
    except FileNotFoundError:
        _fail("schema_missing")
    except OSError:
        _fail("store_unsafe")
    mode = stat.S_IMODE(details.st_mode)
    if (
        not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
        or mode != 0o600 or details.st_nlink != 1
    ):
        _fail("store_unsafe")
    return details.st_dev, details.st_ino, details.st_uid, mode, details.st_nlink


def _create_leaf(path: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    except OSError:
        _fail("store_write_failed")
    _leaf_signature(path)


def _unlink_owned_leaf(
    path: Path, expected: tuple[int, int, int, int, int],
) -> None:
    try:
        details = path.lstat()
    except OSError:
        return
    actual = (
        details.st_dev, details.st_ino, details.st_uid,
        stat.S_IMODE(details.st_mode), details.st_nlink,
    )
    if actual != expected:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    before = _leaf_signature(path)
    connection: sqlite3.Connection | None = None
    try:
        if readonly:
            connection = sqlite3.connect(
                path.as_uri() + "?mode=ro", uri=True, isolation_level=None,
                timeout=5.0,
            )
        else:
            connection = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        after = _leaf_signature(path)
        if before != after:
            connection.close()
            _fail("store_unsafe")
        return connection
    except MemoryStoreError:
        raise
    except (OSError, sqlite3.Error):
        if connection is not None:
            connection.close()
        _fail("store_read_failed" if readonly else "store_write_failed")


def _validate_schema(connection: sqlite3.Connection) -> None:
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            _fail("future_schema")
        if version != SCHEMA_VERSION:
            _fail("schema_fingerprint_mismatch")
        rows = connection.execute(
            "SELECT key,value FROM memory_schema ORDER BY key"
        ).fetchall()
        metadata = {str(row[0]): str(row[1]) for row in rows}
        if metadata != {
            "schema_digest": _SCHEMA_DIGEST,
            "schema_name": "project_memory",
            "schema_version": str(SCHEMA_VERSION),
        }:
            _fail("schema_fingerprint_mismatch")
        if _schema_fingerprint(connection) != _EXPECTED_SCHEMA_FINGERPRINT:
            _fail("schema_fingerprint_mismatch")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            _fail("store_corrupt")
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if quick is None or str(quick[0]).lower() != "ok":
            _fail("store_corrupt")
    except MemoryStoreError:
        raise
    except (IndexError, KeyError, TypeError, ValueError, sqlite3.Error):
        _fail("schema_fingerprint_mismatch")


def _initialize_connection(connection: sqlite3.Connection) -> None:
    connection.executescript(
        "BEGIN IMMEDIATE;" + _SCHEMA
        + "INSERT INTO memory_schema VALUES ('schema_digest','" + _SCHEMA_DIGEST + "');"
        + "INSERT INTO memory_schema VALUES ('schema_name','project_memory');"
        + "INSERT INTO memory_schema VALUES ('schema_version','1');"
        + "PRAGMA user_version=1;COMMIT;"
    )


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def initialize(path: Path) -> "MemoryStore":
    path = _absolute(path)
    _private_parent(path)
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        _fail("store_unsafe")
    else:
        return open_existing(path)

    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
    connection: sqlite3.Connection | None = None
    published = False
    completed = False
    published_signature: tuple[int, int, int, int, int] | None = None
    try:
        _create_leaf(temporary)
        connection = _connect(temporary, readonly=False)
        _initialize_connection(connection)
        connection.execute("BEGIN")
        _validate_schema(connection)
        connection.execute("COMMIT")
        connection.close()
        connection = None
        _fsync_file(temporary)
        published_signature = _leaf_signature(temporary)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            return open_existing(path)
        published = True
        os.unlink(temporary)
        _leaf_signature(path)
        _fsync_parent(path)
        store = open_existing(path)
        completed = True
        return store
    except MemoryStoreError:
        raise
    except (OSError, sqlite3.Error):
        _fail("store_write_failed")
    finally:
        if connection is not None:
            connection.close()
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        if published and not completed and published_signature is not None:
            _unlink_owned_leaf(path, published_signature)


def open_existing(path: Path) -> "MemoryStore":
    path = _absolute(path)
    _private_parent(path)
    connection = _connect(path, readonly=True)
    try:
        connection.execute("BEGIN")
        _validate_schema(connection)
        connection.execute("COMMIT")
    except MemoryStoreError:
        raise
    except sqlite3.Error:
        _fail("store_read_failed")
    finally:
        connection.close()
    return MemoryStore(path)


def _decode_json(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
        return _json_object(decoded)
    except MemoryStoreError:
        _fail("store_corrupt")
    except (TypeError, ValueError, json.JSONDecodeError):
        _fail("store_corrupt")


def _decode_ref(value: object) -> dict[str, str]:
    try:
        decoded = json.loads(str(value))
        return _typed_ref(decoded)
    except MemoryStoreError:
        _fail("store_corrupt")
    except (TypeError, ValueError, json.JSONDecodeError):
        _fail("store_corrupt")


def _stored(callable_: Any) -> Any:
    try:
        return callable_()
    except MemoryStoreError:
        _fail("store_corrupt")
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        _fail("store_corrupt")


def _fact(row: sqlite3.Row) -> FactRecord:
    def materialize() -> FactRecord:
        record = FactRecord(
            _identifier(row["project_id"]), _identifier(row["fact_key"]),
            _kind(row["kind"]), _fact_status(row["status"]),
            _integer(row["version"], minimum=1), _text(row["summary"]),
            _decode_json(row["value_json"]), _decode_ref(row["source_json"]),
            _decode_ref(row["actor_json"]),
            _timestamp(row["verified_at"], nullable=True),
            _timestamp(row["fact_created_at"]),
            _timestamp(row["revision_created_at"]),
        )
        if (
            _kind(row["head_kind"]) != record.kind
            or _fact_status(row["head_status"]) != record.status
            or _integer(row["head_version"], minimum=1) != record.version
        ):
            _fail("store_corrupt")
        return record

    return _stored(materialize)


def _decision(row: sqlite3.Row) -> CandidateDecision:
    return _stored(lambda: CandidateDecision(
        _identifier(row["decision_id"]), _identifier(row["candidate_id"]),
        _identifier(row["project_id"]),
        _integer(row["candidate_revision"], minimum=2),
        row["decision"] if row["decision"] in DECISIONS else _fail("store_corrupt"),
        _decode_ref(row["decided_by_json"]),
        _integer(row["expected_fact_version"], minimum=0),
        None if row["result_fact_key"] is None else _identifier(row["result_fact_key"]),
        None if row["result_fact_version"] is None else _integer(row["result_fact_version"], minimum=1),
        None if row["result_value_digest"] is None else _stored_digest(row["result_value_digest"]),
        _timestamp(row["created_at"]),
    ))


def _candidate(connection: sqlite3.Connection, row: sqlite3.Row) -> CandidateRecord:
    decision = None
    if row["decision_id"] is not None:
        decision_row = connection.execute(
            "SELECT * FROM memory_candidate_decisions WHERE decision_id=?",
            (row["decision_id"],),
        ).fetchone()
        if decision_row is None:
            _fail("store_corrupt")
        decision = _decision(decision_row)

    def materialize() -> CandidateRecord:
        status = (
            row["status"]
            if type(row["status"]) is str and row["status"] in CANDIDATE_STATUSES
            else _fail("store_corrupt")
        )
        record = CandidateRecord(
            _identifier(row["candidate_id"]), _identifier(row["project_id"]),
            _identifier(row["target_fact_key"]), _kind(row["kind"]),
            _text(row["summary"]), _decode_json(row["value_json"]),
            _decode_ref(row["source_json"]), _decode_ref(row["proposer_json"]),
            _confidence(row["confidence"]), status,
            _integer(row["expected_fact_version"], minimum=0),
            _integer(row["revision"], minimum=1), decision,
            _timestamp(row["created_at"]), _timestamp(row["updated_at"]),
        )
        request = {
            "candidate_id": record.candidate_id,
            "project_id": record.project_id,
            "target_fact_key": record.target_fact_key,
            "kind": record.kind,
            "summary": record.summary,
            "value": record.value,
            "source": record.source,
            "proposer": record.proposer,
            "expected_fact_version": record.expected_fact_version,
            "confidence": record.confidence,
        }
        if _stored_digest(row["request_digest"]) != _digest(request):
            _fail("store_corrupt")
        if status == "pending":
            if record.revision != 1 or row["decision_id"] is not None or decision is not None:
                _fail("store_corrupt")
            return record
        expected_decision = {
            "approved": "approve", "rejected": "reject", "merged": "merge",
        }[status]
        if (
            record.revision != 2 or decision is None
            or decision.decision_id != row["decision_id"]
            or decision.candidate_id != record.candidate_id
            or decision.project_id != record.project_id
            or decision.candidate_revision != record.revision
            or decision.decision != expected_decision
            or decision.expected_fact_version != record.expected_fact_version
        ):
            _fail("store_corrupt")
        if status == "rejected":
            if any((
                decision.result_fact_key is not None,
                decision.result_fact_version is not None,
                decision.result_value_digest is not None,
            )):
                _fail("store_corrupt")
            return record
        if (
            decision.result_fact_key != record.target_fact_key
            or decision.result_fact_version != record.expected_fact_version + 1
            or decision.result_value_digest is None
        ):
            _fail("store_corrupt")
        fact_row = connection.execute(
            "SELECT value_json FROM memory_fact_revisions "
            "WHERE project_id=? AND fact_key=? AND version=?",
            (
                record.project_id, record.target_fact_key,
                decision.result_fact_version,
            ),
        ).fetchone()
        if (
            fact_row is None
            or _digest(_decode_json(fact_row["value_json"]))
            != decision.result_value_digest
        ):
            _fail("store_corrupt")
        return record

    return _stored(materialize)


def _event(row: sqlite3.Row) -> MemoryEvent:
    return _stored(lambda: MemoryEvent(
        _integer(row["seq"], minimum=1), _identifier(row["memory_event_id"]),
        _identifier(row["project_id"]),
        _integer(row["project_revision"], minimum=1),
        _identifier(row["event_type"]), _identifier(row["entity_type"]),
        _identifier(row["entity_id"]),
        _integer(row["entity_version"], minimum=1),
        _decode_json(row["summary_json"]), _timestamp(row["created_at"]),
    ))


def _fact_query() -> str:
    return (
        "SELECT f.project_id,f.fact_key,f.kind AS head_kind,"
        "f.status AS head_status,f.current_version AS head_version,"
        "r.kind,r.status,r.version,r.summary,"
        "r.value_json,r.source_json,r.actor_json,r.verified_at,"
        "f.created_at AS fact_created_at,r.created_at AS revision_created_at "
        "FROM memory_facts f LEFT JOIN memory_fact_revisions r ON "
        "r.project_id=f.project_id AND r.fact_key=f.fact_key "
        "AND r.version=f.current_version "
    )


def _ensure_project(connection: sqlite3.Connection, project_id: str, now: str) -> None:
    connection.execute(
        "INSERT INTO memory_projects(project_id,revision,created_at,updated_at) "
        "VALUES (?,0,?,?) ON CONFLICT(project_id) DO NOTHING",
        (project_id, now, now),
    )


def _bump_project(connection: sqlite3.Connection, project_id: str, now: str) -> int:
    row = connection.execute(
        "SELECT revision FROM memory_projects WHERE project_id=?", (project_id,),
    ).fetchone()
    if row is None:
        _fail("store_corrupt")
    current = _stored(lambda: _integer(row[0], minimum=0))
    revision = _stored(lambda: _integer(current + 1, minimum=1))
    connection.execute(
        "UPDATE memory_projects SET revision=?,updated_at=? WHERE project_id=?",
        (revision, now, project_id),
    )
    return revision


def _append_event(
    connection: sqlite3.Connection, *, project_id: str, project_revision: int,
    event_type: str, entity_type: str, entity_id: str, entity_version: int,
    summary: Mapping[str, Any], now: str,
) -> None:
    connection.execute(
        "INSERT INTO memory_events(memory_event_id,project_id,project_revision,"
        "event_type,entity_type,entity_id,entity_version,summary_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "mev_" + secrets.token_hex(16), project_id, project_revision,
            event_type, entity_type, entity_id, entity_version,
            _canonical(_json_object(dict(summary))), now,
        ),
    )


def _current_fact_version(
    connection: sqlite3.Connection, project_id: str, fact_key: str,
) -> tuple[int, str | None]:
    row = connection.execute(
        "SELECT current_version,kind FROM memory_facts "
        "WHERE project_id=? AND fact_key=?", (project_id, fact_key),
    ).fetchone()
    if row is None:
        return 0, None
    return _stored(lambda: (
        _integer(row[0], minimum=1), _kind(row[1]),
    ))


def _append_fact_revision(
    connection: sqlite3.Connection, *, project_id: str, fact_key: str,
    kind: str, status: str, summary: str, value: Mapping[str, Any],
    source: Mapping[str, str], actor: Mapping[str, str],
    verified_at: str | None, expected_version: int, now: str,
) -> FactRecord:
    actual, existing_kind = _current_fact_version(connection, project_id, fact_key)
    if actual != expected_version:
        _fail("fact_version_conflict")
    if existing_kind is not None and existing_kind != kind:
        _fail("fact_kind_conflict")
    version = actual + 1
    if actual == 0:
        connection.execute(
            "INSERT INTO memory_facts(project_id,fact_key,kind,status,current_version,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (project_id, fact_key, kind, status, version, now, now),
        )
    connection.execute(
        "INSERT INTO memory_fact_revisions(project_id,fact_key,version,kind,status,"
        "summary,value_json,source_json,actor_json,verified_at,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            project_id, fact_key, version, kind, status, summary,
            _canonical(dict(value)), _canonical(dict(source)),
            _canonical(dict(actor)), verified_at, now,
        ),
    )
    if actual != 0:
        connection.execute(
            "UPDATE memory_facts SET status=?,current_version=?,updated_at=? "
            "WHERE project_id=? AND fact_key=?",
            (status, version, now, project_id, fact_key),
        )
    row = connection.execute(
        _fact_query() + "WHERE f.project_id=? AND f.fact_key=?",
        (project_id, fact_key),
    ).fetchone()
    if row is None:
        _fail("store_corrupt")
    return _fact(row)


class MemoryStore:
    def __init__(self, path: Path):
        self.path = _absolute(path)

    def close(self) -> None:
        """The store owns no persistent connection."""

    def _read(self, operation: Any) -> Any:
        connection = _connect(self.path, readonly=True)
        try:
            connection.execute("BEGIN")
            _validate_schema(connection)
            result = operation(connection)
            connection.execute("COMMIT")
            return result
        except MemoryStoreError:
            raise
        except sqlite3.Error:
            _fail("store_read_failed")
        finally:
            connection.close()

    def _write(self, operation: Any) -> Any:
        connection = _connect(self.path, readonly=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            _validate_schema(connection)
            result = operation(connection)
            connection.execute("COMMIT")
            return result
        except MemoryStoreError:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        except (OverflowError, sqlite3.Error):
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            _fail("store_write_failed")
        finally:
            connection.close()

    def append_fact(
        self, *, project_id: str, fact_key: str, kind: str, summary: str,
        value: Mapping[str, Any], source: Mapping[str, str],
        actor: Mapping[str, str], expected_version: int,
        status: str = "current", verified_at: str | None = None,
    ) -> FactRecord:
        project_id = _identifier(project_id)
        fact_key = _identifier(fact_key)
        kind = _kind(kind)
        status = _fact_status(status)
        summary = _text(summary)
        value = _json_object(value)
        source = _typed_ref(source)
        actor = _typed_ref(actor)
        expected_version = _integer(expected_version, minimum=0)
        verified_at = _timestamp(verified_at, nullable=True)

        def write(connection: sqlite3.Connection) -> FactRecord:
            now = _now()
            _ensure_project(connection, project_id, now)
            record = _append_fact_revision(
                connection, project_id=project_id, fact_key=fact_key, kind=kind,
                status=status, summary=summary, value=value, source=source,
                actor=actor, verified_at=verified_at,
                expected_version=expected_version, now=now,
            )
            project_revision = _bump_project(connection, project_id, now)
            _append_event(
                connection, project_id=project_id,
                project_revision=project_revision,
                event_type="memory.fact.revised", entity_type="fact",
                entity_id=fact_key, entity_version=record.version,
                summary={
                    "fact_key": fact_key, "status": status,
                    "version": record.version,
                }, now=now,
            )
            return record

        return self._write(write)

    def create_candidate(
        self, *, candidate_id: str, project_id: str, target_fact_key: str,
        kind: str, summary: str, value: Mapping[str, Any],
        source: Mapping[str, str], proposer: Mapping[str, str],
        expected_fact_version: int, confidence: float | None = None,
    ) -> CandidateRecord:
        candidate_id = _identifier(candidate_id)
        project_id = _identifier(project_id)
        target_fact_key = _identifier(target_fact_key)
        kind = _kind(kind)
        summary = _text(summary)
        value = _json_object(value)
        source = _typed_ref(source)
        proposer = _typed_ref(proposer)
        expected_fact_version = _integer(expected_fact_version, minimum=0)
        confidence = _confidence(confidence)
        request = {
            "candidate_id": candidate_id, "project_id": project_id,
            "target_fact_key": target_fact_key, "kind": kind,
            "summary": summary, "value": value, "source": source,
            "proposer": proposer,
            "expected_fact_version": expected_fact_version,
            "confidence": confidence,
        }
        request_digest = _digest(request)

        def write(connection: sqlite3.Connection) -> CandidateRecord:
            existing = connection.execute(
                "SELECT * FROM memory_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if existing is not None:
                existing_candidate = _candidate(connection, existing)
                if _stored_digest(existing["request_digest"]) != request_digest:
                    _fail("idempotency_conflict")
                return existing_candidate
            actual, existing_kind = _current_fact_version(
                connection, project_id, target_fact_key,
            )
            if actual != expected_fact_version:
                _fail("fact_version_conflict")
            if existing_kind is not None and existing_kind != kind:
                _fail("fact_kind_conflict")
            now = _now()
            _ensure_project(connection, project_id, now)
            connection.execute(
                "INSERT INTO memory_candidates(candidate_id,project_id,target_fact_key,"
                "kind,summary,value_json,source_json,proposer_json,confidence,status,"
                "expected_fact_version,revision,request_digest,decision_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,1,?,NULL,?,?)",
                (
                    candidate_id, project_id, target_fact_key, kind, summary,
                    _canonical(value), _canonical(source), _canonical(proposer),
                    confidence, expected_fact_version, request_digest, now, now,
                ),
            )
            project_revision = _bump_project(connection, project_id, now)
            _append_event(
                connection, project_id=project_id,
                project_revision=project_revision,
                event_type="memory.candidate.created", entity_type="candidate",
                entity_id=candidate_id, entity_version=1,
                summary={
                    "candidate_id": candidate_id,
                    "target_fact_key": target_fact_key,
                    "expected_fact_version": expected_fact_version,
                }, now=now,
            )
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            return _candidate(connection, row)

        return self._write(write)

    def decide_candidate(
        self, *, project_id: str, candidate_id: str, decision: str,
        expected_candidate_revision: int, expected_fact_version: int,
        decided_by: Mapping[str, str], merged_summary: str | None = None,
        merged_value: Mapping[str, Any] | None = None,
    ) -> CandidateRecord:
        project_id = _identifier(project_id)
        candidate_id = _identifier(candidate_id)
        if type(decision) is not str or decision not in DECISIONS:
            _fail("invalid_argument")
        expected_candidate_revision = _integer(
            expected_candidate_revision, minimum=1,
        )
        expected_fact_version = _integer(expected_fact_version, minimum=0)
        decided_by = _typed_ref(decided_by)
        if decision == "merge":
            merged_summary = _text(merged_summary)
            merged_value = _json_object(merged_value)
        elif merged_summary is not None or merged_value is not None:
            _fail("invalid_argument")

        def write(connection: sqlite3.Connection) -> CandidateRecord:
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE candidate_id=? AND project_id=?",
                (candidate_id, project_id),
            ).fetchone()
            if row is None:
                _fail("candidate_not_found")
            candidate = _candidate(connection, row)
            if candidate.revision != expected_candidate_revision:
                _fail("candidate_version_conflict")
            if candidate.status != "pending":
                _fail("candidate_state_conflict")
            actual_fact_version, existing_kind = _current_fact_version(
                connection, project_id, candidate.target_fact_key,
            )
            if actual_fact_version != expected_fact_version:
                _fail("fact_version_conflict")
            if candidate.expected_fact_version != expected_fact_version:
                _fail("fact_version_conflict")
            if decision in {"approve", "merge"}:
                if existing_kind is not None and existing_kind != candidate.kind:
                    _fail("fact_kind_conflict")

            now = _now()
            fact: FactRecord | None = None
            if decision in {"approve", "merge"}:
                chosen_summary = (
                    candidate.summary if decision == "approve" else merged_summary
                )
                chosen_value = (
                    candidate.value if decision == "approve" else merged_value
                )
                fact = _append_fact_revision(
                    connection, project_id=project_id,
                    fact_key=candidate.target_fact_key, kind=candidate.kind,
                    status="current", summary=chosen_summary, value=chosen_value,
                    source=candidate.source, actor=decided_by, verified_at=now,
                    expected_version=expected_fact_version, now=now,
                )
            new_status = {
                "approve": "approved", "reject": "rejected", "merge": "merged",
            }[decision]
            new_candidate_revision = candidate.revision + 1
            decision_id = "mdc_" + secrets.token_hex(16)
            result_digest = _digest(fact.value) if fact is not None else None
            connection.execute(
                "UPDATE memory_candidates SET status=?,revision=?,decision_id=?,updated_at=? "
                "WHERE candidate_id=? AND project_id=? AND revision=? AND status='pending'",
                (
                    new_status, new_candidate_revision, decision_id, now,
                    candidate_id, project_id, expected_candidate_revision,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                _fail("candidate_version_conflict")
            connection.execute(
                "INSERT INTO memory_candidate_decisions(decision_id,candidate_id,project_id,"
                "candidate_revision,decision,decided_by_json,expected_fact_version,"
                "result_fact_key,result_fact_version,result_value_digest,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id, candidate_id, project_id, new_candidate_revision,
                    decision, _canonical(decided_by), expected_fact_version,
                    fact.fact_key if fact else None, fact.version if fact else None,
                    result_digest, now,
                ),
            )
            project_revision = _bump_project(connection, project_id, now)
            _append_event(
                connection, project_id=project_id,
                project_revision=project_revision,
                event_type=f"memory.candidate.{new_status}",
                entity_type="candidate", entity_id=candidate_id,
                entity_version=new_candidate_revision,
                summary={
                    "candidate_id": candidate_id, "decision_id": decision_id,
                    "decision": decision,
                    "result_fact_key": fact.fact_key if fact else None,
                    "result_fact_version": fact.version if fact else None,
                }, now=now,
            )
            updated = connection.execute(
                "SELECT * FROM memory_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            return _candidate(connection, updated)

        return self._write(write)

    def summary(self, project_id: str) -> MemorySummary:
        project_id = _identifier(project_id)

        def read(connection: sqlite3.Connection) -> MemorySummary:
            def materialize() -> MemorySummary:
                project = connection.execute(
                    "SELECT revision FROM memory_projects WHERE project_id=?",
                    (project_id,),
                ).fetchone()
                facts = {
                    _fact_status(row[0]): _integer(row[1], minimum=0)
                    for row in connection.execute(
                        "SELECT status,COUNT(*) FROM memory_facts WHERE project_id=? "
                        "GROUP BY status", (project_id,),
                    ).fetchall()
                }
                pending_row = connection.execute(
                    "SELECT COUNT(*) FROM memory_candidates "
                    "WHERE project_id=? AND status='pending'", (project_id,),
                ).fetchone()
                if pending_row is None:
                    _fail("store_corrupt")
                pending = _integer(pending_row[0], minimum=0)
                return MemorySummary(
                    project_id, 0 if project is None else _integer(project[0], minimum=0),
                    facts.get("current", 0), facts.get("stale", 0),
                    facts.get("conflict", 0), facts.get("retired", 0), pending,
                )

            return _stored(materialize)

        return self._read(read)

    def list_facts(
        self, *, project_id: str, statuses: tuple[str, ...] = (),
        after_key: str | None = None, limit: int = 50,
    ) -> tuple[tuple[FactRecord, ...], str | None]:
        project_id = _identifier(project_id)
        if type(statuses) is not tuple or any(
            type(item) is not str or item not in FACT_STATUSES for item in statuses
        ) or len(set(statuses)) != len(statuses):
            _fail("invalid_argument")
        if after_key is not None:
            after_key = _identifier(after_key)
        limit = _integer(limit, minimum=1)
        if limit > 100:
            _fail("invalid_argument")

        def read(connection: sqlite3.Connection) -> tuple[tuple[FactRecord, ...], str | None]:
            clauses = ["f.project_id=?", "f.fact_key>?"]
            params: list[object] = [project_id, after_key or ""]
            if statuses:
                clauses.append("f.status IN (" + ",".join("?" for _ in statuses) + ")")
                params.extend(statuses)
            rows = connection.execute(
                _fact_query() + "WHERE " + " AND ".join(clauses)
                + " ORDER BY f.fact_key LIMIT ?", tuple(params + [limit + 1]),
            ).fetchall()
            visible = rows[:limit]
            records = tuple(_fact(row) for row in visible)
            return records, records[-1].fact_key if len(rows) > limit else None

        return self._read(read)

    def list_candidates(
        self, *, project_id: str, statuses: tuple[str, ...] = (),
        after_candidate_id: str | None = None, limit: int = 50,
    ) -> tuple[tuple[CandidateRecord, ...], str | None]:
        project_id = _identifier(project_id)
        if type(statuses) is not tuple or any(
            type(item) is not str or item not in CANDIDATE_STATUSES for item in statuses
        ) or len(set(statuses)) != len(statuses):
            _fail("invalid_argument")
        if after_candidate_id is not None:
            after_candidate_id = _identifier(after_candidate_id)
        limit = _integer(limit, minimum=1)
        if limit > 100:
            _fail("invalid_argument")

        def read(connection: sqlite3.Connection) -> tuple[tuple[CandidateRecord, ...], str | None]:
            clauses = ["project_id=?", "candidate_id>?"]
            params: list[object] = [project_id, after_candidate_id or ""]
            if statuses:
                clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
                params.extend(statuses)
            rows = connection.execute(
                "SELECT * FROM memory_candidates WHERE " + " AND ".join(clauses)
                + " ORDER BY candidate_id LIMIT ?", tuple(params + [limit + 1]),
            ).fetchall()
            visible = rows[:limit]
            records = tuple(_candidate(connection, row) for row in visible)
            return (
                records,
                records[-1].candidate_id if len(rows) > limit else None,
            )

        return self._read(read)

    def timeline(
        self, *, project_id: str, after_seq: int = 0, limit: int = 50,
    ) -> tuple[tuple[MemoryEvent, ...], int | None]:
        project_id = _identifier(project_id)
        after_seq = _integer(after_seq, minimum=0)
        limit = _integer(limit, minimum=1)
        if limit > 100:
            _fail("invalid_argument")

        def read(connection: sqlite3.Connection) -> tuple[tuple[MemoryEvent, ...], int | None]:
            rows = connection.execute(
                "SELECT * FROM memory_events WHERE project_id=? AND seq>? "
                "ORDER BY seq LIMIT ?", (project_id, after_seq, limit + 1),
            ).fetchall()
            visible = rows[:limit]
            events = tuple(_event(row) for row in visible)
            return events, events[-1].seq if len(rows) > limit else None

        return self._read(read)
