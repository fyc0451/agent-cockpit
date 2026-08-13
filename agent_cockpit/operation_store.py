"""Durable, dormant Operation Journal v1 backed by a private SQLite store."""
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
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
MIGRATION_ID = "operation-journal-v1"


class OperationError(RuntimeError):
    """Sanitized journal error with a stable public ``code``."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Precondition:
    precondition_type: str
    subject_type: str
    subject_id: str
    expected_revision: int | None = None
    expected_generation: str | None = None
    expected_epoch: str | None = None
    expected_digest: str | None = None


@dataclass(frozen=True)
class Step:
    step_id: str
    kind: str
    compensation_kind: str | None = None


@dataclass(frozen=True)
class CreateResult:
    operation_id: str
    request_digest: str
    replayed: bool
    projection: dict[str, object]


@dataclass(frozen=True)
class AttemptResult:
    operation_id: str
    step_execution_id: str
    attempt_no: int
    projection: dict[str, object]


_OPERATION_STATUSES = frozenset({
    "planned", "waiting_approval", "running", "succeeded", "failed",
    "compensating", "compensated", "needs_attention",
})
_TRANSITIONS = {
    "planned": frozenset({"waiting_approval", "running", "failed"}),
    "waiting_approval": frozenset({"running", "failed"}),
    "running": frozenset({"succeeded", "failed", "compensating", "needs_attention"}),
    "compensating": frozenset({"compensated", "needs_attention"}),
}
_TERMINAL = frozenset({"succeeded", "failed", "compensated", "needs_attention"})
_ATTEMPT_MODES = frozenset({"execute", "compensate"})
_ATTEMPT_OUTCOMES = frozenset({"succeeded", "failed", "outcome_unknown"})
_RECEIPT_TYPES = frozenset({
    "provider_outcome", "provider_response_lost", "provider_reconciliation",
})
_EVIDENCE_KINDS = frozenset({"opaque_digest", "provider_execution", "provider_query"})


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE schema_migrations (
      migration_id TEXT PRIMARY KEY,
      schema_version INTEGER NOT NULL,
      schema_digest TEXT NOT NULL,
      applied_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TRIGGER schema_migrations_no_update
    BEFORE UPDATE ON schema_migrations BEGIN SELECT RAISE(ABORT, 'append_only'); END
    """,
    """
    CREATE TRIGGER schema_migrations_no_delete
    BEFORE DELETE ON schema_migrations BEGIN SELECT RAISE(ABORT, 'append_only'); END
    """,
    """
    CREATE TABLE operations (
      operation_id TEXT PRIMARY KEY,
      scope TEXT NOT NULL,
      idempotency_key TEXT NOT NULL,
      request_digest TEXT NOT NULL,
      kind TEXT NOT NULL,
      project_id TEXT,
      workspace_id TEXT,
      subject_type TEXT NOT NULL,
      subject_id TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN (
        'planned','waiting_approval','running','succeeded','failed',
        'compensating','compensated','needs_attention'
      )),
      revision INTEGER NOT NULL CHECK(revision >= 1),
      plan_digest TEXT NOT NULL,
      approval_required INTEGER NOT NULL CHECK(approval_required IN (0,1)),
      failure_code TEXT,
      attention_reason TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      terminal_at TEXT,
      UNIQUE(scope, idempotency_key)
    ) STRICT
    """,
    """
    CREATE TABLE operation_preconditions (
      operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE RESTRICT,
      ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
      precondition_type TEXT NOT NULL,
      subject_type TEXT NOT NULL,
      subject_id TEXT NOT NULL,
      expected_revision INTEGER CHECK(expected_revision >= 0),
      expected_generation TEXT,
      expected_epoch TEXT,
      expected_digest TEXT,
      PRIMARY KEY(operation_id, ordinal)
    ) STRICT
    """,
    """
    CREATE TABLE operation_steps (
      operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE RESTRICT,
      ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
      step_id TEXT NOT NULL,
      kind TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN (
        'pending','running','succeeded','failed','compensated','outcome_unknown'
      )),
      revision INTEGER NOT NULL CHECK(revision >= 1),
      compensation_kind TEXT,
      active_attempt_no INTEGER CHECK(active_attempt_no >= 1),
      PRIMARY KEY(operation_id, ordinal),
      UNIQUE(operation_id, step_id)
    ) STRICT
    """,
    """
    CREATE TABLE operation_attempts (
      operation_id TEXT NOT NULL,
      step_id TEXT NOT NULL,
      attempt_no INTEGER NOT NULL CHECK(attempt_no >= 1),
      step_execution_id TEXT NOT NULL UNIQUE,
      mode TEXT NOT NULL CHECK(mode IN ('execute','compensate')),
      status TEXT NOT NULL CHECK(status IN (
        'prepared','dispatched','succeeded','failed','outcome_unknown'
      )),
      provider_kind TEXT NOT NULL,
      provider_operation_ref TEXT,
      failure_code TEXT,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      PRIMARY KEY(operation_id, step_id, attempt_no),
      UNIQUE(operation_id, step_id, attempt_no, step_execution_id),
      FOREIGN KEY(operation_id, step_id)
        REFERENCES operation_steps(operation_id, step_id) ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE UNIQUE INDEX operation_attempts_one_active
    ON operation_attempts(operation_id, step_id, mode)
    WHERE status IN ('prepared','dispatched','outcome_unknown')
    """,
    """
    CREATE TABLE operation_receipts (
      receipt_id TEXT PRIMARY KEY,
      operation_id TEXT NOT NULL,
      step_id TEXT NOT NULL,
      attempt_no INTEGER NOT NULL,
      step_execution_id TEXT NOT NULL,
      receipt_type TEXT NOT NULL,
      outcome TEXT NOT NULL,
      evidence_kind TEXT NOT NULL,
      evidence_ref TEXT,
      evidence_digest TEXT NOT NULL,
      summary TEXT,
      recorded_at TEXT NOT NULL,
      CHECK(outcome IN ('succeeded','failed','outcome_unknown','not_executed')),
      FOREIGN KEY(operation_id, step_id, attempt_no, step_execution_id)
        REFERENCES operation_attempts(
          operation_id, step_id, attempt_no, step_execution_id
        ) ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TRIGGER operation_receipts_no_update
    BEFORE UPDATE ON operation_receipts BEGIN SELECT RAISE(ABORT, 'append_only'); END
    """,
    """
    CREATE TRIGGER operation_receipts_no_delete
    BEFORE DELETE ON operation_receipts BEGIN SELECT RAISE(ABORT, 'append_only'); END
    """,
)


def _canonical_sql(value: str | None) -> str:
    return " ".join((value or "").split()).lower()


def _schema_objects(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (str(kind), str(name), str(table), _canonical_sql(sql))
        for kind, name, table, sql in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "ORDER BY type,name"
        ).fetchall()
    )


def _expected_schema_objects() -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        return _schema_objects(connection)
    finally:
        connection.close()


_EXPECTED_SCHEMA_OBJECTS = _expected_schema_objects()
SCHEMA_DIGEST = "sha256:" + hashlib.sha256(
    json.dumps(_EXPECTED_SCHEMA_OBJECTS, separators=(",", ":")).encode()
).hexdigest()


def _fail(code: str) -> None:
    raise OperationError(code) from None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(16)


def _text(value: object, *, maximum: int = 256, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail("invalid_argument")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        _fail("invalid_argument")
    return value


def _summary(value: object) -> str | None:
    if value is not None:
        _fail("invalid_argument")
    return None


def _opaque(value: object, *, nullable: bool = False) -> str | None:
    value = _text(value, maximum=256, nullable=nullable)
    if value is None:
        return None
    if not all(character.isalnum() or character in "._:-" for character in value):
        _fail("invalid_argument")
    return value


def _digest(value: object) -> str:
    try:
        canonical = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        _fail("invalid_argument")
    if len(canonical) > 64 * 1024:
        _fail("invalid_argument")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _sha256(value: object) -> str:
    value = _text(value, maximum=71)
    assert isinstance(value, str)
    if len(value) != 71 or not value.startswith("sha256:"):
        _fail("invalid_argument")
    try:
        int(value[7:], 16)
    except ValueError:
        _fail("invalid_argument")
    return value.lower()


def _revision(value: object) -> int:
    if type(value) is not int or value < 1:
        _fail("invalid_argument")
    return value


def _absolute_path(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        _fail("store_unsafe")
    return path


def _reject_symlinks(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            _fail("store_unsafe")
        if stat.S_ISLNK(info.st_mode):
            _fail("store_unsafe")


def _validate_directory(path: Path, *, exact_private: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError:
        _fail("store_unsafe")
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or mode & 0o022
        or (exact_private and mode != 0o700)
    ):
        _fail("store_unsafe")


def _prepare_parent(
    path: Path, *, create: bool, created: list[Path] | None = None,
) -> tuple[Path, ...]:
    _reject_symlinks(path)
    current = path.parent
    missing: list[Path] = []
    while not current.exists():
        missing.append(current)
        current = current.parent
    _validate_directory(current)
    if missing and not create:
        _fail("schema_missing")
    created_directories: list[Path] = []
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
            created_directories.append(directory)
            if created is not None:
                created.append(directory)
            os.chmod(directory, 0o700)
        except OSError:
            _fail("store_unsafe")
        _validate_directory(directory, exact_private=True)
    _reject_symlinks(path)
    if path.parent.exists():
        _validate_directory(path.parent, exact_private=True)
    return tuple(created_directories)


FileSignature = tuple[int, int, int, int, int]


def _leaf_signature(path: Path, *, must_exist: bool) -> FileSignature:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if must_exist:
            _fail("schema_missing")
        return ()
    except OSError:
        _fail("store_unsafe")
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        _fail("store_unsafe")
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink)


def _check_path(path: Path, *, must_exist: bool) -> FileSignature:
    _prepare_parent(path, create=False)
    return _leaf_signature(path, must_exist=must_exist)


def _sidecars(path: Path) -> tuple[Path, Path, Path]:
    return (Path(f"{path}-journal"), Path(f"{path}-wal"), Path(f"{path}-shm"))


def _require_no_sidecars(path: Path) -> None:
    if any(sidecar.exists() or sidecar.is_symlink() for sidecar in _sidecars(path)):
        _fail("store_unsafe")


def _create_leaf(path: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        _fail("store_unsafe")
    try:
        os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            _fail("store_unsafe")
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    _validate_directory(path, exact_private=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        _fail("store_unsafe")
    try:
        os.fsync(fd)
    except OSError:
        _fail("store_unsafe")
    finally:
        os.close(fd)


def _connect_write(path: Path) -> sqlite3.Connection:
    before = _check_path(path, must_exist=True)
    try:
        connection = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    except sqlite3.Error:
        _fail("store_read_failed")
    try:
        if _check_path(path, must_exist=True) != before:
            _fail("store_unsafe")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection
    except BaseException:
        connection.close()
        raise


def _connect_read(path: Path, *, immutable: bool) -> sqlite3.Connection:
    before = _check_path(path, must_exist=True)
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    try:
        connection = sqlite3.connect(path.as_uri() + suffix, uri=True, timeout=1.0)
    except sqlite3.Error:
        _fail("store_read_failed")
    try:
        if _check_path(path, must_exist=True) != before:
            _fail("store_unsafe")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _validate_schema(connection: sqlite3.Connection) -> None:
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < SCHEMA_VERSION:
            _fail("migration_required")
        if version > SCHEMA_VERSION:
            _fail("future_schema")
        if _schema_objects(connection) != _EXPECTED_SCHEMA_OBJECTS:
            _fail("schema_fingerprint_mismatch")
        receipts = [tuple(row) for row in connection.execute(
            "SELECT migration_id,schema_version,schema_digest FROM schema_migrations"
        ).fetchall()]
        if receipts != [(MIGRATION_ID, SCHEMA_VERSION, SCHEMA_DIGEST)]:
            _fail("schema_fingerprint_mismatch")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            _fail("store_corrupt")
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if not quick or str(quick[0]).lower() != "ok":
            _fail("store_corrupt")
    except OperationError:
        raise
    except sqlite3.DatabaseError:
        _fail("store_corrupt")


def _after_schema_hook(_connection: sqlite3.Connection) -> None:
    """Ordinary-test fault injection point before initialization commit."""


class _Transaction:
    def __init__(self, path: Path):
        self.path = path
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        _require_no_sidecars(self.path)
        self.connection = _connect_write(self.path)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            _validate_schema(self.connection)
            return self.connection
        except BaseException:
            self.connection.close()
            self.connection = None
            raise

    def __exit__(self, exc_type, _exc, _traceback) -> bool:
        assert self.connection is not None
        try:
            self.connection.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.connection.close()
        return False


class OperationStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def close(self) -> None:
        pass

    def create_operation(
        self,
        *,
        scope: str,
        idempotency_key: str,
        request: object,
        kind: str,
        subject_type: str,
        subject_id: str,
        plan_digest: str,
        approval_required: bool,
        project_id: str | None = None,
        workspace_id: str | None = None,
        preconditions: Sequence[Precondition] = (),
        steps: Sequence[Step] = (),
    ) -> CreateResult:
        scope = _opaque(scope)
        idempotency_key = _opaque(idempotency_key)
        kind = _opaque(kind)
        subject_type = _opaque(subject_type)
        subject_id = _opaque(subject_id)
        project_id = _opaque(project_id, nullable=True)
        workspace_id = _opaque(workspace_id, nullable=True)
        plan_digest = _sha256(plan_digest)
        if type(approval_required) is not bool:
            _fail("invalid_argument")
        validated_preconditions = _preconditions(preconditions)
        validated_steps = _steps(steps)
        request_digest = _digest({
            "scope": scope,
            "kind": kind,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "plan_digest": plan_digest,
            "approval_required": approval_required,
            "preconditions": [item.__dict__ for item in validated_preconditions],
            "steps": [item.__dict__ for item in validated_steps],
            "request": request,
        })
        with _Transaction(self.path) as connection:
            existing = connection.execute(
                "SELECT operation_id,request_digest FROM operations "
                "WHERE scope=? AND idempotency_key=?",
                (scope, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    _fail("idempotency_conflict")
                operation_id = str(existing["operation_id"])
                replayed = True
            else:
                operation_id = _new_id("op_")
                timestamp = _now()
                connection.execute(
                    "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        operation_id, scope, idempotency_key, request_digest, kind,
                        project_id, workspace_id, subject_type, subject_id, "planned", 1,
                        plan_digest, int(approval_required), None, None,
                        timestamp, timestamp, None,
                    ),
                )
                for ordinal, item in enumerate(validated_preconditions):
                    connection.execute(
                        "INSERT INTO operation_preconditions VALUES (?,?,?,?,?,?,?,?,?)",
                        (operation_id, ordinal, *item.__dict__.values()),
                    )
                for ordinal, item in enumerate(validated_steps):
                    connection.execute(
                        "INSERT INTO operation_steps VALUES (?,?,?,?,?,?,?,?)",
                        (
                            operation_id, ordinal, item.step_id, item.kind,
                            "pending", 1, item.compensation_kind, None,
                        ),
                    )
                replayed = False
        projection = self.get_operation(operation_id)
        assert projection is not None
        return CreateResult(operation_id, request_digest, replayed, projection)

    def get_operation(self, operation_id: str) -> dict[str, object] | None:
        operation_id = _opaque(operation_id)
        _require_no_sidecars(self.path)
        connection = _connect_read(self.path, immutable=False)
        try:
            connection.execute("BEGIN")
            _validate_schema(connection)
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if operation is None:
                connection.execute("ROLLBACK")
                return None
            projection = _projection(connection, operation)
            connection.execute("COMMIT")
            return projection
        except sqlite3.DatabaseError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            _fail("store_read_failed")
        finally:
            connection.close()

    def transition(
        self,
        operation_id: str,
        *,
        expected_operation_revision: int,
        status: str,
        failure_code: str | None = None,
        attention_reason: str | None = None,
    ) -> dict[str, object]:
        operation_id = _opaque(operation_id)
        expected = _revision(expected_operation_revision)
        status = _opaque(status)
        if status not in _OPERATION_STATUSES:
            _fail("invalid_argument")
        failure_code = _opaque(failure_code, nullable=True)
        attention_reason = _text(attention_reason, maximum=512, nullable=True)
        if (status == "failed") != (failure_code is not None):
            _fail("invalid_argument")
        if (status == "needs_attention") != (attention_reason is not None):
            _fail("invalid_argument")
        with _Transaction(self.path) as connection:
            row = _operation_for_update(connection, operation_id, expected)
            current = str(row["status"])
            if status not in _TRANSITIONS.get(current, frozenset()):
                _fail("invalid_transition")
            active_attempts = int(connection.execute(
                "SELECT count(*) FROM operation_attempts WHERE operation_id=? "
                "AND status IN ('prepared','dispatched','outcome_unknown')",
                (operation_id,),
            ).fetchone()[0])
            if active_attempts:
                _fail("attempt_active")
            timestamp = _now()
            terminal_at = timestamp if status in _TERMINAL else None
            changed = connection.execute(
                "UPDATE operations SET status=?,revision=revision+1,failure_code=?,"
                "attention_reason=?,updated_at=?,terminal_at=? "
                "WHERE operation_id=? AND revision=?",
                (
                    status, failure_code, attention_reason, timestamp, terminal_at,
                    operation_id, expected,
                ),
            ).rowcount
            if changed != 1:
                _fail("revision_conflict")
        result = self.get_operation(operation_id)
        assert result is not None
        return result

    def prepare_attempt(
        self,
        operation_id: str,
        step_id: str,
        *,
        expected_operation_revision: int,
        expected_step_revision: int,
        mode: str,
        provider_kind: str,
    ) -> AttemptResult:
        operation_id = _opaque(operation_id)
        step_id = _opaque(step_id)
        operation_revision = _revision(expected_operation_revision)
        step_revision = _revision(expected_step_revision)
        mode = _opaque(mode)
        provider_kind = _opaque(provider_kind)
        if mode not in _ATTEMPT_MODES:
            _fail("invalid_argument")
        with _Transaction(self.path) as connection:
            operation = _operation_for_update(connection, operation_id, operation_revision)
            required = "running" if mode == "execute" else "compensating"
            if operation["status"] != required:
                _fail("invalid_transition")
            step = connection.execute(
                "SELECT * FROM operation_steps WHERE operation_id=? AND step_id=?",
                (operation_id, step_id),
            ).fetchone()
            if step is None:
                _fail("step_not_found")
            if int(step["revision"]) != step_revision:
                _fail("revision_conflict")
            if step["active_attempt_no"] is not None:
                _fail("attempt_active")
            valid_step = (
                mode == "execute" and step["status"] == "pending"
                or mode == "compensate"
                and step["status"] == "succeeded"
                and step["compensation_kind"] is not None
            )
            if not valid_step:
                _fail("invalid_transition")
            attempt_no = int(connection.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 FROM operation_attempts "
                "WHERE operation_id=? AND step_id=?",
                (operation_id, step_id),
            ).fetchone()[0])
            execution_id = _new_id("exec_")
            connection.execute(
                "INSERT INTO operation_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id, step_id, attempt_no, execution_id, mode,
                    "prepared", provider_kind, None, None, _now(), None,
                ),
            )
            if connection.execute(
                "UPDATE operation_steps SET status='running',revision=revision+1,"
                "active_attempt_no=? WHERE operation_id=? AND step_id=? AND revision=?",
                (attempt_no, operation_id, step_id, step_revision),
            ).rowcount != 1:
                _fail("revision_conflict")
            _bump_operation(connection, operation_id, operation_revision)
        projection = self.get_operation(operation_id)
        assert projection is not None
        return AttemptResult(operation_id, execution_id, attempt_no, projection)

    def dispatch_attempt(
        self,
        operation_id: str,
        step_execution_id: str,
        *,
        expected_operation_revision: int,
        provider_operation_ref: str | None = None,
    ) -> dict[str, object]:
        operation_id = _opaque(operation_id)
        execution_id = _opaque(step_execution_id)
        expected = _revision(expected_operation_revision)
        provider_ref = _opaque(provider_operation_ref, nullable=True)
        with _Transaction(self.path) as connection:
            operation = _operation_for_update(connection, operation_id, expected)
            if operation["status"] not in {"running", "compensating"}:
                _fail("invalid_transition")
            changed = connection.execute(
                "UPDATE operation_attempts SET status='dispatched',provider_operation_ref=? "
                "WHERE operation_id=? AND step_execution_id=? AND status='prepared'",
                (provider_ref, operation_id, execution_id),
            ).rowcount
            if changed != 1:
                _fail("attempt_conflict")
            _bump_operation(connection, operation_id, expected)
        result = self.get_operation(operation_id)
        assert result is not None
        return result

    def record_attempt_outcome(
        self,
        operation_id: str,
        step_execution_id: str,
        *,
        expected_operation_revision: int,
        expected_step_revision: int,
        receipt_id: str,
        receipt_type: str,
        outcome: str,
        evidence_kind: str,
        evidence_digest: str,
        evidence_ref: str | None = None,
        summary: str | None = None,
        failure_code: str | None = None,
        expected_prepared_step_revisions: Mapping[str, int] | None = None,
    ) -> dict[str, object]:
        operation_id = _opaque(operation_id)
        execution_id = _opaque(step_execution_id)
        expected = _revision(expected_operation_revision)
        step_revision = _revision(expected_step_revision)
        receipt_id = _opaque(receipt_id)
        receipt_type = _opaque(receipt_type)
        outcome = _opaque(outcome)
        evidence_kind = _opaque(evidence_kind)
        evidence_digest = _sha256(evidence_digest)
        evidence_ref = _opaque(evidence_ref, nullable=True)
        summary = _summary(summary)
        failure_code = _opaque(failure_code, nullable=True)
        sibling_revisions = _step_revisions(expected_prepared_step_revisions)
        if outcome not in _ATTEMPT_OUTCOMES:
            _fail("invalid_argument")
        if receipt_type not in _RECEIPT_TYPES or evidence_kind not in _EVIDENCE_KINDS:
            _fail("invalid_argument")
        if (outcome == "failed") != (failure_code is not None):
            _fail("invalid_argument")
        receipt_identity = (
            receipt_id, operation_id, execution_id, receipt_type, outcome,
            evidence_kind, evidence_ref, evidence_digest, summary, failure_code,
        )
        with _Transaction(self.path) as connection:
            existing = connection.execute(
                "SELECT r.*,a.failure_code FROM operation_receipts r "
                "JOIN operation_attempts a ON a.step_execution_id=r.step_execution_id "
                "WHERE r.receipt_id=?", (receipt_id,),
            ).fetchone()
            if existing is not None:
                actual = (
                    existing["receipt_id"], existing["operation_id"],
                    existing["step_execution_id"], existing["receipt_type"],
                    existing["outcome"], existing["evidence_kind"],
                    existing["evidence_ref"], existing["evidence_digest"],
                    existing["summary"],
                    existing["failure_code"] if existing["outcome"] == "failed" else None,
                )
                if actual != receipt_identity:
                    _fail("idempotency_conflict")
                replay = True
            else:
                _operation_for_update(connection, operation_id, expected)
                attempt = connection.execute(
                    "SELECT * FROM operation_attempts WHERE operation_id=? "
                    "AND step_execution_id=?",
                    (operation_id, execution_id),
                ).fetchone()
                if attempt is None:
                    _fail("attempt_not_found")
                if attempt["status"] != "dispatched":
                    _fail("attempt_conflict")
                step = connection.execute(
                    "SELECT * FROM operation_steps WHERE operation_id=? AND step_id=?",
                    (operation_id, attempt["step_id"]),
                ).fetchone()
                if (
                    step is None
                    or int(step["revision"]) != step_revision
                    or int(step["active_attempt_no"] or 0) != int(attempt["attempt_no"])
                    or step["status"] != "running"
                ):
                    _fail("revision_conflict")
                operation = connection.execute(
                    "SELECT * FROM operations WHERE operation_id=?", (operation_id,),
                ).fetchone()
                assert operation is not None
                if operation["status"] not in {
                    "running", "compensating", "needs_attention",
                }:
                    _fail("invalid_transition")
                required_operation = (
                    "running" if attempt["mode"] == "execute" else "compensating"
                )
                if operation["status"] not in {required_operation, "needs_attention"}:
                    _fail("invalid_transition")
                step_status = (
                    "compensated"
                    if attempt["mode"] == "compensate" and outcome == "succeeded"
                    else outcome
                )
                operation_status: str | None = None
                attention_reason: str | None = None
                terminal_at: str | None = None
                if operation["status"] == "needs_attention":
                    operation_status = "needs_attention"
                    attention_reason = str(operation["attention_reason"])
                    terminal_at = str(operation["terminal_at"])
                elif outcome == "outcome_unknown":
                    operation_status = "needs_attention"
                    attention_reason = "provider_outcome_unknown"
                    terminal_at = _now()
                elif outcome == "failed":
                    operation_status = (
                        "failed" if operation["status"] == "running"
                        else "needs_attention"
                    )
                    if operation_status == "needs_attention":
                        attention_reason = "compensation_failed"
                    terminal_at = _now()
                timestamp = _now()
                connection.execute(
                    "UPDATE operation_attempts SET status=?,failure_code=?,finished_at=? "
                    "WHERE step_execution_id=?",
                    (outcome, failure_code, timestamp, execution_id),
                )
                if outcome in {"failed", "outcome_unknown"}:
                    _settle_prepared_siblings(
                        connection, operation_id, execution_id, timestamp,
                        sibling_revisions,
                    )
                elif sibling_revisions:
                    _fail("invalid_argument")
                other_uncertain = int(connection.execute(
                    "SELECT count(*) FROM operation_attempts WHERE operation_id=? "
                    "AND step_execution_id<>? AND status IN "
                    "('dispatched','outcome_unknown')",
                    (operation_id, execution_id),
                ).fetchone()[0])
                if other_uncertain and operation_status == "failed":
                    operation_status = "needs_attention"
                    attention_reason = "operation_outcomes_pending"
                    terminal_at = timestamp
                    failure_code = None
                active = attempt["attempt_no"] if outcome == "outcome_unknown" else None
                connection.execute(
                    "UPDATE operation_steps SET status=?,revision=revision+1,"
                    "active_attempt_no=? WHERE operation_id=? AND step_id=? AND revision=?",
                    (step_status, active, operation_id, attempt["step_id"], step_revision),
                )
                connection.execute(
                    "INSERT INTO operation_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_id, operation_id, attempt["step_id"], attempt["attempt_no"],
                        execution_id, receipt_type, outcome, evidence_kind,
                        evidence_ref, evidence_digest, summary, timestamp,
                    ),
                )
                if operation_status is None:
                    _bump_operation(connection, operation_id, expected)
                else:
                    changed = connection.execute(
                        "UPDATE operations SET status=?,revision=revision+1,failure_code=?,"
                        "attention_reason=?,updated_at=?,terminal_at=? "
                        "WHERE operation_id=? AND revision=?",
                        (
                            operation_status,
                            failure_code if operation_status == "failed" else None,
                            attention_reason, timestamp,
                            terminal_at, operation_id, expected,
                        ),
                    ).rowcount
                    if changed != 1:
                        _fail("revision_conflict")
                replay = False
        result = self.get_operation(operation_id)
        assert result is not None
        result["receipt_replayed"] = replay
        return result

    def record_not_executed(
        self,
        operation_id: str,
        step_execution_id: str,
        *,
        expected_operation_revision: int,
        expected_step_revision: int,
        receipt_id: str,
        evidence_digest: str,
        evidence_ref: str | None = None,
        summary: str | None = None,
    ) -> dict[str, object]:
        operation_id = _opaque(operation_id)
        execution_id = _opaque(step_execution_id)
        expected = _revision(expected_operation_revision)
        step_revision = _revision(expected_step_revision)
        receipt_id = _opaque(receipt_id)
        evidence_digest = _sha256(evidence_digest)
        evidence_ref = _opaque(evidence_ref, nullable=True)
        summary = _summary(summary)
        identity = (
            receipt_id, operation_id, execution_id, "provider_reconciliation",
            "not_executed", "provider_query", evidence_ref, evidence_digest, summary,
        )
        with _Transaction(self.path) as connection:
            existing = connection.execute(
                "SELECT * FROM operation_receipts WHERE receipt_id=?", (receipt_id,),
            ).fetchone()
            if existing is not None:
                actual = (
                    existing["receipt_id"], existing["operation_id"],
                    existing["step_execution_id"], existing["receipt_type"],
                    existing["outcome"], existing["evidence_kind"],
                    existing["evidence_ref"], existing["evidence_digest"],
                    existing["summary"],
                )
                if actual != identity:
                    _fail("idempotency_conflict")
                replay = True
            else:
                operation = _operation_for_update(connection, operation_id, expected)
                if operation["status"] != "needs_attention":
                    _fail("invalid_transition")
                attempt = connection.execute(
                    "SELECT * FROM operation_attempts WHERE operation_id=? "
                    "AND step_execution_id=?",
                    (operation_id, execution_id),
                ).fetchone()
                if attempt is None or attempt["status"] != "outcome_unknown":
                    _fail("attempt_conflict")
                step = connection.execute(
                    "SELECT * FROM operation_steps WHERE operation_id=? AND step_id=?",
                    (operation_id, attempt["step_id"]),
                ).fetchone()
                if (
                    step is None
                    or int(step["revision"]) != step_revision
                    or step["status"] != "outcome_unknown"
                    or int(step["active_attempt_no"] or 0) != int(attempt["attempt_no"])
                ):
                    _fail("revision_conflict")
                timestamp = _now()
                restored_step_status = (
                    "pending" if attempt["mode"] == "execute" else "succeeded"
                )
                connection.execute(
                    "INSERT INTO operation_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_id, operation_id, attempt["step_id"], attempt["attempt_no"],
                        execution_id, "provider_reconciliation", "not_executed",
                        "provider_query", evidence_ref, evidence_digest, summary, timestamp,
                    ),
                )
                connection.execute(
                    "UPDATE operation_attempts SET status='failed',failure_code='not_executed',"
                    "finished_at=? WHERE step_execution_id=? AND status='outcome_unknown'",
                    (timestamp, execution_id),
                )
                if connection.execute(
                    "UPDATE operation_steps SET status=?,revision=revision+1,"
                    "active_attempt_no=NULL WHERE operation_id=? AND step_id=? AND revision=?",
                    (restored_step_status, operation_id, attempt["step_id"], step_revision),
                ).rowcount != 1:
                    _fail("revision_conflict")
                _bump_operation(connection, operation_id, expected)
                replay = False
        result = self.get_operation(operation_id)
        assert result is not None
        result["receipt_replayed"] = replay
        return result


def _operation_for_update(
    connection: sqlite3.Connection, operation_id: str, expected_revision: int,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,),
    ).fetchone()
    if row is None:
        _fail("operation_not_found")
    if int(row["revision"]) != expected_revision:
        _fail("revision_conflict")
    return row


def _bump_operation(
    connection: sqlite3.Connection, operation_id: str, expected_revision: int,
) -> None:
    if connection.execute(
        "UPDATE operations SET revision=revision+1,updated_at=? "
        "WHERE operation_id=? AND revision=?",
        (_now(), operation_id, expected_revision),
    ).rowcount != 1:
        _fail("revision_conflict")


def _settle_prepared_siblings(
    connection: sqlite3.Connection,
    operation_id: str,
    current_execution_id: str,
    timestamp: str,
    expected_step_revisions: Mapping[str, int],
) -> None:
    prepared = connection.execute(
        "SELECT * FROM operation_attempts WHERE operation_id=? "
        "AND step_execution_id<>? AND status='prepared' ORDER BY step_id,attempt_no",
        (operation_id, current_execution_id),
    ).fetchall()
    if set(expected_step_revisions) != {str(item["step_id"]) for item in prepared}:
        _fail("revision_conflict")
    for attempt in prepared:
        step = connection.execute(
            "SELECT * FROM operation_steps WHERE operation_id=? AND step_id=?",
            (operation_id, attempt["step_id"]),
        ).fetchone()
        expected_revision = expected_step_revisions[str(attempt["step_id"])]
        if (
            step is None
            or int(step["revision"]) != expected_revision
            or step["status"] != "running"
            or int(step["active_attempt_no"] or 0) != int(attempt["attempt_no"])
        ):
            _fail("revision_conflict")
        restored_status = "pending" if attempt["mode"] == "execute" else "succeeded"
        digest = "sha256:" + hashlib.sha256(
            f"not-dispatched:{attempt['step_execution_id']}".encode()
        ).hexdigest()
        receipt_id = "rcpt_" + hashlib.sha256(
            f"prepared:{attempt['step_execution_id']}".encode()
        ).hexdigest()[:32]
        connection.execute(
            "INSERT INTO operation_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_id, operation_id, attempt["step_id"], attempt["attempt_no"],
                attempt["step_execution_id"], "provider_reconciliation",
                "not_executed", "opaque_digest", None, digest, None, timestamp,
            ),
        )
        connection.execute(
            "UPDATE operation_attempts SET status='failed',failure_code='not_dispatched',"
            "finished_at=? WHERE step_execution_id=? AND status='prepared'",
            (timestamp, attempt["step_execution_id"]),
        )
        if connection.execute(
            "UPDATE operation_steps SET status=?,revision=revision+1,active_attempt_no=NULL "
            "WHERE operation_id=? AND step_id=? AND active_attempt_no=? AND revision=?",
            (
                restored_status, operation_id, attempt["step_id"],
                attempt["attempt_no"], expected_revision,
            ),
        ).rowcount != 1:
            _fail("revision_conflict")


def _step_revisions(values: Mapping[str, int] | None) -> dict[str, int]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        _fail("invalid_argument")
    result: dict[str, int] = {}
    for step_id, revision in values.items():
        validated = _opaque(step_id)
        if validated in result:
            _fail("invalid_argument")
        result[validated] = _revision(revision)
    return result


def _preconditions(values: Iterable[Precondition]) -> tuple[Precondition, ...]:
    try:
        items = tuple(values)
    except TypeError:
        _fail("invalid_argument")
    result: list[Precondition] = []
    for item in items:
        if not isinstance(item, Precondition):
            _fail("invalid_argument")
        revision = item.expected_revision
        if revision is not None and (type(revision) is not int or revision < 0):
            _fail("invalid_argument")
        result.append(Precondition(
            _opaque(item.precondition_type),
            _opaque(item.subject_type),
            _opaque(item.subject_id),
            revision,
            _opaque(item.expected_generation, nullable=True),
            _opaque(item.expected_epoch, nullable=True),
            _sha256(item.expected_digest) if item.expected_digest is not None else None,
        ))
    return tuple(result)


def _steps(values: Iterable[Step]) -> tuple[Step, ...]:
    try:
        items = tuple(values)
    except TypeError:
        _fail("invalid_argument")
    result: list[Step] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Step):
            _fail("invalid_argument")
        step = Step(
            _opaque(item.step_id), _opaque(item.kind),
            _opaque(item.compensation_kind, nullable=True),
        )
        if step.step_id in seen:
            _fail("invalid_argument")
        seen.add(step.step_id)
        result.append(step)
    return tuple(result)


def _row(row: sqlite3.Row, *, booleans: Iterable[str] = ()) -> dict[str, object]:
    result = dict(row)
    for key in booleans:
        result[key] = bool(result[key])
    return result


def _projection(connection: sqlite3.Connection, operation: sqlite3.Row) -> dict[str, object]:
    operation_id = operation["operation_id"]
    preconditions = connection.execute(
        "SELECT * FROM operation_preconditions WHERE operation_id=? ORDER BY ordinal",
        (operation_id,),
    ).fetchall()
    steps = connection.execute(
        "SELECT * FROM operation_steps WHERE operation_id=? ORDER BY ordinal",
        (operation_id,),
    ).fetchall()
    attempts = connection.execute(
        "SELECT * FROM operation_attempts WHERE operation_id=? "
        "ORDER BY step_id,attempt_no",
        (operation_id,),
    ).fetchall()
    receipts = connection.execute(
        "SELECT * FROM operation_receipts WHERE operation_id=? "
        "ORDER BY recorded_at,receipt_id",
        (operation_id,),
    ).fetchall()
    public_operation = _row(operation, booleans=("approval_required",))
    public_operation.pop("idempotency_key")
    return {
        "operation": public_operation,
        "preconditions": [_row(item) for item in preconditions],
        "steps": [_row(item) for item in steps],
        "attempts": [_row(item) for item in attempts],
        "receipts": [_row(item) for item in receipts],
    }


def initialize(path: Path) -> OperationStore:
    path = _absolute_path(path)
    created_directories: list[Path] = []
    temp: Path | None = None
    connection: sqlite3.Connection | None = None
    published = False
    completed = False
    try:
        _prepare_parent(path, create=True, created=created_directories)
        _require_no_sidecars(path)
        if path.exists() or path.is_symlink():
            completed = True
            return open_existing(path)
        temp = path.parent / f".{path.name}.init-{secrets.token_hex(16)}.tmp"
        _create_leaf(temp)
        connection = _connect_write(temp)
        connection.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?,?,?,?)",
            (MIGRATION_ID, SCHEMA_VERSION, SCHEMA_DIGEST, _now()),
        )
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        _after_schema_hook(connection)
        _validate_schema(connection)
        connection.execute("COMMIT")
        connection.close()
        connection = None
        with temp.open("rb") as stream:
            os.fsync(stream.fileno())
        try:
            os.link(temp, path, follow_symlinks=False)
        except FileExistsError:
            return open_existing(path)
        published = True
        temp.unlink()
        _leaf_signature(path, must_exist=True)
        _fsync_directory(path.parent)
        completed = True
        return OperationStore(path)
    except OperationError:
        raise
    except (OSError, sqlite3.Error):
        _fail("store_unsafe")
    finally:
        if connection is not None:
            connection.close()
        if temp is not None:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
        if published and not completed:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        if not path.exists():
            for directory in reversed(tuple(created_directories)):
                try:
                    directory.rmdir()
                except OSError:
                    break


def open_existing(path: Path) -> OperationStore:
    path = _absolute_path(path)
    _check_path(path, must_exist=True)
    _require_no_sidecars(path)
    connection = _connect_read(path, immutable=True)
    try:
        _validate_schema(connection)
    finally:
        connection.close()
    return OperationStore(path)
