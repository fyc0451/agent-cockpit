from __future__ import annotations

import hashlib
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
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

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


def _archive_ignored(relative: Path) -> bool:
    parts = relative.parts
    return (
        relative.as_posix() in GATE_FILES
        or ".git" in parts
        or "__pycache__" in parts
        or ".pytest_cache" in parts
        or parts[:2] in {("web", "node_modules"), ("web", "dist")}
    )


def _archive_product_entries(root: Path) -> list[tuple[bytes, int, bytes]]:
    entries: list[tuple[bytes, int, bytes]] = []
    for current, directories, filenames in os.walk(
        root, topdown=True, followlinks=False,
    ):
        current_path = Path(current)
        retained: list[str] = []
        link_names: list[str] = []
        for name in sorted(directories):
            path = current_path / name
            relative = path.relative_to(root)
            if _archive_ignored(relative):
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                link_names.append(name)
                continue
            assert stat.S_ISDIR(info.st_mode), f"archive_product_special:{relative}"
            retained.append(name)
        directories[:] = retained

        for name in sorted([*filenames, *link_names]):
            path = current_path / name
            relative = path.relative_to(root)
            if _archive_ignored(relative):
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                mode = 0o120000
                payload = os.fsencode(os.readlink(path))
            else:
                assert stat.S_ISREG(info.st_mode), f"archive_product_special:{relative}"
                actual_mode = stat.S_IMODE(info.st_mode)
                assert actual_mode in {0o600, 0o644, 0o700, 0o755}, (
                    f"archive_product_file_mode:{relative}"
                )
                mode = 0o100755 if actual_mode & 0o111 else 0o100644
                payload = path.read_bytes()
            entries.append((os.fsencode(relative.as_posix()), mode, payload))
    return sorted(entries, key=lambda entry: entry[0])


def _archive_product_tree(root: Path) -> str:
    temporary = tempfile.mkdtemp(prefix="local-web-product-tree-")
    os.chmod(temporary, 0o700)
    try:
        git_dir = Path(temporary) / "objects.git"
        index = Path(temporary) / "index"
        environment = {
            key: value for key, value in _safe_environment().items()
            if not key.startswith("GIT_")
        }
        subprocess.run(
            [
                "git", "init", "--bare", "--quiet", "--object-format=sha1",
                "--template=", str(git_dir),
            ],
            check=True,
            env=environment,
        )
        index_payload = bytearray()
        for path, mode, payload in _archive_product_entries(root):
            header = f"blob {len(payload)}\0".encode("ascii")
            blob = hashlib.sha1(
                header + payload, usedforsecurity=False,
            ).hexdigest()
            index_payload.extend(f"{mode:o} {blob}\t".encode("ascii"))
            index_payload.extend(path)
            index_payload.append(0)
        index_environment = {
            **environment,
            "GIT_INDEX_FILE": str(index),
        }
        subprocess.run(
            [
                "git", "--git-dir", str(git_dir), "update-index",
                "--info-only", "-z", "--index-info",
            ],
            input=bytes(index_payload),
            check=True,
            env=index_environment,
        )
        result = subprocess.run(
            [
                "git", "--git-dir", str(git_dir), "write-tree", "--missing-ok",
            ],
            check=True,
            stdout=subprocess.PIPE,
            env=index_environment,
        ).stdout.decode("ascii").strip()
        assert len(result) == 40 and all(character in "0123456789abcdef" for character in result)
        return result
    finally:
        shutil.rmtree(temporary)


def _assert_product_identity(root: Path = ROOT) -> None:
    assert len(ACCEPTED_PRODUCT_EXACT) == 40
    assert len(ACCEPTED_PRODUCT_TREE) == 40
    if not (root / ".git").exists():
        assert _archive_product_tree(root) == ACCEPTED_PRODUCT_TREE
        return

    assert _git(root, "cat-file", "-t", ACCEPTED_PRODUCT_EXACT) == "commit"
    assert _git(root, "rev-parse", f"{ACCEPTED_PRODUCT_EXACT}^{{tree}}") == (
        ACCEPTED_PRODUCT_TREE
    )
    changed = set(filter(None, _git(
        root, "diff", "--name-only", ACCEPTED_PRODUCT_EXACT, "--",
    ).splitlines()))
    assert changed == GATE_FILES
    status_paths = {
        line.split(maxsplit=1)[1] for line in _git(
            root, "status", "--porcelain", "--untracked-files=all",
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


def _send_termination(process: subprocess.Popen[str]) -> bool:
    if process.poll() is None:
        assert os.getpgid(process.pid) == process.pid
        os.killpg(process.pid, signal.SIGTERM)
        return True
    return False


def _stop(process: subprocess.Popen[str]) -> None:
    sent_term = _send_termination(process)
    try:
        process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
    assert process.returncode == 0 or (
        sent_term and process.returncode == -signal.SIGTERM
    )


def _execute_playwright_with_cleanup(
    process: subprocess.Popen[str],
    base_url: str,
    session: str,
    invoke,
    *,
    stop_server=None,
) -> subprocess.CompletedProcess[str]:
    stop_server = _stop if stop_server is None else stop_server
    try:
        completed = invoke()
        if completed.stdout:
            print(completed.stdout)
        assert completed.returncode == 0, completed.stdout
        sessions = _herdr_sessions(base_url)
        assert len(sessions) == 1
        assert sessions[0]["name"] == session
        assert sessions[0]["status"] == "running"
        return completed
    finally:
        try:
            if process.poll() is None:
                _cleanup_herdr_session(base_url, session)
        finally:
            stop_server(process)


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


def _verify_finalized_runtime(runtime: Path) -> None:
    next_profile.prepare_ephemeral_runtime_root(runtime)
    _assert_ephemeral_catalog(runtime)


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


def _copy_archive_product(source: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    for relative_bytes, mode, payload in _archive_product_entries(source):
        relative = Path(os.fsdecode(relative_bytes))
        target = destination / relative
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if mode == 0o120000:
            target.symlink_to(os.fsdecode(payload))
        else:
            target.write_bytes(payload)
            target.chmod(0o700 if mode == 0o100755 else 0o600)


def test_archive_product_tree_behavior(tmp_path: Path, monkeypatch) -> None:
    assert _archive_product_tree(ROOT) == ACCEPTED_PRODUCT_TREE
    archive = tmp_path / "archive"
    _copy_archive_product(ROOT, archive)
    _assert_product_identity(archive)

    target = archive / "README.md"
    original = target.read_bytes()
    original_mode = stat.S_IMODE(target.stat().st_mode)

    target.write_bytes(original + b"tampered\n")
    with pytest.raises(AssertionError):
        _assert_product_identity(archive)
    target.write_bytes(original)

    target.chmod(0o700 if original_mode in {0o600, 0o644} else 0o600)
    with pytest.raises(AssertionError):
        _assert_product_identity(archive)
    target.chmod(original_mode)

    extra = archive / "UNEXPECTED_PRODUCT_FILE"
    extra.write_bytes(b"unexpected\n")
    extra.chmod(0o644)
    with pytest.raises(AssertionError):
        _assert_product_identity(archive)
    extra.unlink()

    target.unlink()
    with pytest.raises(AssertionError):
        _assert_product_identity(archive)
    target.write_bytes(original)
    target.chmod(original_mode)

    target.unlink()
    target.symlink_to(".agent-memory-project")
    with pytest.raises(AssertionError):
        _assert_product_identity(archive)
    target.unlink()
    target.write_bytes(original)
    target.chmod(original_mode)

    monkeypatch.setattr(sys.modules[__name__], "ACCEPTED_PRODUCT_TREE", "0" * 40)
    with pytest.raises(AssertionError):
        _assert_product_identity(archive)


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
def test_playwright_failure_cleanup_behavior(monkeypatch, failure: str) -> None:
    session = "ephemeral-behavior-gate"
    sessions = [{"name": session, "status": "running"}]
    events: list[str] = []

    def fake_request_json(
        _base_url: str, path: str, method: str,
    ) -> tuple[int, object]:
        events.append(f"{method} {path}")
        if path == "/api/herdr/sessions" and method == "GET":
            return 200, {"sessions": [dict(item) for item in sessions]}
        if path.endswith("/stop") and method == "POST":
            assert sessions == [{"name": session, "status": "running"}]
            sessions[0]["status"] = "stopped"
            return 200, {"data": {}}
        if path == f"/api/herdr/session/{session}" and method == "DELETE":
            assert sessions == [{"name": session, "status": "stopped"}]
            sessions.clear()
            return 200, {"data": {}}
        raise AssertionError(f"unexpected fake API request: {method} {path}")

    class FakeProcess:
        def poll(self):
            return None

    def stop_server(_process) -> None:
        assert sessions == []
        events.append("server stop")

    def invoke() -> subprocess.CompletedProcess[str]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(["playwright"], 600)
        return subprocess.CompletedProcess(
            ["playwright"], returncode=1, stdout="expected browser failure",
        )

    monkeypatch.setattr(sys.modules[__name__], "_request_json", fake_request_json)
    expected = subprocess.TimeoutExpired if failure == "timeout" else AssertionError
    with pytest.raises(expected):
        _execute_playwright_with_cleanup(
            FakeProcess(), "http://127.0.0.1:1", session, invoke,
            stop_server=stop_server,
        )
    assert sessions == []
    assert events == [
        "GET /api/herdr/sessions",
        f"POST /api/herdr/session/{session}/stop",
        "GET /api/herdr/sessions",
        f"DELETE /api/herdr/session/{session}",
        "GET /api/herdr/sessions",
        "server stop",
    ]


def test_stop_signal_behavior(monkeypatch) -> None:
    events: list[str] = []

    class FakeProcess:
        pid = 12345

        def __init__(self, returncode=None):
            self.returncode = returncode

        def poll(self):
            return self.returncode

        def communicate(self, timeout: int):
            events.append(f"communicate {timeout}")
            if self.returncode is None:
                self.returncode = -signal.SIGTERM
            return "", ""

    monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    process = FakeProcess()

    def successful_killpg(pid: int, sent_signal: int) -> None:
        assert pid == process.pid and sent_signal == signal.SIGTERM
        events.append("killpg success")

    monkeypatch.setattr(os, "killpg", successful_killpg)
    _stop(process)
    assert events == ["killpg success", "communicate 15"]

    events.clear()
    process = FakeProcess()

    def failed_killpg(pid: int, sent_signal: int) -> None:
        process.returncode = -signal.SIGTERM
        events.append("killpg failed")
        raise PermissionError("expected killpg failure")

    monkeypatch.setattr(os, "killpg", failed_killpg)
    with pytest.raises(PermissionError, match="expected killpg failure"):
        _stop(process)
    assert events == ["killpg failed"]

    events.clear()
    process = FakeProcess(returncode=-signal.SIGTERM)
    with pytest.raises(AssertionError):
        _stop(process)
    assert events == ["communicate 15"]


def _finalized_probe_runtime(path: Path) -> None:
    path.mkdir(mode=0o700)
    assert next_profile.initialize_empty_ephemeral_runtime_root(path) is True
    next_profile.activate_ephemeral_runtime_root(path)
    probe = path / "probe"
    probe.write_bytes(b"catalog probe\n")
    probe.chmod(0o600)
    next_profile.finalize_ephemeral_runtime_root({
        next_profile.PROFILE_ENV: next_profile.EPHEMERAL_PROFILE,
        next_profile.EPHEMERAL_ROOT_ENV: str(path),
    })


def test_finalized_runtime_verifier_is_called(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    _finalized_probe_runtime(runtime)
    calls: list[str] = []
    original_prepare = next_profile.prepare_ephemeral_runtime_root
    original_catalog = _assert_ephemeral_catalog

    def prepare(path: Path) -> None:
        calls.append("prepare")
        original_prepare(path)

    def catalog(path: Path) -> None:
        calls.append("catalog")
        original_catalog(path)

    monkeypatch.setattr(next_profile, "prepare_ephemeral_runtime_root", prepare)
    monkeypatch.setattr(sys.modules[__name__], "_assert_ephemeral_catalog", catalog)
    _verify_finalized_runtime(runtime)
    assert calls == ["prepare", "catalog"]


@pytest.mark.parametrize("corruption", ["bytes", "extra", "symlink"])
def test_finalized_runtime_verifier_rejects_corruption(
    tmp_path: Path, corruption: str,
) -> None:
    runtime = tmp_path / "runtime"
    _finalized_probe_runtime(runtime)
    if corruption == "bytes":
        (runtime / "probe").write_bytes(b"changed\n")
    elif corruption == "extra":
        extra = runtime / "extra"
        extra.write_bytes(b"extra\n")
        extra.chmod(0o600)
    else:
        (runtime / "unexpected-link").symlink_to("probe")
    with pytest.raises(next_profile.NextProfileError):
        _verify_finalized_runtime(runtime)


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

    def invoke_playwright() -> subprocess.CompletedProcess[str]:
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
        return completed

    _execute_playwright_with_cleanup(
        process, base_url, session, invoke_playwright,
    )
    _verify_finalized_runtime(runtime)
    receipts = _load_receipts(receipts_path)
    _assert_durable_gate(runtime, snapshots, receipts)
