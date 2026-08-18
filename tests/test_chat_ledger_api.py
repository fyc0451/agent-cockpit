"""Cockpit 3.0 账本 HTTP：工作区 CRUD、thread 绑定、候选列表。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_cockpit import chat_ledger
from agent_cockpit import runtime_paths
from agent_cockpit import server


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    data = tmp_path / "data"
    config = tmp_path / "config"
    state = tmp_path / "state"
    uploads = tmp_path / "uploads"
    for path in (data, config, state, uploads):
        path.mkdir()
    monkeypatch.setenv("COCKPIT_DATA_DIR", str(data))
    monkeypatch.setenv("COCKPIT_CONFIG_DIR", str(config))
    monkeypatch.setenv("COCKPIT_STATE_DIR", str(state))
    monkeypatch.setenv("COCKPIT_UPLOADS_DIR", str(uploads))
    monkeypatch.delenv("COCKPIT_COORDINATION_DB", raising=False)
    runtime_paths.reset_cache()
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    yield tmp_path
    runtime_paths.reset_cache()


def _client() -> TestClient:
    return TestClient(server.app)


def _headers() -> dict[str, str]:
    return {"authorization": "Bearer secret"}


def test_workspace_crud_idempotent_and_delete_skips_disk(isolated_ledger):
    proj = isolated_ledger / "repo"
    proj.mkdir()
    (proj / "keep.txt").write_text("ok", encoding="utf-8")
    client = _client()
    created = client.post(
        "/api/chat/workspaces",
        headers=_headers(),
        json={"path": str(proj), "title": "Repo"},
    )
    again = client.post(
        "/api/chat/workspaces",
        headers=_headers(),
        json={"path": str(proj)},
    )
    assert created.status_code == again.status_code == 200
    assert created.json()["id"] == again.json()["id"]
    listed = client.get("/api/chat/workspaces", headers=_headers())
    assert listed.status_code == 200
    body = listed.json()
    assert len(body["workspaces"]) == 1
    assert body["workspaces"][0]["title"] == "Repo"
    assert body["workspaces"][0]["threads"] == []
    deleted = client.delete(
        f"/api/chat/workspaces/{created.json()['id']}",
        headers=_headers(),
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
    assert proj.is_dir()
    assert (proj / "keep.txt").read_text(encoding="utf-8") == "ok"
    missing = client.delete(
        f"/api/chat/workspaces/{created.json()['id']}",
        headers=_headers(),
    )
    assert missing.status_code == 404


def test_create_workspace_binds_existing_sessions(isolated_ledger, monkeypatch):
    proj = isolated_ledger / "app"
    nested = proj / "web"
    proj.mkdir()
    nested.mkdir()
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [
            {"name": "app-1", "status": "running", "directory": "/tmp/herdr-app-1"},
            {"name": "nested-1", "status": "stopped", "directory": "/tmp/herdr-nested"},
            {"name": "other", "status": "running", "directory": "/tmp/herdr-other"},
        ],
    )
    monkeypatch.setattr(
        server,
        "_chat_session_workdir",
        lambda name: {
            "app-1": proj,
            "nested-1": nested,
            "other": isolated_ledger / "elsewhere",
        }.get(name),
    )
    created = _client().post(
        "/api/chat/workspaces",
        headers=_headers(),
        json={"path": str(proj)},
    )
    assert created.status_code == 200
    names = sorted(row["herdr_session"] for row in created.json()["threads"])
    assert names == ["app-1", "nested-1"]


def test_herdr_state_directory_is_not_a_project_root(isolated_ledger, monkeypatch):
    proj = isolated_ledger / "app"
    proj.mkdir()
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "app-1", "status": "running", "directory": str(proj)}],
    )
    monkeypatch.setattr(server.herdr_client, "recorded_session_workdirs", lambda _n: [])
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: {"panes": []})
    monkeypatch.setattr(server.mail_projects, "get", lambda *_a, **_k: None)
    created = _client().post(
        "/api/chat/workspaces",
        headers=_headers(),
        json={"path": str(proj)},
    )
    assert created.status_code == 200
    assert created.json()["threads"] == []


def test_reject_home_and_missing_path(isolated_ledger):
    client = _client()
    home = client.post(
        "/api/chat/workspaces",
        headers=_headers(),
        json={"path": str(Path.home())},
    )
    assert home.status_code == 400
    missing = client.post(
        "/api/chat/workspaces",
        headers=_headers(),
        json={"path": str(isolated_ledger / "nope")},
    )
    assert missing.status_code == 400


def test_thread_bind_and_candidates(isolated_ledger, monkeypatch):
    proj = isolated_ledger / "app"
    proj.mkdir()
    client = _client()
    ws = client.post(
        "/api/chat/workspaces",
        headers=_headers(),
        json={"path": str(proj)},
    ).json()

    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [
            {"name": "app-1", "status": "running", "directory": "/tmp/herdr-app-1"},
            {"name": "other", "status": "stopped", "directory": "/tmp/herdr-other"},
        ],
    )
    monkeypatch.setattr(
        server,
        "_chat_session_workdir",
        lambda name: proj if name == "app-1" else isolated_ledger / "elsewhere",
    )

    detail = client.get(f"/api/chat/workspaces/{ws['id']}", headers=_headers())
    assert detail.status_code == 200
    assert [row["name"] for row in detail.json()["candidates"]] == ["app-1"]

    bound = client.post(
        f"/api/chat/workspaces/{ws['id']}/threads",
        headers=_headers(),
        json={"herdr_session": "app-1", "title": "主会话"},
    )
    again = client.post(
        f"/api/chat/workspaces/{ws['id']}/threads",
        headers=_headers(),
        json={"herdr_session": "app-1"},
    )
    assert bound.status_code == again.status_code == 200
    assert bound.json()["id"] == again.json()["id"]
    assert bound.json()["title"] == "主会话"

    after = client.get(f"/api/chat/workspaces/{ws['id']}", headers=_headers())
    assert after.json()["candidates"] == []
    assert after.json()["threads"][0]["herdr_session"] == "app-1"

    gone = client.post(
        f"/api/chat/workspaces/ws_{'0' * 12}/threads",
        headers=_headers(),
        json={"herdr_session": "app-2"},
    )
    assert gone.status_code == 404


def _workspace_with_thread(client, root: Path, session: str) -> tuple[dict, dict]:
    root.mkdir()
    workspace = client.post(
        "/api/chat/workspaces", headers=_headers(), json={"path": str(root)},
    ).json()
    from unittest.mock import patch
    with patch.object(server, "_chat_repair_agent_mail", lambda *_a, **_k: {"ok": True}):
        body = client.post(
            f"/api/chat/workspaces/{workspace['id']}/bind",
            headers=_headers(), json={"herdr_session": session},
        ).json()
    thread = body["thread"] if isinstance(body, dict) and "thread" in body else body
    return workspace, thread


def test_open_running_thread_returns_without_start(isolated_ledger, monkeypatch):
    client = _client()
    monkeypatch.setattr(
        server, "_chat_repair_agent_mail", lambda *_: {"ok": True}, raising=False,
    )
    workspace, thread = _workspace_with_thread(client, isolated_ledger / "running", "run-1")
    monkeypatch.setattr(
        server.herdr_client, "list_sessions",
        lambda: [{"name": "run-1", "status": "running"}],
    )
    monkeypatch.setattr(
        server.herdr_client, "start_session",
        lambda _name: (_ for _ in ()).throw(AssertionError("running session 不应 start")),
    )
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: {"panes": []})
    monkeypatch.setattr(server.herdr_client, "list_session_launch_descriptors", lambda _s: [])

    response = client.post(
        f"/api/chat/workspaces/{workspace['id']}/open", headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "thread": thread, "status": "running", "started": False,
        "restored": [],
        "bound": [],
        "agent_mail": {"ok": True},
    }


def test_open_stopped_thread_starts_only_bound_session(isolated_ledger, monkeypatch):
    client = _client()
    monkeypatch.setattr(
        server, "_chat_repair_agent_mail", lambda *_: {"ok": True}, raising=False,
    )
    workspace, thread = _workspace_with_thread(client, isolated_ledger / "stopped", "stop-1")
    monkeypatch.setattr(
        server.herdr_client, "list_sessions",
        lambda: [
            {"name": "stop-1", "status": "stopped"},
            {"name": "unrelated", "status": "stopped"},
        ],
    )
    started = []
    monkeypatch.setattr(
        server.herdr_client, "start_session",
        lambda name: started.append(name) or {"available": True, "started": name},
    )

    response = client.post(
        f"/api/chat/workspaces/{workspace['id']}/open", headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["thread"] == thread
    assert response.json()["status"] == "running"
    assert response.json()["started"] is True
    assert started == ["stop-1"]


def test_open_without_thread_returns_candidates_without_binding(
    isolated_ledger, monkeypatch,
):
    client = _client()
    root = isolated_ledger / "candidate"
    root.mkdir()
    workspace = client.post(
        "/api/chat/workspaces", headers=_headers(), json={"path": str(root)},
    ).json()
    candidates = [{"name": "candidate-1", "status": "running", "directory": "/tmp/c"}]
    monkeypatch.setattr(server, "_bind_matching_sessions", lambda _row: [])
    monkeypatch.setattr(server, "_chat_bind_candidates", lambda _row: candidates)

    response = client.post(
        f"/api/chat/workspaces/{workspace['id']}/open", headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "needs_bind": True, "candidates": candidates, "bound": [],
    }
    assert server.chat_ledger.list_threads(workspace["id"]) == []


def test_open_auto_binds_matching_sessions(isolated_ledger, monkeypatch):
    client = _client()
    root = isolated_ledger / "auto"
    root.mkdir()
    workspace = client.post(
        "/api/chat/workspaces", headers=_headers(), json={"path": str(root)},
    ).json()
    monkeypatch.setattr(
        server.herdr_client, "list_sessions",
        lambda: [{"name": "auto-1", "status": "running", "directory": str(root)}],
    )
    monkeypatch.setattr(server, "_chat_session_workdir", lambda _n: root)
    monkeypatch.setattr(server, "_chat_repair_agent_mail", lambda *_: {"ok": True})
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: {"panes": []})
    monkeypatch.setattr(server.herdr_client, "list_session_launch_descriptors", lambda _s: [])

    response = client.post(
        f"/api/chat/workspaces/{workspace['id']}/open", headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["thread"]["herdr_session"] == "auto-1"
    assert [row["herdr_session"] for row in body["bound"]] == ["auto-1"]
    assert server.chat_ledger.get_thread_by_session("auto-1") is not None


def test_bind_rejects_invalid_session_name(isolated_ledger):
    client = _client()
    root = isolated_ledger / "invalid"
    root.mkdir()
    workspace = client.post(
        "/api/chat/workspaces", headers=_headers(), json={"path": str(root)},
    ).json()

    response = client.post(
        f"/api/chat/workspaces/{workspace['id']}/bind",
        headers=_headers(), json={"herdr_session": "bad session"},
    )

    assert response.status_code == 400
    assert server.chat_ledger.list_threads(workspace["id"]) == []


def _patch_repair_mail_project(monkeypatch, isolated_ledger, *, exists=True):
    monkeypatch.setattr(
        server, "_bind_mail_project",
        lambda name, project: (project, str(isolated_ledger)),
    )
    monkeypatch.setattr(
        server.db, "project_by_canonical_key",
        lambda key: {"id": 1, "slug": "demo-ws", "human_key": key} if exists else None,
    )
    monkeypatch.setattr(
        server.hub_client, "ensure_project",
        lambda key: {"ok": True, "slug": "demo-ws", "human_key": key},
    )


def test_chat_repair_agent_mail_calls_existing_init_for_missing_mail_name(
    isolated_ledger, monkeypatch,
):
    workspace = {"id": "ws_000000000001", "path": str(isolated_ledger)}
    monkeypatch.setattr(
        server.herdr_client, "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(isolated_ledger)}],
    )
    monkeypatch.setattr(
        server, "_board_snapshot",
        lambda: {
            "sessions": [{
                "session": "demo",
                "panes": [{"pane_id": "w1:p2", "agent": "codex"}],
            }],
            "panes": [],
        },
    )
    _patch_repair_mail_project(monkeypatch, isolated_ledger)
    calls = []
    monkeypatch.setattr(
        server, "api_herdr_session_init_mail",
        lambda name: calls.append(name) or {"ok": True, "notified": ["codex(w1:p2)"]},
    )

    result = server._chat_repair_agent_mail(workspace, "demo")

    assert calls == ["demo"]
    assert result["ok"] is True


def test_chat_repair_skips_live_session_when_workspace_cwd_differs(
    isolated_ledger, monkeypatch,
):
    workspace = {"id": "ws_000000000001", "path": str(isolated_ledger / "pytest-tmp")}
    (isolated_ledger / "pytest-tmp").mkdir()
    monkeypatch.setattr(
        server.herdr_client, "list_sessions",
        lambda: [{"name": "cockpit", "status": "running"}],
    )
    monkeypatch.setattr(
        server, "_board_snapshot",
        lambda: {
            "sessions": [],
            "panes": [{
                "session": "cockpit",
                "pane_id": "w1:p1",
                "agent": "grok",
                "cwd": "/home/fyc/github/agent-cockpit",
            }],
        },
    )
    monkeypatch.setattr(
        server, "_bind_mail_project",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不得改绑活 session")),
    )
    monkeypatch.setattr(
        server.hub_client, "ensure_project",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不得登记 pytest 目录")),
    )
    monkeypatch.setattr(
        server, "api_herdr_session_init_mail",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不得对活 pane 发身份告知")),
    )

    result = server._chat_repair_agent_mail(workspace, "cockpit")

    assert result["ok"] is False
    assert result["reason"] == "workspace_not_this_session"


def test_chat_repair_agent_mail_registers_missing_hub_project(
    isolated_ledger, monkeypatch,
):
    workspace = {"id": "ws_000000000001", "path": str(isolated_ledger)}
    monkeypatch.setattr(
        server.herdr_client, "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(isolated_ledger)}],
    )
    monkeypatch.setattr(
        server, "_board_snapshot",
        lambda: {
            "sessions": [{
                "session": "demo",
                "panes": [{"pane_id": "w1:p2", "agent": "codex", "mail_name": "DarkGlacier"}],
            }],
            "panes": [],
        },
    )
    _patch_repair_mail_project(monkeypatch, isolated_ledger, exists=False)
    ensured = []
    monkeypatch.setattr(
        server.hub_client, "ensure_project",
        lambda key: ensured.append(key) or {"ok": True, "slug": "demo-ws"},
    )
    monkeypatch.setattr(
        server, "api_herdr_session_init_mail",
        lambda _name: (_ for _ in ()).throw(AssertionError("init-mail should not run")),
    )

    result = server._chat_repair_agent_mail(workspace, "demo")

    assert result["ok"] is True
    assert result["attempted"] is False
    assert ensured == [str(isolated_ledger)]


def test_chat_repair_agent_mail_failure_returns_reason(
    isolated_ledger, monkeypatch,
):
    workspace = {"id": "ws_000000000001", "path": str(isolated_ledger)}
    monkeypatch.setattr(
        server.herdr_client, "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(isolated_ledger)}],
    )
    monkeypatch.setattr(
        server, "_board_snapshot",
        lambda: {
            "sessions": [{
                "session": "demo",
                "panes": [{"pane_id": "w1:p2", "agent": "codex"}],
            }],
            "panes": [],
        },
    )
    _patch_repair_mail_project(monkeypatch, isolated_ledger)
    monkeypatch.setattr(
        server, "api_herdr_session_init_mail",
        lambda _name: (_ for _ in ()).throw(RuntimeError("mail registration failed")),
    )

    result = server._chat_repair_agent_mail(workspace, "demo")

    assert result["ok"] is False
    assert result["reason"] == "mail registration failed"


def test_chat_repair_skips_descriptor_only_missing_mail(isolated_ledger, monkeypatch):
    workspace = {"id": "ws_000000000001", "path": str(isolated_ledger)}
    monkeypatch.setattr(
        server.herdr_client, "list_sessions",
        lambda: [{"name": "platform-1", "status": "running", "directory": str(isolated_ledger)}],
    )
    monkeypatch.setattr(
        server, "_board_snapshot",
        lambda: {
            "sessions": [],
            "panes": [
                {
                    "session": "platform-1", "pane_id": "w1:p2", "agent": "codex",
                    "mail_name": "DarkGlacier", "cwd": str(isolated_ledger),
                },
                {
                    "session": "platform-1", "pane_id": "w1:p9", "agent": "grok",
                    "from_descriptor": True, "cwd": str(isolated_ledger),
                },
            ],
        },
    )
    _patch_repair_mail_project(monkeypatch, isolated_ledger)
    monkeypatch.setattr(
        server, "api_herdr_session_init_mail",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("descriptor stub 不应去登记")),
    )

    result = server._chat_repair_agent_mail(workspace, "platform-1")

    assert result == {"ok": True, "project": str(isolated_ledger), "attempted": False}


def test_mail_repair_result_maps_error_to_reason():
    mapped = server._mail_repair_result({
        "ok": False, "error": "请选择该 session 使用的 Agent Mail 通信项目",
        "code": "mail_project_required",
    })
    assert mapped["reason"] == "请选择该 session 使用的 Agent Mail 通信项目"
    assert server._mail_repair_result({"ok": False})["reason"] == "init-mail 未完成"


def test_start_session_uses_herdr_flag_and_detaches(monkeypatch):
    from agent_cockpit import herdr_client
    from agent_cockpit import terminal

    calls = {"list": 0, "cmds": [], "writes": []}

    def fake_list():
        calls["list"] += 1
        if calls["list"] == 1:
            return [{"name": "s1", "status": "stopped", "directory": "/tmp/s1"}]
        return [{"name": "s1", "status": "running", "directory": "/tmp/s1"}]

    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "list_sessions", fake_list)
    monkeypatch.setattr(
        terminal, "create_term",
        lambda cwd, **kwargs: calls["cmds"].append((cwd, kwargs.get("command"))) or {"id": "t1"},
    )
    monkeypatch.setattr(terminal, "write_term", lambda tid, data: calls["writes"].append((tid, data)))
    monkeypatch.setattr(terminal, "kill_term", lambda *_a, **_k: None)
    monkeypatch.setattr(herdr_client.time, "sleep", lambda *_: None)

    result = herdr_client.start_session("s1")
    assert result == {"available": True, "started": "s1", "reused": False}
    assert calls["cmds"] == [("/tmp/s1", [herdr_client.HERDR_BIN, "--session", "s1"])]
    assert calls["writes"] == [("t1", "\x02d")]


def test_create_chat_session_starts_herdr_and_leader(isolated_ledger, monkeypatch):
    proj = isolated_ledger / "app"
    proj.mkdir()
    client = _client()
    workspace = client.post(
        "/api/chat/workspaces",
        headers=_headers(),
        json={"path": str(proj)},
    ).json()
    started = []
    agents = []
    monkeypatch.setattr(
        server.herdr_client, "list_sessions", lambda: [],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "start_session",
        lambda name, workdir=None: started.append((name, workdir)) or {
            "available": True, "started": name, "reused": False,
        },
    )
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda session, workdir, agent, **kwargs: agents.append((session, workdir, agent))
        or {"available": True, "pane_id": "w1:p2", "agent": agent},
    )
    monkeypatch.setattr(server, "_chat_repair_agent_mail", lambda *_: {"ok": True})

    created = client.post(
        f"/api/chat/workspaces/{workspace['id']}/sessions",
        headers=_headers(),
        json={"agent": "codex"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["session"].startswith("app-")
    assert body["thread"]["herdr_session"] == body["session"]
    assert started == [(body["session"], str(proj.resolve()))]
    assert agents[0][:3] == (body["session"], str(proj.resolve()), "codex")


def test_create_chat_session_forwards_model_and_args(isolated_ledger, monkeypatch):
    proj = isolated_ledger / "app"
    proj.mkdir()
    client = _client()
    workspace = client.post(
        "/api/chat/workspaces",
        headers=_headers(),
        json={"path": str(proj)},
    ).json()
    launched = []
    monkeypatch.setattr(server.herdr_client, "list_sessions", lambda: [])
    monkeypatch.setattr(
        server.herdr_client,
        "start_session",
        lambda name, workdir=None: {"available": True, "started": name, "reused": False},
    )
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda session, workdir, agent, model=None, **kwargs: launched.append(
            (session, agent, model, kwargs.get("args"))
        ) or {"available": True, "pane_id": "w1:p2", "agent": agent},
    )
    monkeypatch.setattr(server, "_chat_repair_agent_mail", lambda *_: {"ok": True})

    created = client.post(
        f"/api/chat/workspaces/{workspace['id']}/sessions",
        headers=_headers(),
        json={"agent": "kimi", "model": "kimi-code/k3", "args": "-y"},
    )
    assert created.status_code == 200
    assert launched[0][1:] == ("kimi", "kimi-code/k3", "-y")


def test_list_chat_mail_is_one_row_per_message(isolated_ledger, monkeypatch):
    client = _client()
    workspace, thread = _workspace_with_thread(client, isolated_ledger / "mail-ws", "mail-1")
    monkeypatch.setattr(server.db, "status", lambda: {"available": True, "reason": None})
    monkeypatch.setattr(
        server.next_profile, "require_project",
        lambda path: str(Path(path).expanduser().resolve()),
    )
    monkeypatch.setattr(
        server.db, "project_by_canonical_key",
        lambda key: {"id": 7, "human_key": key},
    )
    monkeypatch.setattr(
        server.db,
        "messages_for_canonical_project",
        lambda _pid, _limit: [
            {
                "id": 1, "sender_name": "human", "sender_program": "",
                "body_md": "hello", "subject": "hello", "created_ts": 100,
                "thread_id": "mail-1",
                "recipients": [{"name": "kimi"}],
            },
            {
                "id": 2, "sender_name": "kimi", "sender_program": "kimi",
                "body_md": "ok", "subject": "ok", "created_ts": 200,
                "thread_id": "mail-1",
                "recipients": [{"name": "human"}],
            },
            {
                "id": 3, "sender_name": "WildRidge", "sender_program": "codex",
                "body_md": "old review", "subject": "old review",
                "created_ts": "2026-08-12 09:04:42.746078",
                "thread_id": "",
                "recipients": [{"name": "codex-main"}],
            },
        ],
    )
    response = client.get(
        f"/api/chat/sessions/{thread['herdr_session']}/mail",
        headers=_headers(),
    )
    assert response.status_code == 200
    messages = response.json()["messages"]
    assert [row["id"] for row in messages] == ["1", "2"]
    # 账本已有同文时，Hub 重复信不应再出现
    chat_ledger.append_message(
        thread["herdr_session"], kind="me", sender="human", text="hello", to=["kimi"],
    )
    again = client.get(
        f"/api/chat/sessions/{thread['herdr_session']}/mail",
        headers=_headers(),
    )
    texts = [row["text"] for row in again.json()["messages"] if row["sender"] == "human"]
    assert texts.count("hello") == 1
    assert messages[0]["sender"] == "human"
    assert messages[0]["text"] == "hello"
    assert messages[0]["ts"] == 100_000
    assert messages[1]["sender"] == "kimi"
    assert messages[1]["text"] == "ok"


def test_remembered_pane_name_beats_program_main(isolated_ledger, monkeypatch, tmp_path):
    from agent_cockpit import chat_roster

    monkeypatch.setattr(chat_roster, "PANES_DIR", tmp_path / "panes")
    chat_roster.set_pane_mail_name("cockpit", "w1:p5", "FoggyBasin")
    snapshot = {
        "sessions": [{"session": "cockpit", "directory": "/home/fyc/github/agent-cockpit"}],
        "panes": [{
            "session": "cockpit", "pane_id": "w1:p5", "agent": "kimi",
        }],
    }
    monkeypatch.setattr(server.mail_projects, "get", lambda *_: "/home/fyc/github/agent-cockpit")
    monkeypatch.setattr(server, "_identity_name", lambda *_a, **_k: "kimi-main")
    monkeypatch.setattr(chat_roster, "get_session_leader", lambda *_: {})
    result = server._enrich_board_identities(snapshot)
    assert result["panes"][0]["mail_name"] == "FoggyBasin"


def test_resolve_chat_mail_rewrites_kimi_main_to_unique_flower(monkeypatch):
    monkeypatch.setattr(
        server, "_enrich_board_identities",
        lambda snap: snap,
    )
    monkeypatch.setattr(
        server, "_herdr_runtime_snapshot",
        lambda: {"panes": [{
            "session": "cockpit", "pane_id": "w1:p5", "agent": "kimi",
            "mail_name": "FoggyBasin",
        }]},
    )
    assert server._resolve_chat_mail_recipients("cockpit", ["kimi-main"]) == ["FoggyBasin"]


@pytest.mark.parametrize("agent", ["kimi", "grok"])
def test_resolve_chat_mail_does_not_guess_between_same_kind_panes(monkeypatch, agent):
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    monkeypatch.setattr(
        server,
        "_herdr_runtime_snapshot",
        lambda: {"panes": [
            {
                "session": "cockpit", "pane_id": "w1:p1", "agent": agent,
                "mail_name": "FirstFlower",
            },
            {
                "session": "cockpit", "pane_id": "w1:p2", "agent": agent,
                "mail_name": "SecondFlower",
            },
        ]},
    )
    leftover = f"{agent}-main"
    assert server._resolve_chat_mail_recipients("cockpit", [leftover]) == [leftover]


@pytest.mark.parametrize(
    ("agent", "current_flower", "other_flower"),
    [("kimi", "FoggyBasin", "OtherKimi"), ("grok", "BrownDesert", "OtherGrok")],
)
def test_resolve_chat_mail_only_uses_current_session(
    monkeypatch, agent, current_flower, other_flower,
):
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    monkeypatch.setattr(
        server,
        "_herdr_runtime_snapshot",
        lambda: {"panes": [
            {
                "session": "cockpit", "pane_id": "w1:p1", "agent": agent,
                "mail_name": current_flower,
            },
            {
                "session": "other", "pane_id": "w2:p1", "agent": agent,
                "mail_name": other_flower,
            },
        ]},
    )
    assert server._resolve_chat_mail_recipients(
        "cockpit", [f"{agent}-main"],
    ) == [current_flower]


def test_resolve_bare_codex_does_not_hijack_unique_flower(monkeypatch):
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    monkeypatch.setattr(
        server, "_herdr_runtime_snapshot",
        lambda: {"panes": [{
            "session": "cockpit", "pane_id": "w1:p2", "agent": "codex",
            "mail_name": "EmeraldCave", "display_name": "EmeraldCave",
        }]},
    )
    monkeypatch.setattr(server, "_flower_for_agent", lambda *_: "EmeraldCave")
    assert server._resolve_chat_mail_recipients("cockpit", ["codex"]) == ["codex"]
    assert server._resolve_chat_mail_recipients("cockpit", ["codex-p2"]) == ["EmeraldCave"]


def test_resolve_codex_uses_hub_leftover_mailbox(isolated_ledger, monkeypatch):
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    monkeypatch.setattr(
        server, "_herdr_runtime_snapshot",
        lambda: {"panes": [{
            "session": "scc-1", "pane_id": "w1:p3", "agent": "codex",
            "mail_name": "codex", "display_name": "codex",
        }]},
    )
    monkeypatch.setattr(server, "_chat_workspace_root", lambda _s: isolated_ledger)
    monkeypatch.setattr(server, "_identity_name", lambda *_a, **_k: "codex-main")
    assert server._resolve_chat_mail_recipients("scc-1", ["codex"]) == ["codex-main"]
    assert server._resolve_chat_mail_recipients("scc-1", ["codex-p3"]) == ["codex-main"]


def test_resolve_grok_p2_uses_workspace_registry_flower(isolated_ledger, monkeypatch):
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    monkeypatch.setattr(
        server, "_herdr_runtime_snapshot",
        lambda: {"panes": [{
            "session": "scc-1", "pane_id": "w1:p2", "agent": "grok",
            "mail_name": "", "display_name": "",
        }]},
    )
    monkeypatch.setattr(server, "_chat_workspace_root", lambda _s: isolated_ledger)
    monkeypatch.setattr(server, "_identity_name", lambda *_a, **_k: "DarkBrook")
    assert server._resolve_chat_mail_recipients("scc-1", ["grok-p2"]) == ["DarkBrook"]


def test_notify_wakes_pane_when_mail_name_empty_but_flower_matches(isolated_ledger, monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    monkeypatch.setattr(
        server, "_herdr_runtime_snapshot",
        lambda: {"panes": [{
            "session": "scc-1", "pane_id": "w1:p2", "agent": "grok",
            "mail_name": "", "display_name": "",
        }, {
            "session": "cockpit", "pane_id": "w1:p1", "agent": "grok",
            "mail_name": "BrownDesert", "display_name": "BrownDesert",
        }]},
    )
    monkeypatch.setattr(server, "_ensure_session_leader", lambda *_: {"leader_mail_name": "DarkBrook"})
    monkeypatch.setattr(server, "_flower_for_agent", lambda session, agent: (
        "DarkBrook" if session == "scc-1" and agent == "grok" else None
    ))
    monkeypatch.setattr(
        server.herdr_client, "pane_send",
        lambda *args: sent.append(args) or {"available": True},
    )
    server._notify_chat_recipients("scc-1", ["DarkBrook"], "分析下97号流程的草稿")
    assert len(sent) == 1
    assert sent[0][0] == "scc-1"
    assert sent[0][1] == "w1:p2"
    assert sent[0][3] == "prompt"
    assert "分析下97号流程的草稿" in sent[0][2]
    assert "结论写在终端" in sent[0][2]
    assert "瀑布流" in sent[0][2]
    assert "mail-recv" not in sent[0][2]


def test_notify_does_not_wake_ambiguous_same_kind_panes(isolated_ledger, monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    monkeypatch.setattr(
        server, "_herdr_runtime_snapshot",
        lambda: {"panes": [{
            "session": "scc-1", "pane_id": "w1:p2", "agent": "grok",
            "mail_name": "", "display_name": "",
        }, {
            "session": "scc-1", "pane_id": "w1:p4", "agent": "grok",
            "mail_name": "", "display_name": "",
        }]},
    )
    monkeypatch.setattr(server, "_ensure_session_leader", lambda *_: {})
    monkeypatch.setattr(server, "_flower_for_agent", lambda *_: "DarkBrook")
    monkeypatch.setattr(
        server.herdr_client, "pane_send",
        lambda *args: sent.append(args) or {"available": True},
    )
    server._notify_chat_recipients("scc-1", ["DarkBrook"], "不要乱叫")
    assert sent == []


def test_harvest_status_survives_reload(isolated_ledger, monkeypatch):
    path = isolated_ledger / "state" / "chat-harvest.json"
    monkeypatch.setenv("COCKPIT_STATE_DIR", str(isolated_ledger / "state"))
    server._PANE_LAST_STATUS.clear()
    server._HARVEST_STATUS_LOADED = False
    server._PANE_LAST_STATUS[("cockpit", "w1:p1")] = "working"
    server._save_harvest_status()
    assert path.is_file()
    server._PANE_LAST_STATUS.clear()
    server._HARVEST_STATUS_LOADED = False
    server._load_harvest_status()
    assert server._PANE_LAST_STATUS[("cockpit", "w1:p1")] == "working"


def test_list_chat_skills_reads_skill_dirs(isolated_ledger, monkeypatch, tmp_path):
    home = tmp_path / "home"
    skill = home / ".agents" / "skills" / "lark-im"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# lark-im\n", encoding="utf-8")
    monkeypatch.setattr(server.Path, "home", staticmethod(lambda: home))
    rows = server._list_chat_skills()
    assert any(item["id"] == "lark-im" for item in rows)


def test_ungrouped_session_mail_skips_hub(isolated_ledger, monkeypatch):
    client = _client()
    monkeypatch.setattr(server.db, "status", lambda: {"available": True, "reason": None})
    monkeypatch.setattr(
        server, "_chat_mail_project_key",
        lambda _name: "/home/fyc/github/agent-cockpit",
    )
    monkeypatch.setattr(
        server.db, "project_by_canonical_key",
        lambda _key: {"id": 1, "human_key": "/home/fyc/github/agent-cockpit"},
    )
    monkeypatch.setattr(
        server.db, "messages_for_canonical_project",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("未分组不得扫 Hub")),
    )
    monkeypatch.setattr(server, "_ensure_session_leader", lambda *_: {})
    monkeypatch.setattr(server, "_harvest_settled_replies", lambda *_: None)
    response = client.get("/api/chat/sessions/default/mail", headers=_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["messages"] == []
    assert body["ungrouped"] is True
    assert body["project"] is None


def test_list_chat_mail_does_not_leak_other_session_thread(isolated_ledger, monkeypatch):
    client = _client()
    workspace, first = _workspace_with_thread(client, isolated_ledger / "same-proj", "cockpit")
    monkeypatch.setattr(server, "_chat_repair_agent_mail", lambda *_: {"ok": True})
    other = client.post(
        f"/api/chat/workspaces/{workspace['id']}/bind",
        headers=_headers(),
        json={"herdr_session": "platform"},
    ).json()
    other_thread = other["thread"] if "thread" in other else other
    monkeypatch.setattr(server.db, "status", lambda: {"available": True, "reason": None})
    monkeypatch.setattr(
        server.next_profile, "require_project",
        lambda path: str(Path(path).expanduser().resolve()),
    )
    monkeypatch.setattr(
        server.db, "project_by_canonical_key",
        lambda key: {"id": 7, "human_key": key},
    )
    monkeypatch.setattr(
        server.db,
        "messages_for_canonical_project",
        lambda _pid, _limit: [
            {
                "id": 10, "sender_name": "human", "sender_program": "",
                "body_md": "这是 cockpit 的记录", "subject": "cockpit",
                "created_ts": 1_700_000_000,
                "thread_id": "cockpit",
                "recipients": [{"name": "BrownDesert"}],
            },
            {
                "id": 11, "sender_name": "BrownDesert", "sender_program": "grok",
                "body_md": "回 cockpit", "subject": "ok",
                "created_ts": 1_700_000_100,
                "thread_id": "cockpit",
                "recipients": [{"name": "human"}],
            },
            {
                "id": 12, "sender_name": "human", "sender_program": "",
                "body_md": "这是 platform 自己的", "subject": "platform",
                "created_ts": 1_700_000_200,
                "thread_id": "platform",
                "recipients": [{"name": "codex-main"}],
            },
        ],
    )
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: {"panes": []})
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    leaked = client.get(
        f"/api/chat/sessions/{other_thread['herdr_session']}/mail",
        headers=_headers(),
    )
    assert leaked.status_code == 200
    texts = [row["text"] for row in leaked.json()["messages"]]
    assert texts == ["这是 platform 自己的"]
    own = client.get("/api/chat/sessions/cockpit/mail", headers=_headers())
    assert [row["text"] for row in own.json()["messages"]] == [
        "这是 cockpit 的记录",
        "回 cockpit",
    ]


def test_send_chat_mail_persists_when_hub_fails(isolated_ledger, monkeypatch):
    client = _client()
    workspace, thread = _workspace_with_thread(client, isolated_ledger / "persist", "keep-1")
    monkeypatch.setattr(
        server.next_profile, "require_project",
        lambda path: str(Path(path).expanduser().resolve()),
    )
    monkeypatch.setattr(
        server, "_agent_mail_status",
        lambda: {"write_available": True, "available": True},
    )
    monkeypatch.setattr(
        server.hub_client, "overseer_send",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("overseer down")),
    )
    sent = client.post(
        f"/api/chat/sessions/{thread['herdr_session']}/mail",
        headers=_headers(),
        json={"text": "留下这条", "to": ["kimi-main"]},
    )
    assert sent.status_code == 200
    body = sent.json()
    assert body["message"]["text"] == "留下这条"
    assert body["message"]["to"] == ["kimi-main"]
    assert body["mail_error"]
    listed = client.get(
        f"/api/chat/sessions/{thread['herdr_session']}/mail",
        headers=_headers(),
    )
    assert listed.status_code == 200
    assert [row["text"] for row in listed.json()["messages"]] == ["留下这条"]
    assert listed.json()["messages"][0]["to"] == ["kimi-main"]


def test_ledger_only_terminal_line_skips_hub(isolated_ledger, monkeypatch):
    client = _client()
    _, thread = _workspace_with_thread(client, isolated_ledger / "term-note", "term-1")
    called = []
    monkeypatch.setattr(
        server.hub_client, "overseer_send",
        lambda **kw: called.append(kw) or {"ok": True},
    )
    sent = client.post(
        f"/api/chat/sessions/{thread['herdr_session']}/mail",
        headers=_headers(),
        json={"text": "终端里打的", "ledger_only": True},
    )
    assert sent.status_code == 200
    body = sent.json()
    assert body["message"]["text"] == "终端里打的"
    assert body["message"]["to"] == ["终端"]
    assert body["mail_error"] is None
    assert called == []
    listed = client.get(
        f"/api/chat/sessions/{thread['herdr_session']}/mail",
        headers=_headers(),
    )
    assert [row["text"] for row in listed.json()["messages"]] == ["终端里打的"]


def test_send_chat_mail_persists_neighbor_and_resolves_hub_recipient(
    isolated_ledger, monkeypatch,
):
    client = _client()
    _, thread = _workspace_with_thread(client, isolated_ledger / "neighbor", "neighbor-1")
    sent = []
    monkeypatch.setattr(
        server.next_profile, "require_project",
        lambda path: str(Path(path).expanduser().resolve()),
    )
    monkeypatch.setattr(
        server, "_agent_mail_status",
        lambda: {"write_available": True, "available": True},
    )
    monkeypatch.setattr(
        server.db, "project_by_canonical_key",
        lambda key: {"id": 7, "slug": "neighbor-project", "human_key": key},
    )
    monkeypatch.setattr(server, "_chat_repair_agent_mail", lambda *_: {"ok": True})
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    monkeypatch.setattr(
        server,
        "_herdr_runtime_snapshot",
        lambda: {"panes": [{
            "session": "neighbor-1", "pane_id": "w1:p2", "agent": "kimi",
            "mail_name": "FoggyBasin",
        }]},
    )
    monkeypatch.setattr(
        server.hub_client, "overseer_send",
        lambda **kwargs: sent.append(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(server, "_notify_chat_recipients", lambda *_a: None)

    response = client.post(
        "/api/chat/sessions/neighbor-1/mail",
        headers=_headers(),
        json={"text": "@kimi-main 看一下", "to": ["kimi-main"]},
    )
    assert response.status_code == 200
    assert response.json()["mail_error"] is None
    assert response.json()["message"]["to"] == ["kimi-main"]
    assert sent[0]["recipients"] == ["FoggyBasin"]

    listed = client.get("/api/chat/sessions/neighbor-1/mail", headers=_headers())
    assert listed.status_code == 200
    assert listed.json()["messages"][0]["to"] == ["kimi-main"]


def test_send_chat_mail_uses_overseer_and_notifies(isolated_ledger, monkeypatch):
    client = _client()
    workspace, thread = _workspace_with_thread(client, isolated_ledger / "overseer", "ping-1")
    sent = []
    notified = []
    monkeypatch.setattr(
        server.next_profile, "require_project",
        lambda path: str(Path(path).expanduser().resolve()),
    )
    monkeypatch.setattr(
        server, "_agent_mail_status",
        lambda: {"write_available": True, "available": True},
    )
    monkeypatch.setattr(
        server.db, "project_by_canonical_key",
        lambda key: {"id": 7, "slug": "home-fyc-github-agent-cockpit", "human_key": key},
    )
    monkeypatch.setattr(
        server.hub_client, "overseer_send",
        lambda **kwargs: sent.append(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(server, "_notify_chat_recipients", lambda *a: notified.append(a))
    bound = []
    monkeypatch.setattr(
        server, "_bind_mail_project",
        lambda session, path: bound.append((session, path)) or (path, "/sessions/ping-1"),
    )
    response = client.post(
        f"/api/chat/sessions/{thread['herdr_session']}/mail",
        headers=_headers(),
        json={"text": "@BrownDesert 在吗", "to": ["BrownDesert"]},
    )
    assert response.status_code == 200
    assert response.json()["mail_error"] is None
    assert sent and sent[0]["recipients"] == ["BrownDesert"]
    assert sent[0]["thread_id"] == "ping-1"
    assert notified == [("ping-1", ["BrownDesert"], "@BrownDesert 在吗")]
    assert bound and bound[0][0] == "ping-1"


def test_send_chat_mail_notifies_when_hub_rejects_recipient(isolated_ledger, monkeypatch):
    client = _client()
    workspace, thread = _workspace_with_thread(client, isolated_ledger / "scc", "scc-1")
    notified = []
    monkeypatch.setattr(
        server.next_profile, "require_project",
        lambda path: str(Path(path).expanduser().resolve()),
    )
    monkeypatch.setattr(
        server, "_agent_mail_status",
        lambda: {"write_available": True, "available": True},
    )
    monkeypatch.setattr(
        server.db, "project_by_canonical_key",
        lambda key: {"id": 5, "slug": "home-fyc-badge-all-system-service-scc", "human_key": key},
    )
    monkeypatch.setattr(
        server, "_resolve_chat_mail_recipients",
        lambda _s, dest: dest,
    )
    monkeypatch.setattr(
        server.hub_client, "overseer_send",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("Overseer 发信失败: recipients")),
    )
    monkeypatch.setattr(server, "_notify_chat_recipients", lambda *a: notified.append(a))
    monkeypatch.setattr(server, "_bind_mail_project", lambda *_a, **_k: ("/scc", "/sessions/scc-1"))
    monkeypatch.setattr(server, "_chat_repair_agent_mail", lambda *_: {"ok": True})
    response = client.post(
        f"/api/chat/sessions/{thread['herdr_session']}/mail",
        headers=_headers(),
        json={"text": "@codex 做15轮", "to": ["codex"]},
    )
    assert response.status_code == 200
    assert response.json()["mail_error"]
    assert notified == [("scc-1", ["codex"], "@codex 做15轮")]


def test_send_chat_mail_registers_missing_hub_project(isolated_ledger, monkeypatch):
    client = _client()
    workspace, thread = _workspace_with_thread(client, isolated_ledger / "new-ws", "video-1")
    ensured = []
    sent = []
    monkeypatch.setattr(
        server.next_profile, "require_project",
        lambda path: str(Path(path).expanduser().resolve()),
    )
    monkeypatch.setattr(
        server, "_agent_mail_status",
        lambda: {"write_available": True, "available": True},
    )
    monkeypatch.setattr(
        server.hub_client, "ensure_project",
        lambda key: ensured.append(key) or {"ok": True, "slug": "video-ws"},
    )
    monkeypatch.setattr(
        server.db, "project_by_canonical_key",
        lambda key: (
            None if not ensured else {"id": 9, "slug": "video-ws", "human_key": key}
        ),
    )
    monkeypatch.setattr(
        server.hub_client, "overseer_send",
        lambda **kwargs: sent.append(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(server, "_chat_repair_agent_mail", lambda *_: {"ok": True})
    monkeypatch.setattr(server, "_resolve_chat_mail_recipients", lambda _s, dest: dest)
    monkeypatch.setattr(server, "_notify_chat_recipients", lambda *_a: None)
    response = client.post(
        f"/api/chat/sessions/{thread['herdr_session']}/mail",
        headers=_headers(),
        json={"text": "ecs 代码是最新的吗", "to": ["codex-p2"]},
    )
    assert response.status_code == 200
    assert response.json()["mail_error"] is None
    assert ensured
    assert sent and sent[0]["project"] == "video-ws"


def test_open_stopped_restores_descriptor_members(isolated_ledger, monkeypatch):
    proj = isolated_ledger / "app"
    proj.mkdir()
    client = _client()
    monkeypatch.setattr(server, "_chat_repair_agent_mail", lambda *_: {"ok": True})
    workspace, thread = _workspace_with_thread(client, isolated_ledger / "restored", "stop-1")
    monkeypatch.setattr(
        server.herdr_client, "list_sessions",
        lambda: [{"name": "stop-1", "status": "stopped"}],
    )
    monkeypatch.setattr(
        server.herdr_client, "start_session",
        lambda name, workdir=None: {"available": True, "started": name, "reused": False},
    )
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: {"panes": []})
    monkeypatch.setattr(
        server.herdr_client,
        "list_session_launch_descriptors",
        lambda _s: [{"name": "codex-1", "agent": "codex", "kind": "codex", "args": []}],
    )
    launched = []
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda session, workdir, agent, **kwargs: launched.append((session, agent, kwargs.get("args")))
        or {"available": True, "pane_id": "w1:p2"},
    )
    response = client.post(
        f"/api/chat/workspaces/{workspace['id']}/open", headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["started"] is True
    assert launched == [("stop-1", "codex", "resume --last")]


def test_snapshot_merges_descriptor_members_for_stopped_session(monkeypatch):
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: {"panes": []})
    monkeypatch.setattr(
        server.herdr_client,
        "list_active_launch_descriptors",
        lambda: [
            {
                "session": "stop-1",
                "name": "codex-1",
                "kind": "codex",
                "agent": "codex",
                "pane_id": "w1:p2",
                "workdir": "/repo",
                "display_name": "CalmMarsh",
                "state": "active",
            },
            {
                "session": "stop-1",
                "name": "grok-1",
                "kind": "grok",
                "agent": "grok",
                "pane_id": "w1:p1",
                "workdir": "/repo",
                "display_name": "BrownDesert",
                "state": "active",
            },
        ],
    )
    snap = server._merge_descriptor_roster({"panes": []})
    members = [
        pane for pane in snap["panes"]
        if pane.get("session") == "stop-1" and pane.get("from_descriptor")
    ]
    assert {pane["agent"] for pane in members} == {"codex", "grok"}
    assert all(pane["agent_status"] == "stopped" for pane in members)


def test_restore_session_members_starts_each_descriptor_not_one_kind(monkeypatch):
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: {"panes": []})
    monkeypatch.setattr(
        server.herdr_client,
        "list_session_launch_descriptors",
        lambda _s: [
            {"name": "codex-1", "agent": "codex", "kind": "codex", "pane_id": "w1:p2", "args": []},
            {"name": "codex-2", "agent": "codex", "kind": "codex", "pane_id": "w1:p3", "args": []},
        ],
    )
    launched = []
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda session, workdir, agent, **kwargs: launched.append(
            (session, agent, kwargs.get("label"), kwargs.get("args")),
        ) or {"available": True, "pane_id": f"new-{len(launched)}"},
    )
    restored = server._restore_session_members("stop-1", "/repo")
    assert [row["name"] for row in restored] == ["codex-1", "codex-2"]
    assert launched == [
        ("stop-1", "codex", "codex-1", "resume --last"),
        ("stop-1", "codex", "codex-2", "resume --last"),
    ]


def test_files_and_mail_use_workspace_path(isolated_ledger, monkeypatch):
    proj = isolated_ledger / "app"
    proj.mkdir()
    (proj / "README.md").write_text("from-workspace\n", encoding="utf-8")
    client = _client()
    workspace = client.post(
        "/api/chat/workspaces",
        headers=_headers(),
        json={"path": str(proj)},
    ).json()
    client.post(
        f"/api/chat/workspaces/{workspace['id']}/threads",
        headers=_headers(),
        json={"herdr_session": "app-1"},
    )
    monkeypatch.setattr(server, "_chat_session_workdir", lambda _n: isolated_ledger / "elsewhere")
    listed = client.get("/api/chat/sessions/app-1/files", headers=_headers())
    assert listed.status_code == 200
    names = [row["name"] for row in listed.json().get("entries", [])]
    assert "README.md" in names

    sent: dict = {}
    monkeypatch.setattr(server, "_agent_mail_status", lambda: {"write_available": True})
    monkeypatch.setattr(server, "_chat_repair_agent_mail", lambda *_: {"ok": True})
    monkeypatch.setattr(
        server.next_profile, "require_project", lambda path: str(Path(path).resolve()),
    )
    monkeypatch.setattr(
        server.db, "project_by_canonical_key",
        lambda key: {"id": 1, "slug": "app-ws", "human_key": key},
    )
    monkeypatch.setattr(
        server.hub_client, "overseer_send",
        lambda **kwargs: sent.update(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(server, "_resolve_chat_mail_recipients", lambda _s, dest: dest)
    monkeypatch.setattr(server, "_notify_chat_recipients", lambda *_a: None)
    mailed = client.post(
        "/api/chat/sessions/app-1/mail",
        headers=_headers(),
        json={"text": "hello", "to": ["codex-1"]},
    )
    assert mailed.status_code == 200
    assert mailed.json()["mail_error"] is None
    assert mailed.json()["project"] == str(proj.resolve())
    assert sent["project"] == "app-ws"
    assert sent["recipients"] == ["codex-1"]
    assert sent["thread_id"] == "app-1"


def test_chat_upload_lands_in_workspace_inbox(isolated_ledger):
    client = _client()
    proj = isolated_ledger / "inbox-ws"
    workspace, thread = _workspace_with_thread(client, proj, "app-1")
    uploaded = client.post(
        f"/api/chat/sessions/{thread['herdr_session']}/files/upload",
        headers=_headers(),
        files={"file": ("shot.png", b"pngdata", "image/png")},
    )
    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["filename"] == "shot.png"
    assert body["rel"].startswith("cockpit-inbox/")
    dest = Path(body["path"])
    assert dest.read_bytes() == b"pngdata"
    assert dest.parent == proj.resolve() / "cockpit-inbox"


def test_files_hidden_without_workspace(isolated_ledger):
    listed = _client().get("/api/chat/sessions/orphan/files", headers=_headers())
    assert listed.status_code == 404


def test_delete_herdr_session_drops_thread(isolated_ledger, monkeypatch):
    proj = isolated_ledger / "app"
    proj.mkdir()
    client = _client()
    ws = client.post(
        "/api/chat/workspaces",
        headers=_headers(),
        json={"path": str(proj)},
    ).json()
    client.post(
        f"/api/chat/workspaces/{ws['id']}/threads",
        headers=_headers(),
        json={"herdr_session": "app-1"},
    )
    monkeypatch.setattr(
        server.herdr_client,
        "delete_session",
        lambda name: {"available": True, "deleted": name},
    )
    monkeypatch.setattr(server, "_mail_project_state", lambda _name: {})
    monkeypatch.setattr(server, "_attach_identity_retirement", lambda *_a, **_k: None)
    monkeypatch.setattr(server.coordination, "close_session", lambda *_a, **_k: None)
    monkeypatch.setattr(server.mail_projects, "unbind", lambda *_a, **_k: None)

    deleted = client.delete("/api/herdr/session/app-1", headers=_headers())
    assert deleted.status_code == 200
    assert deleted.json().get("deleted") == "app-1"
    listed = client.get("/api/chat/workspaces", headers=_headers())
    assert listed.json()["threads"] == []


def test_open_chat_terminal_uses_fixed_herdr_command(isolated_ledger, monkeypatch):
    workdir = isolated_ledger / "repo"
    workdir.mkdir()
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo-1", "status": "running"}],
    )
    monkeypatch.setattr(server, "_chat_session_workdir", lambda _name: workdir)
    calls = []

    def replace(cwd, *, cols, rows, label, command, env):
        calls.append((cwd, cols, rows, label, command, env))
        return {"id": "term-1", "pid": 123, "label": label}

    monkeypatch.setattr(server.terminal, "replace_labeled_term", replace)

    response = _client().post(
        "/api/chat/sessions/demo-1/terminal?cols=132&rows=40",
        headers=_headers(),
        json={"command": ["/bin/sh"]},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "term-1"
    assert len(calls) == 1
    cwd, cols, rows, label, command, env = calls[0]
    assert (cwd, cols, rows, label, command) == (
        str(workdir),
        132,
        40,
        "herdr:demo-1",
        [server.herdr_client.HERDR_BIN, "--session", "demo-1"],
    )
    assert "HERDR_CONFIG_PATH" not in env
    assert "HERDR_SESSION" not in env


def test_open_chat_terminal_rejects_invalid_or_missing_session(
    isolated_ledger, monkeypatch,
):
    monkeypatch.setattr(server.herdr_client, "list_sessions", lambda: [])
    client = _client()

    invalid = client.post(
        "/api/chat/sessions/bad.name/terminal", headers=_headers(),
    )
    missing = client.post(
        "/api/chat/sessions/missing/terminal", headers=_headers(),
    )

    assert invalid.status_code == 400
    assert missing.status_code == 404


def test_ensure_session_leader_records_first_named_agent(isolated_ledger, monkeypatch, tmp_path):
    from agent_cockpit import chat_roster

    monkeypatch.setattr(chat_roster, "LEADERS_DIR", tmp_path / "leaders")
    monkeypatch.setattr(
        server, "_herdr_runtime_snapshot",
        lambda: {"panes": [{
            "session": "cockpit",
            "pane_id": "w1:p1",
            "agent": "grok",
            "mail_name": "BrownDesert",
            "display_name": "BrownDesert",
        }]},
    )
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    row = server._ensure_session_leader("cockpit")
    assert row["leader_mail_name"] == "BrownDesert"
    assert row["leader_agent"] == "grok"
    assert chat_roster.get_session_leader("cockpit")["leader_mail_name"] == "BrownDesert"


def test_mail_list_strips_stored_tui_chrome_without_rewriting_ledger(isolated_ledger, monkeypatch):
    client = _client()
    _workspace_with_thread(client, isolated_ledger / "tui-clean", "chat-1")
    monkeypatch.setattr(server.db, "status", lambda: {"available": False})
    monkeypatch.setattr(server, "_harvest_settled_replies", lambda *_a, **_k: None)
    chrome = (
        "┃  ◆ Thought for 0.1s\n"
        "真回复还在。                    12:28 PM\n"
        "     Worked for 1m15s                    stop  [hooks: 1]\n"
    )
    kept = chat_ledger.append_message(
        "chat-1", kind="agent", sender="BrownDesert", text=chrome, to=["human"],
    )
    chat_ledger.append_message(
        "chat-1", kind="me", sender="human",
        text="正文会讨论 Worked for / Thought for / ┃ 过滤规则，这句必须保留。",
    )
    chat_ledger.append_message(
        "chat-1", kind="agent", sender="BrownDesert",
        text="     Worked for 2m57s                    stop  [hooks: 1]\n",
        to=["human"],
    )
    response = client.get("/api/chat/sessions/chat-1/mail", headers=_headers())
    assert response.status_code == 200
    texts = [row["text"] for row in response.json()["messages"]]
    assert texts == [
        "真回复还在。",
        "正文会讨论 Worked for / Thought for / ┃ 过滤规则，这句必须保留。",
    ]
    stored = {row["id"]: row["text"] for row in chat_ledger.list_messages("chat-1")}
    assert stored[kept["id"]] == chrome.strip()
    assert "Worked for" in stored[kept["id"]]


def test_extract_harvest_text_keeps_structured_sections():
    text = server._extract_harvest_text(
        "1. Gateway / Driver 修改\n\n"
        "GET /v1/video-generations/health\n"
        "POST /v1/video-generations/create\n\n"
        "2. 平台镜像修改\n\n"
        "docker compose up -d --build\n\n"
        "3. GPU 主机修改\n\n"
        "sudo ./gpu_service/deploy.sh\n"
    )
    assert "1. Gateway / Driver 修改" in text
    assert "2. 平台镜像修改" in text
    assert "sudo ./gpu_service/deploy.sh" in text


def test_extract_harvest_text_strips_overseer_shell():
    text = server._extract_harvest_text(
        "Boss 在群聊给你发了消息，请用 mail-recv --unread 领取并回复。\n"
        "好，瀑布流继续走消息，不把整屏 pane 塞回去。"
    )
    assert text == "好，瀑布流继续走消息，不把整屏 pane 塞回去。"
    assert server._extract_harvest_text("Boss 在群聊给你发了消息") == ""


def test_hub_message_rejects_foreign_session_sender():
    assert server._hub_message_in_chat(
        {"text": "15轮", "thread": "cockpit", "sender": "BrownDesert"},
        allowed_threads={"cockpit"},
        allowed_senders={"human", "browndesert"},
    )
    assert not server._hub_message_in_chat(
        {"text": "15轮", "thread": "cockpit", "sender": "codex-main"},
        allowed_threads={"cockpit"},
        allowed_senders={"human", "browndesert"},
    )


def test_same_chat_reply_detects_hub_and_harvest_duplicates():
    harvest = (
        "当前没有独立的 status=0 草稿行。画布草稿是：\n"
        "三份 graph sha256 一致：f337cdb5ac2230c4784b1d772c15d469efcd9f417380b2ebcd8da6d248eb1ecc。\n"
        "没有普通节点同时多进多出。\n"
        "全图唯一同时多进多出的是合法汇合节点 N1784001000002「初始化数据汇聚」。\n"
        "U182_DIRECTOR_CONTEXT_PACK code 入 4 出 1。\n"
        "N1782967000001 空操作-日程false汇聚 code 入 2 出 1。\n"
        "N17797766474350 Canonical成品直通 code 出 3。\n"
    )
    hub = (
        "Boss / Leader：\n"
        "已 dump 并分析 app97 当前草稿。\n"
        "三份 graph sha256 一致：f337cdb5ac2230c4784b1d772c15d469efcd9f417380b2ebcd8da6d248eb1ecc。\n"
        "**没有普通节点同时多进多出。**\n"
        "全图只有 1 个节点同时多进多出，但是合法的 variable-aggregator：N1784001000002。\n"
        "U182_DIRECTOR_CONTEXT_PACK code 入 4 / 出 1。\n"
        "N1782967000001 空操作-日程false汇聚 code 入 2 / 出 1。\n"
        "N17797766474350 Canonical成品直通 code 出 3。\n"
    )
    assert server._same_chat_reply(harvest, hub)
    assert not server._same_chat_reply("收到", "好的，我去改")
    old = "地数据库、运行目录、模型或密钥；release 使用 ECS 既有受限权限环境文件。成 `pitapat-video-platform:e0cdd8c-taskcenter-r1`。"
    new = "后台 Key 在 Gateway 容器的环境变量 ADMIN_API_KEY 中，不在 /app/api/admin_key.txt。"
    assert not server._same_harvest_copy(old, new)


def test_merge_chat_timeline_collapses_local_retries_and_hub_copy():
    text = "@grok-p2  分析下97号流程的草稿，是否有普通节点，多进多出的情况"
    merged = server._merge_chat_timeline(
        [
            {"id": "msg_a", "sender": "human", "text": text, "ts": 1},
            {"id": "msg_b", "sender": "human", "text": text, "ts": 2},
        ],
        [{"id": "3920", "sender": "human", "text": text, "ts": 3}],
    )
    assert len(merged) == 1
    assert merged[0]["text"] == text


def test_merge_chat_timeline_keeps_longer_duplicate():
    local = [{
        "id": "msg_harvest", "sender": "DarkBrook",
        "text": "没有普通节点同时多进多出。N1784001000002 初始化数据汇聚。U182_DIRECTOR_CONTEXT_PACK 入4。",
        "ts": 1,
    }]
    hub = [{
        "id": "3925", "sender": "DarkBrook",
        "text": (
            "已 dump 并分析 app97。没有普通节点同时多进多出。"
            "N1784001000002 初始化数据汇聚 入7出3。"
            "U182_DIRECTOR_CONTEXT_PACK 入4出1。"
            "N1782967000001 入2出1。详细报告在 ANALYSIS.md。"
        ),
        "ts": 2,
    }]
    merged = server._merge_chat_timeline(local, hub)
    assert len(merged) == 1
    assert merged[0]["id"] == "3925"


def test_merge_chat_timeline_keeps_later_distinct_claude_reply():
    old = (
        "agent_cockpit/ 下有 17 个 Python 测试文件，但没有 requirements.txt。"
        "前端测试全绿，后端测试跑不起来。量化对比 deepseek-harness vs agent-cockpit。"
        "已经好了 87%。剩下的 98 处硬编码 + 死代码清理。"
    )
    new = (
        "核心发现：1 个致命 bug，herdr_client 的 agent 字段永远是 null，20 分钟可修。"
        "2 个架构决策待定：单页还是多页、死代码清不清理。"
        "5 类技术债：样式硬编码、后端测试、文档过时、安全配置、性能未验证。"
        "系统完成度约 80%，卡点在后端协议适配那一个 join。"
    )
    assert not server._same_chat_reply(old, new)
    merged = server._merge_chat_timeline(
        [
            {"id": "msg_old", "sender": "GrayFalcon", "text": old, "ts": 1},
            {"id": "msg_new", "sender": "GrayFalcon", "text": new, "ts": 2},
        ],
        [],
    )
    assert [row["id"] for row in merged] == ["msg_old", "msg_new"]


def test_same_chat_reply_treats_wrap_variants_as_one_harvest():
    a = (
        "agent_cockpit/ 下有 17 个 Python 测试文件，但没有 requirements.txt。"
        "前端测试全绿。已经好了 87%。剩下的 98 处硬编码 + 死代码清理。"
        "量化对比 deepseek-harness vs agent-cockpit，令牌纯度 54%。"
    )
    b = (
        "量化对比 deepseek-harness vs agent-cockpit，令牌纯度 54%。"
        "agent_cockpit/ 下有 17 个 Python 测试文件，但没有 requirements.txt。"
        "前端测试全绿。已经好了 87%。剩下的 98 处硬编码 + 死代码清理。"
    )
    assert server._same_chat_reply(a, b)


def test_extract_harvest_text_unwraps_narrow_terminal_wrap():
    text = server._extract_harvest_text(
        "  ¥0.00000000\n"
        "  当前 WS 能收到 tts\n"
        "  start，但没有回复正\n"
        "  文，也未创建 Workflow\n"
        "  Run。因此这是生成链路\n"
        "  阻断。\n"
        "  - 有效完成：0/15\n"
        "  - Provider 调用：0\n"
        "  1. WS 连接正常，能收到\n"
        "  hello、ready_new 和\n"
        "  角色信息。\n"
    )
    assert "¥0.00000000" in text
    assert not text.startswith("¥0.00000000当前")
    assert "当前 WS 能收到 tts start，但没有回复正文，也未创建 Workflow Run。因此这是生成链路阻断。" in text
    assert "- 有效完成：0/15" in text
    assert "- Provider 调用：0" in text
    assert "1. WS 连接正常，能收到 hello、ready_new 和角色信息。" in text


def test_extract_harvest_text_strips_hash_and_memory_recap():
    text = server._extract_harvest_text(
        "58d539ed3e4d42f6b00653046c5f43204a2d632d6c308ba0d`。\n"
        "        attempt 计为有效轮次。\n"
        "• 共享记忆 doctor 报了 1 个新记录格式错误。\n"
        "• 已将结论打印到终端，群聊瀑布流可直接收取。\n"
        "卡在 WS 网关接受消息之后、工作流真正启动之前。\n"
        "Provider 调用为 0。\n"
    )
    assert "卡在 WS 网关" in text
    assert "Provider 调用为 0" in text
    assert "58d539ed" not in text
    assert "共享记忆 doctor" not in text
    assert "已将结论打印" not in text


def test_extract_harvest_text_strips_codex_recap_diff():
    text = server._extract_harvest_text(
        "32 +- 当前草稿与最新发布图一致。\n"
        "    33\n"
        "       ⋮\n"
        "    36 +- 尚未制作修复 Candidate。\n"
        "• Edited tools/m2her-verify/handoffs/report.md (+2 -0)\n"
        "    19 +静态校验实际输出：BLOCKED\n"
        "    21  ## 预期断言\n"
        "     8  duration: 约 15 分钟\n"
        "✔ 查询 app97 最新草稿版本并冻结只读 Graph 身份\n"
        "• Ran python3 tools/agent-memory-doctor.py\n"
        "  └ ERROR session-outcome\n"
        "• Updated Plan\n"
        "› Run /review on my current changes\n"
        "普通节点多进：2 个\n"
        "同一个普通节点同时多进多出：0 个\n"
    )
    assert "普通节点多进：2 个" in text
    assert "同时多进多出：0" in text
    assert "Edited tools" not in text
    assert "Updated Plan" not in text
    assert "32 +-" not in text
    assert "Run /review" not in text
    assert "预期断言" not in text
    assert "duration:" not in text
    assert "冻结只读" not in text
    assert "Full Access" not in text


def test_extract_harvest_text_strips_prompt_and_git_chrome():
    text = server._extract_harvest_text(
        "   master ~/badge/all_system/service/scc                          142K / 500K\n"
        "     ❯ Boss 在群聊给你发了消息。请直接做下面的任务，结论写在终端，群聊会收进瀑布流。\n"
        "       本群 Leader 是 DarkBrook。需要写信时用 mail-send --to leader --thread scc-1，不要写 grok-main / 程序-main。\n"
        "没有普通节点同时多进多出。\n"
        "全图唯一多进多出是 aggregator 初始化数据汇聚。\n"
    )
    assert "没有普通节点同时多进多出" in text
    assert "初始化数据汇聚" in text
    assert "Boss 在群聊" not in text
    assert "" not in text
    assert "142K" not in text
    assert "mail-send" not in text


def test_extract_harvest_text_restores_box_table():
    text = server._extract_harvest_text(
        "正式画像：流程自己总结\n"
        "┌──────────┬──────────┬────────────┐\n"
        "│ 产物     │ 节点     │ 写入缓存   │\n"
        "├──────────┼──────────┼────────────┤\n"
        "│ 角色核心（四节） │ 自动生成精简角色核心 → C007 去动作种子 │ mbti_profile │\n"
        "│ 用户核心 + 组合差异 │ 自动生成用户核心与组合差异 │ behavior_contract │\n"
        "└──────────┴──────────┴────────────┘\n"
        "hash 没变就不重跑。\n"
    )
    assert "| 产物 | 节点 | 写入缓存 |" in text
    assert "| 角色核心（四节） | 自动生成精简角色核心 → C007 去动作种子 | mbti_profile |" in text
    assert "behavior_contract" in text
    assert "┌" not in text
    assert "…▲" not in text
    served = server._ledger_chat_mail({
        "id": "msg_table", "session": "scc-1", "kind": "agent",
        "sender": "DarkBrook", "text": text, "to": ["human"], "ts": 1,
    })
    assert served is not None
    assert "| 产物 | 节点 | 写入缓存 |" in served["text"]
    assert "| --- | --- | --- |" in served["text"]
    assert "| 产物 | 节点 | 写入缓存 || ---" not in served["text"]


def test_extract_harvest_text_drops_leftover_identity_inject():
    dumped = (
        "agent-mail-tools/mail-recv --agent\n"
        "  codex --instance main --project /\n"
        "  home/fyc/github/agent-cockpit\n"
        "  --unread。协作通信约定:长任务每完成一个里程碑检查一次未读消息；\n"
        "  注册:花名=codex-luna-agent-cockpit,项\n"
        "  目=/home/fyc/github/agent-cockpit。发\n"
        "作。\n"
    )
    assert server._extract_harvest_text(dumped) == ""
    served = server._ledger_chat_mail({
        "id": "msg_id", "session": "cockpit", "kind": "agent",
        "sender": "EmeraldCave", "text": dumped, "to": ["human"], "ts": 1,
    })
    assert served is None
    kept = server._extract_harvest_text(
        "瀑布流已经加大。请硬刷新 8790。\n"
        "下一步再看终端滑动。\n"
    )
    assert "瀑布流已经加大" in kept
    wrapped = (
        '--instance main --project /home/fyc/github/agent-cockpit --to <花名> '
        '--subject "..." --body "...";收消息: /home/fyc/github/agent-cockpit/'
        'agent-mail-tools/mail-recv --agent codex --instance\n'
        '  main --project /home/fyc/github/agent-cockpit --unread。'
        '协作通信约定:长任务每完成一个里程碑检查一次未读消息；\n'
    )
    assert server._extract_harvest_text(wrapped) == ""
    assert server._ledger_chat_mail({
        "id": "msg_wrap", "session": "cockpit", "kind": "agent",
        "sender": "EmeraldCave", "text": wrapped, "to": ["human"], "ts": 2,
    }) is None


def test_extract_harvest_text_keeps_diagnosis_about_leftover_identity():
    text = server._extract_harvest_text(
        "…\n"
        "这不是在干活的 Codex。这是 leftover 壳在空转。\n"
        "屏幕上的花名=codex-luna-agent-cockpit 和 --instance main 是旧身份，不是任务结论。\n"
        "UserPromptSubmit hook 失败是因为缺环境变量。瀑布流空是因为这些壳被藏掉了。\n"
        "• UserPromptSubmit hook (failed)\n"
        "error: hook exited with code 1\n"
        "❯\n"
        "post_tool_use\n"
    )
    assert "这不是在干活的 Codex" in text
    assert "旧身份" in text
    assert "瀑布流空" in text
    assert "hook (failed)" not in text
    assert "hook exited" not in text
    assert "post_tool_use" not in text
    assert "❯" not in text
    assert not text.lstrip().startswith("…")


def test_extract_harvest_text_strips_grok_tui_chrome():
    text = server._extract_harvest_text(
        "┃  ◆ Run Assign Codex workspace dims follow-up and run doctor  [hooks: 2]\n"
        "     ◆ Thought for 0.1s\n"
        "     #3826 已领。PC 全屏算出 516 列，后端上限是 500，所以直接拒了。                    12:28 PM\n"
        "     Worked for 1m15s                                                                    stop  [hooks: 1]\n"
        "  Shift+Tab:mode  │  Ctrl+x:shortcuts\n"
    )
    assert text == "#3826 已领。PC 全屏算出 516 列，后端上限是 500，所以直接拒了。"
    assert "Worked for" not in text
    assert "Thought for" not in text
    assert "┃" not in text


def test_strip_harvest_tui_chrome_preserves_real_reply_verbatim():
    text = server._strip_harvest_tui_chrome(
        "第一段是真回复。\n\n"
        "正文会讨论 Worked for / Thought for / ┃ 过滤规则，这句必须保留。\n"
        "     Worked for 2m57s                    stop  [hooks: 1]\n"
        "  Shift+Tab:mode  │  Ctrl+x:shortcuts\n"
    )
    assert text == (
        "第一段是真回复。\n\n"
        "正文会讨论 Worked for / Thought for / ┃ 过滤规则，这句必须保留。"
    )


def test_strip_harvest_tui_chrome_removes_pure_shell_and_inline_clock():
    text = server._strip_harvest_tui_chrome(
        "┃  ◆ Run Validate records  [hooks: 2]\n"
        "     ◆ Thought for 0.0s\n"
        "真回复仍在。                                      1:57 AM   █\n"
        "     Worked for 1m15s                    stop  [hooks: 1]\n"
        "  Space:prompt  │  Enter:open  │  Ctrl+x:shortcuts\n"
    )
    assert text == "真回复仍在。"


def test_harvest_settled_reply_is_one_message_not_live_pane(isolated_ledger, monkeypatch):
    client = _client()
    _workspace_with_thread(client, isolated_ledger / "harvest", "chat-1")
    server._PANE_LAST_STATUS.clear()
    panes = [{
        "session": "chat-1",
        "pane_id": "w1:p2",
        "agent": "grok",
        "agent_status": "working",
        "mail_name": "BrownDesert",
        "display_name": "BrownDesert",
    }]
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: {"panes": panes})
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    monkeypatch.setattr(
        server.herdr_client,
        "pane_summary",
        lambda *_a, **_k: {"summary": "好，瀑布流继续走消息，不把整屏 pane 塞回去。"},
    )
    monkeypatch.setattr(server.db, "status", lambda: {"available": False})
    first = client.get("/api/chat/sessions/chat-1/mail", headers=_headers())
    assert first.status_code == 200
    assert first.json()["messages"] == []
    server._harvest_settled_replies("chat-1")
    mid = client.get("/api/chat/sessions/chat-1/mail", headers=_headers())
    assert [row["text"] for row in mid.json()["messages"]] == [
        "好，瀑布流继续走消息，不把整屏 pane 塞回去。",
    ]
    panes[0]["agent_status"] = "idle"
    server._harvest_settled_replies("chat-1")
    second = client.get("/api/chat/sessions/chat-1/mail", headers=_headers())
    messages = second.json()["messages"]
    assert [row["text"] for row in messages] == ["好，瀑布流继续走消息，不把整屏 pane 塞回去。"]
    assert [row["sender"] for row in messages] == ["BrownDesert"]
    assert all(not str(row["id"]).startswith("pane:") for row in messages)
    third = client.get("/api/chat/sessions/chat-1/mail", headers=_headers())
    assert [row["text"] for row in third.json()["messages"]] == [
        "好，瀑布流继续走消息，不把整屏 pane 塞回去。",
    ]


def test_harvest_idle_reply_without_working_edge(isolated_ledger, monkeypatch):
    client = _client()
    _workspace_with_thread(client, isolated_ledger / "harvest-idle", "plat-1")
    server._PANE_LAST_STATUS.clear()
    server._PANE_LAST_HARVEST.clear()
    server._HARVEST_STATUS_LOADED = True
    server._PANE_LAST_STATUS[("plat-1", "w1:p2")] = "idle"
    panes = [{
        "session": "plat-1",
        "pane_id": "w1:p2",
        "agent": "codex",
        "agent_status": "idle",
        "mail_name": "DarkGlacier",
        "display_name": "codex-1",
    }]
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: {"panes": panes})
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    monkeypatch.setattr(
        server.herdr_client,
        "pane_summary",
        lambda *_a, **_k: {"summary": "后台 Key 在 Gateway 容器的环境变量 ADMIN_API_KEY 中。"},
    )
    monkeypatch.setattr(server.db, "status", lambda: {"available": False})
    server._harvest_settled_replies("plat-1")
    first = client.get("/api/chat/sessions/plat-1/mail", headers=_headers())
    assert [row["text"] for row in first.json()["messages"]] == [
        "后台 Key 在 Gateway 容器的环境变量 ADMIN_API_KEY 中。",
    ]
    second = client.get("/api/chat/sessions/plat-1/mail", headers=_headers())
    assert len(second.json()["messages"]) == 1


def test_harvest_working_updates_same_message(isolated_ledger, monkeypatch):
    client = _client()
    _workspace_with_thread(client, isolated_ledger / "harvest-live", "chat-2")
    server._PANE_LAST_STATUS.clear()
    server._PANE_LAST_HARVEST.clear()
    server._HARVEST_STATUS_LOADED = True
    panes = [{
        "session": "chat-2",
        "pane_id": "w1:p1",
        "agent": "grok",
        "agent_status": "working",
        "mail_name": "BrownDesert",
        "display_name": "BrownDesert",
    }]
    drafts = iter((
        {"summary": "这是第一稿，结论还没写完。"},
        {"summary": "这是第一稿，结论还没写完。后面补上完整结论，瀑布流应更新同一条。"},
    ))
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: {"panes": panes})
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    monkeypatch.setattr(server.herdr_client, "pane_summary", lambda *_a, **_k: next(drafts))
    monkeypatch.setattr(server.db, "status", lambda: {"available": False})
    server._harvest_settled_replies("chat-2")
    first = client.get("/api/chat/sessions/chat-2/mail", headers=_headers()).json()["messages"]
    assert [row["text"] for row in first] == ["这是第一稿，结论还没写完。"]
    server._harvest_settled_replies("chat-2")
    second = client.get("/api/chat/sessions/chat-2/mail", headers=_headers()).json()["messages"]
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert "完整结论" in second[0]["text"]
