"""Sealed, per-store atomic backup snapshots for immutable upgrades."""

from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import runtime_paths


INVENTORY_NAME = "backup-inventory.json"
INVENTORY_SCHEMA_VERSION = 1
INVENTORY_ENGINE = "immutable-upgrade-controller"
MAX_INVENTORY_BYTES = 256 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_VAPID_BYTES = 64 * 1024
MAX_SQLITE_BYTES = 16 * 1024 * 1024 * 1024
MAX_TOTAL_SNAPSHOT_BYTES = 32 * 1024 * 1024 * 1024
SPACE_RESERVE_BYTES = 512 * 1024 * 1024
STABLE_FILE_ATTEMPTS = 3
SQLITE_BACKUP_DEADLINE_SECONDS = 30.0

_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_EXPECTED_STORE_LAYOUT = {
    "settings": ("data", "settings.json", "file"),
    "tasks": ("data", "tasks.sqlite3", "file"),
    "worktrees": ("data", "worktrees", "dir"),
    "coordination": ("data", "coordination.sqlite3", "file"),
    "leader_binding": ("data", "leader-binding.sqlite3", "file"),
    "push": ("data", "push.sqlite3", "file"),
    "delivery_outbox": ("data", "delivery-outbox.sqlite3", "file"),
    "vapid": ("data", "vapid-private.pem", "file"),
    "mail_projects": ("data", "mail-projects.json", "file"),
    "team_sessions": ("data", "team-sessions.json", "file"),
    "inbox_route": ("data", "team-inbox-route.json", "file"),
    "upgrade": ("data", "upgrade", "dir"),
    "typing": ("state", "typing.json", "file"),
    "file_roots": ("config", "file-roots.json", "file"),
}
SQLITE_STORE_NAMES = frozenset(
    {
        "tasks",
        "coordination",
        "leader_binding",
        "push",
        "delivery_outbox",
    }
)
PRESERVE_REASONS = {
    "worktrees": "live_task_workspaces",
    "upgrade": "controller_evidence",
    "uploads": "live_upload_payloads",
}
SNAPSHOT_STORE_NAMES = tuple(
    sorted(set(_EXPECTED_STORE_LAYOUT) - set(PRESERVE_REASONS))
)


class SnapshotError(RuntimeError):
    """A sanitized snapshot failure with a stable public code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _SourceChanged(Exception):
    pass


def _reject(code: str) -> None:
    raise SnapshotError(code)


def _reject_os_error(exc: OSError, default: str) -> None:
    if exc.errno in {errno.EDQUOT, errno.EFBIG, errno.ENOSPC}:
        _reject("snapshot_limit_exceeded")
    _reject(default)


def canonical_inventory_bytes(inventory: dict[str, Any]) -> bytes:
    if type(inventory) is not dict:
        _reject("backup_inventory_invalid")
    try:
        raw = (
            json.dumps(
                inventory,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _reject("backup_inventory_invalid")
    if len(raw) > MAX_INVENTORY_BYTES:
        _reject("snapshot_limit_exceeded")
    return raw


def _canonical_absolute(path: Path) -> bool:
    return path.is_absolute() and Path(os.path.abspath(path)) == path


def _has_symlink_component(path: Path) -> bool:
    try:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink():
                return True
    except OSError:
        return True
    return False


def _directory_is_private(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700
        and not path.is_symlink()
    )


def _source_directory_is_safe(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and not path.is_symlink()
        and not info.st_mode & 0o022
    )


def _owned_directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or path.is_symlink()
    ):
        return None
    return info.st_dev, info.st_ino


def _private_directory_identity(path: Path) -> tuple[int, int] | None:
    if not _directory_is_private(path):
        return None
    return _owned_directory_identity(path)


def _require_directory_identity(path: Path, identity: tuple[int, int]) -> None:
    if _private_directory_identity(path) != identity:
        _reject("backup_snapshot_unsafe")


def _cleanup_unsealed_root(path: Path, identity: tuple[int, int]) -> None:
    if _owned_directory_identity(path) == identity:
        shutil.rmtree(path, ignore_errors=True)


def _validate_directory_chain(boundary: Path, parent: Path) -> None:
    if not _canonical_absolute(boundary) or not _canonical_absolute(parent):
        _reject("backup_snapshot_unsafe")
    try:
        relative = parent.relative_to(boundary)
    except ValueError:
        _reject("backup_inventory_source_mismatch")
    current = boundary
    for part in ("", *relative.parts):
        if part:
            current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            return
        except OSError:
            _reject("backup_snapshot_unsafe")
        if not _source_directory_is_safe(current):
            _reject("backup_snapshot_unsafe")


def _source_mode(name: str) -> int | None:
    return runtime_paths.STORES[name][4]


def _leaf_info(
    path: Path, boundary: Path, *, kind: str, name: str
) -> os.stat_result | None:
    if not _canonical_absolute(path):
        _reject("backup_snapshot_unsafe")
    if kind == "file" or path != boundary:
        _validate_directory_chain(boundary, path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        _reject("backup_snapshot_unsafe")
    expected_type = stat.S_ISREG if kind == "file" else stat.S_ISDIR
    mode = stat.S_IMODE(info.st_mode)
    declared_mode = _source_mode(name) if kind == "file" else None
    if (
        not expected_type(info.st_mode)
        or info.st_uid != os.getuid()
        or (kind == "file" and info.st_nlink != 1)
        or (kind == "file" and declared_mode is not None and mode != declared_mode)
        or (kind == "file" and declared_mode is None and bool(mode & 0o022))
    ):
        _reject("backup_snapshot_unsafe")
    return info


def _runtime_roots() -> dict[str, Path]:
    try:
        roots = {
            "data": runtime_paths.data_root(),
            "config": runtime_paths.config_root(),
            "state": runtime_paths.state_root(),
            "uploads": runtime_paths.uploads_root(),
        }
    except runtime_paths.PathResolutionError as exc:
        _reject(
            "backup_snapshot_unsafe"
            if "symlink" in exc.reason
            else "backup_inventory_source_mismatch"
        )
    except (OSError, ValueError):
        _reject("backup_inventory_source_mismatch")
    return roots


def _validate_frozen_layout() -> None:
    actual = {name: tuple(spec[:3]) for name, spec in runtime_paths.STORES.items()}
    if actual != _EXPECTED_STORE_LAYOUT:
        _reject("backup_inventory_source_mismatch")


def _source_path(name: str, roots: dict[str, Path]) -> tuple[Path, Path]:
    if name == "uploads":
        return roots["uploads"], roots["uploads"]
    logical_root, rel, _kind = _EXPECTED_STORE_LAYOUT[name]
    try:
        path = runtime_paths.store(name)
    except runtime_paths.PathResolutionError as exc:
        _reject(
            "backup_snapshot_unsafe"
            if "symlink" in exc.reason
            else "backup_inventory_source_mismatch"
        )
    except (KeyError, OSError, ValueError):
        _reject("backup_inventory_source_mismatch")
    expected = roots[logical_root] / rel
    if name != "coordination" and path != expected:
        _reject("backup_inventory_source_mismatch")
    boundary = roots[logical_root] if path == expected else path.parent
    return path, boundary


def _snapshot_relpath(name: str) -> str:
    logical_root, rel, _kind = _EXPECTED_STORE_LAYOUT[name]
    return f"{logical_root}/{rel}"


def _file_kind(name: str) -> str:
    if name in SQLITE_STORE_NAMES:
        return "sqlite"
    if name == "vapid":
        return "key"
    return "json"


def _file_limit(name: str) -> int:
    if name in SQLITE_STORE_NAMES:
        return MAX_SQLITE_BYTES
    if name == "vapid":
        return MAX_VAPID_BYTES
    return MAX_JSON_BYTES


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_file_info(
    fd: int, path: Path, *, max_bytes: int, source_mode: int | None
) -> os.stat_result:
    info = os.fstat(fd)
    try:
        current = path.lstat()
    except OSError:
        raise _SourceChanged from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_size > max_bytes
        or (source_mode is not None and stat.S_IMODE(info.st_mode) != source_mode)
        or (source_mode is None and bool(stat.S_IMODE(info.st_mode) & 0o022))
    ):
        if info.st_size > max_bytes:
            _reject("snapshot_limit_exceeded")
        _reject("backup_snapshot_unsafe")
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise _SourceChanged
    return info


def _copy_stable_file_once(
    source: Path, destination: Path, *, max_bytes: int, source_mode: int | None
) -> tuple[int, str]:
    source_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source_fd = os.open(source, source_flags)
    except OSError as exc:
        raise _SourceChanged from exc
    temp_fd = -1
    temp_path: Path | None = None
    try:
        before = _safe_file_info(
            source_fd, source, max_bytes=max_bytes, source_mode=source_mode
        )
        temp_fd, raw_temp = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        temp_path = Path(raw_temp)
        os.fchmod(temp_fd, 0o600)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                _reject("snapshot_limit_exceeded")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    _reject("backup_snapshot_unsafe")
                view = view[written:]
        os.fsync(temp_fd)
        after = os.fstat(source_fd)
        try:
            current = source.lstat()
        except OSError as exc:
            raise _SourceChanged from exc
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            identity_before != identity_after
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or total != before.st_size
        ):
            raise _SourceChanged
        os.close(temp_fd)
        temp_fd = -1
        os.replace(temp_path, destination)
        temp_path = None
        _fsync_directory(destination.parent)
        return total, digest.hexdigest()
    finally:
        os.close(source_fd)
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _copy_stable_file(
    source: Path, destination: Path, *, max_bytes: int, source_mode: int | None
) -> tuple[int, str]:
    for _attempt in range(STABLE_FILE_ATTEMPTS):
        try:
            return _copy_stable_file_once(
                source,
                destination,
                max_bytes=max_bytes,
                source_mode=source_mode,
            )
        except _SourceChanged:
            continue
    _reject("snapshot_source_unstable")


def _hash_owned_file(path: Path, *, max_bytes: int) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        _reject_os_error(exc, "backup_snapshot_unsafe")
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > max_bytes
        ):
            _reject(
                "snapshot_limit_exceeded"
                if info.st_size > max_bytes
                else "backup_snapshot_unsafe"
            )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                _reject("snapshot_limit_exceeded")
            digest.update(chunk)
        return total, digest.hexdigest()
    finally:
        os.close(fd)


def _sqlite_fd_uri(fd: int) -> str:
    """Bind sqlite URI to an already-open fd via /proc/self/fd or /dev/fd.

    macOS /dev/fd/N often has Path.stat() metadata that does not match fstat(fd);
    re-open the descriptor path and compare fstat results before accepting it.
    """
    opened = os.fstat(fd)
    candidates: list[Path] = []
    for directory in (Path("/proc/self/fd"), Path("/dev/fd")):
        if directory.is_dir():
            candidates.append(directory / str(fd))
    for descriptor_path in candidates:
        try:
            current = descriptor_path.stat()
        except OSError:
            current = None
        if current is not None and (current.st_dev, current.st_ino) == (
            opened.st_dev,
            opened.st_ino,
        ):
            return f"{descriptor_path.as_uri()}?mode=ro"
        # Darwin: path.stat() can disagree; open the path and fstat the probe.
        probe = -1
        probed: os.stat_result | None = None
        try:
            probe = os.open(
                descriptor_path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            probed = os.fstat(probe)
        except OSError:
            continue
        finally:
            if probe >= 0:
                os.close(probe)
        if probed is not None and (probed.st_dev, probed.st_ino) == (
            opened.st_dev,
            opened.st_ino,
        ):
            return f"{descriptor_path.as_uri()}?mode=ro"
    _reject("backup_snapshot_unsafe")


def _backup_sqlite(
    source: Path,
    destination: Path,
    *,
    source_info: os.stat_result,
    source_mode: int | None,
) -> tuple[int, str]:
    if source_info.st_size > MAX_SQLITE_BYTES:
        _reject("snapshot_limit_exceeded")
    source_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    source_fd = -1
    try:
        source_fd = os.open(source, source_flags)
        opened = _safe_file_info(
            source_fd,
            source,
            max_bytes=MAX_SQLITE_BYTES,
            source_mode=source_mode,
        )
    except OSError as exc:
        if source_fd >= 0:
            os.close(source_fd)
        _reject_os_error(exc, "backup_snapshot_unsafe")
    except _SourceChanged:
        if source_fd >= 0:
            os.close(source_fd)
        _reject("snapshot_source_unstable")
    except SnapshotError:
        if source_fd >= 0:
            os.close(source_fd)
        raise
    temp_fd, raw_temp = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temp_path = Path(raw_temp)
    os.close(temp_fd)
    os.chmod(temp_path, 0o600)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(
            _sqlite_fd_uri(source_fd),
            uri=True,
            timeout=5.0,
        )
        source_connection.execute("PRAGMA query_only=ON")
        source_connection.execute("PRAGMA busy_timeout=5000")
        destination_connection = sqlite3.connect(temp_path, timeout=5.0)
        deadline = time.monotonic() + SQLITE_BACKUP_DEADLINE_SECONDS

        def progress(_status: int, _remaining: int, _total: int) -> None:
            if time.monotonic() > deadline:
                raise sqlite3.OperationalError("backup deadline")

        source_connection.backup(
            destination_connection,
            pages=256,
            progress=progress,
            sleep=0.01,
        )
        check = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if check != ("ok",):
            _reject("sqlite_snapshot_invalid")
        destination_connection.close()
        destination_connection = None
        source_connection.close()
        source_connection = None
        try:
            current = source.lstat()
        except OSError:
            _reject("snapshot_source_unstable")
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            _reject("snapshot_source_unstable")
        for suffix in ("-wal", "-shm"):
            if Path(f"{temp_path}{suffix}").exists():
                _reject("sqlite_snapshot_invalid")
        os.chmod(temp_path, 0o600)
        sync_fd = os.open(temp_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(sync_fd)
        finally:
            os.close(sync_fd)
        size, digest = _hash_owned_file(temp_path, max_bytes=MAX_SQLITE_BYTES)
        os.replace(temp_path, destination)
        _fsync_directory(destination.parent)
        return size, digest
    except SnapshotError:
        raise
    except OSError as exc:
        _reject_os_error(exc, "sqlite_snapshot_invalid")
    except sqlite3.Error:
        _reject("sqlite_snapshot_invalid")
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        os.close(source_fd)
        for path in (temp_path, Path(f"{temp_path}-wal"), Path(f"{temp_path}-shm")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _entry_base(name: str) -> dict[str, Any]:
    if name == "uploads":
        logical_root, rel, kind = "uploads", ".", "dir"
    else:
        logical_root, rel, runtime_kind = _EXPECTED_STORE_LAYOUT[name]
        kind = "dir" if runtime_kind == "dir" else _file_kind(name)
    return {
        "name": name,
        "logical_root": logical_root,
        "source_relpath": rel,
        "kind": kind,
    }


def _preserve_entry(name: str, source_state: str) -> dict[str, Any]:
    return {
        **_entry_base(name),
        "policy": "preserve_in_place",
        "source_state": source_state,
        "capture": "none",
        "snapshot_relpath": None,
        "size_bytes": None,
        "sha256": None,
        "mode": None,
        "reason": PRESERVE_REASONS[name],
    }


def _absent_entry(name: str) -> dict[str, Any]:
    return {
        **_entry_base(name),
        "policy": "snapshot",
        "source_state": "absent",
        "capture": "none",
        "snapshot_relpath": _snapshot_relpath(name),
        "size_bytes": None,
        "sha256": None,
        "mode": None,
        "reason": "source_absent",
    }


def _present_entry(name: str, size: int, digest: str) -> dict[str, Any]:
    return {
        **_entry_base(name),
        "policy": "snapshot",
        "source_state": "present",
        "capture": "sqlite_backup" if name in SQLITE_STORE_NAMES else "stable_file",
        "snapshot_relpath": _snapshot_relpath(name),
        "size_bytes": size,
        "sha256": digest,
        "mode": 0o600,
        "reason": None,
    }


def _validate_inputs(
    snapshot_root: Path,
    snapshot_id: Any,
    request_id: Any,
    source_sha: Any,
    target_digest: Any,
) -> Path:
    def valid_id(value: Any) -> bool:
        return (
            type(value) is str
            and 0 < len(value) <= 200
            and not any(ord(character) < 32 for character in value)
        )

    if (
        not valid_id(snapshot_id)
        or not valid_id(request_id)
        or type(source_sha) is not str
        or _GIT_SHA_RE.fullmatch(source_sha) is None
        or type(target_digest) is not str
        or _SHA256_RE.fullmatch(target_digest) is None
    ):
        _reject("backup_inventory_invalid")
    root = Path(snapshot_root)
    if not _canonical_absolute(root) or _has_symlink_component(root):
        _reject("backup_snapshot_unsafe")
    if root.exists() or not _directory_is_private(root.parent):
        _reject("backup_snapshot_unsafe")
    return root


def _space_precheck(
    sources: dict[str, tuple[Path, Path, os.stat_result | None]], parent: Path
) -> None:
    estimate = 0
    max_entry = 0
    for name in SNAPSHOT_STORE_NAMES:
        info = sources[name][2]
        if info is None:
            continue
        limit = _file_limit(name)
        if info.st_size > limit:
            _reject("snapshot_limit_exceeded")
        estimate += info.st_size
        max_entry = max(max_entry, info.st_size)
    if estimate > MAX_TOTAL_SNAPSHOT_BYTES:
        _reject("snapshot_limit_exceeded")
    try:
        free = shutil.disk_usage(parent).free
    except OSError:
        _reject("backup_snapshot_unsafe")
    if free < estimate + max_entry + SPACE_RESERVE_BYTES:
        _reject("snapshot_limit_exceeded")


def _seal_inventory(path: Path, inventory: dict[str, Any]) -> tuple[str, bytes]:
    raw = canonical_inventory_bytes(inventory)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{INVENTORY_NAME}.", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                _reject("backup_snapshot_unsafe")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
        return hashlib.sha256(raw).hexdigest(), raw
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def create_backup_snapshot(
    *,
    snapshot_root: Path,
    snapshot_id: str,
    request_id: str,
    source_sha: str,
    target_digest: str,
) -> dict[str, Any]:
    """Create and atomically publish one canonical inventory v1 snapshot."""
    root = _validate_inputs(
        snapshot_root,
        snapshot_id,
        request_id,
        source_sha,
        target_digest,
    )
    _validate_frozen_layout()
    roots = _runtime_roots()
    sources: dict[str, tuple[Path, Path, os.stat_result | None]] = {}
    for name in sorted(set(_EXPECTED_STORE_LAYOUT) | {"uploads"}):
        path, boundary = _source_path(name, roots)
        kind = "dir" if name in PRESERVE_REASONS else "file"
        sources[name] = (
            path,
            boundary,
            _leaf_info(path, boundary, kind=kind, name=name),
        )
    _space_precheck(sources, root.parent)

    try:
        root.mkdir(mode=0o700)
    except OSError as exc:
        _reject_os_error(exc, "backup_snapshot_unsafe")
    root_identity = _owned_directory_identity(root)
    if root_identity is None or _private_directory_identity(root) != root_identity:
        if root_identity is not None:
            _cleanup_unsealed_root(root, root_identity)
        _reject("backup_snapshot_unsafe")
    sealed = False
    try:
        for directory in ("data", "config", "state"):
            target = root / directory
            target.mkdir(mode=0o700)
            os.chmod(target, 0o700)
            if not _directory_is_private(target):
                _reject("backup_snapshot_unsafe")
        entries: list[dict[str, Any]] = []
        total = 0
        for name in sorted(set(_EXPECTED_STORE_LAYOUT) | {"uploads"}):
            source, boundary, initial_info = sources[name]
            if name in PRESERVE_REASONS:
                current = _leaf_info(source, boundary, kind="dir", name=name)
                entries.append(
                    _preserve_entry(name, "present" if current else "absent")
                )
                continue
            current = _leaf_info(source, boundary, kind="file", name=name)
            if current is None:
                entries.append(_absent_entry(name))
                continue
            destination = root / _snapshot_relpath(name)
            if name in SQLITE_STORE_NAMES:
                size, digest = _backup_sqlite(
                    source,
                    destination,
                    source_info=current,
                    source_mode=_source_mode(name),
                )
            else:
                size, digest = _copy_stable_file(
                    source,
                    destination,
                    max_bytes=_file_limit(name),
                    source_mode=_source_mode(name),
                )
            total += size
            if total > MAX_TOTAL_SNAPSHOT_BYTES:
                _reject("snapshot_limit_exceeded")
            entries.append(_present_entry(name, size, digest))

        inventory = {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "engine": INVENTORY_ENGINE,
            "snapshot_id": snapshot_id,
            "request_id": request_id,
            "source_sha": source_sha,
            "target_digest": target_digest,
            "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "consistency_scope": "per_store_atomic",
            "entry_count": len(entries),
            "total_snapshot_bytes": total,
            "entries": entries,
        }
        _require_directory_identity(root, root_identity)
        inventory_path = root / INVENTORY_NAME
        inventory_digest, _raw = _seal_inventory(inventory_path, inventory)
        sealed = True
        _require_directory_identity(root, root_identity)
        _fsync_directory(root)
        _fsync_directory(root.parent)
        return {
            "snapshot_root": root,
            "inventory_path": root / INVENTORY_NAME,
            "inventory_sha256": inventory_digest,
            "inventory": inventory,
        }
    except SnapshotError:
        raise
    except OSError as exc:
        _reject_os_error(exc, "backup_snapshot_unsafe")
    except (ValueError, TypeError):
        _reject("backup_snapshot_unsafe")
    except Exception:
        _reject("backup_snapshot_unsafe")
    finally:
        if not sealed:
            _cleanup_unsealed_root(root, root_identity)


__all__ = [
    "INVENTORY_NAME",
    "MAX_INVENTORY_BYTES",
    "MAX_JSON_BYTES",
    "MAX_SQLITE_BYTES",
    "MAX_TOTAL_SNAPSHOT_BYTES",
    "MAX_VAPID_BYTES",
    "PRESERVE_REASONS",
    "SNAPSHOT_STORE_NAMES",
    "SQLITE_STORE_NAMES",
    "SnapshotError",
    "canonical_inventory_bytes",
    "create_backup_snapshot",
]
