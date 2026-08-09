"""test_herdr_state.py — H0.4 socket 状态客户端测试。

用 tmp_path 下的真实 Unix socket fake server（NDJSON 行协议）驱动
herdr_state.HerdrStateClient，遵循真实 herdr 0.8 协议契约：
- 每个请求必须携带 params；server 每连接只读一条请求（普通请求响应后关闭，
  events.subscribe 为长连接）；订阅仅 24 个无参类型。
覆盖 bootstrap、增量更新、线程安全缓存、退避重连+resync、shutdown 与
故障注入。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

import herdr_state
from herdr_client import HERDR_MIN_PROTOCOL


# ---------------------------------------------------------------------------
# Fake herdr server（NDJSON over AF_UNIX，一连接一请求）
# ---------------------------------------------------------------------------

def _line(conn: socket.socket, value: Any) -> None:
    conn.sendall((json.dumps(value) + "\n").encode("utf-8"))


def _read_request(conn: socket.socket) -> dict[str, Any] | None:
    """读一行请求；EOF 返回 None。"""
    buf = bytearray()
    while True:
        chunk = conn.recv(1)
        if not chunk:
            return None
        buf += chunk
        if chunk == b"\n":
            break
    try:
        return json.loads(bytes(buf).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _pong(request_id: str, protocol: int = HERDR_MIN_PROTOCOL) -> dict[str, Any]:
    return {
        "id": request_id,
        "result": {"type": "pong", "version": "0.8.0", "protocol": protocol},
    }


def _snapshot_response(request_id: str, snap: dict[str, Any]) -> dict[str, Any]:
    return {"id": request_id, "result": {"type": "session_snapshot", "snapshot": snap}}


def _started_response(request_id: str) -> dict[str, Any]:
    return {"id": request_id, "result": {"type": "subscription_started"}}


def _error_response(request_id: str, code: str, message: str = "boom") -> dict[str, Any]:
    return {"id": request_id, "error": {"code": code, "message": message}}


def make_snapshot(
    *panes: dict[str, Any], protocol: int = HERDR_MIN_PROTOCOL,
) -> dict[str, Any]:
    return {
        "version": "0.8.0",
        "protocol": protocol,
        "workspaces": [],
        "tabs": [],
        "panes": list(panes),
        "layouts": [],
        "agents": [],
        "focused_pane_id": None,
        "focused_tab_id": None,
        "focused_workspace_id": None,
    }


def make_pane(
    pane_id: str, *, agent: str | None = "codex",
    agent_status: str = "idle", revision: int = 1,
    workspace_id: str = "w1", tab_id: str = "t1", cwd: str = "/tmp/proj",
    focused: bool = False,
) -> dict[str, Any]:
    return {
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "agent": agent,
        "display_agent": agent,
        "agent_status": agent_status,
        "revision": revision,
        "state_change_seq": 0,
        "cwd": cwd,
        "foreground_cwd": cwd,
        "title": None,
        "terminal_title": None,
        "terminal_title_stripped": None,
        "focused": focused,
        "interactive_ready": True,
        "launch_pending": False,
        "state_labels": {},
        "tokens": {},
        "terminal_id": f"term-{pane_id}",
    }


def _hang(conn: socket.socket) -> None:
    """长连接挂起直到对端关闭。"""
    while True:
        if conn.recv(1) == b"":
            return


def _serve_request(conn: socket.socket, server: "FakeHerdrServer") -> None:
    """一连接一请求：普通请求响应后返回；subscribe 转入长连接。"""
    req = server.read(conn)
    if req is None:
        return
    method = req.get("method")
    if method == "ping":
        server.send(conn, _pong(str(req.get("id"))))
    elif method == "session.snapshot":
        server.send(conn, _snapshot_response(str(req.get("id")), server.snapshot))
    elif method == "events.subscribe":
        server.send(conn, _started_response(str(req.get("id"))))
        _hang(conn)
    else:
        server.send(conn, _error_response(str(req.get("id")), "unknown_method"))


class FakeHerdrServer:
    """单 listener 循环 accept；每个连接由 handler(conn, ctx) 驱动。

    默认 handler 遵循真实契约：一连接一请求，普通请求响应后关闭，订阅长连接。
    ctx.send(obj) 写一行 NDJSON；ctx.read() 读一行请求（EOF→None）。
    """

    def __init__(
        self, tmp_path: Path, *, name: str = "work",
        handler: Callable[[socket.socket, "FakeHerdrServer"], None] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        self.path = tmp_path / f"{name}.sock"
        self.snapshot = snapshot if snapshot is not None else make_snapshot()
        self.handler = handler
        self.accepted = 0
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- ctx helpers ------------------------------------------------------
    def send(self, conn: socket.socket, value: Any) -> None:
        _line(conn, value)

    def read(self, conn: socket.socket) -> dict[str, Any] | None:
        return _read_request(conn)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> "FakeHerdrServer":
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(4)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()  # type: ignore[union-attr]
            except OSError:
                continue
            self.accepted += 1
            handler = self.handler or _serve_request
            # 每连接独立线程：订阅长连接不能阻塞后续连接的 accept
            threading.Thread(
                target=self._handle_conn, args=(conn, handler), daemon=True,
            ).start()

    def _handle_conn(self, conn: socket.socket, handler: Callable[..., Any]) -> None:
        try:
            handler(conn, self)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待条件超时")


def _start_client(
    sessions: dict[str, str], *, connect_timeout: float = 0.5,
    request_timeout: float = 0.5, health_check_interval: float | None = 0.3,
    reconnect_base_delay: float = 0.05, reconnect_max_delay: float = 0.2,
    snapshot_timeout: float = 0.5, resync_min_interval: float = 0.1,
    **kwargs: Any,
) -> herdr_state.HerdrStateClient:
    client = herdr_state.HerdrStateClient(
        sessions,
        connect_timeout=connect_timeout,
        request_timeout=request_timeout,
        health_check_interval=health_check_interval,
        reconnect_base_delay=reconnect_base_delay,
        reconnect_max_delay=reconnect_max_delay,
        snapshot_timeout=snapshot_timeout,
        resync_min_interval=resync_min_interval,
        **kwargs,
    )
    client.start()
    return client


# ---------------------------------------------------------------------------
# wire 层
# ---------------------------------------------------------------------------

class TestHerdrSocket:
    def test_request_and_read_line_roundtrip(self, tmp_path: Path) -> None:
        server = FakeHerdrServer(tmp_path).start()
        sock = herdr_state.HerdrSocket(str(server.path), connect_timeout=0.5)
        sock.connect()
        result = sock.request("ping", {}, timeout=0.5, expect_type="pong")
        assert result["type"] == "pong"
        assert result["protocol"] == HERDR_MIN_PROTOCOL
        sock.close()
        server.stop()

    def test_connect_refused_raises(self, tmp_path: Path) -> None:
        sock = herdr_state.HerdrSocket(str(tmp_path / "missing.sock"), connect_timeout=0.3)
        with pytest.raises(herdr_state.HerdrSocketError):
            sock.connect()

    def test_request_error_response_raises(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            server.send(conn, _error_response(str(req["id"]), "server_stopped"))
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        sock = herdr_state.HerdrSocket(str(server.path), connect_timeout=0.5)
        sock.connect()
        with pytest.raises(herdr_state.HerdrSocketError) as exc:
            sock.request("session.snapshot", {}, timeout=0.5)
        assert "server_stopped" in str(exc.value)
        sock.close()
        server.stop()

    def test_response_id_mismatch_raises(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            server.send(conn, {"id": "wrong", "result": {"type": "pong", "protocol": 19}})
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        sock = herdr_state.HerdrSocket(str(server.path), connect_timeout=0.5)
        sock.connect()
        with pytest.raises(herdr_state.HerdrSocketError, match="id 不匹配"):
            sock.request("ping", {}, timeout=0.5, expect_type="pong")
        sock.close()
        server.stop()

    def test_response_type_mismatch_raises(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            server.send(conn, {"id": str(req["id"]), "result": {"type": "not_pong"}})
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        sock = herdr_state.HerdrSocket(str(server.path), connect_timeout=0.5)
        sock.connect()
        with pytest.raises(herdr_state.HerdrSocketError, match="类型不匹配"):
            sock.request("ping", {}, timeout=0.5, expect_type="pong")
        sock.close()
        server.stop()

    def test_malformed_line_raises(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            conn.sendall(b"not-json\n")
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        sock = herdr_state.HerdrSocket(str(server.path), connect_timeout=0.5)
        sock.connect()
        with pytest.raises(herdr_state.HerdrSocketError):
            sock.request("ping", {}, timeout=0.5)
        sock.close()
        server.stop()

    def test_eof_raises(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            return  # 立即关闭
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        sock = herdr_state.HerdrSocket(str(server.path), connect_timeout=0.5)
        sock.connect()
        with pytest.raises(herdr_state.HerdrSocketError):
            sock.request("ping", {}, timeout=0.5)
        sock.close()
        server.stop()

    def test_read_line_idle_timeout_is_idle_error(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            server.send(conn, _started_response(str(req["id"])))
            _hang(conn)
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        sock = herdr_state.HerdrSocket(str(server.path), connect_timeout=0.5)
        sock.connect()
        sock.request("events.subscribe", {"subscriptions": []}, timeout=0.5)
        with pytest.raises(herdr_state.HerdrSocketIdleTimeout):
            sock.read_line(timeout=0.1)
        sock.close()
        server.stop()


# ---------------------------------------------------------------------------
# SessionState 纯逻辑
# ---------------------------------------------------------------------------

class TestSessionState:
    def _state(self, session: str = "work") -> herdr_state.SessionState:
        return herdr_state.SessionState(session)

    def test_apply_snapshot_builds_slim_panes(self) -> None:
        state = self._state()
        snap = make_snapshot(
            make_pane("p1", agent="codex", agent_status="working", revision=3)
        )
        state.apply_snapshot(snap)
        panes = state.panes()
        assert len(panes) == 1
        slim = panes[0]
        assert slim["pane_id"] == "p1"
        assert slim["session"] == "work"
        assert slim["agent"] == "codex"
        assert slim["agent_status"] == "working"
        assert slim["revision"] == 3
        assert slim["cwd_name"] == "proj"
        assert state.focused_pane_id is None

    def test_pane_updated_upserts_and_filters_stale_revision(self) -> None:
        state = self._state()
        state.apply_snapshot(make_snapshot(make_pane("p1", revision=5)))
        # 旧 revision 丢弃
        state.apply_event(
            {"event": "pane_updated", "data": {"type": "pane_updated", "pane": make_pane("p1", revision=4)}}
        )
        assert state.panes()[0]["revision"] == 5
        # 相等 revision 也按重复拒绝
        state.apply_event(
            {"event": "pane_updated", "data": {"type": "pane_updated", "pane": make_pane("p1", revision=5, agent_status="blocked")}}
        )
        assert state.panes()[0]["agent_status"] == "idle"
        # 新 revision 应用
        state.apply_event(
            {"event": "pane_updated", "data": {"type": "pane_updated", "pane": make_pane("p1", revision=6, agent_status="blocked")}}
        )
        assert state.panes()[0]["revision"] == 6
        assert state.panes()[0]["agent_status"] == "blocked"

    def test_pane_created_adds_and_closed_removes(self) -> None:
        state = self._state()
        state.apply_snapshot(make_snapshot())
        state.apply_event(
            {"event": "pane_created", "data": {"type": "pane_created", "pane": make_pane("p9", agent=None)}}
        )
        assert len(state.panes()) == 1
        assert state.panes()[0]["agent"] is None
        state.apply_event(
            {"event": "pane_closed", "data": {"type": "pane_closed", "pane_id": "p9", "workspace_id": "w1"}}
        )
        assert state.panes() == []

    def test_pane_exited_removes_and_detected_updates_agent(self) -> None:
        state = self._state()
        state.apply_snapshot(make_snapshot(make_pane("p1", agent=None)))
        state.apply_event(
            {"event": "pane_agent_detected", "data": {"type": "pane_agent_detected", "pane_id": "p1", "workspace_id": "w1", "agent": "kimi", "released": False}}
        )
        assert state.panes()[0]["agent"] == "kimi"
        state.apply_event(
            {"event": "pane_exited", "data": {"type": "pane_exited", "pane_id": "p1", "workspace_id": "w1"}}
        )
        assert state.panes() == []

    def test_agent_status_changed_applies_when_pane_known(self) -> None:
        state = self._state()
        state.apply_snapshot(make_snapshot(make_pane("p1", agent="codex", agent_status="idle")))
        state.apply_event(
            {"event": "pane_agent_status_changed", "data": {"type": "pane_agent_status_changed", "pane_id": "p1", "workspace_id": "w1", "agent_status": "working", "agent": "codex", "display_agent": "codex", "state_labels": {}, "title": None}}
        )
        assert state.panes()[0]["agent_status"] == "working"

    def test_unknown_pane_events_set_resync_pending(self) -> None:
        state = self._state()
        state.apply_snapshot(make_snapshot())
        applied = state.apply_event(
            {"event": "pane_agent_status_changed", "data": {"type": "pane_agent_status_changed", "pane_id": "ghost", "workspace_id": "w1", "agent_status": "working"}}
        )
        assert applied is False
        assert state.stale_events == 1
        assert state.resync_pending is True

    def test_unknown_pane_focus_does_not_set_ghost(self) -> None:
        state = self._state()
        state.apply_snapshot(make_snapshot(make_pane("p1", focused=False)))
        state.apply_event(
            {"event": "pane_focused", "data": {"type": "pane_focused", "pane_id": "ghost", "workspace_id": "w1"}}
        )
        assert state.focused_pane_id is None
        assert state.resync_pending is True
        state.apply_event(
            {"event": "pane_focused", "data": {"type": "pane_focused", "pane_id": "p1", "workspace_id": "w1"}}
        )
        assert state.focused_pane_id == "p1"
        assert state.panes()[0]["focused"] is True

    def test_output_matched_and_scroll_changed_are_ignored(self) -> None:
        state = self._state()
        state.apply_snapshot(make_snapshot(make_pane("p1")))
        applied = state.apply_event(
            {"event": "pane.output_matched", "data": {"type": "pane.output_matched", "pane_id": "p1", "matched_line": "x", "read": {"pane_id": "p1"}}}
        )
        assert applied is False
        applied = state.apply_event(
            {"event": "pane.scroll_changed", "data": {"type": "pane.scroll_changed", "pane_id": "p1", "scroll": {"offset_from_bottom": 0, "max_offset_from_bottom": 0, "viewport_rows": 10}}}
        )
        assert applied is False

    def test_unknown_event_kind_ignored(self) -> None:
        state = self._state()
        state.apply_snapshot(make_snapshot())
        assert state.apply_event({"event": "no_such_event", "data": {}}) is False

    def test_layout_updated_upserts_by_tab(self) -> None:
        state = self._state()
        state.apply_snapshot(make_snapshot(make_pane("p1", tab_id="t1")))
        layout = {
            "tab_id": "t1", "workspace_id": "w1", "zoomed": True,
            "focused_pane_id": "p1", "panes": [{"pane_id": "p1", "rect": {"x": 0, "y": 0, "width": 10, "height": 5}, "focused": True}],
        }
        state.apply_event({"event": "layout_updated", "data": {"type": "layout_updated", "layout": layout}})
        slim_layouts = state.layouts()
        assert slim_layouts[0]["zoomed"] is True
        assert state.layout_zoomed("t1") is True

    def test_workspace_metadata_updated_and_worktree_removed(self) -> None:
        state = self._state()
        snap = make_snapshot()
        snap["workspaces"] = [{"workspace_id": "w1", "label": "a", "number": 1, "active_tab_id": "t1", "focused": True, "pane_count": 1, "tab_count": 1, "agent_status": "idle", "tokens": {}, "worktree": {"path": "/tmp/x"}}]
        state.apply_snapshot(snap)
        state.apply_event(
            {"event": "workspace_metadata_updated", "data": {"type": "workspace_metadata_updated", "workspace": {"workspace_id": "w1", "label": "a", "number": 1, "active_tab_id": "t1", "focused": True, "pane_count": 2, "tab_count": 1, "agent_status": "working", "tokens": {}, "worktree": {"path": "/tmp/x"}}}}
        )
        assert state.workspaces()[0]["pane_count"] == 2
        state.apply_event(
            {"event": "worktree_removed", "data": {"type": "worktree_removed", "workspace_id": "w1", "worktree": {"path": "/tmp/x"}, "forced": False}}
        )
        assert state.workspaces()[0]["worktree"] is None

    def test_tab_moved_rebuilds_tabs(self) -> None:
        state = self._state()
        snap = make_snapshot()
        snap["tabs"] = [{"tab_id": "t1", "workspace_id": "w1", "label": "tab", "number": 1, "focused": True, "pane_count": 1, "agent_status": "idle"}]
        state.apply_snapshot(snap)
        state.apply_event(
            {"event": "tab_moved", "data": {"type": "tab_moved", "tab_id": "t1", "workspace_id": "w2", "insert_index": 0, "tabs": [{"tab_id": "t1", "workspace_id": "w2", "label": "tab", "number": 1, "focused": True, "pane_count": 1, "agent_status": "idle"}]}}
        )
        assert state.tabs()[0]["workspace_id"] == "w2"
        state.apply_event(
            {"event": "tab_closed", "data": {"type": "tab_closed", "tab_id": "t1"}}
        )
        assert state.tabs() == []

    def test_reads_return_deep_copies(self) -> None:
        state = self._state()
        state.apply_snapshot(make_snapshot(make_pane("p1", revision=1)))
        pane = state.panes()[0]
        pane["revision"] = 999
        assert state.panes()[0]["revision"] == 1

    def test_resync_replaces_everything(self) -> None:
        state = self._state()
        state.apply_snapshot(make_snapshot(make_pane("p1"), make_pane("p2")))
        state.apply_snapshot(make_snapshot(make_pane("p1", revision=9)))
        panes = state.panes()
        assert [p["pane_id"] for p in panes] == ["p1"]
        assert panes[0]["revision"] == 9


# ---------------------------------------------------------------------------
# StateStore 聚合
# ---------------------------------------------------------------------------

class TestStateStore:
    def test_snapshot_cached_shape_matches_existing_snapshot(self) -> None:
        store = herdr_state.StateStore()
        state = herdr_state.SessionState("work")
        state.apply_snapshot(
            make_snapshot(
                make_pane("p1", agent="codex", agent_status="working"),
                make_pane("p2", agent=None, cwd="/tmp"),
            )
        )
        store.set_session("work", state)
        out = store.snapshot_cached()
        assert out["available"] is True
        assert out["total_panes"] == 2
        assert out["agent_panes"] == 1
        assert out["panes"][0]["session"] == "work"
        assert out["sessions"][0]["panes"][0]["pane_id"] == "p1"

    def test_snapshot_cached_unavailable_when_empty(self) -> None:
        store = herdr_state.StateStore()
        out = store.snapshot_cached()
        assert out["available"] is False
        assert out["panes"] == []


# ---------------------------------------------------------------------------
# HerdrStateClient 生命周期
# ---------------------------------------------------------------------------

class TestHerdrStateClient:
    def test_bootstrap_and_subscribe_reach_subscribed(self, tmp_path: Path) -> None:
        server = FakeHerdrServer(
            tmp_path, snapshot=make_snapshot(make_pane("p1", agent="codex"))
        ).start()
        client = _start_client({"work": str(server.path)})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "subscribed"
                and client.state()["sessions"]["work"]["resyncs"] >= 1
            )
            out = client.snapshot_cached()
            assert out["available"] is True
            assert out["panes"][0]["pane_id"] == "p1"
        finally:
            client.stop()
            server.stop()

    def test_subscriptions_are_all_paramless(self) -> None:
        types = [s["type"] for s in herdr_state.ALL_SUBSCRIPTIONS]
        assert len(types) == 24
        assert len(set(types)) == 24
        assert "pane.output_matched" not in types
        assert "pane.agent_status_changed" not in types
        assert "pane.scroll_changed" not in types

    def test_event_applied_to_cache(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            if req is None:
                return
            method = req.get("method")
            if method == "ping":
                server.send(conn, _pong(str(req["id"])))
            elif method == "session.snapshot":
                server.send(conn, _snapshot_response(str(req["id"]), make_snapshot(make_pane("p1"))))
            elif method == "events.subscribe":
                server.send(conn, _started_response(str(req["id"])))
                server.send(conn, {
                    "event": "pane_agent_status_changed",
                    "data": {"type": "pane_agent_status_changed", "pane_id": "p1", "workspace_id": "w1", "agent_status": "blocked", "agent": "codex", "display_agent": "codex", "state_labels": {}, "title": None},
                })
                _hang(conn)
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        client = _start_client({"work": str(server.path)})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["events_seen"] >= 1
            )
            assert client.snapshot_cached()["panes"][0]["agent_status"] == "blocked"
        finally:
            client.stop()
            server.stop()

    def test_connect_refused_retries(self, tmp_path: Path) -> None:
        client = _start_client({"work": str(tmp_path / "not-there.sock")})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "reconnecting",
                timeout=2.0,
            )
            assert client.snapshot_cached()["available"] is False
            assert client.state()["sessions"]["work"]["reconnects"] >= 1
        finally:
            client.stop()

    def test_bootstrap_timeout_retries(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            if req is None:
                return
            if req.get("method") == "session.snapshot":
                time.sleep(0.5)  # 超过 snapshot_timeout
            else:
                server.send(conn, _pong(str(req["id"])))
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        client = herdr_state.HerdrStateClient(
            {"work": str(server.path)},
            connect_timeout=0.2, request_timeout=0.1, health_check_interval=None,
            reconnect_base_delay=0.02, reconnect_max_delay=0.05,
            snapshot_timeout=0.1,
        )
        client.start()
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["reconnects"] >= 1,
                timeout=3.0,
            )
        finally:
            client.stop()
            server.stop()

    def test_protocol_mismatch_stops_reconnecting(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            if req and req.get("method") == "ping":
                server.send(conn, _pong(str(req["id"]), protocol=18))
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        client = _start_client({"work": str(server.path)})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "protocol_mismatch"
            )
            time.sleep(0.3)
            assert client.state()["sessions"]["work"]["state"] == "protocol_mismatch"
            assert client.state()["sessions"]["work"]["reconnects"] == 0
        finally:
            client.stop()
            server.stop()

    def test_subscribe_rejected_retries(self, tmp_path: Path) -> None:
        attempts = {"n": 0}

        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            if req is None:
                return
            method = req.get("method")
            if method == "ping":
                server.send(conn, _pong(str(req["id"])))
            elif method == "session.snapshot":
                server.send(conn, _snapshot_response(str(req["id"]), make_snapshot()))
            elif method == "events.subscribe":
                attempts["n"] += 1
                if attempts["n"] < 3:
                    server.send(conn, _error_response(str(req["id"]), "internal_error"))
                    return
                server.send(conn, _started_response(str(req["id"])))
                _hang(conn)
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        client = _start_client({"work": str(server.path)})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "subscribed",
                timeout=5.0,
            )
            assert attempts["n"] >= 3
        finally:
            client.stop()
            server.stop()

    def test_method_missing_reports_capability_mismatch(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            if req is None:
                return
            method = req.get("method")
            if method == "ping":
                server.send(conn, _pong(str(req["id"])))
            elif method == "session.snapshot":
                server.send(conn, _snapshot_response(str(req["id"]), make_snapshot()))
            elif method == "events.subscribe":
                server.send(conn, _error_response(str(req["id"]), "method_not_found"))
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        client = _start_client({"work": str(server.path)})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "capability_mismatch",
                timeout=3.0,
            )
        finally:
            client.stop()
            server.stop()

    def test_eof_reconnects_and_resyncs(self, tmp_path: Path) -> None:
        """EOF → 重连 → 新 snapshot 全量覆盖（旧 pane 消失）。"""
        subscribe_count = {"n": 0}

        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            if req is None:
                return
            method = req.get("method")
            if method == "ping":
                server.send(conn, _pong(str(req["id"])))
            elif method == "session.snapshot":
                first_cycle = subscribe_count["n"] == 0
                snap = make_snapshot(make_pane("p1")) if first_cycle else make_snapshot(make_pane("p2"))
                server.send(conn, _snapshot_response(str(req["id"]), snap))
            elif method == "events.subscribe":
                subscribe_count["n"] += 1
                server.send(conn, _started_response(str(req["id"])))
                if subscribe_count["n"] == 1:
                    return  # 第一个订阅连接立即断开 → EOF
                _hang(conn)
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        client = _start_client({"work": str(server.path)})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "subscribed"
                and subscribe_count["n"] >= 2
                and client.state()["sessions"]["work"]["resyncs"] >= 2,
                timeout=5.0,
            )
            panes = client.snapshot_cached()["panes"]
            assert [p["pane_id"] for p in panes] == ["p2"]
        finally:
            client.stop()
            server.stop()

    def test_healthy_silent_subscription_is_not_reconnected(self, tmp_path: Path) -> None:
        """无事件 + 独立 ping 成功 → 保持 subscribed，不重连。"""
        server = FakeHerdrServer(tmp_path, snapshot=make_snapshot(make_pane("p1"))).start()
        client = _start_client({"work": str(server.path)}, health_check_interval=0.05)
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "subscribed"
            )
            time.sleep(0.4)  # 多个健康检查周期
            assert client.state()["sessions"]["work"]["state"] == "subscribed"
            assert client.state()["sessions"]["work"]["reconnects"] == 0
        finally:
            client.stop()
            server.stop()

    def test_health_ping_failure_reconnects(self, tmp_path: Path) -> None:
        """订阅后独立 ping 失败 → 重建连接。"""
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            if req is None:
                return
            method = req.get("method")
            if method == "ping":
                server.send(conn, _pong(str(req["id"])))
            elif method == "session.snapshot":
                server.send(conn, _snapshot_response(str(req["id"]), make_snapshot(make_pane("p1"))))
            elif method == "events.subscribe":
                server.send(conn, _started_response(str(req["id"])))
                _hang(conn)
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        client = _start_client({"work": str(server.path)}, health_check_interval=0.05)
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "subscribed"
            )
            # 之后所有 ping 连接都失败 → health ping 失败 → 重连
            orig_handler = server.handler

            def failing_handler(conn: socket.socket, srv: FakeHerdrServer) -> None:
                req = srv.read(conn)
                if req is None:
                    return
                if req.get("method") == "ping":
                    return  # 不响应 → EOF → health ping 失败
                orig_handler(conn, srv)
            server.handler = failing_handler
            _wait_for(
                lambda: client.state()["sessions"]["work"]["reconnects"] >= 1,
                timeout=5.0,
            )
        finally:
            client.stop()
            server.stop()

    def test_unknown_pane_event_triggers_resync(self, tmp_path: Path) -> None:
        """未知 pane 事件 → 触发一次全量 resync 补齐缓存。"""
        snapshots = {"n": 0}

        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            if req is None:
                return
            method = req.get("method")
            if method == "ping":
                server.send(conn, _pong(str(req["id"])))
            elif method == "session.snapshot":
                snapshots["n"] += 1
                if snapshots["n"] <= 2:  # bootstrap + 握手 resync 都是空
                    server.send(conn, _snapshot_response(str(req["id"]), make_snapshot()))
                else:
                    server.send(conn, _snapshot_response(str(req["id"]), make_snapshot(make_pane("p1"))))
            elif method == "events.subscribe":
                server.send(conn, _started_response(str(req["id"])))
                if snapshots["n"] <= 2:
                    server.send(conn, {
                        "event": "pane_agent_status_changed",
                        "data": {"type": "pane_agent_status_changed", "pane_id": "p1", "workspace_id": "w1", "agent_status": "working", "agent": "codex", "display_agent": "codex", "state_labels": {}, "title": None},
                    })
                _hang(conn)
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        client = _start_client({"work": str(server.path)}, resync_min_interval=0.05)
        try:
            # 握手 resync(1) + 未知 pane 事件触发 resync(2)，且 resync 后缓存补齐
            _wait_for(
                lambda: client.state()["sessions"]["work"]["resyncs"] >= 2
                and [p["pane_id"] for p in client.snapshot_cached()["panes"]] == ["p1"],
                timeout=5.0,
            )
        finally:
            client.stop()
            server.stop()

    def test_event_storm_is_not_lost(self, tmp_path: Path) -> None:
        N = 2000

        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            req = server.read(conn)
            if req is None:
                return
            method = req.get("method")
            if method == "ping":
                server.send(conn, _pong(str(req["id"])))
            elif method == "session.snapshot":
                server.send(conn, _snapshot_response(str(req["id"]), make_snapshot(make_pane("p1", revision=1))))
            elif method == "events.subscribe":
                server.send(conn, _started_response(str(req["id"])))
                for i in range(N):
                    server.send(conn, {
                        "event": "pane_updated",
                        "data": {"type": "pane_updated", "pane": make_pane("p1", revision=2 + i, agent_status="working")},
                    })
                _hang(conn)
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        client = _start_client({"work": str(server.path)})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["events_seen"] >= N,
                timeout=10.0,
            )
            assert client.snapshot_cached()["panes"][0]["revision"] == 1 + N
        finally:
            client.stop()
            server.stop()

    def test_multi_session_isolation(self, tmp_path: Path) -> None:
        good = FakeHerdrServer(tmp_path, snapshot=make_snapshot(make_pane("p1")), name="good").start()

        def bad_handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            conn.close()  # 每连接立即断开
        bad = FakeHerdrServer(tmp_path, handler=bad_handler, name="bad").start()

        client = _start_client({"good": str(good.path), "bad": str(bad.path)})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["good"]["state"] == "subscribed"
                and client.state()["sessions"]["bad"]["reconnects"] >= 1,
                timeout=5.0,
            )
            panes = client.snapshot_cached()["panes"]
            assert [p["pane_id"] for p in panes] == ["p1"]
        finally:
            client.stop()
            good.stop()
            bad.stop()

    def test_shutdown_with_hanging_connection(self, tmp_path: Path) -> None:
        """订阅挂起中 stop：快速返回、全部线程退出、幂等。"""
        server = FakeHerdrServer(tmp_path, snapshot=make_snapshot(make_pane("p1"))).start()
        client = _start_client({"work": str(server.path)})
        _wait_for(
            lambda: client.state()["sessions"]["work"]["state"] == "subscribed"
        )
        started = time.monotonic()
        exited = client.stop(join_timeout=2.0)
        elapsed = time.monotonic() - started
        assert exited is True
        assert elapsed < 1.5
        assert client.state()["sessions"]["work"]["state"] == "stopped"
        assert client.stop() is True  # 幂等
        server.stop()

    def test_stop_before_start_is_safe(self) -> None:
        client = herdr_state.HerdrStateClient({"work": "/nonexistent.sock"})
        client.stop()
        assert client.state()["running"] is False

    def test_snapshot_cached_after_stop_still_readable(self, tmp_path: Path) -> None:
        server = FakeHerdrServer(
            tmp_path, snapshot=make_snapshot(make_pane("p1"))
        ).start()
        client = _start_client({"work": str(server.path)})
        _wait_for(
            lambda: client.state()["sessions"]["work"]["state"] == "subscribed"
        )
        client.stop()
        out = client.snapshot_cached()
        assert out["available"] is True
        assert out["panes"][0]["pane_id"] == "p1"
        server.stop()
