import subprocess
import shutil
import threading
from pathlib import Path

from fastapi.testclient import TestClient

import server


def _init_git_repo(path):
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


def test_build_attention_combines_panes_tasks_and_optional_mail(monkeypatch):
    snapshot = {
        "available": True,
        "panes": [
            {
                "session": "demo",
                "pane_id": "w1:p2",
                "agent": "codex",
                "agent_status": "blocked",
                "cwd_name": "project",
            },
            {
                "session": "demo",
                "pane_id": "w1:p3",
                "agent": "kimi",
                "agent_status": "working",
            },
        ],
    }
    monkeypatch.setattr(
        server.tasks,
        "list_tasks",
        lambda limit=50: [
            {
                "id": "failed1",
                "status": "failed",
                "prompt": "fix failing tests",
                "workdir": "/tmp/project",
                "run_workdir": "/tmp/worktree-failed",
                "created_ts": 20,
            },
            {
                "id": "review1",
                "status": "done",
                "prompt": "add feature",
                "workdir": "/tmp/project",
                "run_workdir": "/tmp/worktree-review",
                "preview_hash": None,
                "created_ts": 10,
            },
            {
                "id": "applied1",
                "status": "done",
                "prompt": "already applied",
                "workdir": "/tmp/project",
                "run_workdir": None,
                "created_ts": 30,
            },
        ],
    )
    monkeypatch.setattr(
        server.db,
        "status",
        lambda: {"available": True, "reason": None},
    )
    monkeypatch.setattr(
        server.hub_client,
        "status",
        lambda: {"available": True, "reason": None},
    )
    monkeypatch.setattr(
        server.db,
        "global_unread_count",
        lambda: 1,
    )

    result = server._build_attention(snapshot)

    by_kind = {item["kind"]: item for item in result["items"]}
    assert result["count"] == 3
    assert result["mail_unread"] == 1
    assert set(by_kind) == {"pane_blocked", "task_failed", "task_review"}
    assert by_kind["pane_blocked"]["url"] == "/#/attention/pane/demo/w1%3Ap2"
    assert by_kind["task_failed"]["url"] == "/#/attention/task/failed1"
    assert by_kind["task_review"]["target"]["task_id"] == "review1"
    assert result["capabilities"]["agent_mail"]["available"] is True


def test_build_attention_degrades_when_agent_mail_is_missing(monkeypatch):
    monkeypatch.setattr(server.tasks, "list_tasks", lambda limit=50: [])
    monkeypatch.setattr(
        server.db,
        "status",
        lambda: {"available": False, "reason": "未安装 Agent Mail"},
    )
    monkeypatch.setattr(
        server.db,
        "global_unread_count",
        lambda: (_ for _ in ()).throw(AssertionError("must not query mail DB")),
    )

    result = server._build_attention({"available": True, "panes": []})

    assert result["items"] == []
    assert result["capabilities"]["agent_mail"] == {
        "available": False,
        "reason": "未安装 Agent Mail",
        "read_available": False,
        "write_available": False,
        "write_reason": "未安装 Agent Mail",
    }


def test_attention_change_detection_skips_initial_snapshot_and_notifies_reentry():
    blocked = {"id": "pane:demo:w1:p2"}

    current, new = server._attention_changes(None, [blocked])
    assert current == {blocked["id"]}
    assert new == []

    current, new = server._attention_changes(current, [blocked, {"id": "task:abc:failed"}])
    assert [item["id"] for item in new] == ["task:abc:failed"]

    current, new = server._attention_changes(current, [])
    assert current == set()
    assert new == []

    _, new = server._attention_changes(current, [blocked])
    assert new == [blocked]


def test_overview_returns_degraded_payload_instead_of_500(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.db,
        "status",
        lambda: {"available": False, "reason": "数据库不存在"},
    )
    monkeypatch.setattr(
        server.db,
        "overview",
        lambda: (_ for _ in ()).throw(AssertionError("must not query missing DB")),
    )

    response = TestClient(server.app).get(
        "/api/overview", headers={"authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "projects": [],
        "total_unread": 0,
        "total_projects": 0,
        "total_agents": 0,
        "agent_mail": {
            "available": False,
            "reason": "数据库不存在",
            "read_available": False,
            "write_available": False,
            "write_reason": "数据库不存在",
        },
    }


def test_overview_degrades_when_agent_mail_query_breaks(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.db, "status", lambda: {"available": True, "reason": None}
    )
    monkeypatch.setattr(
        server.hub_client, "status", lambda: {"available": True, "reason": None}
    )
    monkeypatch.setattr(
        server.db,
        "overview",
        lambda: (_ for _ in ()).throw(RuntimeError("schema mismatch")),
    )

    response = TestClient(server.app).get(
        "/api/overview", headers={"authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    assert response.json()["projects"] == []
    assert response.json()["agent_mail"]["available"] is False
    assert "查询失败" in response.json()["agent_mail"]["reason"]


def test_identity_lookup_does_not_fabricate_without_agent_mail(monkeypatch):
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda *_: (_ for _ in ()).throw(RuntimeError("missing DB")),
    )

    assert server._identity_name("/project", "codex") is None


def test_attention_keeps_mail_readable_when_hub_is_down(monkeypatch):
    monkeypatch.setattr(server.tasks, "list_tasks", lambda limit=50: [])
    monkeypatch.setattr(
        server.db, "status", lambda: {"available": True, "reason": None}
    )
    monkeypatch.setattr(server.db, "global_unread_count", lambda: 0)
    monkeypatch.setattr(
        server.hub_client,
        "status",
        lambda: {"available": False, "reason": "Hub 不可连接"},
    )

    result = server._build_attention({"available": True, "panes": []})

    assert result["capabilities"]["agent_mail"] == {
        "available": True,
        "reason": None,
        "read_available": True,
        "write_available": False,
        "write_reason": "Hub 不可连接",
    }


def test_setup_workspace_succeeds_without_agent_mail(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(server, "AGENT_MAIL_INIT_SCRIPT", tmp_path / "missing")
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running"}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda *args, **kwargs: {"available": True, "pane_id": "w1:p2"},
    )
    monkeypatch.setattr(
        server.herdr_client,
        "snapshot",
        lambda: {
            "sessions": [
                {
                    "session": "demo",
                    "panes": [
                        {"pane_id": "w1:p2", "agent": "codex", "cwd": str(tmp_path)}
                    ],
                }
            ]
        },
    )
    sent = []
    monkeypatch.setattr(server.herdr_client, "pane_send", lambda *args: sent.append(args))
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)

    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={"session": "demo", "workdir": str(tmp_path), "agents": ["codex"]},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["registered"] is False
    assert response.json()["agent_mail"]["available"] is False
    assert response.json()["notified"] == []
    assert sent == []


def test_setup_workspace_restarts_stopped_session(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(server.herdr_client, "onboarding_required", lambda: False)
    monkeypatch.setattr(server, "AGENT_MAIL_INIT_SCRIPT", tmp_path / "missing")
    session_states = iter([
        [{"name": "demo", "status": "stopped"}],
        [{"name": "demo", "status": "running"}],
    ])
    monkeypatch.setattr(server.herdr_client, "list_sessions", lambda: next(session_states))
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda *args, **kwargs: {"available": True, "pane_id": "w1:p2"},
    )
    monkeypatch.setattr(server.herdr_client, "snapshot", lambda: {"sessions": []})
    created = []
    writes = []
    monkeypatch.setattr(
        server.terminal,
        "create_term",
        lambda cwd: created.append(cwd) or {"id": "term1"},
    )
    monkeypatch.setattr(
        server.terminal,
        "write_term",
        lambda term_id, data: writes.append((term_id, data)),
    )
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)

    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={"session": "demo", "workdir": str(tmp_path), "agents": ["codex"]},
    )

    body = response.json()
    assert body["ok"] is True
    assert body["session_created"] is False
    assert body["session_started"] is True
    assert body["started"] == ["codex"]
    assert body["failed"] == []
    assert created == [str(tmp_path)]
    assert writes[0] == (
        "term1",
        f"{server.herdr_client.HERDR_BIN} --session demo\r",
    )
    assert writes[-1] == ("term1", "\x02d")


def test_setup_workspace_closes_initial_blank_pane(monkeypatch, tmp_path):
    """PTY 建 session 会留下 TUI 自带的空白 shell pane,agent 启动后应清掉它(且只清它)。"""
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(server.herdr_client, "onboarding_required", lambda: False)
    monkeypatch.setattr(server, "AGENT_MAIL_INIT_SCRIPT", tmp_path / "missing")
    session_states = iter([
        [],
        [{"name": "demo", "status": "running"}],
    ])
    monkeypatch.setattr(server.herdr_client, "list_sessions", lambda: next(session_states))
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda *args, **kwargs: {"available": True, "pane_id": "w1:p2"},
    )
    panes = [
        {"session": "demo", "pane_id": "w1:p1", "agent": None},
        {"session": "demo", "pane_id": "w1:p2", "agent": "codex"},
    ]
    monkeypatch.setattr(server.herdr_client, "snapshot", lambda: {"panes": panes})
    closed = []
    monkeypatch.setattr(
        server.herdr_client,
        "close_pane",
        lambda session, pane_id: closed.append(pane_id) or {"available": True},
    )
    monkeypatch.setattr(server.terminal, "create_term", lambda cwd: {"id": "term1"})
    monkeypatch.setattr(server.terminal, "write_term", lambda *_: None)
    monkeypatch.setattr(server, "_start_pty_drainer", lambda term_id, output: (None, None))
    monkeypatch.setattr(server, "_stop_pty_drainer", lambda *_: None)
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)

    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={"session": "demo", "workdir": str(tmp_path), "agents": ["codex"]},
    )

    body = response.json()
    assert body["ok"] is True
    assert closed == ["w1:p1"]  # agent pane w1:p2 不被误关
    assert body["closed_panes"] == ["w1:p1"]


def test_setup_workspace_timeout_reports_herdr_state_and_cleans_terminal(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(server.herdr_client, "onboarding_required", lambda: False)
    monkeypatch.setattr(server.herdr_client, "list_sessions", lambda: [])
    monkeypatch.setattr(server.terminal, "create_term", lambda cwd: {"id": "term1"})
    monkeypatch.setattr(server.terminal, "write_term", lambda *_: None)
    monkeypatch.setattr(
        server,
        "_start_pty_drainer",
        lambda term_id, output: (output.extend(b"\x1b[31mfatal startup\x1b[0m\r\n") or (None, None)),
    )
    killed = []
    monkeypatch.setattr(server.terminal, "kill_term", lambda term_id: killed.append(term_id))

    class FakeTime:
        values = iter([0.0, server.SESSION_START_TIMEOUT + 1])

        @classmethod
        def monotonic(cls):
            return next(cls.values)

        @staticmethod
        def sleep(_):
            pass

    monkeypatch.setattr(server, "time", FakeTime)

    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={"session": "demo", "workdir": str(tmp_path), "agents": ["codex"]},
    )

    body = response.json()
    assert body["ok"] is False
    assert body["started"] == []
    assert "启动 session 超时(20秒): demo" in body["error"]
    assert "herdr 状态: 未出现在 session 列表" in body["error"]
    assert body["terminal_output"] == "fatal startup"
    assert killed == ["term1"]


def test_hidden_pty_drainer_continuously_reads_and_bounds_output(monkeypatch):
    drained = threading.Event()
    chunks = iter([
        b"x" * (server.SESSION_BOOTSTRAP_OUTPUT_LIMIT + 100),
        b"\x1b[31mfatal\x1b[0m\r\n",
        b"",
    ])

    def read_output(term_id, timeout):
        assert term_id == "term1"
        try:
            data = next(chunks)
        except StopIteration:
            return b""
        if b"fatal" in data:
            drained.set()
        return data

    monkeypatch.setattr(server.terminal, "read_output", read_output)
    output = bytearray()
    stop, thread = server._start_pty_drainer("term1", output)

    assert drained.wait(1)
    server._stop_pty_drainer(stop, thread)

    assert len(output) <= server.SESSION_BOOTSTRAP_OUTPUT_LIMIT
    assert server._pty_output_tail(output).endswith("fatal")


def test_setup_workspace_rejects_missing_herdr_before_creating_terminal(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: False)
    monkeypatch.setattr(server.herdr_client, "HERDR_BIN", "/missing/herdr")
    monkeypatch.setattr(
        server.terminal,
        "create_term",
        lambda *_: (_ for _ in ()).throw(AssertionError("不应创建终端")),
    )

    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={"session": "demo", "workdir": str(tmp_path), "agents": ["codex"]},
    )

    assert response.json() == {
        "ok": False,
        "error": "herdr 未安装或不可执行: /missing/herdr",
        "session": "demo",
        "session_started": False,
        "started": [],
    }


def test_setup_workspace_opens_visible_terminal_for_herdr_onboarding(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(server.herdr_client, "HERDR_BIN", "/opt/herdr")
    monkeypatch.setattr(server.herdr_client, "list_sessions", lambda: [])
    monkeypatch.setattr(server.herdr_client, "onboarding_required", lambda: True)
    monkeypatch.setattr(
        server.terminal,
        "create_term",
        lambda *_: (_ for _ in ()).throw(AssertionError("后台不应启动 onboarding")),
    )

    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={"session": "demo", "workdir": str(tmp_path), "agents": ["codex"]},
    )

    assert response.json() == {
        "ok": False,
        "code": "herdr_onboarding_required",
        "error": (
            "Herdr 首次配置尚未完成。请打开终端完成配置向导，"
            "再按 Ctrl-b d 脱离并重新启动工作区"
        ),
        "herdr_command": "/opt/herdr --session demo",
        "session": "demo",
        "session_created": True,
        "session_started": False,
        "started": [],
    }


def test_setup_workspace_returns_per_agent_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(server, "AGENT_MAIL_INIT_SCRIPT", tmp_path / "missing")
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running"}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda session, workdir, agent, **kwargs: (
            {"available": True, "pane_id": "w1:p2"}
            if agent == "codex"
            else {"available": True, "error": "opencode 启动失败"}
        ),
    )
    monkeypatch.setattr(server.herdr_client, "snapshot", lambda: {"sessions": []})
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)

    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={
            "session": "demo",
            "workdir": str(tmp_path),
            "agents": ["codex", "opencode"],
        },
    )

    body = response.json()
    assert body["ok"] is False
    assert body["started"] == ["codex"]
    assert body["failed"] == [
        {"agent": "opencode", "error": "opencode 启动失败"}
    ]


def test_prepare_parallel_workspace_creates_isolated_and_review_worktrees(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git_repo(repo)
    req = server.SetupWorkspaceReq(
        session="demo",
        workdir=str(repo),
        mode="parallel",
        participants=[
            {"id": "lead", "agent": "codex", "role": "lead", "task": "后端"},
            {"id": "dev", "agent": "kimi", "role": "developer", "task": "前端"},
            {
                "id": "review", "agent": "claude", "role": "reviewer",
                "task": "复核", "review_target": "lead",
            },
        ],
    )

    plans, warnings = server._prepare_workspace(req)

    assert warnings == []
    assert [plan["strategy"] for plan in plans] == ["isolated", "isolated", "review"]
    assert plans[0]["branch"] == "agent-cockpit/demo/1-codex"
    assert plans[1]["branch"] == "agent-cockpit/demo/2-kimi"
    assert plans[2]["branch"] is None
    assert all((Path(plan["workdir"]) / "README.md").is_file() for plan in plans)
    detached = subprocess.run(
        ["git", "-C", plans[2]["workdir"], "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
    )
    assert detached.returncode == 1


def test_prepare_parallel_workspace_rejects_non_git_directory(tmp_path):
    req = server.SetupWorkspaceReq(
        session="demo",
        workdir=str(tmp_path),
        mode="parallel",
        participants=[
            {"id": "one", "agent": "codex", "role": "lead", "task": "后端"},
            {"id": "two", "agent": "kimi", "role": "developer", "task": "前端"},
        ],
    )

    try:
        server._prepare_workspace(req)
    except server.HTTPException as exc:
        assert exc.status_code == 400
        assert "不是 Git 仓库" in exc.detail
    else:
        raise AssertionError("非 Git 目录不应启动并行写入者")


def test_prepare_workspace_rejects_blank_participant_task(tmp_path):
    req = server.SetupWorkspaceReq(
        session="demo",
        workdir=str(tmp_path),
        mode="custom",
        participants=[
            {"id": "lead", "agent": "codex", "role": "lead", "task": "   "},
        ],
    )

    try:
        server._prepare_workspace(req)
    except server.HTTPException as exc:
        assert exc.status_code == 400
        assert "真实任务" in exc.detail
    else:
        raise AssertionError("协作工作区不应接受空白任务")


def test_setup_workspace_briefs_roles_without_agent_mail(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(server, "AGENT_MAIL_INIT_SCRIPT", tmp_path / "missing")
    monkeypatch.setattr(
        server.herdr_client, "list_sessions",
        lambda: [{"name": "demo", "status": "running"}],
    )
    monkeypatch.setattr(
        server.herdr_client, "start_agent",
        lambda session, workdir, agent, **kwargs: {
            "available": True, "pane_id": "w1:p2" if agent == "codex" else "w1:p3",
        },
    )
    monkeypatch.setattr(server.herdr_client, "snapshot", lambda: {"sessions": []})
    sent = []
    monkeypatch.setattr(
        server.herdr_client, "pane_send",
        lambda session, pane, text, mode: sent.append((pane, text, mode)) or {"available": True},
    )
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)

    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={
            "session": "demo",
            "workdir": str(tmp_path),
            "mode": "develop_review",
            "participants": [
                {"id": "lead", "agent": "codex", "role": "lead", "task": "实现"},
                {
                    "id": "review", "agent": "kimi", "role": "reviewer",
                    "task": "复核", "review_target": "lead",
                },
            ],
        },
    )

    body = response.json()
    assert body["ok"] is True
    assert body["briefed"] == ["codex", "kimi"]
    assert body["warnings"] == ["当前不是 Git 仓库，所有 Agent 使用原工作目录"]
    assert "总目标" not in sent[0][1]
    assert "你的任务: 实现" in sent[0][1]
    assert "你的角色: Reviewer" in sent[1][1]
    assert "共享工作目录" in sent[1][1]
    assert "detached worktree" not in sent[1][1]


def test_setup_workspace_reuses_matching_agent_without_reinjecting(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(tmp_path)}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda *args, **kwargs: {
            "available": True,
            "pane_id": "w1:p2",
            "agent": "codex",
            "cwd": str(tmp_path),
            "reused": True,
        },
    )
    script = tmp_path / "am-init-project"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(server, "AGENT_MAIL_INIT_SCRIPT", script)
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "ok", ""),
    )
    monkeypatch.setattr(
        server.herdr_client,
        "snapshot",
        lambda: {
            "sessions": [{
                "session": "demo",
                "panes": [{"pane_id": "w1:p2", "agent": "codex"}],
            }],
        },
    )
    monkeypatch.setattr(server, "_identity_name", lambda *_: "codex-main")
    sent = []
    monkeypatch.setattr(
        server.herdr_client,
        "pane_send",
        lambda *args: sent.append(args) or {"available": True},
    )
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)

    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={
            "session": "demo",
            "workdir": str(tmp_path),
            "mode": "custom",
            "participants": [
                {"id": "lead", "agent": "codex", "role": "lead", "task": "修复启动"},
            ],
        },
    )

    body = response.json()
    assert body["ok"] is True
    assert body["idempotent"] is True
    assert body["started"] == []
    assert body["reused"] == ["codex"]
    assert body["failed"] == []
    assert body["briefed"] == []
    assert body["notified"] == []
    assert sent == []


def test_setup_workspace_rejects_reused_agent_with_unknown_or_different_cwd(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(server, "AGENT_MAIL_INIT_SCRIPT", tmp_path / "missing")
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(tmp_path)}],
    )
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)
    client = TestClient(server.app)
    other = tmp_path / "other"
    other.mkdir()

    for existing_cwd in (None, str(other)):
        result = {
            "available": True,
            "pane_id": "w1:p2",
            "agent": "codex",
            "reused": True,
        }
        if existing_cwd:
            result["cwd"] = existing_cwd
        monkeypatch.setattr(server.herdr_client, "start_agent", lambda *_, **__: result)

        response = client.post(
            "/api/herdr/setup-workspace",
            headers={"authorization": "Bearer secret"},
            json={
                "session": "demo",
                "workdir": str(tmp_path),
                "mode": "custom",
                "participants": [{
                    "id": "lead", "agent": "codex", "role": "lead",
                    "task": "修复启动",
                }],
            },
        )

        body = response.json()
        assert body["ok"] is False
        assert body["idempotent"] is False
        assert body["started"] == []
        assert body["reused"] == []
        assert body["failed"] == [{
            "agent": "codex",
            "error": "session 中已存在 codex，无法应用新的工作目录",
        }]
        assert body["notified"] == []


def test_prepare_workspace_resumes_old_branch_and_warns_after_prune(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git_repo(repo)
    req = server.SetupWorkspaceReq(
        session="resume",
        workdir=str(repo),
        mode="custom",
        participants=[
            {
                "id": "lead", "agent": "codex", "role": "lead",
                "task": "继续任务", "workspace": "isolated",
            },
        ],
    )
    first, _ = server._prepare_workspace(req)
    old_branch_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", first[0]["branch"]],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    shutil.rmtree(first[0]["worktree"])
    (repo / "README.md").write_text("new main\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "advance main"],
        check=True, capture_output=True,
    )

    resumed, warnings = server._prepare_workspace(req)

    assert resumed[0]["resumed"] is True
    assert Path(resumed[0]["worktree"]).is_dir()
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", resumed[0]["branch"]],
        check=True, capture_output=True, text=True,
    ).stdout.strip() == old_branch_head
    assert any("恢复" in warning and "新 session" in warning for warning in warnings)


def test_prepare_workspace_warns_when_reusing_dirty_worktree(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git_repo(repo)
    req = server.SetupWorkspaceReq(
        session="dirty",
        workdir=str(repo),
        mode="custom",
        participants=[
            {
                "id": "lead", "agent": "codex", "role": "lead",
                "task": "复用工作区", "workspace": "isolated",
            },
        ],
    )
    first, _ = server._prepare_workspace(req)
    (Path(first[0]["workdir"]) / "local.txt").write_text("keep me\n", encoding="utf-8")

    reused, warnings = server._prepare_workspace(req)

    assert reused[0]["reused"] is True
    assert reused[0]["dirty"] is True
    assert any("未提交改动" in warning and "原样保留" in warning for warning in warnings)


def test_prepare_workspace_rolls_back_new_worktrees_after_partial_failure(monkeypatch, tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git_repo(repo)
    req = server.SetupWorkspaceReq(
        session="rollback",
        workdir=str(repo),
        mode="parallel",
        participants=[
            {"id": "one", "agent": "codex", "role": "lead", "task": "后端"},
            {"id": "two", "agent": "kimi", "role": "developer", "task": "前端"},
        ],
    )
    real_ensure = server._ensure_worktree

    def fail_second(*args, **kwargs):
        if kwargs.get("index", args[4] if len(args) > 4 else None) == 1:
            raise ValueError("second failed")
        return real_ensure(*args, **kwargs)

    monkeypatch.setattr(server, "_ensure_worktree", fail_second)
    try:
        server._prepare_workspace(req)
    except server.HTTPException as exc:
        assert "second failed" in exc.detail
    else:
        raise AssertionError("第二个 worktree 失败时应终止")

    target = tmp_path / ".demo-cockpit-worktrees" / "rollback" / "1-codex"
    assert not target.exists()
    branch = subprocess.run(
        [
            "git", "-C", str(repo), "show-ref", "--verify", "--quiet",
            "refs/heads/agent-cockpit/rollback/1-codex",
        ],
    )
    assert branch.returncode == 1


def test_inspect_workspace_reports_git_capabilities(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    non_git = client.post(
        "/api/herdr/inspect-workspace",
        headers={"authorization": "Bearer secret"},
        json={"workdir": str(tmp_path)},
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    git = client.post(
        "/api/herdr/inspect-workspace",
        headers={"authorization": "Bearer secret"},
        json={"workdir": str(repo)},
    )

    assert non_git.status_code == 200
    assert non_git.json()["is_git"] is False
    assert git.status_code == 200
    assert git.json()["is_git"] is True
    assert git.json()["dirty"] is False


def test_setup_workspace_rejects_concurrent_request_for_same_session(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    lock = threading.Lock()
    lock.acquire()
    with server._SETUP_WORKSPACE_LOCKS_GUARD:
        server._SETUP_WORKSPACE_LOCKS["busy"] = lock
    try:
        response = TestClient(server.app).post(
            "/api/herdr/setup-workspace",
            headers={"authorization": "Bearer secret"},
            json={"session": "busy", "workdir": str(tmp_path)},
        )
    finally:
        lock.release()
        with server._SETUP_WORKSPACE_LOCKS_GUARD:
            server._SETUP_WORKSPACE_LOCKS.pop("busy", None)

    assert response.status_code == 409
    assert "重复提交" in response.json()["detail"]


def test_rollback_removes_remounted_worktree_but_keeps_old_branch(monkeypatch, tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git_repo(repo)
    first_req = server.SetupWorkspaceReq(
        session="keep",
        workdir=str(repo),
        mode="custom",
        participants=[
            {
                "id": "one", "agent": "codex", "role": "lead",
                "task": "保持旧分支", "workspace": "isolated",
            },
        ],
    )
    first, _ = server._prepare_workspace(first_req)
    branch = first[0]["branch"]
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", first[0]["worktree"]],
        check=True, capture_output=True,
    )
    resume_req = server.SetupWorkspaceReq(
        session="keep",
        workdir=str(repo),
        mode="parallel",
        participants=[
            {"id": "one", "agent": "codex", "role": "lead", "task": "后端"},
            {"id": "two", "agent": "kimi", "role": "developer", "task": "前端"},
        ],
    )
    real_ensure = server._ensure_worktree

    def fail_second(*args, **kwargs):
        if kwargs.get("index", args[4] if len(args) > 4 else None) == 1:
            raise ValueError("second failed")
        return real_ensure(*args, **kwargs)

    monkeypatch.setattr(server, "_ensure_worktree", fail_second)
    try:
        server._prepare_workspace(resume_req)
    except server.HTTPException:
        pass
    else:
        raise AssertionError("第二个 worktree 失败时应终止")

    assert not Path(first[0]["worktree"]).exists()
    assert subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
    ).returncode == 0
