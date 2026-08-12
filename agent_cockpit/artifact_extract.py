from __future__ import annotations

import hashlib
import os
import re
import stat
import tarfile
import zlib
from pathlib import Path
from typing import Any

from .release_index import (
    MAX_ASSET_BYTES,
    MAX_ASSET_NAME_BYTES,
    MAX_LAUNCHER_BYTES,
    SERVER_LAUNCHER_FORMATS,
    SERVER_LAUNCHER_PATH,
)


MAX_MEMBERS = 10_000
MAX_MEMBER_BYTES = 8 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 16 * 1024 * 1024 * 1024
MAX_MEMBER_PATH_BYTES = 4096
MAX_MEMBER_COMPONENT_BYTES = 255
MAX_MEMBER_DEPTH = 128
MAX_ARCHIVE_METADATA_BYTES = 16 * 1024 * 1024
MAX_DESTINATION_NAME_BYTES = 255

_ASSET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_LAUNCHER_FIELDS = {"path", "size", "sha256", "format"}
_MACH_O_MAGICS = {
    b"\xca\xfe\xba\xbe",
    b"\xca\xfe\xba\xbf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
}
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


class _BoundedTarInfo(tarfile.TarInfo):
    """Reject extension-header bombs before tarfile allocates their payload."""

    def _charge_metadata(self, archive: tarfile.TarFile, amount: int) -> None:
        used = getattr(archive, "_artifact_metadata_bytes", 0)
        if amount > MAX_ARCHIVE_METADATA_BYTES - used:
            _reject("archive_limit_exceeded")
        archive._artifact_metadata_bytes = used + amount

    def _proc_member(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        self._charge_metadata(archive, tarfile.BLOCKSIZE)
        if self.type == tarfile.GNUTYPE_SPARSE:
            _reject("archive_invalid")
        if self.type in (
            tarfile.GNUTYPE_LONGNAME,
            tarfile.GNUTYPE_LONGLINK,
            tarfile.XHDTYPE,
            tarfile.XGLTYPE,
            tarfile.SOLARIS_XHDTYPE,
        ):
            if type(self.size) is not int or self.size < 0:
                _reject("archive_invalid")
            self._charge_metadata(archive, self._block(self.size))
        return super()._proc_member(archive)

    def _proc_pax(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        # PAX sparse formats can consume additional map data before yielding a
        # TarInfo. Inspect the already-bounded header first and reject them.
        position = archive.fileobj.tell()
        payload = archive.fileobj.read(self._block(self.size))
        archive.fileobj.seek(position)
        if b"GNU.sparse" in payload:
            _reject("archive_invalid")
        return super()._proc_pax(archive)


class _PathNode:
    __slots__ = ("children", "kind")

    def __init__(self) -> None:
        self.children: dict[str, _PathNode] = {}
        self.kind: str | None = None


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


def _validate_launcher(launcher: Any) -> tuple[tuple[str, ...], int, str, str]:
    if type(launcher) is not dict or set(launcher) != _LAUNCHER_FIELDS:
        _reject("launcher_invalid")
    path = launcher["path"]
    size = launcher["size"]
    digest = launcher["sha256"]
    launcher_format = launcher["format"]
    if type(path) is not str or path != SERVER_LAUNCHER_PATH:
        _reject("launcher_invalid")
    if type(size) is not int or size <= 0 or size > MAX_LAUNCHER_BYTES:
        _reject("launcher_invalid")
    if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
        _reject("launcher_invalid")
    if (
        type(launcher_format) is not str
        or launcher_format not in SERVER_LAUNCHER_FORMATS.values()
    ):
        _reject("launcher_invalid")
    return tuple(path.split("/")), size, digest, launcher_format


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
    try:
        encoded_name = name.encode("utf-8")
        encoded_parts = [part.encode("utf-8") for part in parts]
    except UnicodeEncodeError:
        _reject("archive_invalid")
    if (
        len(encoded_name) > MAX_MEMBER_PATH_BYTES
        or len(parts) > MAX_MEMBER_DEPTH
        or any(len(part) > MAX_MEMBER_COMPONENT_BYTES for part in encoded_parts)
    ):
        _reject("archive_limit_exceeded")
    return tuple(parts)


def _scan_members(archive: tarfile.TarFile) -> list[tuple[tarfile.TarInfo, tuple[str, ...]]]:
    scanned: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
    paths = _PathNode()
    total = 0
    try:
        for member in archive:
            if len(scanned) >= MAX_MEMBERS:
                _reject("archive_limit_exceeded")
            parts = _member_path(member)
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
            node = paths
            for component in parts:
                if node.kind == "file":
                    _reject("archive_invalid")
                node = node.children.setdefault(component, _PathNode())
            if node.kind is not None or (kind == "file" and node.children):
                _reject("archive_invalid")
            node.kind = kind
            scanned.append((member, parts))
    except ArtifactExtractError:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError, zlib.error):
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
        try:
            source = archive.extractfile(member)
        except (tarfile.TarError, EOFError, OSError, ValueError, zlib.error):
            _reject("archive_invalid")
        if source is None:
            _reject("archive_invalid")
        total = 0
        try:
            while total < member.size:
                try:
                    chunk = source.read(min(1024 * 1024, member.size - total))
                except (tarfile.TarError, EOFError, OSError, ValueError, zlib.error):
                    _reject("archive_invalid")
                if not chunk:
                    _reject("archive_invalid")
                total += len(chunk)
                view = memoryview(chunk)
                while view:
                    try:
                        written = os.write(output_fd, view)
                    except OSError:
                        _reject("extract_failed")
                    if written <= 0:
                        _reject("extract_failed")
                    view = view[written:]
            try:
                trailing = source.read(1)
            except (tarfile.TarError, EOFError, OSError, ValueError, zlib.error):
                _reject("archive_invalid")
            if trailing:
                _reject("archive_invalid")
        except ArtifactExtractError:
            raise
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


def _open_existing_member_parent(
    root_fd: int,
    parts: tuple[str, ...],
    expected_directories: dict[tuple[str, ...], tuple[int, int]] | None = None,
) -> int:
    fd = os.dup(root_fd)
    prefix: tuple[str, ...] = ()
    try:
        for component in parts[:-1]:
            prefix += (component,)
            try:
                before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            except OSError:
                _reject("launcher_unsafe")
            expected = (before.st_dev, before.st_ino)
            if expected_directories is not None:
                recorded = expected_directories.get(prefix)
                if recorded is None or recorded != expected:
                    _reject("launcher_unsafe")
            child_fd, identity = _open_child_directory(fd, component, expected)
            if identity != expected:
                os.close(child_fd)
                _reject("launcher_unsafe")
            os.close(fd)
            fd = child_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _native_launcher_magic_matches(launcher_format: str, prefix: bytes) -> bool:
    if launcher_format == "elf":
        return prefix.startswith(b"\x7fELF")
    if launcher_format == "mach-o":
        return prefix[:4] in _MACH_O_MAGICS
    return False


def _verify_server_launcher_fd(
    root_fd: int,
    launcher: dict[str, Any],
    *,
    expected_directories: dict[tuple[str, ...], tuple[int, int]] | None = None,
    expected_files: dict[tuple[str, ...], tuple[int, ...]] | None = None,
    promote_mode: bool,
) -> None:
    parts, expected_size, expected_digest, launcher_format = _validate_launcher(
        launcher
    )
    expected_signature = expected_files.get(parts) if expected_files is not None else None
    if expected_files is not None and expected_signature is None:
        _reject("launcher_missing")
    parent_fd = _open_existing_member_parent(
        root_fd, parts, expected_directories
    )
    launcher_fd = -1
    try:
        try:
            before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            launcher_fd = os.open(parts[-1], _READ_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(launcher_fd)
        except OSError:
            _reject("launcher_unsafe")
        expected_mode = 0o600 if promote_mode else 0o700
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != expected_mode
            or opened.st_nlink != 1
            or opened.st_size != expected_size
            or _file_signature(before) != _file_signature(opened)
            or (
                expected_signature is not None
                and _file_signature(opened) != expected_signature
            )
        ):
            _reject("launcher_unsafe")

        digest = hashlib.sha256()
        prefix = b""
        total = 0
        try:
            while chunk := os.read(launcher_fd, 1024 * 1024):
                if len(prefix) < 8:
                    prefix += chunk[: 8 - len(prefix)]
                total += len(chunk)
                if total > expected_size:
                    _reject("launcher_mismatch")
                digest.update(chunk)
        except OSError:
            _reject("launcher_unsafe")
        if (
            total != expected_size
            or digest.hexdigest() != expected_digest
            or not _native_launcher_magic_matches(launcher_format, prefix)
        ):
            _reject("launcher_mismatch")

        after = os.fstat(launcher_fd)
        current = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if (
            _file_signature(after) != _file_signature(opened)
            or _file_signature(current) != _file_signature(opened)
        ):
            _reject("launcher_unsafe")

        if promote_mode:
            os.fchmod(launcher_fd, 0o700)
            os.fsync(launcher_fd)
            final = os.fstat(launcher_fd)
            current = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(final.st_mode)
                or final.st_uid != os.getuid()
                or stat.S_IMODE(final.st_mode) != 0o700
                or final.st_nlink != 1
                or final.st_size != expected_size
                or _file_signature(current) != _file_signature(final)
            ):
                _reject("launcher_unsafe")
            os.fsync(parent_fd)
    except ArtifactExtractError:
        raise
    except OSError:
        _reject("launcher_unsafe")
    finally:
        if launcher_fd >= 0:
            os.close(launcher_fd)
        os.close(parent_fd)


def verify_server_launcher(generation: Path, launcher: dict[str, Any]) -> Path:
    """Re-verify an extracted native launcher before starting a generation."""
    root_fd, root_info = _open_directory_chain(
        generation, code="launcher_generation_invalid"
    )
    try:
        if not _directory_secure(root_info):
            _reject("launcher_generation_invalid")
        _verify_server_launcher_fd(root_fd, launcher, promote_mode=False)
    finally:
        os.close(root_fd)
    return generation / SERVER_LAUNCHER_PATH


def _extract_verified_archive(
    artifact_path: Path, asset: dict[str, Any], destination: Path
) -> Path:
    """Verify one cached gzip tarball and extract it into a new staging directory.

    Extracted files are deliberately non-executable (0600). A signed v2 server
    launcher contract promotes only ``bin/agent-cockpit`` to mode 0700 after the
    complete tree and the launcher's identity/content/native format are verified.
    On failure, a directory created by this call may remain as forensic evidence.
    Callers must never promote a failed destination and must not recursively clean it.
    """
    _asset_name, expected_size, expected_digest = _validate_asset(asset)
    launcher = asset.get("launcher") if type(asset) is dict else None
    if launcher is not None:
        _validate_launcher(launcher)
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
        try:
            destination_name = destination.name.encode("utf-8")
        except UnicodeEncodeError:
            _reject("destination_invalid")
        if (
            len(destination_name) > MAX_DESTINATION_NAME_BYTES
            or destination.name in {".", ".."}
            or "/" in destination.name
            or "\\" in destination.name
            or "\x00" in destination.name
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
                archive = tarfile.open(
                    fileobj=stream, mode="r:gz", tarinfo=_BoundedTarInfo
                )
            except ArtifactExtractError:
                raise
            except (tarfile.TarError, EOFError, OSError, ValueError, zlib.error):
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
                if launcher is not None:
                    _verify_server_launcher_fd(
                        destination_fd,
                        launcher,
                        expected_directories=directories,
                        expected_files=files,
                        promote_mode=True,
                    )
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


def extract_verified_tarball(
    artifact_path: Path, asset: dict[str, Any], destination: Path
) -> Path:
    """Extract a verified server artifact with a mandatory signed launcher."""
    if type(asset) is not dict or "launcher" not in asset:
        _reject("launcher_invalid")
    return _extract_verified_archive(artifact_path, asset, destination)


__all__ = [
    "ArtifactExtractError",
    "MAX_EXTRACTED_BYTES",
    "MAX_ARCHIVE_METADATA_BYTES",
    "MAX_DESTINATION_NAME_BYTES",
    "MAX_MEMBERS",
    "MAX_MEMBER_BYTES",
    "MAX_MEMBER_COMPONENT_BYTES",
    "MAX_MEMBER_DEPTH",
    "MAX_MEMBER_PATH_BYTES",
    "extract_verified_tarball",
    "verify_server_launcher",
]
