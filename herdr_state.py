"""herdr_state.py — H0.4 socket 状态客户端。

直连 herdr 0.8 API socket（NDJSON 行协议，protocol 19）维护每个 session 的
实时状态缓存：session.snapshot 全量 bootstrap、events.subscribe 长连接增量
更新、线程安全聚合缓存、有上限指数退避重连 + 重连后全量 resync、可靠
shutdown。与 herdr_client.py（CLI 子进程封装）并行存在，不改其接口。

参考: herdr api schema --json; src/api/client.rs(NDJSON 请求/响应行),
src/api/server.rs(events.subscribe 长连接: 先回 subscription_started 再逐行
推送信封), src/api/schema/response.rs(ResponseResult: pong/session_snapshot/
subscription_started)。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

from herdr_client import HERDR_MIN_PROTOCOL

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_CONNECT_TIMEOUT_S = 3.0
DEFAULT_REQUEST_TIMEOUT_S = 5.0
DEFAULT_READ_IDLE_TIMEOUT_S = 30.0
DEFAULT_RECONNECT_BASE_DELAY_S = 0.5
DEFAULT_RECONNECT_MAX_DELAY_S = 8.0
DEFAULT_SNAPSHOT_TIMEOUT_S = 10.0

# events.subscribe 全量订阅（protocol 19 schema Subscription oneOf 全集）
ALL_SUBSCRIPTIONS: list[dict[str, str]] = [
    {"type": t} for t in (
        "workspace.created", "workspace.updated", "workspace.metadata_updated",
        "workspace.renamed", "workspace.moved", "workspace.reordered",
        "workspace.closed", "workspace.focused",
        "worktree.created", "worktree.opened", "worktree.removed",
        "tab.created", "tab.closed", "tab.focused", "tab.renamed", "tab.moved",
        "pane.created", "pane.closed", "pane.updated", "pane.focused",
        "pane.moved", "pane.exited", "pane.agent_detected",
        "pane.output_matched", "pane.agent_status_changed", "pane.scroll_changed",
        "layout.updated",
    )
]

# 订阅事件信封（与普通 EventEnvelope 区分；事件名带点号）
_SUBSCRIPTION_EVENTS = frozenset({
    "pane.output_matched", "pane.agent_status_changed", "pane.scroll_changed",
})

_LIFECYCLE_STATES = frozenset({
    "init", "connecting", "bootstrap", "subscribed", "reconnecting",
    "protocol_mismatch", "capability_mismatch", "stopped",
})


class HerdrSocketError(RuntimeError):
    """socket 传输层错误（连接拒绝/超时/EOF/畸形帧/error_response）。"""


class HerdrProtocolMismatchError(HerdrSocketError):
    """server protocol 与 HERDR_MIN_PROTOCOL 不匹配；永久停止，不再重连。"""


# ---------------------------------------------------------------------------
# wire 层
# ---------------------------------------------------------------------------

class HerdrSocket:
    """单个 per-session API socket 的 NDJSON 客户端。非线程安全，单线程使用。"""

    def __init__(self, path: str | Path, connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S) -> None:
        self.path = str(path)
        self.connect_timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._file: Any = None

    def connect(self) -> None:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.connect_timeout)
            sock.connect(self.path)
        except OSError as exc:
            raise HerdrSocketError(f"herdr socket 连接失败: {exc}") from exc
        self._sock = sock
        self._file = sock.makefile("rb")

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "HerdrSocket":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def request(
        self, method: str, params: dict[str, Any] | None = None,
        *, timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """发一个请求并读响应。返回 result；error_response 抛 HerdrSocketError。"""
        if self._sock is None:
            raise HerdrSocketError("socket 未连接")
        rid = request_id or f"h04:{method}"
        payload = {"id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._sock.settimeout(timeout)
            self._sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            response = self.read_line(timeout=timeout)
        except OSError as exc:
            raise HerdrSocketError(f"herdr {method} 传输失败: {exc}") from exc
        if response is None:
            raise HerdrSocketError(f"herdr {method} 连接被对端关闭")
        error = response.get("error")
        if error is not None:
            code = str(error.get("code") or "")
            message = str(error.get("message") or "")
            if method == "ping":
                raise HerdrSocketError(f"herdr {method} 失败: {code} {message}".strip())
            raise HerdrSocketError(f"herdr {method} 失败: {code} {message}".strip())
        result = response.get("result")
        if not isinstance(result, dict):
            raise HerdrSocketError(f"herdr {method} 响应缺少 result")
        return result

    def read_line(self, timeout: float = DEFAULT_READ_IDLE_TIMEOUT_S) -> dict[str, Any] | None:
        """读一行并解析 JSON。EOF 返回 None；超时/畸形抛 HerdrSocketError。"""
        if self._sock is None or self._file is None:
            raise HerdrSocketError("socket 未连接")
        try:
            self._sock.settimeout(timeout)
            line = self._file.readline()
        except socket.timeout as exc:
            raise HerdrSocketError(f"herdr socket 读空闲超时(>{timeout}s)") from exc
        except OSError as exc:
            raise HerdrSocketError(f"herdr socket 读失败: {exc}") from exc
        if not line:
            return None
        try:
            data = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HerdrSocketError("herdr socket 收到畸形 JSON 行") from exc
        if not isinstance(data, dict):
            raise HerdrSocketError("herdr socket 收到非对象 JSON 行")
        return data


# ---------------------------------------------------------------------------
# 单 session 状态
# ---------------------------------------------------------------------------

class SessionState:
    """单个 session 的实时缓存。由 HerdrStateClient 的 reader 线程写入，
    外部读取一律返回拷贝，保证线程安全。"""

    def __init__(self, session: str) -> None:
        self.session = session
        self._lock = threading.RLock()
        self._panes: dict[str, dict[str, Any]] = {}
        self._agents: list[dict[str, Any]] = []
        self._layouts: dict[str, dict[str, Any]] = {}
        self._workspaces: dict[str, dict[str, Any]] = {}
        self._tabs: dict[str, dict[str, Any]] = {}
        self.focused_pane_id: str | None = None
        self.focused_tab_id: str | None = None
        self.focused_workspace_id: str | None = None
        self.events_seen = 0
        self.applied_events = 0
        self.stale_events = 0
        self._bootstrapped = False

    # -- 写 ----------------------------------------------------------------
    def apply_snapshot(self, snap: dict[str, Any]) -> None:
        """全量覆盖（bootstrap / resync）。snap 是 SessionSnapshot 对象。"""
        if not isinstance(snap, dict):
            return
        with self._lock:
            self._bootstrapped = True
            panes: dict[str, dict[str, Any]] = {}
            for p in snap.get("panes") or []:
                slim = self._slim_pane(p)
                if slim and slim.get("pane_id"):
                    panes[str(slim["pane_id"])] = slim
            self._panes = panes
            agents = snap.get("agents") or []
            self._agents = [a for a in agents if isinstance(a, dict)]
            layouts: dict[str, dict[str, Any]] = {}
            for layout in snap.get("layouts") or []:
                if isinstance(layout, dict) and layout.get("tab_id"):
                    layouts[str(layout["tab_id"])] = self._slim_layout(layout)
            self._layouts = layouts
            workspaces: dict[str, dict[str, Any]] = {}
            for w in snap.get("workspaces") or []:
                if isinstance(w, dict) and w.get("workspace_id"):
                    workspaces[str(w["workspace_id"])] = w
            self._workspaces = workspaces
            tabs: dict[str, dict[str, Any]] = {}
            for t in snap.get("tabs") or []:
                if isinstance(t, dict) and t.get("tab_id"):
                    tabs[str(t["tab_id"])] = t
            self._tabs = tabs
            self.focused_pane_id = snap.get("focused_pane_id")
            self.focused_tab_id = snap.get("focused_tab_id")
            self.focused_workspace_id = snap.get("focused_workspace_id")

    def apply_event(self, envelope: dict[str, Any]) -> bool:
        """应用一个事件信封（普通 EventEnvelope 或 SubscriptionEventEnvelope）。

        返回 True 表示产生了缓存更新；未知事件/未知 pane/陈旧 revision 返回
        False 并计数。绝不抛出。
        """
        if not isinstance(envelope, dict):
            return False
        event = envelope.get("event")
        data = envelope.get("data")
        if not isinstance(event, str) or not isinstance(data, dict):
            return False
        with self._lock:
            self.events_seen += 1
            applied = self._apply(event, data)
            if applied:
                self.applied_events += 1
            else:
                self.stale_events += 1
            return applied

    def _apply(self, event: str, data: dict[str, Any]) -> bool:
        if event in ("pane_agent_status_changed", "pane.agent_status_changed"):
            return self._apply_agent_status(data)
        if event in ("pane_created", "pane_updated", "pane_moved"):
            return self._upsert_pane(data.get("pane"))
        if event in ("pane_closed", "pane_exited"):
            return self._remove_pane(data.get("pane_id"))
        if event == "pane_focused":
            return self._apply_focused(data.get("pane_id"))
        if event == "pane_output_changed":
            return self._apply_output_revision(data)
        if event == "pane_agent_detected":
            return self._apply_agent_detected(data)
        if event == "layout_updated":
            return self._upsert_layout(data.get("layout"))
        if event in ("workspace_created", "workspace_updated",
                     "workspace_moved", "workspace_reordered",
                     "worktree_created", "worktree_opened"):
            return self._upsert_workspace(data.get("workspace"))
        if event == "workspace_closed":
            return self._remove_workspace(data.get("workspace_id"))
        if event == "workspace_renamed":
            return self._rename_workspace(data)
        if event == "workspace_focused":
            return self._apply_workspace_focused(data.get("workspace_id"))
        if event == "tab_created":
            return self._upsert_tab(data.get("tab"))
        if event == "tab_closed":
            return self._remove_tab(data.get("tab_id"))
        if event == "tab_renamed":
            return self._rename_tab(data)
        if event == "tab_focused":
            return self._apply_tab_focused(data.get("tab_id"))
        # pane.output_matched / pane.scroll_changed / worktree.* / 未知事件
        return False

    def _upsert_pane(self, pane: Any) -> bool:
        slim = self._slim_pane(pane)
        if not slim or not slim.get("pane_id"):
            return False
        pane_id = str(slim["pane_id"])
        current = self._panes.get(pane_id)
        if current is not None and int(slim["revision"]) < int(current["revision"]):
            return False  # 乱序/重复：陈旧 revision 丢弃
        self._panes[pane_id] = slim
        return True

    def _remove_pane(self, pane_id: Any) -> bool:
        if not isinstance(pane_id, str):
            return False
        if self._panes.pop(pane_id, None) is None:
            return False
        if self.focused_pane_id == pane_id:
            self.focused_pane_id = None
        return True

    def _apply_focused(self, pane_id: Any) -> bool:
        if not isinstance(pane_id, str):
            return False
        self.focused_pane_id = pane_id
        return True

    def _apply_output_revision(self, data: dict[str, Any]) -> bool:
        pane_id = data.get("pane_id")
        if not isinstance(pane_id, str):
            return False
        current = self._panes.get(pane_id)
        if current is None:
            return False
        revision = data.get("revision")
        if not isinstance(revision, int):
            return False
        if revision < int(current["revision"]):
            return False
        current["revision"] = revision
        return True

    def _apply_agent_detected(self, data: dict[str, Any]) -> bool:
        pane_id = data.get("pane_id")
        if not isinstance(pane_id, str):
            return False
        current = self._panes.get(pane_id)
        if current is None:
            return False
        agent = data.get("agent")
        if agent is not None and not isinstance(agent, str):
            return False
        current["agent"] = agent
        current["agent_status"] = "idle"
        return True

    def _apply_agent_status(self, data: dict[str, Any]) -> bool:
        pane_id = data.get("pane_id")
        if not isinstance(pane_id, str):
            return False
        current = self._panes.get(pane_id)
        if current is None:
            return False  # 未知 pane：bootstrap 未覆盖，等下次 resync
        status = data.get("agent_status")
        if status is not None:
            current["agent_status"] = status
        agent = data.get("agent")
        if agent is not None:
            current["agent"] = agent
        display = data.get("display_agent")
        if display is not None:
            current["display_agent"] = display
        return True

    def _upsert_layout(self, layout: Any) -> bool:
        slim = self._slim_layout(layout)
        if not slim or not slim.get("tab_id"):
            return False
        self._layouts[str(slim["tab_id"])] = slim
        return True

    def _upsert_workspace(self, workspace: Any) -> bool:
        if not isinstance(workspace, dict) or not workspace.get("workspace_id"):
            return False
        self._workspaces[str(workspace["workspace_id"])] = workspace
        return True

    def _remove_workspace(self, workspace_id: Any) -> bool:
        if not isinstance(workspace_id, str):
            return False
        return self._workspaces.pop(workspace_id, None) is not None

    def _rename_workspace(self, data: dict[str, Any]) -> bool:
        workspace_id = data.get("workspace_id")
        label = data.get("label")
        if not isinstance(workspace_id, str) or not isinstance(label, str):
            return False
        current = self._workspaces.get(workspace_id)
        if current is None:
            return False
        current["label"] = label
        return True

    def _apply_workspace_focused(self, workspace_id: Any) -> bool:
        if not isinstance(workspace_id, str):
            return False
        self.focused_workspace_id = workspace_id
        return True

    def _upsert_tab(self, tab: Any) -> bool:
        if not isinstance(tab, dict) or not tab.get("tab_id"):
            return False
        self._tabs[str(tab["tab_id"])] = tab
        return True

    def _remove_tab(self, tab_id: Any) -> bool:
        if not isinstance(tab_id, str):
            return False
        return self._tabs.pop(tab_id, None) is not None

    def _rename_tab(self, data: dict[str, Any]) -> bool:
        tab_id = data.get("tab_id")
        label = data.get("label")
        if not isinstance(tab_id, str) or not isinstance(label, str):
            return False
        current = self._tabs.get(tab_id)
        if current is None:
            return False
        current["label"] = label
        return True

    def _apply_tab_focused(self, tab_id: Any) -> bool:
        if not isinstance(tab_id, str):
            return False
        self.focused_tab_id = tab_id
        return True

    # -- 读（返回拷贝） ------------------------------------------------------
    def panes(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._panes.values())

    def agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._agents)

    def layouts(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._layouts.values())

    def workspaces(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._workspaces.values())

    def tabs(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._tabs.values())

    def layout_zoomed(self, tab_id: str) -> bool:
        with self._lock:
            layout = self._layouts.get(tab_id)
            return bool(layout and layout.get("zoomed"))

    def bootstrapped(self) -> bool:
        with self._lock:
            return self._bootstrapped

    def snapshot_cached(self) -> dict[str, Any]:
        """输出形状与 herdr_client._snapshot_session 兼容。"""
        with self._lock:
            return {
                "session": self.session,
                "status": "running",
                "panes": list(self._panes.values()),
                "agents": list(self._agents),
                "focused_pane_id": self.focused_pane_id,
                "layouts": list(self._layouts.values()),
            }

    # -- slim 构造 -----------------------------------------------------------
    def _slim_pane(self, p: Any) -> dict[str, Any] | None:
        if not isinstance(p, dict) or not p.get("pane_id"):
            return None
        cwd = p.get("cwd") or p.get("foreground_cwd") or ""
        return {
            "pane_id": p.get("pane_id"),
            "session": self.session,
            "workspace_id": p.get("workspace_id"),
            "tab_id": p.get("tab_id"),
            "agent": p.get("agent"),
            "display_agent": p.get("display_agent"),
            "agent_status": p.get("agent_status"),
            "cwd": cwd,
            "cwd_name": cwd.rstrip("/").split("/")[-1] if cwd else "",
            "label": p.get("name") or p.get("label"),
            "terminal_title": p.get("terminal_title_stripped") or p.get("terminal_title"),
            "focused": p.get("focused", False),
            "revision": p.get("revision", 0),
        }

    @staticmethod
    def _slim_layout(layout: Any) -> dict[str, Any] | None:
        if not isinstance(layout, dict):
            return None
        panes = []
        for p in layout.get("panes") or []:
            if not isinstance(p, dict):
                continue
            rect = p.get("rect") or {}
            panes.append({
                "pane_id": p.get("pane_id"),
                "focused": p.get("focused", False),
                "x": rect.get("x", 0),
                "y": rect.get("y", 0),
                "width": rect.get("width", 0),
                "height": rect.get("height", 0),
            })
        ys = [p["y"] for p in panes]
        return {
            "tab_id": layout.get("tab_id"),
            "workspace_id": layout.get("workspace_id"),
            "zoomed": bool(layout.get("zoomed", False)),
            "focused_pane_id": layout.get("focused_pane_id"),
            "panes": panes,
            "horizontal_split": len(panes) > 1 and len(set(ys)) < len(ys),
            "area": layout.get("area") or {},
        }


# ---------------------------------------------------------------------------
# 聚合缓存
# ---------------------------------------------------------------------------

class StateStore:
    """多 session 聚合。session 状态由各自 reader 线程写入，读取加锁。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionState] = {}

    def set_session(self, name: str, state: SessionState) -> None:
        with self._lock:
            self._sessions[name] = state

    def drop_session(self, name: str) -> None:
        with self._lock:
            self._sessions.pop(name, None)

    def get(self, name: str) -> SessionState | None:
        with self._lock:
            return self._sessions.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())

    def snapshot_cached(self) -> dict[str, Any]:
        """聚合形状兼容 herdr_client.snapshot()（sessions/panes/agents/计数）。"""
        with self._lock:
            sessions = list(self._sessions.values())
        if not sessions:
            return {"available": False, "sessions": [], "panes": [], "agents": []}
        results = [s.snapshot_cached() for s in sessions]
        all_panes = [p for s in results for p in s["panes"]]
        all_agents = [a for s in results for a in s["agents"]]
        # available = 至少一个 session 完成过 bootstrap（有真实缓存）
        available = any(s.bootstrapped() for s in sessions)
        return {
            "available": available,
            "sessions": results,
            "panes": all_panes,
            "agents": all_agents,
            "total_panes": len(all_panes),
            "agent_panes": sum(1 for p in all_panes if p.get("agent")),
        }


# ---------------------------------------------------------------------------
# 生命周期客户端
# ---------------------------------------------------------------------------

class HerdrStateClient:
    """为每个 session 启动一个 reader 线程：connect → ping 协议门 → snapshot
    bootstrap → events.subscribe 长连接增量更新；断线按有上限指数退避重连，
    重连成功后全量 resync。stop() 幂等，关闭 socket 后 join 所有线程。
    """

    def __init__(
        self, sessions: dict[str, str], *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
        read_idle_timeout: float = DEFAULT_READ_IDLE_TIMEOUT_S,
        reconnect_base_delay: float = DEFAULT_RECONNECT_BASE_DELAY_S,
        reconnect_max_delay: float = DEFAULT_RECONNECT_MAX_DELAY_S,
        snapshot_timeout: float = DEFAULT_SNAPSHOT_TIMEOUT_S,
    ) -> None:
        self._sessions = dict(sessions)
        self._store = StateStore()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._sockets: dict[str, HerdrSocket] = {}
        self._lifecycle: dict[str, dict[str, Any]] = {}
        for name in self._sessions:
            self._lifecycle[name] = {
                "state": "init",
                "last_error": None,
                "connected_at": None,
                "events_seen": 0,
                "applied_events": 0,
                "stale_events": 0,
                "reconnects": 0,
                "resyncs": 0,
            }
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._read_idle_timeout = read_idle_timeout
        self._reconnect_base_delay = reconnect_base_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._snapshot_timeout = snapshot_timeout

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if not self._stop.is_set():
            self._stop.clear()
        for name, path in self._sessions.items():
            state = SessionState(name)
            self._store.set_session(name, state)
            thread = threading.Thread(
                target=self._run_session, args=(name, path, state),
                name=f"cockpit-state-{name}", daemon=True,
            )
            self._threads[name] = thread
            thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        """幂等：置停止标志 → 关闭所有 socket（解除阻塞读）→ join 线程。"""
        self._stop.set()
        with self._lock:
            sockets = list(self._sockets.values())
            threads = list(self._threads.values())
            self._sockets.clear()
        for sock in sockets:
            sock.close()
        for thread in threads:
            thread.join(timeout=join_timeout)
        for name in self._lifecycle:
            if self._lifecycle[name]["state"] not in ("protocol_mismatch", "capability_mismatch"):
                self._lifecycle[name]["state"] = "stopped"

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": not self._stop.is_set(),
                "sessions": {
                    name: dict(lc) for name, lc in self._lifecycle.items()
                },
            }

    def snapshot_cached(self) -> dict[str, Any]:
        return self._store.snapshot_cached()

    # -- per-session runner -------------------------------------------------
    def _run_session(self, name: str, path: str, state: SessionState) -> None:
        lc = self._lifecycle[name]
        delay = max(0.0, self._reconnect_base_delay)
        while not self._stop.is_set():
            lc["state"] = "connecting"
            sock = HerdrSocket(path, connect_timeout=self._connect_timeout)
            try:
                sock.connect()
            except HerdrSocketError as exc:
                lc["last_error"] = str(exc)
                lc["state"] = "reconnecting"
                lc["reconnects"] += 1
                if not self._backoff(delay):
                    return
                delay = min(delay * 2, self._reconnect_max_delay)
                continue
            delay = max(0.0, self._reconnect_base_delay)
            with self._lock:
                self._sockets[name] = sock
            try:
                self._handle_connection(name, path, sock, state, lc)
                if self._stop.is_set():
                    return
                lc["state"] = "reconnecting"
                lc["reconnects"] += 1
            except HerdrProtocolMismatchError as exc:
                lc["last_error"] = str(exc)
                if lc["state"] != "capability_mismatch":
                    lc["state"] = "protocol_mismatch"
                return
            except HerdrSocketError as exc:
                lc["last_error"] = str(exc)
                if self._stop.is_set():
                    return
                lc["state"] = "reconnecting"
                lc["reconnects"] += 1
            except Exception as exc:  # 兜底：reader 逻辑自身 bug 不杀线程
                lc["last_error"] = f"crash: {exc}"
                if self._stop.is_set():
                    return
                lc["state"] = "reconnecting"
                lc["reconnects"] += 1
            finally:
                with self._lock:
                    if self._sockets.get(name) is sock:
                        self._sockets.pop(name, None)
                sock.close()
            if self._stop.is_set():
                return
            if not self._backoff(delay):
                return
            delay = min(delay * 2, self._reconnect_max_delay)

    def _handle_connection(
        self, name: str, path: str, sock: HerdrSocket, state: SessionState,
        lc: dict[str, Any],
    ) -> None:
        # 协议门：ping → pong.protocol 必须等于 HERDR_MIN_PROTOCOL
        pong = sock.request("ping", timeout=self._request_timeout)
        protocol = pong.get("protocol")
        if protocol != HERDR_MIN_PROTOCOL:
            raise HerdrProtocolMismatchError(
                f"herdr protocol {protocol} 不满足最低要求 {HERDR_MIN_PROTOCOL}"
            )
        # bootstrap：session.snapshot 全量覆盖
        lc["state"] = "bootstrap"
        response = sock.request(
            "session.snapshot", timeout=self._snapshot_timeout,
        )
        snap = response.get("snapshot")
        if not isinstance(snap, dict):
            raise HerdrSocketError("session.snapshot 响应缺少 snapshot")
        was_bootstrapped = lc.get("bootstrapped", False)
        state.apply_snapshot(snap)
        if was_bootstrapped:
            lc["resyncs"] += 1
        lc["bootstrapped"] = True
        lc["connected_at"] = time.monotonic()
        # subscribe：长连接，先回 subscription_started 再逐行推事件
        lc["state"] = "subscribed"
        try:
            sock.request(
                "events.subscribe",
                params={"subscriptions": ALL_SUBSCRIPTIONS},
                timeout=self._request_timeout,
            )
        except HerdrSocketError as exc:
            if "method" in str(exc) and "not_found" in str(exc):
                lc["last_error"] = str(exc)
                lc["state"] = "capability_mismatch"
                raise HerdrProtocolMismatchError(str(exc)) from exc
            raise
        while not self._stop.is_set():
            envelope = sock.read_line(timeout=self._read_idle_timeout)
            if envelope is None:
                raise HerdrSocketError("events.subscribe 连接被对端关闭")
            state.apply_event(envelope)
            # 把 state 侧计数同步到 lifecycle（state() 从这里读）
            lc["events_seen"] = state.events_seen
            lc["applied_events"] = state.applied_events
            lc["stale_events"] = state.stale_events

    def _backoff(self, delay: float) -> bool:
        """等待 delay 秒；stop 时立即返回 False。"""
        if self._stop.wait(delay):
            return False
        return True
