"""deferred_delivery.py — B0-PREP C1 事件聚合 / 延迟投递核心（纯逻辑）。

为 agent 协调提供"目标忙则延迟、闲则补投、按 binding 版本守卫、幂等不重复
prompt"的投递核心。本模块刻意不接 server/Hub poller/Herdr/产品能力：只定义
最小适配接口（DeliveryAdapter），由未来接线层实现投递副作用。

R3（#1759/#1757）修订：
- 真锁外 adapter：公共方法锁内仅做快照/校验/标 inflight，释放锁后才调用
  adapter.deliver，返回后再入锁 CAS 收尾；任何调用栈都不得外层持锁调用
  adapter（RLock 重入不是锁外证明）。
- inflight 记录携带 binding_version/generation + 事件快照：切 binding 与
  adapter 返回交错时按 generation 归属收尾——三类结果（已投旧 target /
  handoff 转投新 target / 未接受重排）保证事件零丢零重。
- DeferredEvent.summary 传 adapter 与 pending 查询用防御性深拷贝，禁止
  越权改核心。

核心不变式：
- 只有目标处于 eligible（idle/done/ready）且事件 binding_version == 当前
  active binding 时才尝试投递；working/typing/unavailable 一律延迟。
- 只有 DeliveryAdapter 明确 accept（返回 True）才算 delivered；拒绝或抛异常
  都保留最新 pending，等下一次 eligible/flush 重试。
- 同 event_id 在同一 generation 内去重；重复 ready / 状态翻转不重复 prompt。
- set_active_binding 切换版本 = 新 generation：旧 pending 事件移交调用方
  （不静默丢弃），delivered 记录清空；在途投递（旧 generation）返回后按
  handoff 规则处理。
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Protocol


ELIGIBLE_STATES = frozenset({"idle", "done", "ready"})
DEFERRED_STATES = frozenset({"working", "typing", "unavailable"})


@dataclass(frozen=True)
class DeferredEvent:
    """一个待聚合/投递的事件。event_id 为稳定去重键；summary 为可变 dict，
    核心在传出/传出查询时深拷贝，调用方不得共享可变引用。"""

    event_id: str
    scope: str
    binding_version: int
    summary: dict[str, Any]
    created_ts: float = 0.0


class DeliveryAdapter(Protocol):
    """投递副作用的最小接口。返回 True 表示已被接受（delivered）。

    deliver 在本核心**锁外**调用（独立线程亦可安全），实现可阻塞而不影响
    其他 scope 的 set/ingest。实现不应回调本核心的公共方法以外的内部状态。
    """

    def deliver(
        self, scope: str, binding_version: int, events: list[DeferredEvent],
    ) -> bool: ...


@dataclass
class _InFlight:
    """一次在途投递的不可变快照：generation + binding_version + 事件。"""

    binding_version: int
    generation: int
    events: list[DeferredEvent]  # 深拷贝快照，与 pending 隔离
    started_ts: float


@dataclass
class _ScopeState:
    active_binding_version: int | None = None
    target_status: str | None = None
    pending: dict[str, DeferredEvent] = field(default_factory=dict)
    delivered_ids: set[str] = field(default_factory=set)  # 当前 generation
    delivered_count: int = 0
    inflight: _InFlight | None = None
    generation: int = 0  # 每次 binding 版本切换 +1
    # late-result handoff 记录（诊断/审计）：generation 交错收尾的三分类。
    late_results: list[dict[str, Any]] = field(default_factory=list)


class DeferredDeliveryCore:
    """线程安全的延迟投递核心。每个 scope 独立状态；adapter 在锁外调用，
    同一 scope 同一时刻最多一次在途投递（inflight 标记）。"""

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

    @staticmethod
    def _snapshot_events(st: _ScopeState) -> list[DeferredEvent]:
        """深拷贝 pending 快照（传 adapter 用，禁止共享可变引用）。"""
        return [copy.deepcopy(e) for e in st.pending.values()]

    def _begin_deliver(self, st: _ScopeState, scope: str) -> _InFlight | None:
        """锁内调用：eligible 且有 pending 且不在途时建立 inflight 快照。"""
        if st.active_binding_version is None or not self._eligible(st):
            return None
        if not st.pending or st.inflight is not None:
            return None
        inflight = _InFlight(
            binding_version=st.active_binding_version,
            generation=st.generation,
            events=self._snapshot_events(st),
            started_ts=time.monotonic(),
        )
        st.inflight = inflight
        return inflight

    def _finish_deliver(
        self, scope: str, inflight: _InFlight, accepted: bool,
    ) -> dict[str, Any]:
        """锁内调用：按 generation 归属 CAS 收尾（三分类，零丢零重）。

        - accepted 且 generation 未变 → delivered（同 generation ledger）。
        - accepted 但 generation 已切换 → 已投旧 target；事件 handoff 转投
          新 target（除非新 generation 已 delivered，防重复）。
        - rejected/异常 → 未接受重排：回 pending（除非新 generation 已
          delivered，防重复投递）。
        """
        st = self._state(scope)
        if st.inflight is inflight:
            st.inflight = None
        result: dict[str, Any] = {"category": None, "handed_off": []}
        if accepted:
            if st.generation == inflight.generation:
                for e in inflight.events:
                    st.pending.pop(e.event_id, None)
                    st.delivered_ids.add(e.event_id)
                st.delivered_count += len(inflight.events)
                result["category"] = "delivered"
            else:
                # 已投旧 target：新 target 也需看到（零丢）；新 generation
                # 已 delivered 的 event_id 跳过（零重）。
                result["category"] = "delivered_stale_target"
                for e in inflight.events:
                    if e.event_id in st.delivered_ids:
                        continue
                    new_ev = replace(e, binding_version=st.active_binding_version)
                    st.pending.setdefault(e.event_id, new_ev)
                    result["handed_off"].append(e.event_id)
                st.late_results.append({
                    "category": "delivered_stale_target",
                    "generation": inflight.generation,
                    "binding_version": inflight.binding_version,
                    "event_ids": [e.event_id for e in inflight.events],
                    "handed_off": list(result["handed_off"]),
                    "ts": time.time(),
                })
        else:
            # 未接受重排：回 pending 等重试；generation 已切换则归入新
            # generation（binding_version 更新），已 delivered 的跳过（零重）。
            result["category"] = "requeued"
            for e in inflight.events:
                if e.event_id in st.delivered_ids:
                    continue
                cur = replace(e, binding_version=st.active_binding_version)
                st.pending.setdefault(e.event_id, cur)
            st.late_results.append({
                "category": "requeued",
                "generation": inflight.generation,
                "binding_version": inflight.binding_version,
                "event_ids": [e.event_id for e in inflight.events],
                "ts": time.time(),
            })
        return result

    def _deliver_outside_lock(
        self, scope: str, inflight: _InFlight,
    ) -> dict[str, Any]:
        """锁外调用 adapter，返回后入锁 CAS 收尾。

        adapter 只收到对 inflight.events 逐 event copy.deepcopy 后的全新
        list——恶意/有缺陷 adapter 对入参的 append/clear/reorder/summary
        变异都无法触及内部 inflight.events（pristine，只供收尾）；核心状态
        守恒。
        """
        adapter_events = [copy.deepcopy(e) for e in inflight.events]
        accepted = False
        try:
            accepted = bool(
                self._adapter.deliver(
                    scope, inflight.binding_version, adapter_events,
                )
            )
        except Exception:
            accepted = False
        with self._lock:
            return self._finish_deliver(scope, inflight, accepted)

    # -- 公共 API -----------------------------------------------------------
    def set_active_binding(self, scope: str, binding_version: int) -> dict[str, Any]:
        """单写者选定当前 active binding。

        - 同版本重绑（幂等声明，如服务重启后恢复）：不清 pending/delivered，
          在途消息与去重记录原样保留。
        - 版本切换 = 新 generation：旧 pending 事件移交调用方（不静默丢弃），
          delivered 记录清空；在途投递（旧 generation）返回后按 handoff 规则
          收尾。切换后若目标 eligible 且无 inflight，立即尝试投递新 pending。
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
            st.generation += 1
            cleared_events = [copy.deepcopy(e) for e in st.pending.values()]
            st.pending.clear()
            st.delivered_ids.clear()
            st.delivered_count = 0
            inflight = self._begin_deliver(st, scope)
        if inflight is None:
            return {
                "binding_version": binding_version,
                "cleared_pending": len(cleared_events),
                "cleared_pending_events": cleared_events,
                "delivered": False,
            }
        result = self._deliver_outside_lock(scope, inflight)
        return {
            "binding_version": binding_version,
            "cleared_pending": len(cleared_events),
            "cleared_pending_events": cleared_events,
            "delivered": result["category"] == "delivered",
        }

    def set_target_status(self, scope: str, status: str) -> dict[str, Any]:
        """更新目标可用状态。变为 eligible 时触发一次补投（锁外 deliver）。"""
        with self._lock:
            st = self._state(scope)
            st.target_status = status
            inflight = self._begin_deliver(st, scope)
        if inflight is None:
            return {"status": status, "delivered": False}
        result = self._deliver_outside_lock(scope, inflight)
        return {"status": status, "delivered": result["category"] == "delivered"}

    def ingest(self, event: DeferredEvent) -> dict[str, Any]:
        """并入一个事件。返回 queued/duplicate/stale/delivered 标志。

        - binding_version != active（含 active 未设）→ stale，丢弃不投。
        - event_id 已 delivered → duplicate，不再投。
        - event_id 已在 pending → duplicate，按 created_ts 更新到最新摘要。
        - 否则 queued，并入当前聚合窗口。
        并入后若目标 eligible 且无 inflight，锁外立即投递。
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
                # 同窗口重复：保留最新（created_ts 更大者）摘要（深拷贝隔离）。
                if event.created_ts >= existing.created_ts:
                    st.pending[event.event_id] = copy.deepcopy(event)
                return {"queued": False, "duplicate": True, "stale": False, "delivered": False}
            st.pending[event.event_id] = copy.deepcopy(event)
            inflight = self._begin_deliver(st, event.scope)
        if inflight is None:
            return {"queued": True, "duplicate": False, "stale": False, "delivered": False}
        result = self._deliver_outside_lock(event.scope, inflight)
        return {
            "queued": True, "duplicate": False, "stale": False,
            "delivered": result["category"] == "delivered",
        }

    def flush(self, scope: str) -> dict[str, Any]:
        """显式触发一次投递尝试（adapter 失败后重试/外部调度），锁外 deliver。"""
        with self._lock:
            st = self._state(scope)
            inflight = self._begin_deliver(st, scope)
        if inflight is None:
            return {"delivered": False}
        result = self._deliver_outside_lock(scope, inflight)
        return {"delivered": result["category"] == "delivered"}

    def pending(self, scope: str) -> list[DeferredEvent]:
        """返回 pending 事件的深拷贝（调用方不得共享可变引用）。"""
        with self._lock:
            st = self._states.get(scope)
            if st is None:
                return []
            return [copy.deepcopy(e) for e in st.pending.values()]

    def late_results(self, scope: str) -> list[dict[str, Any]]:
        """返回 generation 交错收尾记录（诊断/审计用，深拷贝）。"""
        with self._lock:
            st = self._states.get(scope)
            if st is None:
                return []
            return copy.deepcopy(st.late_results)

    def state(self, scope: str) -> dict[str, Any]:
        with self._lock:
            st = self._states.get(scope)
            if st is None:
                return {
                    "active_binding_version": None, "target_status": None,
                    "pending_count": 0, "delivered_count": 0,
                    "eligible": False, "in_flight": False,
                    "generation": 0,
                }
            return {
                "active_binding_version": st.active_binding_version,
                "target_status": st.target_status,
                "pending_count": len(st.pending),
                "delivered_count": st.delivered_count,
                "eligible": self._eligible(st),
                "in_flight": st.inflight is not None,
                "generation": st.generation,
            }
