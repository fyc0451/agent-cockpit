"""deferred_delivery.py — B0-PREP C1 事件聚合 / 延迟投递核心（纯逻辑）。

为 agent 协调提供"目标忙则延迟、闲则补投、按 binding 版本守卫、幂等不重复
prompt"的投递核心。本模块刻意不接 server/Hub poller/Herdr/产品能力：只定义
最小适配接口（DeliveryAdapter），由未来接线层实现投递副作用；持久层（Qoder
负责）通过调用本核心的纯方法适配，不与本文件互改。

核心不变式：
- 只有目标处于 eligible（idle/done/ready）且事件 binding_version == 当前 active
  binding 时才尝试投递；working/typing/unavailable 一律延迟。
- 只有 DeliveryAdapter 明确 accept（返回 True）才算 delivered；拒绝或抛异常都
  保留最新 pending，等下一次 eligible/flush 重试。
- 同 event_id 在同一 active binding 内去重；重复 ready / 状态翻转不重复 prompt。
- set_active_binding 切换版本后清空旧 pending 与 delivered 记录（新 binding 重新
  开始）；旧版本事件视为 stale 不投递，切换后旧 binding 不收新事件。
- 同一聚合窗口内多个事件合并为一次 deliver 调用。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Protocol


ELIGIBLE_STATES = frozenset({"idle", "done", "ready"})
DEFERRED_STATES = frozenset({"working", "typing", "unavailable"})


@dataclass(frozen=True)
class DeferredEvent:
    """一个待聚合/投递的事件。event_id 为稳定去重键。"""

    event_id: str
    scope: str
    binding_version: int
    summary: dict[str, Any]
    created_ts: float = 0.0


class DeliveryAdapter(Protocol):
    """投递副作用的最小接口。返回 True 表示已被接受（delivered）。

    实现可代表"向某 agent pane 发 prompt"等；本核心不假设其内部行为，只据返回
    值与是否抛异常判定 delivered。deliver 在本核心锁外调用，实现不应阻塞回调本
    核心的长操作。
    """

    def deliver(
        self, scope: str, binding_version: int, events: list[DeferredEvent],
    ) -> bool: ...


@dataclass
class _ScopeState:
    active_binding_version: int | None = None
    target_status: str | None = None
    pending: dict[str, DeferredEvent] = field(default_factory=dict)
    delivered_ids: set[str] = field(default_factory=set)
    delivered_count: int = 0
    in_flight: bool = False


class DeferredDeliveryCore:
    """线程安全的延迟投递核心。每个 scope 独立状态，统一由 _lock 串行化
    （单写者语义）：同一 scope 同一时刻最多一次在途投递。"""

    def __init__(self, adapter: DeliveryAdapter) -> None:
        self._adapter = adapter
        self._lock = threading.RLock()
        self._states: dict[str, _ScopeState] = {}

    # -- 内部 ---------------------------------------------------------------
    def _state(self, scope: str) -> _ScopeState:
        st = self._states.get(scope)
        if st is None:
            st = _ScopeState()
            self._states[scope] = st
        return st

    @staticmethod
    def _eligible(st: _ScopeState) -> bool:
        return st.target_status in ELIGIBLE_STATES

    def _try_deliver(self, st: _ScopeState, scope: str) -> bool:
        """eligible 且有 pending 且不在途时投递一次。返回是否被 accept。"""
        if st.active_binding_version is None or not self._eligible(st):
            return False
        if not st.pending or st.in_flight:
            return False
        st.in_flight = True
        active = st.active_binding_version
        events = list(st.pending.values())
        eids = [e.event_id for e in events]
        # 锁外调用 adapter：避免外部副作用阻塞其它 scope 或与回调死锁。
        accepted = False
        try:
            accepted = bool(self._adapter.deliver(scope, active, events))
        except Exception:
            accepted = False
        with self._lock:
            st.in_flight = False
            if accepted:
                # 只清掉本次快照的 id；投递期间新并入的事件保留为下一轮 pending。
                for eid in eids:
                    st.pending.pop(eid, None)
                    st.delivered_ids.add(eid)
                st.delivered_count += len(eids)
        return accepted

    # -- 公共 API -----------------------------------------------------------
    def set_active_binding(self, scope: str, binding_version: int) -> dict[str, Any]:
        """单写者选定当前 active binding。

        - 同版本重绑（幂等声明，如服务重启后恢复）：不清 pending/delivered，
          在途消息与去重记录原样保留。
        - 版本切换 = 新一代：旧 pending 事件**返回给调用方移交**（不静默丢弃，
          调用方负责排空/转投），旧 delivered 记录清空；此后旧版本事件视为
          stale。
        """
        with self._lock:
            st = self._state(scope)
            if st.active_binding_version == binding_version:
                return {
                    "binding_version": binding_version,
                    "cleared_pending": 0,
                    "cleared_pending_events": [],
                    "delivered": False,
                    "rebound_same_version": True,
                }
            st.active_binding_version = binding_version
            cleared_events = list(st.pending.values())
            st.pending.clear()
            st.delivered_ids.clear()
            st.delivered_count = 0
            delivered = self._try_deliver(st, scope)
        return {
            "binding_version": binding_version,
            "cleared_pending": len(cleared_events),
            "cleared_pending_events": cleared_events,
            "delivered": delivered,
        }

    def set_target_status(self, scope: str, status: str) -> dict[str, Any]:
        """更新目标可用状态。变为 eligible 时触发一次补投。"""
        with self._lock:
            st = self._state(scope)
            st.target_status = status
            delivered = self._try_deliver(st, scope)
        return {"status": status, "delivered": delivered}

    def ingest(self, event: DeferredEvent) -> dict[str, Any]:
        """并入一个事件。返回 queued/duplicate/stale/delivered 标志。

        - binding_version != active（含 active 未设）→ stale，丢弃不投。
        - event_id 已 delivered → duplicate，不再投。
        - event_id 已在 pending → duplicate，按 created_ts 更新到最新摘要。
        - 否则 queued，并入当前聚合窗口。
        并入后若目标 eligible，立即尝试投递。
        """
        with self._lock:
            st = self._state(event.scope)
            if (
                st.active_binding_version is None
                or event.binding_version != st.active_binding_version
            ):
                return {"queued": False, "duplicate": False, "stale": True, "delivered": False}
            if event.event_id in st.delivered_ids:
                return {"queued": False, "duplicate": True, "stale": False, "delivered": False}
            existing = st.pending.get(event.event_id)
            if existing is not None:
                # 同窗口重复：保留最新（created_ts 更大者）摘要。
                if event.created_ts >= existing.created_ts:
                    st.pending[event.event_id] = event
                return {"queued": False, "duplicate": True, "stale": False, "delivered": False}
            st.pending[event.event_id] = event
            delivered = self._try_deliver(st, event.scope)
        return {"queued": True, "duplicate": False, "stale": False, "delivered": delivered}

    def flush(self, scope: str) -> dict[str, Any]:
        """显式触发一次投递尝试（用于 adapter 失败后重试或外部调度）。"""
        with self._lock:
            st = self._state(scope)
            delivered = self._try_deliver(st, scope)
        return {"delivered": delivered}

    def pending(self, scope: str) -> list[DeferredEvent]:
        with self._lock:
            st = self._states.get(scope)
            return list(st.pending.values()) if st is not None else []

    def state(self, scope: str) -> dict[str, Any]:
        with self._lock:
            st = self._states.get(scope)
            if st is None:
                return {
                    "active_binding_version": None, "target_status": None,
                    "pending_count": 0, "delivered_count": 0,
                    "eligible": False, "in_flight": False,
                }
            return {
                "active_binding_version": st.active_binding_version,
                "target_status": st.target_status,
                "pending_count": len(st.pending),
                "delivered_count": st.delivered_count,
                "eligible": self._eligible(st),
                "in_flight": st.in_flight,
            }
