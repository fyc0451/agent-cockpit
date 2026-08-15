"""Checkpoint A 真实产品流测试：一个真实 ephemeral server 上的 Boss 保存主链。

对应 Wiki 37 §1/§2/§6：
- 临时 0700 runtime root + 随机非保留 loopback 端口（禁 8790/18790）；
- active Project/RepoLocation/Workspace 经真实 registry store 建立；
- POST body/acceptance/constraints -> 201，GET 精确回读同一聚合；
- 丢弃首个 201 后同 Idempotency-Key 重试只落 1 thread/1 message/1 work_item/1 receipt；
- 同 key 改 body = 409；400/404/409 校验失败零写入；写故障 503 零半条且旧状态可读。
"""
from __future__ import annotations

import json
import os
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent_cockpit import project_registry_store as registry_store
from agent_cockpit import workspace_work_store as work_store


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "next_ephemeral_server.py"
RESERVED_PORTS = {8790, 18790}
SOURCE_SHA = "a" * 40


class _Server:
    def __init__(self, process, base_url: str, runtime_root: Path) -> None:
        self.process = process
        self.base_url = base_url
        self.runtime_root = runtime_root

    def url(self, path: str) -> str:
        return self.base_url + path

    def work_items(self, project_id: str, workspace_id: str) -> str:
        return self.url(
            f"/api/projects/{project_id}/workspaces/{workspace_id}/work-items",
        )


def _stop(process: subprocess.Popen[str]) -> str:
    stdout = ""
    try:
        if process.poll() is None:
            process_group = os.getpgid(process.pid)
            if process_group == process.pid:
                os.killpg(process_group, signal.SIGTERM)
        try:
            stdout, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process_group = os.getpgid(process.pid)
            if process_group == process.pid:
                os.killpg(process_group, signal.SIGKILL)
            stdout, _ = process.communicate(timeout=5)
        return stdout
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.communicate(timeout=5)


def _wait_ready(base_url: str, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    last = "not_attempted"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/health/ephemeral", timeout=1) as r:
                payload = json.loads(r.read())
            if payload.get("ready") is True:
                return payload
            last = "ready_false"
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last = type(exc).__name__
        time.sleep(0.05)
    raise AssertionError(f"ephemeral server not ready: {last}")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    runtime_root = tmp_path_factory.mktemp("checkpoint-a-product-flow")
    runtime_root.chmod(0o700)
    previous_umask = os.umask(0o077)
    process = subprocess.Popen(
        [
            sys.executable,
            str(LAUNCHER),
            "--runtime-root",
            str(runtime_root),
            "--source-sha",
            SOURCE_SHA,
        ],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        readable, _, _ = select.select([process.stdout], [], [], 15)
        assert readable, "ephemeral launcher produced no descriptor"
        descriptor = json.loads(process.stdout.readline())
        base_url = descriptor["base_url"]
        assert base_url.startswith("http://127.0.0.1:")
        port = int(base_url.rsplit(":", 1)[1])
        assert port not in RESERVED_PORTS, "live product flow must not touch 8790/18790"
        assert stat.S_IMODE(runtime_root.stat().st_mode) == 0o700
        _wait_ready(base_url)
        yield _Server(process, base_url, runtime_root)
    finally:
        os.umask(previous_umask)
        assert _stop(process) == ""


def _request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body: object | None = None,
) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            **(headers or {}),
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _seed_scope(
    server: _Server, slug: str, *, lifecycle: str = "active",
) -> tuple[str, str]:
    registry = registry_store.initialize(
        server.runtime_root / "data" / "project-registry.sqlite3",
    )
    try:
        project = registry.create_project(slug=slug, display_name=slug, goal=None)
        location = registry.add_repo_location(
            project_id=project.project_id,
            node_id="local",
            canonical_path=f"/repo/{slug}",
            vcs_kind="none",
            availability="available",
        )
        workspace = registry.create_workspace(
            project_id=project.project_id,
            repo_location_id=location.repo_location_id,
            name="main",
            goal=None,
            isolation_kind="shared",
        )
        if lifecycle != "active":
            with sqlite3.connect(registry.path) as connection:
                connection.execute(
                    "UPDATE workspaces SET lifecycle=? WHERE workspace_id=?",
                    (lifecycle, workspace.workspace_id),
                )
                connection.commit()
        return project.project_id, workspace.workspace_id
    finally:
        registry.close()


def _work_db(server: _Server) -> Path:
    return server.runtime_root / "data" / "workspace-work.sqlite3"


def _domain_counts(server: _Server) -> dict[str, int]:
    path = _work_db(server)
    if not path.exists():
        return {table: 0 for table in work_store.DOMAIN_TABLES}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return {
            table: int(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
            )
            for table in work_store.DOMAIN_TABLES
        }


def _assert_exact_aggregate(
    item: dict, *, project_id: str, workspace_id: str,
    body: str, acceptance: str | None, constraints: str | None,
) -> None:
    assert set(item) == {"thread", "root_message", "work_item"}
    thread, root_message, work_item = item["thread"], item["root_message"], item["work_item"]
    assert set(thread) == {
        "thread_id", "project_id", "workspace_id", "revision", "created_at",
    }
    assert thread["thread_id"].startswith("thr_")
    assert thread["project_id"] == project_id
    assert thread["workspace_id"] == workspace_id
    assert thread["revision"] == 1
    assert isinstance(thread["created_at"], str) and thread["created_at"]
    assert set(root_message) == {
        "message_id", "thread_id", "author_kind", "author_ref", "body",
    }
    assert root_message["message_id"].startswith("msg_")
    assert root_message["thread_id"] == thread["thread_id"]
    assert root_message["author_kind"] == "boss"
    assert root_message["author_ref"] is None
    assert root_message["body"] == body
    assert set(work_item) == {
        "work_item_id", "source_message_id", "status", "acceptance", "constraints",
    }
    assert work_item["work_item_id"].startswith("wrk_")
    assert work_item["source_message_id"] == root_message["message_id"]
    assert work_item["status"] == "unassigned"
    assert work_item["acceptance"] == acceptance
    assert work_item["constraints"] == constraints


def _assert_g3_success(payload: dict, status: int) -> dict:
    assert set(payload) == {"data", "meta"}
    assert payload["meta"]["request_id"].startswith("req_")
    assert payload["meta"]["partial"] is False
    return payload["data"]


def _assert_error(payload: dict, status: int, code: str, *, retryable: bool) -> dict:
    assert set(payload) == {"error"}
    error = payload["error"]
    assert set(error) == {"code", "message", "retryable", "request_id", "details"}
    assert error["code"] == code
    assert error["retryable"] is retryable
    assert error["request_id"].startswith("req_")
    assert error["details"] == {}
    return error


BODY = "修复登录失败"
ACCEPTANCE = "刷新后仍保持登录"
CONSTRAINTS = "不要修改现有会话格式"


def test_save_then_get_returns_exact_aggregate_on_live_server(server) -> None:
    project_id, workspace_id = _seed_scope(server, "flow-alpha")
    url = server.work_items(project_id, workspace_id)
    status, payload = _request(
        "POST",
        url,
        headers={"Idempotency-Key": "flow-save-1"},
        body={"body": BODY, "acceptance": ACCEPTANCE, "constraints": CONSTRAINTS},
    )
    assert status == 201
    item = _assert_g3_success(payload, status)
    _assert_exact_aggregate(
        item,
        project_id=project_id,
        workspace_id=workspace_id,
        body=BODY,
        acceptance=ACCEPTANCE,
        constraints=CONSTRAINTS,
    )

    listed_status, listed = _request("GET", url)
    assert listed_status == 200
    assert _assert_g3_success(listed, listed_status) == {"items": [item], "next_cursor": None}
    assert _domain_counts(server) == {
        "message_threads": 1, "messages": 1, "work_items": 1, "idempotency_records": 1,
    }


def test_discarded_201_replay_same_key_and_conflict_on_changed_body(server) -> None:
    project_id, workspace_id = _seed_scope(server, "flow-beta")
    url = server.work_items(project_id, workspace_id)
    body = {"body": BODY, "acceptance": None, "constraints": None}

    status, first_payload = _request(
        "POST", url, headers={"Idempotency-Key": "intent-beta"}, body=body,
    )
    assert status == 201
    first = first_payload["data"]
    # 模拟客户端丢弃首个 201（如响应在网络中丢失）后原样重试。
    status, replay_payload = _request(
        "POST", url, headers={"Idempotency-Key": "intent-beta"}, body=body,
    )
    assert status == 201
    assert replay_payload["data"] == first
    assert _domain_counts(server) == {
        "message_threads": 2, "messages": 2, "work_items": 2, "idempotency_records": 2,
    }

    status, conflict = _request(
        "POST",
        url,
        headers={"Idempotency-Key": "intent-beta"},
        body={**body, "body": "换了问题"},
    )
    _assert_error(conflict, status, "idempotency_conflict", retryable=False)
    assert _domain_counts(server) == {
        "message_threads": 2, "messages": 2, "work_items": 2, "idempotency_records": 2,
    }
    listed_status, listed = _request("GET", url)
    assert listed_status == 200
    assert listed["data"] == {"items": [first], "next_cursor": None}


def test_validation_and_scope_failures_leave_zero_domain_rows(server) -> None:
    project_id, workspace_id = _seed_scope(server, "flow-gamma")
    url = server.work_items(project_id, workspace_id)
    before = _domain_counts(server)

    missing_key = _request(
        "POST", url, body={"body": BODY, "acceptance": None, "constraints": None},
    )
    _assert_error(missing_key[1], missing_key[0], "idempotency_key_required", retryable=False)
    for bad_body in (
        {"body": "   ", "acceptance": None, "constraints": None},
        {"body": BODY, "acceptance": None, "constraints": None, "agent": "codex"},
    ):
        rejected = _request(
            "POST", url, headers={"Idempotency-Key": "invalid-gamma"}, body=bad_body,
        )
        _assert_error(rejected[1], rejected[0], "invalid_argument", retryable=False)

    unknown = _request(
        "POST",
        server.work_items("prj_" + "0" * 32, workspace_id),
        headers={"Idempotency-Key": "unknown-project"},
        body={"body": BODY, "acceptance": None, "constraints": None},
    )
    _assert_error(unknown[1], unknown[0], "project_not_found", retryable=False)

    archived_project, archived_workspace = _seed_scope(
        server, "flow-archived", lifecycle="archived",
    )
    archived = _request(
        "POST",
        server.work_items(archived_project, archived_workspace),
        headers={"Idempotency-Key": "archived-scope"},
        body={"body": BODY, "acceptance": None, "constraints": None},
    )
    _assert_error(archived[1], archived[0], "workspace_not_active", retryable=False)

    assert _domain_counts(server) == before
    listed_status, listed = _request("GET", url)
    assert listed_status == 200
    assert listed["data"] == {"items": [], "next_cursor": None}


def test_store_write_fault_returns_503_zero_half_and_keeps_saved_work(server) -> None:
    project_id, workspace_id = _seed_scope(server, "flow-delta")
    url = server.work_items(project_id, workspace_id)
    body = {"body": "已保存的基线工作", "acceptance": None, "constraints": None}
    status, baseline_payload = _request(
        "POST", url, headers={"Idempotency-Key": "delta-baseline"}, body=body,
    )
    assert status == 201
    baseline = baseline_payload["data"]
    assert _domain_counts(server) == {
        "message_threads": 3, "messages": 3, "work_items": 3, "idempotency_records": 3,
    }

    data_dir = server.runtime_root / "data"
    original_mode = stat.S_IMODE(data_dir.stat().st_mode)
    data_dir.chmod(0o500)
    try:
        status, fault = _request(
            "POST",
            url,
            headers={"Idempotency-Key": "delta-fault"},
            body={"body": "写故障期间的半条", "acceptance": None, "constraints": None},
        )
        assert status == 503
        assert fault["error"]["code"] == "store_write_failed"
        assert fault["error"]["retryable"] is True
        assert fault["error"]["details"] == {}
    finally:
        data_dir.chmod(original_mode)

    assert _domain_counts(server) == {
        "message_threads": 3, "messages": 3, "work_items": 3, "idempotency_records": 3,
    }
    listed_status, listed = _request("GET", url)
    assert listed_status == 200
    assert listed["data"] == {"items": [baseline], "next_cursor": None}

    status, recovered = _request(
        "POST",
        url,
        headers={"Idempotency-Key": "delta-recovered"},
        body={"body": "故障恢复后的新工作", "acceptance": None, "constraints": None},
    )
    assert status == 201
    assert _domain_counts(server) == {
        "message_threads": 4, "messages": 4, "work_items": 4, "idempotency_records": 4,
    }
