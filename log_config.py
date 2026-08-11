"""Cockpit process logging: level gate + platform handlers (O3 R2).

Linux keeps stderr/journald only (no unbounded app files).
macOS writes a size-rotated private app log under fixed <install>/logs
and keeps separate launchd bootstrap paths for pre-Python startup failures.
"""
from __future__ import annotations

import logging
import os
import stat
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final

ALLOWED_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
DEFAULT_LEVEL: Final[str] = "INFO"
# macOS app log: size-capped, fixed retention (handler-owned only).
MAC_MAX_BYTES: Final[int] = 5 * 1024 * 1024
MAC_BACKUP_COUNT: Final[int] = 5
# launchd bootstrap stdio (pre-Python); rotated only by prepare_macos_log_dir.
LAUNCHD_MAX_BYTES: Final[int] = 1 * 1024 * 1024
LAUNCHD_BACKUP_COUNT: Final[int] = 3
APP_LOG_NAME: Final[str] = "agent-cockpit.log"
LAUNCHD_STDOUT_NAME: Final[str] = "launchd.stdout.log"
LAUNCHD_STDERR_NAME: Final[str] = "launchd.stderr.log"
LOG_DIR_NAME: Final[str] = "logs"


class LogConfigError(ValueError):
    """Invalid logging configuration (stable public message)."""


def resolve_level(name: str | None = None) -> tuple[str, int]:
    """Return (LEVEL_NAME, logging level). Invalid values fail closed."""
    raw = DEFAULT_LEVEL if name is None else str(name).strip()
    if not raw:
        raw = DEFAULT_LEVEL
    key = raw.upper()
    if key not in ALLOWED_LEVELS:
        allowed = ", ".join(sorted(ALLOWED_LEVELS))
        raise LogConfigError(
            f"COCKPIT_LOG_LEVEL 非法: {name!r}；允许: {allowed}（默认 {DEFAULT_LEVEL}）"
        )
    return key, getattr(logging, key)


def default_log_dir(install_dir: str | Path) -> Path:
    """Fixed layout: <install_dir>/logs (no env override)."""
    root = Path(install_dir)
    if not root.is_absolute():
        raise LogConfigError("install_dir 必须是绝对路径")
    return root / LOG_DIR_NAME


def _require_owner_mode(
    path: Path, info: os.stat_result, *, expect_dir: bool, mode: int
) -> None:
    if expect_dir:
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise LogConfigError(f"路径不是普通目录: {path}")
    else:
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise LogConfigError(f"路径不是普通文件: {path}")
    if info.st_uid != os.getuid():
        raise LogConfigError(f"路径 owner 不正确: {path}")
    if stat.S_IMODE(info.st_mode) != mode:
        raise LogConfigError(f"路径 mode 必须为 {oct(mode)}: {path}")


def _reject_symlink_components(path: Path) -> None:
    """Reject any symlink in the absolute path chain (including leaf)."""
    path = Path(path)
    if not path.is_absolute():
        raise LogConfigError(f"路径必须是绝对路径: {path}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                raise LogConfigError(f"日志路径含符号链接: {current}")
        except OSError as exc:
            raise LogConfigError(f"无法检查日志路径: {current}") from exc


def ensure_private_log_dir(path: Path) -> Path:
    """Create/validate log directory as 0700 current-uid; reject symlink chain."""
    path = Path(path)
    if not path.is_absolute():
        raise LogConfigError(f"日志目录必须是绝对路径: {path}")
    _reject_symlink_components(path if path.exists() or path.is_symlink() else path.parent)
    # Parent chain must exist and be free of symlinks; only leaf may be created.
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        # re-check each parent component
        _reject_symlink_components(parent)
        if not parent.is_dir():
            raise LogConfigError(f"日志父目录不存在或不可用: {parent}")
    if path.is_symlink():
        raise LogConfigError(f"日志目录不得为符号链接: {path}")
    if path.exists():
        try:
            info = path.lstat()
        except OSError as exc:
            raise LogConfigError(f"无法访问日志目录: {path}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise LogConfigError(f"日志路径不是普通目录: {path}")
        if info.st_uid != os.getuid():
            raise LogConfigError(f"日志目录 owner 不正确: {path}")
        if stat.S_IMODE(info.st_mode) != 0o700:
            try:
                os.chmod(path, 0o700)
            except OSError as exc:
                raise LogConfigError(f"无法收紧日志目录权限: {path}") from exc
            try:
                after = path.lstat()
            except OSError as exc:
                raise LogConfigError(f"无法复验日志目录权限: {path}") from exc
            if stat.S_IMODE(after.st_mode) != 0o700 or after.st_uid != os.getuid():
                raise LogConfigError(f"日志目录权限复验失败: {path}")
    else:
        try:
            os.mkdir(path, 0o700)
        except OSError as exc:
            raise LogConfigError(f"无法创建日志目录: {path}") from exc
        try:
            os.chmod(path, 0o700)
            info = path.lstat()
        except OSError as exc:
            raise LogConfigError(f"无法收紧新建日志目录权限: {path}") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise LogConfigError(f"新建日志目录不安全: {path}")
    _reject_symlink_components(path)
    return path


def _chmod_file_strict(path: Path, mode: int = 0o600) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise LogConfigError(f"无法访问日志文件: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LogConfigError(f"日志文件不是普通文件: {path}")
    if info.st_uid != os.getuid():
        raise LogConfigError(f"日志文件 owner 不正确: {path}")
    if info.st_nlink != 1:
        raise LogConfigError(f"日志文件不得为硬链接: {path}")
    if stat.S_IMODE(info.st_mode) != mode:
        try:
            os.chmod(path, mode)
        except OSError as exc:
            raise LogConfigError(f"无法收紧日志文件权限: {path}") from exc
        try:
            after = path.lstat()
        except OSError as exc:
            raise LogConfigError(f"无法复验日志文件权限: {path}") from exc
        if (
            stat.S_IMODE(after.st_mode) != mode
            or after.st_uid != os.getuid()
            or after.st_nlink != 1
        ):
            raise LogConfigError(f"日志文件权限复验失败: {path}")


def _prepare_app_log_file(path: Path) -> None:
    """Ensure app log is a regular 0600 nlink=1 file owned by current uid.

    fstat regular/uid/nlink checks run **before** fchmod so a hardlinked
    existing file is rejected without changing the shared inode mode.
    """
    path = Path(path)
    _reject_symlink_components(path if path.exists() or path.is_symlink() else path.parent)
    if path.is_symlink():
        raise LogConfigError(f"应用日志不得为符号链接: {path}")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_APPEND
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = -1
    try:
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        # Reject hardlink / non-regular / wrong owner before any mode change.
        if not stat.S_ISREG(info.st_mode):
            raise LogConfigError(f"应用日志文件不安全: {path}")
        if info.st_uid != os.getuid():
            raise LogConfigError(f"应用日志文件不安全: {path}")
        if info.st_nlink != 1:
            raise LogConfigError(f"应用日志不得为硬链接: {path}")
        os.fchmod(fd, 0o600)
        after = os.fstat(fd)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_uid != os.getuid()
            or stat.S_IMODE(after.st_mode) != 0o600
            or after.st_nlink != 1
        ):
            raise LogConfigError(f"应用日志文件不安全: {path}")
    except LogConfigError:
        raise
    except OSError as exc:
        raise LogConfigError(f"无法安全打开应用日志: {path}") from exc
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    _chmod_file_strict(path, 0o600)


def _precheck_log_slot(path: Path, *, role: str) -> None:
    """If path exists, require current-uid regular file with nlink==1.

    Symlink (including broken), directory, FIFO, socket, hardlink, or wrong
    owner → error. Must run before any rename/unlink so base/slots stay
    unchanged on failure. Mode may be tightened only after precheck passes.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LogConfigError(f"无法检查 {role}: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise LogConfigError(f"{role} 不得为符号链接: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise LogConfigError(f"{role} 必须是普通文件: {path}")
    if info.st_uid != os.getuid():
        raise LogConfigError(f"{role} owner 不正确: {path}")
    if info.st_nlink != 1:
        raise LogConfigError(f"{role} 不得为硬链接: {path}")


def _precheck_app_backup_slots(
    base_path: Path, *, backup_count: int = MAC_BACKUP_COUNT
) -> None:
    """Full precheck of existing app log backup slots before stdlib mutation."""
    base_path = Path(base_path)
    for index in range(1, backup_count + 1):
        _precheck_log_slot(
            Path(f"{base_path}.{index}"), role=f"应用日志轮转副本 .{index}"
        )


def _secure_retained_log_files(
    base_path: Path, *, backup_count: int
) -> None:
    """Force exact 0600 + nlink1 on base and every present backup slot."""
    base_path = Path(base_path)
    _chmod_file_strict(base_path, 0o600)
    for index in range(1, backup_count + 1):
        slot = Path(f"{base_path}.{index}")
        try:
            slot.lstat()
        except FileNotFoundError:
            continue
        _chmod_file_strict(slot, 0o600)


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that prechecks/secures all app base + backup slots."""

    def doRollover(self) -> None:  # noqa: N802 — logging API
        base = Path(self.baseFilename)
        # Fail closed before stdlib delete/rename of any .1..N slot.
        _precheck_app_backup_slots(base, backup_count=self.backupCount)
        _precheck_log_slot(base, role="应用日志")
        super().doRollover()
        self._secure_after_rollover()

    def _secure_after_rollover(self) -> None:
        path = Path(self.baseFilename)
        stream = self.stream
        if stream is not None:
            try:
                os.fchmod(stream.fileno(), 0o600)
            except OSError as exc:
                raise LogConfigError(
                    f"rollover 后无法 fchmod 应用日志: {path}"
                ) from exc
        try:
            _secure_retained_log_files(path, backup_count=self.backupCount)
        except LogConfigError:
            raise
        try:
            info = path.lstat()
        except OSError as exc:
            raise LogConfigError(f"rollover 后无法复验应用日志: {path}") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise LogConfigError(f"rollover 后应用日志不安全: {path}")


def _precheck_rotate_slot(path: Path, *, role: str) -> None:
    """Bootstrap slot precheck (same rules as app slots)."""
    _precheck_log_slot(path, role=f"bootstrap {role}")


def rotate_file_if_needed(
    path: Path,
    *,
    max_bytes: int = LAUNCHD_MAX_BYTES,
    backup_count: int = LAUNCHD_BACKUP_COUNT,
) -> None:
    """Simple size rotation for launchd bootstrap logs (pre-Python) only."""
    path = Path(path)
    if backup_count < 1 or max_bytes < 1:
        raise LogConfigError("轮转参数无效")
    if path.is_symlink():
        raise LogConfigError(f"bootstrap 日志不得为符号链接: {path}")
    # Preflight every backup slot before any mutation (zero change on fail).
    for index in range(1, backup_count + 1):
        _precheck_rotate_slot(Path(f"{path}.{index}"), role=f"轮转副本 .{index}")
    # Missing base is OK; existing non-regular base must fail closed.
    try:
        path.lstat()
    except FileNotFoundError:
        return
    _precheck_rotate_slot(path, role="当前日志")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise LogConfigError(f"无法读取 bootstrap 日志大小: {path}") from exc
    if size < max_bytes:
        # Even without rollover, tighten base + retained backups to exact 0600.
        _chmod_file_strict(path, 0o600)
        for index in range(1, backup_count + 1):
            slot = Path(f"{path}.{index}")
            try:
                slot.lstat()
            except FileNotFoundError:
                continue
            _chmod_file_strict(slot, 0o600)
        return
    try:
        oldest = Path(f"{path}.{backup_count}")
        try:
            oldest.lstat()
        except FileNotFoundError:
            pass
        else:
            oldest.unlink()
        for index in range(backup_count - 1, 0, -1):
            src = Path(f"{path}.{index}")
            dst = Path(f"{path}.{index + 1}")
            try:
                src.lstat()
            except FileNotFoundError:
                continue
            src.replace(dst)
            _chmod_file_strict(dst, 0o600)
        path.replace(Path(f"{path}.1"))
        _chmod_file_strict(Path(f"{path}.1"), 0o600)
        path.write_bytes(b"")
        _chmod_file_strict(path, 0o600)
    except LogConfigError:
        raise
    except OSError as exc:
        raise LogConfigError(f"bootstrap 日志轮转失败: {path}") from exc


def prepare_macos_log_dir(install_dir: str | Path) -> Path:
    """Ensure fixed <install>/logs and rotate launchd bootstrap files only."""
    log_dir = ensure_private_log_dir(default_log_dir(install_dir))
    for name in (LAUNCHD_STDOUT_NAME, LAUNCHD_STDERR_NAME):
        rotate_file_if_needed(log_dir / name)
    return log_dir


def _build_formatter() -> logging.Formatter:
    # No request body/query/token fields — callers must not pass them.
    return logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _close_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
        try:
            logger.removeHandler(handler)
        except Exception:
            pass


def configure_logging(
    *,
    level_name: str | None = None,
    platform: str | None = None,
    install_dir: str | Path | None = None,
) -> str:
    """Configure root logging once. Returns resolved level name.

    - Linux/other: stderr only (journald captures unit stderr).
    - macOS: rotating file under fixed <install>/logs (requires install_dir).
    - uvicorn.access is disabled (no full_path/query in app logs).
    """
    name, level = resolve_level(
        level_name if level_name is not None else os.environ.get("COCKPIT_LOG_LEVEL")
    )
    plat = platform if platform is not None else sys.platform
    root = logging.getLogger()
    _close_handlers(root)
    root.setLevel(level)
    formatter = _build_formatter()

    if plat == "darwin":
        if install_dir is None:
            raise LogConfigError("macOS 日志需要绝对 install_dir")
        directory = ensure_private_log_dir(default_log_dir(install_dir))
        app_path = directory / APP_LOG_NAME
        try:
            _prepare_app_log_file(app_path)
            # Existing app backups must be safe before handler can ever rollover.
            _precheck_app_backup_slots(app_path, backup_count=MAC_BACKUP_COUNT)
            handler = _PrivateRotatingFileHandler(
                app_path,
                maxBytes=MAC_MAX_BYTES,
                backupCount=MAC_BACKUP_COUNT,
                encoding="utf-8",
                delay=False,
            )
        except LogConfigError:
            raise
        except OSError as exc:
            raise LogConfigError(f"无法创建轮转日志 handler: {app_path}") from exc
        try:
            if app_path.is_symlink() or not app_path.is_file():
                raise LogConfigError(f"应用日志在 handler 打开后变得不安全: {app_path}")
            _secure_retained_log_files(app_path, backup_count=MAC_BACKUP_COUNT)
        except LogConfigError:
            try:
                handler.close()
            except Exception:
                pass
            raise
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setLevel(level)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # App loggers: single sink via root.
    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "agent-cockpit",
        "agent-cockpit.tasks",
        "agent-cockpit.web-push",
        "agent-cockpit.upgrade",
    ):
        lg = logging.getLogger(logger_name)
        _close_handlers(lg)
        lg.setLevel(level)
        lg.propagate = True

    # F1: never emit access lines (full_path includes query).
    access = logging.getLogger("uvicorn.access")
    _close_handlers(access)
    access.propagate = False
    access.disabled = True
    access.setLevel(logging.CRITICAL)

    return name


__all__ = [
    "ALLOWED_LEVELS",
    "APP_LOG_NAME",
    "DEFAULT_LEVEL",
    "LAUNCHD_BACKUP_COUNT",
    "LAUNCHD_MAX_BYTES",
    "LAUNCHD_STDERR_NAME",
    "LAUNCHD_STDOUT_NAME",
    "LOG_DIR_NAME",
    "LogConfigError",
    "MAC_BACKUP_COUNT",
    "MAC_MAX_BYTES",
    "configure_logging",
    "default_log_dir",
    "ensure_private_log_dir",
    "prepare_macos_log_dir",
    "resolve_level",
    "rotate_file_if_needed",
]
