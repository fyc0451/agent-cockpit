"""H0 state consumer 的默认关闭与显式启用边界。"""
from __future__ import annotations

import asyncio

import pytest

import server


def test_mode_parser_defaults_off_and_rejects_unknown() -> None:
    assert server._parse_h0_state_mode(None) == "off"
    assert server._parse_h0_state_mode("") == "off"
    assert server._parse_h0_state_mode(" CANARY ") == "canary"
    assert server._parse_h0_state_mode(" ON ") == "on"
    with pytest.raises(RuntimeError, match="COCKPIT_HERDR_STATE_MODE"):
        server._parse_h0_state_mode("shadow")


def test_canary_scope_parser_and_config_are_fail_closed() -> None:
    assert server._parse_h0_canary_sessions(None) == frozenset()
    assert server._parse_h0_canary_sessions(" one,two ") == frozenset({"one", "two"})
    with pytest.raises(RuntimeError, match="CANARY_SESSIONS"):
        server._parse_h0_canary_sessions("one,,two")
    with pytest.raises(RuntimeError, match="duplicate"):
        server._parse_h0_canary_sessions("one,one")
    with pytest.raises(RuntimeError, match="required for canary"):
        server._validate_h0_state_config("canary", frozenset())
    server._validate_h0_state_config("off", frozenset())
    server._validate_h0_state_config("canary", frozenset({"one"}))


def test_canary_mode_enables_state_lifecycle(monkeypatch) -> None:
    monkeypatch.setattr(server, "H0_STATE_MODE", "canary")
    assert server._h0_state_enabled() is True


def test_runtime_snapshot_off_uses_legacy_cli(monkeypatch) -> None:
    legacy = {"available": True, "sessions": [{"session": "legacy"}]}
    monkeypatch.setattr(server, "H0_STATE_MODE", "off")
    monkeypatch.setattr(server.herdr_client, "snapshot", lambda: legacy)
    monkeypatch.setattr(
        server,
        "_state_client_snapshot",
        lambda: pytest.fail("off must not read H0 state clients"),
    )
    assert server._herdr_runtime_snapshot() is legacy


def test_runtime_snapshot_on_uses_state_cache(monkeypatch) -> None:
    cached = {"available": True, "sessions": [{"session": "cached"}]}
    monkeypatch.setattr(server, "H0_STATE_MODE", "on")
    monkeypatch.setattr(server, "_state_client_snapshot", lambda: cached)
    monkeypatch.setattr(
        server.herdr_client,
        "snapshot",
        lambda: pytest.fail("on must not fork the legacy snapshot CLI"),
    )
    assert server._herdr_runtime_snapshot() is cached


def test_runtime_snapshot_canary_only_replaces_scoped_sessions(monkeypatch) -> None:
    legacy = {
        "available": True,
        "sessions": [
            {"session": "target", "panes": [{"pane_id": "legacy-target"}], "agents": []},
            {"session": "other", "panes": [{"pane_id": "legacy-other"}], "agents": []},
        ],
    }
    cached = {
        "available": True,
        "degraded": False,
        "sessions": [
            {"session": "target", "panes": [{"pane_id": "cached-target"}], "agents": []},
        ],
    }
    monkeypatch.setattr(server, "H0_STATE_MODE", "canary")
    monkeypatch.setattr(server, "H0_STATE_CANARY_SESSIONS", frozenset({"target"}))
    legacy_calls = []
    monkeypatch.setattr(
        server.herdr_client,
        "snapshot",
        lambda **kwargs: legacy_calls.append(kwargs) or legacy,
    )
    monkeypatch.setattr(server, "_state_client_snapshot", lambda: cached)

    result = server._herdr_runtime_snapshot()

    assert result["available"] is True
    assert [pane["pane_id"] for pane in result["panes"]] == [
        "cached-target", "legacy-other",
    ]
    assert legacy_calls == [{"exclude_sessions": frozenset({"target"})}]


def test_runtime_snapshot_canary_never_falls_back_for_scoped_session(monkeypatch) -> None:
    legacy = {
        "available": True,
        "sessions": [
            {"session": "target", "panes": [{"pane_id": "legacy-target"}], "agents": []},
        ],
    }
    cached = {
        "available": False,
        "degraded": True,
        "reason": "not bootstrapped",
        "sessions": [],
    }
    monkeypatch.setattr(server, "H0_STATE_MODE", "canary")
    monkeypatch.setattr(server, "H0_STATE_CANARY_SESSIONS", frozenset({"target"}))
    monkeypatch.setattr(server.herdr_client, "snapshot", lambda **_: legacy)
    monkeypatch.setattr(server, "_state_client_snapshot", lambda: cached)

    result = server._herdr_runtime_snapshot()

    assert result["available"] is False
    assert result["degraded"] is True
    assert result["panes"] == []
    assert result["sessions"][0]["state_status"] == "unavailable"
    assert "not bootstrapped" in result["reason"]


def test_discovery_canary_only_opens_scoped_sessions(monkeypatch) -> None:
    monkeypatch.setattr(server, "H0_STATE_MODE", "canary")
    monkeypatch.setattr(server, "H0_STATE_CANARY_SESSIONS", frozenset({"target"}))
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        server.herdr_client._LIST_SESSIONS_FAILED, "value", False, raising=False
    )
    monkeypatch.setattr(
        server.herdr_client,
        "list_sessions",
        lambda: [
            {"name": "target", "status": "running", "socket": "/target.sock"},
            {"name": "other", "status": "running", "socket": "/other.sock"},
        ],
    )
    assert server._discover_running_sessions() == {
        "target": {"socket": "/target.sock", "directory": ""},
    }


def test_lifespan_off_never_opens_or_stops_state_clients(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(server, "H0_STATE_MODE", "off")
    monkeypatch.setattr(server, "_open_state_clients", lambda: calls.append("open"))
    monkeypatch.setattr(
        server, "_reconcile_state_client", lambda: calls.append("reconcile")
    )
    monkeypatch.setattr(server, "_stop_state_client", lambda: calls.append("stop"))
    monkeypatch.setattr(server, "_release_all_zoom_leases", lambda: None)

    async def waiting_loop() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "_poll_live_state", waiting_loop)
    monkeypatch.setattr(server, "_poll_message_state", waiting_loop)
    monkeypatch.setattr(server, "_worktree_cleanup_loop", waiting_loop)

    async def exercise() -> None:
        async with server.lifespan(server.app):
            await asyncio.sleep(0)

    asyncio.run(exercise())
    assert calls == []


def test_poll_off_does_not_reconcile_state_clients(monkeypatch) -> None:
    class StopLoop(Exception):
        pass

    monkeypatch.setattr(server, "H0_STATE_MODE", "off")
    monkeypatch.setattr(
        server,
        "_reconcile_state_client",
        lambda: pytest.fail("off must not reconcile H0 state clients"),
    )
    monkeypatch.setattr(server, "_expire_zoom_leases", lambda: None)
    monkeypatch.setattr(
        server,
        "_board_snapshot",
        lambda: {"available": True, "sessions": [], "panes": []},
    )
    monkeypatch.setattr(server.coordination, "maintain_live_claims", lambda snap: None)
    monkeypatch.setattr(
        server,
        "_build_attention",
        lambda snap: {
            "items": [], "mail_unread": 0, "capabilities": {}, "sessions": [],
        },
    )
    monkeypatch.setattr(server.web_push, "notify", lambda items: None)
    monkeypatch.setattr(server, "_record_poll_metrics", lambda *args: None)
    monkeypatch.setattr(
        server, "_poll_delay", lambda count: (_ for _ in ()).throw(StopLoop())
    )
    monkeypatch.setattr(
        server,
        "_live_state",
        {"revision": 0, "unread": None, "snapshot": None, "attention": None},
    )

    with pytest.raises(StopLoop):
        asyncio.run(server._poll_live_state())


def test_health_exposes_current_mode(monkeypatch) -> None:
    monkeypatch.setattr(server, "H0_STATE_MODE", "canary")
    monkeypatch.setattr(
        server, "H0_STATE_CANARY_SESSIONS", frozenset({"target"})
    )
    monkeypatch.setattr(
        server, "_agent_mail_status",
        lambda: {"available": True, "write_available": True},
    )
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(server.web_push, "public_config", lambda: {"available": True})
    result = server.health()
    assert result["herdr_state_mode"] == "canary"
    assert result["herdr_state_canary_sessions"] == ["target"]
