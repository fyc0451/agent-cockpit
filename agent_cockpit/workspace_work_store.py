"""Independent workspace-work.sqlite3: Boss root, claim, ordered agent reply."""
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
from typing import Callable


SCHEMA_VERSION = 2
V1_SCHEMA_VERSION = 1
V1_MIGRATION_ID = "workspace-work-v1"
MIGRATION_ID = "workspace-work-v2"
CREATE_SCOPE = "workspace-work.create.v1"
RESERVE_SCOPE = "workspace-work.claim.reserve.v1"
ACTIVATE_SCOPE = "workspace-work.claim.activate.v1"
REPLY_SCOPE = "workspace-work.reply.complete.v1"
DELIVERY_SCOPE = "workspace-work.delivery.v1"
BODY_MAX = 32768
NOTE_MAX = 8192
AUTHOR_KIND = "boss"
STATUS = "unassigned"

V1_SCHEMA = (
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
        ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
        message_kind TEXT NOT NULL CHECK(message_kind IN ('root','reply')),
        author_kind TEXT NOT NULL CHECK(author_kind IN ('boss','agent')),
        author_ref TEXT,
        author_generation INTEGER,
        reply_to_message_id TEXT REFERENCES messages(message_id),
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(thread_id, ordinal)
    ) STRICT""",
    """CREATE UNIQUE INDEX messages_one_root
        ON messages(thread_id) WHERE message_kind = 'root'""",
    """CREATE TABLE work_items (
        work_item_id TEXT PRIMARY KEY,
        source_message_id TEXT NOT NULL UNIQUE REFERENCES messages(message_id),
        status TEXT NOT NULL CHECK(status IN (
            'unassigned','working','completed','failed'
        )),
        acceptance TEXT,
        constraints TEXT,
        revision INTEGER NOT NULL CHECK(revision >= 1),
        updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE work_item_claims (
        claim_id TEXT PRIMARY KEY,
        work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
        identity_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK(generation >= 1),
        state TEXT NOT NULL CHECK(state IN (
            'pending_gate','active','closed'
        )),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE UNIQUE INDEX work_item_claims_one_current
        ON work_item_claims(work_item_id)
        WHERE state IN ('pending_gate','active')""",
    """CREATE TABLE message_receipts (
        receipt_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
        claim_id TEXT REFERENCES work_item_claims(claim_id),
        message_id TEXT REFERENCES messages(message_id),
        identity_id TEXT,
        generation INTEGER,
        kind TEXT NOT NULL CHECK(kind IN (
            'delivery','claim','reply','complete','failure'
        )),
        outcome TEXT NOT NULL,
        reason TEXT,
        evidence_digest TEXT,
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TRIGGER message_receipts_no_update
        BEFORE UPDATE ON message_receipts
        BEGIN SELECT RAISE(ABORT, 'append_only'); END""",
    """CREATE TRIGGER message_receipts_no_delete
        BEFORE DELETE ON message_receipts
        BEGIN SELECT RAISE(ABORT, 'append_only'); END""",
    """CREATE TABLE idempotency_records (
        project_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        command_scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        response_json TEXT NOT NULL,
        PRIMARY KEY(
            project_id, workspace_id, command_scope, idempotency_key
        )
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


def _schema_objects_for(statements: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    memory = sqlite3.connect(":memory:")
    try:
        memory.execute("PRAGMA foreign_keys=ON")
        for statement in statements:
            memory.execute(statement)
        return _schema_objects(memory)
    finally:
        memory.close()


def _digest_objects(objects: tuple[tuple[str, ...], ...]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(objects, separators=(",", ":")).encode()
    ).hexdigest()


_V1_EXPECTED = _schema_objects_for(V1_SCHEMA)
_EXPECTED = _schema_objects_for(_SCHEMA)
V1_SCHEMA_DIGEST = _digest_objects(_V1_EXPECTED)
SCHEMA_DIGEST = _digest_objects(_EXPECTED)


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


def _generation(value: object) -> int:
    if type(value) is not int or value < 1:
        _fail("invalid_argument")
    return value


def _revision(value: object) -> int:
    if type(value) is not int or value < 1:
        _fail("invalid_argument")
    return value


def _sha256(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(char not in "0123456789abcdef" for char in value[7:])
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
        current = [
            tuple(row) for row in rows
            if row["migration_id"] == MIGRATION_ID
        ]
        if current != [(MIGRATION_ID, SCHEMA_VERSION, SCHEMA_DIGEST)]:
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


def _validate_v1(connection: sqlite3.Connection) -> None:
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        rows = connection.execute(
            "SELECT migration_id, schema_version, schema_digest "
            "FROM schema_migrations"
        ).fetchall()
        if (
            version != V1_SCHEMA_VERSION
            or [tuple(row) for row in rows] != [
                (V1_MIGRATION_ID, V1_SCHEMA_VERSION, V1_SCHEMA_DIGEST)
            ]
            or _schema_objects(connection) != _V1_EXPECTED
        ):
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


def _after_migration_copy(_connection: sqlite3.Connection) -> None:
    return None


def _after_migration_swap(_connection: sqlite3.Connection) -> None:
    return None


def _after_migration_receipt(_connection: sqlite3.Connection) -> None:
    return None


def _after_claim_activate(_connection: sqlite3.Connection) -> None:
    return None


def _after_reply_complete(_connection: sqlite3.Connection) -> None:
    return None


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER idempotency_records_no_update")
    connection.execute("DROP TRIGGER idempotency_records_no_delete")
    connection.execute("ALTER TABLE messages RENAME TO messages_v1")
    connection.execute("ALTER TABLE work_items RENAME TO work_items_v1")
    connection.execute("ALTER TABLE idempotency_records RENAME TO idempotency_records_v1")
    for statement in _SCHEMA:
        if "CREATE TABLE schema_migrations" in statement:
            continue
        if "CREATE TRIGGER schema_migrations_" in statement:
            continue
        if "CREATE TABLE message_threads" in statement:
            continue
        connection.execute(statement)
    connection.execute(
        "INSERT INTO messages SELECT "
        "m.message_id, m.thread_id, 1, 'root', m.author_kind, m.author_ref, "
        "NULL, NULL, m.body, t.created_at "
        "FROM messages_v1 m JOIN message_threads t ON t.thread_id = m.thread_id"
    )
    connection.execute(
        "INSERT INTO work_items SELECT "
        "w.work_item_id, w.source_message_id, w.status, w.acceptance, "
        "w.constraints, 1, t.created_at "
        "FROM work_items_v1 w "
        "JOIN messages_v1 m ON m.message_id = w.source_message_id "
        "JOIN message_threads t ON t.thread_id = m.thread_id"
    )
    connection.execute(
        "INSERT INTO idempotency_records SELECT "
        "project_id, workspace_id, ?, idempotency_key, request_digest, "
        "response_json FROM idempotency_records_v1",
        (CREATE_SCOPE,),
    )
    _after_migration_copy(connection)
    connection.execute("DROP TABLE work_items_v1")
    connection.execute("DROP TABLE messages_v1")
    connection.execute("DROP TABLE idempotency_records_v1")
    _after_migration_swap(connection)
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?,?,?,?)",
        (MIGRATION_ID, SCHEMA_VERSION, SCHEMA_DIGEST, _now()),
    )
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    _after_migration_receipt(connection)


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


def _idempotency_row(
    connection: sqlite3.Connection, *, project_id: str, workspace_id: str,
    scope: str, key: str, digest: str,
) -> dict[str, object] | None:
    existing = connection.execute(
        "SELECT request_digest, response_json FROM idempotency_records "
        "WHERE project_id=? AND workspace_id=? AND command_scope=? "
        "AND idempotency_key=?",
        (project_id, workspace_id, scope, key),
    ).fetchone()
    if existing is None:
        return None
    if existing["request_digest"] != digest:
        _fail("idempotency_conflict")
    return json.loads(existing["response_json"])


def _remember(
    connection: sqlite3.Connection, *, project_id: str, workspace_id: str,
    scope: str, key: str, digest: str, payload: object,
) -> None:
    connection.execute(
        "INSERT INTO idempotency_records VALUES (?,?,?,?,?,?)",
        (project_id, workspace_id, scope, key, digest, _canonical(payload)),
    )


def _load_work(
    connection: sqlite3.Connection, *, project_id: str, workspace_id: str,
    work_item_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT w.work_item_id, w.source_message_id, w.status, w.acceptance, "
        "w.constraints, w.revision AS work_revision, w.updated_at, "
        "t.thread_id, t.revision AS thread_revision, t.created_at, "
        "m.message_id, m.body, m.author_kind, m.author_ref "
        "FROM work_items w "
        "JOIN messages m ON m.message_id = w.source_message_id "
        "JOIN message_threads t ON t.thread_id = m.thread_id "
        "WHERE t.project_id=? AND t.workspace_id=? AND w.work_item_id=?",
        (project_id, workspace_id, work_item_id),
    ).fetchone()


def _claim_public(row: sqlite3.Row | dict[str, object]) -> dict[str, object]:
    return {
        "claim_id": row["claim_id"],
        "work_item_id": row["work_item_id"],
        "identity_id": row["identity_id"],
        "generation": int(row["generation"]),
        "state": row["state"],
        "revision": int(row["revision"]),
    }


def _insert_receipt(
    connection: sqlite3.Connection, *, project_id: str, workspace_id: str,
    work_item_id: str, claim_id: str | None, message_id: str | None,
    identity_id: str | None, generation: int | None, kind: str,
) -> None:
    payload = {
        "claim_id": claim_id,
        "generation": generation,
        "identity_id": identity_id,
        "kind": kind,
        "message_id": message_id,
        "work_item_id": work_item_id,
    }
    connection.execute(
        "INSERT INTO message_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _new_id("rct_"), project_id, workspace_id, work_item_id, claim_id,
            message_id, identity_id, generation, kind, "ok", None,
            "sha256:" + _digest(payload), _now(),
        ),
    )


def _write(
    path: Path, operation: Callable[[sqlite3.Connection], object],
) -> object:
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(path, write=True)
        _require_current_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        result = operation(connection)
        connection.execute("COMMIT")
        return result
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
            existing = _idempotency_row(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=CREATE_SCOPE, key=idempotency_key, digest=request_digest,
            )
            if existing is not None:
                connection.execute("COMMIT")
                return CommandResult(201, WorkItemAggregate(
                    existing["thread"], existing["root_message"],
                    existing["work_item"],
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
                "INSERT INTO messages ("
                "message_id, thread_id, ordinal, message_kind, author_kind, "
                "author_ref, author_generation, reply_to_message_id, body, "
                "created_at) VALUES (?,?,1,'root',?,?,NULL,NULL,?,?)",
                (message_id, thread_id, AUTHOR_KIND, None, body, created_at),
            )
            _after_root_message_insert(connection)
            connection.execute(
                "INSERT INTO work_items ("
                "work_item_id, source_message_id, status, acceptance, "
                "constraints, revision, updated_at) VALUES (?,?,?,?,?,1,?)",
                (
                    work_item_id, message_id, STATUS, acceptance, constraints,
                    created_at,
                ),
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
            _remember(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=CREATE_SCOPE, key=idempotency_key, digest=request_digest,
                payload=item.public_dict(),
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

    def reserve_claim(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        identity_id: object, generation: object, expected_revision: object,
        idempotency_key: object,
    ) -> dict[str, object]:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        identity_id = _opaque(identity_id, "idn_")
        generation = _generation(generation)
        expected_revision = _revision(expected_revision)
        idempotency_key = _idempotency_key(idempotency_key)
        request_digest = _digest({
            "expected_revision": expected_revision,
            "generation": generation,
            "identity_id": identity_id,
            "work_item_id": work_item_id,
        })

        def operate(connection: sqlite3.Connection) -> dict[str, object]:
            replay = _idempotency_row(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=RESERVE_SCOPE, key=idempotency_key, digest=request_digest,
            )
            if replay is not None:
                return replay
            work = _load_work(
                connection, project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            if work is None:
                _fail("work_item_not_found")
            if work["status"] in {"completed", "failed"}:
                _fail("execution_terminal")
            if int(work["work_revision"]) != expected_revision:
                _fail("stale_revision")
            if work["status"] != "unassigned":
                _fail("claim_conflict")
            claim_id = _new_id("clm_")
            created_at = _now()
            try:
                connection.execute(
                    "INSERT INTO work_item_claims VALUES (?,?,?,?,?,?,?,?)",
                    (
                        claim_id, work_item_id, identity_id, generation,
                        "pending_gate", 1, created_at, created_at,
                    ),
                )
            except sqlite3.IntegrityError:
                _fail("claim_conflict")
            payload = {
                "claim": {
                    "claim_id": claim_id,
                    "work_item_id": work_item_id,
                    "identity_id": identity_id,
                    "generation": generation,
                    "state": "pending_gate",
                    "revision": 1,
                },
            }
            _remember(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=RESERVE_SCOPE, key=idempotency_key, digest=request_digest,
                payload=payload,
            )
            return payload

        result = _write(self.path, operate)
        assert isinstance(result, dict)
        return result

    def record_delivery(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        expected_revision: object, outcome: object, evidence_digest: object,
        idempotency_key: object,
    ) -> dict[str, object]:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        expected_revision = _revision(expected_revision)
        if outcome not in {"intent", "succeeded", "outcome_unknown"}:
            _fail("invalid_argument")
        assert isinstance(outcome, str)
        evidence_digest = _sha256(evidence_digest)
        idempotency_key = _idempotency_key(idempotency_key)
        scope = f"{DELIVERY_SCOPE}.{outcome}"
        request_digest = _digest({
            "evidence_digest": evidence_digest,
            "expected_revision": expected_revision,
            "outcome": outcome,
            "work_item_id": work_item_id,
        })

        def operate(connection: sqlite3.Connection) -> dict[str, object]:
            replay = _idempotency_row(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=scope, key=idempotency_key, digest=request_digest,
            )
            if replay is not None:
                return replay
            work = _load_work(
                connection, project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            if work is None:
                _fail("work_item_not_found")
            if work["status"] != "unassigned":
                _fail("delivery_conflict")
            if int(work["work_revision"]) != expected_revision:
                _fail("stale_revision")
            revision = expected_revision + 1
            now = _now()
            if connection.execute(
                "UPDATE work_items SET revision=?,updated_at=? "
                "WHERE work_item_id=? AND revision=? AND status='unassigned'",
                (revision, now, work_item_id, expected_revision),
            ).rowcount != 1:
                _fail("stale_revision")
            connection.execute(
                "INSERT INTO message_receipts VALUES (?,?,?,?,NULL,NULL,NULL,NULL,"
                "'delivery',?,NULL,?,?)",
                (
                    _new_id("rct_"), project_id, workspace_id, work_item_id,
                    outcome, evidence_digest, now,
                ),
            )
            payload = {
                "work_item_id": work_item_id,
                "status": "unassigned",
                "revision": revision,
                "outcome": outcome,
                "evidence_digest": evidence_digest,
            }
            _remember(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=scope, key=idempotency_key, digest=request_digest,
                payload=payload,
            )
            return payload

        result = _write(self.path, operate)
        assert isinstance(result, dict)
        return result

    def activate_claim(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        claim_id: object, identity_id: object, generation: object,
        expected_claim_revision: object, expected_work_revision: object,
        idempotency_key: object,
    ) -> dict[str, object]:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        claim_id = _opaque(claim_id, "clm_")
        identity_id = _opaque(identity_id, "idn_")
        generation = _generation(generation)
        expected_claim_revision = _revision(expected_claim_revision)
        expected_work_revision = _revision(expected_work_revision)
        idempotency_key = _idempotency_key(idempotency_key)
        request_digest = _digest({
            "claim_id": claim_id,
            "expected_claim_revision": expected_claim_revision,
            "expected_work_revision": expected_work_revision,
            "generation": generation,
            "identity_id": identity_id,
            "work_item_id": work_item_id,
        })

        def operate(connection: sqlite3.Connection) -> dict[str, object]:
            replay = _idempotency_row(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=ACTIVATE_SCOPE, key=idempotency_key, digest=request_digest,
            )
            if replay is not None:
                return replay
            work = _load_work(
                connection, project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            if work is None:
                _fail("work_item_not_found")
            claim = connection.execute(
                "SELECT * FROM work_item_claims WHERE claim_id=? "
                "AND work_item_id=?",
                (claim_id, work_item_id),
            ).fetchone()
            if claim is None:
                _fail("work_item_not_found")
            if int(claim["generation"]) != generation:
                _fail("stale_generation")
            if (
                int(claim["revision"]) != expected_claim_revision
                or int(work["work_revision"]) != expected_work_revision
            ):
                _fail("stale_revision")
            if claim["identity_id"] != identity_id:
                _fail("claim_conflict")
            if claim["state"] != "pending_gate" or work["status"] != "unassigned":
                _fail("claim_not_active")
            now = _now()
            connection.execute(
                "UPDATE work_item_claims SET state='active', revision=?, "
                "updated_at=? WHERE claim_id=?",
                (int(claim["revision"]) + 1, now, claim_id),
            )
            connection.execute(
                "UPDATE work_items SET status='working', revision=?, "
                "updated_at=? WHERE work_item_id=?",
                (int(work["work_revision"]) + 1, now, work_item_id),
            )
            _insert_receipt(
                connection, project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, claim_id=claim_id,
                message_id=None, identity_id=identity_id, generation=generation,
                kind="claim",
            )
            _after_claim_activate(connection)
            payload = {
                "claim": {
                    "claim_id": claim_id,
                    "work_item_id": work_item_id,
                    "identity_id": identity_id,
                    "generation": generation,
                    "state": "active",
                    "revision": int(claim["revision"]) + 1,
                },
                "work_item": {
                    "work_item_id": work_item_id,
                    "source_message_id": work["source_message_id"],
                    "status": "working",
                    "acceptance": work["acceptance"],
                    "constraints": work["constraints"],
                    "revision": int(work["work_revision"]) + 1,
                },
                "root_message": {
                    "message_id": work["message_id"],
                    "thread_id": work["thread_id"],
                    "author_kind": work["author_kind"],
                    "author_ref": work["author_ref"],
                    "body": work["body"],
                },
            }
            _remember(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=ACTIVATE_SCOPE, key=idempotency_key, digest=request_digest,
                payload=payload,
            )
            return payload

        result = _write(self.path, operate)
        assert isinstance(result, dict)
        return result

    def reply_complete(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        claim_id: object, identity_id: object, generation: object,
        expected_claim_revision: object, expected_work_revision: object,
        body: object, idempotency_key: object,
    ) -> dict[str, object]:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        claim_id = _opaque(claim_id, "clm_")
        identity_id = _opaque(identity_id, "idn_")
        generation = _generation(generation)
        expected_claim_revision = _revision(expected_claim_revision)
        expected_work_revision = _revision(expected_work_revision)
        body = body_text(body)
        idempotency_key = _idempotency_key(idempotency_key)
        request_digest = _digest({
            "body": body,
            "claim_id": claim_id,
            "expected_claim_revision": expected_claim_revision,
            "expected_work_revision": expected_work_revision,
            "generation": generation,
            "identity_id": identity_id,
            "work_item_id": work_item_id,
        })

        def operate(connection: sqlite3.Connection) -> dict[str, object]:
            replay = _idempotency_row(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=REPLY_SCOPE, key=idempotency_key, digest=request_digest,
            )
            if replay is not None:
                return replay
            work = _load_work(
                connection, project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            if work is None:
                _fail("work_item_not_found")
            claim = connection.execute(
                "SELECT * FROM work_item_claims WHERE claim_id=? "
                "AND work_item_id=?",
                (claim_id, work_item_id),
            ).fetchone()
            if claim is None:
                _fail("work_item_not_found")
            if int(claim["generation"]) != generation:
                _fail("stale_generation")
            if (
                int(claim["revision"]) != expected_claim_revision
                or int(work["work_revision"]) != expected_work_revision
            ):
                _fail("stale_revision")
            if claim["identity_id"] != identity_id:
                _fail("claim_conflict")
            if claim["state"] != "active" or work["status"] != "working":
                _fail("claim_not_active")
            now = _now()
            ordinal = int(connection.execute(
                "SELECT MAX(ordinal) FROM messages WHERE thread_id=?",
                (work["thread_id"],),
            ).fetchone()[0]) + 1
            reply_id = _new_id("msg_")
            connection.execute(
                "INSERT INTO messages ("
                "message_id, thread_id, ordinal, message_kind, author_kind, "
                "author_ref, author_generation, reply_to_message_id, body, "
                "created_at) VALUES (?,?,?,'reply','agent',?,?,?,?,?)",
                (
                    reply_id, work["thread_id"], ordinal, identity_id,
                    generation, work["message_id"], body, now,
                ),
            )
            connection.execute(
                "UPDATE message_threads SET revision=? WHERE thread_id=?",
                (int(work["thread_revision"]) + 1, work["thread_id"]),
            )
            connection.execute(
                "UPDATE work_items SET status='completed', revision=?, "
                "updated_at=? WHERE work_item_id=?",
                (int(work["work_revision"]) + 1, now, work_item_id),
            )
            connection.execute(
                "UPDATE work_item_claims SET state='closed', revision=?, "
                "updated_at=? WHERE claim_id=?",
                (int(claim["revision"]) + 1, now, claim_id),
            )
            _insert_receipt(
                connection, project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, claim_id=claim_id,
                message_id=reply_id, identity_id=identity_id,
                generation=generation, kind="reply",
            )
            _insert_receipt(
                connection, project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, claim_id=claim_id,
                message_id=reply_id, identity_id=identity_id,
                generation=generation, kind="complete",
            )
            _after_reply_complete(connection)
            payload = {
                "claim": {
                    "claim_id": claim_id,
                    "work_item_id": work_item_id,
                    "identity_id": identity_id,
                    "generation": generation,
                    "state": "closed",
                    "revision": int(claim["revision"]) + 1,
                },
                "work_item": {
                    "work_item_id": work_item_id,
                    "source_message_id": work["source_message_id"],
                    "status": "completed",
                    "acceptance": work["acceptance"],
                    "constraints": work["constraints"],
                    "revision": int(work["work_revision"]) + 1,
                },
                "reply_message": {
                    "message_id": reply_id,
                    "thread_id": work["thread_id"],
                    "ordinal": ordinal,
                    "message_kind": "reply",
                    "author_kind": "agent",
                    "author_ref": identity_id,
                    "author_generation": generation,
                    "reply_to_message_id": work["message_id"],
                    "body": body,
                },
            }
            _remember(
                connection, project_id=project_id, workspace_id=workspace_id,
                scope=REPLY_SCOPE, key=idempotency_key, digest=request_digest,
                payload=payload,
            )
            return payload

        result = _write(self.path, operate)
        assert isinstance(result, dict)
        return result

    def get_work_item_detail(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
    ) -> dict[str, object] | None:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect(self.path, write=False)
            _require_current_schema(connection)
            connection.execute("BEGIN")
            work = _load_work(
                connection, project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            if work is None:
                return None
            messages = [
                {
                    "message_id": row["message_id"],
                    "thread_id": row["thread_id"],
                    "ordinal": int(row["ordinal"]),
                    "message_kind": row["message_kind"],
                    "author_kind": row["author_kind"],
                    "author_ref": row["author_ref"],
                    "author_generation": row["author_generation"],
                    "reply_to_message_id": row["reply_to_message_id"],
                    "body": row["body"],
                    "created_at": row["created_at"],
                }
                for row in connection.execute(
                    "SELECT * FROM messages WHERE thread_id=? ORDER BY ordinal",
                    (work["thread_id"],),
                )
            ]
            claim_row = connection.execute(
                "SELECT * FROM work_item_claims WHERE work_item_id=? "
                "ORDER BY CASE state WHEN 'closed' THEN 1 ELSE 0 END, "
                "updated_at DESC, claim_id DESC",
                (work_item_id,),
            ).fetchone()
            receipts = [
                {
                    "receipt_id": row["receipt_id"],
                    "kind": row["kind"],
                    "outcome": row["outcome"],
                    "reason": row["reason"],
                    "evidence_digest": row["evidence_digest"],
                    "created_at": row["created_at"],
                    "claim_id": row["claim_id"],
                    "message_id": row["message_id"],
                    "identity_id": row["identity_id"],
                    "generation": row["generation"],
                }
                for row in connection.execute(
                    "SELECT * FROM message_receipts WHERE work_item_id=? "
                    "ORDER BY created_at, receipt_id",
                    (work_item_id,),
                )
            ]
            return {
                "thread": {
                    "thread_id": work["thread_id"],
                    "project_id": project_id,
                    "workspace_id": workspace_id,
                    "revision": int(work["thread_revision"]),
                    "created_at": work["created_at"],
                    "messages": messages,
                },
                "work_item": {
                    "work_item_id": work["work_item_id"],
                    "source_message_id": work["source_message_id"],
                    "status": work["status"],
                    "acceptance": work["acceptance"],
                    "constraints": work["constraints"],
                    "revision": int(work["work_revision"]),
                    "updated_at": work["updated_at"],
                },
                "claim": None if claim_row is None else _claim_public(claim_row),
                "receipts": receipts,
            }
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


def _migrate_existing(path: Path) -> WorkspaceWorkStore:
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(path, write=True)
        connection.execute("BEGIN IMMEDIATE")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == SCHEMA_VERSION:
            connection.execute("COMMIT")
            _validate_schema(connection)
            return WorkspaceWorkStore(path)
        if version != V1_SCHEMA_VERSION:
            _fail("migration_required")
        _validate_v1(connection)
        _migrate_v1_to_v2(connection)
        connection.execute("COMMIT")
        _validate_schema(connection)
        return WorkspaceWorkStore(path)
    except WorkspaceWorkError:
        if connection is not None and connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
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


def open_existing(path: Path) -> WorkspaceWorkStore:
    path = _path(path)
    _leaf(path, missing="workspace_work_schema_missing")
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(path, write=False)
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='schema_migrations'"
        ).fetchone()
        if present is None:
            _fail("workspace_work_schema_missing")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            _fail("future_schema")
        if version == SCHEMA_VERSION:
            _validate_schema(connection)
            return WorkspaceWorkStore(path)
        if version != V1_SCHEMA_VERSION:
            _fail("migration_required")
    except sqlite3.DatabaseError as exc:
        _fail("store_corrupt", exc)
    finally:
        if connection is not None:
            connection.close()
    return _migrate_existing(path)
