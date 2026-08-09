"""herdr_state.py — H0.4 socket 状态客户端。

直连 herdr 0.8 API socket（NDJSON 行协议，protocol 19）维护每个 session 的
实时状态缓存：session.snapshot 全量 bootstrap、events.subscribe 长连接增量
更新、线程安全聚合缓存、有上限指数退避重连 + 重连后全量 resync、可靠
shutdown。与 herdr_client.py（CLI 子进程封装）并行存在，不改其接口。

真实协议契约（herdr 0.8.0 实测）：
- 每个请求必须携带 `params` 字段（serde flatten+tag/content 要求，缺则
  `invalid_request missing field params`）；Request={id, method, params}。
- server 每连接只读一条 initial request：普通请求响应后即关闭连接；仅
  events.subscribe（与 pane.graphics.stream）为长连接。
- events.subscribe 的 Subscription 只有 24 个无参类型；pane.output_matched、
  pane.agent_status_changed、pane.scroll_changed 必须带 pane_id 等参数，
  不能全局订阅。
- 成功响应 {id, result:{type:...}}；error {id, error:{code, message}}。
参考: src/api/schema.rs(Request), src/api/client.rs(NDJSON 行),
src/api/server.rs(handle_connection 一连接一请求; stream_subscriptions 长连接)。
"""
from __future__ import annotations

import copy
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
# 订阅连接健康检查间隔：读空闲超过该值后用独立连接 ping 探测 server；
# None 表示无限阻塞（不探测）。真实订阅无事件时本就静默，不能直接重连。
DEFAULT_HEALTH_CHECK_INTERVAL_S = 30.0
DEFAULT_RECONNECT_BASE_DELAY_S = 0.5
DEFAULT_RECONNECT_MAX_DELAY_S = 8.0
DEFAULT_SNAPSHOT_TIMEOUT_S = 10.0
# 未知 pane 事件触发 resync 的最小间隔（防抖）
DEFAULT_RESYNC_MIN_INTERVAL_S = 5.0

# events.subscribe 合法无参订阅（protocol 19 schema Subscription oneOf 中
# 不带必填参数的 24 个类型；pane.output_matched / pane.agent_status_changed /
# pane.scroll_changed 必须带 pane_id 等参数，不能全局订阅）。
ALL_SUBSCRIPTIONS: list[dict[str, str]] = [
    {"type": t} for t in (
        "workspace.created", "workspace.updated", "workspace.metadata_updated",
        "workspace.renamed", "workspace.moved", "workspace.reordered",
        "workspace.closed", "workspace.focused",
        "worktree.created", "worktree.opened", "worktree.removed",
        "tab.created", "tab.closed", "tab.focused", "tab.renamed", "tab.moved",
        "pane.created", "pane.closed", "pane.updated", "pane.focused",
        "pane.moved", "pane.exited", "pane.agent_detected",
        "layout.updated",
    )
]


class HerdrSocketError(RuntimeError):
    """socket 传输层错误（连接拒绝/超时/EOF/畸形帧/error_response/响应不匹配）。"""


class HerdrSocketIdleTimeout(HerdrSocketError):
    """订阅连接读空闲超时（仅健康检查用，不代表断连）。"""


class HerdrProtocolMismatchError(HerdrSocketError):
    """server protocol 与 HERDR_MIN_PROTOCOL 不匹配；永久停止，不再重连。"""


# ---------------------------------------------------------------------------
# wire 层
# ---------------------------------------------------------------------------

class HerdrSocket:
    """单个 API socket 的 NDJSON 客户端。一连接一请求（订阅除外），单线程使用。

    不用 socket.makefile（超时触发后其内部缓冲进入 timed out 状态，后续
    readline 全部失败），改为 recv 手动缓冲按行切分。
    """

    def __init__(self, path: str | Path, connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S) -> None:
        self.path = str(path)
        self.connect_timeout = connect_timeout
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._buf = b""
        self._closed = False

    def connect(self) -> None:
        """创建 socket 后立即赋值 self._sock，再执行阻塞 connect。与 close
        互斥（_lock）：stop 在 register→connect 之间 close 会置 closed 标志，
        connect 入口直接失败；close 在 connect 阻塞中执行会关闭真实 fd 并
        中断 connect。"""
        with self._lock:
            if self._closed:
                raise HerdrSocketError("herdr socket 已关闭")
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            except OSError as exc:
                raise HerdrSocketError(f"herdr socket 创建失败: {exc}") from exc
            self._sock = sock
            sock.settimeout(self.connect_timeout)
        try:
            sock.connect(self.path)
        except OSError as exc:
            with self._lock:
                if self._sock is sock:
                    self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            raise HerdrSocketError(f"herdr socket 连接失败: {exc}") from exc

    def close(self) -> None:
        """跨线程幂等：锁内置 closed 并快照清空，锁外 shutdown/close。
        stop 主线程与 reader finally 并发 close 同一对象时后到者直接返回；
        close 与 connect 互斥，保证 connect 前/中/后任意时刻均可取消。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sock = self._sock
            self._sock = None
            self._buf = b""
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "HerdrSocket":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def request(
        self, method: str, params: dict[str, Any], *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
        request_id: str | None = None,
        expect_type: str | None = None,
    ) -> dict[str, Any]:
        """发一个请求并读响应。

        params 必传（真实 server 要求字段存在，缺则 invalid_request）。
        校验响应 id 与请求一致、result.type 为 expect_type；error 抛
        HerdrSocketError。
        """
        if self._sock is None:
            raise HerdrSocketError("socket 未连接")
        rid = request_id or f"h04:{method}:{time.monotonic_ns()}"
        payload = {"id": rid, "method": method, "params": params}
        try:
            sock = self._sock  # 局部快照：close 并发时不出现 NoneType 访问
            if sock is None:
                raise HerdrSocketError("socket 未连接")
            sock.settimeout(timeout)
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            response = self.read_line(timeout=timeout)
        except OSError as exc:
            raise HerdrSocketError(f"herdr {method} 传输失败: {exc}") from exc
        if response is None:
            raise HerdrSocketError(f"herdr {method} 连接被对端关闭")
        # 严格校验响应 id（success/error 一律先验 id）：客户端发出的请求
        # 始终有可解析 id，无法关联的响应不能归属于当前请求；server 原始
        # error 附在 mismatch 文本中保留诊断，但不按当前请求错误分支解释。
        response_id = response.get("id")
        error = response.get("error")
        if response_id != rid:
            detail = ""
            if error is not None:
                code = str(error.get("code") or "")
                message = str(error.get("message") or "")
                detail = f" (server error: {code} {message})".rstrip()
            raise HerdrSocketError(f"herdr {method} 响应 id 不匹配{detail}")
        if error is not None:
            code = str(error.get("code") or "")
            message = str(error.get("message") or "")
            raise HerdrSocketError(f"herdr {method} 失败: {code} {message}".strip())
        result = response.get("result")
        if not isinstance(result, dict):
            raise HerdrSocketError(f"herdr {method} 响应缺少 result")
        rtype = result.get("type")
        if expect_type is not None and rtype != expect_type:
            raise HerdrSocketError(
                f"herdr {method} 响应类型不匹配: {rtype!r} != {expect_type!r}"
            )
        return result

    def read_line(self, timeout: float | None = None) -> dict[str, Any] | None:
        """读一行并解析 JSON。EOF 返回 None；超时抛 HerdrSocketIdleTimeout；
        畸形抛 HerdrSocketError。timeout=None 无限阻塞。"""
        sock = self._sock  # 局部快照：close 并发时以已关闭 fd 失败而非崩溃
        if sock is None:
            raise HerdrSocketError("socket 未连接")
        sock.settimeout(timeout)
        while b"\n" not in self._buf:
            try:
                chunk = sock.recv(4096)
            except socket.timeout as exc:
                raise HerdrSocketIdleTimeout(
                    f"herdr socket 读空闲超时(>{timeout}s)"
                ) from exc
            except OSError as exc:
                raise HerdrSocketError(f"herdr socket 读失败: {exc}") from exc
            if not chunk:
                break  # EOF
            self._buf += chunk
        if b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
        else:
            line, self._buf = self._buf, b""
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
    外部读取一律返回深拷贝，保证线程安全与返回值隔离。"""

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
        self.resync_pending = False

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
            self.resync_pending = False

    def apply_event(self, envelope: dict[str, Any]) -> bool:
        """应用一个事件信封（普通 EventEnvelope）。

        返回 True 表示产生了缓存更新；False 为忽略（未知事件/陈旧/无状态
        事件）。未知 pane 引用会置 resync_pending（客户端据此触发 resync）。
        绝不抛出。
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
            return self._apply_focused(data)
        if event == "pane_output_changed":
            return self._apply_output_revision(data)
        if event == "pane_agent_detected":
            return self._apply_agent_detected(data)
        if event == "layout_updated":
            return self._upsert_layout(data.get("layout"))
        if event in ("workspace_created", "workspace_updated",
                     "workspace_metadata_updated", "workspace_moved",
                     "workspace_reordered", "worktree_created", "worktree_opened"):
            return self._upsert_workspace(data.get("workspace"))
        if event == "workspace_closed":
            return self._remove_workspace(data.get("workspace_id"))
        if event == "workspace_renamed":
            return self._rename_workspace(data)
        if event == "workspace_focused":
            return self._apply_workspace_focused(data.get("workspace_id"))
        if event == "worktree_removed":
            return self._remove_worktree(data.get("workspace_id"))
        if event == "tab_created":
            return self._upsert_tab(data.get("tab"))
        if event == "tab_closed":
            return self._remove_tab(data.get("tab_id"))
        if event == "tab_renamed":
            return self._rename_tab(data)
        if event == "tab_moved":
            return self._move_tab(data)
        if event == "tab_focused":
            return self._apply_tab_focused(data.get("tab_id"))
        # pane.output_matched / pane.scroll_changed / 未知事件
        return False

    def _upsert_pane(self, pane: Any) -> bool:
        slim = self._slim_pane(pane)
        if not slim or not slim.get("pane_id"):
            return False
        pane_id = str(slim["pane_id"])
        current = self._panes.get(pane_id)
        if current is not None and int(slim["revision"]) <= int(current["revision"]):
            return False  # 乱序/重复/相等 revision 一律按重复拒绝
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

    def _apply_focused(self, data: dict[str, Any]) -> bool:
        pane_id = data.get("pane_id")
        if not isinstance(pane_id, str):
            return False
        current = self._panes.get(pane_id)
        if current is None:
            self.resync_pending = True  # 未知 pane：不设 ghost，等 resync
            return False
        self.focused_pane_id = pane_id
        current["focused"] = True
        return True

    def _apply_output_revision(self, data: dict[str, Any]) -> bool:
        pane_id = data.get("pane_id")
        if not isinstance(pane_id, str):
            return False
        current = self._panes.get(pane_id)
        if current is None:
            self.resync_pending = True
            return False
        revision = data.get("revision")
        if not isinstance(revision, int):
            return False
        if revision <= int(current["revision"]):
            return False
        current["revision"] = revision
        return True

    def _apply_agent_detected(self, data: dict[str, Any]) -> bool:
        pane_id = data.get("pane_id")
        if not isinstance(pane_id, str):
            return False
        current = self._panes.get(pane_id)
        if current is None:
            self.resync_pending = True
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
            self.resync_pending = True  # 未知 pane：等 resync，不静默忽略
            return False
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

    def _remove_worktree(self, workspace_id: Any) -> bool:
        if not isinstance(workspace_id, str):
            return False
        current = self._workspaces.get(workspace_id)
        if current is None:
            return False
        current["worktree"] = None
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

    def _move_tab(self, data: dict[str, Any]) -> bool:
        tab_id = data.get("tab_id")
        workspace_id = data.get("workspace_id")
        tabs = data.get("tabs")
        if not isinstance(tab_id, str) or not isinstance(workspace_id, str):
            return False
        current = self._tabs.get(tab_id)
        if current is not None:
            current["workspace_id"] = workspace_id
        if isinstance(tabs, list):  # 完整 tabs 列表（重排结果）
            rebuilt: dict[str, dict[str, Any]] = {}
            for t in tabs:
                if isinstance(t, dict) and t.get("tab_id"):
                    rebuilt[str(t["tab_id"])] = t
            self._tabs = rebuilt
        return True

    def _apply_tab_focused(self, tab_id: Any) -> bool:
        if not isinstance(tab_id, str):
            return False
        self.focused_tab_id = tab_id
        return True

    # -- 读（深拷贝） ---------------------------------------------------------
    def panes(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(list(self._panes.values()))

    def agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._agents)

    def layouts(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(list(self._layouts.values()))

    def workspaces(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(list(self._workspaces.values()))

    def tabs(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(list(self._tabs.values()))

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
                "panes": copy.deepcopy(list(self._panes.values())),
                "agents": copy.deepcopy(self._agents),
                "focused_pane_id": self.focused_pane_id,
                "layouts": copy.deepcopy(list(self._layouts.values())),
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
    """为每个 session 启动一个 reader 线程，连接流程（真实协议契约）：

      1. 独立连接 ping → 校验 protocol（协议门）
      2. 独立连接 session.snapshot → 全量 bootstrap
      3. 专用流连接 events.subscribe → 确认 subscription_started 后才置 subscribed
      4. 独立连接 session.snapshot → 握手后全量 resync（消除回放窗口）
      5. 读循环：阻塞读事件；读空闲超过 health_check_interval 时用独立连接
         ping 探测（成功=健康静默继续读，失败=重建连接）；EOF/错误→重连

    重连后重复 1-5；有上限指数退避。stop() 幂等，关闭 socket 唤醒 reader 后
    join，返回是否全部线程退出。
    """

    def __init__(
        self, sessions: dict[str, str], *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
        health_check_interval: float | None = DEFAULT_HEALTH_CHECK_INTERVAL_S,
        reconnect_base_delay: float = DEFAULT_RECONNECT_BASE_DELAY_S,
        reconnect_max_delay: float = DEFAULT_RECONNECT_MAX_DELAY_S,
        snapshot_timeout: float = DEFAULT_SNAPSHOT_TIMEOUT_S,
        resync_min_interval: float = DEFAULT_RESYNC_MIN_INTERVAL_S,
    ) -> None:
        self._sessions = dict(sessions)
        self._store = StateStore()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._active: set[HerdrSocket] = set()
        self._started = False
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
        self._health_check_interval = health_check_interval
        self._reconnect_base_delay = reconnect_base_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._snapshot_timeout = snapshot_timeout
        self._resync_min_interval = resync_min_interval

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """一次性启动：重复 start 或 stop 后再 start 均拒绝（不支持 restart）。"""
        with self._lock:
            if self._started:
                raise RuntimeError("HerdrStateClient 已启动过，不支持重复 start 或 restart")
            self._started = True
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

    def stop(self, join_timeout: float = 5.0) -> bool:
        """幂等。置停止标志 → close 全部在途 socket（含 snapshot/health 临时
        socket，唤醒阻塞读）→ 按总 deadline join；返回是否全部线程退出
        （不虚报 stopped）。"""
        self._stop.set()
        with self._lock:
            active = list(self._active)
            threads = list(self._threads.values())
        for sock in active:
            sock.close()
        deadline = time.monotonic() + join_timeout
        all_exited = True
        for thread in threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)
            all_exited = all_exited and not thread.is_alive()
        return all_exited

    def _register(self, sock: HerdrSocket) -> None:
        """在途 socket 登记（connect 前调用）。与 stop 原子协作：stop 已置位
        时立即关闭并抛错，保证 stop 后不会再有新 I/O 存活。"""
        with self._lock:
            if self._stop.is_set():
                sock.close()
                raise HerdrSocketError("client 已停止，拒绝新连接")
            self._active.add(sock)

    def _unregister(self, sock: HerdrSocket) -> None:
        with self._lock:
            self._active.discard(sock)

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
        try:
            while not self._stop.is_set():
                lc["state"] = "connecting"
                sock = HerdrSocket(path, connect_timeout=self._connect_timeout)
                self._register(sock)  # connect 前登记，stop 可原子关闭
                try:
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
                    self._unregister(sock)
                    sock.close()
                # 仅在完成稳定 subscribe+resync 后重置退避；connect/ping/
                # snapshot/subscribe/EOF 连续失败共享指数退避与封顶
                if lc.get("stable"):
                    delay = max(0.0, self._reconnect_base_delay)
                    lc["stable"] = False
                if self._stop.is_set():
                    return
                if not self._backoff(delay):
                    return
                delay = min(delay * 2, self._reconnect_max_delay)
        finally:
            if self._stop.is_set():
                lc["state"] = "stopped"

    def _handle_connection(
        self, name: str, path: str, sock: HerdrSocket, state: SessionState,
        lc: dict[str, Any],
    ) -> None:
        # 1. 协议门：独立连接 ping → pong.protocol 必须等于 HERDR_MIN_PROTOCOL
        pong = sock.request("ping", {}, timeout=self._request_timeout, expect_type="pong")
        protocol = pong.get("protocol")
        if protocol != HERDR_MIN_PROTOCOL:
            raise HerdrProtocolMismatchError(
                f"herdr protocol {protocol} 不满足最低要求 {HERDR_MIN_PROTOCOL}"
            )
        # 2. bootstrap：独立连接 session.snapshot 全量覆盖
        lc["state"] = "bootstrap"
        response = self._snapshot_request(path)
        state.apply_snapshot(response)
        lc["bootstrapped"] = True
        lc["connected_at"] = time.monotonic()
        # 3. 订阅专用流：确认 subscription_started 后才发布 subscribed
        sub = HerdrSocket(path, connect_timeout=self._connect_timeout)
        self._register(sub)
        try:
            sub.connect()
            try:
                sub.request(
                    "events.subscribe",
                    {"subscriptions": ALL_SUBSCRIPTIONS},
                    timeout=self._request_timeout,
                    expect_type="subscription_started",
                )
            except HerdrSocketError as exc:
                if "method_not_found" in str(exc) or "unknown_method" in str(exc):
                    lc["state"] = "capability_mismatch"
                    raise HerdrProtocolMismatchError(str(exc)) from exc
                raise
            lc["state"] = "subscribed"
            # 4. 握手后 resync：消除 snapshot→subscribe 窗口的事件回放错位
            self._resync(name, path, state, lc)
            lc["stable"] = True  # 完成稳定 subscribe+resync，退避可重置
            # 5. 读循环：resync_pending 时读超时最多等剩余防抖间隔，
            #    保证未知 pane 即使事件流静默也能及时触发 resync。
            last_resync = time.monotonic()
            while not self._stop.is_set():
                if state.resync_pending and (
                    time.monotonic() - last_resync >= self._resync_min_interval
                ):
                    self._resync(name, path, state, lc)
                    last_resync = time.monotonic()
                    continue
                read_timeout = self._health_check_interval
                if state.resync_pending:
                    remaining = self._resync_min_interval - (
                        time.monotonic() - last_resync
                    )
                    remaining = max(0.0, remaining)
                    if read_timeout is None or remaining < read_timeout:
                        read_timeout = remaining
                try:
                    envelope = sub.read_line(timeout=read_timeout)
                except HerdrSocketIdleTimeout:
                    # 无事件不代表断连；独立 ping 探测 server 健康
                    if self._health_check_interval is not None and not self._health_ok(path):
                        raise HerdrSocketError("health ping 失败，重建连接")
                    continue
                if envelope is None:
                    raise HerdrSocketError("events.subscribe 连接被对端关闭")
                state.apply_event(envelope)
                self._sync_counts(lc, state)
        finally:
            self._unregister(sub)
            sub.close()

    def _snapshot_request(self, path: str) -> dict[str, Any]:
        """独立连接执行 session.snapshot；返回 SessionSnapshot 对象。"""
        sock = HerdrSocket(path, connect_timeout=self._connect_timeout)
        self._register(sock)  # 临时 socket 也登记，stop 可中断在途 snapshot
        try:
            sock.connect()
            response = sock.request(
                "session.snapshot", {}, timeout=self._snapshot_timeout,
                expect_type="session_snapshot",
            )
        finally:
            self._unregister(sock)
            sock.close()
        snap = response.get("snapshot")
        if not isinstance(snap, dict):
            raise HerdrSocketError("session.snapshot 响应缺少 snapshot")
        return snap

    def _resync(self, name: str, path: str, state: SessionState, lc: dict[str, Any]) -> None:
        state.apply_snapshot(self._snapshot_request(path))
        lc["resyncs"] += 1

    def _health_ok(self, path: str) -> bool:
        """独立连接 ping 探测 server 健康。"""
        sock = HerdrSocket(path, connect_timeout=self._connect_timeout)
        self._register(sock)  # 临时 socket 也登记，stop 可中断 health ping
        try:
            sock.connect()
            pong = sock.request(
                "ping", {}, timeout=self._request_timeout, expect_type="pong",
            )
            return pong.get("protocol") == HERDR_MIN_PROTOCOL
        except HerdrSocketError:
            return False
        finally:
            self._unregister(sock)
            sock.close()

    def _sync_counts(self, lc: dict[str, Any], state: SessionState) -> None:
        with self._lock:
            lc["events_seen"] = state.events_seen
            lc["applied_events"] = state.applied_events
            lc["stale_events"] = state.stale_events

    def _backoff(self, delay: float) -> bool:
        """等待 delay 秒；stop 时立即返回 False。"""
        if self._stop.wait(delay):
            return False
        return True
