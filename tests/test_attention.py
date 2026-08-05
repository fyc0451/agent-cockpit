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
    monkeypatch.setattr(
        server.coordination,
        "run_by_session",
        lambda session: {
            "participants": [
                {
                    "participant_id": "lead",
                    "agent_type": "codex",
                    "mail_name": "codex-main",
                    "pane_id": "w1:p2",
                    "role": "lead",
                    "task_text": "完成任务看板",
                    "task_revision": 2,
                    "state": "working",
                },
                {
                    "participant_id": "developer",
                    "agent_type": "kimi",
                    "mail_name": "kimi-main",
                    "pane_id": "w1:p3",
                    "role": "developer",
                    "task_text": "补充测试",
                    "task_revision": 1,
                    "state": "working",
                },
            ]
        },
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
    assert len(result["sessions"]) == 1
    session = result["sessions"][0]
    assert session["session"] == "demo"
    assert session["status"] == "blocked"
    assert session["progress"] == 0
    assert session["summary"] == {
        "working": 1,
        "blocked": 1,
        "done": 0,
        "idle": 0,
        "unknown": 0,
    }
    assert session["agents"][0] == {
        "agent": "codex",
        "mail_name": "codex-main",
        "role": "lead",
        "task": "完成任务看板",
        "status": "blocked",
        "pane_id": "w1:p2",
        "cwd": "",
        "coordination_state": "working",
        "task_revision": 2,
        "report": None,
    }


def test_refresh_reports_prompts_agents_and_exposes_structured_progress(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.coordination, "DB_PATH", tmp_path / "coordination.sqlite3"
    )
    snapshot = {
        "sessions": [{
            "session": "demo",
            "directory": str(tmp_path),
            "panes": [{
                "session": "demo", "pane_id": "w1:p2", "agent": "codex",
                "agent_status": "working", "mail_name": "codex-main",
            }],
        }],
        "panes": [],
    }
    monkeypatch.setattr(server, "_board_snapshot", lambda: snapshot)
    monkeypatch.setattr(server.coordination, "run_by_session", lambda session: None)
    prompts = []
    monkeypatch.setattr(
        server.herdr_client,
        "pane_send",
        lambda session, pane, text, mode: prompts.append(
            (session, pane, text, mode)
        ) or {"available": True},
    )

    response = TestClient(server.app).post(
        "/api/attention/refresh-reports",
        headers={"authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json()["requested"] == 1
    assert len(prompts) == 1
    assert "非打断状态上报" in prompts[0][2]
    assert "task-report" in prompts[0][2]
    report = server.coordination.task_report("demo", "w1:p2")
    assert report["pending"] is True

    server.coordination.submit_task_report(
        "demo", "w1:p2", report["request_id"], 60,
        "完成后端", "继续前端", "", now=200,
    )
    sessions = server._build_session_progress(snapshot)
    shown = sessions[0]["agents"][0]["report"]
    assert shown["progress"] == 60
    assert shown["summary"] == "完成后端"


def test_session_progress_keeps_legacy_sessions_without_coordination(monkeypatch):
    monkeypatch.setattr(server.coordination, "run_by_session", lambda session: None)

    sessions = server._build_session_progress({
        "sessions": [
            {
                "session": "legacy",
                "directory": "/sessions/legacy",
                "panes": [
                    {
                        "session": "legacy",
                        "pane_id": "w1:p2",
                        "agent": "opencode",
                        "agent_status": "done",
                        "mail_name": "opencode-main",
                        "cwd": "/project",
                    },
                    {"session": "legacy", "pane_id": "w1:p3", "agent": None},
                ],
            },
            {"session": "terminal-only", "panes": []},
        ],
        "panes": [],
    })

    assert [item["session"] for item in sessions] == ["legacy", "terminal-only"]
    assert sessions[0]["progress"] == 100
    assert sessions[0]["status"] == "done"
    assert sessions[0]["agents"][0]["mail_name"] == "opencode-main"
    assert sessions[0]["agents"][0]["task"] is None
    assert sessions[1]["status"] == "empty"
    assert sessions[1]["agents"] == []


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
        lambda cwd, **_: created.append(cwd) or {"id": "term1"},
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


def test_setup_workspace_bootstraps_safe_width_before_horizontal_splits(
    monkeypatch, tmp_path,
):
    """三个 agent 启动时不能继续沿用 80 列，避免末端 pane 只有约 27 列。"""
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
        lambda session, workdir, agent, **kwargs: {
            "available": True, "pane_id": f"w1:p-{agent}",
        },
    )
    monkeypatch.setattr(server.herdr_client, "snapshot", lambda: {"panes": []})
    created = []

    def create_term(cwd, cols=80, rows=24):
        created.append((cwd, cols, rows))
        return {"id": "term1"}

    monkeypatch.setattr(server.terminal, "create_term", create_term)
    monkeypatch.setattr(server.terminal, "write_term", lambda *_: None)
    monkeypatch.setattr(server, "_start_pty_drainer", lambda *_: (None, None))
    monkeypatch.setattr(server, "_stop_pty_drainer", lambda *_: None)
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)

    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={
            "session": "demo",
            "workdir": str(tmp_path),
            "agents": ["codex", "kimi", "opencode"],
            "layout": "right",
        },
    )

    assert response.json()["ok"] is True
    # 启动阶段还有一个 Herdr 初始 shell pane，因此为 4 个 pane 各留 100 列。
    assert created == [(str(tmp_path), 400, 30)]


def test_workspace_bootstrap_dimensions_cover_all_layouts_and_caps():
    assert server._workspace_bootstrap_dims("horizontal", 3) == (400, 30)
    assert server._workspace_bootstrap_dims("vertical", 3) == (100, 120)
    assert server._workspace_bootstrap_dims("tab", 3) == (100, 30)
    assert server._workspace_bootstrap_dims("right", 99) == (
        server.terminal.MAX_COLS, 30,
    )
    assert server._workspace_bootstrap_dims("down", 99) == (
        100, server.terminal.MAX_ROWS,
    )


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
    monkeypatch.setattr(server.terminal, "create_term", lambda cwd, **_: {"id": "term1"})
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
    monkeypatch.setattr(server.terminal, "create_term", lambda cwd, **_: {"id": "term1"})
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
    assert plans[0]["branch"] == "agent-cockpit/demo/codex-1"
    assert plans[1]["branch"] == "agent-cockpit/demo/kimi-1"
    assert plans[2]["branch"] is None
    assert all((Path(plan["workdir"]) / "README.md").is_file() for plan in plans)
    detached = subprocess.run(
        ["git", "-C", plans[2]["workdir"], "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
    )
    assert detached.returncode == 1


def test_prepare_parallel_workspace_allows_two_codex_instances(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git_repo(repo)
    req = server.SetupWorkspaceReq(
        session="double-codex",
        workdir=str(repo),
        mode="parallel",
        participants=[
            {
                "id": "backend", "name": "codex-backend", "agent": "codex",
                "role": "lead", "task": "实现后端",
            },
            {
                "id": "frontend", "name": "codex-frontend", "agent": "codex",
                "role": "developer", "task": "实现前端",
            },
        ],
    )

    plans, warnings = server._prepare_workspace(req)

    assert warnings == []
    assert [plan["agent"] for plan in plans] == ["codex", "codex"]
    assert [plan["name"] for plan in plans] == ["codex-backend", "codex-frontend"]
    assert [plan["strategy"] for plan in plans] == ["isolated", "isolated"]
    assert plans[0]["workdir"] != plans[1]["workdir"]


def test_prepare_workspace_rejects_duplicate_instance_names(tmp_path):
    req = server.SetupWorkspaceReq(
        session="duplicate-name",
        workdir=str(tmp_path),
        mode="custom",
        participants=[
            {
                "id": "one", "name": "codex-main", "agent": "codex",
                "role": "researcher", "task": "调研 A",
            },
            {
                "id": "two", "name": "codex-main", "agent": "codex",
                "role": "researcher", "task": "调研 B",
            },
        ],
    )

    try:
        server._prepare_workspace(req)
    except server.HTTPException as exc:
        assert exc.status_code == 400
        assert "实例名称不能重复" in exc.detail
    else:
        raise AssertionError("同一工作区不应接受重复实例名称")


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
    for _, briefing, _ in sent:
        assert "每完成一个里程碑检查一次未读消息" in briefing
        assert "多封消息按时间顺序处理" in briefing
        assert "完成当前原子操作并保存状态后立即停手汇报" in briefing


def test_agent_mail_identity_hint_includes_safe_message_checkpoint_protocol():
    hint = server._identity_hint("codex-main", "/tmp/project", "codex")

    assert "每完成一个里程碑检查一次未读消息" in hint
    assert "多封消息按时间顺序处理" in hint
    assert "完成当前原子操作并保存状态后立即停手汇报" in hint


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

    target = tmp_path / ".demo-cockpit-worktrees" / "rollback" / "codex-1"
    assert not target.exists()
    branch = subprocess.run(
        [
            "git", "-C", str(repo), "show-ref", "--verify", "--quiet",
            "refs/heads/agent-cockpit/rollback/codex-1",
        ],
    )
    assert branch.returncode == 1


def test_prepare_workspace_reuses_legacy_index_named_worktree(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git_repo(repo)
    participant = server.WorkspaceParticipantReq(
        id="one", agent="codex", role="lead", task="兼容旧工作区",
        workspace="isolated",
    )
    legacy = server._ensure_worktree(
        repo, repo, "legacy", participant, 0, detached=False,
    )
    req = server.SetupWorkspaceReq(
        session="legacy", workdir=str(repo), mode="custom",
        participants=[participant],
    )

    plans, _ = server._prepare_workspace(req)

    assert plans[0]["name"] == "codex-1"
    assert plans[0]["reused"] is True
    assert plans[0]["worktree"] == legacy["worktree"]
    assert plans[0]["branch"] == "agent-cockpit/legacy/1-codex"


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


def test_start_agent_can_create_named_isolated_worktree(monkeypatch, tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    calls = []
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "available": True, "pane_id": "w1:p3", "label": kwargs.get("label"),
        },
    )

    response = TestClient(server.app).post(
        "/api/herdr/start",
        headers={"authorization": "Bearer secret"},
        json={
            "session": "demo", "workdir": str(repo), "agent": "codex",
            "name": "codex-2", "layout": "right", "workspace": "isolated",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["workspace"]["strategy"] == "isolated"
    assert body["workspace"]["branch"] == "agent-cockpit/demo/codex-2"
    assert Path(body["workspace"]["worktree"]).is_dir()
    assert calls[0][0][1] == body["workspace"]["workdir"]
    assert calls[0][1] == {"layout": "right", "label": "codex-2"}


def test_start_agent_registers_and_notifies_unique_qoder_identity(
    monkeypatch, tmp_path,
):
    workdir = tmp_path / "worktree"
    canonical = tmp_path / "project"
    workdir.mkdir()
    canonical.mkdir()
    init_script = tmp_path / "am-init-project"
    init_script.touch()
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server, "AGENT_MAIL_INIT_SCRIPT", init_script)
    monkeypatch.setattr(
        server.herdr_client, "start_agent",
        lambda *args, **kwargs: {"available": True, "pane_id": "w1:pA"},
    )
    monkeypatch.setattr(
        server.herdr_client, "snapshot",
        lambda: {"panes": [{
            "session": "demo", "pane_id": "w1:pA", "agent": "qodercli",
        }]},
    )
    monkeypatch.setattr(
        server, "_mail_project_state",
        lambda _: {"bound": True, "project": str(canonical)},
    )
    registered = {"value": False}
    monkeypatch.setattr(
        server, "_identity_name",
        lambda project, agent: (
            "qodercn-main"
            if registered["value"] and project == str(canonical) and agent == "qodercli"
            else None
        ),
    )
    init_calls = []

    def run_init(args, **kwargs):
        init_calls.append((args, kwargs))
        registered["value"] = True
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(server.subprocess, "run", run_init)
    sent = []
    monkeypatch.setattr(
        server.herdr_client, "pane_send",
        lambda *args: sent.append(args) or {"available": True},
    )
    joined = []
    monkeypatch.setattr(
        server.coordination, "add_participant",
        lambda **kwargs: joined.append(kwargs) or {
            "joined": True, "reused": False, "participant_id": "qodercli-1",
        },
    )

    response = TestClient(server.app).post(
        "/api/herdr/start",
        headers={"authorization": "Bearer secret"},
        json={
            "session": "demo", "workdir": str(workdir), "agent": "qodercli",
            "name": "qodercli-1", "workspace": "shared",
        },
    )

    assert response.status_code == 200
    mail = response.json()["agent_mail"]
    assert mail == {
        "project": str(canonical), "name": "qodercn-main",
        "registered": True, "registered_now": True, "notified": True,
    }
    assert init_calls[0][0] == [
        str(init_script), "--project", str(canonical), "--only", "qodercn",
    ]
    assert init_calls[0][1]["cwd"] == str(canonical)
    assert sent[0][0:2] == ("demo", "w1:pA")
    assert "项目=" + str(canonical) in sent[0][2]
    assert "--agent qodercn" in sent[0][2]
    assert joined == [{
        "session": "demo", "participant_id": "qodercli-1",
        "agent": "qodercli", "pane_id": "w1:pA", "workdir": str(workdir),
        "mail_name": "qodercn-main",
    }]
    assert response.json()["coordination"]["joined"] is True


def test_start_agent_keeps_launch_success_when_identity_registration_fails(
    monkeypatch, tmp_path,
):
    workdir = tmp_path / "worktree"
    canonical = tmp_path / "project"
    workdir.mkdir()
    canonical.mkdir()
    init_script = tmp_path / "am-init-project"
    init_script.touch()
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server, "AGENT_MAIL_INIT_SCRIPT", init_script)
    monkeypatch.setattr(
        server.herdr_client, "start_agent",
        lambda *args, **kwargs: {"available": True, "pane_id": "w1:pA"},
    )
    monkeypatch.setattr(
        server.herdr_client, "snapshot",
        lambda: {"panes": [{
            "session": "demo", "pane_id": "w1:pA", "agent": "qodercli",
        }]},
    )
    monkeypatch.setattr(
        server, "_mail_project_state",
        lambda _: {"bound": True, "project": str(canonical)},
    )
    monkeypatch.setattr(server, "_identity_name", lambda *_: None)
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "hub down"),
    )

    response = TestClient(server.app).post(
        "/api/herdr/start",
        headers={"authorization": "Bearer secret"},
        json={
            "session": "demo", "workdir": str(workdir), "agent": "qodercli",
            "name": "qodercli-1", "workspace": "shared",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["pane_id"] == "w1:pA"
    assert body["agent_mail"]["registered"] is False
    assert "hub down" in body["agent_mail"]["warning"]


def test_start_agent_skips_ambiguous_same_type_mail_identity(monkeypatch, tmp_path):
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.herdr_client, "start_agent",
        lambda *args, **kwargs: {"available": True, "pane_id": "w1:p4"},
    )
    monkeypatch.setattr(
        server.herdr_client, "snapshot",
        lambda: {"panes": [
            {"session": "demo", "pane_id": "w1:p2", "agent": "codex"},
            {"session": "demo", "pane_id": "w1:p4", "agent": "codex"},
        ]},
    )
    monkeypatch.setattr(
        server, "_mail_project_state",
        lambda _: (_ for _ in ()).throw(AssertionError("不应绑定重复类型身份")),
    )

    response = TestClient(server.app).post(
        "/api/herdr/start",
        headers={"authorization": "Bearer secret"},
        json={
            "session": "demo", "workdir": str(workdir), "agent": "codex",
            "name": "codex-2", "workspace": "shared",
        },
    )

    assert response.status_code == 200
    mail = response.json()["agent_mail"]
    assert mail["skipped"] == "ambiguous_same_type"
    assert "同类型多实例" in mail["warning"]


def test_start_agent_from_existing_worktree_creates_sibling_at_primary_repo(
    monkeypatch, tmp_path,
):
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git_repo(repo)
    first = server._ensure_worktree(
        repo, repo, "demo",
        server.WorkspaceParticipantReq(
            id="one", name="codex-1", agent="codex", task="first",
        ),
        0, detached=False, slug="codex-1",
    )
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.herdr_client, "start_agent",
        lambda *args, **kwargs: {"available": True, "pane_id": "w1:p3"},
    )

    response = TestClient(server.app).post(
        "/api/herdr/start",
        headers={"authorization": "Bearer secret"},
        json={
            "session": "demo", "workdir": first["workdir"], "agent": "codex",
            "name": "codex-2", "workspace": "isolated",
        },
    )

    assert response.status_code == 200
    expected = tmp_path / ".demo-cockpit-worktrees" / "demo" / "codex-2"
    assert Path(response.json()["workspace"]["worktree"]) == expected.resolve()
    assert expected.is_dir()


def test_start_agent_rolls_back_new_isolated_worktree_on_launch_failure(
    monkeypatch, tmp_path,
):
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.herdr_client, "start_agent",
        lambda *args, **kwargs: {"available": True, "error": "launch failed"},
    )

    response = TestClient(server.app).post(
        "/api/herdr/start",
        headers={"authorization": "Bearer secret"},
        json={
            "session": "demo", "workdir": str(repo), "agent": "codex",
            "name": "codex-2", "workspace": "isolated",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"] == "launch failed"
    assert body["worktree_rolled_back"] is True
    assert not Path(body["workspace"]["worktree"]).exists()
    assert subprocess.run(
        [
            "git", "-C", str(repo), "show-ref", "--verify", "--quiet",
            "refs/heads/agent-cockpit/demo/codex-2",
        ],
    ).returncode == 1


def test_start_agent_rolls_back_new_worktree_after_unexpected_exception(
    monkeypatch, tmp_path,
):
    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.herdr_client, "start_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    response = TestClient(server.app, raise_server_exceptions=False).post(
        "/api/herdr/start",
        headers={"authorization": "Bearer secret"},
        json={
            "session": "demo", "workdir": str(repo), "agent": "codex",
            "name": "codex-2", "workspace": "isolated",
        },
    )

    assert response.status_code == 500
    target = tmp_path / ".demo-cockpit-worktrees" / "demo" / "codex-2"
    assert not target.exists()
    assert subprocess.run(
        [
            "git", "-C", str(repo), "show-ref", "--verify", "--quiet",
            "refs/heads/agent-cockpit/demo/codex-2",
        ],
    ).returncode == 1


def test_start_agent_rejects_concurrent_session_change(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.herdr_client, "start_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应启动")),
    )
    lock = threading.Lock()
    lock.acquire()
    with server._SETUP_WORKSPACE_LOCKS_GUARD:
        server._SETUP_WORKSPACE_LOCKS["busy-agent"] = lock
    try:
        response = TestClient(server.app).post(
            "/api/herdr/start",
            headers={"authorization": "Bearer secret"},
            json={
                "session": "busy-agent", "workdir": str(tmp_path),
                "agent": "codex", "name": "codex-2",
            },
        )
    finally:
        lock.release()
        with server._SETUP_WORKSPACE_LOCKS_GUARD:
            server._SETUP_WORKSPACE_LOCKS.pop("busy-agent", None)

    assert response.status_code == 409
    assert "请勿重复提交" in response.json()["detail"]


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
