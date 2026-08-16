from __future__ import annotations

import json
import os
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from agent_cockpit import local_codex_harness as harness_mod
from agent_cockpit import next_profile


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "next_ephemeral_server.py"
RESERVED_PORTS = {8790, 18790}
SOURCE_SHA = "a" * 40
REAL_PROVIDER_CONFIG = Path.home() / ".codex" / "relay.config.toml"


def _spawn(
    runtime_root: Path, *, provider_config: Path | None = None,
) -> subprocess.Popen[str]:
    argv = [
        sys.executable,
        str(LAUNCHER),
        "--runtime-root",
        str(runtime_root),
        "--source-sha",
        SOURCE_SHA,
    ]
    if provider_config is not None:
        argv.extend(["--codex-provider-config", str(provider_config)])
    return subprocess.Popen(
        argv,
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
    runtime_root: Path, *, provider_config: Path | None = None,
) -> tuple[subprocess.Popen[str], dict[str, object], dict[str, object]]:
    process = _spawn(runtime_root, provider_config=provider_config)
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


def _request_json(
    descriptor: dict[str, object], path: str, *, method: str = "GET",
    body: dict[str, object] | None = None,
    idempotency_key: str | None = None, timeout: float = 15,
) -> tuple[int, dict[str, object]]:
    base_url = descriptor["base_url"]
    assert isinstance(base_url, str)
    headers: dict[str, str] = {}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    request = Request(
        base_url + path, data=data, headers=headers, method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
            assert isinstance(payload, dict)
            return response.status, payload
    except HTTPError as exc:
        payload = json.loads(exc.read())
        assert isinstance(payload, dict)
        return exc.code, payload


def _provider_config(tmp_path: Path) -> tuple[Path, Path]:
    authority_dir = tmp_path / "external-authority"
    provider_home = tmp_path / "provider-home"
    authority_dir.mkdir(mode=0o700)
    provider_home.mkdir(mode=0o700)
    authority = authority_dir / "auth.json"
    authority.write_bytes(b"credential-bytes-must-not-be-read")
    authority.chmod(0o600)
    config = provider_home / "relay.config.toml"
    config.write_text(
        "\n".join([
            'model_provider = "relay"',
            "[model_providers.relay]",
            'name = "Fixture Relay"',
            'base_url = "https://relay.invalid"',
            'wire_api = "responses"',
            "[model_providers.relay.auth]",
            'command = "/usr/bin/jq"',
            f'args = ["-r", ".OPENAI_API_KEY", {json.dumps(str(authority))}]',
            "timeout_ms = 5000",
            "",
        ]),
        encoding="utf-8",
    )
    config.chmod(0o600)
    return config, authority


def test_launcher_ignores_inherited_provider_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    monkeypatch.setenv(
        "COCKPIT_CODEX_PROVIDER_CONFIG_PATH", "/inherited/not/authorized.toml",
    )
    monkeypatch.setenv("CODEX_HOME", "/inherited/not/authorized-home")
    process, _descriptor, _ready_result = _launch(runtime_root)
    try:
        environment = _process_env(process.pid)
        assert "COCKPIT_CODEX_PROVIDER_CONFIG_PATH" not in environment
        assert "CODEX_HOME" not in environment
    finally:
        assert _stop(process) == ""


def test_launcher_injects_only_explicit_validated_provider_reference(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    config, authority = _provider_config(tmp_path)
    identity = (authority.stat().st_dev, authority.stat().st_ino)
    process, _descriptor, _ready_result = _launch(
        runtime_root, provider_config=config,
    )
    try:
        environment = _process_env(process.pid)
        assert environment["COCKPIT_CODEX_PROVIDER_CONFIG_PATH"] == str(config)
        assert (authority.stat().st_dev, authority.stat().st_ino) == identity
    finally:
        assert _stop(process) == ""


@pytest.mark.parametrize("failure", ["config_symlink", "authority_mode", "inside_runtime"])
def test_launcher_rejects_invalid_explicit_provider_before_server_start(
    tmp_path: Path, failure: str,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    config, authority = _provider_config(tmp_path)
    if failure == "config_symlink":
        target = config.with_name("provider-target.toml")
        config.replace(target)
        config.symlink_to(target)
    elif failure == "authority_mode":
        authority.chmod(0o640)
    else:
        inside = runtime_root / "provider.toml"
        config.replace(inside)
        config = inside

    process = _spawn(runtime_root, provider_config=config)
    stdout, stderr = process.communicate(timeout=15)

    assert process.returncode == 2
    assert stdout == ""
    assert stderr.strip() == "provider_config_invalid"


@pytest.mark.skipif(
    shutil.which("codex") is None
    or shutil.which("herdr") is None
    or not REAL_PROVIDER_CONFIG.is_file(),
    reason="需要真实 herdr/codex 与显式 host provider config",
)
def test_real_http_attach_dispatch_proves_working_without_prompt_bypass(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    process, descriptor, _ready_result = _launch(
        runtime_root, provider_config=REAL_PROVIDER_CONFIG,
    )
    session = _session_from_marker(runtime_root)
    prep_url: str | None = None
    try:
        source = runtime_root / "uploads" / "e3-http-source"
        source.mkdir(mode=0o700)
        (source / "README.md").write_text("isolated source\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(source)], check=True,
        )
        subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
        subprocess.run(
            [
                "git", "-C", str(source), "-c", "user.name=E3 HTTP",
                "-c", "user.email=e3-http.invalid", "commit", "-q", "-m", "seed",
            ],
            check=True,
        )
        source_head = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
        ).strip()
        source_tree = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], text=True,
        ).strip()
        source_readme = (source / "README.md").read_bytes()

        status, roots = _request_json(
            descriptor, "/api/runtime-nodes/local/roots",
        )
        assert status == 200
        root = next(
            item for item in roots["data"]["items"]
            if item["display_name"] == "uploads"
        )
        locator = {
            "node_id": "local", "root_id": root["root_id"],
            "path": source.name,
        }
        status, discovered = _request_json(
            descriptor, "/api/project-discovery", method="POST",
            body={"locator": locator},
        )
        assert status == 200
        discovery = discovered["data"]
        assert discovery["complete"] is True

        status, created_project = _request_json(
            descriptor, "/api/project-registry/projects", method="POST",
            body={
                "display_name": "E3 HTTP", "slug": "e3-http", "goal": None,
                "locator": locator,
                "expected_discovery_fingerprint": discovery["discovery_fingerprint"],
            },
            idempotency_key="e3-http-project",
        )
        assert status == 201
        project = created_project["data"]
        project_id = project["project_id"]
        location_id = project["repo_location"]["repo_location_id"]

        status, created_workspace = _request_json(
            descriptor,
            f"/api/project-registry/projects/{project_id}/workspaces",
            method="POST",
            body={
                "repo_location_id": location_id, "name": "E3 HTTP",
                "goal": None, "isolation_kind": "shared",
            },
            idempotency_key="e3-http-workspace",
        )
        assert status == 201
        workspace_id = created_workspace["data"]["workspace_id"]
        work_url = (
            f"/api/projects/{project_id}/workspaces/{workspace_id}/work-items"
        )
        status, created_work = _request_json(
            descriptor, work_url, method="POST",
            body={
                "body": (
                    "Create E3_HTTP_PROOF.txt containing exactly the single line "
                    "http product chain, then complete the work."
                ),
                "acceptance": None,
                "constraints": "Only change E3_HTTP_PROOF.txt in the managed checkout.",
            },
            idempotency_key="e3-http-work",
        )
        assert status == 201
        work = created_work["data"]["work_item"]
        work_item_id = work["work_item_id"]

        members_url = (
            f"/api/projects/{project_id}/workspaces/{workspace_id}/members"
        )
        status, member = _request_json(
            descriptor, members_url, method="POST",
            body={"display_name": "E3 HTTP Codex"},
            idempotency_key="e3-http-member",
        )
        assert status == 201
        prep_url = work_url + f"/{work_item_id}/preparation"
        status, prepared = _request_json(
            descriptor, prep_url, method="POST",
            body={"identity_id": member["data"]["identity_id"]},
            idempotency_key="e3-http-prepare",
        )
        assert status == 201
        status, attached = _request_json(
            descriptor, prep_url + "/attach", method="POST",
            body={"expected_revision": prepared["data"]["revision"]},
            idempotency_key="e3-http-attach", timeout=45,
        )
        assert status == 200, attached
        assert attached["data"]["state"] == "connected_readonly"
        detail_url = work_url + f"/{work_item_id}"
        status, dispatch_detail = _request_json(descriptor, detail_url)
        assert status == 200

        status, dispatched = _request_json(
            descriptor, work_url + f"/{work_item_id}/dispatch", method="POST",
            body={
                "expected_work_revision": dispatch_detail["data"]["work_item"][
                    "revision"
                ],
                "expected_preparation_revision": attached["data"]["revision"],
            },
            idempotency_key="e3-http-dispatch", timeout=60,
        )
        assert status == 200
        assert dispatched["data"]["outcome"] == "succeeded"
        status, detail = _request_json(descriptor, detail_url)
        assert status == 200
        assert [item["outcome"] for item in detail["data"]["receipts"]] == [
            "intent", "succeeded",
        ]
        assert detail["data"]["receipts"][-1]["evidence_digest"] == (
            harness_mod.WAKEUP_DIGEST
        )

        assert subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
        ).strip() == source_head
        assert subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], text=True,
        ).strip() == source_tree
        assert subprocess.check_output(
            ["git", "-C", str(source), "status", "--porcelain"], text=True,
        ) == ""
        assert (source / "README.md").read_bytes() == source_readme
        managed = list((runtime_root / "data/worktrees/managed-checkouts").iterdir())
        assert len(managed) == 1
        assert subprocess.check_output(
            ["git", "-C", str(managed[0]), "status", "--porcelain"], text=True,
        ) == ""

        status, current_prep = _request_json(descriptor, prep_url)
        assert status == 200
        status, detached = _request_json(
            descriptor, prep_url + "/detach", method="POST",
            body={"expected_revision": current_prep["data"]["revision"]},
            idempotency_key="e3-http-detach",
        )
        assert status == 200
        assert detached["data"]["state"] == "detached"
        capability_root = runtime_root / "data/workspace-capabilities"
        assert list(capability_root.iterdir()) == []
        prep_url = None
        status, stopped = _request_json(
            descriptor, f"/api/herdr/session/{session}/stop", method="POST",
        )
        assert status == 200 and not stopped.get("error")
        status, deleted = _request_json(
            descriptor, f"/api/herdr/session/{session}", method="DELETE",
        )
        assert status == 200 and deleted.get("deleted") == session
        assert _stop(process) == ""
        process = None

        marker_path = runtime_root / next_profile.EPHEMERAL_MARKER
        catalog_path = runtime_root / next_profile.EPHEMERAL_CATALOG
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        catalog_bytes = catalog_path.read_bytes()
        catalog = json.loads(catalog_bytes)
        assert marker["state"] == "ready"
        assert marker["catalog_sha256"] == sha256(catalog_bytes).hexdigest()
        assert not any(
            entry["path"].startswith("data/workspace-capabilities/att_")
            for entry in catalog["entries"]
        )

        restarted, restarted_descriptor, restarted_ready = _launch(
            runtime_root, provider_config=REAL_PROVIDER_CONFIG,
        )
        try:
            assert restarted_ready["ready_token"] == (
                restarted_descriptor["ready_token"]
            )
            assert _live(restarted_descriptor)["status"] == "live"
        finally:
            assert _stop(restarted) == ""
    finally:
        if prep_url is not None:
            try:
                _status, current = _request_json(descriptor, prep_url)
                _request_json(
                    descriptor, prep_url + "/detach", method="POST",
                    body={"expected_revision": current["data"]["revision"]},
                    idempotency_key="e3-http-detach-finally",
                )
            except Exception:
                pass
        try:
            _request_json(
                descriptor, f"/api/herdr/session/{session}/stop", method="POST",
            )
        except Exception:
            pass
        if process is not None:
            assert _stop(process) == ""


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


def _long_runtime_root() -> Path:
    root = Path(tempfile.mkdtemp(
        prefix="agent-cockpit-e3-socket-" + "x" * 48 + ".",
        dir="/tmp",
    ))
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


def test_long_runtime_root_gets_private_bounded_herdr_socket_path() -> None:
    runtime_root = _long_runtime_root()
    alias: Path | None = None
    listener: socket.socket | None = None
    try:
        _environment, session = _bound_ephemeral_root(runtime_root)
        alias = next_profile.prepare_ephemeral_herdr_config_home(runtime_root)
        assert alias == next_profile.ephemeral_herdr_config_home(runtime_root)
        info = alias.lstat()
        assert stat.S_ISDIR(info.st_mode)
        assert not stat.S_ISLNK(info.st_mode)
        assert info.st_uid == os.getuid()
        assert stat.S_IMODE(info.st_mode) == 0o700
        longest = alias / "herdr" / "sessions" / session / "herdr-client.sock"
        assert len(os.fsencode(longest)) <= 107
        longest.parent.mkdir(parents=True, mode=0o700)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(longest))
        assert longest.is_socket()
        assert longest.resolve().is_relative_to(runtime_root / "config" / "herdr")
    finally:
        if listener is not None:
            listener.close()
        if alias is not None:
            target = alias / "herdr" / "sessions"
            if target.exists():
                shutil.rmtree(target)
            next_profile.release_ephemeral_herdr_config_home(runtime_root)
            assert not alias.exists()
        shutil.rmtree(runtime_root, ignore_errors=True)


def test_two_long_runtime_roots_prepare_distinct_herdr_sessions_concurrently() -> None:
    roots = [_long_runtime_root(), _long_runtime_root()]
    aliases: list[Path] = []
    listeners: list[socket.socket] = []
    try:
        sessions = [_bound_ephemeral_root(root)[1] for root in roots]
        with ThreadPoolExecutor(max_workers=2) as pool:
            aliases = list(pool.map(
                next_profile.prepare_ephemeral_herdr_config_home,
                roots,
            ))
        assert len(set(aliases)) == 2
        assert len(set(sessions)) == 2
        for alias, session in zip(aliases, sessions, strict=True):
            path = alias / "herdr" / "sessions" / session / "herdr.sock"
            assert len(os.fsencode(path)) <= 107
            path.parent.mkdir(parents=True, mode=0o700)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            listeners.append(listener)
        assert all(
            (alias / "herdr" / "sessions" / session / "herdr.sock").is_socket()
            for alias, session in zip(aliases, sessions, strict=True)
        )
    finally:
        for listener in listeners:
            listener.close()
        for root, alias in zip(roots, aliases, strict=False):
            target = alias / "herdr" / "sessions"
            if target.exists():
                shutil.rmtree(target)
            next_profile.release_ephemeral_herdr_config_home(root)
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)


def test_short_herdr_home_prefix_collision_is_rejected_without_borrowing() -> None:
    first = _long_runtime_root()
    second = _long_runtime_root()
    first_alias: Path | None = None
    try:
        _bound_ephemeral_root(first)
        _bound_ephemeral_root(second)
        first_marker = json.loads(
            (first / next_profile.EPHEMERAL_MARKER).read_text()
        )
        second_marker_path = second / next_profile.EPHEMERAL_MARKER
        second_marker = json.loads(second_marker_path.read_text())
        second_marker["root_id"] = (
            first_marker["root_id"][:20] + second_marker["root_id"][20:]
        )
        assert second_marker["root_id"] != first_marker["root_id"]
        second_marker_path.write_text(
            json.dumps(second_marker, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
        second_marker_path.chmod(0o600)
        first_alias = next_profile.prepare_ephemeral_herdr_config_home(first)
        before = {
            path.name: (
                path.lstat().st_mode,
                os.readlink(path) if path.is_symlink() else path.read_bytes(),
            )
            for path in first_alias.iterdir()
        }
        with pytest.raises(
            next_profile.NextProfileError,
            match="ephemeral_herdr_home_collision",
        ):
            next_profile.prepare_ephemeral_herdr_config_home(second)
        after = {
            path.name: (
                path.lstat().st_mode,
                os.readlink(path) if path.is_symlink() else path.read_bytes(),
            )
            for path in first_alias.iterdir()
        }
        assert after == before
    finally:
        if first_alias is not None:
            next_profile.release_ephemeral_herdr_config_home(first)
        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)


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
    runtime_root = _long_runtime_root()
    herdr_proc: subprocess.Popen[str] | None = None
    first: subprocess.Popen[str] | None = None
    second: subprocess.Popen[str] | None = None
    session = ""
    isolated: dict[str, str] = {}
    alias: Path | None = None
    try:
        first, _, _ = _launch(runtime_root)
        first_environment = _process_env(first.pid)
        session = first_environment["HERDR_SESSION"]
        alias = Path(first_environment["XDG_CONFIG_HOME"])
        assert alias == next_profile.ephemeral_herdr_config_home(runtime_root)
        isolated = {
            **os.environ,
            "HERDR_CONFIG_PATH": str(runtime_root / "herdr" / "config.toml"),
            "XDG_CONFIG_HOME": str(alias),
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
        session_dir = alias / "herdr" / "sessions" / session
        target_session_dir = (
            runtime_root / "config" / "herdr" / "sessions" / session
        )
        assert len(os.fsencode(session_dir / "herdr-client.sock")) <= 107
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
        assert not alias.exists()
        assert (target_session_dir / "herdr.sock").exists()

        second, _, _ = _launch(runtime_root)
        second_environment = _process_env(second.pid)
        assert second_environment["HERDR_SESSION"] == session
        assert Path(second_environment["XDG_CONFIG_HOME"]) == alias
        assert alias.exists()
        assert _snapshot_panes(herdr, session, isolated) == panes
    finally:
        if first is not None:
            assert _stop(first) == ""
        if second is not None:
            assert _stop(second) == ""
            assert alias is not None
            assert not alias.exists()
        if session and runtime_root.exists():
            alias = next_profile.prepare_ephemeral_herdr_config_home(runtime_root)
            stopped = subprocess.run(
                [str(herdr), "session", "stop", session],
                env=isolated or None,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            assert stopped.returncode == 0
            deleted = subprocess.run(
                [str(herdr), "session", "delete", session],
                env=isolated or None,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            assert deleted.returncode == 0
            next_profile.release_ephemeral_herdr_config_home(runtime_root)
            assert not alias.exists()
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
