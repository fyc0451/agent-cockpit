"""Independent workspace-execution.sqlite3 for Checkpoint B preparation."""
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
MIGRATION_ID = "workspace-execution-v1"
ROLE = "member"
PROVIDER = "local_herdr"
HARNESS = "codex_terminal_managed_v1"
REF_KIND = "detached"

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
    """CREATE TABLE agent_identities (
        identity_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role = 'member'),
        lifecycle TEXT NOT NULL CHECK(lifecycle = 'active'),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE work_item_preparations (
        preparation_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL UNIQUE,
        identity_id TEXT NOT NULL REFERENCES agent_identities(identity_id),
        generation INTEGER NOT NULL CHECK(generation >= 1),
        checkout_id TEXT,
        lease_id TEXT,
        attachment_id TEXT,
        state TEXT NOT NULL CHECK(state IN (
            'preparing','prepared','attaching','connected_readonly',
            'detaching','detached','outcome_unknown'
        )),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE managed_checkouts (
        checkout_id TEXT PRIMARY KEY,
        preparation_id TEXT NOT NULL REFERENCES work_item_preparations(preparation_id),
        source_head TEXT NOT NULL,
        source_tree TEXT NOT NULL,
        internal_path TEXT NOT NULL,
        ref_name TEXT,
        preflight TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('ready','failed')),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE writer_leases (
        lease_id TEXT PRIMARY KEY,
        checkout_id TEXT NOT NULL REFERENCES managed_checkouts(checkout_id),
        identity_id TEXT NOT NULL REFERENCES agent_identities(identity_id),
        generation INTEGER NOT NULL CHECK(generation >= 1),
        status TEXT NOT NULL CHECK(status IN ('reserved','revoked')),
        fence_digest TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE runtime_attachments (
        attachment_id TEXT PRIMARY KEY,
        identity_id TEXT NOT NULL REFERENCES agent_identities(identity_id),
        generation INTEGER NOT NULL CHECK(generation >= 1),
        checkout_id TEXT NOT NULL REFERENCES managed_checkouts(checkout_id),
        provider TEXT NOT NULL,
        harness TEXT NOT NULL,
        pane_id TEXT,
        instance_id TEXT,
        session_name TEXT,
        native_receipt TEXT,
        status TEXT NOT NULL CHECK(status IN (
            'attaching','connected_readonly','detaching','detached','outcome_unknown'
        )),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE idempotency_records (
        project_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        response_json TEXT NOT NULL,
        PRIMARY KEY(project_id, workspace_id, scope, idempotency_key)
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


class WorkspaceExecutionError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str, cause: BaseException | None = None) -> None:
    error = WorkspaceExecutionError(code)
    if cause is None:
        raise error
    try:
        raise error from None
    except WorkspaceExecutionError:
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


def display_name_text(value: object) -> str:
    if not isinstance(value, str):
        _fail("invalid_argument")
    name = value.strip()
    if not name or len(name) > 64:
        _fail("invalid_argument")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        _fail("invalid_argument")
    return name


def _revision(value: object) -> int:
    if type(value) is not int or value < 1:
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
    _leaf(path, missing="workspace_execution_schema_missing")
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
            _fail("workspace_execution_schema_missing")
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
    except WorkspaceExecutionError:
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
    except WorkspaceExecutionError:
        raise
    except sqlite3.DatabaseError as exc:
        _fail("store_corrupt", exc)
    except sqlite3.Error as exc:
        _fail("store_read_failed", exc)


def _write_txn(path: Path):
    connection = _connect(path, write=True)
    try:
        _require_current_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        yield_conn = connection
    except BaseException:
        connection.close()
        raise
    return yield_conn


@dataclass(frozen=True)
class IdentityView:
    identity_id: str
    display_name: str
    role: str
    lifecycle: str
    revision: int

    def public_dict(self) -> dict[str, object]:
        return {
            "identity_id": self.identity_id,
            "display_name": self.display_name,
            "role": self.role,
            "lifecycle": self.lifecycle,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class CheckoutView:
    checkout_id: str
    status: str
    source_head: str
    source_tree: str
    ref_kind: str
    revision: int

    def public_dict(self) -> dict[str, object]:
        return {
            "checkout_id": self.checkout_id,
            "status": self.status,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
            "ref_kind": self.ref_kind,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class LeaseView:
    lease_id: str
    status: str
    generation: int
    revision: int

    def public_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "status": self.status,
            "generation": self.generation,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class AttachmentView:
    attachment_id: str
    status: str
    provider: str
    harness: str
    generation: int
    identity_verified: bool
    revision: int

    def public_dict(self) -> dict[str, object]:
        return {
            "attachment_id": self.attachment_id,
            "status": self.status,
            "provider": self.provider,
            "harness": self.harness,
            "generation": self.generation,
            "identity_verified": self.identity_verified,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class PreparationView:
    work_item_id: str
    state: str
    revision: int
    work_item_status: str
    identity: IdentityView
    principal: dict[str, object]
    checkout: CheckoutView | None
    lease: LeaseView | None
    attachment: AttachmentView | None

    def public_dict(self) -> dict[str, object]:
        return {
            "work_item_id": self.work_item_id,
            "state": self.state,
            "revision": self.revision,
            "work_item_status": self.work_item_status,
            "identity": self.identity.public_dict(),
            "principal": dict(self.principal),
            "checkout": None if self.checkout is None else self.checkout.public_dict(),
            "lease": None if self.lease is None else self.lease.public_dict(),
            "attachment": (
                None if self.attachment is None else self.attachment.public_dict()
            ),
        }


@dataclass(frozen=True)
class CommandResult:
    status_code: int
    item: object


@dataclass(frozen=True)
class CheckoutInternal:
    checkout_id: str
    internal_path: str
    source_head: str
    source_tree: str


@dataclass(frozen=True)
class PrepareClaim:
    status: str
    checkout_id: str | None
    item: PreparationView | None


@dataclass(frozen=True)
class AttachmentInternal:
    attachment_id: str
    pane_id: str | None
    instance_id: str | None
    session_name: str | None
    generation: int
    status: str


def _identity_view(row: sqlite3.Row) -> IdentityView:
    return IdentityView(
        row["identity_id"], row["display_name"], row["role"],
        row["lifecycle"], int(row["revision"]),
    )


def _checkout_view(row: sqlite3.Row | None) -> CheckoutView | None:
    if row is None:
        return None
    return CheckoutView(
        row["checkout_id"], row["status"], row["source_head"],
        row["source_tree"], REF_KIND, int(row["revision"]),
    )


def _lease_view(row: sqlite3.Row | None) -> LeaseView | None:
    if row is None:
        return None
    return LeaseView(
        row["lease_id"], row["status"], int(row["generation"]), int(row["revision"]),
    )


def _attachment_verified(row: sqlite3.Row) -> bool:
    raw = row["native_receipt"]
    if not isinstance(raw, str) or not raw.startswith("{"):
        return False
    try:
        payload = json.loads(raw)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("identity_verified") is True


def _attachment_view(row: sqlite3.Row | None) -> AttachmentView | None:
    if row is None:
        return None
    return AttachmentView(
        row["attachment_id"], row["status"], row["provider"], row["harness"],
        int(row["generation"]), _attachment_verified(row), int(row["revision"]),
    )


class WorkspaceExecutionStore:
    def __init__(self, path: Path):
        self.path = _path(path)

    def close(self) -> None:
        return None

    def create_identity(
        self, *, project_id: str, workspace_id: str, display_name: object,
        idempotency_key: object,
    ) -> CommandResult:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        display_name = display_name_text(display_name)
        key = _idempotency_key(idempotency_key)
        digest = _digest({
            "display_name": display_name, "project_id": project_id,
            "workspace_id": workspace_id,
        })
        connection = _write_txn(self.path)
        try:
            replay = _replay(
                connection, project_id, workspace_id, "members.create", key, digest,
            )
            if replay is not None:
                connection.execute("COMMIT")
                return CommandResult(201, IdentityView(
                    replay["identity_id"], replay["display_name"], replay["role"],
                    replay["lifecycle"], int(replay["revision"]),
                ))
            now = _now()
            identity_id = _new_id("idn_")
            connection.execute(
                "INSERT INTO agent_identities VALUES (?,?,?,?,?,?,1,?,?)",
                (
                    identity_id, project_id, workspace_id, display_name, ROLE,
                    "active", now, now,
                ),
            )
            item = IdentityView(identity_id, display_name, ROLE, "active", 1)
            _remember(
                connection, project_id, workspace_id, "members.create", key, digest,
                item.public_dict(),
            )
            connection.execute("COMMIT")
            return CommandResult(201, item)
        except WorkspaceExecutionError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            _fail("store_write_failed", exc)
        finally:
            connection.close()
        raise AssertionError("unreachable")

    def list_identities(
        self, *, project_id: str, workspace_id: str,
    ) -> tuple[IdentityView, ...]:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        connection = _connect(self.path, write=False)
        try:
            _require_current_schema(connection)
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT * FROM agent_identities WHERE project_id=? AND workspace_id=? "
                "ORDER BY created_at, identity_id",
                (project_id, workspace_id),
            ).fetchall()
            return tuple(_identity_view(row) for row in rows)
        except WorkspaceExecutionError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            connection.close()

    def get_identity(
        self, *, project_id: str, workspace_id: str, identity_id: str,
    ) -> IdentityView | None:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        identity_id = _opaque(identity_id, "idn_")
        connection = _connect(self.path, write=False)
        try:
            _require_current_schema(connection)
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM agent_identities WHERE project_id=? AND workspace_id=? "
                "AND identity_id=?",
                (project_id, workspace_id, identity_id),
            ).fetchone()
            return None if row is None else _identity_view(row)
        except WorkspaceExecutionError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            connection.close()

    def get_preparation(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
    ) -> PreparationView | None:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        connection = _connect(self.path, write=False)
        try:
            _require_current_schema(connection)
            connection.execute("BEGIN")
            return _load_preparation(connection, project_id, workspace_id, work_item_id)
        except WorkspaceExecutionError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            connection.close()

    def checkout_internal(self, checkout_id: str) -> CheckoutInternal | None:
        checkout_id = _opaque(checkout_id, "chk_")
        connection = _connect(self.path, write=False)
        try:
            _require_current_schema(connection)
            row = connection.execute(
                "SELECT checkout_id, internal_path, source_head, source_tree "
                "FROM managed_checkouts WHERE checkout_id=?",
                (checkout_id,),
            ).fetchone()
            if row is None:
                return None
            return CheckoutInternal(
                row["checkout_id"], row["internal_path"],
                row["source_head"], row["source_tree"],
            )
        except WorkspaceExecutionError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            connection.close()

    def attachment_internal(self, attachment_id: str) -> AttachmentInternal | None:
        attachment_id = _opaque(attachment_id, "att_")
        connection = _connect(self.path, write=False)
        try:
            _require_current_schema(connection)
            row = connection.execute(
                "SELECT * FROM runtime_attachments WHERE attachment_id=?",
                (attachment_id,),
            ).fetchone()
            if row is None:
                return None
            return AttachmentInternal(
                row["attachment_id"], row["pane_id"], row["instance_id"],
                row["session_name"], int(row["generation"]), row["status"],
            )
        except WorkspaceExecutionError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            connection.close()

    def replay(
        self, *, project_id: str, workspace_id: str, scope: str,
        idempotency_key: object, request: object,
    ) -> dict[str, object] | None:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        key = _idempotency_key(idempotency_key)
        digest = _digest(request)
        connection = _connect(self.path, write=False)
        try:
            _require_current_schema(connection)
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT request_digest, response_json FROM idempotency_records "
                "WHERE project_id=? AND workspace_id=? AND scope=? AND idempotency_key=?",
                (project_id, workspace_id, scope, key),
            ).fetchone()
            if row is None:
                return None
            if row["request_digest"] != digest:
                _fail("idempotency_conflict")
            return json.loads(row["response_json"])
        except WorkspaceExecutionError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            connection.close()

    def remember(
        self, *, project_id: str, workspace_id: str, scope: str,
        idempotency_key: object, request: object, response: object,
    ) -> None:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        key = _idempotency_key(idempotency_key)
        digest = _digest(request)
        connection = _write_txn(self.path)
        try:
            existing = connection.execute(
                "SELECT request_digest FROM idempotency_records "
                "WHERE project_id=? AND workspace_id=? AND scope=? AND idempotency_key=?",
                (project_id, workspace_id, scope, key),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != digest:
                    _fail("idempotency_conflict")
                connection.execute("COMMIT")
                return
            _remember(
                connection, project_id, workspace_id, scope, key, digest, response,
            )
            connection.execute("COMMIT")
        except WorkspaceExecutionError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            _fail("store_write_failed", exc)
        finally:
            connection.close()

    def claim_prepare(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        identity_id: str,
    ) -> PrepareClaim:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        identity_id = _opaque(identity_id, "idn_")
        connection = _write_txn(self.path)
        try:
            identity = connection.execute(
                "SELECT * FROM agent_identities WHERE project_id=? AND workspace_id=? "
                "AND identity_id=?",
                (project_id, workspace_id, identity_id),
            ).fetchone()
            if identity is None:
                _fail("identity_not_found")
            prep = connection.execute(
                "SELECT * FROM work_item_preparations WHERE project_id=? "
                "AND workspace_id=? AND work_item_id=?",
                (project_id, workspace_id, work_item_id),
            ).fetchone()
            if prep is not None:
                if prep["identity_id"] != identity_id:
                    _fail("checkout_conflict")
                if prep["state"] == "preparing":
                    connection.execute("COMMIT")
                    return PrepareClaim("pending", prep["checkout_id"], None)
                view = _load_preparation(
                    connection, project_id, workspace_id, work_item_id,
                )
                connection.execute("COMMIT")
                return PrepareClaim("ready", prep["checkout_id"], view)
            now = _now()
            checkout_id = _new_id("chk_")
            connection.execute(
                "INSERT INTO work_item_preparations VALUES "
                "(?,?,?,?,?,1,?,NULL,NULL,'preparing',1,?,?)",
                (
                    _new_id("pre_"), project_id, workspace_id, work_item_id,
                    identity_id, checkout_id, now, now,
                ),
            )
            connection.execute("COMMIT")
            return PrepareClaim("created", checkout_id, None)
        except WorkspaceExecutionError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            _fail("store_write_failed", exc)
        finally:
            connection.close()
        raise AssertionError("unreachable")

    def abort_prepare(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        checkout_id: str,
    ) -> None:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        checkout_id = _opaque(checkout_id, "chk_")
        connection = _write_txn(self.path)
        try:
            connection.execute(
                "DELETE FROM work_item_preparations WHERE project_id=? "
                "AND workspace_id=? AND work_item_id=? AND state='preparing' "
                "AND checkout_id=?",
                (project_id, workspace_id, work_item_id, checkout_id),
            )
            connection.execute("COMMIT")
        except WorkspaceExecutionError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            _fail("store_write_failed", exc)
        finally:
            connection.close()

    def complete_preparation(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        identity_id: str, source_head: str, source_tree: str, internal_path: str,
        operation_id: str | None,
    ) -> PreparationView:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        identity_id = _opaque(identity_id, "idn_")
        if not isinstance(source_head, str) or not isinstance(source_tree, str):
            _fail("invalid_argument")
        if not isinstance(internal_path, str) or not internal_path.startswith("/"):
            _fail("invalid_argument")
        connection = _write_txn(self.path)
        try:
            prep = connection.execute(
                "SELECT * FROM work_item_preparations WHERE project_id=? "
                "AND workspace_id=? AND work_item_id=?",
                (project_id, workspace_id, work_item_id),
            ).fetchone()
            if prep is not None and prep["identity_id"] != identity_id:
                _fail("checkout_conflict")
            if prep is not None and prep["state"] != "preparing":
                view = _load_preparation(
                    connection, project_id, workspace_id, work_item_id,
                )
                assert view is not None
                connection.execute("COMMIT")
                return view
            identity = connection.execute(
                "SELECT * FROM agent_identities WHERE project_id=? AND workspace_id=? "
                "AND identity_id=?",
                (project_id, workspace_id, identity_id),
            ).fetchone()
            if identity is None:
                _fail("identity_not_found")
            now = _now()
            lease_id = _new_id("les_")
            if prep is None:
                preparation_id = _new_id("pre_")
                checkout_id = _new_id("chk_")
                connection.execute(
                    "INSERT INTO work_item_preparations VALUES "
                    "(?,?,?,?,?,1,?,?,NULL,'prepared',1,?,?)",
                    (
                        preparation_id, project_id, workspace_id, work_item_id,
                        identity_id, checkout_id, lease_id, now, now,
                    ),
                )
            else:
                preparation_id = prep["preparation_id"]
                checkout_id = prep["checkout_id"]
                if connection.execute(
                    "UPDATE work_item_preparations SET lease_id=?, state='prepared', "
                    "revision=revision+1, updated_at=? WHERE preparation_id=? "
                    "AND state='preparing'",
                    (lease_id, now, preparation_id),
                ).rowcount != 1:
                    _fail("checkout_conflict")
            fence = "sha256:" + _digest({
                "checkout_id": checkout_id, "identity_id": identity_id,
                "generation": 1, "nonce": secrets.token_hex(16),
            })
            connection.execute(
                "INSERT INTO managed_checkouts VALUES (?,?,?,?,?,?,?,?,1,?)",
                (
                    checkout_id, preparation_id, source_head, source_tree,
                    internal_path, None,
                    _canonical({"operation_id": operation_id, "source_clean": True}),
                    "ready", now,
                ),
            )
            connection.execute(
                "INSERT INTO writer_leases VALUES (?,?,?,?, 'reserved',?,1,?)",
                (lease_id, checkout_id, identity_id, 1, fence, now),
            )
            view = _load_preparation(
                connection, project_id, workspace_id, work_item_id,
            )
            assert view is not None
            connection.execute("COMMIT")
            return view
        except WorkspaceExecutionError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            _fail("store_write_failed", exc)
        finally:
            connection.close()
        raise AssertionError("unreachable")

    def begin_attach(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        expected_revision: int, session_name: str,
    ) -> tuple[PreparationView, AttachmentInternal, CheckoutInternal]:
        return self._transition_runtime(
            project_id, workspace_id, work_item_id, expected_revision,
            action="attach", session_name=session_name,
        )

    def finish_attach(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        expected_revision: int, pane_id: str, instance_id: str,
        native_receipt: str, identity_verified: bool,
    ) -> PreparationView:
        if identity_verified is not True:
            _fail("runtime_identity_unverified")
        return self._complete_runtime(
            project_id, workspace_id, work_item_id, expected_revision,
            status="connected_readonly", pane_id=pane_id, instance_id=instance_id,
            native_receipt=_canonical({
                "identity_verified": True, "receipt": native_receipt,
            }),
        )

    def begin_detach(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        expected_revision: int,
    ) -> tuple[PreparationView, AttachmentInternal, CheckoutInternal]:
        return self._transition_runtime(
            project_id, workspace_id, work_item_id, expected_revision,
            action="detach", session_name=None,
        )

    def finish_detach(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        expected_revision: int,
    ) -> PreparationView:
        return self._complete_runtime(
            project_id, workspace_id, work_item_id, expected_revision,
            status="detached", pane_id=None, instance_id=None, native_receipt=None,
        )

    def mark_unknown(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        expected_revision: int,
    ) -> PreparationView:
        return self._complete_runtime(
            project_id, workspace_id, work_item_id, expected_revision,
            status="outcome_unknown", pane_id=None, instance_id=None,
            native_receipt=None,
        )

    def fail_attach(
        self, *, project_id: str, workspace_id: str, work_item_id: str,
        expected_revision: int,
    ) -> PreparationView:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        expected_revision = _revision(expected_revision)
        connection = _write_txn(self.path)
        try:
            prep = connection.execute(
                "SELECT * FROM work_item_preparations WHERE project_id=? "
                "AND workspace_id=? AND work_item_id=?",
                (project_id, workspace_id, work_item_id),
            ).fetchone()
            if prep is None:
                _fail("preparation_not_found")
            if int(prep["revision"]) != expected_revision:
                _fail("stale_revision")
            if prep["state"] != "attaching":
                if prep["state"] == "prepared":
                    view = _load_preparation(
                        connection, project_id, workspace_id, work_item_id,
                    )
                    connection.execute("COMMIT")
                    assert view is not None
                    return view
                _fail("lease_conflict")
            now = _now()
            new_revision = expected_revision + 1
            if prep["attachment_id"]:
                connection.execute(
                    "UPDATE runtime_attachments SET status='detached', "
                    "revision=revision+1, updated_at=? WHERE attachment_id=?",
                    (now, prep["attachment_id"]),
                )
            if connection.execute(
                "UPDATE work_item_preparations SET state='prepared', revision=?, "
                "updated_at=? WHERE preparation_id=? AND revision=? "
                "AND state='attaching'",
                (new_revision, now, prep["preparation_id"], expected_revision),
            ).rowcount != 1:
                _fail("stale_revision")
            view = _load_preparation(
                connection, project_id, workspace_id, work_item_id,
            )
            assert view is not None
            connection.execute("COMMIT")
            return view
        except WorkspaceExecutionError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            _fail("store_write_failed", exc)
        finally:
            connection.close()
        raise AssertionError("unreachable")

    def _transition_runtime(
        self, project_id: str, workspace_id: str, work_item_id: str,
        expected_revision: int, *, action: str, session_name: str | None,
    ) -> tuple[PreparationView, AttachmentInternal, CheckoutInternal]:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        expected_revision = _revision(expected_revision)
        connection = _write_txn(self.path)
        try:
            prep = connection.execute(
                "SELECT * FROM work_item_preparations WHERE project_id=? "
                "AND workspace_id=? AND work_item_id=?",
                (project_id, workspace_id, work_item_id),
            ).fetchone()
            if prep is None:
                _fail("preparation_not_found")
            if int(prep["revision"]) != expected_revision:
                _fail("stale_revision")
            checkout = connection.execute(
                "SELECT * FROM managed_checkouts WHERE checkout_id=?",
                (prep["checkout_id"],),
            ).fetchone()
            if checkout is None:
                _fail("checkout_conflict")
            now = _now()
            new_revision = expected_revision + 1
            if action == "attach":
                if prep["state"] not in {
                    "prepared", "detached", "outcome_unknown", "connected_readonly",
                }:
                    _fail("lease_conflict")
                if prep["state"] == "connected_readonly":
                    view = _load_preparation(
                        connection, project_id, workspace_id, work_item_id,
                    )
                    assert view is not None
                    attachment = connection.execute(
                        "SELECT * FROM runtime_attachments WHERE attachment_id=?",
                        (prep["attachment_id"],),
                    ).fetchone()
                    connection.execute("COMMIT")
                    return (
                        view,
                        AttachmentInternal(
                            attachment["attachment_id"], attachment["pane_id"],
                            attachment["instance_id"], attachment["session_name"],
                            int(attachment["generation"]), attachment["status"],
                        ),
                        CheckoutInternal(
                            checkout["checkout_id"], checkout["internal_path"],
                            checkout["source_head"], checkout["source_tree"],
                        ),
                    )
                generation = int(prep["generation"]) + (
                    0 if prep["state"] == "prepared" else 1
                )
                if prep["state"] == "prepared":
                    generation = int(prep["generation"])
                if prep["lease_id"]:
                    lease = connection.execute(
                        "SELECT * FROM writer_leases WHERE lease_id=?",
                        (prep["lease_id"],),
                    ).fetchone()
                    if lease is None or lease["status"] != "reserved":
                        if prep["state"] != "detached":
                            _fail("lease_conflict")
                if prep["state"] == "detached":
                    lease_id = _new_id("les_")
                    fence = "sha256:" + _digest({
                        "checkout_id": checkout["checkout_id"],
                        "identity_id": prep["identity_id"],
                        "generation": generation,
                        "nonce": secrets.token_hex(16),
                    })
                    connection.execute(
                        "INSERT INTO writer_leases VALUES (?,?,?,?, 'reserved',?,1,?)",
                        (
                            lease_id, checkout["checkout_id"], prep["identity_id"],
                            generation, fence, now,
                        ),
                    )
                else:
                    lease_id = prep["lease_id"]
                attachment_id = _new_id("att_")
                connection.execute(
                    "INSERT INTO runtime_attachments VALUES "
                    "(?,?,?,?,?,?,?,?,?,?, 'attaching',1,?,?)",
                    (
                        attachment_id, prep["identity_id"], generation,
                        checkout["checkout_id"], PROVIDER, HARNESS, None, None,
                        session_name, None, now, now,
                    ),
                )
                if connection.execute(
                    "UPDATE work_item_preparations SET generation=?, lease_id=?, "
                    "attachment_id=?, state='attaching', revision=?, updated_at=? "
                    "WHERE preparation_id=? AND revision=?",
                    (
                        generation, lease_id, attachment_id, new_revision, now,
                        prep["preparation_id"], expected_revision,
                    ),
                ).rowcount != 1:
                    _fail("stale_revision")
                internal = AttachmentInternal(
                    attachment_id, None, None, session_name, generation, "attaching",
                )
            else:
                if prep["state"] != "connected_readonly" or not prep["attachment_id"]:
                    _fail("lease_conflict")
                attachment = connection.execute(
                    "SELECT * FROM runtime_attachments WHERE attachment_id=?",
                    (prep["attachment_id"],),
                ).fetchone()
                if attachment is None:
                    _fail("lease_conflict")
                if connection.execute(
                    "UPDATE runtime_attachments SET status='detaching', "
                    "revision=revision+1, updated_at=? WHERE attachment_id=?",
                    (now, attachment["attachment_id"]),
                ).rowcount != 1:
                    _fail("store_write_failed")
                if prep["lease_id"]:
                    if connection.execute(
                        "UPDATE writer_leases SET status='revoked', "
                        "revision=revision+1 WHERE lease_id=? AND status='reserved'",
                        (prep["lease_id"],),
                    ).rowcount != 1:
                        _fail("lease_conflict")
                if connection.execute(
                    "UPDATE work_item_preparations SET state='detaching', "
                    "revision=?, updated_at=? WHERE preparation_id=? AND revision=?",
                    (
                        new_revision, now, prep["preparation_id"], expected_revision,
                    ),
                ).rowcount != 1:
                    _fail("stale_revision")
                internal = AttachmentInternal(
                    attachment["attachment_id"], attachment["pane_id"],
                    attachment["instance_id"], attachment["session_name"],
                    int(attachment["generation"]), "detaching",
                )
            view = _load_preparation(connection, project_id, workspace_id, work_item_id)
            assert view is not None
            connection.execute("COMMIT")
            return (
                view, internal,
                CheckoutInternal(
                    checkout["checkout_id"], checkout["internal_path"],
                    checkout["source_head"], checkout["source_tree"],
                ),
            )
        except WorkspaceExecutionError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            _fail("store_write_failed", exc)
        finally:
            connection.close()
        raise AssertionError("unreachable")

    def _complete_runtime(
        self, project_id: str, workspace_id: str, work_item_id: str,
        expected_revision: int, *, status: str, pane_id: str | None,
        instance_id: str | None, native_receipt: str | None,
    ) -> PreparationView:
        project_id = _opaque(project_id, "prj_")
        workspace_id = _opaque(workspace_id, "ws_")
        work_item_id = _opaque(work_item_id, "wrk_")
        expected_revision = _revision(expected_revision)
        connection = _write_txn(self.path)
        try:
            prep = connection.execute(
                "SELECT * FROM work_item_preparations WHERE project_id=? "
                "AND workspace_id=? AND work_item_id=?",
                (project_id, workspace_id, work_item_id),
            ).fetchone()
            if prep is None:
                _fail("preparation_not_found")
            if int(prep["revision"]) != expected_revision:
                _fail("stale_revision")
            now = _now()
            new_revision = expected_revision + 1
            prep_state = (
                "connected_readonly" if status == "connected_readonly"
                else "detached" if status == "detached"
                else "outcome_unknown"
            )
            if prep["attachment_id"]:
                connection.execute(
                    "UPDATE runtime_attachments SET status=?, pane_id=COALESCE(?, pane_id), "
                    "instance_id=COALESCE(?, instance_id), native_receipt=COALESCE(?, native_receipt), "
                    "revision=revision+1, updated_at=? WHERE attachment_id=?",
                    (
                        status, pane_id, instance_id, native_receipt, now,
                        prep["attachment_id"],
                    ),
                )
            if connection.execute(
                "UPDATE work_item_preparations SET state=?, revision=?, updated_at=? "
                "WHERE preparation_id=? AND revision=?",
                (
                    prep_state, new_revision, now, prep["preparation_id"],
                    expected_revision,
                ),
            ).rowcount != 1:
                _fail("stale_revision")
            view = _load_preparation(connection, project_id, workspace_id, work_item_id)
            assert view is not None
            connection.execute("COMMIT")
            return view
        except WorkspaceExecutionError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            _fail("store_write_failed", exc)
        finally:
            connection.close()
        raise AssertionError("unreachable")


def _replay(
    connection: sqlite3.Connection, project_id: str, workspace_id: str,
    scope: str, key: str, digest: str,
) -> dict[str, object] | None:
    existing = connection.execute(
        "SELECT request_digest, response_json FROM idempotency_records "
        "WHERE project_id=? AND workspace_id=? AND scope=? AND idempotency_key=?",
        (project_id, workspace_id, scope, key),
    ).fetchone()
    if existing is None:
        return None
    if existing["request_digest"] != digest:
        _fail("idempotency_conflict")
    return json.loads(existing["response_json"])


def _remember(
    connection: sqlite3.Connection, project_id: str, workspace_id: str,
    scope: str, key: str, digest: str, response: object,
) -> None:
    connection.execute(
        "INSERT INTO idempotency_records VALUES (?,?,?,?,?,?)",
        (project_id, workspace_id, scope, key, digest, _canonical(response)),
    )


def _load_preparation(
    connection: sqlite3.Connection, project_id: str, workspace_id: str,
    work_item_id: str,
) -> PreparationView | None:
    prep = connection.execute(
        "SELECT * FROM work_item_preparations WHERE project_id=? AND workspace_id=? "
        "AND work_item_id=?",
        (project_id, workspace_id, work_item_id),
    ).fetchone()
    if prep is None:
        return None
    identity = connection.execute(
        "SELECT * FROM agent_identities WHERE identity_id=?",
        (prep["identity_id"],),
    ).fetchone()
    checkout = None
    if prep["checkout_id"]:
        checkout = connection.execute(
            "SELECT * FROM managed_checkouts WHERE checkout_id=?",
            (prep["checkout_id"],),
        ).fetchone()
    lease = None
    if prep["lease_id"]:
        lease = connection.execute(
            "SELECT * FROM writer_leases WHERE lease_id=?",
            (prep["lease_id"],),
        ).fetchone()
    attachment = None
    if prep["attachment_id"]:
        attachment = connection.execute(
            "SELECT * FROM runtime_attachments WHERE attachment_id=?",
            (prep["attachment_id"],),
        ).fetchone()
    if identity is None:
        _fail("store_corrupt")
    return PreparationView(
        prep["work_item_id"], prep["state"], int(prep["revision"]), "unassigned",
        _identity_view(identity),
        {"identity_id": prep["identity_id"], "generation": int(prep["generation"])},
        _checkout_view(checkout), _lease_view(lease), _attachment_view(attachment),
    )


def initialize(path: Path) -> WorkspaceExecutionStore:
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
    except WorkspaceExecutionError:
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
    return WorkspaceExecutionStore(path)


def open_existing(path: Path) -> WorkspaceExecutionStore:
    path = _path(path)
    _leaf(path, missing="workspace_execution_schema_missing")
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(path, write=False)
        _validate_schema(connection)
    except sqlite3.DatabaseError as exc:
        _fail("store_corrupt", exc)
    finally:
        if connection is not None:
            connection.close()
    return WorkspaceExecutionStore(path)
