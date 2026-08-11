"""First-install a release-external native maintenance controller."""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path

import generation_prepare
import generation_switch
import upgrade_layout


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_DIRECTORY", 0
) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_NOFOLLOW", 0
)


class NativeControllerInstallError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise NativeControllerInstallError(code)


def _validate_layout(layout: upgrade_layout.UpgradeLayout) -> None:
    if type(layout) is not upgrade_layout.UpgradeLayout:
        _fail("layout_invalid")
    try:
        plan = upgrade_layout.build_controller_plan(layout)
    except Exception:
        _fail("layout_invalid")
    if (
        plan.state_root != layout.state_root
        or plan.deploy_root != layout.deploy_root
        or plan.current != layout.current
        or plan.controller_root != layout.controller_root
        or layout.controller_launcher
        != layout.controller_root / upgrade_layout.CONTROLLER_LAUNCHER
        or layout.public_key_path
        != layout.controller_root / upgrade_layout.PUBLIC_KEY_NAME
    ):
        _fail("layout_invalid")


def _validate_prepared(
    layout: upgrade_layout.UpgradeLayout,
    prepared: generation_prepare.PreparedGeneration,
) -> None:
    if (
        type(prepared) is not generation_prepare.PreparedGeneration
        or type(prepared.version) is not str
        or type(prepared.source_sha) is not str
        or type(prepared.artifact_digest) is not str
        or type(prepared.generation_id) is not str
        or not isinstance(prepared.generation_path, Path)
        or not isinstance(prepared.launcher_path, Path)
    ):
        _fail("prepared_invalid")
    try:
        identity = generation_switch.GenerationIdentity(
            prepared.source_sha, prepared.artifact_digest
        )
    except generation_switch.GenerationSwitchError:
        _fail("prepared_invalid")
    expected = layout.deploy_root / "generations" / identity.generation_id
    if (
        prepared.generation_id != identity.generation_id
        or not prepared.generation_path.is_absolute()
        or prepared.generation_path != expected
        or prepared.launcher_path
        != expected / upgrade_layout.CONTROLLER_LAUNCHER
        or ".." in prepared.generation_path.parts
    ):
        _fail("prepared_invalid")


def _checked_info(path: Path, *, directory: bool, mode: int, code: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError:
        _fail(code)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != mode
        or (not directory and info.st_nlink != 1)
    ):
        _fail(code)
    return info


def _validate_regular_tree(root: Path, launcher: Path, *, code: str) -> None:
    _checked_info(root, directory=True, mode=0o700, code=code)
    found_launcher = False
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            _fail(code)
        for entry in entries:
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                _fail(code)
            if stat.S_ISDIR(info.st_mode):
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    _fail(code)
                pending.append(path)
                continue
            expected_mode = 0o700 if path == launcher else 0o600
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != expected_mode
                or info.st_nlink != 1
            ):
                _fail(code)
            if path == launcher:
                if info.st_size <= 0:
                    _fail(code)
                found_launcher = True
    if not found_launcher:
        _fail(code)


def _digest(path: Path, *, code: str) -> bytes:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        _fail(code)
    return digest.digest()


def _write_key(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    try:
        fd = os.open(path, flags, 0o600)
        view = memoryview(value)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
    finally:
        if fd >= 0:
            os.close(fd)


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        directories.append(current)
        for name in files:
            fd = os.open(current / name, _READ_FLAGS)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        for name in names:
            if (current / name).is_symlink():
                _fail("install_failed")
    for directory in reversed(directories):
        fd = os.open(directory, _DIR_FLAGS)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _cleanup_temp(path: Path | None) -> None:
    if path is None or not os.path.lexists(path):
        return
    try:
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
            shutil.rmtree(path)
    except OSError:
        pass


def install_native_controller(
    layout: upgrade_layout.UpgradeLayout,
    prepared: generation_prepare.PreparedGeneration,
    release_public_key: bytes,
) -> Path:
    _validate_layout(layout)
    _validate_prepared(layout, prepared)
    if type(release_public_key) is not bytes or len(release_public_key) != 32:
        _fail("public_key_invalid")
    if os.path.lexists(layout.controller_root):
        _fail("controller_exists")

    source_bin = prepared.generation_path / "bin"
    _checked_info(prepared.generation_path, directory=True, mode=0o700, code="generation_invalid")
    _validate_regular_tree(source_bin, prepared.launcher_path, code="generation_invalid")
    source_launcher_digest = _digest(prepared.launcher_path, code="generation_invalid")
    parent = layout.controller_root.parent
    try:
        parent_info = parent.lstat()
    except OSError:
        _fail("layout_invalid")
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid():
        _fail("layout_invalid")

    temporary: Path | None = None
    try:
        temporary = Path(tempfile.mkdtemp(
            prefix=f".{layout.controller_root.name}.tmp-", dir=parent,
        ))
        shutil.copytree(source_bin, temporary / "bin", symlinks=True)
        _write_key(temporary / upgrade_layout.PUBLIC_KEY_NAME, release_public_key)

        target_launcher = temporary / upgrade_layout.CONTROLLER_LAUNCHER
        _checked_info(
            temporary, directory=True, mode=0o700, code="install_failed"
        )
        _validate_regular_tree(
            temporary / "bin", target_launcher, code="install_failed"
        )
        if (
            {path.name for path in temporary.iterdir()}
            != {"bin", upgrade_layout.PUBLIC_KEY_NAME}
            or _digest(target_launcher, code="install_failed")
            != source_launcher_digest
            or (temporary / upgrade_layout.PUBLIC_KEY_NAME).read_bytes()
            != release_public_key
        ):
            _fail("install_failed")
        _checked_info(
            temporary / upgrade_layout.PUBLIC_KEY_NAME,
            directory=False,
            mode=0o600,
            code="install_failed",
        )
        _fsync_tree(temporary)
        if os.path.lexists(layout.controller_root):
            _fail("controller_exists")
        os.rename(temporary, layout.controller_root)
        temporary = None
        parent_fd = os.open(parent, _DIR_FLAGS)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        upgrade_layout.validate_controller_launcher(layout)
        if upgrade_layout.load_release_public_key(layout) != release_public_key:
            _fail("install_failed")
        return layout.controller_root
    except NativeControllerInstallError:
        raise
    except (OSError, UnicodeError) as exc:
        raise NativeControllerInstallError("install_failed") from exc
    finally:
        _cleanup_temp(temporary)


__all__ = [
    "NativeControllerInstallError",
    "install_native_controller",
]
