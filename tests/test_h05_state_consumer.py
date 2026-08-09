"""H0.5:稳态消费者切换到 H0.4 socket 状态客户端缓存的契约测试。

覆盖:lifespan 启停、缓存读取、显式 degraded/unavailable、动态 session
增删重建、稳态轮询不 fork herdr snapshot CLI、OpenCode 颜色字节注入清债。
"""
import asyncio
import threading

import pytest

import herdr_client
import server


class FakeStateClient:
    """模拟 HerdrStateClient 的最小接口:start/stop/snapshot_cached/state。"""

    instances: list["FakeStateClient"] = []

    def __init__(self, sessions, store=None, lifecycle=None):
        self.sessions = dict(sessions)
        self.started = False
        self.stopped = False
        self._store = store if store is not None else {
            "available": True,
            "sessions": [
                {
                    "session": name,
                    "status": "running",
                    "panes": [
                        {
                            "pane_id": "w1:p1",
                            "session": name,
                            "agent": "codex",
                            "agent_status": "idle",
                            "cwd": "/tmp/demo",
                            "revision": 1,
                        }
                    ],
                    "agents": [],
                    "focused_pane_id": None,
                    "layouts": [],
                }
                for name in sessions
            ],
            "panes": [],
            "agents": [],
            "total_panes": len(sessions),
            "agent_panes": len(sessions),
        }
        # 扁平化 panes,与 StateStore.snapshot_cached 形状一致
        self._store["panes"] = [
            pane for entry in self._store["sessions"] for pane in entry["panes"]
        ]
        self._lifecycle = lifecycle if lifecycle is not None else {
            name: {"state": "subscribed"} for name in sessions
        }
        FakeStateClient.instances.append(self)

    def start(self):
        self.started = True

    def stop(self, join_timeout=5.0):
        self.stopped = True
        return True

    def snapshot_cached(self):
        import copy

        return copy.deepcopy(self._store)

    def state(self):
        return {"running": not self.stopped, "sessions": dict(self._lifecycle)}


@pytest.fixture(autouse=True)
def _reset_state_client(monkeypatch):
    """每个测试前后隔离 server 的状态客户端全局量。"""
    FakeStateClient.instances = []
    monkeypatch.setattr(server, "_state_client", None)
    monkeypatch.setattr(server, "_state_sessions_meta", {})
    monkeypatch.setattr(server, "_state_discovery_ok", False)
    yield
    monkeypatch.setattr(server, "_state_client", None)
    monkeypatch.setattr(server, "_state_sessions_meta", {})
    monkeypatch.setattr(server, "_state_discovery_ok", False)


def _install_client(monkeypatch, sessions=("demo",), lifecycle=None):
    client = FakeStateClient(
        {name: f"/tmp/{name}.sock" for name in sessions}, lifecycle=lifecycle
    )
    monkeypatch.setattr(server, "_state_client", client)
    monkeypatch.setattr(
        server,
        "_state_sessions_meta",
        {
            name: {"socket": f"/tmp/{name}.sock", "directory": f"/home/u/{name}"}
            for name in sessions
        },
    )
    monkeypatch.setattr(server, "_state_discovery_ok", True)
    return client


# ── 缓存读取与显式降级 ─────────────────────────────────────────


def test_snapshot_from_cache_merges_directory(monkeypatch):
    """稳态读缓存:合并发现层 directory,subscribed 时不 degraded。"""
    monkeypatch.setattr(
        herdr_client,
        "snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("禁止 fork snapshot CLI")),
    )
    _install_client(monkeypatch)
    snap = server._state_client_snapshot()
    assert snap["available"] is True
    assert snap["degraded"] is False
    assert snap["sessions"][0]["directory"] == "/home/u/demo"
    assert snap["sessions"][0]["state_status"] == "subscribed"
    assert snap["panes"][0]["pane_id"] == "w1:p1"


def test_snapshot_no_sessions_is_legit_empty(monkeypatch):
    """发现正常且无 running session:available 空态,不是 unavailable。"""
    monkeypatch.setattr(server, "_state_discovery_ok", True)
    snap = server._state_client_snapshot()
    assert snap["available"] is True
    assert snap["degraded"] is False
    assert snap["sessions"] == []


def test_snapshot_unavailable_when_client_not_running(monkeypatch):
    """无可靠缓存:显式 unavailable + degraded + reason,不回退 CLI fork。"""
    monkeypatch.setattr(
        herdr_client,
        "snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("禁止 fork snapshot CLI")),
    )
    snap = server._state_client_snapshot()
    assert snap["available"] is False
    assert snap["degraded"] is True
    assert snap["reason"]


def test_snapshot_degraded_on_reconnect_keeps_old_cache(monkeypatch):
    """socket 中断(reconnecting):继续服务旧缓存并显式 degraded。"""
    _install_client(
        monkeypatch, lifecycle={"demo": {"state": "reconnecting", "reconnects": 2}}
    )
    snap = server._state_client_snapshot()
    assert snap["degraded"] is True
    assert snap["sessions"][0]["state_status"] == "reconnecting"
    # 旧缓存仍然可读
    assert snap["panes"][0]["pane_id"] == "w1:p1"


def test_snapshot_recovers_after_resync(monkeypatch):
    """恢复订阅(resync 完成)后 degraded 解除。"""
    client = _install_client(
        monkeypatch, lifecycle={"demo": {"state": "reconnecting"}}
    )
    assert server._state_client_snapshot()["degraded"] is True
    client._lifecycle["demo"]["state"] = "subscribed"
    assert server._state_client_snapshot()["degraded"] is False


def test_snapshot_degraded_when_session_not_yet_cached(monkeypatch):
    """已发现但尚未 bootstrap 的 session 视为 degraded。"""
    _install_client(monkeypatch, sessions=("demo",))
    meta = server._state_sessions_meta
    meta["new-session"] = {"socket": "/tmp/new.sock", "directory": "/d"}
    monkeypatch.setattr(server, "_state_sessions_meta", meta)
    assert server._state_client_snapshot()["degraded"] is True


def test_snapshot_degraded_when_discovery_failing(monkeypatch):
    _install_client(monkeypatch)
    monkeypatch.setattr(server, "_state_discovery_ok", False)
    assert server._state_client_snapshot()["degraded"] is True


# ── 动态 session 增删重建 ──────────────────────────────────────


def _fake_discovery(monkeypatch, sessions):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client,
        "list_sessions",
        lambda: [
            {
                "name": name,
                "status": "running",
                "directory": f"/home/u/{name}",
                "socket": f"/tmp/{name}.sock",
            }
            for name in sessions
        ],
    )
    monkeypatch.setattr(server.herdr_state, "HerdrStateClient", FakeStateClient)


def test_reconcile_starts_client_on_first_discovery(monkeypatch):
    _fake_discovery(monkeypatch, ["demo"])
    server._reconcile_state_client()
    assert len(FakeStateClient.instances) == 1
    client = FakeStateClient.instances[0]
    assert client.started and not client.stopped
    assert client.sessions == {"demo": "/tmp/demo.sock"}
    assert server._state_discovery_ok is True


def test_reconcile_recreates_on_session_add_and_remove(monkeypatch):
    _fake_discovery(monkeypatch, ["a"])
    server._reconcile_state_client()
    _fake_discovery(monkeypatch, ["a", "b"])
    server._reconcile_state_client()
    assert len(FakeStateClient.instances) == 2
    first, second = FakeStateClient.instances
    assert first.stopped is True  # 旧客户端被停止
    assert second.started and not second.stopped
    assert second.sessions == {"a": "/tmp/a.sock", "b": "/tmp/b.sock"}
    # 删除 session 同样触发重建
    _fake_discovery(monkeypatch, ["b"])
    server._reconcile_state_client()
    assert len(FakeStateClient.instances) == 3
    assert FakeStateClient.instances[2].sessions == {"b": "/tmp/b.sock"}


def test_reconcile_noop_when_sessions_unchanged(monkeypatch):
    _fake_discovery(monkeypatch, ["a"])
    server._reconcile_state_client()
    server._reconcile_state_client()
    assert len(FakeStateClient.instances) == 1


def test_reconcile_recreates_on_socket_path_change(monkeypatch):
    """session restart 后 socket 路径变化也触发重建。"""
    _fake_discovery(monkeypatch, ["a"])
    server._reconcile_state_client()
    monkeypatch.setattr(
        herdr_client,
        "list_sessions",
        lambda: [
            {
                "name": "a",
                "status": "running",
                "directory": "/home/u/a",
                "socket": "/tmp/a-new.sock",
            }
        ],
    )
    server._reconcile_state_client()
    assert len(FakeStateClient.instances) == 2
    assert FakeStateClient.instances[1].sessions == {"a": "/tmp/a-new.sock"}


def test_reconcile_discovery_failure_marks_not_ok(monkeypatch):
    def _boom():
        raise RuntimeError("herdr cli failed")

    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "list_sessions", _boom)
    server._reconcile_state_client()
    assert server._state_discovery_ok is False


def test_stop_state_client_is_idempotent(monkeypatch):
    client = _install_client(monkeypatch)
    server._stop_state_client()
    assert client.stopped is True
    assert server._state_client is None
    server._stop_state_client()  # 第二次安全


# ── 稳态轮询不 fork CLI ───────────────────────────────────────


def test_poll_loop_reads_cache_without_fork(monkeypatch):
    """_poll_live_state 单轮:看板来自缓存,全程不调用 herdr_client.snapshot。"""
    _install_client(monkeypatch)
    monkeypatch.setattr(
        herdr_client,
        "snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("禁止 fork snapshot CLI")),
    )
    monkeypatch.setattr(server, "_reconcile_state_client", lambda: None)
    monkeypatch.setattr(server, "_expire_zoom_leases", lambda: None)
    monkeypatch.setattr(
        server.coordination, "maintain_live_claims", lambda snap: None
    )
    monkeypatch.setattr(
        server,
        "_build_attention",
        lambda snap: {
            "items": [],
            "mail_unread": 0,
            "capabilities": {},
            "sessions": [],
        },
    )
    monkeypatch.setattr(server.web_push, "notify", lambda items: None)
    monkeypatch.setattr(server, "_record_poll_metrics", lambda *a: None)

    class _StopLoop(Exception):
        pass

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


# ── lifespan 启停 ─────────────────────────────────────────────


def test_lifespan_starts_and_stops_state_client(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(
        server,
        "_reconcile_state_client",
        lambda: events.append("reconcile"),
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
    """server.py 不再引用 OpenCode pane 注入与其任务登记。"""
    from pathlib import Path

    source = (Path(server.__file__)).read_text(encoding="utf-8")
    assert "notify_opencode_color_scheme" not in source
    assert "_TERM_THEME_TASKS" not in source
