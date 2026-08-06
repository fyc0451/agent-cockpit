import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import mail_projects
import server


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def test_binding_is_scoped_by_session_name_and_directory(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    project = tmp_path / "project"
    for path in (first, second, project):
        path.mkdir()

    mail_projects.bind("demo", str(first), str(project))

    assert mail_projects.get("demo", str(first)) == str(project)
    assert mail_projects.get("demo", str(second)) is None


def test_recreated_session_can_replace_stale_generation_but_live_rebind_is_explicit(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    one = tmp_path / "one"
    two = tmp_path / "two"
    for path in (first, second, one, two):
        path.mkdir()
    mail_projects.bind("demo", str(first), str(one))

    with pytest.raises(ValueError, match="已绑定"):
        mail_projects.bind("demo", str(first), str(two))

    assert mail_projects.bind("demo", str(first), str(two), replace=True) == str(two)
    assert mail_projects.bind("demo", str(second), str(one)) == str(one)
    assert mail_projects.get("demo", str(first)) is None
    assert mail_projects.get("demo", str(second)) == str(one)


def test_deleting_session_clears_even_a_stale_same_name_binding(monkeypatch, tmp_path):
    old_session = tmp_path / "old"
    project = tmp_path / "project"
    old_session.mkdir()
    project.mkdir()
    mail_projects.bind("demo", str(old_session), str(project))
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.herdr_client,
        "delete_session",
        lambda name: {"available": True, "deleted": name},
    )

    response = TestClient(server.app).delete(
        "/api/herdr/session/demo", headers={"authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    assert mail_projects.get("demo", str(old_session)) is None


def test_state_file_contains_no_registry_secret(tmp_path):
    session_dir = tmp_path / "session"
    project = tmp_path / "project"
    session_dir.mkdir()
    project.mkdir()

    mail_projects.bind("demo", str(session_dir), str(project))

    state = json.loads(mail_projects.STATE_PATH.read_text(encoding="utf-8"))
    assert state == {
        "version": 1,
        "sessions": {
            "demo": {"session_dir": str(session_dir), "project": str(project)}
        },
    }


def test_canonical_project_uses_main_worktree_root(tmp_path):
    repo = tmp_path / "repo"
    worktree = tmp_path / "review"
    repo.mkdir()
    _init_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree)],
        check=True,
        capture_output=True,
    )

    assert server._canonical_mail_project(worktree) == str(repo.resolve())


def test_nested_independent_clone_is_not_merged_with_parent_clone(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "independent"
    outer.mkdir()
    _init_repo(outer)
    inner.mkdir()
    _init_repo(inner)

    assert server._same_mail_project_family(str(outer), str(inner)) is False


def test_old_session_with_multiple_registered_candidates_requires_selection(
    monkeypatch, tmp_path,
):
    session_dir = tmp_path / "session"
    subdir = session_dir / "sub"
    subdir.mkdir(parents=True)
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(session_dir)}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "snapshot",
        lambda: {
            "sessions": [{
                "session": "demo",
                "directory": str(session_dir),
                "panes": [{"pane_id": "w1:p1", "cwd": str(subdir)}],
            }]
        },
    )
    monkeypatch.setattr(server.db, "status", lambda: {"available": True, "reason": None})
    monkeypatch.setattr(
        server.db,
        "list_projects",
        lambda: [
            {"id": 1, "slug": "one", "human_key": str(session_dir)},
            {"id": 2, "slug": "two", "human_key": str(subdir)},
        ],
    )

    state = server._mail_project_state("demo")

    assert state["bound"] is False
    assert state["needs_selection"] is True
    assert [item["human_key"] for item in state["candidates"]] == [
        str(session_dir), str(subdir)
    ]


def test_old_session_auto_adopts_only_registered_candidate(monkeypatch, tmp_path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(session_dir)}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "snapshot",
        lambda: {"sessions": [{"session": "demo", "panes": []}]},
    )
    monkeypatch.setattr(server.db, "status", lambda: {"available": True, "reason": None})
    monkeypatch.setattr(
        server.db,
        "list_projects",
        lambda: [{"id": 1, "slug": "demo", "human_key": str(session_dir)}],
    )

    state = server._mail_project_state("demo")

    assert state["bound"] is True
    assert state["project"] == str(session_dir)
    assert state["migrated"] is True
    assert mail_projects.get("demo", str(session_dir)) == str(session_dir)


def test_zero_candidate_suggests_unique_pane_project_not_herdr_state_dir(
    monkeypatch, tmp_path,
):
    state_dir = tmp_path / "herdr-state"
    project = tmp_path / "project"
    state_dir.mkdir()
    project.mkdir()
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(state_dir)}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "snapshot",
        lambda: {"sessions": [{
            "session": "demo", "panes": [{"pane_id": "w1:p1", "cwd": str(project)}],
        }]},
    )
    monkeypatch.setattr(server.db, "status", lambda: {"available": True, "reason": None})
    monkeypatch.setattr(server.db, "list_projects", lambda: [])

    state = server._mail_project_state("demo")

    assert state["needs_selection"] is True
    assert state["candidates"] == []
    assert state["suggested_project"] == str(project)


def test_archived_stale_binding_is_replaced_by_unique_active_candidate(
    monkeypatch, tmp_path,
):
    state_dir = tmp_path / "herdr-state"
    archived = tmp_path / "archived"
    active = tmp_path / "active"
    for path in (state_dir, archived, active):
        path.mkdir()
    mail_projects.bind("demo", str(state_dir), str(archived))
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(state_dir)}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "snapshot",
        lambda: {"sessions": [{
            "session": "demo", "panes": [{"pane_id": "w1:p1", "cwd": str(active)}],
        }]},
    )
    monkeypatch.setattr(server.db, "status", lambda: {"available": True, "reason": None})
    monkeypatch.setattr(
        server.db,
        "list_projects",
        lambda: [{"id": 2, "slug": "active", "human_key": str(active)}],
    )

    state = server._mail_project_state("demo")

    assert state["binding_invalidated"] is True
    assert state["migrated"] is True
    assert state["project"] == str(active)
    assert mail_projects.get("demo", str(state_dir)) == str(active)


def test_identity_endpoint_uses_bound_project_not_pane_cwd(monkeypatch, tmp_path):
    project = tmp_path / "project"
    worktree = tmp_path / "worktree"
    session_dir = tmp_path / "session"
    for path in (project, worktree, session_dir):
        path.mkdir()
    mail_projects.bind("demo", str(session_dir), str(project))
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.db, "status", lambda: {"available": True, "reason": None})
    monkeypatch.setattr(
        server.db,
        "list_projects",
        lambda: [{"id": 1, "slug": "project", "human_key": str(project)}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(session_dir)}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "snapshot",
        lambda: {"panes": [{
            "session": "demo", "pane_id": "w1:p1", "cwd": str(worktree), "agent": "codex",
        }]},
    )
    seen = []
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda cwd, agent: seen.append((cwd, agent)) or {
            "name": "SilverPine", "program": "codex", "model": "gpt", "human_key": cwd,
        },
    )

    response = TestClient(server.app).get(
        "/api/herdr/pane/demo/w1:p1/identity",
        headers={"authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json()["project_key"] == str(project)
    assert "--to SilverPine" in response.json()["mail_hint"]
    assert seen == [(str(project), "codex")]


def test_identity_endpoint_requests_project_selection_instead_of_guessing(
    monkeypatch, tmp_path,
):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(session_dir)}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "snapshot",
        lambda: {"panes": [{
            "session": "demo", "pane_id": "w1:p1", "cwd": str(session_dir), "agent": "codex",
        }]},
    )
    monkeypatch.setattr(server.db, "status", lambda: {"available": True, "reason": None})
    monkeypatch.setattr(server.db, "list_projects", lambda: [])
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda *_: (_ for _ in ()).throw(AssertionError("不能按 pane cwd 猜身份")),
    )

    response = TestClient(server.app).get(
        "/api/herdr/pane/demo/w1:p1/identity",
        headers={"authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json()["found"] is False
    assert response.json()["needs_project"] is True


def test_init_mail_explicitly_binds_and_passes_project_argument(monkeypatch, tmp_path):
    project = tmp_path / "project"
    session_dir = tmp_path / "session"
    project.mkdir()
    session_dir.mkdir()
    init_script = tmp_path / "am-init-project"
    init_script.touch()
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server, "AGENT_MAIL_INIT_SCRIPT", init_script)
    monkeypatch.setattr(
        server, "_canonical_mail_project", lambda path: str(path.resolve())
    )
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(session_dir)}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "snapshot",
        lambda: {"sessions": [{
            "session": "demo",
            "panes": [{"pane_id": "w1:p1", "cwd": str(session_dir), "agent": "claude"}],
        }]},
    )
    calls = []
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)) or subprocess.CompletedProcess(args, 0, "ok", ""),
    )
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda cwd, agent: {
            "name": "AmberLake", "program": agent, "model": "", "human_key": cwd,
        },
    )
    sent = []
    monkeypatch.setattr(
        server.herdr_client,
        "pane_send",
        lambda *args: sent.append(args) or {"available": True},
    )

    response = TestClient(server.app).post(
        "/api/herdr/session/demo/init-mail",
        headers={"authorization": "Bearer secret"},
        json={"project": str(project)},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["project"] == str(project)
    init_call = next(call for call in calls if call[0][0] == str(init_script))
    assert init_call[0] == [str(init_script), "--project", str(project)]
    assert init_call[1]["cwd"] == str(project)
    assert mail_projects.get("demo", str(session_dir)) == str(project)
    assert str(project) in sent[0][2]


def test_missing_registered_identity_is_not_fabricated(monkeypatch):
    monkeypatch.setattr(server.db, "identity_by_cwd", lambda *_: None)

    assert server._identity_name("/project", "claude") is None


def test_setup_binds_canonical_project_before_registration(monkeypatch, tmp_path):
    workdir = tmp_path / "worktree"
    canonical = tmp_path / "main"
    session_dir = tmp_path / "session"
    for path in (workdir, canonical, session_dir):
        path.mkdir()
    init_script = tmp_path / "am-init-project"
    init_script.touch()
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server, "_agent_mail_requirement", lambda: None)
    monkeypatch.setattr(server, "AGENT_MAIL_INIT_SCRIPT", init_script)
    monkeypatch.setattr(server, "_canonical_mail_project", lambda _: str(canonical))
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(session_dir)}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda *args, **kwargs: {"available": True, "pane_id": "w1:p1"},
    )
    monkeypatch.setattr(
        server.herdr_client,
        "snapshot",
        lambda: {"sessions": [{
            "session": "demo",
            "panes": [{"pane_id": "w1:p1", "agent": "codex", "cwd": str(workdir)}],
        }]},
    )
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda cwd, agent: {
            "name": "SilverPine", "program": agent, "model": "", "human_key": cwd,
        },
    )
    monkeypatch.setattr(server.herdr_client, "pane_send", lambda *_: {"available": True})
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)
    calls = []

    def run(args, **kwargs):
        assert mail_projects.get("demo", str(session_dir)) == str(canonical)
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(server.subprocess, "run", run)

    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={"session": "demo", "workdir": str(workdir), "agents": ["codex"]},
    )

    assert response.status_code == 200
    assert response.json()["mail_project"] == str(canonical)
    assert calls[0][0] == [str(init_script), "--project", str(canonical)]
    assert calls[0][1]["cwd"] == str(canonical)


def test_setup_rejects_project_conflict_using_real_herdr_session_dir(
    monkeypatch, tmp_path,
):
    requested = tmp_path / "requested"
    bound = tmp_path / "bound"
    session_dir = tmp_path / "herdr-state"
    for path in (requested, bound, session_dir):
        path.mkdir()
    mail_projects.bind("demo", str(session_dir), str(bound))
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server, "_agent_mail_requirement", lambda: None)
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [{"name": "demo", "status": "running", "directory": str(session_dir)}],
    )
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("绑定冲突时不能启动 agent")
        ),
    )

    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={"session": "demo", "workdir": str(requested), "agents": ["codex"]},
    )

    assert response.status_code == 409
    assert str(bound) in response.json()["detail"]
