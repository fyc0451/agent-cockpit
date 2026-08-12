"""B0 R6 runtime rollout gates and scope-bound control authorization."""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest

from agent_cockpit import b0_wiring
from agent_cockpit import leader_binding

from tests.test_b0_wiring import (  # noqa: F401
    ISSUER, _RecordingAdapter, binding_db, registry,
)


@pytest.mark.parametrize("raw, expected", [
    (None, "off"), ("", "off"), (" OFF ", "off"),
    ("shadow", "shadow"), ("canary", "canary"), ("on", "on"),
])
def test_parse_b0_mode(raw: str | None, expected: str) -> None:
    import server

    assert server._parse_b0_mode(raw) == expected


def test_parse_b0_mode_rejects_unknown() -> None:
    import server

    with pytest.raises(RuntimeError, match="COCKPIT_B0_MODE"):
        server._parse_b0_mode("enabled")


def test_parse_b0_canary_scopes_is_strict() -> None:
    import server

    assert server._parse_b0_canary_scopes(
        " user/default,team/team-1,channel/a/b "
    ) == frozenset({
        ("user", "default"), ("team", "team-1"), ("channel", "a/b"),
    })
    for raw in ("user", "other/x", "team/", "user/a,user/a", "user/a,,team/b"):
        with pytest.raises(RuntimeError, match="COCKPIT_B0_CANARY_SCOPES"):
            server._parse_b0_canary_scopes(raw)


def test_canary_requires_nonempty_scope() -> None:
    import server

    with pytest.raises(RuntimeError, match="COCKPIT_B0_CANARY_SCOPES"):
        server._validate_b0_config("canary", frozenset())
    server._validate_b0_config("off", frozenset())
    server._validate_b0_config("shadow", frozenset())
    server._validate_b0_config("on", frozenset())


def test_scope_enablement_by_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import server

    monkeypatch.setattr(server, "B0_CANARY_SCOPES", frozenset({("team", "t1")}))
    monkeypatch.setattr(server, "B0_MODE", "off")
    assert not server._b0_scope_enabled("team", "t1")
    monkeypatch.setattr(server, "B0_MODE", "shadow")
    assert not server._b0_scope_enabled("team", "t1")
    monkeypatch.setattr(server, "B0_MODE", "canary")
    assert server._b0_scope_enabled("team", "t1")
    assert not server._b0_scope_enabled("team", "t2")
    monkeypatch.setattr(server, "B0_MODE", "on")
    assert server._b0_scope_enabled("team", "t2")


def test_runtime_tick_only_pulls_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    import server

    calls: list[bool] = []

    class Coordinator:
        def sync_bindings(self):
            return []

        def fanout_control_events(self, **_kwargs):
            return 0

        def poll_once(self, *, unread_only: bool):
            calls.append(unread_only)
            return {}

        def state(self):
            return {"scopes": {}, "last_reasons": {}}

    monkeypatch.setattr(server, "B0_MODE", "canary")
    monkeypatch.setattr(server, "_b0_get_coordinator", lambda: Coordinator())

    server._b0_poll_tick()

    assert calls == [True]


def test_off_does_not_construct_coordinator(monkeypatch: pytest.MonkeyPatch) -> None:
    import server

    monkeypatch.setattr(server, "B0_MODE", "off")
    monkeypatch.setattr(server, "_b0_coordinator", None)
    monkeypatch.setattr(
        server.b0_wiring, "B0Coordinator",
        lambda *a, **kw: pytest.fail("off mode constructed coordinator"),
    )
    assert server._b0_get_coordinator() is None
    server._b0_poll_tick()


def test_shadow_tick_only_runs_readonly_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    import server

    expected = {
        "available": False, "degraded": True,
        "reason": "binding_store_incompatible", "scopes": 0,
    }
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(server, "B0_MODE", "shadow")
    monkeypatch.setattr(server, "_b0_coordinator", None)
    monkeypatch.setattr(
        server.b0_wiring, "B0Coordinator",
        lambda *a, **kw: pytest.fail("shadow mode constructed coordinator"),
    )
    monkeypatch.setattr(
        server.b0_wiring, "shadow_probe",
        lambda issuer, scope_filter=None: calls.append((issuer, scope_filter)) or expected,
    )
    server._b0_poll_tick()
    assert calls and calls[0][0] == server.B0_ISSUER
    assert server._b0_shadow_state == expected


def test_shadow_probe_does_not_migrate_legacy_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE leader_bindings (scope_kind TEXT, scope_id TEXT, "
        "mail_name TEXT, state TEXT, binding_version INTEGER)"
    )
    con.execute(
        "INSERT INTO leader_bindings VALUES ('team','t1','legacy','active',1)"
    )
    con.commit()
    con.close()
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    monkeypatch.setattr(leader_binding, "DB_PATH", path)
    real_connect = sqlite3.connect
    opened: list[str] = []

    def record_connect(database, *args, **kwargs):
        opened.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(b0_wiring.sqlite3, "connect", record_connect)

    state = b0_wiring.shadow_probe(ISSUER)

    assert state["degraded"] is True
    assert state["reason"] == "binding_store_incompatible"
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime
    assert opened and opened[0].endswith("?mode=ro&immutable=1")


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_shadow_probe_rejects_live_sidecar_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    path = tmp_path / "leader-binding.sqlite3"
    path.write_bytes(b"not-opened")
    sidecar = Path(str(path) + suffix)
    sidecar.write_bytes(b"sidecar-state")
    before = {
        item: (hashlib.sha256(item.read_bytes()).hexdigest(), item.stat().st_mtime_ns)
        for item in (path, sidecar)
    }
    monkeypatch.setattr(leader_binding, "DB_PATH", path)
    monkeypatch.setattr(
        b0_wiring.sqlite3, "connect",
        lambda *a, **kw: pytest.fail("live sidecar must gate before SQLite open"),
    )

    state = b0_wiring.shadow_probe(ISSUER)

    assert state["available"] is False
    assert state["degraded"] is True
    assert state["reason"] == "probe_requires_quiescence"
    for item, expected in before.items():
        assert hashlib.sha256(item.read_bytes()).hexdigest() == expected[0]
        assert item.stat().st_mtime_ns == expected[1]


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_lifespan_inactive_modes_do_not_install_claim_gate_or_rebuild(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    import server

    calls: list[str] = []
    monkeypatch.setattr(server, "B0_MODE", mode)
    monkeypatch.setattr(server, "H0_STATE_MODE", "off")
    monkeypatch.setattr(
        server.b0_wiring, "install_claim_gate",
        lambda **kw: pytest.fail(f"{mode} installed claim gate"),
    )
    monkeypatch.setattr(
        server.b0_wiring, "uninstall_claim_gate",
        lambda: calls.append("uninstall"),
    )
    monkeypatch.setattr(
        server, "_b0_rebuild_on_start",
        lambda: pytest.fail(f"{mode} rebuilt B0 runtime"),
    )
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
    assert calls == ["uninstall"]


def test_lifespan_active_mode_uninstalls_claim_gate_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    calls: list[str] = []
    monkeypatch.setattr(server, "B0_MODE", "canary")
    monkeypatch.setattr(server, "B0_CANARY_SCOPES", frozenset({("team", "t1")}))
    monkeypatch.setattr(server, "H0_STATE_MODE", "off")
    monkeypatch.setattr(
        server.b0_wiring, "install_claim_gate",
        lambda **kw: calls.append("install"),
    )
    monkeypatch.setattr(
        server.b0_wiring, "uninstall_claim_gate",
        lambda: calls.append("uninstall"),
    )
    monkeypatch.setattr(
        server, "_b0_rebuild_on_start", lambda: calls.append("rebuild"),
    )
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
    assert calls == ["install", "rebuild", "uninstall"]


def test_rebuild_preserves_scope_degraded_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    class Coordinator:
        def rebuild(self) -> None:
            return None

        def state(self) -> dict[str, object]:
            return {
                "scopes": {"scope": {"degraded": "credential_unavailable"}},
                "last_reasons": {"credential_unavailable": 1},
            }

    monkeypatch.setattr(server, "B0_MODE", "on")
    monkeypatch.setattr(server, "_b0_get_coordinator", lambda: Coordinator())
    monkeypatch.setattr(server, "_b0_runtime_state", {})

    server._b0_rebuild_on_start()

    assert server._b0_runtime_state["available"] is False
    assert server._b0_runtime_state["degraded"] is True
    assert server._b0_runtime_state["reason"] == "credential_unavailable"


def test_coordinator_filters_bindings_and_control_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"issuer": ISSUER, "scope_kind": "team", "scope_id": "allowed"},
        {"issuer": ISSUER, "scope_kind": "team", "scope_id": "blocked"},
    ]
    monkeypatch.setattr(leader_binding, "list_bindings", lambda **kw: list(rows))
    coord = b0_wiring.B0Coordinator(
        _RecordingAdapter(), ISSUER,
        scope_filter=lambda kind, scope_id: (kind, scope_id) == ("team", "allowed"),
    )
    assert coord.active_bindings() == [rows[0]]

    events = [
        {**rows[0], "event_id": "e1", "event_type": "binding_changed",
         "binding_version": 1, "payload_json": "{}", "created_ts": 1},
        {**rows[1], "event_id": "e2", "event_type": "binding_changed",
         "binding_version": 1, "payload_json": "{}", "created_ts": 1},
    ]
    monkeypatch.setattr(
        leader_binding, "undelivered_control_events", lambda issuer, limit=100: events,
    )
    monkeypatch.setattr(leader_binding, "mark_event_fanned_out", lambda *a: True)
    coord.core.set_active_binding(
        b0_wiring.scope_key(ISSUER, "team", "allowed"), 1,
    )
    coord.set_target_status(
        b0_wiring.scope_key(ISSUER, "team", "allowed"), "ready",
    )
    transported: list[str] = []
    coord.fanout_control_events(
        transport=lambda event: transported.append(event["event_id"]) or True,
    )
    assert transported == ["e1"]


def _control_message(sender: str, meta: dict[str, object]) -> dict[str, object]:
    return {"id": 1, "from": sender, "subject": "stop"}


def test_control_claim_gate_binds_sender_scope_and_version(
    binding_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader_binding.bind_leader(
        ISSUER, "user", "default", mail_name="leader-user",
        session="s1", pane_id="p1", registry_selector="x/a.json",
        expected_version=0,
    )
    leader_binding.bind_leader(
        ISSUER, "team", "t1", mail_name="leader-team",
        session="s2", pane_id="p2", registry_selector="x/b.json",
        expected_version=0,
    )
    gate = b0_wiring.make_control_claim_gate(
        ISSUER, scope_filter=lambda kind, scope_id: True,
    )
    exact = {
        "intent": "stop", "binding_issuer": ISSUER,
        "binding_scope_kind": "team", "binding_scope_id": "t1",
        "binding_version": 1,
    }
    assert gate("p", "r", _control_message("leader-team", exact), exact) == (True, None)
    assert gate("p", "r", _control_message("leader-user", exact), exact) == (
        False, b0_wiring.REASON_STALE_BINDING_VERSION,
    )


def test_control_claim_gate_fails_closed_when_scoped_store_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = b0_wiring.make_control_claim_gate(
        ISSUER, scope_filter=lambda kind, scope_id: True,
    )
    monkeypatch.setattr(
        leader_binding, "get_active_binding",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    meta = {
        "intent": "redirect", "binding_issuer": ISSUER,
        "binding_scope_kind": "team", "binding_scope_id": "t1",
        "binding_version": 1,
    }
    assert gate("p", "r", _control_message("leader", meta), meta) == (
        False, b0_wiring.REASON_STALE_BINDING_VERSION,
    )


def test_health_exposes_b0_mode_without_initializing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    monkeypatch.setattr(server, "B0_MODE", "off")
    monkeypatch.setattr(server, "B0_CANARY_SCOPES", frozenset())
    monkeypatch.setattr(server, "_b0_coordinator", None)
    monkeypatch.setattr(
        server, "_agent_mail_status",
        lambda: {"available": True, "write_available": True},
    )
    monkeypatch.setattr(server.herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        server.web_push, "public_config", lambda: {"available": True},
    )
    body = server.health()
    assert body["b0_mode"] == "off"
    assert body["b0_canary_scopes"] == []
    assert body["b0"]["active"] is False
    assert server._b0_coordinator is None
