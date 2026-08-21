"""4.0 团队账本与本机群彻底隔离。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent_cockpit import chat_ledger
from agent_cockpit import herdr_client
from agent_cockpit import runtime_paths
from agent_cockpit import server
from agent_cockpit import settings
from agent_cockpit import team_ledger


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
    yield {"data": data}
    runtime_paths.reset_cache()


def test_append_stays_off_chat_messages(isolated_ledger):
    chat = runtime_paths.store("chat_messages")
    team = runtime_paths.store("team_messages")
    assert chat != team
    assert team.name == "team-messages.json"
    row = team_ledger.append_message(
        "agent-cockpit.general",
        hub="https://hub.example",
        kind="me",
        sender="human",
        text="hello team",
        to=["BrownDesert"],
    )
    assert row["id"].startswith("tmsg_")
    assert not chat.exists()
    saved = json.loads(team.read_text(encoding="utf-8"))
    assert saved["version"] == 1
    assert saved["messages"][0]["text"] == "hello team"
    assert chat_ledger.list_messages("cockpit") == []


def test_hand_to_leader_does_not_copy_local(isolated_ledger):
    row = team_ledger.append_message(
        "agent-cockpit.general",
        hub="https://hub.example",
        kind="agent",
        sender="peer-lead",
        text="请看这条",
        to=["BrownDesert"],
    )
    marked = team_ledger.mark_handed_to_leader(row["id"])
    assert marked is not None
    assert marked["handed_to_leader"] is True
    assert not runtime_paths.store("chat_messages").exists()
    assert chat_ledger.list_messages("cockpit") == []


def test_team_ledger_does_not_import_chat_ledger():
    assert "agent_cockpit.chat_ledger" not in getattr(team_ledger, "__dict__", {})
    assert "chat_ledger" not in team_ledger.__dict__
    import ast
    from pathlib import Path

    tree = ast.parse(Path(team_ledger.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(alias.name for alias in node.names)
    assert "chat_ledger" not in imported
    assert "agent_cockpit.chat_ledger" not in imported


HUB = "https://hub.example"
TOPIC = "agent-cockpit.general"
TEAM_JWT = "human.jwt"


def _forbidden(calls, name):
    def fail(*args, **kwargs):
        calls.append((name, args, kwargs))
        raise AssertionError(f"{name} must not be called")

    return fail


def _chat_bytes():
    path = runtime_paths.store("chat_messages")
    if not path.exists():
        return None
    return path.read_bytes()


@pytest.fixture
def team_http(isolated_ledger, monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    settings.update({
        "team_hub_url": HUB,
        "human_auth_url": "https://auth.example",
    })
    monkeypatch.setattr(
        server.hub_client,
        "public_team_config",
        lambda: {"team_hub": HUB, "human_auth": "https://auth.example"},
    )
    monkeypatch.setattr(
        server.hub_client,
        "human_profile",
        lambda authorization: {"profile": {"username": "fyc"}},
    )
    calls = []
    monkeypatch.setattr(
        chat_ledger, "append_message", _forbidden(calls, "chat_ledger.append_message"),
    )
    monkeypatch.setattr(
        server.chat_ledger,
        "append_message",
        _forbidden(calls, "server.chat_ledger.append_message"),
    )
    monkeypatch.setattr(
        herdr_client, "pane_send", _forbidden(calls, "herdr_client.pane_send"),
    )
    monkeypatch.setattr(
        server.herdr_client, "pane_send", _forbidden(calls, "server.herdr_client.pane_send"),
    )
    client = TestClient(server.app)
    client.cookies.set(server.TEAM_AUTH_COOKIE, TEAM_JWT, path="/api")
    return {
        "client": client,
        "headers": {"authorization": "Bearer secret"},
        "calls": calls,
    }


def test_http_send_stays_off_chat_messages(team_http):
    before = _chat_bytes()
    response = team_http["client"].post(
        "/api/team/ledger/messages",
        headers=team_http["headers"],
        json={"topic": TOPIC, "text": "hello team", "to": ["BrownDesert"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"]["text"] == "hello team"
    assert body["message"]["id"].startswith("tmsg_")
    assert team_http["calls"] == []
    assert _chat_bytes() == before
    assert not runtime_paths.store("chat_messages").exists()
    saved = json.loads(runtime_paths.store("team_messages").read_text(encoding="utf-8"))
    assert saved["messages"][0]["text"] == "hello team"
    assert chat_ledger.list_messages("cockpit") == []


def test_http_receive_hub_history_does_not_copy_local(team_http):
    before = _chat_bytes()
    response = team_http["client"].post(
        "/api/team/ledger/receive",
        headers=team_http["headers"],
        json={
            "topic": TOPIC,
            "text": "hub history line",
            "sender": "peer-lead",
            "to": ["human"],
            "kind": "agent",
            "ts": 1_700_000_000_000,
        },
    )
    assert response.status_code == 200
    assert response.json()["message"]["text"] == "hub history line"
    assert team_http["calls"] == []
    assert _chat_bytes() == before
    assert not runtime_paths.store("chat_messages").exists()
    listed = team_http["client"].get(
        "/api/team/ledger/messages",
        headers=team_http["headers"],
        params={"topic": TOPIC},
    )
    assert listed.status_code == 200
    assert listed.json()["messages"][0]["text"] == "hub history line"
    assert chat_ledger.list_messages("cockpit") == []


def test_http_hand_to_leader_marks_team_ledger_without_pane_send(team_http):
    created = team_http["client"].post(
        "/api/team/ledger/messages",
        headers=team_http["headers"],
        json={"topic": TOPIC, "text": "请看这条", "sender": "peer-lead", "kind": "agent"},
    )
    assert created.status_code == 200
    message_id = created.json()["message"]["id"]
    before = _chat_bytes()
    response = team_http["client"].post(
        f"/api/team/ledger/messages/{message_id}/hand-to-leader",
        headers=team_http["headers"],
    )
    assert response.status_code == 200
    marked = response.json()["message"]
    assert marked["id"] == message_id
    assert marked["handed_to_leader"] is True
    assert team_http["calls"] == []
    assert _chat_bytes() == before
    assert not runtime_paths.store("chat_messages").exists()
    assert chat_ledger.list_messages("cockpit") == []


def test_hub_chat_history_proxy_does_not_write_local_chat(team_http, monkeypatch):
    hub_payload = {
        "messages": [{"id": 9, "body_md": "remote only", "thread": "hub-thread"}],
    }
    monkeypatch.setattr(
        server.hub_client,
        "human_api",
        lambda method, path, authorization, payload=None: hub_payload,
    )
    sentinel = runtime_paths.store("chat_messages")
    sentinel.write_text('{"version":1,"messages":[]}\n', encoding="utf-8")
    before = sentinel.read_bytes()
    team_before = (
        runtime_paths.store("team_messages").read_bytes()
        if runtime_paths.store("team_messages").exists() else None
    )
    response = team_http["client"].get(
        "/api/team/projects/demo/chat/messages",
        headers=team_http["headers"],
    )
    assert response.status_code == 200
    assert response.json() == hub_payload
    assert team_http["calls"] == []
    assert sentinel.read_bytes() == before
    after_team = (
        runtime_paths.store("team_messages").read_bytes()
        if runtime_paths.store("team_messages").exists() else None
    )
    assert after_team == team_before
    assert chat_ledger.list_messages("cockpit") == []


def test_unconfigured_hub_keeps_team_ledger_http_absent(isolated_ledger, monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    settings.update({"team_hub_url": "", "human_auth_url": ""})
    calls = []
    monkeypatch.setattr(
        chat_ledger, "append_message", _forbidden(calls, "chat_ledger.append_message"),
    )
    monkeypatch.setattr(
        server.chat_ledger,
        "append_message",
        _forbidden(calls, "server.chat_ledger.append_message"),
    )
    monkeypatch.setattr(
        server.herdr_client, "pane_send", _forbidden(calls, "pane_send"),
    )
    client = TestClient(server.app)
    client.cookies.set(server.TEAM_AUTH_COOKIE, TEAM_JWT, path="/api")
    headers = {"authorization": "Bearer secret"}
    send = client.post(
        "/api/team/ledger/messages",
        headers=headers,
        json={"topic": TOPIC, "text": "should not land"},
    )
    receive = client.post(
        "/api/team/ledger/receive",
        headers=headers,
        json={"topic": TOPIC, "text": "should not land", "sender": "peer"},
    )
    hand = client.post(
        "/api/team/ledger/messages/tmsg_aaaaaaaaaaaa/hand-to-leader",
        headers=headers,
    )
    listed = client.get(
        "/api/team/ledger/messages",
        headers=headers,
        params={"topic": TOPIC},
    )
    assert send.status_code == receive.status_code == hand.status_code == listed.status_code == 404
    assert calls == []
    assert not runtime_paths.store("chat_messages").exists()
    assert not runtime_paths.store("team_messages").exists()
