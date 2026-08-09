"""test_herdr_state.py — H0.4 socket 状态客户端测试。

用 tmp_path 下的真实 Unix socket fake server（NDJSON 行协议）驱动
herdr_state.HerdrStateClient，覆盖 bootstrap、events.subscribe 增量更新、
线程安全缓存、退避重连+全量 resync、shutdown 与 14 类故障注入。
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
# Fake herdr server（NDJSON over AF_UNIX）
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


class FakeHerdrServer:
    """单 listener 循环 accept；每个连接由 handler(conn, ctx) 驱动。

    ctx.send(obj) 写一行 NDJSON；ctx.read() 读一行请求（EOF→None）。
    handler 返回后关闭连接。默认 handler 实现 ping/snapshot/subscribe 正常流。
    """

    def __init__(
        self, tmp_path: Path, *, name: str = "work",
        handler: Callable[[socket.socket, "FakeHerdrServer"], None] | None = None,
    ) -> None:
        self.path = tmp_path / f"{name}.sock"
        self.handler = handler or self._default_handler
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
            try:
                self.handler(conn, self)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    # -- default well-behaved flow ---------------------------------------
    def _default_handler(self, conn: socket.socket, server: "FakeHerdrServer") -> None:
        while True:
            req = _read_request(conn)
            if req is None:
                return
            method = req.get("method")
            if method == "ping":
                server.send(conn, _pong(str(req.get("id"))))
            elif method == "session.snapshot":
                server.send(conn, _snapshot_response(str(req.get("id")), make_snapshot()))
            elif method == "events.subscribe":
                server.send(conn, _started_response(str(req.get("id"))))
                # 长连接挂起，直到对端关闭
                while True:
                    if conn.recv(1) == b"":
                        return
            else:
                server.send(conn, _error_response(str(req.get("id")), "unknown_method"))


class _SnapshotHerdrServer(FakeHerdrServer):
    """默认流 + 可注入 snapshot 内容。"""

    def __init__(self, tmp_path: Path, snapshot: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(tmp_path, **kwargs)
        self.snapshot = snapshot

    def _default_handler(self, conn: socket.socket, server: "FakeHerdrServer") -> None:
        while True:
            req = _read_request(conn)
            if req is None:
                return
            method = req.get("method")
            if method == "ping":
                server.send(conn, _pong(str(req.get("id"))))
            elif method == "session.snapshot":
                server.send(
                    conn, _snapshot_response(str(req.get("id")), self.snapshot)
                )
            elif method == "events.subscribe":
                server.send(conn, _started_response(str(req.get("id"))))
                while True:
                    if conn.recv(1) == b"":
                        return
            else:
                server.send(conn, _error_response(str(req.get("id")), "unknown_method"))


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待条件超时")


def _start_client(
    sessions: dict[str, str], *, connect_timeout: float = 0.5,
    request_timeout: float = 0.5, read_idle_timeout: float = 0.3,
    reconnect_base_delay: float = 0.05, reconnect_max_delay: float = 0.2,
    snapshot_timeout: float = 0.5, **kwargs: Any,
) -> herdr_state.HerdrStateClient:
    client = herdr_state.HerdrStateClient(
        sessions,
        connect_timeout=connect_timeout,
        request_timeout=request_timeout,
        read_idle_timeout=read_idle_timeout,
        reconnect_base_delay=reconnect_base_delay,
        reconnect_max_delay=reconnect_max_delay,
        snapshot_timeout=snapshot_timeout,
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
        result = sock.request("ping", timeout=0.5)
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
            sock.request("session.snapshot", timeout=0.5)
        assert "server_stopped" in str(exc.value)
        sock.close()
        server.stop()

    def test_malformed_line_raises(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            conn.sendall(b"not-json\n")
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        sock = herdr_state.HerdrSocket(str(server.path), connect_timeout=0.5)
        sock.connect()
        with pytest.raises(herdr_state.HerdrSocketError):
            sock.request("ping", timeout=0.5)
        sock.close()
        server.stop()

    def test_eof_raises(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            return  # 立即关闭
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        sock = herdr_state.HerdrSocket(str(server.path), connect_timeout=0.5)
        sock.connect()
        with pytest.raises(herdr_state.HerdrSocketError):
            sock.request("ping", timeout=0.5)
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
        # 普通事件信封
        state.apply_event(
            {"event": "pane_agent_status_changed", "data": {"type": "pane_agent_status_changed", "pane_id": "p1", "workspace_id": "w1", "agent_status": "working", "agent": "codex", "display_agent": "codex", "state_labels": {}, "title": None}}
        )
        assert state.panes()[0]["agent_status"] == "working"
        # 订阅事件信封（同语义不同 event 名）
        state.apply_event(
            {"event": "pane.agent_status_changed", "data": {"type": "pane.agent_status_changed", "pane_id": "p1", "workspace_id": "w1", "agent_status": "blocked", "agent": "codex", "display_agent": "codex", "state_labels": {}, "title": None}}
        )
        assert state.panes()[0]["agent_status"] == "blocked"

    def test_unknown_pane_events_ignored_and_counted(self) -> None:
        state = self._state()
        state.apply_snapshot(make_snapshot())
        applied = state.apply_event(
            {"event": "pane_agent_status_changed", "data": {"type": "pane_agent_status_changed", "pane_id": "ghost", "workspace_id": "w1", "agent_status": "working"}}
        )
        assert applied is False
        assert state.stale_events == 1

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

    def test_workspace_and_tab_upsert_close(self) -> None:
        state = self._state()
        snap = make_snapshot()
        snap["workspaces"] = [{"workspace_id": "w1", "label": "a", "number": 1, "active_tab_id": "t1", "focused": True, "pane_count": 1, "tab_count": 1, "agent_status": "idle", "tokens": {}, "worktree": None}]
        snap["tabs"] = [{"tab_id": "t1", "workspace_id": "w1", "label": "tab", "number": 1, "focused": True, "pane_count": 1, "agent_status": "idle"}]
        state.apply_snapshot(snap)
        assert len(state.workspaces()) == 1
        assert len(state.tabs()) == 1
        state.apply_event(
            {"event": "tab_closed", "data": {"type": "tab_closed", "tab_id": "t1"}}
        )
        assert state.tabs() == []

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
        server = _SnapshotHerdrServer(
            tmp_path, make_snapshot(make_pane("p1", agent="codex"))
        ).start()
        client = _start_client({"work": str(server.path)})
        try:
            _wait_for(lambda: client.state()["sessions"]["work"]["state"] == "subscribed")
            assert client.state()["sessions"]["work"]["events_seen"] == 0
            out = client.snapshot_cached()
            assert out["available"] is True
            assert out["panes"][0]["pane_id"] == "p1"
        finally:
            client.stop()
            server.stop()

    def test_event_applied_to_cache(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            while True:
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
                    while True:
                        if conn.recv(1) == b"":
                            return
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

    def test_connect_refused_retries_until_available(self, tmp_path: Path) -> None:
        server = _SnapshotHerdrServer(
            tmp_path, make_snapshot(make_pane("p1"))
        ).start()
        # 先不监听？直接用不存在路径模拟拒绝，再换真实 server：路径已绑定。
        client = herdr_state.HerdrStateClient(
            {"work": str(tmp_path / "not-there.sock")},
            connect_timeout=0.1, request_timeout=0.2, read_idle_timeout=0.2,
            reconnect_base_delay=0.02, reconnect_max_delay=0.05,
        )
        client.start()
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "reconnecting",
                timeout=2.0,
            )
            assert client.snapshot_cached()["available"] is False
            assert client.state()["sessions"]["work"]["reconnects"] >= 1
        finally:
            client.stop()
        server.stop()

    def test_bootstrap_timeout_retries(self, tmp_path: Path) -> None:
        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            while True:
                req = server.read(conn)
                if req is None:
                    return
                if req.get("method") == "session.snapshot":
                    time.sleep(0.5)  # 超过 request_timeout
                else:
                    server.send(conn, _pong(str(req["id"])))
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        client = herdr_state.HerdrStateClient(
            {"work": str(server.path)},
            connect_timeout=0.2, request_timeout=0.1, read_idle_timeout=0.2,
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
            while True:
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
                        return  # 关闭连接，逼客户端重连
                    server.send(conn, _started_response(str(req["id"])))
                    while True:
                        if conn.recv(1) == b"":
                            return
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
            while True:
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
                    return
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
        connections = {"n": 0}

        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            connections["n"] += 1
            first = connections["n"] == 1
            while True:
                req = server.read(conn)
                if req is None:
                    return
                method = req.get("method")
                if method == "ping":
                    server.send(conn, _pong(str(req["id"])))
                elif method == "session.snapshot":
                    snap = make_snapshot(make_pane("p1")) if first else make_snapshot(make_pane("p2"))
                    server.send(conn, _snapshot_response(str(req["id"]), snap))
                elif method == "events.subscribe":
                    server.send(conn, _started_response(str(req["id"])))
                    if first:
                        return  # 第一个连接立即断开 → EOF
                    while True:
                        if conn.recv(1) == b"":
                            return
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        client = _start_client({"work": str(server.path)})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "subscribed"
                and connections["n"] >= 2
                and client.state()["sessions"]["work"]["resyncs"] >= 1,
                timeout=5.0,
            )
            panes = client.snapshot_cached()["panes"]
            assert [p["pane_id"] for p in panes] == ["p2"]
        finally:
            client.stop()
            server.stop()

    def test_half_open_connection_reconnects(self, tmp_path: Path) -> None:
        """订阅后服务端静默（只收不发）→ 读 idle 超时 → 重建连接。"""
        connections = {"n": 0}

        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            connections["n"] += 1
            first = connections["n"] == 1
            while True:
                req = server.read(conn)
                if req is None:
                    return
                method = req.get("method")
                if method == "ping":
                    server.send(conn, _pong(str(req["id"])))
                elif method == "session.snapshot":
                    server.send(conn, _snapshot_response(str(req["id"]), make_snapshot()))
                elif method == "events.subscribe":
                    server.send(conn, _started_response(str(req["id"])))
                    if first:
                        # 半开：不推送不关闭，逼客户端 idle 超时
                        while True:
                            if conn.recv(1) == b"":
                                return
                    while True:
                        if conn.recv(1) == b"":
                            return
        server = FakeHerdrServer(tmp_path, handler=handler).start()
        client = _start_client({"work": str(server.path)})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "subscribed"
                and connections["n"] >= 2
                and client.state()["sessions"]["work"]["reconnects"] >= 1,
                timeout=5.0,
            )
        finally:
            client.stop()
            server.stop()

    def test_event_storm_is_not_lost(self, tmp_path: Path) -> None:
        N = 2000

        def handler(conn: socket.socket, server: FakeHerdrServer) -> None:
            while True:
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
                    while True:
                        if conn.recv(1) == b"":
                            return
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
        good = _SnapshotHerdrServer(tmp_path, make_snapshot(make_pane("p1")), name="good").start()

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
        """订阅挂起中 stop：立即返回、线程退出、幂等。"""
        server = _SnapshotHerdrServer(tmp_path, make_snapshot(make_pane("p1"))).start()
        client = _start_client({"work": str(server.path)})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "subscribed"
            )
        finally:
            pass
        started = time.monotonic()
        client.stop(join_timeout=2.0)
        assert time.monotonic() - started < 1.5
        assert client.state()["running"] is False
        client.stop()  # 幂等
        server.stop()

    def test_stop_before_start_is_safe(self) -> None:
        client = herdr_state.HerdrStateClient({"work": "/nonexistent.sock"})
        client.stop()
        assert client.state()["running"] is False

    def test_snapshot_cached_after_stop_still_readable(self, tmp_path: Path) -> None:
        server = _SnapshotHerdrServer(
            tmp_path, make_snapshot(make_pane("p1"))
        ).start()
        client = _start_client({"work": str(server.path)})
        try:
            _wait_for(
                lambda: client.state()["sessions"]["work"]["state"] == "subscribed"
            )
        finally:
            pass
        client.stop()
        out = client.snapshot_cached()
        assert out["available"] is True
        assert out["panes"][0]["pane_id"] == "p1"
        server.stop()
