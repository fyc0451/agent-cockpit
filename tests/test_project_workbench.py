from fastapi.testclient import TestClient
import pytest

from agent_cockpit import mail_projects
import server


def _project(project_dir):
    return {
        "id": 7,
        "slug": "demo",
        "human_key": str(project_dir),
        "created_at": 123.0,
        "archived_at": None,
    }


def _setup(monkeypatch, project_dir, assignments=None):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server, "_agent_mail_status", lambda: {"available": True, "reason": None},
    )
    monkeypatch.setattr(server.db, "project_by_slug", lambda _slug: _project(project_dir))
    monkeypatch.setattr(
        server.coordination,
        "list_assignments",
        lambda project_key: list(assignments or []),
    )
    return TestClient(server.app)


def _get(client):
    return client.get(
        "/api/projects/demo/workbench",
        headers={"authorization": "Bearer secret"},
    )


def test_workbench_returns_503_when_agent_mail_is_unreadable(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server,
        "_agent_mail_status",
        lambda: {"available": False, "reason": "mail unavailable"},
    )

    response = _get(TestClient(server.app))

    assert response.status_code == 503
    assert response.json() == {"detail": "mail unavailable"}


def test_workbench_returns_503_when_agent_mail_query_fails(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server, "_agent_mail_status", lambda: {"available": True, "reason": None},
    )
    monkeypatch.setattr(
        server.db,
        "project_by_slug",
        lambda _slug: (_ for _ in ()).throw(OSError("private database path")),
    )

    response = _get(TestClient(server.app, raise_server_exceptions=False))

    assert response.status_code == 503
    assert response.json() == {"detail": "Agent Mail 查询失败"}
    assert "private database path" not in response.text


def test_workbench_returns_404_for_unknown_project(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server, "_agent_mail_status", lambda: {"available": True, "reason": None},
    )
    monkeypatch.setattr(server.db, "project_by_slug", lambda _slug: None)

    response = _get(TestClient(server.app))

    assert response.status_code == 404


def test_workbench_returns_503_when_assignment_query_fails(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = _setup(monkeypatch, project_dir)
    monkeypatch.setattr(
        server.coordination,
        "list_assignments",
        lambda _project_key: (_ for _ in ()).throw(OSError("private db path")),
    )

    response = _get(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "Coordination 查询失败"}
    assert "private db path" not in response.text


def test_workbench_only_returns_exactly_bound_project_sessions(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    other_project = tmp_path / "other-project"
    target_session_dir = tmp_path / "target-session"
    other_session_dir = tmp_path / "other-session"
    for path in (project_dir, other_project, target_session_dir, other_session_dir):
        path.mkdir()
    mail_projects.bind("target", str(target_session_dir), str(project_dir))
    mail_projects.bind("other", str(other_session_dir), str(other_project))
    assignments = [{
        "assignment_id": "a-1",
        "project_key": str(project_dir),
        "assignment": "Implement the API",
        "assignee": "opencode",
        "expected_reply": None,
        "deadline": None,
        "status": "in_progress",
        "closed_at": None,
        "version": 2,
        "created_at": 100.0,
        "updated_at": 110.0,
    }]
    client = _setup(monkeypatch, project_dir, assignments)
    monkeypatch.setattr(
        server,
        "_herdr_runtime_snapshot",
        lambda: {
            "available": True,
            "degraded": False,
            "sessions": [
                {
                    "session": "target",
                    "directory": str(target_session_dir),
                    "status": "running",
                    "focused_pane_id": "w1:p1",
                    "panes": [{
                        "pane_id": "w1:p1",
                        "agent": "opencode",
                        "agent_status": "working",
                        "focused": True,
                        "revision": 9,
                        "cwd": "/secret/worktree",
                    }],
                },
                {
                    "session": "other",
                    "directory": str(other_session_dir),
                    "status": "running",
                    "focused_pane_id": "w2:p1",
                    "panes": [{"pane_id": "w2:p1", "agent": "codex"}],
                },
            ],
        },
    )

    response = _get(client)

    assert response.status_code == 200
    body = response.json()
    assert body["project"] == {"id": 7, "slug": "demo", "created_at": 123.0}
    assert body["assignments"] == [{
        "assignment_id": "a-1",
        "assignment": "Implement the API",
        "assignee": "opencode",
        "expected_reply": None,
        "deadline": None,
        "status": "in_progress",
        "closed_at": None,
        "version": 2,
        "created_at": 100.0,
        "updated_at": 110.0,
    }]
    assert body["sessions"] == [{
        "session": "target",
        "status": "running",
        "focused_pane_id": "w1:p1",
        "panes": [{
            "pane_id": "w1:p1",
            "agent": "opencode",
            "agent_status": "working",
            "focused": True,
            "revision": 9,
        }],
    }]
    assert body["source"]["available"] is True
    assert body["source"]["degraded"] is False
    assert isinstance(body["source"]["observed_at"], float)


def test_workbench_rejects_stale_session_generation(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    old_session_dir = tmp_path / "old-session"
    current_session_dir = tmp_path / "current-session"
    for path in (project_dir, old_session_dir, current_session_dir):
        path.mkdir()
    mail_projects.bind("demo", str(old_session_dir), str(project_dir))
    client = _setup(monkeypatch, project_dir)
    monkeypatch.setattr(
        server,
        "_herdr_runtime_snapshot",
        lambda: {
            "available": True,
            "degraded": False,
            "sessions": [{
                "session": "demo",
                "directory": str(current_session_dir),
                "status": "running",
                "panes": [{
                    "pane_id": "w1:p1",
                    "agent": "opencode",
                    "cwd": str(project_dir),
                }],
            }],
        },
    )

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["sessions"] == []


@pytest.mark.parametrize(
    "snapshot",
    [
        {
            "available": False,
            "sessions": [{"session": "secret", "directory": "/secret"}],
            "error": "token=raw-secret",
        },
        {
            "available": True,
            "degraded": True,
            "sessions": [{"session": "cached", "directory": "/secret"}],
            "reason": "authorization Bearer raw-secret",
        },
    ],
)
def test_workbench_degrades_without_runtime_sessions(
    monkeypatch, tmp_path, snapshot,
):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    assignments = [{
        "assignment_id": "a-1",
        "project_key": str(project_dir),
        "assignment": "Durable work",
        "assignee": "opencode",
        "expected_reply": None,
        "deadline": None,
        "status": "assigned",
        "closed_at": None,
        "version": 1,
        "created_at": 1.0,
        "updated_at": 1.0,
    }]
    client = _setup(monkeypatch, project_dir, assignments)
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: snapshot)

    response = _get(client)

    assert response.status_code == 200
    body = response.json()
    assert body["project"] == {"id": 7, "slug": "demo", "created_at": 123.0}
    assert len(body["assignments"]) == 1
    assert body["sessions"] == []
    assert body["source"] == {
        "available": snapshot["available"],
        "degraded": True,
        "observed_at": body["source"]["observed_at"],
    }
    assert "raw-secret" not in response.text


def test_workbench_snapshot_failure_is_degraded_without_raw_error(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    client = _setup(monkeypatch, project_dir)
    monkeypatch.setattr(
        server,
        "_herdr_runtime_snapshot",
        lambda: (_ for _ in ()).throw(OSError("token=raw-secret")),
    )

    response = _get(client)

    assert response.status_code == 200
    assert response.json()["sessions"] == []
    assert response.json()["source"]["available"] is False
    assert response.json()["source"]["degraded"] is True
    assert "raw-secret" not in response.text


def test_workbench_response_uses_strict_field_allowlists(monkeypatch, tmp_path):
    project_dir = tmp_path / "project"
    session_dir = tmp_path / "session"
    project_dir.mkdir()
    session_dir.mkdir()
    mail_projects.bind("demo", str(session_dir), str(project_dir))
    client = _setup(monkeypatch, project_dir)
    monkeypatch.setattr(
        server,
        "_herdr_runtime_snapshot",
        lambda: {
            "available": True,
            "degraded": False,
            "token": "top-secret",
            "sessions": [{
                "session": "demo",
                "directory": str(session_dir),
                "status": "running",
                "focused_pane_id": "w1:p1",
                "raw_error": "session-secret",
                "agents": [{"registration_token": "agent-secret"}],
                "panes": [{
                    "pane_id": "w1:p1",
                    "agent": "opencode",
                    "agent_status": "working",
                    "focused": True,
                    "revision": 4,
                    "cwd": "/private/project",
                    "title": "private-title",
                    "mail_name": "PrivateName",
                    "agent_session": "resume-secret",
                    "tokens": {"resume": "pane-secret"},
                }],
            }],
        },
    )

    response = _get(client)

    assert response.status_code == 200
    assert set(response.json()) == {"project", "assignments", "sessions", "source"}
    assert set(response.json()["project"]) == {"id", "slug", "created_at"}
    assert set(response.json()["sessions"][0]) == {
        "session", "status", "focused_pane_id", "panes",
    }
    assert set(response.json()["sessions"][0]["panes"][0]) == {
        "pane_id", "agent", "agent_status", "focused", "revision",
    }
    for secret in (
        "top-secret", "session-secret", "agent-secret", "private-title",
        "PrivateName", "resume-secret", "pane-secret", str(session_dir),
        str(project_dir),
    ):
        assert secret not in response.text
