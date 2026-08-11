"""Fixed release-external paths and trust material for Server upgrades."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import maintenance_controller


CONTROLLER_LAUNCHER = Path("bin") / "agent-cockpit"
PUBLIC_KEY_NAME = "release-public-key.bin"
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_DIRECTORY", 0
) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_NOFOLLOW", 0
)


class UpgradeLayoutError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class UpgradeLayout:
    state_root: Path
    deploy_root: Path
    current: Path
    controller_root: Path
    controller_launcher: Path
    public_key_path: Path


def _fail(code: str) -> None:
    raise UpgradeLayoutError(code)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_layout(layout: UpgradeLayout) -> None:
    if type(layout) is not UpgradeLayout:
        _fail("layout_invalid")
    paths = (
        layout.state_root,
        layout.deploy_root,
        layout.current,
        layout.controller_root,
        layout.controller_launcher,
        layout.public_key_path,
    )
    if (
        any(not isinstance(path, Path) or not path.is_absolute() for path in paths)
        or any(".." in path.parts for path in paths)
        or layout.current != layout.deploy_root / "current"
        or layout.controller_launcher
        != layout.controller_root / CONTROLLER_LAUNCHER
        or layout.public_key_path != layout.controller_root / PUBLIC_KEY_NAME
    ):
        _fail("layout_invalid")
    roots = (layout.state_root, layout.deploy_root, layout.controller_root)
    if any(
        left == right or _inside(left, right) or _inside(right, left)
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    ):
        _fail("layout_invalid")


def default_upgrade_layout(*, home: Path | None = None) -> UpgradeLayout:
    root = Path.home() if home is None else home
    if (
        not isinstance(root, Path)
        or not root.is_absolute()
        or root == Path("/")
        or ".." in root.parts
    ):
        _fail("layout_invalid")
    deploy = root / ".local" / "share" / "agent-cockpit-server"
    controller = root / ".local" / "share" / "agent-cockpit-controller"
    layout = UpgradeLayout(
        state_root=root / ".local" / "state" / "agent-cockpit" / "upgrade-v2",
        deploy_root=deploy,
        current=deploy / "current",
        controller_root=controller,
        controller_launcher=controller / CONTROLLER_LAUNCHER,
        public_key_path=controller / PUBLIC_KEY_NAME,
    )
    _validate_layout(layout)
    return layout


def build_controller_plan(
    layout: UpgradeLayout,
) -> maintenance_controller.ControllerPlan:
    _validate_layout(layout)
    try:
        return maintenance_controller.build_controller_plan(
            state_root=layout.state_root,
            deploy_root=layout.deploy_root,
            current=layout.current,
            controller_root=layout.controller_root,
        )
    except maintenance_controller.ControllerPreflightError:
        _fail("layout_invalid")


def _signature(info: os.stat_result) -> tuple[int, ...]:
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


def _open_checked(
    path: Path | str,
    *,
    directory: bool,
    mode: int,
    code: str,
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
            or (not directory and opened.st_nlink != 1)
            or _signature(before) != _signature(opened)
            or _signature(opened) != _signature(after)
        ):
            _fail(code)
        return fd
    except UpgradeLayoutError:
        if fd >= 0:
            os.close(fd)
        raise
    except OSError:
        if fd >= 0:
            os.close(fd)
        _fail(code)


def validate_controller_launcher(layout: UpgradeLayout) -> Path:
    _validate_layout(layout)
    root_fd = bin_fd = launcher_fd = -1
    try:
        root_fd = _open_checked(
            layout.controller_root,
            directory=True,
            mode=0o700,
            code="controller_unavailable",
        )
        bin_fd = _open_checked(
            CONTROLLER_LAUNCHER.parts[0],
            directory=True,
            mode=0o700,
            code="controller_unavailable",
            dir_fd=root_fd,
        )
        launcher_fd = _open_checked(
            CONTROLLER_LAUNCHER.parts[1],
            directory=False,
            mode=0o700,
            code="controller_unavailable",
            dir_fd=bin_fd,
        )
        if os.fstat(launcher_fd).st_size <= 0:
            _fail("controller_unavailable")
        return layout.controller_launcher
    finally:
        for fd in (launcher_fd, bin_fd, root_fd):
            if fd >= 0:
                os.close(fd)


def load_release_public_key(layout: UpgradeLayout) -> bytes:
    _validate_layout(layout)
    root_fd = key_fd = -1
    try:
        root_fd = _open_checked(
            layout.controller_root,
            directory=True,
            mode=0o700,
            code="trust_unavailable",
        )
        key_fd = _open_checked(
            PUBLIC_KEY_NAME,
            directory=False,
            mode=0o600,
            code="trust_unavailable",
            dir_fd=root_fd,
        )
        before = os.fstat(key_fd)
        raw = os.read(key_fd, 33)
        after = os.fstat(key_fd)
        if len(raw) != 32 or before.st_size != 32 or _signature(before) != _signature(after):
            _fail("trust_unavailable")
        return raw
    except OSError:
        _fail("trust_unavailable")
    finally:
        for fd in (key_fd, root_fd):
            if fd >= 0:
                os.close(fd)


__all__ = [
    "UpgradeLayout",
    "UpgradeLayoutError",
    "build_controller_plan",
    "default_upgrade_layout",
    "load_release_public_key",
    "validate_controller_launcher",
]
