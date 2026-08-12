"""Detached IPC seam from a prepared release to the external controller."""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import generation_prepare
from . import maintenance_controller
from . import maintenance_request
from . import supervisor_adapter


_LAUNCHER_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class MaintenanceIpcError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ControllerAccepted:
    pid: int
    accepted: bool


def _validate_plan(
    plan: maintenance_controller.ControllerPlan,
) -> maintenance_controller.ControllerPlan:
    if type(plan) is not maintenance_controller.ControllerPlan:
        raise MaintenanceIpcError("plan_invalid")
    try:
        canonical = maintenance_controller.build_controller_plan(
            state_root=plan.state_root,
            deploy_root=plan.deploy_root,
            current=plan.current,
            controller_root=plan.controller_root,
        )
    except maintenance_controller.ControllerPreflightError as exc:
        raise MaintenanceIpcError("plan_invalid") from exc
    if canonical != plan:
        raise MaintenanceIpcError("plan_invalid")
    return canonical


def _argv(
    plan: maintenance_controller.ControllerPlan,
    prepared: generation_prepare.PreparedGeneration,
    request_id: str,
    controller_launcher: Path,
) -> tuple[str, ...]:
    if not isinstance(controller_launcher, Path):
        raise MaintenanceIpcError("launcher_invalid")
    launcher_fd = -1
    try:
        before = controller_launcher.lstat()
        launcher_fd = os.open(controller_launcher, _LAUNCHER_FLAGS)
        opened = os.fstat(launcher_fd)
        after = controller_launcher.lstat()
    except OSError as exc:
        raise MaintenanceIpcError("launcher_invalid") from exc
    finally:
        if launcher_fd >= 0:
            os.close(launcher_fd)
    identities = {
        (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink)
        for info in (before, opened, after)
    }
    if len(identities) != 1:
        raise MaintenanceIpcError("launcher_invalid")
    if not (
        stat.S_ISREG(opened.st_mode)
        and opened.st_uid == os.getuid()
        and stat.S_IMODE(opened.st_mode) == 0o700
        and opened.st_nlink == 1
    ):
        raise MaintenanceIpcError("launcher_invalid")
    raw = (
        str(controller_launcher),
        "maintenance-controller",
        "execute",
        "--state-root", str(plan.state_root),
        "--deploy-root", str(plan.deploy_root),
        "--current", str(plan.current),
        "--controller-root", str(plan.controller_root),
        "--request-id", request_id,
        "--version", prepared.version,
        "--source-sha", prepared.source_sha,
        "--artifact-digest", prepared.artifact_digest,
        "--generation-id", prepared.generation_id,
        "--generation-path", str(prepared.generation_path),
        "--launcher-path", str(prepared.launcher_path),
    )
    try:
        return supervisor_adapter.normalize_controller_argv(
            raw,
            controller_dir=plan.controller_root,
            current=plan.current,
            deploy_root=plan.deploy_root,
        )
    except supervisor_adapter.SupervisorAdapterError as exc:
        raise MaintenanceIpcError("launcher_invalid") from exc


def spawn_maintenance_controller(
    *,
    plan: maintenance_controller.ControllerPlan,
    prepared: generation_prepare.PreparedGeneration,
    request_id: str,
    controller_launcher: Path,
    popen: Callable[..., Any] = subprocess.Popen,
) -> ControllerAccepted:
    """Validate fixed IPC fields and detach one external controller process."""
    canonical_plan = _validate_plan(plan)
    try:
        maintenance_request.build_maintenance_request(
            plan=canonical_plan,
            prepared=prepared,
            request_id=request_id,
        )
    except maintenance_request.MaintenanceRequestBuildError as exc:
        raise MaintenanceIpcError("request_invalid") from exc
    argv = _argv(canonical_plan, prepared, request_id, controller_launcher)
    if not callable(popen):
        raise MaintenanceIpcError("spawn_invalid")
    try:
        process = popen(
            argv,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            shell=False,
        )
    except Exception as exc:
        raise MaintenanceIpcError("spawn_failed") from exc
    pid = getattr(process, "pid", None)
    if type(pid) is not int or pid <= 0:
        raise MaintenanceIpcError("spawn_result_invalid")
    return ControllerAccepted(pid=pid, accepted=True)


__all__ = [
    "ControllerAccepted",
    "MaintenanceIpcError",
    "spawn_maintenance_controller",
]
