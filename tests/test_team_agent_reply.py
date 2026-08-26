import time

import pytest
from fastapi.testclient import TestClient

from agent_cockpit import hub_client
import server
from agent_cockpit import team_sessions


HUB = "http://team.example"
PROJECT = "/work/demo"


def _prepare(
    monkeypatch, *, ready=True, client_host="127.0.0.1", reply_mode="confirm",
):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "cockpit-secret")
    monkeypatch.setattr(
        server.hub_client,
        "public_team_config",
        lambda: {"team_hub": HUB, "human_auth": "http://auth.example"},
    )
    monkeypatch.setattr(server.hub_client, "session_lead_status", lambda *_args: {})
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
        managed_runtime=True,
        reply_token="reply-secret",
        reply_mode=reply_mode,
        auth_expires_at=time.time() + 3600,
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
    monkeypatch.setattr(
        server,
        "_context_pack_for_binding",
        lambda _binding: {
            "version": 1,
            "project": {"key": "demo"},
            "fingerprint": "f" * 64,
        },
    )
    monkeypatch.setattr(
        server.team_lead_worker,
        "attach_context_pack",
        lambda _work_id, _binding, pack: pack,
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
    assert valid.json()["work"]["context_pack"] == {
        "version": 1,
        "project": {"key": "demo"},
        "fingerprint": "f" * 64,
    }
    assert invalid.status_code == 403
    assert len(calls) == 1


def test_team_work_context_pack_failure_is_bounded_and_does_not_hide_work(
    monkeypatch,
):
    client = _prepare(monkeypatch)
    monkeypatch.setattr(
        server.team_lead_worker,
        "next_for_binding",
        lambda _binding: {
            "work_id": "a" * 32,
            "reply_mode": "confirm",
            "state": "pending",
            "message": {"body_md": "remote body"},
        },
    )
    monkeypatch.setattr(
        server.team_context_pack,
        "build_context_pack",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("secret path")),
    )
    monkeypatch.setattr(
        server.team_lead_worker,
        "attach_context_pack",
        lambda _work_id, _binding, pack: pack,
    )

    response = client.post("/api/agent/team-work/next", json={
        "mail_project": PROJECT,
        "sender_name": "codex-main",
        "registration_token": "registration-secret",
    })

    assert response.status_code == 200
    assert response.json()["work"]["message"]["body_md"] == "remote body"
    assert response.json()["work"]["context_pack"] == {
        "version": 1, "available": False, "reason": "unavailable",
    }
    assert "secret path" not in response.text


def test_context_pack_exposes_only_explicit_development_lead_status(monkeypatch):
    _prepare(monkeypatch)
    candidates = [{
        "session": "dev-demo",
        "generation": "dev-run-1",
        "mail_project": PROJECT,
        "ready": True,
        "status": "idle",
        "lead": {
            "agent": "codex",
            "mail_name": "dev-main",
            "participant_id": "dev-lead",
            "status": "idle",
        },
    }]
    team_sessions.set_consult_target(
        hub=HUB,
        human_id=7,
        project_slug="core",
        target={
            "session": "dev-demo",
            "session_generation": "dev-run-1",
            "mail_project": PROJECT,
            "lead": candidates[0]["lead"],
        },
    )
    monkeypatch.setattr(server, "_team_session_candidates", lambda: candidates)
    captured = {}
    monkeypatch.setattr(
        server.team_context_pack,
        "build_context_pack",
        lambda **kwargs: captured.update(kwargs) or {"version": 1},
    )

    binding = team_sessions.list_bindings(HUB, 7)[0]
    assert server._context_pack_for_binding(binding) == {"version": 1}
    assert captured == {
        "workspace": PROJECT,
        "development_lead": {"available": True, "status": "idle"},
    }


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


@pytest.mark.parametrize(
    ("generation", "mail_name"),
    [("run-2", "codex-main"), ("run-1", "replacement-main")],
)
def test_team_work_respond_rejects_changed_source_identity_before_hub(
    monkeypatch, generation, mail_name,
):
    client = _prepare(monkeypatch, reply_mode="auto")
    candidate = {
        "session": "demo", "generation": "run-1", "mail_project": PROJECT,
        "ready": True, "status": "done",
        "lead": {
            "agent": "codex", "mail_name": "codex-main",
            "pane_id": "team-pane", "status": "done",
        },
    }
    monkeypatch.setattr(server, "_team_session_candidates", lambda: [candidate])
    binding = team_sessions.list_bindings(HUB, 7)[0]
    claimed = server.team_lead_worker.poll_binding(
        binding, candidate,
        claim=lambda *_args: {
            "status": "claimed", "claim_token": "claim-secret",
            "claim_expires_at": "2099-01-01T00:00:00+00:00",
            "reply_mode": "auto",
            "message": {
                "inbox_item_id": 5, "message_id": 6, "subject": "remote",
                "body_md": "REMOTE SECRET", "importance": "normal",
                "sender_name": "Alice", "sender_handle": "alice",
                "created_ts": "2026-08-25T10:00:00+08:00",
            },
        },
        notify=lambda *_args: True,
    )
    candidate["generation"] = generation
    candidate["lead"]["mail_name"] = mail_name
    monkeypatch.setattr(
        server.hub_client, "session_lead_reply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale source must not reply")
        ),
    )
    monkeypatch.setattr(
        server.hub_client, "session_lead_complete",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale source must not complete")
        ),
    )

    response = client.post(
        f"/api/agent/team-work/{claimed['work_id']}/respond",
        json={
            "mail_project": PROJECT, "sender_name": "codex-main",
            "registration_token": "registration-secret",
            "mention_handles": ["fyc-mac"], "subject": "Re: remote",
            "body_md": "done", "importance": "normal",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid reply credentials"
    assert server.team_lead_worker.work_for_binding(
        claimed["work_id"], binding,
    )["state"] == "pending"
    assert server.team_lead_worker.reply_evidence_for_binding(HUB, "core") == {}


def test_consult_bridge_uses_explicit_same_project_lead_and_fixed_prompt(monkeypatch):
    client = _prepare(monkeypatch)
    monkeypatch.setattr(server, "_registry_scan", lambda: [
        {
            "project_key": PROJECT, "name": "codex-main",
            "registration_token": "registration-secret",
        },
        {
            "project_key": PROJECT, "name": "dev-main",
            "registration_token": "dev-secret",
        },
    ])
    candidates = [
        {
            "session": "demo", "generation": "run-1", "mail_project": PROJECT,
            "ready": True, "status": "done",
            "lead": {
                "agent": "codex", "mail_name": "codex-main",
                "pane_id": "team-pane", "status": "done",
            },
        },
        {
            "session": "dev-demo", "generation": "dev-run-1",
            "mail_project": PROJECT, "ready": True, "status": "idle",
            "lead": {
                "agent": "codex", "mail_name": "dev-main",
                "pane_id": "dev-pane", "participant_id": "dev-lead",
                "status": "idle",
            },
        },
    ]
    monkeypatch.setattr(server, "_team_session_candidates", lambda: candidates)
    binding = team_sessions.list_bindings(HUB, 7)[0]
    team_sessions.set_consult_target(
        hub=HUB, human_id=7, project_slug="core",
        target={
            "session": "dev-demo", "session_generation": "dev-run-1",
            "mail_project": PROJECT,
            "lead": {
                "agent": "codex", "mail_name": "dev-main",
                "participant_id": "dev-lead",
            },
        },
    )
    binding = team_sessions.list_bindings(HUB, 7)[0]
    claimed = server.team_lead_worker.poll_binding(
        binding, candidates[0],
        claim=lambda *_args: {
            "status": "claimed", "claim_token": "claim-secret",
            "claim_expires_at": "2099-01-01T00:00:00+00:00",
            "reply_mode": "confirm",
            "message": {
                "inbox_item_id": 3, "message_id": 4, "subject": "remote",
                "body_md": "REMOTE SECRET", "importance": "normal",
                "sender_name": "Alice", "sender_handle": "alice",
                "created_ts": "2026-08-25T10:00:00+08:00",
            },
        },
        notify=lambda *_args: True,
    )
    prompts = []
    monkeypatch.setattr(
        server, "_notify_team_lead_work",
        lambda session, pane, prompt: prompts.append((session, pane, prompt)) or True,
    )
    team_identity = {
        "mail_project": PROJECT, "sender_name": "codex-main",
        "registration_token": "registration-secret",
    }

    created = client.post(
        f"/api/agent/team-work/{claimed['work_id']}/consult",
        json={**team_identity, "kind": "evidence", "question": "数据库迁移通过了吗？"},
    )

    assert created.status_code == 200
    request_id = created.json()["consult"]["request_id"]
    assert prompts == [("dev-demo", "dev-pane", prompts[0][2])]
    assert request_id in prompts[0][2]
    assert "数据库迁移通过了吗" not in prompts[0][2]
    assert "REMOTE SECRET" not in prompts[0][2]

    next_response = client.post("/api/agent/project-consult/next", json={
        "mail_project": PROJECT, "sender_name": "dev-main",
        "registration_token": "dev-secret",
    })
    assert next_response.status_code == 200
    assert next_response.json()["consult"]["question"] == "数据库迁移通过了吗？"

    answer_payload = {
        "mail_project": PROJECT, "sender_name": "dev-main",
        "registration_token": "dev-secret", "response": "迁移测试已通过",
    }
    answered = client.post(
        f"/api/agent/project-consult/{request_id}/respond", json=answer_payload,
    )
    replay = client.post(
        f"/api/agent/project-consult/{request_id}/respond", json=answer_payload,
    )
    status = client.post(
        f"/api/agent/team-work/{claimed['work_id']}/consult/status",
        json=team_identity,
    )
    assert answered.status_code == replay.status_code == status.status_code == 200
    assert status.json()["consult"]["response"] == "迁移测试已通过"


@pytest.mark.parametrize("restarted", ["source", "target"])
def test_consult_bridge_fails_closed_after_session_generation_changes(
    monkeypatch, restarted,
):
    client = _prepare(monkeypatch)
    monkeypatch.setattr(server, "_registry_scan", lambda: [
        {
            "project_key": PROJECT, "name": "codex-main",
            "registration_token": "registration-secret",
        },
        {
            "project_key": PROJECT, "name": "dev-main",
            "registration_token": "dev-secret",
        },
    ])
    target = {
        "session": "dev-demo", "generation": "dev-run-1",
        "mail_project": PROJECT, "ready": True, "status": "idle",
        "lead": {
            "agent": "codex", "mail_name": "dev-main",
            "pane_id": "dev-pane", "status": "idle",
        },
    }
    team_sessions.set_consult_target(
        hub=HUB, human_id=7, project_slug="core",
        target={
            "session": "dev-demo", "session_generation": "dev-run-1",
            "mail_project": PROJECT,
            "lead": {"agent": "codex", "mail_name": "dev-main"},
        },
    )
    binding = team_sessions.list_bindings(HUB, 7)[0]
    work_id = server.team_lead_worker.poll_binding(
        binding,
        {"session": "demo", "generation": "run-1", "lead": {"pane_id": "p"}},
        claim=lambda *_args: {
            "status": "claimed", "claim_token": "claim",
            "claim_expires_at": "2099-01-01T00:00:00+00:00",
            "reply_mode": "auto",
            "message": {
                "inbox_item_id": 1, "message_id": 2, "subject": "s", "body_md": "b",
                "importance": "normal", "sender_name": "a", "sender_handle": "a",
                "created_ts": "2026-08-25T00:00:00Z",
            },
        }, notify=lambda *_args: True,
    )["work_id"]
    source = {
        "session": "demo", "generation": "run-1", "mail_project": PROJECT,
        "ready": True, "status": "done",
        "lead": {"agent": "codex", "mail_name": "codex-main", "status": "done"},
    }
    monkeypatch.setattr(server, "_team_session_candidates", lambda: [source, target])
    created = client.post(f"/api/agent/team-work/{work_id}/consult", json={
        "mail_project": PROJECT, "sender_name": "codex-main",
        "registration_token": "registration-secret",
        "kind": "status", "question": "状态？",
    })
    assert created.status_code == 200
    request_id = created.json()["consult"]["request_id"]
    if restarted == "source":
        source["generation"] = "run-2"
    else:
        target["generation"] = "dev-run-2"

    response = client.post(
        f"/api/agent/project-consult/{request_id}/respond",
        json={
            "mail_project": PROJECT, "sender_name": "dev-main",
            "registration_token": "dev-secret", "response": "不得送达",
        },
    )
    denied = client.post("/api/agent/project-consult/next", json={
        "mail_project": PROJECT, "sender_name": "dev-main",
        "registration_token": "dev-secret",
    })
    snapshot = server.team_lead_worker.consult_snapshot(request_id)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Team Agent 已重启或身份变化"
        if restarted == "source"
        else "咨询目标已重启或身份变化"
    )
    assert denied.status_code == 200
    assert denied.json()["status"] == "empty"
    assert snapshot["state"] == "invalidated"
    assert snapshot["failure_reason"] == f"{restarted}_identity_changed"


@pytest.mark.parametrize("reply_mode", ["confirm", "auto"])
def test_worker_tick_wakes_running_done_lead(monkeypatch, reply_mode):
    _prepare(monkeypatch, reply_mode=reply_mode)
    monkeypatch.setattr(server, "_team_session_candidates", lambda: [{
        "session": "demo", "generation": "run-1", "mail_project": PROJECT,
        "ready": True, "status": "done",
        "lead": {
            "agent": "codex", "mail_name": "codex-main",
            "pane_id": "done-pane", "status": "done",
        },
    }])
    calls = []
    statuses = []
    monkeypatch.setattr(
        server.hub_client, "session_lead_status",
        lambda slug, payload: statuses.append((slug, payload)) or {},
    )
    monkeypatch.setattr(
        server.team_lead_worker, "poll_binding",
        lambda binding, candidate, **_kwargs: calls.append((binding, candidate)),
    )

    server._team_lead_worker_tick()

    assert len(calls) == 1
    assert calls[0][0]["reply_mode"] == reply_mode
    assert calls[0][1]["lead"]["pane_id"] == "done-pane"
    assert statuses == [("core", {
        "client_session_id": "client-1",
        "reply_token": "reply-secret",
        "status": "idle",
    })]


def test_worker_tick_status_failure_does_not_block_inbox(monkeypatch):
    _prepare(monkeypatch)
    monkeypatch.setattr(server, "_team_session_candidates", lambda: [{
        "session": "demo", "generation": "run-1", "mail_project": PROJECT,
        "ready": True, "status": "working",
        "lead": {
            "agent": "codex", "mail_name": "codex-main",
            "pane_id": "pane-1", "status": "working",
        },
    }])
    monkeypatch.setattr(
        server.hub_client,
        "session_lead_status",
        lambda *_args: (_ for _ in ()).throw(server.hub_client.HumanAPIError(502, "down")),
    )
    calls = []
    monkeypatch.setattr(
        server.team_lead_worker, "poll_binding",
        lambda binding, candidate, **_kwargs: calls.append((binding, candidate)),
    )

    server._team_lead_worker_tick()

    assert len(calls) == 1


@pytest.mark.parametrize("status", ["stopped", "unknown"])
def test_worker_tick_rejects_non_running_lead(monkeypatch, status):
    _prepare(monkeypatch)
    monkeypatch.setattr(server, "_team_session_candidates", lambda: [{
        "session": "demo", "generation": "run-1", "mail_project": PROJECT,
        "ready": True, "status": status,
        "lead": {
            "agent": "codex", "mail_name": "codex-main",
            "pane_id": "inactive-pane", "status": status,
        },
    }])
    calls = []
    monkeypatch.setattr(
        server.team_lead_worker, "poll_binding",
        lambda binding, candidate, **_kwargs: calls.append((binding, candidate)),
    )

    server._team_lead_worker_tick()

    assert calls == []


@pytest.mark.parametrize(
    ("generation", "mail_name"),
    [("run-2", "codex-main"), ("run-1", "other-lead")],
)
def test_worker_tick_rejects_generation_or_identity_mismatch(
    monkeypatch, generation, mail_name,
):
    _prepare(monkeypatch)
    monkeypatch.setattr(server, "_team_session_candidates", lambda: [{
        "session": "demo", "generation": generation, "mail_project": PROJECT,
        "ready": True, "status": "done",
        "lead": {
            "agent": "codex", "mail_name": mail_name,
            "pane_id": "mismatched-pane", "status": "done",
        },
    }])
    calls = []
    monkeypatch.setattr(
        server.team_lead_worker, "poll_binding",
        lambda binding, candidate, **_kwargs: calls.append((binding, candidate)),
    )

    server._team_lead_worker_tick()

    assert calls == []
