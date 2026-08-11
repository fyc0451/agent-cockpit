"""Release-external controller preflight with no upgrade mutation capability."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import supervisor_adapter
import upgrade_journal

LOCK_NAME = "controller.lock"
JOURNAL_DIR_NAME = "upgrade-journal"
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_DIRECTORY", 0
) | getattr(os, "O_NOFOLLOW", 0)
_LOCK_FLAGS = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_NOFOLLOW", 0
)
_STATUS_FIELDS = (
    "request_id",
    "target_digest",
    "target_source_sha",
    "target_generation",
    "previous_generation",
    "stage",
    "intent",
    "revision",
    "primary_error_code",
    "rollback_error_code",
)


class ControllerPreflightError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ControllerPlan:
    state_root: Path
    journal_root: Path
    deploy_root: Path
    current: Path
    controller_root: Path
    engine: str = upgrade_journal.ENGINE
    schema_version: int = upgrade_journal.SCHEMA_VERSION


def _fail(code: str) -> None:
    raise ControllerPreflightError(code)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _open_directory_chain(path: Path, code: str) -> tuple[int, os.stat_result]:
    fd = -1
    try:
        fd = os.open("/", _DIR_FLAGS)
        current = os.fstat(fd)
        for component in path.parts[1:]:
            before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            after = os.fstat(child)
            if not stat.S_ISDIR(before.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                os.close(child)
                _fail(code)
            os.close(fd)
            fd, current = child, after
        return fd, current
    except ControllerPreflightError:
        if fd >= 0: os.close(fd)
        raise
    except OSError:
        if fd >= 0: os.close(fd)
        _fail(code)


def _secure_state(path: Path) -> None:
    fd, info = _open_directory_chain(path, "state_unsafe")
    os.close(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        _fail("state_unsafe")


def _validate_plan(plan: ControllerPlan) -> None:
    if (
        not isinstance(plan, ControllerPlan)
        or plan.journal_root != plan.state_root / JOURNAL_DIR_NAME
        or plan.current != plan.deploy_root / "current"
        or plan.engine != upgrade_journal.ENGINE
        or plan.schema_version != upgrade_journal.SCHEMA_VERSION
    ):
        _fail("plan_invalid")
    try:
        supervisor_adapter.require_canonical_current_and_deploy(current=plan.current, deploy_root=plan.deploy_root)
        supervisor_adapter.validate_controller_path(plan.controller_root, current=plan.current, deploy_root=plan.deploy_root)
    except supervisor_adapter.SupervisorAdapterError:
        _fail("plan_invalid")


def build_controller_plan(
    *,
    state_root: Path,
    deploy_root: Path,
    current: Path,
    controller_root: Path,
) -> ControllerPlan:
    """Validate and freeze canonical release-external controller paths."""
    if not all(isinstance(path, Path) for path in (state_root, deploy_root, current, controller_root)):
        _fail("plan_invalid")
    try:
        root, current_path = supervisor_adapter.require_canonical_current_and_deploy(current=current, deploy_root=deploy_root)
        controller = supervisor_adapter.validate_controller_path(controller_root, current=current_path, deploy_root=root)
        state = supervisor_adapter.validate_absolute_path(state_root, role="state_root")
    except supervisor_adapter.SupervisorAdapterError:
        _fail("plan_invalid")
    try:
        state_resolved, root_resolved = state.resolve(strict=True), root.resolve(strict=True)
    except OSError:
        _fail("state_unsafe")
    if state == root or _inside(state, root) or state_resolved == root_resolved or _inside(state_resolved, root_resolved):
        _fail("plan_invalid")
    _secure_state(state)
    return ControllerPlan(state, state / JOURNAL_DIR_NAME, root, current_path, controller)


@contextmanager
def controller_lock(plan: ControllerPlan) -> Iterator[None]:
    """Hold the controller's nonblocking lock; never unlink it."""
    _validate_plan(plan)
    state_fd, info = _open_directory_chain(plan.state_root, "state_unsafe")
    lock_fd = -1
    try:
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            _fail("state_unsafe")
        try:
            before = os.stat(LOCK_NAME, dir_fd=state_fd, follow_symlinks=False)
        except FileNotFoundError:
            lock_fd = os.open(LOCK_NAME, _LOCK_FLAGS | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=state_fd)
            os.fchmod(lock_fd, 0o600)
            os.fsync(state_fd)
            before = os.fstat(lock_fd)
        else:
            lock_fd = os.open(LOCK_NAME, _LOCK_FLAGS, dir_fd=state_fd)
        opened = os.fstat(lock_fd)
        current = os.stat(LOCK_NAME, dir_fd=state_fd, follow_symlinks=False)
        signature = lambda value: (value.st_dev, value.st_ino, value.st_uid, value.st_mode)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1 or signature(before) != signature(opened) or signature(current) != signature(opened):
            _fail("lock_unsafe")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("controller_locked")
    except ControllerPreflightError:
        if lock_fd >= 0: os.close(lock_fd)
        os.close(state_fd)
        raise
    except OSError:
        if lock_fd >= 0: os.close(lock_fd)
        os.close(state_fd)
        _fail("lock_unsafe")
    try:
        yield
    finally:
        if lock_fd >= 0: os.close(lock_fd)
        os.close(state_fd)


def read_controller_status(plan: ControllerPlan) -> dict[str, object]:
    """Read journal state without locking, creating, repairing, or reconciling."""
    _validate_plan(plan)
    try:
        value = upgrade_journal.load_journal(root=plan.journal_root)
    except upgrade_journal.UpgradeJournalError as exc:
        if exc.code == "journal_missing":
            return {"state": "idle", "journal": None}
        _fail("journal_invalid")
    return {"state": value["stage"], "journal": {field: value[field] for field in _STATUS_FIELDS}}


__all__ = [
    "ControllerPlan",
    "ControllerPreflightError",
    "build_controller_plan",
    "controller_lock",
    "read_controller_status",
]
