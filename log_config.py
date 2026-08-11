"""Cockpit process logging: level gate + platform handlers (O3).

Linux keeps stderr/journald only (no unbounded app files).
macOS writes a size-rotated private app log and keeps separate launchd
bootstrap paths for pre-Python startup failures.
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
# macOS app log: size-capped, fixed retention (not unlimited).
MAC_MAX_BYTES: Final[int] = 5 * 1024 * 1024
MAC_BACKUP_COUNT: Final[int] = 5
# launchd bootstrap stdio (pre-Python); rotated by launchd.sh.
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


def default_log_dir(install_dir: str | Path | None = None) -> Path:
    """Prefer COCKPIT_LOG_DIR, else <install>/logs, else cwd/logs."""
    env = os.environ.get("COCKPIT_LOG_DIR")
    if env:
        return Path(env).expanduser()
    if install_dir is not None:
        return Path(install_dir) / LOG_DIR_NAME
    return Path.cwd() / LOG_DIR_NAME


def ensure_private_log_dir(path: Path) -> Path:
    """Create log directory as 0700 owned layout; reject symlinks."""
    path = Path(path)
    if path.exists() and path.is_symlink():
        raise LogConfigError(f"日志目录不得为符号链接: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as exc:
        raise LogConfigError(f"无法访问日志目录: {path}") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise LogConfigError(f"日志路径不是普通目录: {path}")
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise LogConfigError(f"无法收紧日志目录权限: {path}") from exc
    return path


def _chmod_file(path: Path, mode: int = 0o600) -> None:
    try:
        if path.is_file() and not path.is_symlink():
            os.chmod(path, mode)
    except OSError:
        pass


def rotate_file_if_needed(
    path: Path,
    *,
    max_bytes: int = LAUNCHD_MAX_BYTES,
    backup_count: int = LAUNCHD_BACKUP_COUNT,
) -> None:
    """Simple size rotation for launchd bootstrap logs (pre-Python)."""
    path = Path(path)
    if backup_count < 1 or max_bytes < 1:
        raise LogConfigError("轮转参数无效")
    if not path.is_file() or path.is_symlink():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < max_bytes:
        _chmod_file(path)
        return
    try:
        oldest = Path(f"{path}.{backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(backup_count - 1, 0, -1):
            src = Path(f"{path}.{index}")
            dst = Path(f"{path}.{index + 1}")
            if src.is_file() and not src.is_symlink():
                src.replace(dst)
                _chmod_file(dst)
        path.replace(Path(f"{path}.1"))
        _chmod_file(Path(f"{path}.1"))
        path.write_bytes(b"")
        _chmod_file(path)
    except OSError:
        pass


def prepare_macos_log_dir(install_dir: str | Path) -> Path:
    """Ensure macOS log dir + rotate launchd bootstrap files if oversized."""
    log_dir = ensure_private_log_dir(default_log_dir(install_dir))
    for name in (LAUNCHD_STDOUT_NAME, LAUNCHD_STDERR_NAME, APP_LOG_NAME):
        rotate_file_if_needed(log_dir / name)
    return log_dir


def _build_formatter() -> logging.Formatter:
    # No request body/query/token fields — callers must not pass them.
    return logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def configure_logging(
    *,
    level_name: str | None = None,
    platform: str | None = None,
    log_dir: str | Path | None = None,
    install_dir: str | Path | None = None,
) -> str:
    """Configure root logging once. Returns resolved level name.

    - Linux/other: stderr only (journald captures unit stderr).
    - macOS: rotating file under private logs/ + no extra stream handler
      (launchd still has separate bootstrap stdio files).
    Uvicorn loggers propagate to root only (no duplicate handlers).
    """
    name, level = resolve_level(
        level_name if level_name is not None else os.environ.get("COCKPIT_LOG_LEVEL")
    )
    plat = platform if platform is not None else sys.platform
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    formatter = _build_formatter()

    if plat == "darwin":
        directory = ensure_private_log_dir(
            Path(log_dir) if log_dir is not None else default_log_dir(install_dir)
        )
        app_path = directory / APP_LOG_NAME
        handler: logging.Handler = RotatingFileHandler(
            app_path,
            maxBytes=MAC_MAX_BYTES,
            backupCount=MAC_BACKUP_COUNT,
            encoding="utf-8",
            delay=False,
        )
        _chmod_file(app_path)
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setLevel(level)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Single sink: clear third-party handlers and propagate to root.
    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "agent-cockpit",
        "agent-cockpit.tasks",
        "agent-cockpit.web-push",
        "agent-cockpit.upgrade",
    ):
        lg = logging.getLogger(logger_name)
        lg.handlers.clear()
        lg.setLevel(level)
        lg.propagate = True

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
