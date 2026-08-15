"""Independent workspace-work.sqlite3: one Boss root Message + WorkItem."""
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


SCHEMA_VERSION = 1
MIGRATION_ID = "workspace-work-v1"
CREATE_SCOPE = "workspace-work.create.v1"
BODY_MAX = 32768
NOTE_MAX = 8192
AUTHOR_KIND = "boss"
STATUS = "unassigned"

_SCHEMA = (
    """CREATE TABLE schema_migrations (
        migration_id TEXT PRIMARY KEY,
        schema_version INTEGER NOT NULL,
        schema_digest TEXT NOT NULL,
        applied_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TRIGGER schema_migrations_no_update
        BEFORE UPDATE ON schema_migrations
        BEGIN SELECT RAISE(ABORT, 'append_only'); END""",
    """CREATE TRIGGER schema_migrations_no_delete
        BEFORE DELETE ON schema_migrations
        BEGIN SELECT RAISE(ABORT, 'append_only'); END""",
    """CREATE TABLE message_threads (
        thread_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE messages (
        message_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES message_threads(thread_id),
        author_kind TEXT NOT NULL CHECK(author_kind = 'boss'),
        author_ref TEXT,
        body TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE work_items (
        work_item_id TEXT PRIMARY KEY,
        source_message_id TEXT NOT NULL UNIQUE REFERENCES messages(message_id),
        status TEXT NOT NULL CHECK(status = 'unassigned'),
        acceptance TEXT,
        constraints TEXT
    ) STRICT""",
    """CREATE TABLE idempotency_records (
        project_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        response_json TEXT NOT NULL,
        PRIMARY KEY(project_id, workspace_id, idempotency_key)
    ) STRICT""",
    """CREATE TRIGGER idempotency_records_no_update
        BEFORE UPDATE ON idempotency_records
        BEGIN SELECT RAISE(ABORT, 'append_only'); END""",
    """CREATE TRIGGER idempotency_records_no_delete
        BEFORE DELETE ON idempotency_records
        BEGIN SELECT RAISE(ABORT, 'append_only'); END""",
)


def _canonical_sql(value: str | None) -> str:
    return " ".join((value or "").split()).lower()


def _schema_objects(connection: sqlite3.Connection) -> tuple[tuple[str, ...], ...]:
    return tuple(
        (str(kind), str(name), str(table), _canonical_sql(sql))
        for kind, name, table, sql in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )


def _expected_schema_objects() -> tuple[tuple[str, ...], ...]:
    memory = sqlite3.connect(":memory:")
    try:
        memory.execute("PRAGMA foreign_keys=ON")
        for statement in _SCHEMA:
            memory.execute(statement)
        return _schema_objects(memory)
    finally:
        memory.close()


_EXPECTED = _expected_schema_objects()
SCHEMA_DIGEST = "sha256:" + hashlib.sha256(
    json.dumps(_EXPECTED, separators=(",", ":")).encode()
).hexdigest()


class WorkspaceWorkError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str, cause: BaseException | None = None) -> None:
    error = WorkspaceWorkError(code)
    if cause is None:
        raise error
    try:
        raise error from None
    except WorkspaceWorkError:
        error.__cause__ = error.__context__ = None
        error.__suppress_context__ = True
        raise


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(16)


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        _fail("invalid_argument", exc)
    raise AssertionError("unreachable")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _opaque(value: object, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or len(value) != len(prefix) + 32
        or any(char not in "0123456789abcdef" for char in value[len(prefix):])
    ):
        _fail("invalid_argument")
    return value


def _idempotency_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        _fail("invalid_argument")
    return value


def _has_illegal_control(value: str) -> bool:
    return any(
        (ord(char) < 32 and char not in "\t\n\r") or ord(char) == 127
        for char in value
    )


def body_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > BODY_MAX:
        _fail("invalid_argument")
    if _has_illegal_control(value):
        _fail("invalid_argument")
    return value


def note_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > NOTE_MAX:
        _fail("invalid_argument")
    if _has_illegal_control(value):
        _fail("invalid_argument")
    return value


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
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        _fail("store_unsafe")


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    _leaf(path, missing="workspace_work_schema_missing")
    uri = path.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(
            str(path) if write else uri,
            uri=not write,
            isolation_level=None,
            timeout=5.0,
        )
    except sqlite3.DatabaseError as exc:
        _fail("store_corrupt", exc)
    except sqlite3.Error as exc:
        _fail("store_write_failed" if write else "store_read_failed", exc)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        if not write:
            connection.execute("PRAGMA query_only=ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _require_current_schema(connection: sqlite3.Connection) -> None:
    try:
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='schema_migrations'"
        ).fetchone()
        if present is None:
            _fail("workspace_work_schema_missing")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < SCHEMA_VERSION:
            _fail("migration_required")
        if version > SCHEMA_VERSION:
            _fail("future_schema")
        rows = connection.execute(
            "SELECT migration_id, schema_version, schema_digest "
            "FROM schema_migrations"
        ).fetchall()
        if len(rows) != 1 or tuple(rows[0]) != (
            MIGRATION_ID, SCHEMA_VERSION, SCHEMA_DIGEST,
        ):
            _fail("schema_fingerprint_mismatch")
    except WorkspaceWorkError:
        raise
    except sqlite3.DatabaseError as exc:
        _fail("store_corrupt", exc)
    except sqlite3.Error as exc:
        _fail("store_read_failed", exc)


def _validate_schema(connection: sqlite3.Connection) -> None:
    _require_current_schema(connection)
    try:
        if _schema_objects(connection) != _EXPECTED:
            _fail("schema_fingerprint_mismatch")
    except WorkspaceWorkError:
        raise
    except sqlite3.DatabaseError as exc:
        _fail("store_corrupt", exc)
    except sqlite3.Error as exc:
        _fail("store_read_failed", exc)


def _after_thread_insert(_connection: sqlite3.Connection) -> None:
    return None


def _after_root_message_insert(_connection: sqlite3.Connection) -> None:
    return None


def _after_work_item_insert(_connection: sqlite3.Connection) -> None:
    return None


def _after_receipt_insert(_connection: sqlite3.Connection) -> None:
    return None


@dataclass(frozen=True)
class WorkItemAggregate:
    thread: dict[str, object]
    root_message: dict[str, object]
    work_item: dict[str, object]

    def public_dict(self) -> dict[str, object]:
        return {
            "thread": dict(self.thread),
            "root_message": dict(self.root_message),
            "work_item": dict(self.work_item),
        }


@dataclass(frozen=True)
class CommandResult:
    status_code: int
    item: WorkItemAggregate


def _row_to_aggregate(row: sqlite3.Row) -> WorkItemAggregate:
    return WorkItemAggregate(
        {
            "thread_id": row["thread_id"],
            "project_id": row["project_id"],
            "workspace_id": row["workspace_id"],
            "revision": int(row["revision"]),
            "created_at": row["created_at"],
        },
        {
            "message_id": row["message_id"],
            "thread_id": row["message_thread_id"],
            "author_kind": row["author_kind"],
            "author_ref": row["author_ref"],
            "body": row["body"],
        },
        {
            "work_item_id": row["work_item_id"],
            "source_message_id": row["source_message_id"],
            "status": row["status"],
            "acceptance": row["acceptance"],
            "constraints": row["constraints"],
        },
    )


_LIST_SQL = """
SELECT
    t.thread_id AS thread_id,
    t.project_id AS project_id,
    t.workspace_id AS workspace_id,
    t.revision AS revision,
    t.created_at AS created_at,
    m.message_id AS message_id,
    m.thread_id AS message_thread_id,
    m.author_kind AS author_kind,
    m.author_ref AS author_ref,
    m.body AS body,
    w.work_item_id AS work_item_id,
    w.source_message_id AS source_message_id,
    w.status AS status,
    w.acceptance AS acceptance,
    w.constraints AS constraints
FROM work_items w
JOIN messages m ON m.message_id = w.source_message_id
JOIN message_threads t ON t.thread_id = m.thread_id
WHERE t.project_id = ? AND t.workspace_id = ?
ORDER BY t.created_at, t.thread_id
"""

DOMAIN_TABLES = (
    "message_threads", "messages", "work_items", "idempotency_records",
)


class WorkspaceWorkStore:
    def __init__(self, path: Path):
        self.path = _path(path)

    def close(self) -> None:
        return None

    def create_work_item(
        self, *, project_id: str, workspace_id: str, body: object,
        acceptance: object, constraints: object, idempotency_key: object,
    ) -> CommandResult:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        body = body_text(body)
        acceptance = note_text(acceptance)
        constraints = note_text(constraints)
        idempotency_key = _idempotency_key(idempotency_key)
        request_digest = _digest({
            "acceptance": acceptance,
            "body": body,
            "constraints": constraints,
            "project_id": project_id,
            "workspace_id": workspace_id,
        })
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect(self.path, write=True)
            _require_current_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_digest, response_json FROM idempotency_records "
                "WHERE project_id=? AND workspace_id=? AND idempotency_key=?",
                (project_id, workspace_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    _fail("idempotency_conflict")
                stored = json.loads(existing["response_json"])
                connection.execute("COMMIT")
                return CommandResult(201, WorkItemAggregate(
                    stored["thread"], stored["root_message"], stored["work_item"],
                ))
            thread_id = _new_id("thr_")
            message_id = _new_id("msg_")
            work_item_id = _new_id("wrk_")
            created_at = _now()
            connection.execute(
                "INSERT INTO message_threads VALUES (?,?,?,1,?)",
                (thread_id, project_id, workspace_id, created_at),
            )
            _after_thread_insert(connection)
            connection.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?)",
                (message_id, thread_id, AUTHOR_KIND, None, body),
            )
            _after_root_message_insert(connection)
            connection.execute(
                "INSERT INTO work_items VALUES (?,?,?,?,?)",
                (work_item_id, message_id, STATUS, acceptance, constraints),
            )
            _after_work_item_insert(connection)
            item = WorkItemAggregate(
                {
                    "thread_id": thread_id,
                    "project_id": project_id,
                    "workspace_id": workspace_id,
                    "revision": 1,
                    "created_at": created_at,
                },
                {
                    "message_id": message_id,
                    "thread_id": thread_id,
                    "author_kind": AUTHOR_KIND,
                    "author_ref": None,
                    "body": body,
                },
                {
                    "work_item_id": work_item_id,
                    "source_message_id": message_id,
                    "status": STATUS,
                    "acceptance": acceptance,
                    "constraints": constraints,
                },
            )
            connection.execute(
                "INSERT INTO idempotency_records VALUES (?,?,?,?,?)",
                (
                    project_id, workspace_id, idempotency_key, request_digest,
                    _canonical(item.public_dict()),
                ),
            )
            _after_receipt_insert(connection)
            connection.execute("COMMIT")
            return CommandResult(201, item)
        except WorkspaceWorkError:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            _fail("store_write_failed", exc)
        finally:
            if connection is not None:
                connection.close()
        raise AssertionError("unreachable")

    def list_work_items(
        self, *, project_id: str, workspace_id: str,
    ) -> tuple[WorkItemAggregate, ...]:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect(self.path, write=False)
            _require_current_schema(connection)
            connection.execute("BEGIN")
            rows = connection.execute(
                _LIST_SQL, (project_id, workspace_id),
            ).fetchall()
            return tuple(_row_to_aggregate(row) for row in rows)
        except WorkspaceWorkError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()

    def get_work_item(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
    ) -> WorkItemAggregate | None:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect(self.path, write=False)
            _require_current_schema(connection)
            connection.execute("BEGIN")
            row = connection.execute(
                _LIST_SQL.replace(
                    "WHERE t.project_id = ? AND t.workspace_id = ?",
                    "WHERE t.project_id = ? AND t.workspace_id = ? "
                    "AND w.work_item_id = ?",
                ),
                (project_id, workspace_id, work_item_id),
            ).fetchone()
            return None if row is None else _row_to_aggregate(row)
        except WorkspaceWorkError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()


def initialize(path: Path) -> WorkspaceWorkStore:
    path = _path(path)
    if path.exists():
        return open_existing(path)
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        _fail("store_unsafe", exc)
    if not stat.S_ISDIR(parent.st_mode) or path.parent.is_symlink():
        _fail("store_unsafe")
    connection: sqlite3.Connection | None = None
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        connection = _connect(path, write=True)
        connection.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?,?,?,?)",
            (MIGRATION_ID, SCHEMA_VERSION, SCHEMA_DIGEST, _now()),
        )
        _validate_schema(connection)
        connection.execute("COMMIT")
    except WorkspaceWorkError:
        if connection is not None and connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    except (OSError, sqlite3.Error) as exc:
        if connection is not None and connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        _fail("store_write_failed", exc)
    finally:
        if connection is not None:
            connection.close()
    return WorkspaceWorkStore(path)


def open_existing(path: Path) -> WorkspaceWorkStore:
    path = _path(path)
    _leaf(path, missing="workspace_work_schema_missing")
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(path, write=False)
        _validate_schema(connection)
    except sqlite3.DatabaseError as exc:
        _fail("store_corrupt", exc)
    finally:
        if connection is not None:
            connection.close()
    return WorkspaceWorkStore(path)
