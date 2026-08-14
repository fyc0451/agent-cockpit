#!/usr/bin/env python3
"""Validate and start the isolated Cockpit Next development runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_cockpit.instance_lock import LOCK_FD_ENV, InstanceLock, LockError


BASE_SHA = "169d0af7751b568e813d2cbca285a9f147e86001"
NEXT_SESSION = "github-agent-cockpit-next"
NEXT_UNIT = "agent-cockpit-next.service"
PRODUCTION_UNIT = "agent-cockpit.service"
PRODUCTION_PORT = "8790"
TOKEN_FILE_NAME = "cockpit.token"
MAX_TOKEN_BYTES = 256
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
SANITIZED_PREFIXES = ("COCKPIT_", "AGENT_COCKPIT_", "HERDR_")
SANITIZED_KEYS = frozenset({
    "AGENT_MAIL_DB_PATH", "AGENT_MAIL_PROJECT", "HERDR_SESSION",
    "HUMAN_AUTH_URL", "TEAM_HUB_URL", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "XDG_STATE_HOME", "HERDR_CONFIG_PATH", LOCK_FD_ENV,
})
PYTHON_ENV_KEYS = frozenset({
    "LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV",
})


class IsolationError(RuntimeError):
    """Fail-closed N0 configuration error with a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def expected(home: Path | None = None) -> dict[str, str]:
    root = (Path.home() if home is None else home).resolve()
    worktree = root / "github" / "agent-cockpit-next"
    data = root / ".local" / "share" / "agent-cockpit-next-data"
    config = root / ".config" / "agent-cockpit-next"
    state = root / ".local" / "state" / "agent-cockpit-next"
    uploads = root / ".local" / "share" / "agent-cockpit-next-uploads"
    return {
        "COCKPIT_NEXT_PROFILE": "1",
        "COCKPIT_NEXT_WORKTREE": str(worktree),
        "COCKPIT_HOST": "127.0.0.1",
        "COCKPIT_PORT": "18790",
        "COCKPIT_DATA_DIR": str(data),
        "COCKPIT_CONFIG_DIR": str(config),
        "COCKPIT_STATE_DIR": str(state),
        "COCKPIT_UPLOADS_DIR": str(uploads),
        "COCKPIT_COORDINATION_DB": str(data / "coordination.sqlite3"),
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH": str(data / "launch-descriptors.json"),
        "AGENT_COCKPIT_RELEASE_STATE_DIR": str(state / "release-lane"),
        "AGENT_MAIL_PROJECT": str(worktree),
        "HERDR_SESSION": NEXT_SESSION,
        "COCKPIT_SYSTEMD_UNIT": NEXT_UNIT,
        "COCKPIT_UPGRADE_V2_ENABLED": "0",
        "COCKPIT_B0_MODE": "off",
        "COCKPIT_HERDR_STATE_MODE": "off",
        "COCKPIT_EDITION": "source",
        "XDG_DATA_HOME": str(data),
        "XDG_CONFIG_HOME": str(config),
        "XDG_STATE_HOME": str(state),
        "HERDR_CONFIG_PATH": str(config / "herdr" / "config.toml"),
        "AGENT_MAIL_DB_PATH": str(root / "mcp_agent_mail" / "storage.sqlite3"),
        "TEAM_HUB_URL": "http://127.0.0.1:8765",
        "HUMAN_AUTH_URL": "http://127.0.0.1:8766",
    }


def load_env(path: Path, *, home: Path | None = None) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise IsolationError("env_unreadable") from exc
    home_text = str((Path.home() if home is None else home).resolve())
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (
            not separator
            or not key
            or not key.replace("_", "").isalnum()
            or not key[0].isalpha()
            or key in values
        ):
            raise IsolationError("env_invalid")
        value = value.replace("${HOME}", home_text)
        if "$" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise IsolationError("env_invalid")
        values[key] = value
    return values


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _canonical(value: str, code: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise IsolationError(code)
    resolved = path.resolve(strict=False)
    if resolved != path:
        raise IsolationError(code)
    return resolved


def _protected_roots(home: Path) -> tuple[Path, ...]:
    return tuple(
        path.resolve(strict=False)
        for path in (
            home / "dashboard-data",
            home / "dashboard-uploads",
            home / ".config" / "agent-cockpit",
            home / ".local" / "state" / "agent-cockpit",
            home / ".local" / "share" / "agent-cockpit-server",
            home / ".local" / "share" / "agent-cockpit-controller",
        )
    )


def validate(
    values: Mapping[str, str], *, repo: Path, home: Path | None = None,
    check_git: bool = True,
) -> dict[str, str]:
    expected_values = expected(home)
    if set(values) != set(expected_values):
        raise IsolationError("env_keys_mismatch")
    if values["COCKPIT_PORT"] == PRODUCTION_PORT:
        raise IsolationError("production_port")
    if values["COCKPIT_SYSTEMD_UNIT"] == PRODUCTION_UNIT:
        raise IsolationError("production_unit")

    home_path = (Path.home() if home is None else home).resolve()
    roots = [
        _canonical(values[key], f"unsafe_path:{key}")
        for key in (
            "COCKPIT_DATA_DIR",
            "COCKPIT_CONFIG_DIR",
            "COCKPIT_STATE_DIR",
            "COCKPIT_UPLOADS_DIR",
        )
    ]
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if left == right or _inside(left, right) or _inside(right, left):
                raise IsolationError("runtime_roots_overlap")
    for root in roots:
        for protected in _protected_roots(home_path):
            if root == protected or _inside(root, protected) or _inside(protected, root):
                raise IsolationError("production_path")
    for key, wanted in expected_values.items():
        if values.get(key) != wanted:
            raise IsolationError(f"value_mismatch:{key}")

    selected_repo = repo.resolve()
    if selected_repo != Path(values["COCKPIT_NEXT_WORKTREE"]):
        raise IsolationError("wrong_worktree")
    try:
        marker = (selected_repo / ".agent-memory-project").read_text(
            encoding="ascii"
        )
    except (OSError, UnicodeError) as exc:
        raise IsolationError("memory_marker_unreadable") from exc
    if marker != "agent-cockpit-next\n":
        raise IsolationError("memory_project_mismatch")
    if check_git:
        try:
            branch = subprocess.run(
                ["git", "-C", str(selected_repo), "branch", "--show-current"],
                check=False, capture_output=True, text=True, timeout=5,
            )
            ancestor = subprocess.run(
                ["git", "-C", str(selected_repo), "merge-base", "--is-ancestor", BASE_SHA, "HEAD"],
                check=False, capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IsolationError("git_unavailable") from exc
        if branch.returncode or branch.stdout.strip() != "next" or ancestor.returncode:
            raise IsolationError("git_baseline_mismatch")
        try:
            delivery = json.loads(
                (selected_repo / ".delivery" / "cockpit-next.json").read_text(
                    encoding="ascii"
                )
            )
            delivery_sha = delivery["baseline"]["main_sha"]
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise IsolationError("delivery_baseline_unreadable") from exc
        if delivery_sha != BASE_SHA:
            raise IsolationError("delivery_baseline_mismatch")
    return dict(values)


def _reject_symlink_chain(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            raise IsolationError("runtime_symlink")


def ensure_runtime_roots(values: Mapping[str, str]) -> None:
    for key in (
        "COCKPIT_DATA_DIR",
        "COCKPIT_CONFIG_DIR",
        "COCKPIT_STATE_DIR",
        "COCKPIT_UPLOADS_DIR",
    ):
        path = Path(values[key])
        _reject_symlink_chain(path)
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise IsolationError("runtime_root_unsafe")


def token_file_path(values: Mapping[str, str]) -> Path:
    return Path(values["COCKPIT_CONFIG_DIR"]) / TOKEN_FILE_NAME


def _token_file_signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def load_cockpit_token(values: Mapping[str, str]) -> str | None:
    path = token_file_path(values)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IsolationError("token_file_unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
        or before.st_size < 32
        or before.st_size > MAX_TOKEN_BYTES + 1
    ):
        raise IsolationError("token_file_unsafe")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise IsolationError("token_file_unavailable") from exc
    try:
        opened = os.fstat(fd)
        if _token_file_signature(opened) != _token_file_signature(before):
            raise IsolationError("token_file_unsafe")
        chunks: list[bytes] = []
        remaining = MAX_TOKEN_BYTES + 2
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise IsolationError("token_file_unsafe")
        after = os.fstat(fd)
        if _token_file_signature(after) != _token_file_signature(opened):
            raise IsolationError("token_file_unsafe")
        try:
            current = path.lstat()
        except OSError as exc:
            raise IsolationError("token_file_unsafe") from exc
        if _token_file_signature(current) != _token_file_signature(after):
            raise IsolationError("token_file_unsafe")
    except OSError as exc:
        raise IsolationError("token_file_unavailable") from exc
    finally:
        os.close(fd)

    raw = b"".join(chunks)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        token = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise IsolationError("token_file_invalid") from exc
    if TOKEN_RE.fullmatch(token) is None:
        raise IsolationError("token_file_invalid")
    return token


def sanitized_environment(values: Mapping[str, str]) -> dict[str, str]:
    clean = {
        key: value
        for key, value in os.environ.items()
        if key not in SANITIZED_KEYS | PYTHON_ENV_KEYS
        and not any(key.startswith(prefix) for prefix in SANITIZED_PREFIXES)
    }
    clean.update(values)
    clean["PYTHONUNBUFFERED"] = "1"
    return clean


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _unit_not_installed() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", NEXT_UNIT, "--property=LoadState", "--value"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "not-found"


def _prepare_exec_fds(lock_fd: int | None) -> None:
    if lock_fd is None:
        raise IsolationError("lock_fd_missing")
    try:
        entries = os.listdir("/proc/self/fd")
    except OSError as exc:
        raise IsolationError("fd_inventory_unavailable") from exc
    for entry in entries:
        if not entry.isdecimal():
            continue
        fd = int(entry)
        if fd <= 2 or fd == lock_fd:
            continue
        try:
            os.set_inheritable(fd, False)
        except OSError as exc:
            try:
                os.fstat(fd)
            except OSError:
                continue
            raise IsolationError("fd_sanitize_failed") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "start"))
    parser.add_argument("--env-file", type=Path, default=Path(".env.next"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    try:
        values = validate(load_env(args.env_file), repo=repo)
        token = load_cockpit_token(values)
        if args.command == "start":
            if not _unit_not_installed():
                raise IsolationError("next_unit_installed")
            if not _port_available(values["COCKPIT_HOST"], int(values["COCKPIT_PORT"])):
                raise IsolationError("next_port_in_use")
            ensure_runtime_roots(values)
            python = repo / ".venv" / "bin" / "python"
            if not python.is_file():
                raise IsolationError("venv_missing")
            environment = sanitized_environment(values)
            if token is not None:
                environment["COCKPIT_TOKEN"] = token
            environment["VIRTUAL_ENV"] = str(repo / ".venv")
            try:
                with InstanceLock(values) as lock:
                    _prepare_exec_fds(lock.fd)
                    environment[LOCK_FD_ENV] = str(lock.fd)
                    os.chdir(repo)
                    os.execve(
                        str(python),
                        [str(python), str(repo / "server.py")],
                        environment,
                    )
            except LockError as exc:
                raise IsolationError(exc.code) from exc
    except IsolationError as exc:
        result = {"ok": False, "error": exc.code}
        if args.json:
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        else:
            print(exc.code, file=sys.stderr)
        return 1
    result = {"ok": True, "profile": "agent-cockpit-next"}
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
