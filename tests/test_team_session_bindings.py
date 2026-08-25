import time

from fastapi.testclient import TestClient

import server
from agent_cockpit import team_sessions


HUB = "http://team.example"
PROJECT_KEY = "/work/demo"
IDENTITY_ID = "work-demo/codex--main.json"


def _identity(hub=HUB):
    return {
        "identity_id": IDENTITY_ID,
        "project_key": PROJECT_KEY,
        "project_slug": "work-demo",
        "agent": "codex",
        "instance": "main",
        "name": "codex-main",
        "registration_token": "registration-secret",
        "hub": hub,
    }


def _prepare(monkeypatch, participants=None, identities=None):
    participants = participants if participants is not None else [{
        "participant_id": "lead",
        "agent_type": "codex",
        "mail_name": "codex-main",
        "pane_id": "w1:p2",
        "role": "lead",
        "task_text": "负责",
        "state": "working",
    }]
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "cockpit-secret")
    monkeypatch.setattr(
        server.hub_client,
        "public_team_config",
        lambda: {"team_hub": HUB, "human_auth": "http://auth.example"},
    )
    panes = [{
        "session": "demo",
        "pane_id": str(participant.get("pane_id") or ""),
        "agent": str(participant.get("agent_type") or ""),
        "agent_status": "working",
        "cwd": PROJECT_KEY,
    } for participant in participants]
    monkeypatch.setattr(server, "_board_snapshot", lambda: {
        "sessions": [{
            "session": "demo",
            "directory": PROJECT_KEY,
            "panes": panes,
        }],
        "panes": [],
    })
    monkeypatch.setattr(
        server.coordination,
        "run_by_session",
        lambda _session: {"run_id": "run-1", "participants": participants},
    )
    monkeypatch.setattr(server.coordination, "task_reports", lambda _session: {})
    monkeypatch.setattr(
        server.herdr_client, "readonly_agent_process_verified",
        lambda *_args: True,
    )
    monkeypatch.setattr(server, "_mail_project_state", lambda _session: {
        "bound": True,
        "project": PROJECT_KEY,
    })
    registry = [_identity()] if identities is None else identities
    monkeypatch.setattr(server, "_registry_scan", lambda: registry)
    client = TestClient(server.app)
    client.cookies.set(server.TEAM_AUTH_COOKIE, "human.jwt", path="/api")
    return client, {"authorization": "Bearer cockpit-secret"}


def _human_api(membership=None, calls=None):
    membership = membership or {
        "status": "active",
        "default_agent_id": 3,
        "mention_handle": "fyc",
    }

    def call(method, path, authorization, payload=None):
        if calls is not None:
            calls.append((method, path, authorization, payload))
        if path == "/hub/api/humans/me":
            return {"id": 7, "display_name": "FYC"}
        if path == "/hub/api/projects/demo/membership" and method == "GET":
            return dict(membership)
        if path.endswith("/session-lead") and method == "PUT":
            return {
                "active": True,
                "agent": {"id": 41, "name": "SessionLead41"},
                "reply_token": "reply-secret",
            }
        if path.endswith("/session-lead") and method == "DELETE":
            return {"ok": True, "active": False}
        raise AssertionError((method, path, payload))

    return call


def test_public_context_summary_reports_freshness_sha_and_handoff(monkeypatch):
    monkeypatch.setattr(server, "_context_pack_for_binding", lambda _binding: {
        "version": 1,
        "git": {"available": True, "head": "a" * 40, "dirty": True},
        "handoff": {"available": True, "updated": "2026-08-25"},
        "fingerprint": "f" * 64,
    })

    result = server._public_context_summary({"mail_project": PROJECT_KEY})

    assert result["freshness"] == "current"
    assert result["sha"] == "a" * 40
    assert result["dirty"] is True
    assert result["handoff_updated"] == "2026-08-25"
    assert result["fingerprint"] == "f" * 64
    assert result["observed_at"].endswith("+00:00")


def test_session_candidates_only_accept_one_lead_and_hide_registry_details(monkeypatch):
    participants = [
        {
            "participant_id": "lead",
            "agent_type": "codex",
            "mail_name": "codex-main",
            "pane_id": "w1:p2",
            "role": "lead",
            "state": "working",
        },
        {
            "participant_id": "reviewer",
            "agent_type": "kimi",
            "mail_name": "kimi-main",
            "pane_id": "w1:p3",
            "role": "reviewer",
            "state": "working",
        },
    ]
    client, headers = _prepare(monkeypatch, participants=participants)
    monkeypatch.setattr(server.hub_client, "human_api", _human_api())

    response = client.get("/api/team-auth/session-bindings", headers=headers)

    assert response.status_code == 200
    candidate = response.json()["sessions"][0]
    assert candidate["session"] == "demo"
    assert candidate["lead"] == {
        "agent": "codex", "mail_name": "codex-main", "status": "working",
    }
    assert candidate["ready"] is True
    assert isinstance(candidate["project_ref"], str)
    assert len(candidate["project_ref"]) == 64
    assert PROJECT_KEY not in response.text
    assert "mail_project" not in response.text
    assert "identity_id" not in response.text
    assert "registration-secret" not in response.text
    assert "pane_id" not in response.text
    assert "participant_id" not in response.text


def test_session_candidate_deduplicates_same_pane_from_snapshot_views(monkeypatch):
    participants = [{
        "participant_id": "lead",
        "agent_type": "codex",
        "mail_name": "codex-main",
        "pane_id": "w1:p2",
        "role": "lead",
        "state": "working",
    }]
    client, headers = _prepare(monkeypatch, participants=participants)
    nested = {
        "pane_id": "w1:p2", "session": "demo", "agent": "codex",
        "agent_status": "working", "cwd": PROJECT_KEY,
    }
    top_level = {**nested, "mail_name": "codex-main"}
    monkeypatch.setattr(server, "_board_snapshot", lambda: {
        "sessions": [{
            "session": "demo", "directory": PROJECT_KEY, "panes": [nested],
        }],
        "panes": [top_level],
    })
    monkeypatch.setattr(server.hub_client, "human_api", _human_api())

    response = client.get("/api/team-auth/session-bindings", headers=headers)

    assert response.status_code == 200
    candidate = response.json()["sessions"][0]
    assert candidate["ready"] is True
    assert candidate["lead"]["mail_name"] == "codex-main"


def test_descriptor_only_session_stays_on_board_but_not_team_candidate(monkeypatch):
    client, headers = _prepare(monkeypatch)
    snapshot = server._board_snapshot()
    snapshot["panes"].append({
        "session": "stopped-demo",
        "pane_id": "w1:p9",
        "agent": "codex",
        "agent_status": "stopped",
        "cwd": PROJECT_KEY,
        "from_descriptor": True,
    })
    monkeypatch.setattr(server, "_board_snapshot", lambda: snapshot)
    live_run = server.coordination.run_by_session("demo")
    monkeypatch.setattr(
        server.coordination,
        "run_by_session",
        lambda session: live_run if session == "demo" else {
            "run_id": "run-stopped",
            "participants": [{
                "participant_id": "ghost-lead",
                "agent_type": "codex",
                "mail_name": "ghost-main",
                "pane_id": "w1:p9",
                "role": "lead",
                "state": "done",
            }],
        },
    )
    mail_project_reads = []

    def mail_project_state(session):
        mail_project_reads.append(session)
        return {"bound": True, "project": PROJECT_KEY}

    monkeypatch.setattr(server, "_mail_project_state", mail_project_state)
    monkeypatch.setattr(server.hub_client, "human_api", _human_api())

    progress = server._build_session_progress(snapshot)
    response = client.get("/api/team-auth/session-bindings", headers=headers)

    assert {row["session"] for row in progress} == {"demo", "stopped-demo"}
    assert response.status_code == 200
    assert [row["session"] for row in response.json()["sessions"]] == ["demo"]
    assert mail_project_reads == ["demo"]


def test_lightweight_session_uses_registered_leader_instance_as_generation(monkeypatch):
    client, headers = _prepare(monkeypatch)
    instance_id = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setattr(server.coordination, "run_by_session", lambda _session: None)
    monkeypatch.setattr(server.chat_roster, "get_session_leader", lambda _session: {
        "leader_mail_name": "codex-main",
        "leader_agent": "codex",
    })
    monkeypatch.setattr(server, "_board_snapshot", lambda: {
        "sessions": [{
            "session": "demo",
            "directory": PROJECT_KEY,
            "panes": [{
                "session": "demo",
                "pane_id": "w1:p2",
                "agent": "codex",
                "agent_status": "working",
                "cwd": PROJECT_KEY,
                "mail_name": "codex-main",
                "instance_id": instance_id,
            }],
        }],
        "panes": [],
    })
    monkeypatch.setattr(server.hub_client, "human_api", _human_api())

    response = client.get("/api/team-auth/session-bindings", headers=headers)

    assert response.status_code == 200
    candidate = response.json()["sessions"][0]
    assert candidate["ready"] is True
    assert candidate["lead"]["mail_name"] == "codex-main"
    internal = server._team_session_candidates()[0]
    assert internal["generation"] == instance_id


def test_managed_restart_identity_change_degrades_binding_and_stops_worker(monkeypatch):
    client, headers = _prepare(monkeypatch)
    new_identity = {
        **_identity(),
        "identity_id": "work-demo/codex--new.json",
        "instance": "i-bbbbbbbbbbbbbbbbbbbbbbbbbb",
        "name": "FreshLead",
    }
    monkeypatch.setattr(server, "_registry_scan", lambda: [_identity(), new_identity])
    monkeypatch.setattr(server, "_board_snapshot", lambda: {
        "sessions": [{
            "session": "demo",
            "directory": PROJECT_KEY,
            "panes": [{
                "session": "demo",
                "pane_id": "w1:p2",
                "agent": "codex",
                "agent_status": "working",
                "cwd": PROJECT_KEY,
            }],
        }],
        "panes": [{
            "session": "demo",
            "pane_id": "w1:p2",
            "agent": "codex",
            "agent_status": "working",
            "cwd": PROJECT_KEY,
            "mail_name": "FreshLead",
            "instance_id": "i-bbbbbbbbbbbbbbbbbbbbbbbbbb",
        }],
    })
    monkeypatch.setattr(server.hub_client, "human_api", _human_api())
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="demo",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id=server._team_client_session_id("demo", "run-1"),
        agent_id=41,
        managed_runtime=True,
        reply_token="old-secret",
    )

    response = client.get("/api/team-auth/session-bindings", headers=headers)

    assert response.status_code == 200
    assert response.json()["sessions"][0]["lead"]["mail_name"] == "FreshLead"
    binding = response.json()["bindings"][0]
    assert binding["active"] is True
    assert binding["ready"] is False
    assert binding["reason"] == "Session 负责人身份已变化，需要重新绑定"
    assert server._active_team_lead_bindings() == []


def test_managed_restart_rebind_requires_confirmation_and_rotates_capability(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    new_identity = {
        **_identity(),
        "identity_id": "work-demo/codex--new.json",
        "instance": "i-bbbbbbbbbbbbbbbbbbbbbbbbbb",
        "name": "FreshLead",
    }
    monkeypatch.setattr(server, "_registry_scan", lambda: [_identity(), new_identity])
    monkeypatch.setattr(server, "_board_snapshot", lambda: {
        "sessions": [{
            "session": "demo",
            "directory": PROJECT_KEY,
            "panes": [{
                "session": "demo",
                "pane_id": "w1:p2",
                "agent": "codex",
                "agent_status": "working",
                "cwd": PROJECT_KEY,
            }],
        }],
        "panes": [{
            "session": "demo",
            "pane_id": "w1:p2",
            "agent": "codex",
            "agent_status": "working",
            "cwd": PROJECT_KEY,
            "mail_name": "FreshLead",
            "instance_id": "i-bbbbbbbbbbbbbbbbbbbbbbbbbb",
        }],
    })
    monkeypatch.setattr(server.hub_client, "human_api", _human_api(calls=calls))
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="demo",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id=server._team_client_session_id("demo", "run-1"),
        agent_id=41,
        managed_runtime=True,
        reply_token="old-secret",
    )

    rejected = client.put(
        "/api/team-auth/session-bindings/demo",
        headers=headers,
        json={"session": "demo"},
    )
    rebound = client.put(
        "/api/team-auth/session-bindings/demo",
        headers=headers,
        json={"session": "demo", "replace": True},
    )

    assert rejected.status_code == 409
    assert "身份已变化" in rejected.json()["detail"]
    assert rebound.status_code == 200
    puts = [call for call in calls if call[0] == "PUT"]
    assert len(puts) == 1
    assert puts[0][3]["lead_label"] == "FreshLead"
    assert puts[0][3]["rotate_reply_token"] is True
    saved = team_sessions.list_bindings(HUB, 7)[0]
    assert saved["lead"]["mail_name"] == "FreshLead"
    assert rebound.json()["binding"]["ready"] is True


def test_session_candidate_explains_missing_and_multiple_lead(monkeypatch):
    cases = [
        ([], [], "负责人未配置"),
        ([
            {"participant_id": "a", "agent_type": "codex", "mail_name": "a", "pane_id": "w1:p2", "role": "lead"},
            {"participant_id": "b", "agent_type": "codex", "mail_name": "b", "pane_id": "w1:p3", "role": "lead"},
        ], [], "存在多个负责人"),
        ([{"participant_id": "lead", "agent_type": "codex", "mail_name": "codex-main", "pane_id": "w1:p2", "role": "lead"}], [], "负责人本机身份缺失或不唯一"),
    ]
    for participants, identities, reason in cases:
        client, headers = _prepare(
            monkeypatch, participants=participants, identities=identities,
        )
        monkeypatch.setattr(server.hub_client, "human_api", _human_api())
        response = client.get("/api/team-auth/session-bindings", headers=headers)
        assert response.status_code == 200
        assert response.json()["sessions"][0]["ready"] is False
        assert reason in response.json()["sessions"][0]["reason"]


def test_local_agent_mail_hub_does_not_need_to_match_team_hub(monkeypatch):
    client, headers = _prepare(
        monkeypatch, identities=[_identity("http://127.0.0.1:8765")],
    )
    monkeypatch.setattr(server.hub_client, "human_api", _human_api())

    response = client.get("/api/team-auth/session-bindings", headers=headers)

    assert response.status_code == 200
    assert response.json()["sessions"][0]["ready"] is True


def test_legacy_binding_without_reply_capability_requires_resync(monkeypatch):
    client, headers = _prepare(monkeypatch)
    monkeypatch.setattr(server.hub_client, "human_api", _human_api())
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="demo",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id=server._team_client_session_id("demo", "run-1"),
        agent_id=41,
        managed_runtime=True,
    )

    response = client.get("/api/team-auth/session-bindings", headers=headers)

    assert response.status_code == 200
    binding = response.json()["bindings"][0]
    assert binding["active"] is True
    assert binding["ready"] is False
    assert binding["reason"] == "负责人通信凭据需要重新同步"
    assert "reply_token" not in response.text


def test_inbox_route_is_disabled_without_fetching_remote_items(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(server.hub_client, "human_api", _human_api(calls=calls))

    response = client.post("/api/team-auth/inbox-route/route", headers=headers)

    assert response.status_code == 200
    assert [path for _method, path, _auth, _payload in calls] == [
        "/hub/api/humans/me"
    ]
    assert response.json()["available"] is False
    assert response.json()["reason"] == "remote_inbox_pane_delivery_disabled"
    assert response.json()["fetched"] == 0
    assert response.json()["delivered"] == 0
    assert not hasattr(server, "_team_inbox_reply_command")


def test_binding_existing_local_session_is_legacy_and_requires_migration(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(server.hub_client, "human_api", _human_api(calls=calls))
    monkeypatch.setattr(
        server.hub_client,
        "claim_agent",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not claim")),
    )

    response = client.put(
        "/api/team-auth/session-bindings/demo",
        headers=headers,
        json={"session": "demo"},
    )

    assert response.status_code == 200
    assert response.json()["binding"]["session"] == "demo"
    assert "registration-secret" not in response.text
    put = next(call for call in calls if call[0] == "PUT")
    assert put[1] == "/hub/api/projects/demo/session-lead"
    assert put[3]["lead_label"] == "codex-main"
    assert len(put[3]["client_session_id"]) == 64
    saved = team_sessions.list_bindings(HUB, 7)
    assert len(saved) == 1
    assert saved[0]["session_generation"] == "run-1"
    assert saved[0]["mail_project"] == PROJECT_KEY
    assert saved[0]["client_session_id"] == put[3]["client_session_id"]
    assert saved[0]["lead"]["participant_id"] == "lead"
    assert saved[0]["reply_token"] == "reply-secret"
    assert saved[0]["reply_mode"] == "confirm"
    assert response.json()["binding"]["reply_mode"] == "confirm"
    assert response.json()["binding"]["managed_runtime"] is False
    assert response.json()["binding"]["ready"] is False
    assert response.json()["binding"]["reason"] == (
        "旧绑定使用普通本地会话，需要迁移为 Topic 专用 Agent"
    )
    assert saved[0]["managed_runtime"] is False
    assert "reply-secret" not in response.text


def test_reply_mode_switch_rotates_capability_and_updates_pending_work(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    regular = _human_api(calls=calls)
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="demo",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id=server._team_client_session_id("demo", "run-1"),
        agent_id=41,
        managed_runtime=True,
        reply_token="old-secret",
        reply_mode="confirm",
    )

    def switch(method, path, authorization, payload=None):
        if method == "PUT" and path.endswith("/session-lead"):
            calls.append((method, path, authorization, payload))
            return {
                "active": True,
                "agent": {"id": 41},
                "reply_token": "rotated-secret",
                "binding": {"reply_mode": payload["reply_mode"]},
            }
        return regular(method, path, authorization, payload)

    updated_work = []
    monkeypatch.setattr(server.hub_client, "human_api", switch)
    monkeypatch.setattr(
        server.team_lead_worker,
        "update_binding_reply_mode",
        lambda **kwargs: updated_work.append(kwargs) or 1,
    )

    response = client.patch(
        "/api/team-auth/session-bindings/demo/reply-mode",
        headers=headers,
        json={"reply_mode": "auto"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["binding"]["reply_mode"] == "auto"
    put = next(call for call in calls if call[0] == "PUT")
    assert put[3]["reply_mode"] == "auto"
    saved = team_sessions.list_bindings(HUB, 7)[0]
    assert saved["reply_mode"] == "auto"
    assert saved["reply_token"] == "rotated-secret"
    assert updated_work == [{
        "hub": HUB,
        "project_slug": "demo",
        "client_session_id": saved["client_session_id"],
        "reply_mode": "auto",
    }]
    assert "rotated-secret" not in response.text


def test_reply_mode_switch_rejects_invalid_mode_before_hub(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(server.hub_client, "human_api", _human_api(calls=calls))

    response = client.patch(
        "/api/team-auth/session-bindings/demo/reply-mode",
        headers=headers,
        json={"reply_mode": "sometimes"},
    )

    assert response.status_code == 422
    assert calls == []


def test_reply_mode_switch_rejects_changed_lead_identity(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(server.hub_client, "human_api", _human_api(calls=calls))
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="demo",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id=server._team_client_session_id("demo", "run-1"),
        agent_id=41,
        managed_runtime=True,
        reply_token="old-secret",
        reply_mode="confirm",
    )
    monkeypatch.setattr(server, "_team_session_candidates", lambda: [{
        "session": "demo",
        "generation": "run-1",
        "mail_project": PROJECT_KEY,
        "ready": True,
        "lead": {"agent": "codex", "mail_name": "FreshLead"},
    }])

    response = client.patch(
        "/api/team-auth/session-bindings/demo/reply-mode",
        headers=headers,
        json={"reply_mode": "auto"},
    )

    assert response.status_code == 409
    assert "身份已变化" in response.json()["detail"]
    assert not any(call[0] == "PUT" for call in calls)


def test_reply_mode_idempotent_retry_preserves_existing_capability(monkeypatch):
    client, headers = _prepare(monkeypatch)
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="demo",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id=server._team_client_session_id("demo", "run-1"),
        agent_id=41,
        managed_runtime=True,
        reply_token="existing-secret",
        reply_mode="confirm",
    )
    regular = _human_api()

    def same_mode(method, path, authorization, payload=None):
        if method == "PUT" and path.endswith("/session-lead"):
            return {
                "active": True,
                "agent": {"id": 41},
                "binding": {"reply_mode": "confirm"},
            }
        return regular(method, path, authorization, payload)

    monkeypatch.setattr(server.hub_client, "human_api", same_mode)

    response = client.patch(
        "/api/team-auth/session-bindings/demo/reply-mode",
        headers=headers,
        json={"reply_mode": "confirm"},
    )

    assert response.status_code == 200, response.text
    assert team_sessions.list_bindings(HUB, 7)[0]["reply_token"] == "existing-secret"


def test_reusing_binding_preserves_one_time_reply_token(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    regular = _human_api(calls=calls)
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="demo",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id=server._team_client_session_id("demo", "run-1"),
        agent_id=41,
        managed_runtime=True,
        reply_token="one-time-secret",
    )

    def reuse_without_plaintext(method, path, authorization, payload=None):
        if method == "PUT" and path.endswith("/session-lead"):
            calls.append((method, path, authorization, payload))
            return {"active": True, "agent": {"id": 41}}
        return regular(method, path, authorization, payload)

    monkeypatch.setattr(server.hub_client, "human_api", reuse_without_plaintext)

    response = client.put(
        "/api/team-auth/session-bindings/demo",
        headers=headers,
        json={"session": "demo"},
    )

    assert response.status_code == 200
    put = next(call for call in calls if call[0] == "PUT")
    assert "rotate_reply_token" not in put[3]
    saved = team_sessions.list_bindings(HUB, 7)
    assert saved[0]["reply_token"] == "one-time-secret"


def test_binding_without_local_capability_forces_rotation(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(server.hub_client, "human_api", _human_api(calls=calls))
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="demo",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id=server._team_client_session_id("demo", "run-1"),
        agent_id=41,
        managed_runtime=True,
    )

    response = client.put(
        "/api/team-auth/session-bindings/demo",
        headers=headers,
        json={"session": "demo"},
    )

    assert response.status_code == 200
    put = next(call for call in calls if call[0] == "PUT")
    assert put[3]["rotate_reply_token"] is True
    assert team_sessions.list_bindings(HUB, 7)[0]["reply_token"] == "reply-secret"


def test_bind_requires_active_membership_before_remote_create(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(
        server.hub_client,
        "human_api",
        _human_api({"status": "invited", "default_agent_id": None}, calls),
    )

    response = client.put(
        "/api/team-auth/session-bindings/demo",
        headers=headers,
        json={"session": "demo"},
    )

    assert response.status_code == 403
    assert not any(call[0] == "PUT" for call in calls)
    assert team_sessions.list_bindings(HUB, 7) == []


def test_conflict_is_rejected_before_remote_create(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(server.hub_client, "human_api", _human_api(calls=calls))
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="other",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id="old-client",
        agent_id=9,
        managed_runtime=True,
    )

    response = client.put(
        "/api/team-auth/session-bindings/demo",
        headers=headers,
        json={"session": "demo"},
    )

    assert response.status_code == 409
    assert not any(call[0] == "PUT" for call in calls)


def test_local_write_failure_deactivates_remote_managed_lead(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(server.hub_client, "human_api", _human_api(calls=calls))
    monkeypatch.setattr(
        server.team_sessions,
        "bind",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    response = client.put(
        "/api/team-auth/session-bindings/demo",
        headers=headers,
        json={"session": "demo"},
    )

    assert response.status_code == 500
    assert [call[0] for call in calls] == ["GET", "GET", "PUT", "DELETE"]
    assert calls[-1][3] == {"client_session_id": calls[-2][3]["client_session_id"]}
    assert team_sessions.list_bindings(HUB, 7) == []


def test_invalid_remote_response_is_deactivated_without_local_binding(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    remote = _human_api(calls=calls)

    def invalid_remote(method, path, authorization, payload=None):
        if method == "PUT" and path.endswith("/session-lead"):
            calls.append((method, path, authorization, payload))
            return {"active": True, "agent": {"id": "invalid"}}
        return remote(method, path, authorization, payload)

    monkeypatch.setattr(server.hub_client, "human_api", invalid_remote)

    response = client.put(
        "/api/team-auth/session-bindings/demo",
        headers=headers,
        json={"session": "demo"},
    )

    assert response.status_code == 502
    assert [call[0] for call in calls][-2:] == ["PUT", "DELETE"]
    assert team_sessions.list_bindings(HUB, 7) == []


def test_replace_deactivates_previous_project_route(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(server.hub_client, "human_api", _human_api(calls=calls))
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="other",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id="old-client",
        agent_id=9,
        managed_runtime=True,
    )

    response = client.put(
        "/api/team-auth/session-bindings/demo",
        headers=headers,
        json={"session": "demo", "replace": True},
    )

    assert response.status_code == 200
    remote_calls = [call for call in calls if "/session-lead" in call[1]]
    assert [(call[0], call[1]) for call in remote_calls] == [
        ("DELETE", "/hub/api/projects/other/session-lead"),
        ("PUT", "/hub/api/projects/demo/session-lead"),
    ]
    assert remote_calls[0][3] == {"client_session_id": "old-client"}
    assert [row["project_slug"] for row in team_sessions.list_bindings(HUB, 7)] == [
        "demo"
    ]


def test_failed_replace_restores_previous_project_route(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    regular = _human_api(calls=calls)

    def fail_new_route(method, path, authorization, payload=None):
        if method == "PUT" and path == "/hub/api/projects/demo/session-lead":
            calls.append((method, path, authorization, payload))
            raise server.hub_client.HumanAPIError(503, "temporarily unavailable")
        return regular(method, path, authorization, payload)

    monkeypatch.setattr(server.hub_client, "human_api", fail_new_route)
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="other",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id="old-client",
        agent_id=9,
        managed_runtime=True,
    )

    response = client.put(
        "/api/team-auth/session-bindings/demo",
        headers=headers,
        json={"session": "demo", "replace": True},
    )

    assert response.status_code == 503
    remote_calls = [call for call in calls if "/session-lead" in call[1]]
    assert [(call[0], call[1]) for call in remote_calls] == [
        ("DELETE", "/hub/api/projects/other/session-lead"),
        ("PUT", "/hub/api/projects/demo/session-lead"),
        ("PUT", "/hub/api/projects/other/session-lead"),
    ]
    assert [row["project_slug"] for row in team_sessions.list_bindings(HUB, 7)] == [
        "other"
    ]
    restored = team_sessions.list_bindings(HUB, 7)[0]
    assert restored["reply_token"] == "reply-secret"
    assert remote_calls[-1][3]["rotate_reply_token"] is True


def test_unbind_only_removes_route_and_deactivates_managed_lead(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(
        server.hub_client,
        "human_api",
        _human_api({"status": "active", "default_agent_id": 41}, calls),
    )
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="demo",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id="client-run-1",
        agent_id=41,
        managed_runtime=True,
    )
    monkeypatch.setattr(
        server.herdr_client,
        "stop_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not stop")),
    )

    response = client.delete(
        "/api/team-auth/session-bindings/demo", headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "removed": True}
    assert calls[-1] == (
        "DELETE", "/hub/api/projects/demo/session-lead",
        "Bearer human.jwt", {"client_session_id": "client-run-1"},
    )
    assert team_sessions.list_bindings(HUB, 7) == []


def test_delete_managed_topic_agent_stops_unbinds_then_deletes_runtime(monkeypatch):
    client, headers = _prepare(monkeypatch)
    events = []
    def human_api(method, path, authorization, payload=None):
        if path == "/hub/api/humans/me":
            return {"id": 7, "display_name": "FYC"}
        events.append(("remote", method, path, payload))
        return {}

    monkeypatch.setattr(server.hub_client, "human_api", human_api)
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="demo",
        session="team-demo-1",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id="client-run-1",
        agent_id=41,
        managed_runtime=True,
    )
    monkeypatch.setattr(
        server.herdr_client,
        "stop_session",
        lambda session: events.append(("stop", session))
        or {"available": True, "stopped": session},
    )
    monkeypatch.setattr(
        server.coordination,
        "close_session",
        lambda session, reason: events.append(("coordination", session, reason)),
    )
    original_unbind = team_sessions.unbind_project
    monkeypatch.setattr(
        team_sessions,
        "unbind_project",
        lambda hub, human_id, slug: events.append(("local_unbind", slug))
        or original_unbind(hub, human_id, slug),
    )
    monkeypatch.setattr(
        server,
        "api_herdr_session_delete",
        lambda session: events.append(("delete", session))
        or {"available": True, "deleted": session},
    )

    response = client.delete(
        "/api/team-auth/session-bindings/demo?delete_runtime=true",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "removed": True,
        "runtime_deleted": True,
    }
    assert events == [
        ("stop", "team-demo-1"),
        ("coordination", "team-demo-1", "stopped"),
        (
            "remote", "DELETE", "/hub/api/projects/demo/session-lead",
            {"client_session_id": "client-run-1"},
        ),
        ("local_unbind", "demo"),
        ("delete", "team-demo-1"),
    ]
    assert team_sessions.list_bindings(HUB, 7) == []


def test_delete_managed_topic_agent_stop_failure_keeps_binding(monkeypatch):
    client, headers = _prepare(monkeypatch)
    remote_calls = []
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="demo",
        session="team-demo-1",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id="client-run-1",
        agent_id=41,
        managed_runtime=True,
    )
    monkeypatch.setattr(
        server.herdr_client,
        "stop_session",
        lambda _session: {"available": True, "error": "busy"},
    )
    monkeypatch.setattr(server.hub_client, "human_api", _human_api(calls=remote_calls))

    response = client.delete(
        "/api/team-auth/session-bindings/demo?delete_runtime=true",
        headers=headers,
    )

    assert response.status_code == 502
    assert "均未删除" in response.json()["detail"]
    assert not any(path.endswith("/session-lead") for _, path, _, _ in remote_calls)
    assert len(team_sessions.list_bindings(HUB, 7)) == 1


def test_managed_runtime_never_appears_in_ordinary_session_read_model(monkeypatch):
    monkeypatch.setattr(
        server.team_sessions,
        "managed_session_names",
        lambda: {"team-demo-1"},
    )
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [
            {"name": "daily-1", "status": "running"},
            {"name": "team-demo-1", "status": "running"},
        ],
    )
    monkeypatch.setattr(
        server.chat_ledger,
        "list_workspaces",
        lambda: [{"id": "ws-1", "path": PROJECT_KEY, "title": "demo"}],
    )
    monkeypatch.setattr(
        server.chat_ledger,
        "list_threads",
        lambda: [
            {"id": "th-1", "workspace_id": "ws-1", "herdr_session": "daily-1"},
            {"id": "th-2", "workspace_id": "ws-1", "herdr_session": "team-demo-1"},
        ],
    )

    assert [
        row["name"] for row in server.api_herdr_sessions()["sessions"]
    ] == ["daily-1"]
    ledger = server.api_chat_workspaces()
    assert [row["herdr_session"] for row in ledger["threads"]] == ["daily-1"]
    assert [
        row["herdr_session"] for row in ledger["workspaces"][0]["threads"]
    ] == ["daily-1"]


def test_logout_suspends_worker_revokes_remote_capability_and_keeps_binding(
    monkeypatch,
):
    client, headers = _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(server.hub_client, "human_api", _human_api(calls=calls))
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="demo",
        session="demo",
        session_generation="run-1",
        session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id=server._team_client_session_id("demo", "run-1"),
        agent_id=41,
        managed_runtime=True,
        reply_token="reply-secret",
        auth_expires_at=time.time() + 3600,
    )
    saved = team_sessions.list_bindings(HUB, 7)[0]
    server.team_lead_worker.poll_binding(
        saved,
        {
            "session": "demo",
            "generation": "run-1",
            "lead": {"mail_name": "codex-main", "pane_id": "w1:p2"},
        },
        claim=lambda *_args: {
            "status": "claimed",
            "claim_token": "claim-secret",
            "claim_expires_at": "2026-08-25 10:00:00",
            "reply_mode": "confirm",
            "message": {
                "inbox_item_id": 31,
                "message_id": 41,
                "subject": "question",
                "body_md": "body",
                "importance": "normal",
                "sender_name": "Alice",
                "sender_handle": "alice",
                "created_ts": "2026-08-24 10:00:00",
            },
        },
        notify=lambda *_args: True,
    )

    response = client.post(
        "/api/team-auth/logout",
        headers=headers,
        json={"client_id": "cockpit-wsl-1234"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "automation_suspended": 1,
        "revocation_failures": 0,
    }
    assert calls[-2] == (
        "POST",
        "/hub/api/presence",
        "Bearer human.jwt",
        {"client_id": "cockpit-wsl-1234", "online": False},
    )
    assert calls[-1] == (
        "DELETE",
        "/hub/api/projects/demo/session-lead",
        "Bearer human.jwt",
        {"client_session_id": saved["client_session_id"]},
    )
    current = team_sessions.list_bindings(HUB, 7)[0]
    assert current["auth_expires_at"] == 0
    assert "reply_token" not in current
    assert current["session"] == "demo"
    assert server.team_lead_worker.next_for_binding(saved) is None
    assert server._active_team_lead_bindings() == []


def test_one_click_team_session_starts_read_only_before_binding(monkeypatch):
    client, headers = _prepare(monkeypatch)
    monkeypatch.setattr(server.hub_client, "human_api", _human_api())
    monkeypatch.setattr(server, "_chat_workspace", lambda workspace_id: {
        "id": workspace_id,
        "title": "HR Ready",
        "path": PROJECT_KEY,
    })
    monkeypatch.setattr(server, "_next_team_session_name", lambda _slug: "team-demo-1")
    created_calls = []
    bind_calls = []

    def create(workspace_id, req, *, session_name=None, managed_team=False):
        created_calls.append((workspace_id, req, session_name, managed_team))
        return {
            "session": session_name,
            "thread": {"id": "th-1"},
            "agent_mail": {"ok": True},
            "leader": {"leader_mail_name": "codex-main"},
        }

    def bind(slug, req, _request):
        bind_calls.append((slug, req, _request.state.team_managed_runtime))
        return {"ok": True, "binding": {"session": req.session}}

    monkeypatch.setattr(server, "_create_chat_session", create)
    monkeypatch.setattr(server, "api_team_session_bind", bind)

    response = client.post(
        "/api/team-auth/session-bindings/demo/create",
        headers=headers,
        json={
            "workspace_id": "ws-1",
            "agent": "codex",
            "reply_mode": "auto",
        },
    )

    assert response.status_code == 200
    assert response.json()["session"] == "team-demo-1"
    workspace_id, create_req, session_name, managed_team = created_calls[0]
    assert workspace_id == "ws-1"
    assert session_name == "team-demo-1"
    assert managed_team is True
    assert create_req.args == "--sandbox read-only"
    assert bind_calls[0][0] == "demo"
    assert bind_calls[0][1].session == "team-demo-1"
    assert bind_calls[0][1].reply_mode == "auto"
    assert bind_calls[0][2] is True


def test_managed_binding_is_not_ready_when_real_process_fails_fence(monkeypatch):
    client, headers = _prepare(monkeypatch)
    monkeypatch.setattr(server.hub_client, "human_api", _human_api())
    monkeypatch.setattr(
        server.herdr_client, "readonly_agent_process_verified",
        lambda *_args: False,
    )
    team_sessions.bind(
        hub=HUB, human_id=7, project_slug="demo", session="demo",
        session_generation="run-1", session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "codex-main"},
        client_session_id="team-client", agent_id=11, managed_runtime=True,
        reply_token="reply-secret", auth_expires_at=time.time() + 3600,
    )

    response = client.get("/api/team-auth/session-bindings", headers=headers)

    assert response.status_code == 200
    binding = response.json()["bindings"][0]
    assert binding["ready"] is False
    assert binding["reason"] == "Team Agent 真实进程未通过只读栅栏校验"


def test_one_click_team_session_rolls_back_when_lead_registration_fails(monkeypatch):
    client, headers = _prepare(monkeypatch)
    monkeypatch.setattr(server.hub_client, "human_api", _human_api())
    monkeypatch.setattr(server, "_chat_workspace", lambda workspace_id: {
        "id": workspace_id,
        "title": "HR Ready",
        "path": PROJECT_KEY,
    })
    monkeypatch.setattr(server, "_next_team_session_name", lambda _slug: "team-demo-2")
    monkeypatch.setattr(server, "_create_chat_session", lambda *_args, **_kwargs: {
        "session": "team-demo-2",
        "thread": {"id": "th-2"},
        "agent_mail": {"ok": False, "reason": "registration failed"},
        "leader": {},
    })
    rolled_back = []
    monkeypatch.setattr(
        server,
        "_rollback_created_team_session",
        lambda session: rolled_back.append(session) or {"stopped": True},
    )

    response = client.post(
        "/api/team-auth/session-bindings/demo/create",
        headers=headers,
        json={"workspace_id": "ws-1", "agent": "codex"},
    )

    assert response.status_code == 502
    assert response.json()["detail"]["stage"] == "register_lead"
    assert rolled_back == ["team-demo-2"]


def test_one_click_team_session_rejects_agent_without_read_only_mode(monkeypatch):
    client, headers = _prepare(monkeypatch)
    monkeypatch.setattr(server.hub_client, "human_api", _human_api())
    monkeypatch.setattr(server, "_chat_workspace", lambda workspace_id: {
        "id": workspace_id,
        "title": "HR Ready",
        "path": PROJECT_KEY,
    })

    response = client.post(
        "/api/team-auth/session-bindings/demo/create",
        headers=headers,
        json={"workspace_id": "ws-1", "agent": "opencode"},
    )

    assert response.status_code == 400
    assert "暂不支持 Team Session 只读模式" in response.text
def test_consult_target_requires_explicit_same_project_ordinary_lead(monkeypatch):
    client, headers = _prepare(monkeypatch)
    monkeypatch.setattr(server.hub_client, "human_api", _human_api())
    team_sessions.bind(
        hub=HUB, human_id=7, project_slug="demo", session="team-demo",
        session_generation="team-run", session_dir=PROJECT_KEY,
        mail_project=PROJECT_KEY,
        lead={"agent": "codex", "mail_name": "team-main"},
        client_session_id="team-client", agent_id=11, managed_runtime=True,
        reply_token="reply-secret", auth_expires_at=time.time() + 3600,
    )
    candidates = [
        {
            "session": "team-demo", "generation": "team-run",
            "mail_project": PROJECT_KEY, "ready": True, "status": "done",
            "agent_count": 1,
            "lead": {"agent": "codex", "mail_name": "team-main", "status": "done"},
        },
        {
            "session": "dev-demo", "generation": "dev-run",
            "mail_project": PROJECT_KEY, "ready": True, "status": "idle",
            "agent_count": 1,
            "lead": {
                "agent": "codex", "mail_name": "dev-main",
                "participant_id": "dev-lead", "status": "idle",
            },
        },
        {
            "session": "other", "generation": "other-run",
            "mail_project": "/work/other", "ready": True, "status": "idle",
            "agent_count": 1,
            "lead": {"agent": "codex", "mail_name": "other-main", "status": "idle"},
        },
    ]
    monkeypatch.setattr(server, "_team_session_candidates", lambda: candidates)

    selected = client.patch(
        "/api/team-auth/session-bindings/demo/consult-target",
        headers=headers, json={"session": "dev-demo"},
    )
    cross_project = client.patch(
        "/api/team-auth/session-bindings/demo/consult-target",
        headers=headers, json={"session": "other"},
    )
    listed = client.get("/api/team-auth/session-bindings", headers=headers)

    assert selected.status_code == 200
    assert selected.json()["binding"]["consult_target"]["session"] == "dev-demo"
    assert cross_project.status_code == 409
    assert [row["session"] for row in listed.json()["consult_targets"]] == ["dev-demo"]
    assert PROJECT_KEY not in listed.text
