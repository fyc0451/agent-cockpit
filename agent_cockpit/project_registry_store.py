"""Strict, dormant SQLite repository for Project Registry v1."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import project_registry_contracts as contracts
from . import project_registry_domain as domain


class ProjectRegistryError(RuntimeError):
    """Sanitized product error; ``code`` is the stable public contract."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str, cause: BaseException | None = None):
    error = ProjectRegistryError(code)
    if cause is None:
        raise error
    raise error from cause


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return prefix + secrets.token_hex(16)


def _validate(callable_, *args, **kwargs):
    try:
        return callable_(*args, **kwargs)
    except ValueError as exc:
        _fail("invalid_argument", exc)


def _canonical_sql(value: str | None) -> str:
    return " ".join((value or "").split()).lower()


def _schema_objects(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (str(kind), str(name), str(table), _canonical_sql(sql))
        for kind, name, table, sql in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    )


def _expected_schema_objects() -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in contracts.SCHEMA_STATEMENTS:
            connection.execute(statement)
        return _schema_objects(connection)
    finally:
        connection.close()


_EXPECTED_SCHEMA_OBJECTS = _expected_schema_objects()


def _absolute_path(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        _fail("store_unsafe")
    return path


def _reject_symlink_components(path: Path) -> None:
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


def _validate_directory(path: Path, *, created: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        _fail("store_unsafe", exc)
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or mode & 0o022
        or (created and mode != 0o700)
    ):
        _fail("store_unsafe")


def _prepare_parent(path: Path, *, create: bool) -> None:
    _reject_symlink_components(path)
    anchor = path.parent
    missing: list[Path] = []
    while True:
        try:
            anchor.lstat()
            break
        except FileNotFoundError:
            missing.append(anchor)
            if anchor.parent == anchor:
                _fail("store_unsafe")
            anchor = anchor.parent
        except OSError as exc:
            _fail("store_unsafe", exc)
    _validate_directory(anchor)
    if missing and not create:
        _fail("schema_missing")
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
            os.chmod(directory, 0o700)
        except OSError as exc:
            _fail("store_unsafe", exc)
        _validate_directory(directory, created=True)
    _reject_symlink_components(path)


FileSignature = tuple[int, int, int, int, int]
SidecarSignature = tuple[int, int, int, int, int, int]


def _leaf_signature(path: Path, *, must_exist: bool) -> FileSignature:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if must_exist:
            _fail("schema_missing")
        return ()
    except OSError as exc:
        _fail("store_unsafe", exc)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        _fail("store_unsafe")
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink)


def _check_path(path: Path, *, must_exist: bool) -> FileSignature:
    try:
        _prepare_parent(path, create=False)
        return _leaf_signature(path, must_exist=must_exist)
    except ProjectRegistryError:
        raise
    except OSError as exc:
        _fail("store_unsafe", exc)


def _create_leaf(path: Path) -> FileSignature:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        _fail("store_unsafe")
    except OSError as exc:
        _fail("store_unsafe", exc)
    try:
        os.fchmod(fd, 0o600)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
        ):
            _fail("store_unsafe")
        opened_signature = (
            opened.st_dev, opened.st_ino, opened.st_uid,
            opened.st_mode, opened.st_nlink,
        )
    finally:
        os.close(fd)
    _reject_symlink_components(path)
    if _leaf_signature(path, must_exist=True) != opened_signature:
        _fail("store_unsafe")
    return opened_signature


def _sidecar_paths(path: Path) -> tuple[Path, Path, Path]:
    return (
        Path(f"{path}-journal"),
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    )


def _snapshot_sidecars(path: Path) -> dict[Path, SidecarSignature]:
    snapshot: dict[Path, SidecarSignature] = {}
    for sidecar in _sidecar_paths(path):
        try:
            info = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            _fail("store_unsafe", exc)
        snapshot[sidecar] = (
            info.st_dev, info.st_ino, info.st_uid, info.st_mode,
            info.st_nlink, info.st_size,
        )
    return snapshot


def _require_no_sidecars(path: Path) -> None:
    current = _snapshot_sidecars(path)
    if current:
        _fail("store_unsafe")


def _require_leaf_signature(
    path: Path, expected: FileSignature
) -> None:
    if _check_path(path, must_exist=True) != expected:
        _fail("store_unsafe")


def _connect_write(path: Path) -> sqlite3.Connection:
    before = _check_path(path, must_exist=True)
    connection = sqlite3.connect(path, isolation_level=None, timeout=5.0)
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


def _connect_read(path: Path) -> sqlite3.Connection:
    before = _check_path(path, must_exist=True)
    uri = path.as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
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


def _fsync_file(path: Path, expected: FileSignature) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        _fail("store_unsafe", exc)
    try:
        info = os.fstat(fd)
        actual = (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink)
        if actual != expected:
            _fail("store_unsafe")
        os.fsync(fd)
    except OSError as exc:
        _fail("store_unsafe", exc)
    finally:
        os.close(fd)
    _require_leaf_signature(path, expected)


def _fsync_directory(path: Path) -> None:
    _validate_directory(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        _fail("store_unsafe", exc)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            _fail("store_unsafe")
        os.fsync(fd)
    except OSError as exc:
        _fail("store_unsafe", exc)
    finally:
        os.close(fd)
    _validate_directory(path)


def _publish_noreplace(source: Path, destination: Path) -> bool:
    """Atomically rename a complete same-directory temp without overwrite."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError:
            _fail("store_unsafe")
        rename.argtypes = (
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        result = rename(
            -100, os.fsencode(source), -100, os.fsencode(destination), 1
        )
    elif sys.platform == "darwin":
        try:
            rename = libc.renamex_np
        except AttributeError:
            _fail("store_unsafe")
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(source), os.fsencode(destination), 0x00000004)
    else:
        _fail("store_unsafe")
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        return False
    _fail("store_unsafe", OSError(error_number, os.strerror(error_number)))


def _validate_schema(connection: sqlite3.Connection) -> None:
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _fail("store_corrupt", exc)
    if version > contracts.SCHEMA_VERSION:
        _fail("future_schema")
    if version < contracts.SCHEMA_VERSION:
        _fail("migration_required")
    try:
        if _schema_objects(connection) != _EXPECTED_SCHEMA_OBJECTS:
            _fail("schema_fingerprint_mismatch")
        receipt = connection.execute(
            "SELECT migration_id, schema_version, schema_digest FROM schema_migrations"
        ).fetchall()
        if [tuple(row) for row in receipt] != [(
            contracts.MIGRATION_ID, contracts.SCHEMA_VERSION, contracts.SCHEMA_DIGEST,
        )]:
            _fail("schema_fingerprint_mismatch")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            _fail("store_corrupt")
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if not quick or str(quick[0]).lower() != "ok":
            _fail("store_corrupt")
    except ProjectRegistryError:
        raise
    except sqlite3.DatabaseError as exc:
        _fail("store_corrupt", exc)


def _after_schema_hook(_connection: sqlite3.Connection) -> None:
    """Test injection point inside the migration transaction."""


class ProjectRegistryStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def close(self) -> None:
        pass

    def _transaction(self):
        return _Transaction(self.path)

    def create_project(
        self, *, slug: str, display_name: str, goal: str | None
    ) -> domain.ProjectRecord:
        slug = _validate(domain.slug, slug)
        display_name = _validate(domain.text, display_name, maximum=256)
        goal = _validate(domain.optional_text, goal, maximum=4096)
        with self._transaction() as connection:
            return self._insert_project(connection, slug, display_name, goal)

    @staticmethod
    def _insert_project(
        connection: sqlite3.Connection, slug: str, display_name: str, goal: str | None
    ) -> domain.ProjectRecord:
        project_id = _id("prj_")
        now = _now()
        try:
            connection.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, 'active', 1, ?, ?)",
                (project_id, slug, display_name, goal, now, now),
            )
        except sqlite3.IntegrityError as exc:
            _fail("project_slug_conflict", exc)
        return domain.ProjectRecord(
            project_id, slug, display_name, goal, "active", 1, now, now
        )

    def add_repo_location(
        self, *, project_id: str, node_id: str, canonical_path: str,
        vcs_kind: str, availability: str,
    ) -> domain.RepoLocationRecord:
        project_id = _validate(domain.opaque, project_id, maximum=64)
        node_id = _validate(domain.opaque, node_id, maximum=128)
        canonical_path = _validate(domain.canonical_path, canonical_path)
        vcs_kind = _validate(domain.enum, vcs_kind, frozenset({"git", "none"}))
        availability = _validate(
            domain.enum, availability,
            frozenset({"available", "offline", "missing", "unknown"}),
        )
        location_id = _id("loc_")
        now = _now()
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
            ).fetchone() is None:
                _fail("project_not_found")
            try:
                connection.execute(
                    "INSERT INTO repo_locations "
                    "(repo_location_id, project_id, node_id, canonical_path, lifecycle, "
                    "vcs_kind, git_root, git_remote_fingerprint, default_ref_observed, "
                    "availability, version, created_at, updated_at) VALUES "
                    "(?, ?, ?, ?, 'active', ?, NULL, NULL, NULL, ?, 1, ?, ?)",
                    (location_id, project_id, node_id, canonical_path, vcs_kind,
                     availability, now, now),
                )
            except sqlite3.IntegrityError as exc:
                _fail("location_already_registered", exc)
        return domain.RepoLocationRecord(
            location_id, project_id, node_id, canonical_path, "active", vcs_kind,
            availability, 1,
        )

    def create_workspace(
        self, *, project_id: str, repo_location_id: str, name: str,
        goal: str | None, isolation_kind: str,
    ) -> domain.WorkspaceRecord:
        project_id = _validate(domain.opaque, project_id, maximum=64)
        repo_location_id = _validate(domain.opaque, repo_location_id, maximum=64)
        name = _validate(domain.text, name, maximum=256)
        goal = _validate(domain.optional_text, goal, maximum=4096)
        isolation_kind = _validate(
            domain.enum, isolation_kind,
            frozenset({"shared", "isolated_worktree", "review_detached"}),
        )
        workspace_id = _id("ws_")
        now = _now()
        with self._transaction() as connection:
            owner = connection.execute(
                "SELECT 1 FROM repo_locations WHERE project_id=? "
                "AND repo_location_id=? AND lifecycle='active'",
                (project_id, repo_location_id),
            ).fetchone()
            if owner is None:
                _fail("repo_location_not_found")
            try:
                connection.execute(
                    "INSERT INTO workspaces VALUES "
                    "(?, ?, ?, ?, ?, ?, 'active', NULL, 1, ?, ?)",
                    (workspace_id, project_id, repo_location_id, name, goal,
                     isolation_kind, now, now),
                )
            except sqlite3.IntegrityError as exc:
                _fail("workspace_name_conflict", exc)
        return domain.WorkspaceRecord(
            workspace_id, project_id, repo_location_id, name, goal,
            isolation_kind, "active", None, 1, now, now,
        )

    def bind_legacy_source(
        self, *, project_id: str, source_kind: str, source_key: str,
        source_digest: str,
    ) -> domain.LegacyBindingRecord:
        project_id = _validate(domain.opaque, project_id, maximum=64)
        source_kind = _validate(domain.enum, source_kind, domain.LEGACY_SOURCE_KINDS)
        source_key = _validate(domain.text, source_key, maximum=4096)
        source_digest = _validate(domain.text, source_digest, maximum=256)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM legacy_project_bindings "
                "WHERE source_kind=? AND source_key=?",
                (source_kind, source_key),
            ).fetchone()
            if row is not None:
                if row["project_id"] != project_id or row["source_digest"] != source_digest:
                    _fail("legacy_binding_conflict")
                return _binding_record(row)
            if connection.execute(
                "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
            ).fetchone() is None:
                _fail("project_not_found")
            binding_id = _id("bnd_")
            imported_at = _now()
            try:
                connection.execute(
                    "INSERT INTO legacy_project_bindings VALUES (?, ?, ?, ?, ?, ?)",
                    (binding_id, project_id, source_kind, source_key,
                     source_digest, imported_at),
                )
            except sqlite3.IntegrityError as exc:
                _fail("legacy_binding_conflict", exc)
            return domain.LegacyBindingRecord(
                binding_id, project_id, source_kind, source_key,
                source_digest, imported_at,
            )

    def idempotent_create_project(
        self, *, scope: str, idempotency_key: str, payload: dict[str, object]
    ) -> domain.CommandResult:
        scope = _validate(domain.text, scope, maximum=128)
        idempotency_key = _validate(domain.text, idempotency_key, maximum=128)
        request_json = _validate(domain.canonical_json, payload)
        request_digest = hashlib.sha256(request_json.encode("ascii")).hexdigest()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT request_digest, status_code, response_json "
                "FROM idempotency_records WHERE scope=? AND idempotency_key=?",
                (scope, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    _fail("idempotency_conflict")
                return domain.CommandResult(
                    int(existing["status_code"]), json.loads(existing["response_json"])
                )
            if not isinstance(payload, dict):
                _fail("invalid_argument")
            if set(payload) - {"slug", "display_name", "goal"}:
                _fail("invalid_argument")
            slug = _validate(domain.slug, payload.get("slug"))
            display_name = _validate(
                domain.text, payload.get("display_name", slug), maximum=256
            )
            goal = _validate(domain.optional_text, payload.get("goal"), maximum=4096)
            project = self._insert_project(connection, slug, display_name, goal)
            response = {"project_id": project.project_id, "slug": project.slug}
            response_json = domain.canonical_json(response)
            connection.execute(
                "INSERT INTO idempotency_records VALUES (?, ?, ?, 201, ?, ?)",
                (scope, idempotency_key, request_digest, response_json, _now()),
            )
            return domain.CommandResult(201, response)


class _Transaction:
    def __init__(self, path: Path):
        self.path = path
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        _check_path(self.path, must_exist=True)
        self.connection = _connect_write(self.path)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            return self.connection
        except BaseException:
            self.connection.close()
            self.connection = None
            raise

    def __exit__(self, exc_type, exc, traceback) -> bool:
        assert self.connection is not None
        try:
            self.connection.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.connection.close()
        return False


def _binding_record(row: sqlite3.Row) -> domain.LegacyBindingRecord:
    return domain.LegacyBindingRecord(
        row["binding_id"], row["project_id"], row["source_kind"],
        row["source_key"], row["source_digest"], row["imported_at"],
    )


def initialize(path: Path) -> ProjectRegistryStore:
    path = _absolute_path(path)
    _prepare_parent(path, create=True)
    if _snapshot_sidecars(path):
        _fail("store_unsafe")
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _fail("store_unsafe", exc)
    else:
        return open_existing(path)

    temp_path = path.parent / f".{path.name}.init-{secrets.token_hex(16)}.tmp"
    temp_signature = _create_leaf(temp_path)

    connection: sqlite3.Connection | None = None
    try:
        _require_no_sidecars(temp_path)
        _require_leaf_signature(temp_path, temp_signature)
        connection = _connect_write(temp_path)
        _require_leaf_signature(temp_path, temp_signature)
    except BaseException:
        if connection is not None:
            connection.close()
        raise
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in contracts.SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
            (contracts.MIGRATION_ID, contracts.SCHEMA_VERSION,
             contracts.SCHEMA_DIGEST, _now()),
        )
        connection.execute(f"PRAGMA user_version={contracts.SCHEMA_VERSION}")
        _after_schema_hook(connection)
        _validate_schema(connection)
        _require_leaf_signature(temp_path, temp_signature)
        connection.execute("COMMIT")
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()

    _require_leaf_signature(temp_path, temp_signature)
    _require_no_sidecars(temp_path)
    _fsync_file(temp_path, temp_signature)
    _require_no_sidecars(path)
    published = _publish_noreplace(temp_path, path)
    if published:
        _require_leaf_signature(path, temp_signature)
        _require_no_sidecars(path)
        _fsync_directory(path.parent)
        return ProjectRegistryStore(path)
    winner = open_existing(path)
    _fsync_directory(path.parent)
    return winner


def open_existing(path: Path) -> ProjectRegistryStore:
    path = _absolute_path(path)
    _check_path(path, must_exist=True)
    _require_no_sidecars(path)
    try:
        connection = _connect_read(path)
    except sqlite3.DatabaseError as exc:
        _fail("store_corrupt", exc)
    try:
        _validate_schema(connection)
    finally:
        connection.close()
    return ProjectRegistryStore(path)
