from fastapi.testclient import TestClient

from agent_cockpit import hub_client
import server
from agent_cockpit import team_sessions


HUB = "http://team.example"
PROJECT = "/work/demo"


def _prepare(monkeypatch, *, ready=True, client_host="127.0.0.1"):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "cockpit-secret")
    monkeypatch.setattr(
        server.hub_client,
        "public_team_config",
        lambda: {"team_hub": HUB, "human_auth": "http://auth.example"},
    )
    monkeypatch.setattr(server, "_registry_scan", lambda: [{
        "project_key": PROJECT,
        "name": "codex-main",
        "registration_token": "registration-secret",
    }])
    monkeypatch.setattr(server, "_team_session_candidates", lambda: [{
        "session": "demo",
        "generation": "run-1",
        "mail_project": PROJECT,
        "ready": ready,
        "lead": {"mail_name": "codex-main"},
    }])
    team_sessions.bind(
        hub=HUB,
        human_id=7,
        project_slug="core",
        session="demo",
        session_generation="run-1",
        session_dir="/session/demo",
        mail_project=PROJECT,
        lead={"mail_name": "codex-main", "agent": "codex"},
        client_session_id="client-1",
        agent_id=41,
        reply_token="reply-secret",
    )
    return TestClient(server.app, client=(client_host, 50000))


def _payload(**overrides):
    value = {
        "mail_project": PROJECT,
        "sender_name": "codex-main",
        "registration_token": "registration-secret",
        "mention_handles": ["fyc-mac"],
        "subject": "需要支持",
        "body_md": "请看这个问题",
        "importance": "normal",
        "idempotency_key": "reply-1",
    }
    value.update(overrides)
    return value


def test_local_lead_can_proxy_reply_without_cockpit_login_token(monkeypatch):
    client = _prepare(monkeypatch)
    calls = []

    def reply(project_slug, payload):
        calls.append((project_slug, payload))
        return {
            "status": "delivered",
            "message_id": 91,
            "deliveries": [{
                "name": "fyc-mac",
                "status": "delivered_human_inbox",
                "receipt_message_id": 92,
                "reason": None,
                "private": "must-not-pass",
            }],
        }

    monkeypatch.setattr(server.hub_client, "session_lead_reply", reply)

    response = client.post("/api/agent/team-reply", json=_payload())

    assert response.status_code == 200
    assert response.json()["message_id"] == 91
    assert calls[0][0] == "core"
    assert calls[0][1]["reply_token"] == "reply-secret"
    assert calls[0][1]["client_session_id"] == "client-1"
    assert "registration-secret" not in response.text
    assert "reply-secret" not in response.text
    assert "private" not in response.text


def test_wrong_local_identity_or_stopped_session_is_opaque(monkeypatch):
    client = _prepare(monkeypatch)
    monkeypatch.setattr(
        server.hub_client,
        "session_lead_reply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not call")),
    )

    wrong = client.post(
        "/api/agent/team-reply",
        json=_payload(registration_token="wrong-secret"),
    )
    assert wrong.status_code == 403
    assert wrong.json()["detail"] == "Invalid reply credentials"
    assert "wrong-secret" not in wrong.text

    monkeypatch.setattr(server, "_team_session_candidates", lambda: [])
    stopped = client.post("/api/agent/team-reply", json=_payload())
    assert stopped.status_code == 403
    assert stopped.json()["detail"] == "Invalid reply credentials"


def test_agent_reply_rejects_non_loopback_even_with_valid_registration(monkeypatch):
    client = _prepare(monkeypatch, client_host="10.0.0.2")
    monkeypatch.setattr(
        server.hub_client,
        "session_lead_reply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not call")),
    )

    response = client.post("/api/agent/team-reply", json=_payload())

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid reply credentials"


def test_agent_reply_hides_upstream_credential_detail(monkeypatch):
    client = _prepare(monkeypatch)

    def denied(*_args, **_kwargs):
        raise hub_client.HumanAPIError(403, "reply-secret belongs to binding 7")

    monkeypatch.setattr(server.hub_client, "session_lead_reply", denied)

    response = client.post("/api/agent/team-reply", json=_payload())

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid reply credentials"
    assert "reply-secret" not in response.text


def test_team_work_body_requires_same_active_local_lead(monkeypatch):
    client = _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(
        server.team_lead_worker,
        "next_for_binding",
        lambda binding: calls.append(binding) or {
            "work_id": "a" * 32,
            "reply_mode": "confirm",
            "state": "pending",
            "message": {"body_md": "remote body"},
        },
    )

    valid = client.post("/api/agent/team-work/next", json={
        "mail_project": PROJECT,
        "sender_name": "codex-main",
        "registration_token": "registration-secret",
    })
    invalid = client.post("/api/agent/team-work/next", json={
        "mail_project": PROJECT,
        "sender_name": "codex-main",
        "registration_token": "wrong",
    })

    assert valid.status_code == 200
    assert valid.json()["work"]["message"]["body_md"] == "remote body"
    assert invalid.status_code == 403
    assert len(calls) == 1


def test_team_work_caller_cannot_select_session_or_pane(monkeypatch):
    client = _prepare(monkeypatch)
    monkeypatch.setattr(
        server.team_lead_worker,
        "next_for_binding",
        lambda _binding: (_ for _ in ()).throw(AssertionError("must not read")),
    )

    response = client.post("/api/agent/team-work/next", json={
        "mail_project": PROJECT,
        "sender_name": "codex-main",
        "registration_token": "registration-secret",
        "session": "victim",
        "pane_id": "victim-pane",
    })

    assert response.status_code == 400


def test_team_work_respond_uses_persisted_mode_and_hides_secrets(monkeypatch):
    client = _prepare(monkeypatch)
    calls = []

    def respond(work_id, binding, response, **callbacks):
        calls.append((work_id, binding, response, callbacks))
        return {"status": "replied", "message_id": 8}

    monkeypatch.setattr(server.team_lead_worker, "respond", respond)
    payload = {
        "mail_project": PROJECT,
        "sender_name": "codex-main",
        "registration_token": "registration-secret",
        "mention_handles": ["fyc-mac"],
        "subject": "Re: support",
        "body_md": "done",
        "importance": "normal",
    }

    response = client.post(f"/api/agent/team-work/{'a' * 32}/respond", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "replied", "message_id": 8}
    assert calls[0][0] == "a" * 32
    assert "reply_mode" not in calls[0][2]
    assert set(calls[0][3]) == {"direct_reply", "complete"}
    assert "reply-secret" not in response.text
    assert "registration-secret" not in response.text


def test_worker_tick_only_uses_current_active_lead_pane(monkeypatch):
    _prepare(monkeypatch)
    monkeypatch.setattr(server, "_team_session_candidates", lambda: [
        {
            "session": "demo", "generation": "run-1", "mail_project": PROJECT,
            "ready": True, "status": "working",
            "lead": {
                "mail_name": "codex-main", "pane_id": "active-pane",
                "status": "idle",
            },
        },
        {
            "session": "done", "generation": "run-2", "mail_project": PROJECT,
            "ready": True, "status": "done",
            "lead": {
                "mail_name": "codex-main", "pane_id": "stale-pane",
                "status": "done",
            },
        },
    ])
    calls = []
    monkeypatch.setattr(
        server.team_lead_worker, "poll_binding",
        lambda binding, candidate, **_kwargs: calls.append((binding, candidate)),
    )

    server._team_lead_worker_tick()

    assert len(calls) == 1
    assert calls[0][1]["lead"]["pane_id"] == "active-pane"
