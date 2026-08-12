"""Release-external controller preflight with no upgrade mutation capability."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import supervisor_adapter
from . import upgrade_journal

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


_LEASE_TOKEN = object()


class ControllerLease:
    __slots__ = (
        "_active",
        "_lock_fd",
        "_lock_identity",
        "_plan",
        "_state_fd",
        "_state_identity",
    )

    def __init__(
        self,
        token: object,
        plan: ControllerPlan,
        state_fd: int,
        state_identity: tuple[int, ...],
        lock_fd: int,
        lock_identity: tuple[int, ...],
    ) -> None:
        if token is not _LEASE_TOKEN:
            _fail("controller_lease_invalid")
        self._plan = plan
        self._state_fd = state_fd
        self._state_identity = state_identity
        self._lock_fd = lock_fd
        self._lock_identity = lock_identity
        self._active = True

    def _invalidate(self) -> None:
        self._active = False


def _fail(code: str) -> None:
    raise ControllerPreflightError(code)


def _state_identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode)


def _lock_identity(info: os.stat_result) -> tuple[int, ...]:
    return (*_state_identity(info), info.st_nlink)


def _secure_lock(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
    )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _open_directory_chain(
    path: Path, code: str, *, missing_ok: bool = False
) -> tuple[int, os.stat_result] | None:
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
    except FileNotFoundError:
        if fd >= 0: os.close(fd)
        if missing_ok:
            return None
        _fail(code)
    except OSError:
        if fd >= 0: os.close(fd)
        _fail(code)


def _secure_state(
    path: Path, *, missing_ok: bool = False
) -> tuple[int, os.stat_result] | None:
    opened = _open_directory_chain(path, "state_unsafe", missing_ok=missing_ok)
    if opened is None:
        return None
    fd, info = opened
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        _fail("state_unsafe")
    return fd, info


def _validate_plan(plan: ControllerPlan) -> None:
    if (
        not isinstance(plan, ControllerPlan)
        or not all(
            isinstance(path, Path)
            for path in (
                plan.state_root,
                plan.journal_root,
                plan.deploy_root,
                plan.current,
                plan.controller_root,
            )
        )
        or plan.journal_root != plan.state_root / JOURNAL_DIR_NAME
        or plan.current != plan.deploy_root / "current"
        or plan.engine != upgrade_journal.ENGINE
        or plan.schema_version != upgrade_journal.SCHEMA_VERSION
    ):
        _fail("plan_invalid")
    try:
        state = supervisor_adapter.validate_absolute_path(
            plan.state_root, role="state_root"
        )
        supervisor_adapter.require_canonical_current_and_deploy(current=plan.current, deploy_root=plan.deploy_root)
        supervisor_adapter.validate_controller_path(plan.controller_root, current=plan.current, deploy_root=plan.deploy_root)
    except supervisor_adapter.SupervisorAdapterError:
        _fail("plan_invalid")
    if state == plan.deploy_root or _inside(state, plan.deploy_root):
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
    if state == root or _inside(state, root):
        _fail("plan_invalid")
    return ControllerPlan(state, state / JOURNAL_DIR_NAME, root, current_path, controller)


def require_controller_lease(
    *, plan: ControllerPlan, lease: ControllerLease
) -> None:
    """Require one live lease bound to this exact controller plan."""
    if (
        type(lease) is not ControllerLease
        or getattr(lease, "_active", False) is not True
        or getattr(lease, "_plan", None) != plan
    ):
        _fail("controller_lease_invalid")
    rebound_fd = -1
    try:
        state_opened = os.fstat(lease._state_fd)
        lock_opened = os.fstat(lease._lock_fd)
        rebound = _secure_state(plan.state_root)
        assert rebound is not None
        rebound_fd, state_current = rebound
        lock_current = os.stat(
            LOCK_NAME, dir_fd=rebound_fd, follow_symlinks=False
        )
        if (
            _state_identity(state_opened) != lease._state_identity
            or _state_identity(state_current) != lease._state_identity
            or _lock_identity(lock_opened) != lease._lock_identity
            or _lock_identity(lock_current) != lease._lock_identity
            or not _secure_lock(lock_opened)
            or not _secure_lock(lock_current)
        ):
            _fail("controller_lease_invalid")
    except ControllerPreflightError:
        _fail("controller_lease_invalid")
    except (AttributeError, OSError, ValueError, TypeError):
        _fail("controller_lease_invalid")
    finally:
        if rebound_fd >= 0:
            os.close(rebound_fd)


@contextmanager
def controller_lock(plan: ControllerPlan) -> Iterator[ControllerLease]:
    """Hold the controller's nonblocking lock; never unlink it."""
    _validate_plan(plan)
    secured = _secure_state(plan.state_root)
    assert secured is not None
    state_fd, _ = secured
    lock_fd = -1
    lease: ControllerLease | None = None
    try:
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
        if not _secure_lock(opened) or _lock_identity(before) != _lock_identity(opened) or _lock_identity(current) != _lock_identity(opened):
            _fail("lock_unsafe")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("controller_locked")
        state_identity = _state_identity(os.fstat(state_fd))
        lock_identity = _lock_identity(os.fstat(lock_fd))
        lease = ControllerLease(
            _LEASE_TOKEN,
            plan,
            state_fd,
            state_identity,
            lock_fd,
            lock_identity,
        )
    except ControllerPreflightError:
        if lock_fd >= 0: os.close(lock_fd)
        os.close(state_fd)
        raise
    except OSError:
        if lock_fd >= 0: os.close(lock_fd)
        os.close(state_fd)
        _fail("lock_unsafe")
    try:
        assert lease is not None
        yield lease
    finally:
        if lease is not None:
            lease._invalidate()
        if lock_fd >= 0: os.close(lock_fd)
        os.close(state_fd)


def read_controller_status(plan: ControllerPlan) -> dict[str, object]:
    """Read journal state without locking, creating, repairing, or reconciling."""
    _validate_plan(plan)
    secured = _secure_state(plan.state_root, missing_ok=True)
    if secured is None:
        return {"state": "idle", "journal": None}
    state_fd, state_info = secured
    try:
        try:
            value = upgrade_journal.load_journal(root=plan.journal_root)
            result = {
                "state": value["stage"],
                "journal": {field: value[field] for field in _STATUS_FIELDS},
            }
        except upgrade_journal.UpgradeJournalError as exc:
            if exc.code == "journal_missing":
                result = {"state": "idle", "journal": None}
            else:
                _fail("journal_invalid")
        rebound = _secure_state(plan.state_root)
        assert rebound is not None
        rebound_fd, rebound_info = rebound
        try:
            if (state_info.st_dev, state_info.st_ino) != (
                rebound_info.st_dev,
                rebound_info.st_ino,
            ):
                _fail("state_unsafe")
        finally:
            os.close(rebound_fd)
        return result
    finally:
        os.close(state_fd)


__all__ = [
    "ControllerLease",
    "ControllerPlan",
    "ControllerPreflightError",
    "build_controller_plan",
    "controller_lock",
    "require_controller_lease",
    "read_controller_status",
]
