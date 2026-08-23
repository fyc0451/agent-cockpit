"""Process-lifetime lock for one canonical Cockpit Next profile."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Mapping


LOCK_NAME = "instance.lock"
LOCK_FD_ENV = "COCKPIT_NEXT_LOCK_FD"
METADATA_VERSION = 1
MAX_METADATA_BYTES = 4096
_adopted_owner: InstanceLock | None = None


class LockError(RuntimeError):
    """Fail-closed instance lock error with a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical_root(values: Mapping[str, str], key: str) -> Path:
    try:
        path = Path(values[key])
    except (KeyError, TypeError) as exc:
        raise LockError("invalid_profile") from exc
    if not path.is_absolute():
        raise LockError("invalid_profile")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise LockError("invalid_profile") from exc


def profile_id(values: Mapping[str, str]) -> str:
    """Return an opaque identity shared by symlink aliases of both roots."""
    data = os.fsencode(_canonical_root(values, "COCKPIT_DATA_DIR"))
    config = os.fsencode(_canonical_root(values, "COCKPIT_CONFIG_DIR"))
    digest = hashlib.sha256(data + b"\0" + config).hexdigest()
    return f"sha256:{digest}"


def _darwin_process_starttime() -> str:
    """Return an opaque, exec-stable process birth identity on macOS."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(os.getpid()), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
        value = result.stdout.strip()
        if result.returncode or not value:
            raise ValueError("process start time missing")
        opaque = int.from_bytes(
            hashlib.sha256(value.encode("ascii")).digest()[:8], "big",
        )
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        raise LockError("process_identity_unavailable") from exc
    if opaque <= 0:
        raise LockError("process_identity_unavailable")
    return str(opaque)


def _process_starttime() -> str:
    if sys.platform == "darwin":
        return _darwin_process_starttime()
    try:
        content = Path("/proc/self/stat").read_text(encoding="ascii")
        value = content[content.rindex(") ") + 2 :].split()[19]
    except (OSError, UnicodeError, ValueError, IndexError) as exc:
        raise LockError("process_identity_unavailable") from exc
    if not value.isdecimal() or int(value) <= 0:
        raise LockError("process_identity_unavailable")
    return value


def _validate_metadata(
    value: object, expected_profile: str, *, current_owner: bool = False,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "version", "pid", "process_starttime", "profile_id",
    }:
        raise LockError("lock_metadata_invalid")
    version = value["version"]
    pid = value["pid"]
    starttime = value["process_starttime"]
    identity = value["profile_id"]
    if version != METADATA_VERSION or isinstance(version, bool):
        raise LockError("lock_metadata_invalid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise LockError("lock_metadata_invalid")
    if (
        not isinstance(starttime, str)
        or not starttime.isdecimal()
        or int(starttime) <= 0
    ):
        raise LockError("lock_metadata_invalid")
    if identity != expected_profile or not isinstance(identity, str):
        raise LockError("lock_metadata_invalid")
    prefix, separator, digest = identity.partition(":")
    if (
        prefix != "sha256"
        or separator != ":"
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise LockError("lock_metadata_invalid")
    if current_owner and (
        pid != os.getpid() or starttime != _process_starttime()
    ):
        raise LockError("lock_owner_mismatch")
    return value


class InstanceLock:
    """An advisory lock whose FD is intentionally inherited across execve."""

    def __init__(self, values: Mapping[str, str]) -> None:
        data_root = _canonical_root(values, "COCKPIT_DATA_DIR")
        self.path = data_root / LOCK_NAME
        self.profile_id = profile_id(values)
        self.fd: int | None = None

    def _open(self) -> int:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise LockError("lock_open_failed") from exc
        try:
            try:
                info = os.fstat(fd)
            except OSError as exc:
                raise LockError("lock_file_validation_failed") from exc
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise LockError("lock_file_unsafe")
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _validate_file(fd: int) -> os.stat_result:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise LockError("lock_fd_invalid") from exc
        if (
            fd <= 2
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise LockError("lock_file_unsafe")
        return info

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("short lock metadata write")
            offset += written

    def _write_metadata(self, fd: int) -> None:
        payload = json.dumps(
            {
                "version": METADATA_VERSION,
                "pid": os.getpid(),
                "process_starttime": _process_starttime(),
                "profile_id": self.profile_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            self._write_all(fd, payload)
            os.fsync(fd)
        except OSError as exc:
            raise LockError("lock_metadata_write_failed") from exc

    def acquire(self) -> "InstanceLock":
        if self.fd is not None:
            return self
        fd = self._open()
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockError("instance_locked") from exc
            except OSError as exc:
                raise LockError("lock_acquire_failed") from exc
            self._write_metadata(fd)
            os.set_inheritable(fd, True)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def read_metadata(self, *, current_owner: bool = False) -> dict[str, object]:
        if self.fd is None:
            raise LockError("lock_not_held")
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            size = 0
            while size <= MAX_METADATA_BYTES:
                chunk = os.read(self.fd, MAX_METADATA_BYTES + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            payload = b"".join(chunks)
            if len(payload) > MAX_METADATA_BYTES:
                raise LockError("lock_metadata_invalid")
            value = json.loads(payload.decode("ascii"))
        except LockError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LockError("lock_metadata_invalid") from exc
        return _validate_metadata(
            value, self.profile_id, current_owner=current_owner,
        )

    @classmethod
    def adopt_inherited(
        cls, values: Mapping[str, str], fd: int,
    ) -> "InstanceLock":
        lock: InstanceLock | None = None
        try:
            inherited = cls._validate_file(fd)
            lock = cls(values)
            try:
                path_info = lock.path.stat()
            except OSError as exc:
                raise LockError("lock_path_invalid") from exc
            if (inherited.st_dev, inherited.st_ino) != (
                path_info.st_dev, path_info.st_ino,
            ):
                raise LockError("lock_fd_wrong_file")
            lock.fd = fd
            lock.read_metadata(current_owner=True)

            probe = lock._open()
            try:
                try:
                    probe_info = os.fstat(probe)
                except OSError as exc:
                    raise LockError("lock_probe_failed") from exc
                if (probe_info.st_dev, probe_info.st_ino) != (
                    inherited.st_dev, inherited.st_ino,
                ):
                    raise LockError("lock_fd_wrong_file")
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                except OSError as exc:
                    raise LockError("lock_probe_failed") from exc
                else:
                    fcntl.flock(probe, fcntl.LOCK_UN)
                    raise LockError("lock_not_held")
            finally:
                try:
                    os.close(probe)
                except OSError:
                    pass
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockError("lock_fd_forged") from exc
            except OSError as exc:
                raise LockError("lock_fd_relock_failed") from exc
            try:
                os.set_inheritable(fd, False)
            except OSError as exc:
                raise LockError("lock_fd_cloexec_failed") from exc
            return lock
        except BaseException:
            if lock is not None:
                lock.fd = None
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *_args: object) -> None:
        self.release()


def register_adopted_owner(owner: InstanceLock) -> None:
    global _adopted_owner
    if not isinstance(owner, InstanceLock) or owner.fd is None:
        raise LockError("lock_owner_invalid")
    owner.read_metadata(current_owner=True)
    if _adopted_owner is not None and _adopted_owner is not owner:
        raise LockError("lock_owner_already_registered")
    _adopted_owner = owner


def require_registered_owner() -> InstanceLock:
    owner = _adopted_owner
    if not isinstance(owner, InstanceLock) or owner.fd is None:
        raise LockError("lock_owner_required")
    owner.read_metadata(current_owner=True)
    return owner
