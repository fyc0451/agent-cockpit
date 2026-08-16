from __future__ import annotations

import json
import os
import select
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_cockpit import next_profile


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
LAUNCHER = ROOT / "scripts" / "next_ephemeral_server.py"
PROVIDER_CONFIG = Path.home() / ".codex" / "relay.config.toml"
RESERVED_PORTS = {8790, 18790}
SOURCES = {
    "release-gate-happy": "release-gate-happy-source",
    "release-gate-recovery": "release-gate-recovery-source",
    "release-gate-malformed": "release-gate-malformed-source",
}
TARGET = "LOCAL_WEB_RELEASE_GATE.txt"
TARGET_BYTES = b"local web release gate\n"


def _safe_environment() -> dict[str, str]:
    blocked = ("TOKEN", "COOKIE", "CREDENTIAL", "PASSWORD", "SECRET", "API_KEY")
    return {
        key: value for key, value in os.environ.items()
        if not any(part in key.upper() for part in blocked)
        and key not in {"CODEX_HOME", "COCKPIT_TOKEN"}
    }


def _require_prerequisites() -> tuple[str, str, str, Path]:
    codex = shutil.which("codex")
    herdr = shutil.which("herdr")
    npm = shutil.which("npm")
    assert codex is not None, "local_web_release_gate: codex_missing"
    assert herdr is not None, "local_web_release_gate: herdr_missing"
    assert npm is not None, "local_web_release_gate: npm_missing"
    playwright = WEB / "node_modules" / ".bin" / "playwright"
    assert playwright.is_file() and os.access(playwright, os.X_OK), (
        "local_web_release_gate: playwright_missing"
    )
    info = PROVIDER_CONFIG.lstat()
    assert stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
    return codex, herdr, npm, playwright


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True,
    ).strip()


def _seed_source(path: Path) -> dict[str, object]:
    path.mkdir(mode=0o700)
    readme = path / "README.md"
    readme.write_bytes(b"local web release gate source\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(path), "-c", "user.name=Local Web Gate",
            "-c", "user.email=local-web-gate.invalid", "commit", "-q",
            "-m", "seed",
        ],
        check=True,
    )
    return {
        "path": path.resolve(),
        "head": _git(path, "rev-parse", "HEAD"),
        "tree": _git(path, "rev-parse", "HEAD^{tree}"),
        "status": _git(path, "status", "--porcelain"),
        "readme": readme.read_bytes(),
    }


def _spawn(runtime: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable, str(LAUNCHER), "--runtime-root", str(runtime),
            "--source-sha", "0" * 40,
            "--codex-provider-config", str(PROVIDER_CONFIG),
        ],
        cwd=ROOT,
        env={**_safe_environment(), "PYTHONUNBUFFERED": "1"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _launch(runtime: Path) -> tuple[subprocess.Popen[str], dict[str, object]]:
    process = _spawn(runtime)
    assert process.stdout is not None
    readable, _, _ = select.select([process.stdout], [], [], 20)
    assert readable, "local_web_release_gate: launcher_descriptor_timeout"
    descriptor = json.loads(process.stdout.readline())
    base_url = descriptor["base_url"]
    assert isinstance(base_url, str) and base_url.startswith("http://127.0.0.1:")
    port = int(base_url.rsplit(":", 1)[1])
    assert 1024 <= port <= 65535 and port not in RESERVED_PORTS
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urlopen(base_url + str(descriptor["ready_path"]), timeout=1) as response:
                ready = json.loads(response.read())
            assert ready["ready"] is True
            assert ready["ready_token"] == descriptor["ready_token"]
            return process, descriptor
        except OSError:
            time.sleep(0.05)
    raise AssertionError("local_web_release_gate: server_ready_timeout")


def _request(base_url: str, path: str, method: str) -> int:
    request = Request(base_url + path, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            response.read()
            return response.status
    except HTTPError as exc:
        exc.read()
        return exc.code


def _stop(process: subprocess.Popen[str]) -> None:
    sent_term = process.poll() is None
    if process.poll() is None:
        assert os.getpgid(process.pid) == process.pid
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
    assert process.returncode == 0 or (
        sent_term and process.returncode == -signal.SIGTERM
    )


def _rows(path: Path, query: str, parameters: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query, parameters).fetchall()
    finally:
        connection.close()


def _assert_source_unchanged(snapshot: dict[str, object]) -> None:
    path = snapshot["path"]
    assert isinstance(path, Path)
    assert path.resolve() == snapshot["path"]
    assert _git(path, "rev-parse", "HEAD") == snapshot["head"]
    assert _git(path, "rev-parse", "HEAD^{tree}") == snapshot["tree"]
    assert _git(path, "status", "--porcelain") == snapshot["status"] == ""
    assert (path / "README.md").read_bytes() == snapshot["readme"]
    assert not (path / TARGET).exists()


def _assert_durable_gate(runtime: Path, snapshots: dict[str, dict[str, object]]) -> None:
    registry_path = runtime / "data" / "project-registry.sqlite3"
    work_path = runtime / "data" / "workspace-work.sqlite3"
    execution_path = runtime / "data" / "workspace-execution.sqlite3"

    projects = _rows(
        registry_path,
        "SELECT project_id,slug FROM projects ORDER BY slug",
    )
    assert {row["slug"] for row in projects} == set(SOURCES)
    assert len(projects) == len({row["project_id"] for row in projects}) == 3
    project_ids = {row["slug"]: row["project_id"] for row in projects}

    locations = _rows(
        registry_path,
        "SELECT project_id,canonical_path FROM repo_locations",
    )
    workspaces = _rows(
        registry_path,
        "SELECT project_id,workspace_id FROM workspaces",
    )
    assert len(locations) == len(workspaces) == 3
    assert {row["project_id"] for row in locations} == set(project_ids.values())
    assert {row["project_id"] for row in workspaces} == set(project_ids.values())
    expected_paths = {str(snapshot["path"]) for snapshot in snapshots.values()}
    assert {row["canonical_path"] for row in locations} == expected_paths

    works = _rows(
        work_path,
        "SELECT t.project_id,t.workspace_id,w.work_item_id,w.status "
        "FROM message_threads t JOIN messages m ON m.thread_id=t.thread_id "
        "AND m.message_kind='root' JOIN work_items w "
        "ON w.source_message_id=m.message_id ORDER BY t.project_id",
    )
    assert len(works) == 3
    assert len({row["work_item_id"] for row in works}) == 3
    work_by_project = {row["project_id"]: row for row in works}
    happy_project = project_ids["release-gate-happy"]
    assert work_by_project[happy_project]["status"] == "completed"
    assert {
        row["status"] for row in works if row["project_id"] != happy_project
    } == {"unassigned"}

    identities = _rows(
        execution_path,
        "SELECT p.project_id,p.workspace_id,p.identity_id,p.generation,"
        "a.project_id AS identity_project_id,"
        "a.workspace_id AS identity_workspace_id "
        "FROM work_item_preparations p JOIN agent_identities a USING(identity_id)",
    )
    preparations = _rows(
        execution_path,
        "SELECT project_id,workspace_id,work_item_id,preparation_id,checkout_id,"
        "lease_id,attachment_id,state,generation FROM work_item_preparations",
    )
    checkouts = _rows(
        execution_path,
        "SELECT checkout_id,preparation_id,internal_path FROM managed_checkouts",
    )
    leases = _rows(
        execution_path,
        "SELECT lease_id,checkout_id,identity_id,generation,status,claim_id "
        "FROM writer_leases",
    )
    assert len(identities) == len(preparations) == len(checkouts) == len(leases) == 3
    assert all(
        row["project_id"] == row["identity_project_id"]
        and row["workspace_id"] == row["identity_workspace_id"]
        for row in identities
    )
    for rows, key in (
        (identities, "identity_id"),
        (preparations, "preparation_id"),
        (checkouts, "checkout_id"),
        (leases, "lease_id"),
    ):
        assert len({row[key] for row in rows}) == 3

    claims = _rows(
        work_path,
        "SELECT claim_id,work_item_id,identity_id,generation,state "
        "FROM work_item_claims",
    )
    assert len(claims) == 1
    claim = claims[0]
    happy_work = work_by_project[happy_project]
    happy_prep = next(row for row in preparations if row["project_id"] == happy_project)
    happy_lease = next(row for row in leases if row["lease_id"] == happy_prep["lease_id"])
    assert claim["work_item_id"] == happy_work["work_item_id"]
    assert claim["claim_id"] == happy_lease["claim_id"]
    assert claim["identity_id"] == happy_lease["identity_id"]
    assert claim["generation"] == happy_lease["generation"] == happy_prep["generation"]
    assert claim["state"] == "closed"
    assert happy_lease["status"] == "revoked"
    assert happy_prep["state"] == "detached"

    checkout_by_id = {row["checkout_id"]: Path(row["internal_path"]) for row in checkouts}
    for prep in preparations:
        checkout = checkout_by_id[prep["checkout_id"]]
        assert checkout.is_relative_to(runtime / "data" / "worktrees" / "managed-checkouts")
        assert checkout not in {snapshot["path"] for snapshot in snapshots.values()}
        status = _git(checkout, "status", "--porcelain", "--untracked-files=all")
        if prep["project_id"] == happy_project:
            assert status == f"?? {TARGET}"
            assert (checkout / TARGET).read_bytes() == TARGET_BYTES
        else:
            assert status == ""

    for snapshot in snapshots.values():
        _assert_source_unchanged(snapshot)


def test_local_web_release_gate(tmp_path: Path) -> None:
    _codex, _herdr, npm, playwright = _require_prerequisites()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700

    subprocess.run([npm, "run", "build"], cwd=WEB, check=True, env=_safe_environment())
    process, descriptor = _launch(runtime)
    base_url = descriptor["base_url"]
    assert isinstance(base_url, str)
    snapshots: dict[str, dict[str, object]] = {}
    try:
        for slug, source_name in SOURCES.items():
            snapshots[slug] = _seed_source(runtime / "uploads" / source_name)

        playwright_env = {
            **_safe_environment(),
            "PLAYWRIGHT_LIVE_BASE_URL": base_url,
            "PLAYWRIGHT_LIVE_ARTIFACT_DIR": str(tmp_path / "playwright-artifacts"),
            "LOCAL_WEB_GATE_HAPPY_SOURCE": SOURCES["release-gate-happy"],
            "LOCAL_WEB_GATE_RECOVERY_SOURCE": SOURCES["release-gate-recovery"],
            "LOCAL_WEB_GATE_MALFORMED_SOURCE": SOURCES["release-gate-malformed"],
        }
        completed = subprocess.run(
            [
                str(playwright), "test", "-c",
                "playwright.live.config.ts", "local-web-release-gate.spec.ts",
            ],
            cwd=WEB,
            env=playwright_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=600,
        )
        if completed.stdout:
            print(completed.stdout)
        assert completed.returncode == 0, completed.stdout

        session = next_profile.ephemeral_session_for_root(runtime)
        assert _request(base_url, f"/api/herdr/session/{session}/stop", "POST") == 200
        assert _request(base_url, f"/api/herdr/session/{session}", "DELETE") == 200
    finally:
        _stop(process)

    marker = json.loads((runtime / next_profile.EPHEMERAL_MARKER).read_text())
    assert marker["state"] == "ready"
    assert (runtime / next_profile.EPHEMERAL_CATALOG).is_file()
    _assert_durable_gate(runtime, snapshots)
