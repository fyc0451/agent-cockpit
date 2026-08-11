from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

import httpx

from release_index import MAX_ASSET_BYTES, MAX_ASSET_NAME_BYTES


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


def _prepare_cache_dir(cache_dir: Path) -> None:
    try:
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = cache_dir.lstat()
    except OSError:
        _reject("cache_path_invalid")
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _reject("cache_path_invalid")


def _open_regular_nofollow(path: Path) -> tuple[int, os.stat_result]:
    try:
        before = path.lstat()
    except OSError:
        _reject("cache_invalid")
    if not stat.S_ISREG(before.st_mode):
        _reject("cache_invalid")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        after = os.fstat(fd)
    except OSError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        _reject("cache_invalid")
    if (
        not stat.S_ISREG(after.st_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
    ):
        os.close(fd)
        _reject("cache_invalid")
    return fd, after


def _verify_cached(path: Path, expected_size: int, expected_digest: str) -> Path:
    fd, info = _open_regular_nofollow(path)
    digest = hashlib.sha256()
    total = 0
    try:
        if info.st_size != expected_size:
            _reject("cache_invalid")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > expected_size:
                    _reject("cache_invalid")
                digest.update(chunk)
    except ArtifactDownloadError:
        raise
    except OSError:
        _reject("cache_invalid")
    finally:
        if fd >= 0:
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


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        _reject("download_failed")


def _create_part(cache_dir: Path, digest: str) -> tuple[int, Path]:
    fd = -1
    raw_part: str | None = None
    try:
        fd, raw_part = tempfile.mkstemp(
            prefix=f".{digest}.", suffix=".part", dir=cache_dir
        )
        os.fchmod(fd, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            _reject("cache_path_invalid")
        return fd, Path(raw_part)
    except (ArtifactDownloadError, OSError) as exc:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if raw_part is not None:
            try:
                Path(raw_part).unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, ArtifactDownloadError):
            raise
        _reject("cache_path_invalid")


def download_verified_artifact(
    asset: dict[str, Any],
    cache_dir: Path,
    *,
    transport: Transport | None = None,
) -> Path:
    """Download one verified release asset into an immutable digest cache."""
    _name, expected_size, expected_digest, url = _validate_asset(asset)
    if not isinstance(cache_dir, Path):
        _reject("cache_path_invalid")
    _prepare_cache_dir(cache_dir)
    target = cache_dir / expected_digest
    if target.exists() or target.is_symlink():
        return _verify_cached(target, expected_size, expected_digest)

    part_fd, part = _create_part(cache_dir, expected_digest)
    try:
        _download_part(
            part_fd,
            url,
            expected_size,
            expected_digest,
            transport or _default_transport,
        )
        try:
            os.link(part, target)
        except FileExistsError:
            return _verify_cached(target, expected_size, expected_digest)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return _verify_cached(target, expected_size, expected_digest)
            _reject("download_failed")
        part.unlink()
        _fsync_directory(cache_dir)
        return target
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "ArtifactDownloadError",
    "MAX_ASSET_BYTES",
    "download_verified_artifact",
]
