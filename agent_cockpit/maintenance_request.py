"""Build a canonical maintenance request from one prepared generation."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from . import generation_prepare
from . import generation_switch
from . import maintenance_controller
from . import maintenance_evidence
from . import maintenance_executor


_REQUEST_RE = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
_VERSION_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_DIRECTORY", 0
) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_NOFOLLOW", 0
)
_LAUNCHER_PATH = Path("bin") / "agent-cockpit"
_MAX_VERSION_BYTES = 64


class MaintenanceRequestBuildError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise MaintenanceRequestBuildError(code)


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_checked(
    path: Path | str,
    *,
    directory: bool,
    mode: int,
    dir_fd: int | None = None,
) -> int:
    fd = -1
    try:
        before = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        fd = os.open(path, _DIR_FLAGS if directory else _READ_FLAGS, dir_fd=dir_fd)
        opened = os.fstat(fd)
        after = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            not expected_kind(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != mode
            or (not directory and (opened.st_nlink != 1 or opened.st_size <= 0))
            or _signature(before) != _signature(opened)
            or _signature(opened) != _signature(after)
        ):
            _fail("generation_invalid")
        return fd
    except MaintenanceRequestBuildError:
        if fd >= 0:
            os.close(fd)
        raise
    except OSError:
        if fd >= 0:
            os.close(fd)
        _fail("generation_invalid")


def _read_version(generation_fd: int) -> str:
    version_fd = -1
    try:
        version_fd = _open_checked(
            "VERSION", directory=False, mode=0o600, dir_fd=generation_fd
        )
        before = os.fstat(version_fd)
        raw = os.read(version_fd, _MAX_VERSION_BYTES + 1)
        after = os.fstat(version_fd)
        if (
            before.st_size != len(raw)
            or len(raw) > _MAX_VERSION_BYTES
            or _signature(before) != _signature(after)
        ):
            _fail("generation_invalid")
        value = raw.decode("ascii")
    except (OSError, UnicodeError):
        _fail("generation_invalid")
    finally:
        if version_fd >= 0:
            os.close(version_fd)
    if not value.endswith("\n") or _VERSION_RE.fullmatch(value[:-1]) is None:
        _fail("generation_invalid")
    return value[:-1]


def _validate_launcher(generation_fd: int) -> None:
    bin_fd = launcher_fd = -1
    try:
        bin_fd = _open_checked(
            _LAUNCHER_PATH.parts[0],
            directory=True,
            mode=0o700,
            dir_fd=generation_fd,
        )
        launcher_fd = _open_checked(
            _LAUNCHER_PATH.parts[1], directory=False, mode=0o700, dir_fd=bin_fd
        )
    finally:
        if launcher_fd >= 0:
            os.close(launcher_fd)
        if bin_fd >= 0:
            os.close(bin_fd)


def _validate_prepared(
    plan: maintenance_controller.ControllerPlan,
    prepared: generation_prepare.PreparedGeneration,
) -> generation_switch.GenerationIdentity:
    if (
        type(prepared) is not generation_prepare.PreparedGeneration
        or type(prepared.version) is not str
        or _VERSION_RE.fullmatch(prepared.version) is None
        or type(prepared.source_sha) is not str
        or type(prepared.artifact_digest) is not str
        or type(prepared.generation_id) is not str
        or not isinstance(prepared.generation_path, Path)
        or not isinstance(prepared.launcher_path, Path)
    ):
        _fail("prepared_invalid")
    try:
        target = generation_switch.GenerationIdentity(
            prepared.source_sha, prepared.artifact_digest
        )
    except generation_switch.GenerationSwitchError:
        _fail("prepared_invalid")
    expected_root = plan.deploy_root / "generations" / target.generation_id
    if (
        prepared.generation_id != target.generation_id
        or not prepared.generation_path.is_absolute()
        or prepared.generation_path != expected_root
        or prepared.launcher_path != expected_root / _LAUNCHER_PATH
        or ".." in prepared.generation_path.parts
        or ".." in prepared.launcher_path.parts
    ):
        _fail("prepared_invalid")
    return target


def _generation_version(path: Path, *, launcher: bool = False) -> str:
    generation_fd = _open_checked(path, directory=True, mode=0o700)
    try:
        value = _read_version(generation_fd)
        if launcher:
            _validate_launcher(generation_fd)
        return value
    finally:
        os.close(generation_fd)


def build_maintenance_request(
    *,
    plan: maintenance_controller.ControllerPlan,
    prepared: generation_prepare.PreparedGeneration,
    request_id: str,
) -> maintenance_executor.MaintenanceRequest:
    """Validate immutable inputs and return an executor request without writes."""
    if type(request_id) is not str or _REQUEST_RE.fullmatch(request_id) is None:
        _fail("request_invalid")
    try:
        maintenance_evidence.validate_evidence_plan(plan)
    except maintenance_evidence.EvidenceEnvironmentError:
        _fail("request_invalid")

    target = _validate_prepared(plan, prepared)
    target_version = _generation_version(prepared.generation_path, launcher=True)
    if target_version != prepared.version:
        _fail("generation_invalid")

    try:
        previous = maintenance_executor.inspect_current_generation(plan)
    except Exception:
        _fail("current_invalid")
    if target == previous:
        _fail("already_current")

    previous_root = plan.deploy_root / "generations" / previous.generation_id
    previous_version = _generation_version(previous_root)
    try:
        evidence_path = maintenance_evidence.evidence_binding_path(
            plan=plan,
            request_id=request_id,
            role="target",
            generation=target,
        )
    except maintenance_evidence.EvidenceEnvironmentError:
        _fail("request_invalid")

    return maintenance_executor.MaintenanceRequest(
        plan=plan,
        request_id=request_id,
        target_version=target_version,
        target=target,
        previous_version=previous_version,
        previous=previous,
        target_root=prepared.generation_path,
        snapshot_root=plan.state_root
        / maintenance_executor.SNAPSHOT_DIR_NAME
        / request_id,
        evidence_path=evidence_path,
    )


__all__ = [
    "MaintenanceRequestBuildError",
    "build_maintenance_request",
]
