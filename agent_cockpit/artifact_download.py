from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

import httpx

from .release_index import MAX_ASSET_BYTES, MAX_ASSET_NAME_BYTES


_ASSET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DOWNLOAD_PATH_RE = re.compile(
    r"/fyc0451/agent-cockpit/releases/download/"
    r"agent-cockpit-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)/([^/]+)\Z"
)


class ArtifactDownloadError(ValueError):
    """A public, stable artifact-download rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Response(Protocol):
    status_code: int
    headers: Any

    def iter_bytes(self) -> Any: ...


Transport = Callable[[str], AbstractContextManager[_Response]]


def _reject(code: str) -> None:
    raise ArtifactDownloadError(code)


def _validate_asset(asset: Any) -> tuple[str, int, str, str]:
    if type(asset) is not dict:
        _reject("invalid_asset")
    try:
        name = asset["name"]
        size = asset["size"]
        digest = asset["sha256"]
        url = asset["url"]
    except (KeyError, TypeError):
        _reject("invalid_asset")

    if type(name) is not str or _ASSET_NAME_RE.fullmatch(name) is None:
        _reject("invalid_asset")
    try:
        name_bytes = name.encode("ascii")
    except UnicodeEncodeError:
        _reject("invalid_asset")
    if (
        len(name_bytes) > MAX_ASSET_NAME_BYTES
        or ".." in name
        or "/" in name
        or "\\" in name
    ):
        _reject("invalid_asset")
    if type(size) is not int or size <= 0 or size > MAX_ASSET_BYTES:
        _reject("invalid_asset")
    if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
        _reject("invalid_asset")
    if type(url) is not str:
        _reject("invalid_url")

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        _reject("invalid_url")
    path_match = _DOWNLOAD_PATH_RE.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or path_match is None
        or path_match.group(1) != name
    ):
        _reject("invalid_url")
    return name, size, digest, url


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_NOFOLLOW", 0
)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _secure_cache_file(info: os.stat_result) -> bool:
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


def _open_cache_dir(cache_dir: Path) -> int:
    if not cache_dir.is_absolute() or ".." in cache_dir.parts:
        _reject("cache_path_invalid")
    components = cache_dir.parts[1:]
    if not components:
        _reject("cache_path_invalid")

    fd = -1
    try:
        fd = os.open("/", _DIRECTORY_FLAGS)
        leaf_before: os.stat_result | None = None
        leaf_after: os.stat_result | None = None
        for component in components:
            try:
                before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=fd)
                before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                _reject("cache_path_invalid")
            child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            after = os.fstat(child_fd)
            if not stat.S_ISDIR(after.st_mode) or not _same_inode(before, after):
                os.close(child_fd)
                _reject("cache_path_invalid")
            os.close(fd)
            fd = child_fd
            leaf_before = before
            leaf_after = after
        assert leaf_before is not None and leaf_after is not None
        if (
            leaf_before.st_uid != os.getuid()
            or stat.S_IMODE(leaf_before.st_mode) != 0o700
            or leaf_after.st_uid != os.getuid()
            or stat.S_IMODE(leaf_after.st_mode) != 0o700
        ):
            _reject("cache_path_invalid")
        return fd
    except ArtifactDownloadError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    except OSError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        _reject("cache_path_invalid")


def _open_regular_nofollow(
    cache_fd: int, name: str
) -> tuple[int, os.stat_result]:
    try:
        before = os.stat(name, dir_fd=cache_fd, follow_symlinks=False)
    except OSError:
        _reject("cache_invalid")
    if not _secure_cache_file(before):
        _reject("cache_invalid")

    fd = -1
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=cache_fd)
        after = os.fstat(fd)
    except OSError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        _reject("cache_invalid")
    if (
        not _secure_cache_file(after)
        or _file_signature(before) != _file_signature(after)
    ):
        os.close(fd)
        _reject("cache_invalid")
    return fd, after


def _verify_cached(
    cache_fd: int,
    name: str,
    path: Path,
    expected_size: int,
    expected_digest: str,
) -> Path:
    fd, info = _open_regular_nofollow(cache_fd, name)
    digest = hashlib.sha256()
    total = 0
    try:
        if info.st_size != expected_size:
            _reject("cache_invalid")
        while chunk := os.read(fd, 1024 * 1024):
            total += len(chunk)
            if total > expected_size:
                _reject("cache_invalid")
            digest.update(chunk)
        after = os.fstat(fd)
        if (
            not _secure_cache_file(after)
            or _file_signature(info) != _file_signature(after)
        ):
            _reject("cache_invalid")
    except ArtifactDownloadError:
        raise
    except OSError:
        _reject("cache_invalid")
    finally:
        os.close(fd)
    if total != expected_size or digest.hexdigest() != expected_digest:
        _reject("cache_invalid")
    return path


def _default_transport(url: str) -> AbstractContextManager[_Response]:
    return httpx.stream(
        "GET", url, follow_redirects=False, timeout=httpx.Timeout(30.0)
    )


def _download_part(
    part_fd: int,
    url: str,
    expected_size: int,
    expected_digest: str,
    transport: Transport,
) -> None:
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(part_fd, "wb") as output:
            part_fd = -1
            try:
                with transport(url) as response:
                    status_code = response.status_code
                    if 300 <= status_code < 400:
                        _reject("download_redirect")
                    if status_code != 200:
                        _reject("download_failed")
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        if (
                            not content_length.isascii()
                            or not content_length.isdecimal()
                        ):
                            _reject("download_failed")
                        if int(content_length) != expected_size:
                            _reject("size_mismatch")
                    for chunk in response.iter_bytes():
                        if type(chunk) is not bytes:
                            _reject("download_failed")
                        total += len(chunk)
                        if total > expected_size or total > MAX_ASSET_BYTES:
                            _reject("size_mismatch")
                        output.write(chunk)
                        digest.update(chunk)
            except ArtifactDownloadError:
                raise
            except Exception:
                _reject("download_failed")
            if total != expected_size:
                _reject("size_mismatch")
            if digest.hexdigest() != expected_digest:
                _reject("digest_mismatch")
            output.flush()
            os.fsync(output.fileno())
    except ArtifactDownloadError:
        raise
    except OSError:
        _reject("download_failed")
    finally:
        if part_fd >= 0:
            os.close(part_fd)


def _fsync_directory(cache_fd: int) -> None:
    try:
        os.fsync(cache_fd)
    except OSError:
        _reject("download_failed")


def _create_part(cache_fd: int, digest: str) -> tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _ in range(100):
        name = f".{digest}.{secrets.token_hex(12)}.part"
        try:
            fd = os.open(name, flags, 0o600, dir_fd=cache_fd)
        except FileExistsError:
            continue
        except OSError:
            _reject("cache_path_invalid")
        try:
            os.fchmod(fd, 0o600)
            if not _secure_cache_file(os.fstat(fd)):
                _reject("cache_path_invalid")
            return fd, name
        except (ArtifactDownloadError, OSError) as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(name, dir_fd=cache_fd)
            except OSError:
                pass
            if isinstance(exc, ArtifactDownloadError):
                raise
            _reject("cache_path_invalid")
    _reject("cache_path_invalid")


def _entry_exists(cache_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=cache_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        _reject("cache_invalid")


def download_verified_artifact(
    asset: dict[str, Any],
    cache_dir: Path,
    *,
    transport: Transport | None = None,
) -> Path:
    """Download one verified release asset into an immutable digest cache."""
    _name, expected_size, expected_digest, url = _validate_asset(asset)
    if not isinstance(cache_dir, Path) or not cache_dir.is_absolute():
        _reject("cache_path_invalid")
    cache_fd = _open_cache_dir(cache_dir)
    target = cache_dir / expected_digest
    try:
        if _entry_exists(cache_fd, expected_digest):
            return _verify_cached(
                cache_fd,
                expected_digest,
                target,
                expected_size,
                expected_digest,
            )

        part_fd, part = _create_part(cache_fd, expected_digest)
        try:
            _download_part(
                part_fd,
                url,
                expected_size,
                expected_digest,
                transport or _default_transport,
            )
            try:
                os.link(
                    part,
                    expected_digest,
                    src_dir_fd=cache_fd,
                    dst_dir_fd=cache_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                return _verify_cached(
                    cache_fd,
                    expected_digest,
                    target,
                    expected_size,
                    expected_digest,
                )
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    return _verify_cached(
                        cache_fd,
                        expected_digest,
                        target,
                        expected_size,
                        expected_digest,
                    )
                _reject("download_failed")
            try:
                os.unlink(part, dir_fd=cache_fd)
            except OSError:
                _reject("download_failed")
            part = ""
            result = _verify_cached(
                cache_fd,
                expected_digest,
                target,
                expected_size,
                expected_digest,
            )
            _fsync_directory(cache_fd)
            return result
        finally:
            if part:
                try:
                    os.unlink(part, dir_fd=cache_fd)
                except OSError:
                    pass
    finally:
        try:
            os.close(cache_fd)
        except OSError:
            pass


__all__ = [
    "ArtifactDownloadError",
    "MAX_ASSET_BYTES",
    "download_verified_artifact",
]
