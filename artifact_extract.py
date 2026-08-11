from __future__ import annotations

import hashlib
import os
import re
import stat
import tarfile
from pathlib import Path
from typing import Any

from release_index import MAX_ASSET_BYTES, MAX_ASSET_NAME_BYTES


MAX_MEMBERS = 10_000
MAX_MEMBER_BYTES = 8 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 16 * 1024 * 1024 * 1024

_ASSET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_NOFOLLOW", 0
)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class ArtifactExtractError(ValueError):
    """A public, stable artifact extraction rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject(code: str) -> None:
    raise ArtifactExtractError(code)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _directory_secure(info: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700
    )


def _artifact_secure(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
    )


def _file_signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _validate_asset(asset: Any) -> tuple[str, int, str]:
    if type(asset) is not dict:
        _reject("invalid_asset")
    try:
        name = asset["name"]
        size = asset["size"]
        digest = asset["sha256"]
    except (KeyError, TypeError):
        _reject("invalid_asset")
    if type(name) is not str or _ASSET_NAME_RE.fullmatch(name) is None:
        _reject("invalid_asset")
    try:
        encoded_name = name.encode("ascii")
    except UnicodeEncodeError:
        _reject("invalid_asset")
    if (
        len(encoded_name) > MAX_ASSET_NAME_BYTES
        or ".." in name
        or "/" in name
        or "\\" in name
    ):
        _reject("invalid_asset")
    if type(size) is not int or size <= 0 or size > MAX_ASSET_BYTES:
        _reject("invalid_asset")
    if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
        _reject("invalid_asset")
    return name, size, digest


def _open_directory_chain(path: Path, *, code: str) -> tuple[int, os.stat_result]:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        _reject(code)
    components = path.parts[1:]
    if not components:
        _reject(code)
    fd = -1
    try:
        fd = os.open("/", _DIRECTORY_FLAGS)
        current: os.stat_result | None = None
        for component in components:
            before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                _reject(code)
            child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            after = os.fstat(child_fd)
            if not stat.S_ISDIR(after.st_mode) or not _same_inode(before, after):
                os.close(child_fd)
                _reject(code)
            os.close(fd)
            fd = child_fd
            current = after
        assert current is not None
        return fd, current
    except ArtifactExtractError:
        if fd >= 0:
            os.close(fd)
        raise
    except OSError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        _reject(code)


def _open_artifact(
    artifact_path: Path, expected_size: int, expected_digest: str
) -> tuple[int, tuple[int, ...], int, os.stat_result]:
    if (
        not isinstance(artifact_path, Path)
        or not artifact_path.is_absolute()
        or ".." in artifact_path.parts
        or artifact_path.name != expected_digest
    ):
        _reject("artifact_path_invalid")
    parent_fd, parent_info = _open_directory_chain(
        artifact_path.parent, code="artifact_path_invalid"
    )
    if not _directory_secure(parent_info):
        os.close(parent_fd)
        _reject("artifact_path_invalid")
    fd = -1
    try:
        before = os.stat(
            artifact_path.name, dir_fd=parent_fd, follow_symlinks=False
        )
        if not _artifact_secure(before):
            _reject("artifact_unsafe")
        fd = os.open(artifact_path.name, _READ_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if (
            not _artifact_secure(opened)
            or _file_signature(before) != _file_signature(opened)
            or opened.st_size != expected_size
        ):
            _reject("artifact_unsafe")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, 1024 * 1024):
            total += len(chunk)
            if total > expected_size:
                _reject("artifact_mismatch")
            digest.update(chunk)
        if total != expected_size or digest.hexdigest() != expected_digest:
            _reject("artifact_mismatch")
        after = os.fstat(fd)
        if _file_signature(opened) != _file_signature(after):
            _reject("artifact_unsafe")
        os.lseek(fd, 0, os.SEEK_SET)
        return fd, _file_signature(opened), parent_fd, parent_info
    except ArtifactExtractError:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)
        raise
    except OSError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        os.close(parent_fd)
        _reject("artifact_unsafe")


def _verify_artifact_binding(
    artifact_path: Path,
    fd: int,
    signature: tuple[int, ...],
    parent_fd: int,
    parent_info: os.stat_result,
) -> None:
    try:
        opened = os.fstat(fd)
        current = os.stat(
            artifact_path.name, dir_fd=parent_fd, follow_symlinks=False
        )
    except OSError:
        _reject("artifact_unsafe")
    if (
        not _artifact_secure(opened)
        or _file_signature(opened) != signature
        or _file_signature(current) != signature
    ):
        _reject("artifact_unsafe")
    rebound_fd, rebound_info = _open_directory_chain(
        artifact_path.parent, code="artifact_unsafe"
    )
    try:
        rebound_leaf = os.stat(
            artifact_path.name, dir_fd=rebound_fd, follow_symlinks=False
        )
        if (
            not _directory_secure(rebound_info)
            or not _same_inode(rebound_info, parent_info)
            or _file_signature(rebound_leaf) != signature
        ):
            _reject("artifact_unsafe")
    except ArtifactExtractError:
        raise
    except OSError:
        _reject("artifact_unsafe")
    finally:
        os.close(rebound_fd)


def _member_path(member: tarfile.TarInfo) -> tuple[str, ...]:
    name = member.name
    if type(name) is not str or not name or "\x00" in name or "\\" in name:
        _reject("archive_invalid")
    if name.startswith("/"):
        _reject("archive_invalid")
    if member.isdir() and name.endswith("/"):
        name = name[:-1]
    parts = name.split("/")
    if not name or any(not part or part in {".", ".."} for part in parts):
        _reject("archive_invalid")
    return tuple(parts)


def _scan_members(archive: tarfile.TarFile) -> list[tuple[tarfile.TarInfo, tuple[str, ...]]]:
    scanned: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
    kinds: dict[tuple[str, ...], str] = {}
    total = 0
    try:
        for member in archive:
            if len(scanned) >= MAX_MEMBERS:
                _reject("archive_limit_exceeded")
            parts = _member_path(member)
            if parts in kinds:
                _reject("archive_invalid")
            if member.issparse() or not (member.isdir() or member.isreg()):
                _reject("archive_invalid")
            if type(member.size) is not int or member.size < 0:
                _reject("archive_invalid")
            if member.isdir():
                if member.size != 0:
                    _reject("archive_invalid")
                kind = "dir"
            else:
                if member.size > MAX_MEMBER_BYTES:
                    _reject("archive_limit_exceeded")
                total += member.size
                if total > MAX_EXTRACTED_BYTES:
                    _reject("archive_limit_exceeded")
                kind = "file"
            for index in range(1, len(parts)):
                if kinds.get(parts[:index]) == "file":
                    _reject("archive_invalid")
            if kind == "file" and any(
                existing[: len(parts)] == parts for existing in kinds
            ):
                _reject("archive_invalid")
            kinds[parts] = kind
            scanned.append((member, parts))
    except ArtifactExtractError:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError):
        _reject("archive_invalid")
    return scanned


def _open_child_directory(
    parent_fd: int,
    name: str,
    expected: tuple[int, int] | None,
) -> tuple[int, tuple[int, int]]:
    if expected is None:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileExistsError:
            _reject("destination_unsafe")
        except OSError:
            _reject("extract_failed")
    else:
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            _reject("destination_unsafe")
    if not _directory_secure(before):
        _reject("destination_unsafe")
    child_fd = -1
    try:
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        after = os.fstat(child_fd)
    except OSError:
        if child_fd >= 0:
            os.close(child_fd)
        _reject("destination_unsafe")
    if not _directory_secure(after) or not _same_inode(before, after):
        os.close(child_fd)
        _reject("destination_unsafe")
    identity = (after.st_dev, after.st_ino)
    if expected is not None and identity != expected:
        os.close(child_fd)
        _reject("destination_unsafe")
    return child_fd, identity


def _open_member_parent(
    root_fd: int,
    parts: tuple[str, ...],
    directories: dict[tuple[str, ...], tuple[int, int]],
) -> int:
    fd = os.dup(root_fd)
    prefix: tuple[str, ...] = ()
    try:
        for component in parts[:-1]:
            prefix += (component,)
            child_fd, identity = _open_child_directory(
                fd, component, directories.get(prefix)
            )
            directories.setdefault(prefix, identity)
            os.close(fd)
            fd = child_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _write_member_file(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    root_fd: int,
    parts: tuple[str, ...],
    directories: dict[tuple[str, ...], tuple[int, int]],
) -> tuple[int, ...]:
    parent_fd = _open_member_parent(root_fd, parts, directories)
    output_fd = -1
    try:
        output_fd = os.open(parts[-1], _WRITE_FLAGS, 0o600, dir_fd=parent_fd)
        os.fchmod(output_fd, 0o600)
        opened = os.fstat(output_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
        ):
            _reject("destination_unsafe")
        source = archive.extractfile(member)
        if source is None:
            _reject("archive_invalid")
        total = 0
        try:
            while total < member.size:
                chunk = source.read(min(1024 * 1024, member.size - total))
                if not chunk:
                    _reject("archive_invalid")
                total += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(output_fd, view)
                    if written <= 0:
                        _reject("extract_failed")
                    view = view[written:]
            if source.read(1):
                _reject("archive_invalid")
        except ArtifactExtractError:
            raise
        except (tarfile.TarError, EOFError, OSError, ValueError):
            _reject("archive_invalid")
        finally:
            source.close()
        os.fsync(output_fd)
        final = os.fstat(output_fd)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_uid != os.getuid()
            or stat.S_IMODE(final.st_mode) != 0o600
            or final.st_nlink != 1
            or final.st_size != member.size
        ):
            _reject("destination_unsafe")
        current = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if _file_signature(final) != _file_signature(current):
            _reject("destination_unsafe")
        os.fsync(parent_fd)
        return _file_signature(final)
    except ArtifactExtractError:
        raise
    except OSError:
        _reject("extract_failed")
    finally:
        if output_fd >= 0:
            os.close(output_fd)
        os.close(parent_fd)


def _write_members(
    archive: tarfile.TarFile,
    members: list[tuple[tarfile.TarInfo, tuple[str, ...]]],
    destination_fd: int,
) -> tuple[
    dict[tuple[str, ...], tuple[int, int]],
    dict[tuple[str, ...], tuple[int, ...]],
]:
    directories: dict[tuple[str, ...], tuple[int, int]] = {}
    files: dict[tuple[str, ...], tuple[int, ...]] = {}
    for member, parts in members:
        if member.isdir():
            parent_fd = _open_member_parent(destination_fd, parts, directories)
            child_fd = -1
            try:
                child_fd, identity = _open_child_directory(
                    parent_fd, parts[-1], directories.get(parts)
                )
                directories.setdefault(parts, identity)
                os.fsync(child_fd)
                os.fsync(parent_fd)
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
                os.close(parent_fd)
        else:
            files[parts] = _write_member_file(
                archive, member, destination_fd, parts, directories
            )
    try:
        os.fsync(destination_fd)
    except OSError:
        _reject("extract_failed")
    return directories, files


def _verify_written_tree(
    root_fd: int,
    directories: dict[tuple[str, ...], tuple[int, int]],
    files: dict[tuple[str, ...], tuple[int, ...]],
) -> None:
    expected_children: dict[tuple[str, ...], set[str]] = {(): set()}
    for parts in (*directories, *files):
        expected_children.setdefault(parts[:-1], set()).add(parts[-1])
        if parts in directories:
            expected_children.setdefault(parts, set())

    opened: dict[tuple[str, ...], int] = {(): os.dup(root_fd)}
    try:
        for parts in sorted(directories, key=len):
            parent_fd = opened.get(parts[:-1])
            if parent_fd is None:
                _reject("destination_unsafe")
            child_fd, _ = _open_child_directory(
                parent_fd, parts[-1], directories[parts]
            )
            opened[parts] = child_fd

        for parent, expected in expected_children.items():
            fd = opened.get(parent)
            if fd is None:
                _reject("destination_unsafe")
            try:
                actual = set(os.listdir(fd))
            except OSError:
                _reject("destination_unsafe")
            if actual != expected:
                _reject("destination_unsafe")

        for parts, signature in files.items():
            parent_fd = opened.get(parts[:-1])
            if parent_fd is None:
                _reject("destination_unsafe")
            file_fd = -1
            try:
                before = os.stat(
                    parts[-1], dir_fd=parent_fd, follow_symlinks=False
                )
                file_fd = os.open(parts[-1], _READ_FLAGS, dir_fd=parent_fd)
                after = os.fstat(file_fd)
            except OSError:
                if file_fd >= 0:
                    os.close(file_fd)
                _reject("destination_unsafe")
            try:
                if (
                    not stat.S_ISREG(after.st_mode)
                    or after.st_uid != os.getuid()
                    or stat.S_IMODE(after.st_mode) != 0o600
                    or after.st_nlink != 1
                    or _file_signature(before) != signature
                    or _file_signature(after) != signature
                ):
                    _reject("destination_unsafe")
            finally:
                os.close(file_fd)
    finally:
        for fd in opened.values():
            os.close(fd)


def extract_verified_tarball(
    artifact_path: Path, asset: dict[str, Any], destination: Path
) -> Path:
    """Verify one cached gzip tarball and extract it into a new staging directory.

    On failure, a directory created by this call may remain as forensic evidence.
    Callers must never promote a failed destination and must not recursively clean it.
    """
    _asset_name, expected_size, expected_digest = _validate_asset(asset)
    (
        artifact_fd,
        artifact_signature,
        artifact_parent_fd,
        artifact_parent_info,
    ) = _open_artifact(
        artifact_path, expected_size, expected_digest
    )
    destination_parent_fd = -1
    destination_fd = -1
    try:
        if (
            not isinstance(destination, Path)
            or not destination.is_absolute()
            or ".." in destination.parts
            or not destination.name
        ):
            _reject("destination_invalid")
        destination_parent_fd, destination_parent_info = _open_directory_chain(
            destination.parent, code="destination_invalid"
        )
        if not _directory_secure(destination_parent_info):
            _reject("destination_unsafe")
        try:
            os.stat(
                destination.name,
                dir_fd=destination_parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError:
            _reject("destination_unsafe")
        else:
            _reject("destination_exists")

        try:
            magic = os.read(artifact_fd, 2)
            os.lseek(artifact_fd, 0, os.SEEK_SET)
        except OSError:
            _reject("artifact_unsafe")
        if magic != b"\x1f\x8b":
            _reject("archive_invalid")

        stream = os.fdopen(artifact_fd, "rb", closefd=False)
        try:
            try:
                archive = tarfile.open(fileobj=stream, mode="r:gz")
            except (tarfile.TarError, EOFError, OSError, ValueError):
                _reject("archive_invalid")
            with archive:
                members = _scan_members(archive)
                _verify_artifact_binding(
                    artifact_path,
                    artifact_fd,
                    artifact_signature,
                    artifact_parent_fd,
                    artifact_parent_info,
                )
                destination_fd, _destination_identity = _open_child_directory(
                    destination_parent_fd, destination.name, None
                )
                try:
                    os.fsync(destination_parent_fd)
                except OSError:
                    _reject("extract_failed")
                directories, files = _write_members(
                    archive, members, destination_fd
                )
                _verify_written_tree(destination_fd, directories, files)
        finally:
            stream.close()

        _verify_artifact_binding(
            artifact_path,
            artifact_fd,
            artifact_signature,
            artifact_parent_fd,
            artifact_parent_info,
        )
        destination_info = os.fstat(destination_fd)
        current_destination = os.stat(
            destination.name,
            dir_fd=destination_parent_fd,
            follow_symlinks=False,
        )
        if (
            not _directory_secure(destination_info)
            or not _same_inode(destination_info, current_destination)
        ):
            _reject("destination_unsafe")
        try:
            os.fsync(destination_parent_fd)
        except OSError:
            _reject("extract_failed")
        rebound_fd, rebound_info = _open_directory_chain(
            destination.parent, code="destination_unsafe"
        )
        try:
            if (
                not _directory_secure(rebound_info)
                or not _same_inode(rebound_info, destination_parent_info)
            ):
                _reject("destination_unsafe")
        finally:
            os.close(rebound_fd)
        return destination
    except ArtifactExtractError:
        raise
    except Exception:
        _reject("extract_failed")
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if destination_parent_fd >= 0:
            os.close(destination_parent_fd)
        os.close(artifact_fd)
        os.close(artifact_parent_fd)


__all__ = [
    "ArtifactExtractError",
    "MAX_EXTRACTED_BYTES",
    "MAX_MEMBERS",
    "MAX_MEMBER_BYTES",
    "extract_verified_tarball",
]
