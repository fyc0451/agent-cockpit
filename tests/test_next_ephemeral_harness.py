from __future__ import annotations

import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen

import pytest

from agent_cockpit import next_profile


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "next_ephemeral_server.py"
RESERVED_PORTS = {8790, 18790}
SOURCE_SHA = "a" * 40


def _spawn(runtime_root: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            str(LAUNCHER),
            "--runtime-root",
            str(runtime_root),
            "--source-sha",
            SOURCE_SHA,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _owned_process_group(process: subprocess.Popen[str]) -> int | None:
    try:
        process_group = os.getpgid(process.pid)
    except ProcessLookupError:
        return None
    assert process_group == process.pid
    return process_group


def _port(descriptor: dict[str, object]) -> int:
    base_url = descriptor["base_url"]
    assert isinstance(base_url, str)
    port = int(base_url.rsplit(":", 1)[1])
    assert port not in RESERVED_PORTS
    return port


def _ready(descriptor: dict[str, object]) -> dict[str, object]:
    base_url = descriptor["base_url"]
    ready_path = descriptor["ready_path"]
    assert isinstance(base_url, str)
    assert isinstance(ready_path, str)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urlopen(base_url + ready_path, timeout=1) as response:
                return json.loads(response.read())
        except OSError:
            time.sleep(0.05)
    raise AssertionError("ephemeral server did not become ready")


def _live(descriptor: dict[str, object]) -> dict[str, object]:
    base_url = descriptor["base_url"]
    assert isinstance(base_url, str)
    with urlopen(base_url + "/health/live", timeout=5) as response:
        return json.loads(response.read())


def _launch(
    runtime_root: Path,
) -> tuple[subprocess.Popen[str], dict[str, object], dict[str, object]]:
    process = _spawn(runtime_root)
    try:
        assert process.stdout is not None
        readable, _, _ = select.select([process.stdout], [], [], 15)
        assert readable
        descriptor = json.loads(process.stdout.readline())
        _port(descriptor)
        return process, descriptor, _ready(descriptor)
    except BaseException:
        _stop(process)
        raise


def _stop(process: subprocess.Popen[str]) -> str:
    stdout = ""
    try:
        if process.poll() is None:
            process_group = _owned_process_group(process)
            if process_group is not None:
                os.killpg(process_group, signal.SIGTERM)
        try:
            stdout, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process_group = _owned_process_group(process)
            if process_group is not None:
                os.killpg(process_group, signal.SIGKILL)
            stdout, _ = process.communicate(timeout=5)
        return stdout
    finally:
        if process.poll() is None:
            process_group = _owned_process_group(process)
            if process_group is not None:
                os.killpg(process_group, signal.SIGKILL)
            process.communicate(timeout=5)


def _registry(descriptor: dict[str, object]) -> dict[str, object]:
    base_url = descriptor["base_url"]
    assert isinstance(base_url, str)
    with urlopen(base_url + "/api/project-registry/projects", timeout=5) as response:
        return json.loads(response.read())


def test_ephemeral_launcher_runs_real_lifespan_and_reuses_known_root(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    first, descriptor, ready = _launch(runtime_root)
    try:
        assert set(descriptor) == {
            "schema_version", "state", "base_url", "pid", "ready_path", "ready_token",
        }
        assert descriptor["schema_version"] == 1
        assert descriptor["state"] == "starting"
        assert descriptor["base_url"].startswith("http://127.0.0.1:")
        assert descriptor["ready_path"] == "/health/ephemeral"
        assert descriptor["pid"] == first.pid
        assert ready == {
            "ready": True,
            "ready_token": descriptor["ready_token"],
            "pid": descriptor["pid"],
            "port": _port(descriptor),
        }
        live = _live(descriptor)
        assert live["status"] == "live"
        identity = live["identity"]
        assert identity["edition"] == "source"
        assert identity["source_sha"] == SOURCE_SHA
        assert identity["pid"] == descriptor["pid"]
        running_marker = json.loads(
            (runtime_root / ".cockpit-ephemeral-root.json").read_text()
        )
        assert set(running_marker) == {
            "catalog_sha256", "root_id", "schema_version", "state",
        }
        assert running_marker["catalog_sha256"] is None
        assert running_marker["schema_version"] == 1
        assert running_marker["state"] == "running"
    finally:
        assert _stop(first) == ""

    marker = json.loads((runtime_root / ".cockpit-ephemeral-root.json").read_text())
    catalog_bytes = (runtime_root / ".cockpit-ephemeral-catalog.json").read_bytes()
    catalog = json.loads(catalog_bytes)
    herdr_config = runtime_root / "herdr" / "config.toml"
    herdr_config_bytes = herdr_config.read_bytes()
    assert tomllib.loads(herdr_config_bytes.decode("ascii")) == {
        "onboarding": False,
        "ui": {
            "agent_panel_sort": "spaces",
            "toast": {"delivery": "terminal"},
        },
        "theme": {"name": "catppuccin", "auto_switch": False},
        "terminal": {"default_shell": "/bin/sh", "shell_mode": "non_login"},
    }
    assert herdr_config.stat().st_mode & 0o777 == 0o600
    assert marker["state"] == "ready"
    assert marker["catalog_sha256"] == sha256(catalog_bytes).hexdigest()
    assert {entry["path"] for entry in catalog["entries"]} >= {
        "config", "data", "herdr", "home", "mail", "release", "state", "tmp", "uploads",
    }
    config_entry = next(
        entry for entry in catalog["entries"]
        if entry["path"] == "herdr/config.toml"
    )
    assert config_entry["mode"] == 0o600
    assert config_entry["sha256"] == sha256(herdr_config_bytes).hexdigest()

    second, descriptor, ready = _launch(runtime_root)
    try:
        assert ready["ready_token"] == descriptor["ready_token"]
    finally:
        assert _stop(second) == ""


def test_ephemeral_launchers_keep_private_roots_independent(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir(mode=0o700)
    second_root.mkdir(mode=0o700)
    first, first_descriptor, first_ready = _launch(first_root)
    second, second_descriptor, second_ready = _launch(second_root)
    try:
        assert first_descriptor["pid"] != second_descriptor["pid"]
        assert first_descriptor["ready_token"] != second_descriptor["ready_token"]
        assert first_ready["port"] != second_ready["port"]
        assert _registry(first_descriptor)["data"]["items"] == []
        assert _registry(second_descriptor)["data"]["items"] == []
    finally:
        assert _stop(second) == ""
        assert _stop(first) == ""


def test_ephemeral_live_root_rejects_second_launcher(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    first, _, _ = _launch(runtime_root)
    contender = _spawn(runtime_root)
    try:
        stdout, stderr = contender.communicate(timeout=15)
        assert contender.returncode == 2
        assert stdout == ""
        assert stderr.strip() == "instance_locked"
    finally:
        assert _stop(contender) == ""
        assert _stop(first) == ""


def _process_env(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    values: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        values[key.decode("ascii", "replace")] = value.decode("utf-8", "replace")
    return values


def _session_from_marker(runtime_root: Path) -> str:
    marker = json.loads((runtime_root / ".cockpit-ephemeral-root.json").read_text())
    root_id = marker["root_id"]
    assert isinstance(root_id, str) and len(root_id) == 64
    return f"ephemeral-{root_id[:32]}"


def _plant_unix_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
    finally:
        listener.close()
    os.chmod(path, 0o600)


def _short_runtime_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="bx-eph.", dir="/tmp"))
    os.chmod(root, 0o700)
    return root


def _bound_ephemeral_root(runtime_root: Path) -> tuple[dict[str, str], str]:
    runtime_root.mkdir(mode=0o700, exist_ok=True)
    assert next_profile.initialize_empty_ephemeral_runtime_root(runtime_root)
    for name in ("data", "config", "state", "uploads", "mail", "release", "herdr", "home", "tmp"):
        (runtime_root / name).mkdir(mode=0o700)
    session = _session_from_marker(runtime_root)
    environment = {
        "COCKPIT_NEXT_PROFILE": next_profile.EPHEMERAL_PROFILE,
        "COCKPIT_EPHEMERAL_ROOT": str(runtime_root),
    }
    next_profile.activate_ephemeral_runtime_root(runtime_root)
    return environment, session


def test_same_root_keeps_herdr_session_when_ready_token_rotates() -> None:
    runtime_root = _short_runtime_root()
    try:
        _assert_same_root_session(runtime_root)
    finally:
        shutil.rmtree(runtime_root, ignore_errors=True)


def _assert_same_root_session(runtime_root: Path) -> None:
    first, first_descriptor, _ = _launch(runtime_root)
    try:
        first_session = _process_env(first.pid)["HERDR_SESSION"]
        assert first_session == _session_from_marker(runtime_root)
        assert first_session != f"ephemeral-{first_descriptor['ready_token']}"
    finally:
        assert _stop(first) == ""

    second, second_descriptor, _ = _launch(runtime_root)
    try:
        second_session = _process_env(second.pid)["HERDR_SESSION"]
        assert first_descriptor["ready_token"] != second_descriptor["ready_token"]
        assert second_session == first_session
        assert second_session == _session_from_marker(runtime_root)
    finally:
        assert _stop(second) == ""


def test_authorized_herdr_socket_can_finalize_and_prepare() -> None:
    runtime_root = _short_runtime_root()
    try:
        _assert_authorized_socket_catalog(runtime_root)
    finally:
        shutil.rmtree(runtime_root, ignore_errors=True)


def _assert_authorized_socket_catalog(runtime_root: Path) -> None:
    environment, session = _bound_ephemeral_root(runtime_root)
    session_dir = runtime_root / "config" / "herdr" / "sessions" / session
    _plant_unix_socket(session_dir / "herdr.sock")
    _plant_unix_socket(session_dir / "herdr-client.sock")
    log = session_dir / "herdr-server.log"
    log.write_bytes(b"boot\n")
    os.chmod(log, 0o644)
    next_profile.finalize_ephemeral_runtime_root(environment)
    next_profile.prepare_ephemeral_runtime_root(runtime_root)
    catalog = json.loads((runtime_root / next_profile.EPHEMERAL_CATALOG).read_text())
    assert catalog["schema_version"] == 1
    assert set(catalog) == {"schema_version", "root_id", "entries"}
    omitted = {
        f"config/herdr/sessions/{session}/herdr.sock",
        f"config/herdr/sessions/{session}/herdr-client.sock",
        f"config/herdr/sessions/{session}/herdr-server.log",
    }
    paths = {entry["path"] for entry in catalog["entries"]}
    assert omitted.isdisjoint(paths)
    assert {entry["type"] for entry in catalog["entries"]} <= {"directory", "file"}
    assert f"config/herdr/sessions/{session}" in paths
    assert (session_dir / "herdr.sock").exists()
    assert (session_dir / "herdr-client.sock").exists()
    assert (session_dir / "herdr-server.log").exists()


def test_authorized_herdr_log_rejects_group_or_other_write() -> None:
    runtime_root = _short_runtime_root()
    try:
        environment, session = _bound_ephemeral_root(runtime_root)
        log = runtime_root / "config" / "herdr" / "sessions" / session / "herdr-server.log"
        log.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        log.write_bytes(b"boot\n")
        os.chmod(log, 0o666)
        with pytest.raises(next_profile.NextProfileError, match="ephemeral_catalog_invalid"):
            next_profile.finalize_ephemeral_runtime_root(environment)
    finally:
        shutil.rmtree(runtime_root, ignore_errors=True)


def test_foreign_herdr_session_socket_is_rejected() -> None:
    runtime_root = _short_runtime_root()
    try:
        environment, _session = _bound_ephemeral_root(runtime_root)
        foreign = runtime_root / "config" / "herdr" / "sessions" / (
            "ephemeral-" + "f" * 32
        )
        _plant_unix_socket(foreign / "herdr.sock")
        with pytest.raises(next_profile.NextProfileError, match="ephemeral_catalog_invalid"):
            next_profile.finalize_ephemeral_runtime_root(environment)
    finally:
        shutil.rmtree(runtime_root, ignore_errors=True)


def _snapshot_panes(herdr: Path, session: str, env: dict[str, str]) -> list[object]:
    raw = subprocess.check_output(
        [str(herdr), "api", "snapshot", "--session", session],
        env=env,
        text=True,
    )
    payload = raw
    for line in raw.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            break
    data = json.loads(payload)
    panes = data["result"]["snapshot"]["panes"]
    assert isinstance(panes, list)
    return panes


def test_real_herdr_session_survives_graceful_shutdown_and_restart() -> None:
    previous = os.umask(0o022)
    herdr = Path.home() / ".local" / "bin" / "herdr"
    assert herdr.is_file()
    runtime_root = _short_runtime_root()
    herdr_proc: subprocess.Popen[str] | None = None
    first: subprocess.Popen[str] | None = None
    second: subprocess.Popen[str] | None = None
    session = ""
    isolated: dict[str, str] = {}
    try:
        first, _, _ = _launch(runtime_root)
        session = _process_env(first.pid)["HERDR_SESSION"]
        isolated = {
            **os.environ,
            "HERDR_CONFIG_PATH": str(runtime_root / "herdr" / "config.toml"),
            "XDG_CONFIG_HOME": str(runtime_root / "config"),
            "XDG_DATA_HOME": str(runtime_root / "data"),
            "XDG_STATE_HOME": str(runtime_root / "state"),
            "HOME": str(runtime_root / "home"),
            "HERDR_SESSION": session,
        }
        herdr_proc = subprocess.Popen(
            [str(herdr), "--session", session, "server"],
            cwd=runtime_root,
            env=isolated,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        session_dir = runtime_root / "config" / "herdr" / "sessions" / session
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (session_dir / "herdr.sock").exists() and (
                session_dir / "herdr-client.sock"
            ).exists():
                break
            time.sleep(0.05)
        else:
            raise AssertionError("herdr session sockets did not appear")
        listed = subprocess.check_output(
            [str(herdr), "session", "list", "--json"],
            env=isolated,
            text=True,
        )
        assert session in listed
        panes = _snapshot_panes(herdr, session, isolated)
        assert _stop(first) == ""
        first = None

        marker = json.loads((runtime_root / ".cockpit-ephemeral-root.json").read_text())
        assert marker["state"] == "ready"
        assert (session_dir / "herdr.sock").exists()

        second, _, _ = _launch(runtime_root)
        assert _process_env(second.pid)["HERDR_SESSION"] == session
        assert _snapshot_panes(herdr, session, isolated) == panes
    finally:
        if first is not None:
            _stop(first)
        if second is not None:
            _stop(second)
        if session:
            subprocess.run(
                [str(herdr), "session", "stop", session],
                env=isolated or None,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [str(herdr), "session", "delete", session],
                env=isolated or None,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if herdr_proc is not None and herdr_proc.poll() is None:
            try:
                os.killpg(herdr_proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            herdr_proc.wait(timeout=5)
        shutil.rmtree(runtime_root, ignore_errors=True)
        os.umask(previous)


def test_cleanup_kills_only_a_stubborn_owned_process_group() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)",
        ],
        start_new_session=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _owned_process_group(process) == process.pid
        assert process.stdout is not None
        assert process.stdout.readline() == "ready\n"
        assert _stop(process) == ""
        assert process.returncode == -signal.SIGKILL
    finally:
        _stop(process)
