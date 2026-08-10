"""H0 state consumer 的默认关闭与显式启用边界。"""
from __future__ import annotations

import asyncio

import pytest

import server


def test_mode_parser_defaults_off_and_rejects_unknown() -> None:
    assert server._parse_h0_state_mode(None) == "off"
    assert server._parse_h0_state_mode("") == "off"
    assert server._parse_h0_state_mode(" ON ") == "on"
    with pytest.raises(RuntimeError, match="COCKPIT_HERDR_STATE_MODE"):
        server._parse_h0_state_mode("shadow")


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
    monkeypatch.setattr(server, "H0_STATE_MODE", "off")
    monkeypatch.setattr(
        server, "_agent_mail_status",
        lambda: {"available": True, "write_available": True},
    )
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(server.web_push, "public_config", lambda: {"available": True})
    assert server.health()["herdr_state_mode"] == "off"
