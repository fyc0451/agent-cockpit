from fastapi.testclient import TestClient

import server
import team_sessions


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
    assert "identity_id" not in response.text
    assert "registration-secret" not in response.text
    assert "pane_id" not in response.text
    assert "participant_id" not in response.text


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
    )

    response = client.get("/api/team-auth/session-bindings", headers=headers)

    assert response.status_code == 200
    binding = response.json()["bindings"][0]
    assert binding["active"] is True
    assert binding["ready"] is False
    assert binding["reason"] == "负责人通信凭据需要重新同步"
    assert "reply_token" not in response.text


def test_inbox_route_fetches_read_items_for_pending_retry(monkeypatch):
    client, headers = _prepare(monkeypatch)
    calls = []
    regular = _human_api(calls=calls)

    def human_api(method, path, authorization, payload=None):
        if method == "GET" and path.startswith("/hub/api/inbox?"):
            calls.append((method, path, authorization, payload))
            return {"items": []}
        return regular(method, path, authorization, payload)

    def route_inbox(authorization, *, hub, human_id, fetch_inbox):
        assert hub == HUB
        assert human_id == 7
        assert fetch_inbox(authorization) == {"items": []}
        return {"fetched": 0, "delivered": 0, "pending": 0}

    monkeypatch.setattr(server.hub_client, "human_api", human_api)
    monkeypatch.setattr(server.team_inbox_router, "route_inbox", route_inbox)

    response = client.post("/api/team-auth/inbox-route/route", headers=headers)

    assert response.status_code == 200
    assert calls[-1][1] == "/hub/api/inbox?limit=100"
    assert "unread_only" not in calls[-1][1]


def test_bind_creates_managed_lead_and_saves_local_mapping(monkeypatch):
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
    assert "reply-secret" not in response.text


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
