import json
import os
import time

import pytest

from agent_cockpit import team_sessions


def _bind(**overrides):
    values = {
        "hub": "http://team.example",
        "human_id": 7,
        "project_slug": "alpha",
        "session": "workspace-a",
        "session_generation": "run-a",
        "session_dir": "/work/a",
        "mail_project": "/work/a",
        "lead": {
            "pane_id": "w1:p2",
            "agent": "codex",
            "mail_name": "codex-main",
            "participant_id": "lead",
        },
        "client_session_id": "client-run-a",
        "agent_id": 11,
        "managed_runtime": True,
        "auth_expires_at": time.time() + 3600,
    }
    values.update(overrides)
    return team_sessions.bind(**values)


def test_bind_persists_minimal_private_state(tmp_path, monkeypatch):
    state = tmp_path / "team-sessions.json"
    monkeypatch.setattr(team_sessions, "STATE_PATH", state)

    saved = _bind()

    assert saved["session_generation"] == "run-a"
    assert saved["client_session_id"] == "client-run-a"
    assert state.stat().st_mode & 0o777 == 0o600
    raw = state.read_text(encoding="utf-8")
    assert "registration_token" not in raw
    assert json.loads(raw)["bindings"][0]["lead"]["mail_name"] == "codex-main"


def test_managed_session_matches_exact_bound_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(team_sessions, "STATE_PATH", tmp_path / "state.json")
    _bind()

    assert team_sessions.is_managed_session("workspace-a", "run-a") is True
    assert team_sessions.is_managed_session("workspace-a", "rebuilt") is False
    assert team_sessions.is_managed_session("other", "run-a") is False

    team_sessions.unbind_project("http://team.example", 7, "alpha")
    assert team_sessions.is_managed_session("workspace-a", "run-a") is False


def test_legacy_local_session_binding_is_not_managed_or_worker_eligible(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(team_sessions, "STATE_PATH", tmp_path / "state.json")
    saved = _bind(
        managed_runtime=False,
        reply_token="reply-secret",
    )

    assert saved["managed_runtime"] is False
    assert team_sessions.is_managed_session("workspace-a", "run-a") is False
    assert team_sessions.reply_bindings_for_lead("/work/a", "codex-main") == []


def test_reply_token_is_private_and_preserved_on_idempotent_bind(
    tmp_path, monkeypatch,
):
    state = tmp_path / "team-sessions.json"
    monkeypatch.setattr(team_sessions, "STATE_PATH", state)
    _bind(reply_token="reply-secret")

    rebound = _bind()

    assert rebound["reply_token"] == "reply-secret"
    assert state.stat().st_mode & 0o777 == 0o600


def test_reply_mode_defaults_confirm_and_capability_update_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(team_sessions, "STATE_PATH", tmp_path / "state.json")
    saved = _bind(reply_token="old-secret")

    updated = team_sessions.update_reply_capability(
        hub="http://team.example",
        human_id=7,
        project_slug="alpha",
        client_session_id="client-run-a",
        reply_token="new-secret",
        reply_mode="auto",
    )

    assert saved["reply_mode"] == "confirm"
    assert updated["reply_mode"] == "auto"
    assert updated["reply_token"] == "new-secret"
    assert _bind()["reply_mode"] == "auto"


def test_update_and_resolve_reply_token_by_exact_local_lead(tmp_path, monkeypatch):
    monkeypatch.setattr(team_sessions, "STATE_PATH", tmp_path / "state.json")
    _bind(reply_token="old-secret")

    updated = team_sessions.update_reply_token(
        hub="http://team.example",
        human_id=7,
        project_slug="alpha",
        client_session_id="client-run-a",
        reply_token="new-secret",
    )
    matches = team_sessions.reply_bindings_for_lead("/work/a", "codex-main")

    assert updated["reply_token"] == "new-secret"
    assert len(matches) == 1
    assert matches[0]["reply_token"] == "new-secret"
    assert team_sessions.reply_bindings_for_lead("/work/a", "other-main") == []


def test_human_auth_lease_fails_closed_and_logout_revokes_capability(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(team_sessions, "STATE_PATH", tmp_path / "state.json")
    saved = _bind(auth_expires_at=None, reply_token="reply-secret")

    assert team_sessions.binding_auth_active(saved) is False
    assert team_sessions.reply_bindings_for_lead("/work/a", "codex-main") == []

    team_sessions.authorize_human(
        hub="http://team.example",
        human_id=7,
        auth_expires_at=time.time() + 3600,
    )
    assert len(team_sessions.reply_bindings_for_lead("/work/a", "codex-main")) == 1

    suspended = team_sessions.suspend_human(
        hub="http://team.example", human_id=7, revoke_capability=True,
    )
    current = team_sessions.list_bindings("http://team.example", 7)[0]
    assert suspended[0]["reply_token"] == "reply-secret"
    assert current["auth_expires_at"] == 0
    assert "reply_token" not in current
    assert current["session"] == "workspace-a"
    assert team_sessions.reply_bindings_for_lead("/work/a", "codex-main") == []


def test_same_session_generation_cannot_change_project_without_replace(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(team_sessions, "STATE_PATH", tmp_path / "state.json")
    _bind()

    with pytest.raises(ValueError, match="显式确认"):
        _bind(project_slug="beta", agent_id=12)

    replaced = _bind(project_slug="beta", agent_id=12, replace=True)
    assert replaced["project_slug"] == "beta"
    assert [row["project_slug"] for row in team_sessions.list_bindings(
        "http://team.example", 7,
    )] == ["beta"]


def test_project_cannot_change_session_without_replace(tmp_path, monkeypatch):
    monkeypatch.setattr(team_sessions, "STATE_PATH", tmp_path / "state.json")
    _bind()

    with pytest.raises(ValueError, match="显式确认"):
        _bind(session="workspace-b", session_generation="run-b", session_dir="/work/b")


def test_one_human_can_use_different_agents_for_different_topics(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(team_sessions, "STATE_PATH", tmp_path / "state.json")
    _bind()
    _bind(
        project_slug="beta",
        session="workspace-b",
        session_generation="run-b",
        session_dir="/work/b",
        mail_project="/work/b",
        lead={"agent": "claude", "mail_name": "claude-beta"},
        client_session_id="client-run-b",
        agent_id=12,
    )

    rows = team_sessions.list_bindings("http://team.example", 7)
    assert [(row["project_slug"], row["session_dir"]) for row in rows] == [
        ("alpha", "/work/a"),
        ("beta", "/work/b"),
    ]


def test_rebuilt_session_is_new_generation_and_prunes_stale_name(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(team_sessions, "STATE_PATH", tmp_path / "state.json")
    _bind()

    rebuilt = _bind(
        project_slug="beta",
        session_generation="run-b",
        session_dir="/work/rebuilt",
        agent_id=12,
    )

    assert rebuilt["session_generation"] == "run-b"
    rows = team_sessions.list_bindings("http://team.example", 7)
    assert [(row["project_slug"], row["session_generation"]) for row in rows] == [
        ("beta", "run-b")
    ]


def test_scope_isolated_by_hub_and_human(tmp_path, monkeypatch):
    monkeypatch.setattr(team_sessions, "STATE_PATH", tmp_path / "state.json")
    _bind()
    _bind(hub="http://other.example", project_slug="beta", agent_id=12)
    _bind(human_id=8, project_slug="gamma", agent_id=13)

    assert len(team_sessions.list_bindings("http://team.example", 7)) == 1
    assert len(team_sessions.list_bindings("http://other.example", 7)) == 1
    assert len(team_sessions.list_bindings("http://team.example", 8)) == 1


def test_conflicts_and_unbind(tmp_path, monkeypatch):
    monkeypatch.setattr(team_sessions, "STATE_PATH", tmp_path / "state.json")
    _bind()

    conflicts = team_sessions.conflicts_for(
        hub="http://team.example",
        human_id=7,
        project_slug="beta",
        session="workspace-a",
        session_generation="run-a",
    )
    assert [row["project_slug"] for row in conflicts] == ["alpha"]
    removed = team_sessions.unbind_project("http://team.example", 7, "alpha")
    assert removed and removed["session"] == "workspace-a"
    assert team_sessions.unbind_project("http://team.example", 7, "alpha") is None


def test_failed_atomic_replace_keeps_previous_file(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    monkeypatch.setattr(team_sessions, "STATE_PATH", state)
    _bind()
    before = state.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError, match="disk"):
        _bind(agent_id=12)

    assert state.read_bytes() == before


def test_corrupt_state_is_reported_and_not_overwritten(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(team_sessions, "STATE_PATH", state)

    with pytest.raises(OSError, match="已损坏"):
        _bind()

    assert state.read_text(encoding="utf-8") == "{broken"
