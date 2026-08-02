from fastapi.testclient import TestClient

import server


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
        "unread_messages",
        lambda limit=50: [
            {
                "id": 42,
                "project_slug": "demo-project",
                "subject": "Review this",
                "sender_name": "kimi-main",
                "created_ts": 30,
                "recipients": "codex-main",
            }
        ],
    )

    result = server._build_attention(snapshot)

    by_kind = {item["kind"]: item for item in result["items"]}
    assert result["count"] == 4
    assert set(by_kind) == {"pane_blocked", "task_failed", "task_review", "mail_unread"}
    assert by_kind["pane_blocked"]["url"] == "/#/attention/pane/demo/w1%3Ap2"
    assert by_kind["task_failed"]["url"] == "/#/attention/task/failed1"
    assert by_kind["task_review"]["target"]["task_id"] == "review1"
    assert by_kind["mail_unread"]["url"] == "/#/attention/mail/demo-project/42"
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
        "unread_messages",
        lambda limit=50: (_ for _ in ()).throw(AssertionError("must not query mail DB")),
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


def test_identity_lookup_falls_back_without_agent_mail(monkeypatch):
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda *_: (_ for _ in ()).throw(RuntimeError("missing DB")),
    )

    assert server._identity_name("/project", "codex") == "codex-main"


def test_attention_keeps_mail_readable_when_hub_is_down(monkeypatch):
    monkeypatch.setattr(server.tasks, "list_tasks", lambda limit=50: [])
    monkeypatch.setattr(
        server.db, "status", lambda: {"available": True, "reason": None}
    )
    monkeypatch.setattr(server.db, "unread_messages", lambda limit=50: [])
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
