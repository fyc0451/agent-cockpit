from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
import tomllib
from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen


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
