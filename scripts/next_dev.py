#!/usr/bin/env python3
"""Validate and start the isolated Cockpit Next development runtime."""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_cockpit.instance_lock import LOCK_FD_ENV, InstanceLock, LockError
from agent_cockpit import auth_token, next_profile


BASE_SHA = "169d0af7751b568e813d2cbca285a9f147e86001"
NEXT_SESSION = "github-agent-cockpit-next"
NEXT_UNIT = "agent-cockpit-next.service"
PRODUCTION_UNIT = "agent-cockpit.service"
PRODUCTION_PORT = "8790"
ALLOWED_HOSTS = next_profile.FIXED_HOSTS
TOKEN_FILE_NAME = auth_token.TOKEN_FILE_NAME
MAX_TOKEN_BYTES = auth_token.MAX_TOKEN_BYTES
TOKEN_RE = auth_token.TOKEN_RE
_token_file_signature = auth_token._token_file_signature
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


def error_hint(code: str, values: Mapping[str, str] | None = None) -> str:
    """One-line Chinese next action for common launcher codes. Never includes secrets."""
    token_path = (
        str(token_file_path(values))
        if values is not None and "COCKPIT_CONFIG_DIR" in values
        else str(Path.home() / ".config" / "agent-cockpit-next" / TOKEN_FILE_NAME)
    )
    hints = {
        "wrong_worktree": (
            "必须在 $HOME/github/agent-cockpit-next 运行；固定 profile 不接受其他 checkout 路径。"
        ),
        "lan_host_token_required": (
            f"先创建 {token_path}（0600，仅当前用户）。不要写入 .env.next。"
        ),
        "env_keys_mismatch": (
            "只允许 .env.next.example 那一组键，不要增加 COCKPIT_TOKEN。"
        ),
        "next_port_in_use": (
            "18790 已被占用；不要改端口，先停掉占用该口的 Next 进程后再 start。"
        ),
        "next_web_build_unavailable": (
            "先运行 npm run --prefix web build，确认 web/dist/index.html 存在。"
        ),
        "git_baseline_mismatch": (
            "当前分支必须是 next，且 HEAD 包含固定 baseline。"
        ),
        "value_mismatch:COCKPIT_HOST": (
            "COCKPIT_HOST 只能是 127.0.0.1 或 0.0.0.0。"
        ),
        "env_invalid": "检查 .env.next 是否有非法字符、重复键或 HOST 行带空格。",
        "production_port": "COCKPIT_PORT 不能是 8790；Next 固定为 18790。",
        "token_file_unsafe": (
            f"{token_path} 必须是当前用户、0600、非常规链接的普通文件。"
        ),
        "token_file_invalid": f"{token_path} 内容格式无效；不要把令牌写进日志。",
        "token_file_unavailable": f"无法读取 {token_path}。",
        "venv_missing": "先在仓库根创建 .venv 并 pip install -r requirements.txt。",
        "next_unit_installed": "不要安装 agent-cockpit-next.service；只用 next_dev.py start。",
    }
    return hints.get(code, "")


def success_guidance(values: Mapping[str, str]) -> list[str]:
    """User-facing next steps. Prints token path only, never token contents."""
    port = values["COCKPIT_PORT"]
    if values["COCKPIT_HOST"] == "127.0.0.1":
        open_line = f"打开 http://127.0.0.1:{port}"
    else:
        open_line = (
            f"绑定 0.0.0.0:{port}；用本机局域网地址访问 "
            f"http://<本机局域网IP>:{port}"
        )
    return [
        "OK",
        open_line,
        f"令牌文件（不要打印内容）：{token_file_path(values)}",
        "空首页下一步：选择代码目录",
    ]


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
        "COCKPIT_PROJECT_ROOT": str(root / "github"),
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
        if key == "COCKPIT_HOST" and raw != line:
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
    if next_profile.PROJECT_ROOT_ENV not in values:
        raise IsolationError(
            f"next_profile_missing:{next_profile.PROJECT_ROOT_ENV}"
        )
    if set(values) != set(expected_values):
        raise IsolationError("env_keys_mismatch")
    if values["COCKPIT_PORT"] == PRODUCTION_PORT:
        raise IsolationError("production_port")
    if values["COCKPIT_SYSTEMD_UNIT"] == PRODUCTION_UNIT:
        raise IsolationError("production_unit")
    if values["COCKPIT_HOST"] not in ALLOWED_HOSTS:
        raise IsolationError("value_mismatch:COCKPIT_HOST")

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
        if key in {next_profile.PROJECT_ROOT_ENV, "COCKPIT_HOST"}:
            continue
        if values.get(key) != wanted:
            raise IsolationError(f"value_mismatch:{key}")

    try:
        next_profile.configured_project_root(values, home=home_path)
    except next_profile.NextProfileError as exc:
        raise IsolationError(str(exc)) from None

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
    return auth_token.token_file_path(values)


def load_cockpit_token(values: Mapping[str, str]) -> str | None:
    try:
        return auth_token.load_cockpit_token(values)
    except auth_token.TokenFileError as exc:
        raise IsolationError(exc.code) from exc


def validate_host_token(values: Mapping[str, str], token: str | None) -> None:
    if values["COCKPIT_HOST"] == "0.0.0.0" and token is None:
        raise IsolationError("lan_host_token_required")


def _validate_web_build(repo: Path) -> None:
    dist = repo / "web" / "dist"
    index = dist / "index.html"
    assets = dist / "assets"
    try:
        ready = (
            index.is_file()
            and assets.is_dir()
            and any(path.is_file() for path in assets.iterdir())
        )
    except OSError:
        ready = False
    if not ready:
        raise IsolationError("next_web_build_unavailable")


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
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
    values: dict[str, str] | None = None
    try:
        values = validate(load_env(args.env_file), repo=repo)
        token = load_cockpit_token(values)
        validate_host_token(values, token)
        _validate_web_build(repo)
        if args.command == "start":
            if not _unit_not_installed():
                raise IsolationError("next_unit_installed")
            if not _port_available(values["COCKPIT_HOST"], int(values["COCKPIT_PORT"])):
                raise IsolationError("next_port_in_use")
            ensure_runtime_roots(values)
            try:
                next_profile.ensure_private_herdr_config(values)
            except next_profile.NextProfileError as exc:
                raise IsolationError(str(exc)) from exc
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
                    _emit_success(args.json, values)
                    os.execve(
                        str(python),
                        [str(python), str(repo / "server.py")],
                        environment,
                    )
                    return 0
            except LockError as exc:
                raise IsolationError(exc.code) from exc
    except IsolationError as exc:
        result = {"ok": False, "error": exc.code}
        if args.json:
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        else:
            print(exc.code, file=sys.stderr)
            hint = error_hint(exc.code, values)
            if hint:
                print(hint, file=sys.stderr)
        return 1
    _emit_success(args.json, values)
    return 0


def _emit_success(as_json: bool, values: Mapping[str, str]) -> None:
    if as_json:
        print(json.dumps(
            {"ok": True, "profile": "agent-cockpit-next"},
            sort_keys=True, separators=(",", ":"),
        ))
        return
    print("\n".join(success_guidance(values)), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
