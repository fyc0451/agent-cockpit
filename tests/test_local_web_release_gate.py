from __future__ import annotations

import hashlib
import json
import inspect
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
IGNORED_BYTES = b"ignored source bytes\x00\xff\n"
ACCEPTED_PRODUCT_EXACT = "eefc4fbedb4bb7d289a0f6ba0191989ed6e6f784"
ACCEPTED_PRODUCT_TREE = "9131a92d1b3ef463123adf4cb4f5610066a6a84c"
GATE_FILES = {
    "tests/test_local_web_release_gate.py",
    "web/e2e-live/local-web-release-gate.spec.ts",
}


def test_local_web_release_gate_static_contract() -> None:
    module = sys.modules[__name__]
    expected = "eefc4fbedb4bb7d289a0f6ba0191989ed6e6f784"
    assert getattr(module, "ACCEPTED_PRODUCT_EXACT", None) == expected

    spawn_source = inspect.getsource(module._spawn)
    assert "ACCEPTED_PRODUCT_EXACT" in spawn_source
    assert '"0" * 40' not in spawn_source

    stop_source = inspect.getsource(module._stop)
    assert stop_source.index("os.killpg") < stop_source.index("sent_term = True")

    outer_source = inspect.getsource(module.test_local_web_release_gate)
    for required in (
        "_assert_product_identity",
        "_herdr_sessions",
        "_cleanup_herdr_session",
        "prepare_ephemeral_runtime_root",
        "_assert_ephemeral_catalog",
    ):
        assert required in outer_source
    cleanup_block = (
        "finally:\n"
        "        try:\n"
        "            if process.poll() is None:\n"
        "                _cleanup_herdr_session(base_url, session)"
    )
    assert cleanup_block in outer_source
    assert outer_source.index("_assert_product_identity") < outer_source.index("_launch")

    spec = (WEB / "e2e-live" / "local-web-release-gate.spec.ts").read_text(
        encoding="utf-8",
    )
    for required in (
        "type PreparationReceipt",
        "createdBySlug",
        "recoveredPreparation",
        "LOCAL_WEB_GATE_RECEIPTS",
        "preparation_work_item_id",
        "checkout_id",
        "lease_id",
        "identity_id",
        "generation",
    ):
        assert required in spec


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


def _assert_product_identity() -> None:
    assert len(ACCEPTED_PRODUCT_EXACT) == 40
    assert len(ACCEPTED_PRODUCT_TREE) == 40
    if not (ROOT / ".git").exists():
        assert (ROOT / ".agent-memory-project").read_text(
            encoding="ascii",
        ) == "agent-cockpit-next\n"
        return

    assert _git(ROOT, "cat-file", "-t", ACCEPTED_PRODUCT_EXACT) == "commit"
    assert _git(ROOT, "rev-parse", f"{ACCEPTED_PRODUCT_EXACT}^{{tree}}") == (
        ACCEPTED_PRODUCT_TREE
    )
    changed = set(filter(None, _git(
        ROOT, "diff", "--name-only", ACCEPTED_PRODUCT_EXACT, "--",
    ).splitlines()))
    assert changed == GATE_FILES
    status_paths = {
        line.split(maxsplit=1)[1] for line in _git(
            ROOT, "status", "--porcelain", "--untracked-files=all",
        ).splitlines()
    }
    assert status_paths <= GATE_FILES


def _working_tree_bytes(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path)
        if relative.parts[0] == ".git":
            continue
        assert not item.is_symlink()
        if item.is_file():
            files[relative.as_posix()] = item.read_bytes()
    return files


def _git_worktree_paths(path: Path) -> set[Path]:
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in _git(path, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    }


def _seed_source(path: Path) -> dict[str, object]:
    path.mkdir(mode=0o700)
    readme = path / "README.md"
    readme.write_bytes(b"local web release gate source\n")
    (path / ".gitignore").write_bytes(b"ignored.bin\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md", ".gitignore"], check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(path), "-c", "user.name=Local Web Gate",
            "-c", "user.email=local-web-gate.invalid", "commit", "-q",
            "-m", "seed",
        ],
        check=True,
    )
    (path / "ignored.bin").write_bytes(IGNORED_BYTES)
    return {
        "path": path.resolve(),
        "head": _git(path, "rev-parse", "HEAD"),
        "tree": _git(path, "rev-parse", "HEAD^{tree}"),
        "status": _git(path, "status", "--porcelain"),
        "files": _working_tree_bytes(path),
    }


def _spawn(runtime: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable, str(LAUNCHER), "--runtime-root", str(runtime),
            "--source-sha", ACCEPTED_PRODUCT_EXACT,
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
            status, live = _request_json(base_url, "/health/live", "GET")
            assert status == 200 and isinstance(live, dict)
            identity = live.get("identity")
            assert isinstance(identity, dict)
            assert identity["source_sha"] == ACCEPTED_PRODUCT_EXACT
            assert identity["edition"] == "source"
            assert identity["pid"] == descriptor["pid"] == process.pid
            return process, descriptor
        except OSError:
            time.sleep(0.05)
    raise AssertionError("local_web_release_gate: server_ready_timeout")


def _request_json(base_url: str, path: str, method: str) -> tuple[int, object]:
    request = Request(base_url + path, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
    except HTTPError as exc:
        payload = exc.read()
        return exc.code, json.loads(payload) if payload else None


def _request(base_url: str, path: str, method: str) -> int:
    return _request_json(base_url, path, method)[0]


def _herdr_sessions(base_url: str) -> list[dict[str, object]]:
    status, payload = _request_json(base_url, "/api/herdr/sessions", "GET")
    assert status == 200 and isinstance(payload, dict)
    sessions = payload.get("sessions")
    assert isinstance(sessions, list)
    assert all(isinstance(item, dict) for item in sessions)
    return sessions


def _cleanup_herdr_session(base_url: str, session: str) -> None:
    sessions = _herdr_sessions(base_url)
    assert all(item.get("name") == session for item in sessions)
    if sessions:
        assert len(sessions) == 1
        assert _request(base_url, f"/api/herdr/session/{session}/stop", "POST") == 200
        stopped = _herdr_sessions(base_url)
        assert len(stopped) == 1 and stopped[0].get("name") == session
        assert _request(base_url, f"/api/herdr/session/{session}", "DELETE") == 200
    assert all(item.get("name") != session for item in _herdr_sessions(base_url))


def _stop(process: subprocess.Popen[str]) -> None:
    sent_term = False
    if process.poll() is None:
        assert os.getpgid(process.pid) == process.pid
        os.killpg(process.pid, signal.SIGTERM)
        sent_term = True
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
    assert _working_tree_bytes(path) == snapshot["files"]
    assert not (path / TARGET).exists()


def _load_receipts(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict) and set(value) == {
        "created_by_slug", "recovered_preparation",
    }
    created = value["created_by_slug"]
    assert isinstance(created, dict) and set(created) == set(SOURCES)
    expected_keys = {
        "preparation_work_item_id", "checkout_id", "lease_id", "identity_id",
        "generation", "source_head", "source_tree",
    }
    for receipt in [*created.values(), value["recovered_preparation"]]:
        assert isinstance(receipt, dict) and set(receipt) == expected_keys
        assert all(
            isinstance(receipt[key], str) and receipt[key]
            for key in expected_keys - {"generation"}
        )
        assert type(receipt["generation"]) is int and receipt["generation"] >= 1
    assert value["recovered_preparation"] == created["release-gate-recovery"]
    return value


def _assert_ephemeral_catalog(runtime: Path) -> None:
    marker_path = runtime / next_profile.EPHEMERAL_MARKER
    catalog_path = runtime / next_profile.EPHEMERAL_CATALOG
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    catalog_bytes = catalog_path.read_bytes()
    catalog = json.loads(catalog_bytes)
    assert set(marker) == {
        "schema_version", "root_id", "state", "catalog_sha256",
    }
    assert marker["schema_version"] == 1 and marker["state"] == "ready"
    assert marker["catalog_sha256"] == hashlib.sha256(catalog_bytes).hexdigest()
    assert set(catalog) == {"schema_version", "root_id", "entries"}
    assert catalog["schema_version"] == marker["schema_version"]
    assert catalog["root_id"] == marker["root_id"]
    assert isinstance(catalog["entries"], list) and catalog["entries"]
    paths = [entry["path"] for entry in catalog["entries"]]
    assert paths == sorted(paths) and len(paths) == len(set(paths))


def _assert_durable_gate(
    runtime: Path,
    snapshots: dict[str, dict[str, object]],
    receipts: dict[str, object],
) -> None:
    registry_path = runtime / "data" / "project-registry.sqlite3"
    work_path = runtime / "data" / "workspace-work.sqlite3"
    execution_path = runtime / "data" / "workspace-execution.sqlite3"

    projects = _rows(
        registry_path,
        "SELECT project_id,slug FROM projects ORDER BY slug",
    )
    assert {row["slug"] for row in projects} == set(SOURCES)
    assert len(projects) == len({row["project_id"] for row in projects}) == 3
    project_by_slug = {row["slug"]: row for row in projects}

    locations = _rows(
        registry_path,
        "SELECT project_id,repo_location_id,canonical_path FROM repo_locations",
    )
    workspaces = _rows(
        registry_path,
        "SELECT project_id,workspace_id,repo_location_id FROM workspaces",
    )
    assert len(locations) == len(workspaces) == 3
    assert len({row["workspace_id"] for row in workspaces}) == 3
    location_by_project = {row["project_id"]: row for row in locations}
    workspace_by_project = {row["project_id"]: row for row in workspaces}
    assert len(location_by_project) == len(workspace_by_project) == 3
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

    identities = _rows(
        execution_path,
        "SELECT identity_id,project_id,workspace_id FROM agent_identities",
    )
    preparations = _rows(
        execution_path,
        "SELECT project_id,workspace_id,work_item_id,preparation_id,identity_id,"
        "checkout_id,lease_id,attachment_id,state,generation "
        "FROM work_item_preparations",
    )
    checkouts = _rows(
        execution_path,
        "SELECT checkout_id,preparation_id,source_head,source_tree,internal_path,"
        "status FROM managed_checkouts",
    )
    leases = _rows(
        execution_path,
        "SELECT lease_id,checkout_id,identity_id,generation,status,claim_id "
        "FROM writer_leases",
    )
    assert len(identities) == len(preparations) == len(checkouts) == len(leases) == 3
    for rows, key in (
        (identities, "identity_id"),
        (preparations, "preparation_id"),
        (checkouts, "checkout_id"),
        (leases, "lease_id"),
    ):
        assert len({row[key] for row in rows}) == 3

    identity_by_id = {row["identity_id"]: row for row in identities}
    preparation_by_project = {row["project_id"]: row for row in preparations}
    checkout_by_id = {row["checkout_id"]: row for row in checkouts}
    lease_by_id = {row["lease_id"]: row for row in leases}
    assert all(len(value) == 3 for value in (
        identity_by_id, preparation_by_project, checkout_by_id, lease_by_id,
    ))

    claims = _rows(
        work_path,
        "SELECT claim_id,work_item_id,identity_id,generation,state "
        "FROM work_item_claims",
    )
    assert len(claims) == 1
    claim = claims[0]
    created = receipts["created_by_slug"]
    assert isinstance(created, dict)
    managed_root = runtime / "data" / "worktrees" / "managed-checkouts"
    db_checkout_paths = {Path(row["internal_path"]).resolve() for row in checkouts}
    assert {path.resolve() for path in managed_root.iterdir()} == db_checkout_paths

    for slug, snapshot in snapshots.items():
        project = project_by_slug[slug]
        project_id = project["project_id"]
        location = location_by_project[project_id]
        workspace = workspace_by_project[project_id]
        work = work_by_project[project_id]
        preparation = preparation_by_project[project_id]
        identity = identity_by_id[preparation["identity_id"]]
        checkout = checkout_by_id[preparation["checkout_id"]]
        lease = lease_by_id[preparation["lease_id"]]
        receipt = created[slug]
        assert isinstance(receipt, dict)

        assert location["canonical_path"] == str(snapshot["path"])
        assert workspace["repo_location_id"] == location["repo_location_id"]
        assert work["workspace_id"] == workspace["workspace_id"]
        assert preparation["workspace_id"] == workspace["workspace_id"]
        assert preparation["work_item_id"] == work["work_item_id"]
        assert identity["project_id"] == project_id
        assert identity["workspace_id"] == workspace["workspace_id"]
        assert checkout["preparation_id"] == preparation["preparation_id"]
        assert lease["checkout_id"] == checkout["checkout_id"]
        assert lease["identity_id"] == identity["identity_id"]
        assert lease["generation"] == preparation["generation"]
        assert checkout["status"] == "ready"

        assert receipt["preparation_work_item_id"] == work["work_item_id"]
        assert receipt["checkout_id"] == checkout["checkout_id"]
        assert receipt["lease_id"] == lease["lease_id"]
        assert receipt["identity_id"] == identity["identity_id"]
        assert receipt["generation"] == preparation["generation"]
        assert receipt["source_head"] == checkout["source_head"] == snapshot["head"]
        assert receipt["source_tree"] == checkout["source_tree"] == snapshot["tree"]

        checkout_path = Path(checkout["internal_path"]).resolve()
        assert checkout_path in db_checkout_paths
        assert checkout_path.is_relative_to(managed_root)
        assert checkout_path != snapshot["path"]
        assert _git_worktree_paths(snapshot["path"]) == {
            snapshot["path"], checkout_path,
        }
        checkout_status = _git(
            checkout_path, "status", "--porcelain", "--untracked-files=all",
        )
        if slug == "release-gate-happy":
            assert work["status"] == "completed"
            assert preparation["state"] == "detached"
            assert lease["status"] == "revoked"
            assert lease["claim_id"] == claim["claim_id"]
            assert claim["work_item_id"] == work["work_item_id"]
            assert claim["identity_id"] == identity["identity_id"]
            assert claim["generation"] == preparation["generation"]
            assert claim["state"] == "closed"
            assert checkout_status == f"?? {TARGET}"
            assert (checkout_path / TARGET).read_bytes() == TARGET_BYTES
        else:
            assert work["status"] == "unassigned"
            assert preparation["state"] == "prepared"
            assert preparation["attachment_id"] is None
            assert lease["status"] == "reserved"
            assert lease["claim_id"] is None
            assert checkout_status == ""

        _assert_source_unchanged(snapshot)


def test_local_web_release_gate(tmp_path: Path) -> None:
    _assert_product_identity()
    _codex, _herdr, npm, playwright = _require_prerequisites()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    receipts_path = tmp_path / "playwright-receipts.json"

    subprocess.run([npm, "run", "build"], cwd=WEB, check=True, env=_safe_environment())
    process, descriptor = _launch(runtime)
    base_url = descriptor["base_url"]
    assert isinstance(base_url, str)
    session = next_profile.ephemeral_session_for_root(runtime)
    snapshots: dict[str, dict[str, object]] = {}
    assert _herdr_sessions(base_url) == []
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
            "LOCAL_WEB_GATE_RECEIPTS": str(receipts_path),
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
        sessions = _herdr_sessions(base_url)
        assert len(sessions) == 1
        assert sessions[0]["name"] == session
        assert sessions[0]["status"] == "running"
    finally:
        try:
            if process.poll() is None:
                _cleanup_herdr_session(base_url, session)
        finally:
            _stop(process)

    next_profile.prepare_ephemeral_runtime_root(runtime)
    _assert_ephemeral_catalog(runtime)
    receipts = _load_receipts(receipts_path)
    _assert_durable_gate(runtime, snapshots, receipts)
