"""Fail-closed filesystem primitives for immutable generation activation."""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path


_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_ID_RE = re.compile(r"^([0-9a-f]{40})-([0-9a-f]{64})$")
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_LOCK_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_LOCK_NAME = ".generation-switch.lock"


class GenerationSwitchError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class GenerationIdentity:
    source_sha: str
    artifact_digest: str

    def __post_init__(self) -> None:
        if not _SOURCE_SHA_RE.fullmatch(self.source_sha):
            raise GenerationSwitchError("invalid_source_sha")
        if not _ARTIFACT_DIGEST_RE.fullmatch(self.artifact_digest):
            raise GenerationSwitchError("invalid_artifact_digest")

    @property
    def generation_id(self) -> str:
        return f"{self.source_sha}-{self.artifact_digest}"


@dataclass(frozen=True)
class SwitchResult:
    changed: bool
    previous_target: str | None
    current_target: str


def _relative_target(identity: GenerationIdentity) -> str:
    return f"generations/{identity.generation_id}"


def _validate_directory(info: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise GenerationSwitchError(f"{label}_not_directory")
    if info.st_uid != os.getuid():
        raise GenerationSwitchError(f"{label}_owner_unsafe")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise GenerationSwitchError(f"{label}_mode_unsafe")


def _root_path(deploy_root: Path | str) -> Path:
    root = Path(deploy_root).expanduser()
    if not root.is_absolute() or ".." in root.parts:
        raise GenerationSwitchError("deploy_root_invalid")
    return root


def _open_root_chain(deploy_root: Path | str) -> int:
    root = _root_path(deploy_root)
    fd = -1
    try:
        fd = os.open("/", _DIRECTORY_FLAGS)
        for component in root.parts[1:]:
            before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise GenerationSwitchError("deploy_root_symlink")
            if not stat.S_ISDIR(before.st_mode):
                raise GenerationSwitchError("deploy_root_not_directory")
            child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            after = os.fstat(child_fd)
            if not stat.S_ISDIR(after.st_mode) or not _same_inode(before, after):
                os.close(child_fd)
                raise GenerationSwitchError("deploy_root_changed")
            os.close(fd)
            fd = child_fd
        return fd
    except GenerationSwitchError:
        if fd >= 0:
            os.close(fd)
        raise
    except OSError as exc:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        raise GenerationSwitchError("deploy_root_unavailable") from exc


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_layout(deploy_root: Path | str) -> tuple[int, int]:
    root_fd = _open_root_chain(deploy_root)
    generations_fd = -1
    try:
        _validate_directory(os.fstat(root_fd), "deploy_root")
        try:
            before = os.stat("generations", dir_fd=root_fd, follow_symlinks=False)
            generations_fd = os.open(
                "generations", _DIRECTORY_FLAGS, dir_fd=root_fd
            )
        except OSError as exc:
            raise GenerationSwitchError("generations_unavailable") from exc
        after = os.fstat(generations_fd)
        if not _same_inode(before, after):
            raise GenerationSwitchError("generations_changed")
        _validate_directory(after, "generations")
        return root_fd, generations_fd
    except BaseException:
        if generations_fd >= 0:
            os.close(generations_fd)
        os.close(root_fd)
        raise


def _entry_signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode)


def _open_generation(
    generations_fd: int, identity: GenerationIdentity
) -> tuple[int, tuple[int, int, int, int]]:
    generation_fd = -1
    try:
        before = os.stat(
            identity.generation_id,
            dir_fd=generations_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise GenerationSwitchError("generation_unavailable") from exc
    _validate_directory(before, "generation")
    try:
        generation_fd = os.open(
            identity.generation_id,
            _DIRECTORY_FLAGS,
            dir_fd=generations_fd,
        )
        opened = os.fstat(generation_fd)
        after = os.stat(
            identity.generation_id,
            dir_fd=generations_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        if generation_fd >= 0:
            os.close(generation_fd)
        raise GenerationSwitchError("generation_changed") from exc
    if not _same_inode(before, opened) or not _same_inode(opened, after):
        os.close(generation_fd)
        raise GenerationSwitchError("generation_changed")
    _validate_directory(opened, "generation")
    signature = _entry_signature(opened)
    if _entry_signature(before) != signature or _entry_signature(after) != signature:
        os.close(generation_fd)
        raise GenerationSwitchError("generation_changed")
    return generation_fd, signature


def _verify_generation(
    generations_fd: int,
    identity: GenerationIdentity,
    generation_fd: int,
    signature: tuple[int, int, int, int],
) -> None:
    try:
        opened = os.fstat(generation_fd)
        current = os.stat(
            identity.generation_id,
            dir_fd=generations_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise GenerationSwitchError("generation_changed") from exc
    if (
        _entry_signature(opened) != signature
        or _entry_signature(current) != signature
    ):
        raise GenerationSwitchError("generation_changed")
    _validate_directory(opened, "generation")


def _open_lock(root_fd: int) -> int:
    lock_fd = -1
    try:
        lock_fd = os.open(_LOCK_NAME, _LOCK_FLAGS, 0o600, dir_fd=root_fd)
        opened = os.fstat(lock_fd)
        current = os.stat(_LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or _entry_signature(opened) != _entry_signature(current)
        ):
            raise GenerationSwitchError("lock_unsafe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        rebound = os.stat(_LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
        if _entry_signature(rebound) != _entry_signature(opened):
            raise GenerationSwitchError("lock_changed")
        return lock_fd
    except GenerationSwitchError:
        if lock_fd >= 0:
            os.close(lock_fd)
        raise
    except OSError as exc:
        if lock_fd >= 0:
            os.close(lock_fd)
        raise GenerationSwitchError("lock_unavailable") from exc


def _temp_signature(root_fd: int, name: str, target: str) -> tuple[int, int, int, int]:
    try:
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        link_target = os.readlink(name, dir_fd=root_fd)
    except OSError as exc:
        raise GenerationSwitchError("temp_changed") from exc
    if (
        not stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or link_target != target
    ):
        raise GenerationSwitchError("temp_changed")
    return _entry_signature(info)


def _verify_temp(
    root_fd: int,
    name: str,
    target: str,
    signature: tuple[int, int, int, int],
) -> None:
    if _temp_signature(root_fd, name, target) != signature:
        raise GenerationSwitchError("temp_changed")


def _read_current(root_fd: int) -> str | None:
    try:
        info = os.stat("current", dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GenerationSwitchError("current_unavailable") from exc
    if not stat.S_ISLNK(info.st_mode):
        raise GenerationSwitchError("current_not_symlink")
    try:
        target = os.readlink("current", dir_fd=root_fd)
    except OSError as exc:
        raise GenerationSwitchError("current_unavailable") from exc
    parts = target.split("/")
    if len(parts) != 2 or parts[0] != "generations":
        raise GenerationSwitchError("current_target_invalid")
    if not _GENERATION_ID_RE.fullmatch(parts[1]):
        raise GenerationSwitchError("current_target_invalid")
    return target


def _switch(
    deploy_root: Path | str,
    target: GenerationIdentity,
    *,
    expected_previous: GenerationIdentity | None,
) -> SwitchResult:
    root_fd, generations_fd = _open_layout(deploy_root)
    lock_fd = -1
    target_fd = -1
    expected_fd = -1
    temp_name: str | None = None
    temp_signature: tuple[int, int, int, int] | None = None
    try:
        lock_fd = _open_lock(root_fd)
        target_fd, target_signature = _open_generation(generations_fd, target)
        current = _read_current(root_fd)
        target_path = _relative_target(target)
        expected_path = (
            _relative_target(expected_previous) if expected_previous is not None else None
        )
        if current == target_path:
            if current != expected_path:
                raise GenerationSwitchError("current_drift")
            _verify_generation(
                generations_fd, target, target_fd, target_signature
            )
            return SwitchResult(False, current, current)

        if current != expected_path:
            raise GenerationSwitchError("current_drift")
        if expected_previous is not None:
            expected_fd, expected_signature = _open_generation(
                generations_fd, expected_previous
            )

        temp_name = f".current.tmp-{secrets.token_hex(12)}"
        try:
            os.symlink(target_path, temp_name, dir_fd=root_fd)
            temp_signature = _temp_signature(root_fd, temp_name, target_path)
        except OSError as exc:
            raise GenerationSwitchError("temp_create_failed") from exc

        if _read_current(root_fd) != current:
            raise GenerationSwitchError("current_drift")
        _verify_generation(generations_fd, target, target_fd, target_signature)
        if expected_previous is not None:
            _verify_generation(
                generations_fd,
                expected_previous,
                expected_fd,
                expected_signature,
            )
        assert temp_name is not None and temp_signature is not None
        _verify_temp(root_fd, temp_name, target_path, temp_signature)
        try:
            os.replace(
                temp_name,
                "current",
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            temp_signature = None
        except OSError as exc:
            raise GenerationSwitchError("atomic_replace_failed") from exc
        _verify_generation(generations_fd, target, target_fd, target_signature)
        try:
            os.fsync(root_fd)
        except OSError as exc:
            raise GenerationSwitchError("parent_fsync_failed") from exc
        return SwitchResult(True, current, target_path)
    finally:
        if temp_signature is not None and temp_name is not None:
            try:
                _verify_temp(root_fd, temp_name, target_path, temp_signature)
                os.unlink(temp_name, dir_fd=root_fd)
            except (GenerationSwitchError, OSError):
                pass
        if expected_fd >= 0:
            os.close(expected_fd)
        if target_fd >= 0:
            os.close(target_fd)
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(generations_fd)
        os.close(root_fd)


def activate_generation(
    deploy_root: Path | str,
    target: GenerationIdentity,
    *,
    expected_previous: GenerationIdentity | None,
) -> SwitchResult:
    """Atomically activate target if current exactly matches expected_previous.

    The same-root flock serializes compliant controllers. A malicious process
    running as the same uid is outside this primitive's threat model.
    """
    return _switch(deploy_root, target, expected_previous=expected_previous)


def rollback_generation(
    deploy_root: Path | str,
    *,
    journal_previous: GenerationIdentity,
    expected_current: GenerationIdentity,
) -> SwitchResult:
    """Restore only the journal's exact previous generation without drift."""
    return _switch(
        deploy_root,
        journal_previous,
        expected_previous=expected_current,
    )
