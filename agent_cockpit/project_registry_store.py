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
    try:
        raise error from None
    except ProjectRegistryError:
        error.__cause__ = None
        error.__context__ = None
        error.__suppress_context__ = True
        raise


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


def _connect_live_read(path: Path) -> sqlite3.Connection:
    before = _check_path(path, must_exist=True)
    uri = path.as_uri() + "?mode=ro"
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


def _after_legacy_binding_insert(
    _connection: sqlite3.Connection, _binding: domain.LegacyBindingRecord,
) -> None:
    """Test injection point inside the legacy import transaction."""


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

    def get_project_by_slug(self, slug: str) -> domain.ProjectRecord | None:
        slug = _validate(domain.slug, slug)
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_live_read(self.path)
            row = connection.execute(
                "SELECT * FROM projects WHERE slug=?", (slug,)
            ).fetchone()
            return None if row is None else _project_record(row)
        except ProjectRegistryError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()

    def get_project_by_id(self, project_id: str) -> domain.ProjectSnapshot | None:
        project_id = _validate(domain.opaque, project_id, maximum=64)
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_live_read(self.path)
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if row is None:
                return None
            return _project_snapshot(connection, row)
        except ProjectRegistryError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()

    def list_projects(
        self, *, lifecycle: str = "active", after_project_id: str | None = None,
        limit: int = 50,
    ) -> domain.ProjectPage:
        lifecycle = _validate(
            domain.enum, lifecycle, frozenset({"active", "archived"}),
        )
        if after_project_id is not None:
            after_project_id = _validate(
                domain.opaque, after_project_id, maximum=64,
            )
        if type(limit) is not int or not 1 <= limit <= 100:
            _fail("invalid_argument")
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_live_read(self.path)
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT * FROM projects WHERE lifecycle=? "
                "AND (? IS NULL OR project_id > ?) "
                "ORDER BY project_id LIMIT ?",
                (lifecycle, after_project_id, after_project_id, limit + 1),
            ).fetchall()
            visible = rows[:limit]
            return domain.ProjectPage(
                tuple(_project_snapshot(connection, row) for row in visible),
                str(visible[-1]["project_id"]) if len(rows) > limit else None,
            )
        except ProjectRegistryError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()

    def list_repo_locations(
        self, project_id: str,
    ) -> tuple[domain.RepoLocationRecord, ...] | None:
        project_id = _validate(domain.opaque, project_id, maximum=64)
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_live_read(self.path)
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if row is None:
                return None
            rows = connection.execute(
                "SELECT * FROM repo_locations WHERE project_id=? "
                "ORDER BY repo_location_id", (project_id,),
            ).fetchall()
            return tuple(_repo_location_record(row) for row in rows)
        except ProjectRegistryError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()

    def list_workspaces(
        self, project_id: str,
    ) -> tuple[domain.WorkspaceRecord, ...] | None:
        project_id = _validate(domain.opaque, project_id, maximum=64)
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_live_read(self.path)
            connection.execute("BEGIN")
            project = connection.execute(
                "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if project is None:
                return None
            rows = connection.execute(
                "SELECT * FROM workspaces WHERE project_id=? ORDER BY workspace_id",
                (project_id,),
            ).fetchall()
            return tuple(_workspace_record(row) for row in rows)
        except ProjectRegistryError:
            raise
        except (sqlite3.Error, IndexError, KeyError, TypeError, ValueError) as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()

    def get_workspace(
        self, project_id: str, workspace_id: str,
    ) -> domain.WorkspaceRecord | None:
        project_id = _validate(domain.opaque, project_id, maximum=64)
        workspace_id = _validate(domain.opaque, workspace_id, maximum=64)
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_live_read(self.path)
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM workspaces WHERE project_id=? AND workspace_id=?",
                (project_id, workspace_id),
            ).fetchone()
            return None if row is None else _workspace_record(row)
        except ProjectRegistryError:
            raise
        except (sqlite3.Error, IndexError, KeyError, TypeError, ValueError) as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()

    def list_legacy_bindings(
        self, project_id: str,
    ) -> tuple[domain.LegacyBindingRecord, ...] | None:
        project_id = _validate(domain.opaque, project_id, maximum=64)
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_live_read(self.path)
            connection.execute("BEGIN")
            project = connection.execute(
                "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if project is None:
                return None
            rows = connection.execute(
                "SELECT * FROM legacy_project_bindings WHERE project_id=? "
                "ORDER BY source_kind, source_key",
                (project_id,),
            ).fetchall()
            return tuple(_binding_record(row) for row in rows)
        except ProjectRegistryError:
            raise
        except (sqlite3.Error, IndexError, KeyError, TypeError, ValueError) as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()

    def match_discovery(
        self, *, node_id: str, canonical_path: str,
        repository_fingerprint: str | None,
    ) -> tuple[domain.DiscoveryMatch | None, tuple[domain.DiscoveryMatch, ...]]:
        node_id = _validate(domain.opaque, node_id, maximum=128)
        canonical_path = _validate(domain.canonical_path, canonical_path)
        if repository_fingerprint is not None:
            repository_fingerprint = _validate(
                domain.sha256_ref, repository_fingerprint,
            )
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_live_read(self.path)
            connection.execute("BEGIN")
            exact_row = connection.execute(
                "SELECT p.project_id, p.slug, p.display_name "
                "FROM repo_locations r JOIN projects p ON p.project_id=r.project_id "
                "WHERE r.node_id=? AND r.canonical_path=? "
                "AND r.lifecycle='active' AND p.lifecycle='active'",
                (node_id, canonical_path),
            ).fetchone()
            exact = None if exact_row is None else _discovery_match(exact_row)
            if repository_fingerprint is None:
                return exact, ()
            rows = connection.execute(
                "SELECT DISTINCT p.project_id, p.slug, p.display_name "
                "FROM repo_locations r JOIN projects p ON p.project_id=r.project_id "
                "WHERE r.lifecycle='active' AND p.lifecycle='active' "
                "AND r.git_remote_fingerprint=? "
                "ORDER BY p.project_id LIMIT 101",
                (repository_fingerprint,),
            ).fetchall()
            possible = tuple(
                _discovery_match(row) for row in rows
                if exact is None or row["project_id"] != exact.project_id
            )
            return exact, possible[:100]
        except ProjectRegistryError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()

    def preflight_idempotency(
        self, *, scope: str, idempotency_key: str, payload: object,
    ) -> domain.CommandResult | None:
        scope = _validate(domain.text, scope, maximum=128)
        idempotency_key = _validate(domain.text, idempotency_key, maximum=128)
        request_digest = _request_digest(payload)
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_live_read(self.path)
            row = connection.execute(
                "SELECT request_digest, status_code, response_json "
                "FROM idempotency_records WHERE scope=? AND idempotency_key=?",
                (scope, idempotency_key),
            ).fetchone()
            if row is None:
                return None
            if row["request_digest"] != request_digest:
                _fail("idempotency_conflict")
            return domain.CommandResult(
                int(row["status_code"]), json.loads(row["response_json"]),
            )
        except ProjectRegistryError:
            raise
        except json.JSONDecodeError as exc:
            _fail("store_corrupt", exc)
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()

    def get_active_repo_location(
        self, node_id: str, canonical_path: str,
    ) -> domain.RepoLocationRecord | None:
        node_id = _validate(domain.opaque, node_id, maximum=128)
        canonical_path = _validate(domain.canonical_path, canonical_path)
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_live_read(self.path)
            row = connection.execute(
                "SELECT * FROM repo_locations WHERE node_id=? AND canonical_path=? "
                "AND lifecycle='active'",
                (node_id, canonical_path),
            ).fetchone()
            return None if row is None else _repo_location_record(row)
        except ProjectRegistryError:
            raise
        except sqlite3.Error as exc:
            _fail("store_read_failed", exc)
        finally:
            if connection is not None:
                connection.close()

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

    def import_legacy_project(
        self, *, slug: str, display_name: str, goal: str | None,
        node_id: str, canonical_path: str, vcs_kind: str, availability: str,
        sources: tuple[domain.LegacySourceInput, ...],
    ) -> domain.LegacyImportResult:
        slug = _validate(domain.slug, slug)
        display_name = _validate(domain.text, display_name, maximum=256)
        goal = _validate(domain.optional_text, goal, maximum=4096)
        node_id = _validate(domain.opaque, node_id, maximum=128)
        canonical_path = _validate(domain.canonical_path, canonical_path)
        vcs_kind = _validate(domain.enum, vcs_kind, frozenset({"git", "none"}))
        availability = _validate(
            domain.enum, availability,
            frozenset({"available", "offline", "missing", "unknown"}),
        )
        sources = _validated_legacy_sources(sources)

        try:
            with self._transaction() as connection:
                return self._import_legacy_project(
                    connection,
                    slug=slug,
                    display_name=display_name,
                    goal=goal,
                    node_id=node_id,
                    canonical_path=canonical_path,
                    vcs_kind=vcs_kind,
                    availability=availability,
                    sources=sources,
                )
        except ProjectRegistryError:
            raise
        except sqlite3.Error as exc:
            _fail("store_write_failed", exc)

    def idempotent_register_project(
        self, *, scope: str, idempotency_key: str, payload: object,
        slug: str, display_name: str, goal: str | None, node_id: str,
        canonical_path: str, vcs_kind: str, availability: str,
        git_remote_fingerprint: str | None,
    ) -> domain.CommandResult:
        scope, idempotency_key, request_digest = _idempotency_identity(
            scope, idempotency_key, payload,
        )
        slug = _validate(domain.slug, slug)
        display_name = _validate(domain.text, display_name, maximum=256)
        goal = _validate(domain.optional_text, goal, maximum=4096)
        node_id, canonical_path, vcs_kind, availability, remote = _location_input(
            node_id=node_id,
            canonical_path=canonical_path,
            vcs_kind=vcs_kind,
            availability=availability,
            git_remote_fingerprint=git_remote_fingerprint,
        )
        try:
            with self._transaction() as connection:
                replay = _idempotency_replay(
                    connection, scope, idempotency_key, request_digest,
                )
                if replay is not None:
                    return replay
                project = self._insert_project(connection, slug, display_name, goal)
                location = _insert_repo_location(
                    connection,
                    project_id=project.project_id,
                    node_id=node_id,
                    canonical_path=canonical_path,
                    vcs_kind=vcs_kind,
                    availability=availability,
                    git_remote_fingerprint=remote,
                )
                response = _registration_response(project, location)
                _store_idempotency_receipt(
                    connection, scope, idempotency_key, request_digest, 201, response,
                )
                return domain.CommandResult(201, response)
        except ProjectRegistryError:
            raise
        except sqlite3.Error as exc:
            _fail("store_write_failed", exc)

    def idempotent_add_repo_location(
        self, *, scope: str, idempotency_key: str, payload: object,
        project_id: str, expected_project_version: int, node_id: str,
        canonical_path: str, vcs_kind: str, availability: str,
        git_remote_fingerprint: str | None,
    ) -> domain.CommandResult:
        scope, idempotency_key, request_digest = _idempotency_identity(
            scope, idempotency_key, payload,
        )
        project_id = _validate(domain.opaque, project_id, maximum=64)
        if type(expected_project_version) is not int or expected_project_version < 1:
            _fail("invalid_argument")
        node_id, canonical_path, vcs_kind, availability, remote = _location_input(
            node_id=node_id,
            canonical_path=canonical_path,
            vcs_kind=vcs_kind,
            availability=availability,
            git_remote_fingerprint=git_remote_fingerprint,
        )
        if vcs_kind != "git" or remote is None:
            _fail("repository_identity_unproven")
        try:
            with self._transaction() as connection:
                replay = _idempotency_replay(
                    connection, scope, idempotency_key, request_digest,
                )
                if replay is not None:
                    return replay
                project_row = connection.execute(
                    "SELECT * FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
                if project_row is None or project_row["lifecycle"] != "active":
                    _fail("project_not_found")
                if int(project_row["version"]) != expected_project_version:
                    _fail("version_conflict")
                proof = connection.execute(
                    "SELECT 1 FROM repo_locations WHERE project_id=? "
                    "AND lifecycle='active' AND git_remote_fingerprint=? LIMIT 1",
                    (project_id, remote),
                ).fetchone()
                if proof is None:
                    _fail("repository_identity_unproven")
                location = _insert_repo_location(
                    connection,
                    project_id=project_id,
                    node_id=node_id,
                    canonical_path=canonical_path,
                    vcs_kind=vcs_kind,
                    availability=availability,
                    git_remote_fingerprint=remote,
                )
                updated_at = _now()
                updated = connection.execute(
                    "UPDATE projects SET version=version+1, updated_at=? "
                    "WHERE project_id=? AND version=?",
                    (updated_at, project_id, expected_project_version),
                )
                if updated.rowcount != 1:
                    _fail("version_conflict")
                updated_row = connection.execute(
                    "SELECT * FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
                assert updated_row is not None
                project = _project_record(updated_row)
                response = _attach_response(project, location)
                _store_idempotency_receipt(
                    connection, scope, idempotency_key, request_digest, 201, response,
                )
                return domain.CommandResult(201, response)
        except ProjectRegistryError:
            raise
        except sqlite3.Error as exc:
            _fail("store_write_failed", exc)

    def idempotent_create_workspace(
        self, *, scope: str, idempotency_key: str, payload: object,
        project_id: str, repo_location_id: str, name: str,
        goal: str | None, isolation_kind: str, response_meta: dict[str, object],
    ) -> domain.CommandResult:
        scope, idempotency_key, request_digest = _idempotency_identity(
            scope, idempotency_key, payload,
        )
        project_id = _validate(domain.opaque, project_id, maximum=64)
        repo_location_id = _validate(domain.opaque, repo_location_id, maximum=64)
        if not isinstance(name, str) or not 1 <= len(name) <= 256:
            _fail("invalid_argument")
        goal = _validate(domain.optional_text, goal, maximum=4096)
        if isolation_kind != "shared":
            _fail("unsupported_isolation_kind")
        response_meta_json = _validate(domain.canonical_json, response_meta)
        response_meta = json.loads(response_meta_json)
        try:
            with self._transaction() as connection:
                project = connection.execute(
                    "SELECT 1 FROM projects WHERE project_id=? AND lifecycle='active'",
                    (project_id,),
                ).fetchone()
                if project is None:
                    _fail("project_not_found")
                location_row = connection.execute(
                    "SELECT * FROM repo_locations WHERE project_id=? "
                    "AND repo_location_id=? AND lifecycle='active'",
                    (project_id, repo_location_id),
                ).fetchone()
                if location_row is None:
                    _fail("repo_location_not_found")
                location = _repo_location_record(location_row)
                if location.node_id != "local":
                    _fail("repo_location_not_local")
                if location.availability != "available":
                    _fail("repo_location_unavailable")
                replay = _idempotency_replay(
                    connection, scope, idempotency_key, request_digest,
                )
                if replay is not None:
                    return replay
                workspace_id = _id("ws_")
                now = _now()
                try:
                    connection.execute(
                        "INSERT INTO workspaces VALUES "
                        "(?, ?, ?, ?, ?, 'shared', 'active', NULL, 1, ?, ?)",
                        (workspace_id, project_id, repo_location_id, name, goal, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    _fail("workspace_name_conflict", exc)
                workspace = domain.WorkspaceRecord(
                    workspace_id, project_id, repo_location_id, name, goal,
                    "shared", "active", None, 1, now, now,
                )
                response = json.loads(domain.canonical_json({
                    "data": _workspace_response(workspace, location),
                    "meta": response_meta,
                }))
                _store_idempotency_receipt(
                    connection, scope, idempotency_key, request_digest, 201, response,
                )
                return domain.CommandResult(201, response)
        except ProjectRegistryError:
            raise
        except sqlite3.Error as exc:
            _fail("store_write_failed", exc)

    @staticmethod
    def _import_legacy_project(
        connection: sqlite3.Connection, *, slug: str, display_name: str,
        goal: str | None, node_id: str, canonical_path: str, vcs_kind: str,
        availability: str, sources: tuple[domain.LegacySourceInput, ...],
    ) -> domain.LegacyImportResult:
        binding_rows: dict[tuple[str, str], sqlite3.Row] = {}
        owner_ids: set[str] = set()
        for source in sources:
            row = connection.execute(
                "SELECT * FROM legacy_project_bindings "
                "WHERE source_kind=? AND source_key=?",
                (source.source_kind, source.source_key),
            ).fetchone()
            if row is None:
                continue
            if row["source_digest"] != source.source_digest:
                _fail("legacy_import_conflict")
            binding_rows[(source.source_kind, source.source_key)] = row
            owner_ids.add(row["project_id"])

        location_row = connection.execute(
            "SELECT * FROM repo_locations WHERE node_id=? AND canonical_path=? "
            "AND lifecycle='active'",
            (node_id, canonical_path),
        ).fetchone()
        if location_row is not None:
            owner_ids.add(location_row["project_id"])

        slug_row = connection.execute(
            "SELECT * FROM projects WHERE slug=?", (slug,)
        ).fetchone()
        if slug_row is not None:
            owner_ids.add(slug_row["project_id"])
        if len(owner_ids) > 1:
            _fail("legacy_import_conflict")

        changed = False
        if owner_ids:
            owner_id = next(iter(owner_ids))
            project_row = connection.execute(
                "SELECT * FROM projects WHERE project_id=?", (owner_id,)
            ).fetchone()
            if (
                project_row is None
                or project_row["slug"] != slug
                or project_row["lifecycle"] != "active"
            ):
                _fail("legacy_import_conflict")
            project = _project_record(project_row)
        else:
            try:
                project = ProjectRegistryStore._insert_project(
                    connection, slug, display_name, goal
                )
            except ProjectRegistryError as exc:
                if exc.code == "project_slug_conflict":
                    _fail("legacy_import_conflict", exc)
                raise
            owner_id = project.project_id
            changed = True

        if location_row is None:
            location_id = _id("loc_")
            now = _now()
            try:
                connection.execute(
                    "INSERT INTO repo_locations "
                    "(repo_location_id, project_id, node_id, canonical_path, lifecycle, "
                    "vcs_kind, git_root, git_remote_fingerprint, default_ref_observed, "
                    "availability, version, created_at, updated_at) VALUES "
                    "(?, ?, ?, ?, 'active', ?, NULL, NULL, NULL, ?, 1, ?, ?)",
                    (location_id, owner_id, node_id, canonical_path, vcs_kind,
                     availability, now, now),
                )
            except sqlite3.IntegrityError as exc:
                _fail("legacy_import_conflict", exc)
            location = domain.RepoLocationRecord(
                location_id, owner_id, node_id, canonical_path, "active", vcs_kind,
                availability, 1,
            )
            changed = True
        else:
            location = _repo_location_record(location_row)

        bindings: list[domain.LegacyBindingRecord] = []
        for source in sources:
            key = (source.source_kind, source.source_key)
            row = binding_rows.get(key)
            if row is not None:
                binding = _binding_record(row)
                if binding.project_id != owner_id:
                    _fail("legacy_import_conflict")
            else:
                binding = domain.LegacyBindingRecord(
                    _id("bnd_"), owner_id, source.source_kind, source.source_key,
                    source.source_digest, _now(),
                )
                try:
                    connection.execute(
                        "INSERT INTO legacy_project_bindings VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            binding.binding_id, binding.project_id,
                            binding.source_kind, binding.source_key,
                            binding.source_digest, binding.imported_at,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    _fail("legacy_import_conflict", exc)
                _after_legacy_binding_insert(connection, binding)
                changed = True
            bindings.append(binding)

        return domain.LegacyImportResult(
            project, location, tuple(bindings), replayed=not changed,
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


def _request_digest(payload: object) -> str:
    request_json = _validate(domain.canonical_json, payload)
    return hashlib.sha256(request_json.encode("ascii")).hexdigest()


def _idempotency_identity(
    scope: str, idempotency_key: str, payload: object,
) -> tuple[str, str, str]:
    return (
        _validate(domain.text, scope, maximum=128),
        _validate(domain.text, idempotency_key, maximum=128),
        _request_digest(payload),
    )


def _location_input(
    *, node_id: str, canonical_path: str, vcs_kind: str, availability: str,
    git_remote_fingerprint: str | None,
) -> tuple[str, str, str, str, str | None]:
    node_id = _validate(domain.opaque, node_id, maximum=128)
    canonical_path = _validate(domain.canonical_path, canonical_path)
    vcs_kind = _validate(domain.enum, vcs_kind, frozenset({"git", "none"}))
    availability = _validate(
        domain.enum, availability,
        frozenset({"available", "offline", "missing", "unknown"}),
    )
    if git_remote_fingerprint is not None:
        git_remote_fingerprint = _validate(
            domain.sha256_ref, git_remote_fingerprint,
        )
    if vcs_kind != "git" and git_remote_fingerprint is not None:
        _fail("invalid_argument")
    return node_id, canonical_path, vcs_kind, availability, git_remote_fingerprint


def _idempotency_replay(
    connection: sqlite3.Connection, scope: str, idempotency_key: str,
    request_digest: str,
) -> domain.CommandResult | None:
    row = connection.execute(
        "SELECT request_digest, status_code, response_json "
        "FROM idempotency_records WHERE scope=? AND idempotency_key=?",
        (scope, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if row["request_digest"] != request_digest:
        _fail("idempotency_conflict")
    try:
        response = json.loads(row["response_json"])
    except json.JSONDecodeError as exc:
        _fail("store_corrupt", exc)
    return domain.CommandResult(int(row["status_code"]), response)


def _store_idempotency_receipt(
    connection: sqlite3.Connection, scope: str, idempotency_key: str,
    request_digest: str, status_code: int, response: dict[str, object],
) -> None:
    response_json = domain.canonical_json(response)
    connection.execute(
        "INSERT INTO idempotency_records VALUES (?, ?, ?, ?, ?, ?)",
        (scope, idempotency_key, request_digest, status_code, response_json, _now()),
    )


def _workspace_response(
    workspace: domain.WorkspaceRecord, location: domain.RepoLocationRecord,
) -> dict[str, object]:
    return {
        "workspace_id": workspace.workspace_id,
        "project_id": workspace.project_id,
        "repo_location_id": workspace.repo_location_id,
        "name": workspace.name,
        "goal": workspace.goal,
        "isolation_kind": workspace.isolation_kind,
        "lifecycle": workspace.lifecycle,
        "active_run_id": workspace.active_run_id,
        "version": workspace.version,
        "created_at": workspace.created_at,
        "updated_at": workspace.updated_at,
        "repo_location": {
            "node_id": location.node_id,
            "availability": location.availability,
        },
    }


def _insert_repo_location(
    connection: sqlite3.Connection, *, project_id: str, node_id: str,
    canonical_path: str, vcs_kind: str, availability: str,
    git_remote_fingerprint: str | None,
) -> domain.RepoLocationRecord:
    location_id = _id("loc_")
    now = _now()
    try:
        connection.execute(
            "INSERT INTO repo_locations "
            "(repo_location_id, project_id, node_id, canonical_path, lifecycle, "
            "vcs_kind, git_root, git_remote_fingerprint, default_ref_observed, "
            "availability, version, created_at, updated_at) VALUES "
            "(?, ?, ?, ?, 'active', ?, NULL, ?, NULL, ?, 1, ?, ?)",
            (location_id, project_id, node_id, canonical_path, vcs_kind,
             git_remote_fingerprint, availability, now, now),
        )
    except sqlite3.IntegrityError as exc:
        _fail("location_already_registered", exc)
    return domain.RepoLocationRecord(
        location_id, project_id, node_id, canonical_path, "active", vcs_kind,
        availability, 1,
    )


def _registration_response(
    project: domain.ProjectRecord, location: domain.RepoLocationRecord,
) -> dict[str, object]:
    return {
        "project_id": project.project_id,
        "slug": project.slug,
        "project": _project_response(project),
        "repo_location": _repo_location_response(location),
        "replayed": False,
    }


def _attach_response(
    project: domain.ProjectRecord, location: domain.RepoLocationRecord,
) -> dict[str, object]:
    return {
        "project": _project_response(project),
        "repo_location": _repo_location_response(location),
    }


def _project_response(project: domain.ProjectRecord) -> dict[str, object]:
    return {
        "project_id": project.project_id,
        "slug": project.slug,
        "display_name": project.display_name,
        "goal": project.goal,
        "lifecycle": project.lifecycle,
        "version": project.version,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def _repo_location_response(location: domain.RepoLocationRecord) -> dict[str, object]:
    return {
        "repo_location_id": location.repo_location_id,
        "project_id": location.project_id,
        "node_id": location.node_id,
        "canonical_path": location.canonical_path,
        "lifecycle": location.lifecycle,
        "vcs_kind": location.vcs_kind,
        "availability": location.availability,
        "version": location.version,
    }


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
            if exc_type:
                self.connection.execute("ROLLBACK")
            else:
                try:
                    self.connection.execute("COMMIT")
                except BaseException:
                    if self.connection.in_transaction:
                        try:
                            self.connection.execute("ROLLBACK")
                        except BaseException:
                            pass
                    raise
        finally:
            self.connection.close()
        return False


def _binding_record(row: sqlite3.Row) -> domain.LegacyBindingRecord:
    return domain.LegacyBindingRecord(
        row["binding_id"], row["project_id"], row["source_kind"],
        row["source_key"], row["source_digest"], row["imported_at"],
    )


def _project_record(row: sqlite3.Row) -> domain.ProjectRecord:
    return domain.ProjectRecord(
        row["project_id"], row["slug"], row["display_name"], row["goal"],
        row["lifecycle"], int(row["version"]), row["created_at"], row["updated_at"],
    )


def _project_snapshot(
    connection: sqlite3.Connection, project_row: sqlite3.Row,
) -> domain.ProjectSnapshot:
    locations = connection.execute(
        "SELECT * FROM repo_locations WHERE project_id=? ORDER BY repo_location_id",
        (project_row["project_id"],),
    ).fetchall()
    return domain.ProjectSnapshot(
        _project_record(project_row),
        tuple(_repo_location_record(row) for row in locations),
    )


def _discovery_match(row: sqlite3.Row) -> domain.DiscoveryMatch:
    return domain.DiscoveryMatch(
        row["project_id"], row["slug"], row["display_name"],
    )


def _repo_location_record(row: sqlite3.Row) -> domain.RepoLocationRecord:
    return domain.RepoLocationRecord(
        row["repo_location_id"], row["project_id"], row["node_id"],
        row["canonical_path"], row["lifecycle"], row["vcs_kind"],
        row["availability"], int(row["version"]),
    )


def _workspace_record(row: sqlite3.Row) -> domain.WorkspaceRecord:
    return domain.WorkspaceRecord(
        row["workspace_id"], row["project_id"], row["repo_location_id"],
        row["name"], row["goal"], row["isolation_kind"], row["lifecycle"],
        row["active_run_id"], int(row["version"]), row["created_at"],
        row["updated_at"],
    )


def _validated_legacy_sources(
    sources: tuple[domain.LegacySourceInput, ...],
) -> tuple[domain.LegacySourceInput, ...]:
    try:
        values = tuple(sources)
    except TypeError as exc:
        _fail("invalid_argument", exc)
    if not values:
        _fail("invalid_argument")
    validated: list[domain.LegacySourceInput] = []
    identities: set[tuple[str, str]] = set()
    for source in values:
        if not isinstance(source, domain.LegacySourceInput):
            _fail("invalid_argument")
        source_kind = _validate(
            domain.enum, source.source_kind, domain.LEGACY_SOURCE_KINDS,
        )
        source_key = _validate(domain.sha256_ref, source.source_key)
        source_digest = _validate(domain.sha256_ref, source.source_digest)
        identity = (source_kind, source_key)
        if identity in identities:
            _fail("invalid_argument")
        identities.add(identity)
        validated.append(domain.LegacySourceInput(*identity, source_digest))
    return tuple(sorted(validated, key=lambda source: (
        source.source_kind, source.source_key,
    )))


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
