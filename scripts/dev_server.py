#!/usr/bin/env python3
"""Start this checkout as a real-HOME source server on :8790.

Not the isolated next (18790) or ephemeral sandbox profiles.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_cockpit import next_profile
from agent_cockpit.instance_lock import LOCK_FD_ENV, InstanceLock, LockError


_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def _source_sha(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip().lower() if result.returncode == 0 else ""
    return value if _SHA40_RE.fullmatch(value) else None


def _load_next_dev():
    spec = importlib.util.spec_from_file_location(
        "agent_cockpit_next_dev", ROOT / "scripts" / "next_dev.py",
    )
    if spec is None or spec.loader is None:
        raise SystemExit("dev_launcher_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dev_values(repo: Path, home: Path, host: str = "127.0.0.1") -> dict[str, str]:
    if host not in next_profile.FIXED_HOSTS:
        raise next_profile.NextProfileError("next_profile_invalid:COCKPIT_HOST")
    override = os.environ.get(next_profile.PROJECT_ROOT_ENV, "").strip()
    project_root = (
        Path(override) if override else next_profile.default_dev_project_root(repo, home)
    )
    values = {
        "COCKPIT_NEXT_PROFILE": next_profile.DEV_PROFILE,
        "COCKPIT_PROJECT_ROOT": str(project_root),
        "COCKPIT_HOST": host,
    }
    values.update(next_profile.dev_layout(home, repo))
    source_sha = _source_sha(repo)
    if source_sha is not None:
        values["COCKPIT_SOURCE_SHA"] = source_sha
    return values


def main() -> int:
    next_dev = _load_next_dev()
    repo = ROOT
    home = Path.home().resolve()
    host = os.environ.get("COCKPIT_HOST", "127.0.0.1")
    values = dev_values(repo, home, host)
    python = repo / ".venv" / "bin" / "python"
    if not python.is_file():
        print("venv_missing", file=sys.stderr)
        return 1
    try:
        token = next_dev.load_cockpit_token(values)
        if token is not None:
            values["COCKPIT_TOKEN"] = token
        if values["COCKPIT_HOST"] == "0.0.0.0" and token is None:
            print("lan_host_token_required", file=sys.stderr)
            return 1
        next_profile.validate_server_environment(repo, values)
        next_dev._validate_web_build(repo)
        if not next_dev._port_available(values["COCKPIT_HOST"], int(values["COCKPIT_PORT"])):
            print("dev_port_in_use", file=sys.stderr)
            return 1
        next_dev.ensure_runtime_roots(values)
        environment = next_dev.sanitized_environment(values)
        if token is not None:
            environment["COCKPIT_TOKEN"] = token
        environment["VIRTUAL_ENV"] = str(repo / ".venv")
        environment.pop("HOME", None)
        environment["HOME"] = str(home)
        with InstanceLock(values) as lock:
            next_dev._prepare_exec_fds(lock.fd)
            environment[LOCK_FD_ENV] = str(lock.fd)
            os.chdir(repo)
            if values["COCKPIT_HOST"] == "0.0.0.0":
                print(f"OK http://0.0.0.0:{values['COCKPIT_PORT']}", flush=True)
            else:
                print(f"OK http://127.0.0.1:{values['COCKPIT_PORT']}", flush=True)
            os.execve(str(python), [str(python), str(repo / "server.py")], environment)
    except next_profile.NextProfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (LockError, next_dev.IsolationError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
