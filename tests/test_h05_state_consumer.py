"""H0.5:稳态消费者切换到 H0.4 socket 状态客户端缓存的契约测试。

复审修订(#1683 四项 blocking)后覆盖:lifespan 启停、缓存读取、显式
degraded/unavailable、discovery fail-closed(含 list_sessions 真实吞错
路径与 is_available=False)、per-session 无空窗增删/换 socket、稳态零
fork(轮询 + team router 10 pending)、SSE sig 纳入 degraded/state_status、
OpenCode 颜色字节注入清债。
"""
import asyncio
import threading
import time

import pytest

import herdr_client
import herdr_state
import server
import team_inbox_router

_REAL_STATE_CLIENT = herdr_state.HerdrStateClient


def _tt_binding(project_slug="acme", session="s1", pane_id="p1"):
    return {
        "hub": "http://hub:8765", "human_id": 7, "project_slug": project_slug,
        "session": session, "session_generation": "g1", "session_dir": "/tmp/s1",
        "lead": {"pane_id": pane_id, "agent": "codex", "mail_name": "codex-main"},
        "agent_id": 3, "updated_ts": 1.0,
    }


def _tt_item(item_id=101, project_slug="acme"):
    return {
        "id": item_id, "message_id": 55, "project_slug": project_slug,
        "subject": "主题", "body_md": "正文", "importance": "normal",
        "kind": "mention", "sender_name": "AgentA", "sender_kind": "agent",
        "read_ts": None, "created_ts": "2026-08-06 10:00:00",
    }


def _tt_write_bindings(path, rows):
    import json as _json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"version": 1, "bindings": rows}), encoding="utf-8")


def _pane(name, pane_id="w1:p1"):
    return {
        "pane_id": pane_id,
        "session": name,
        "agent": "codex",
        "agent_status": "idle",
        "cwd": "/tmp/demo",
        "revision": 1,
    }


class FakeStateClient:
    """模拟 HerdrStateClient 最小接口;实例级 ready 控制 bootstrap/生命周期。"""

    instances: list["FakeStateClient"] = []
    ready_next = True  # 下一个构造实例的 ready 初值

    def __init__(self, sessions):
        self.sessions = dict(sessions)
        self.ready = {name: FakeStateClient.ready_next for name in sessions}
        self.started = False
        self.stopped = False
        FakeStateClient.instances.append(self)

    def start(self):
        self.started = True

    def stop(self, join_timeout=5.0):
        self.stopped = True
        return True

    def snapshot_cached(self):
        sessions = []
        for name in self.sessions:
            panes = [_pane(name)] if self.ready.get(name) else []
            sessions.append({
                "session": name, "status": "running", "panes": panes,
                "agents": [], "focused_pane_id": None, "layouts": [],
            })
        flat = [p for s in sessions for p in s["panes"]]
        return {
            "available": any(self.ready.get(n) for n in self.sessions),
            "sessions": sessions,
            "panes": flat,
            "agents": [],
            "total_panes": len(flat),
            "agent_panes": len(flat),
        }

    def state(self):
        return {
            "running": not self.stopped,
            "sessions": {
                name: {"state": "subscribed" if self.ready.get(name) else "connecting"}
                for name in self.sessions
            },
        }


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    FakeStateClient.instances = []
    FakeStateClient.ready_next = True
    monkeypatch.setattr(server, "_state_clients", {})
    monkeypatch.setattr(server, "_state_sessions_meta", {})
    monkeypatch.setattr(server, "_state_discovery_ok", False)
    monkeypatch.setattr(server, "_state_discovery_reason", "test reset")
    monkeypatch.setattr(server, "STATE_SWAP_READY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(
        server.herdr_state, "HerdrStateClient", FakeStateClient
    )
    yield
    monkeypatch.setattr(server, "_state_clients", {})
    monkeypatch.setattr(server, "_state_sessions_meta", {})


def _install(monkeypatch, names=("demo",), discovery_ok=True):
    clients = {}
    for name in names:
        client = FakeStateClient({name: f"/tmp/{name}.sock"})
        client.start()
        clients[name] = client
    monkeypatch.setattr(server, "_state_clients", clients)
    monkeypatch.setattr(server, "_state_sessions_meta", {
        name: {"socket": f"/tmp/{name}.sock", "directory": f"/home/u/{name}"}
        for name in names
    })
    monkeypatch.setattr(server, "_state_discovery_ok", discovery_ok)
    monkeypatch.setattr(server, "_state_discovery_reason", "")
    return dict(clients)  # 副本:换入新客户端后旧引用仍有效


def _fake_discovery(monkeypatch, sessions):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    # 真实吞错路径测试会残留 threading.local 失败标志,发现桩前必须清除
    herdr_client._LIST_SESSIONS_FAILED.value = False
    monkeypatch.setattr(
        herdr_client,
        "list_sessions",
        lambda: [
            {
                "name": name,
                "status": "running",
                "directory": f"/home/u/{name}",
                "socket": sock,
            }
            for name, sock in sessions.items()
        ],
    )


def _no_cli_fork(monkeypatch):
    monkeypatch.setattr(
        herdr_client,
        "snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("禁止 fork snapshot CLI")),
    )


# ── 缓存读取与显式降级 ─────────────────────────────────────────


def test_snapshot_from_cache_merges_directory(monkeypatch):
    _no_cli_fork(monkeypatch)
    _install(monkeypatch)
    snap = server._state_client_snapshot()
    assert snap["available"] is True
    assert snap["degraded"] is False
    assert snap["sessions"][0]["directory"] == "/home/u/demo"
    assert snap["sessions"][0]["state_status"] == "subscribed"
    assert snap["panes"][0]["pane_id"] == "w1:p1"


def test_snapshot_no_sessions_is_legit_empty(monkeypatch):
    monkeypatch.setattr(server, "_state_discovery_ok", True)
    snap = server._state_client_snapshot()
    assert snap["available"] is True
    assert snap["degraded"] is False
    assert snap["sessions"] == []


def test_snapshot_unavailable_when_client_not_running(monkeypatch):
    _no_cli_fork(monkeypatch)
    snap = server._state_client_snapshot()
    assert snap["available"] is False
    assert snap["degraded"] is True
    assert snap["reason"]


def test_snapshot_degraded_on_reconnect_keeps_old_cache(monkeypatch):
    clients = _install(monkeypatch)
    clients["demo"].ready["demo"] = True
    clients["demo"].state = lambda: {  # 生命周期掉线但缓存仍在
        "running": True, "sessions": {"demo": {"state": "reconnecting"}},
    }
    snap = server._state_client_snapshot()
    assert snap["degraded"] is True
    assert snap["sessions"][0]["state_status"] == "reconnecting"
    assert snap["panes"][0]["pane_id"] == "w1:p1"


def test_snapshot_degraded_when_discovery_failing(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(server, "_state_discovery_ok", False)
    monkeypatch.setattr(server, "_state_discovery_reason", "session discovery unavailable")
    snap = server._state_client_snapshot()
    assert snap["degraded"] is True
    assert snap["reason"] == "session discovery unavailable"


# ── R1:discovery fail-closed ──────────────────────────────────


def test_discovery_unavailable_keeps_old_client_and_cache(monkeypatch):
    """is_available=False 不是成功空发现:保留旧 client/meta/cache,显式 degraded。"""
    clients = _install(monkeypatch)
    monkeypatch.setattr(herdr_client, "is_available", lambda: False)
    server._reconcile_state_client()
    assert server._state_discovery_ok is False
    assert server._state_clients["demo"] is clients["demo"]
    assert clients["demo"].stopped is False
    assert server._state_sessions_meta.get("demo")
    snap = server._state_client_snapshot()
    assert snap["degraded"] is True
    assert snap["panes"][0]["pane_id"] == "w1:p1"  # 旧缓存仍在


def test_discovery_double_failure_real_swallow_path(monkeypatch):
    """走 list_sessions 真实吞错路径(JSON+表格双命令失败)而非 mock 抛异常。"""
    clients = _install(monkeypatch)
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)

    def _boom(*args, **kwargs):
        raise RuntimeError("herdr cli down")

    monkeypatch.setattr(herdr_client, "_run", _boom)
    # 确认真实 list_sessions 吞错返回 [] 且置失败标志
    assert herdr_client.list_sessions() == []
    assert getattr(herdr_client._LIST_SESSIONS_FAILED, "value", False) is True
    server._reconcile_state_client()
    assert server._state_discovery_ok is False
    assert server._state_clients["demo"] is clients["demo"]
    assert clients["demo"].stopped is False
    snap = server._state_client_snapshot()
    assert snap["degraded"] is True
    assert snap["panes"]  # 旧缓存保留


def test_discovery_empty_success_stops_clients(monkeypatch):
    """成功发现零 session(无失败标志)才允许清空。"""
    clients = _install(monkeypatch)
    _fake_discovery(monkeypatch, {})
    server._reconcile_state_client()
    assert server._state_discovery_ok is True
    assert clients["demo"].stopped is True
    snap = server._state_client_snapshot()
    assert snap["available"] is True
    assert snap["degraded"] is False
    assert snap["sessions"] == []


# ── R3:per-session 无空窗 ─────────────────────────────────────


def test_add_session_keeps_existing_client_running(monkeypatch):
    clients = _install(monkeypatch, names=("demo",))
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo.sock", "new": "/tmp/new.sock"})
    server._reconcile_state_client()
    assert clients["demo"].stopped is False  # 旧 session 客户端不动
    new_client = server._state_clients["new"]
    assert new_client is not clients["demo"]
    assert new_client.started and not new_client.stopped
    snap = server._state_client_snapshot()
    assert {s["session"] for s in snap["sessions"]} == {"demo", "new"}
    assert snap["degraded"] is False


def test_add_bad_new_session_does_not_hide_old_sessions(monkeypatch):
    """新增坏 socket session:自身 degraded,旧健康 session 缓存持续可读。"""
    _install(monkeypatch, names=("demo",))
    FakeStateClient.ready_next = False  # 新客户端永不 bootstrap
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo.sock", "bad": "/tmp/bad.sock"})
    server._reconcile_state_client()
    snap = server._state_client_snapshot()
    by_name = {s["session"]: s for s in snap["sessions"]}
    assert by_name["demo"]["state_status"] == "subscribed"
    assert by_name["demo"]["panes"][0]["pane_id"] == "w1:p1"
    assert by_name["bad"]["state_status"] == "connecting"
    assert snap["degraded"] is True
    assert snap["available"] is True  # 旧 session 有真实缓存
    FakeStateClient.ready_next = True


def test_remove_session_stops_only_that_client(monkeypatch):
    clients = _install(monkeypatch, names=("demo", "extra"))
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo.sock"})
    server._reconcile_state_client()
    assert clients["extra"].stopped is True
    assert clients["demo"].stopped is False
    snap = server._state_client_snapshot()
    assert {s["session"] for s in snap["sessions"]} == {"demo"}
    assert snap["degraded"] is False


def test_socket_change_swaps_after_ready(monkeypatch):
    clients = _install(monkeypatch, names=("demo",))
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo-new.sock"})
    server._reconcile_state_client()
    new_client = server._state_clients["demo"]
    assert new_client is not clients["demo"]
    assert clients["demo"].stopped is True  # 就绪后才停旧
    assert server._state_sessions_meta["demo"]["socket"] == "/tmp/demo-new.sock"
    snap = server._state_client_snapshot()
    assert snap["degraded"] is False


def test_socket_change_keeps_old_when_new_never_ready(monkeypatch):
    clients = _install(monkeypatch, names=("demo",))
    FakeStateClient.ready_next = False
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo-dead.sock"})
    server._reconcile_state_client()
    # 弃新留旧:旧客户端继续服务,meta 不推进(下轮发现重试)
    assert server._state_clients["demo"] is clients["demo"]
    assert clients["demo"].stopped is False
    assert server._state_sessions_meta["demo"]["socket"] == "/tmp/demo.sock"
    assert FakeStateClient.instances[-1].stopped is True  # 坏的新客户端已停
    snap = server._state_client_snapshot()
    assert snap["panes"][0]["pane_id"] == "w1:p1"  # 旧缓存持续可读
    # 下一轮发现(reconcile)会再次尝试切换
    FakeStateClient.ready_next = True
    server._reconcile_state_client()
    assert server._state_clients["demo"] is not clients["demo"]
    assert server._state_sessions_meta["demo"]["socket"] == "/tmp/demo-dead.sock"


def test_real_client_stop_leaves_no_thread():
    """真实 HerdrStateClient(坏 socket):stop 确定性退出,无线程残留。"""
    client = _REAL_STATE_CLIENT(
        {"h05ghost": "/nonexistent/h05-ghost.sock"},
        reconnect_base_delay=0.05,
        reconnect_max_delay=0.1,
        health_check_interval=None,
    )
    client.start()
    assert client.stop(join_timeout=5.0) is True
    assert not [
        t for t in threading.enumerate()
        if t.name.startswith("cockpit-state-h05ghost") and t.is_alive()
    ]


def test_stop_state_client_stops_all_and_is_idempotent(monkeypatch):
    clients = _install(monkeypatch, names=("a", "b"))
    server._stop_state_client()
    assert clients["a"].stopped and clients["b"].stopped
    assert server._state_clients == {}
    server._stop_state_client()


# ── R2:稳态零 fork(轮询 + team router) ────────────────────────


def _stub_poll_side_effects(monkeypatch):
    monkeypatch.setattr(server, "_reconcile_state_client", lambda: None)
    monkeypatch.setattr(server, "_expire_zoom_leases", lambda: None)
    monkeypatch.setattr(server.coordination, "maintain_live_claims", lambda snap: None)
    monkeypatch.setattr(server, "_build_attention", lambda snap: {
        "items": [], "mail_unread": 0, "capabilities": {}, "sessions": [],
    })
    monkeypatch.setattr(server.web_push, "notify", lambda items: None)
    monkeypatch.setattr(server, "_record_poll_metrics", lambda *a: None)


class _StopLoop(Exception):
    pass


def test_poll_loop_reads_cache_without_fork(monkeypatch):
    _no_cli_fork(monkeypatch)
    _install(monkeypatch)
    _stub_poll_side_effects(monkeypatch)
    monkeypatch.setattr(
        server, "_poll_delay", lambda count: (_ for _ in ()).throw(_StopLoop())
    )
    monkeypatch.setattr(server, "_live_state", {
        "revision": 0, "unread": None, "snapshot": None, "attention": None,
    })
    with pytest.raises(_StopLoop):
        asyncio.run(server._poll_live_state())
    state = server._live_state
    assert state["revision"] == 1
    assert state["snapshot"]["sessions"][0]["session"] == "demo"
    assert state["snapshot"]["degraded"] is False


def test_poll_loop_revision_follows_degraded_both_directions(monkeypatch):
    """R4:仅 degraded/state_status 变化与恢复都必须 revision+1。"""
    _no_cli_fork(monkeypatch)
    clients = _install(monkeypatch)
    _stub_poll_side_effects(monkeypatch)
    rounds = []

    class _Flip:
        """第 2 轮掉线、第 3 轮恢复、第 3 轮末停止。"""

        def __init__(self):
            self.n = 0

        def delay(self, count):
            self.n += 1
            if self.n == 1:
                clients["demo"].state = lambda: {
                    "running": True,
                    "sessions": {"demo": {"state": "reconnecting"}},
                }
            elif self.n == 2:
                clients["demo"].state = lambda: {
                    "running": True,
                    "sessions": {"demo": {"state": "subscribed"}},
                }
            elif self.n >= 3:
                raise _StopLoop()
            return 0.0

    flip = _Flip()
    monkeypatch.setattr(server, "_poll_delay", flip.delay)
    real_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(server, "_live_state", {
        "revision": 0, "unread": None, "snapshot": None, "attention": None,
    })
    orig_snapshot = server._state_client_snapshot

    def _tracking_snapshot():
        snap = orig_snapshot()
        rounds.append(snap.get("degraded"))
        return snap

    monkeypatch.setattr(server, "_state_client_snapshot", _tracking_snapshot)
    with pytest.raises(_StopLoop):
        asyncio.run(server._poll_live_state())
    assert rounds[:3] == [False, True, False]
    assert server._live_state["revision"] == 3  # 每轮状态翻转都 +1


def test_team_router_uses_cache_provider_no_fork(monkeypatch, tmp_path):
    """R2:team router 走共享缓存;10 条 pending 只读回归,一轮零 fork。"""
    _no_cli_fork(monkeypatch)
    assert not hasattr(team_inbox_router, "snapshot")  # CLI 兼容残留已删
    monkeypatch.setattr(
        team_inbox_router.team_sessions, "STATE_PATH", tmp_path / "ts.json"
    )
    monkeypatch.setattr(
        team_inbox_router, "ROUTE_STATE", tmp_path / "route.json"
    )
    calls = []

    def _provider():
        calls.append(1)
        return {"available": True, "sessions": [], "panes": []}  # lead 离线

    team_inbox_router.set_snapshot_provider(_provider)
    try:
        _tt_write_bindings(
            tmp_path / "ts.json",
            [_tt_binding(project_slug="acme", session="s1", pane_id="p1")],
        )
        items = [_tt_item(item_id=1000 + i) for i in range(10)]
        result = team_inbox_router.route_inbox(
            "Bearer x",
            hub="http://hub:8765",
            human_id=7,
            fetch_inbox=lambda auth: {"items": items},
        )
        assert result["matched"] == 10
        assert result["pending"] == 10
        assert result["delivered"] == 0
        assert len(calls) == 10  # 全部读缓存,零 CLI fork
    finally:
        team_inbox_router.set_snapshot_provider(server._state_client_snapshot)


def test_team_router_without_provider_is_safe_offline(tmp_path, monkeypatch):
    """未注入 provider 时安全默认:lead 离线、消息留 pending、不投递。"""
    monkeypatch.setattr(
        team_inbox_router.team_sessions, "STATE_PATH", tmp_path / "ts.json"
    )
    monkeypatch.setattr(team_inbox_router, "ROUTE_STATE", tmp_path / "route.json")
    team_inbox_router.set_snapshot_provider(None)
    try:
        _tt_write_bindings(
            tmp_path / "ts.json",
            [_tt_binding(project_slug="acme", session="s1", pane_id="p1")],
        )
        result = team_inbox_router.route_inbox(
            "Bearer x",
            hub="http://hub:8765",
            human_id=7,
            fetch_inbox=lambda auth: {"items": [_tt_item()]},
        )
        assert result["pending"] == 1
        assert result["delivered"] == 0
    finally:
        team_inbox_router.set_snapshot_provider(server._state_client_snapshot)


def test_pane_identity_reads_cache(monkeypatch):
    """R2:pane identity GET 走缓存;CLI snapshot 被禁。"""
    _no_cli_fork(monkeypatch)
    monkeypatch.setattr(server, "_state_client_snapshot", lambda: {
        "available": True,
        "sessions": [],
        "panes": [{
            "session": "demo", "pane_id": "w1:p1",
            "cwd": "/tmp/demo", "agent": "codex",
        }],
    })
    monkeypatch.setattr(server.db, "status", lambda: {"available": True, "reason": None})
    monkeypatch.setattr(server, "_mail_project_state", lambda s: {
        "bound": False, "project": None,
    })
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    response = TestClient(server.app).get(
        "/api/herdr/pane/demo/w1:p1/identity",
        headers={"authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    assert response.json()["needs_project"] is True


def test_mutation_adjacent_snapshot_stays_cli():
    """R2:仅 mutation 前后即时验证保留 CLI snapshot,逐处源码证明。"""
    from pathlib import Path

    source = Path(server.__file__).read_text(encoding="utf-8")
    kept = source.count("herdr_client.snapshot()")
    assert kept == 3  # start_agent 同类型判定 + launch 建 session/清 pane 验证
    for marker in ("H0.5 保留 CLI",):
        assert source.count(marker) == 3


# ── lifespan 启停 ─────────────────────────────────────────────


def test_lifespan_starts_and_stops_state_client(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(
        server, "_reconcile_state_client", lambda: events.append("reconcile"),
    )
    monkeypatch.setattr(
        server, "_stop_state_client", lambda: events.append("stop")
    )

    async def _noop_loop():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(server, "_poll_live_state", _noop_loop)
    monkeypatch.setattr(server, "_poll_message_state", _noop_loop)
    monkeypatch.setattr(server, "_worktree_cleanup_loop", _noop_loop)
    monkeypatch.setattr(server, "_release_all_zoom_leases", lambda: None)

    from fastapi.testclient import TestClient

    with TestClient(server.app):
        pass
    assert events[0] == "reconcile"
    assert "stop" in events


# ── 主题清债 ───────────────────────────────────────────────────


def test_opencode_injection_removed_from_server_source():
    from pathlib import Path

    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "notify_opencode_color_scheme" not in source
    assert "_TERM_THEME_TASKS" not in source
