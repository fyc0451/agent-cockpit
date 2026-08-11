"""Fail-closed filesystem primitives for immutable generation activation."""

from __future__ import annotations

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


def _canonical_root(deploy_root: Path | str) -> Path:
    root = Path(deploy_root).expanduser()
    if not root.is_absolute() or ".." in root.parts:
        raise GenerationSwitchError("deploy_root_invalid")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GenerationSwitchError("deploy_root_unavailable") from exc
    if resolved != root:
        raise GenerationSwitchError("deploy_root_symlink")
    return root


def _open_layout(deploy_root: Path | str) -> tuple[int, int]:
    root = _canonical_root(deploy_root)
    try:
        root_fd = os.open(root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise GenerationSwitchError("deploy_root_unavailable") from exc
    generations_fd = -1
    try:
        _validate_directory(os.fstat(root_fd), "deploy_root")
        try:
            generations_fd = os.open("generations", _DIRECTORY_FLAGS, dir_fd=root_fd)
        except OSError as exc:
            raise GenerationSwitchError("generations_unavailable") from exc
        _validate_directory(os.fstat(generations_fd), "generations")
        return root_fd, generations_fd
    except BaseException:
        if generations_fd >= 0:
            os.close(generations_fd)
        os.close(root_fd)
        raise


def _validate_generation(generations_fd: int, identity: GenerationIdentity) -> None:
    try:
        info = os.stat(
            identity.generation_id,
            dir_fd=generations_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise GenerationSwitchError("generation_unavailable") from exc
    _validate_directory(info, "generation")


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
    temp_name: str | None = None
    temp_created = False
    try:
        _validate_generation(generations_fd, target)
        current = _read_current(root_fd)
        target_path = _relative_target(target)
        if current == target_path:
            return SwitchResult(False, current, current)

        expected_path = (
            _relative_target(expected_previous) if expected_previous is not None else None
        )
        if current != expected_path:
            raise GenerationSwitchError("current_drift")
        if expected_previous is not None:
            _validate_generation(generations_fd, expected_previous)

        temp_name = f".current.tmp-{secrets.token_hex(12)}"
        try:
            os.symlink(target_path, temp_name, dir_fd=root_fd)
            temp_created = True
        except OSError as exc:
            raise GenerationSwitchError("temp_create_failed") from exc

        if _read_current(root_fd) != current:
            raise GenerationSwitchError("current_drift")
        try:
            os.replace(
                temp_name,
                "current",
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            temp_created = False
        except OSError as exc:
            raise GenerationSwitchError("atomic_replace_failed") from exc
        try:
            os.fsync(root_fd)
        except OSError as exc:
            raise GenerationSwitchError("parent_fsync_failed") from exc
        return SwitchResult(True, current, target_path)
    finally:
        if temp_created and temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=root_fd)
            except OSError:
                pass
        os.close(generations_fd)
        os.close(root_fd)


def activate_generation(
    deploy_root: Path | str,
    target: GenerationIdentity,
    *,
    expected_previous: GenerationIdentity | None,
) -> SwitchResult:
    """Atomically activate target if current exactly matches expected_previous."""
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
