#!/usr/bin/env python3
"""Start an isolated Cockpit Next server for real local journey tests."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_cockpit.instance_lock import LOCK_FD_ENV, InstanceLock, LockError
from agent_cockpit import next_profile


ROOT = Path(__file__).resolve().parents[1]
LISTEN_FD_ENV = next_profile.EPHEMERAL_LISTEN_FD_ENV
LAYOUT = ("data", "config", "state", "uploads", "mail", "release", "herdr", "home", "tmp")
RESERVED_PORTS = {8790, 18790}


class EphemeralError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _runtime_root(raw: str) -> Path:
    path = Path(raw)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.lstat()
    except OSError as exc:
        raise EphemeralError("runtime_root_invalid") from exc
    if (
        not path.is_absolute()
        or path != resolved
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise EphemeralError("runtime_root_invalid")
    return resolved


def _initialize_layout(root: Path) -> bool:
    try:
        fresh = next_profile.initialize_empty_ephemeral_runtime_root(root)
    except next_profile.NextProfileError as exc:
        raise EphemeralError("runtime_root_layout_invalid") from exc
    if fresh:
        for name in LAYOUT:
            (root / name).mkdir(mode=0o700)
    return fresh


def _prepare_layout(root: Path, *, fresh: bool) -> None:
    if not fresh:
        try:
            next_profile.prepare_ephemeral_runtime_root(root)
        except next_profile.NextProfileError as exc:
            raise EphemeralError("runtime_root_layout_invalid") from exc
    for name in LAYOUT:
        path = root / name
        try:
            info = path.lstat()
        except OSError as exc:
            raise EphemeralError("runtime_root_layout_invalid") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise EphemeralError("runtime_root_layout_invalid")


def _listen() -> socket.socket:
    for _ in range(16):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            if port not in RESERVED_PORTS:
                return listener
        except OSError as exc:
            listener.close()
            raise EphemeralError("ephemeral_bind_failed") from exc
        listener.close()
    raise EphemeralError("ephemeral_port_exhausted")


def _environment(root: Path, port: int, token: str) -> dict[str, str]:
    clean = {
        name: value
        for name, value in os.environ.items()
        if not (
            name.startswith(("COCKPIT_", "AGENT_COCKPIT_", "AGENT_MAIL_", "HERDR_", "XDG_"))
            or name in {"LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
        )
    }
    session = f"ephemeral-{token}"
    clean.update({
        "COCKPIT_NEXT_PROFILE": next_profile.EPHEMERAL_PROFILE,
        "COCKPIT_EPHEMERAL_ROOT": str(root),
        "COCKPIT_EPHEMERAL_READY_TOKEN": token,
        "COCKPIT_NEXT_WORKTREE": str(ROOT),
        "COCKPIT_HOST": "127.0.0.1",
        "COCKPIT_PORT": str(port),
        "COCKPIT_DATA_DIR": str(root / "data"),
        "COCKPIT_CONFIG_DIR": str(root / "config"),
        "COCKPIT_STATE_DIR": str(root / "state"),
        "COCKPIT_UPLOADS_DIR": str(root / "uploads"),
        "COCKPIT_COORDINATION_DB": str(root / "data/coordination.sqlite3"),
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH": str(root / "data/launch-descriptors.json"),
        "AGENT_COCKPIT_RELEASE_STATE_DIR": str(root / "release"),
        "AGENT_MAIL_DB_PATH": str(root / "mail/storage.sqlite3"),
        "AGENT_MAIL_PROJECT": str(ROOT),
        "HERDR_SESSION": session,
        "COCKPIT_SYSTEMD_UNIT": "agent-cockpit-next-ephemeral.service",
        "COCKPIT_UPGRADE_V2_ENABLED": "0",
        "COCKPIT_B0_MODE": "off",
        "COCKPIT_HERDR_STATE_MODE": "off",
        "COCKPIT_EDITION": "source",
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_STATE_HOME": str(root / "state"),
        "HERDR_CONFIG_PATH": str(root / "herdr/config.toml"),
        "TEAM_HUB_URL": "http://127.0.0.1:9",
        "HUMAN_AUTH_URL": "http://127.0.0.1:9",
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "PYTHONUNBUFFERED": "1",
    })
    return clean


def _prepare_exec_fds(*fds: int) -> None:
    keep = set(fds)
    if any(fd <= 2 for fd in keep):
        raise EphemeralError("ephemeral_fd_invalid")
    try:
        entries = os.listdir("/proc/self/fd")
    except OSError as exc:
        raise EphemeralError("ephemeral_fd_inventory_failed") from exc
    for entry in entries:
        if not entry.isdecimal():
            continue
        fd = int(entry)
        if fd <= 2 or fd in keep:
            continue
        try:
            os.set_inheritable(fd, False)
        except OSError:
            try:
                os.fstat(fd)
            except OSError:
                continue
            raise EphemeralError("ephemeral_fd_sanitize_failed") from None
    for fd in keep:
        try:
            os.set_inheritable(fd, True)
        except OSError as exc:
            raise EphemeralError("ephemeral_fd_handoff_failed") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True)
    args = parser.parse_args(argv)
    listener: socket.socket | None = None
    lock: InstanceLock | None = None
    try:
        os.setsid()
        root = _runtime_root(args.runtime_root)
        fresh = _initialize_layout(root)
        listener = _listen()
        port = listener.getsockname()[1]
        token = secrets.token_hex(16)
        environment = _environment(root, port, token)
        lock = InstanceLock(environment).acquire()
        _prepare_layout(root, fresh=fresh)
        try:
            next_profile.activate_ephemeral_runtime_root(root)
        except next_profile.NextProfileError as exc:
            raise EphemeralError("runtime_root_layout_invalid") from exc
        try:
            next_profile.ensure_private_herdr_config(environment)
        except next_profile.NextProfileError as exc:
            raise EphemeralError(str(exc)) from exc
        if lock.fd is None:
            raise EphemeralError("ephemeral_lock_missing")
        _prepare_exec_fds(lock.fd, listener.fileno())
        environment[LOCK_FD_ENV] = str(lock.fd)
        environment[LISTEN_FD_ENV] = str(listener.fileno())
        print(json.dumps({
            "schema_version": 1,
            "state": "starting",
            "base_url": f"http://127.0.0.1:{port}",
            "pid": os.getpid(),
            "ready_path": "/health/ephemeral",
            "ready_token": token,
        }, separators=(",", ":")), flush=True)
        python = Path(sys.executable)
        if not python.is_file():
            raise EphemeralError("ephemeral_python_missing")
        os.chdir(ROOT)
        os.execve(str(python), [str(python), str(ROOT / "server.py")], environment)
    except (EphemeralError, LockError) as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except OSError:
        print("ephemeral_start_failed", file=sys.stderr)
        return 2
    finally:
        if lock is not None:
            lock.release()
        if listener is not None:
            listener.close()


if __name__ == "__main__":
    raise SystemExit(main())
