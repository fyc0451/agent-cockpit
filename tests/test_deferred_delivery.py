"""B0-PREP C1：deferred delivery 核心——事件聚合/延迟投递纯逻辑测试。

不接 server/Hub/Herdr；用 RecordingAdapter 验证状态机、幂等、绑定版本守卫、
聚合窗口与 adapter accept/失败语义。
"""
import threading
import time

import pytest

import deferred_delivery as dd


class RecordingAdapter:
    """记录 deliver() 调用；可配置 accept / 指定调用序号 raise。"""

    def __init__(self, accept: bool = True, raise_on: set[int] | None = None) -> None:
        self.calls: list[tuple[str, int, list[str]]] = []
        self.accept = accept
        self.raise_on = raise_on or set()

    def deliver(self, scope: str, binding_version: int, events: list[dd.DeferredEvent]) -> bool:
        self.calls.append((scope, binding_version, [e.event_id for e in events]))
        if len(self.calls) - 1 in self.raise_on:
            raise RuntimeError("adapter boom")
        return self.accept


def ev(eid: str, scope: str = "s1", binding_version: int = 1,
       summary: dict | None = None, ts: float = 0.0) -> dd.DeferredEvent:
    return dd.DeferredEvent(
        event_id=eid, scope=scope, binding_version=binding_version,
        summary=summary or {"t": eid}, created_ts=ts,
    )


def _ready(core, scope="s1"):
    core.set_active_binding(scope, 1)
    core.set_target_status(scope, "ready")


# ── 状态机：working/typing/unavailable 延迟，idle/done/ready 可投 ──────────

def test_working_status_does_not_deliver():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "working")
    r = core.ingest(ev("e1"))
    assert r["queued"] is True
    assert adapter.calls == []                      # working → 不调 prompt
    assert [e.event_id for e in core.pending("s1")] == ["e1"]


def test_typing_and_unavailable_defer_then_eligible_delivers():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    for busy in ("typing", "unavailable"):
        core.set_target_status("s1", busy)
        core.ingest(ev("e-" + busy))
        assert adapter.calls == []                   # busy 全部延迟
    core.set_target_status("s1", "idle")            # 恢复到 eligible
    assert len(adapter.calls) == 1
    assert set(adapter.calls[0][2]) == {"e-typing", "e-unavailable"}


def test_ingest_while_eligible_delivers_immediately():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    _ready(core)
    r = core.ingest(ev("e1"))
    assert r["delivered"] is True
    assert len(adapter.calls) == 1 and adapter.calls[0][2] == ["e1"]


# ── ready 一次补投 + 幂等 ────────────────────────────────────────────────

def test_ready_catch_up_delivers_once_then_idempotent():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "working")
    core.ingest(ev("e1"))
    r = core.set_target_status("s1", "ready")
    assert r["delivered"] is True
    assert len(adapter.calls) == 1
    # 重复 ready 不重复投递
    r2 = core.set_target_status("s1", "ready")
    assert r2["delivered"] is False
    assert len(adapter.calls) == 1


def test_status_flip_does_not_redeliver():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    _ready(core)
    core.ingest(ev("e1"))                            # 立即投递
    assert len(adapter.calls) == 1
    core.set_target_status("s1", "working")
    core.set_target_status("s1", "ready")           # 翻转回来
    assert len(adapter.calls) == 1                   # pending 空，不重复


def test_duplicate_event_id_dedups():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "working")
    core.ingest(ev("e1", ts=1.0))
    r = core.ingest(ev("e1", ts=2.0))                # 重复 event_id
    assert r["duplicate"] is True
    assert len(core.pending("s1")) == 1
    core.set_target_status("s1", "ready")
    assert len(adapter.calls) == 1 and adapter.calls[0][2] == ["e1"]


def test_duplicate_keeps_latest_summary():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "working")
    core.ingest(ev("e1", summary={"v": 1}, ts=1.0))
    core.ingest(ev("e1", summary={"v": 2}, ts=2.0))  # 更新摘要
    pending = core.pending("s1")
    assert pending[0].summary == {"v": 2}


def test_already_delivered_event_not_redelivered():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    _ready(core)
    core.ingest(ev("e1"))                            # 投递
    r = core.ingest(ev("e1"))                        # 再来同 id
    assert r["duplicate"] is True
    assert len(adapter.calls) == 1                   # 不重复


# ── 绑定版本守卫 ─────────────────────────────────────────────────────────

def test_stale_binding_version_not_delivered():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 2)
    core.set_target_status("s1", "ready")
    r = core.ingest(ev("e1", binding_version=1))     # 旧版本
    assert r["stale"] is True
    assert adapter.calls == []
    assert core.pending("s1") == []


def test_binding_switch_clears_old_pending():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "working")
    core.ingest(ev("e1", binding_version=1))
    core.set_active_binding("s1", 2)                 # 切换
    assert core.pending("s1") == []                  # 旧 v1 pending 清空
    core.set_target_status("s1", "ready")            # eligible
    assert adapter.calls == []                        # 无可投
    core.ingest(ev("e2", binding_version=2))         # 新版本立即投
    assert len(adapter.calls) == 1 and adapter.calls[0][2] == ["e2"]


def test_after_switch_old_binding_events_not_delivered():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 2)
    core.set_target_status("s1", "ready")
    r = core.ingest(ev("e_old", binding_version=1))  # 切换后旧 binding 事件
    assert r["stale"] is True
    assert adapter.calls == []


# ── adapter 失败保最新 pending + 重试 ────────────────────────────────────

def test_adapter_reject_keeps_pending_and_retries():
    adapter = RecordingAdapter(accept=False)
    core = dd.DeferredDeliveryCore(adapter)
    _ready(core)
    r = core.ingest(ev("e1"))
    assert r["delivered"] is False                    # adapter 拒绝
    assert [e.event_id for e in core.pending("s1")] == ["e1"]
    assert len(adapter.calls) == 1
    r2 = core.flush("s1")                             # 重试
    assert r2["delivered"] is False
    assert len(adapter.calls) == 2


def test_adapter_raise_keeps_pending():
    adapter = RecordingAdapter(raise_on={0})
    core = dd.DeferredDeliveryCore(adapter)
    _ready(core)
    core.ingest(ev("e1"))
    assert [e.event_id for e in core.pending("s1")] == ["e1"]  # raise 后保留
    assert len(adapter.calls) == 1


def test_adapter_failure_then_success_delivers_and_clears():
    adapter = RecordingAdapter(accept=False)
    core = dd.DeferredDeliveryCore(adapter)
    _ready(core)
    core.ingest(ev("e1"))                             # 失败
    adapter.accept = True
    r = core.flush("s1")                              # 重试成功
    assert r["delivered"] is True
    assert core.pending("s1") == []                   # 已投递清空
    assert core.state("s1")["delivered_count"] == 1


# ── 聚合窗口 ─────────────────────────────────────────────────────────────

def test_aggregation_window_merges_into_one_delivery():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "working")
    core.ingest(ev("e1", ts=1))
    core.ingest(ev("e2", ts=2))
    core.ingest(ev("e3", ts=3))
    core.set_target_status("s1", "ready")
    assert len(adapter.calls) == 1                    # 合并为一次投递
    assert set(adapter.calls[0][2]) == {"e1", "e2", "e3"}
    assert adapter.calls[0][1] == 1                   # binding_version 透传


# ── 并发：单写者绑定 + 安全聚合 ──────────────────────────────────────────

def test_concurrent_ingest_no_loss_no_dup():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "working")

    def worker(n):
        for i in range(50):
            core.ingest(ev(f"t{n}-{i}"))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(core.pending("s1")) == 200             # 无丢失/重复
    core.set_target_status("s1", "ready")
    assert len(adapter.calls) == 1
    assert len(adapter.calls[0][2]) == 200


# ── 诊断 ────────────────────────────────────────────────────────────────

def test_state_reports_status_binding_and_counts():
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 3)
    core.set_target_status("s1", "working")
    core.ingest(ev("e1", binding_version=3))
    st = core.state("s1")
    assert st["active_binding_version"] == 3
    assert st["target_status"] == "working"
    assert st["pending_count"] == 1
    assert st["delivered_count"] == 0
    assert st["eligible"] is False


# ── #1706 复核：同版本重绑 / 切换移交 / 锁外 adapter / 多 scope 隔离 ─────

def test_same_version_rebind_keeps_pending():
    """同版本重绑（幂等声明，如重启后恢复）不清 pending。"""
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 2)
    core.set_target_status("s1", "working")
    core.ingest(ev("e1", binding_version=2))
    r = core.set_active_binding("s1", 2)  # 同版本重绑
    assert r["rebound_same_version"] is True
    assert r["cleared_pending"] == 0
    assert [e.event_id for e in core.pending("s1")] == ["e1"]  # 在途保留
    assert adapter.calls == []            # 不触发投递
    core.set_target_status("s1", "ready")
    assert len(adapter.calls) == 1 and adapter.calls[0][2] == ["e1"]


def test_same_version_rebind_keeps_delivered_dedup():
    """同版本重绑后 delivered 去重记录保留（不重复投递）。"""
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "ready")
    core.ingest(ev("e1"))                 # 已投递
    core.set_active_binding("s1", 1)      # 同版本重绑
    r = core.ingest(ev("e1"))             # 再来同 id
    assert r["duplicate"] is True
    assert len(adapter.calls) == 1


def test_binding_switch_returns_cleared_events_for_handoff():
    """切 binding 时旧在途事件本体返回给调用方移交（不静默丢弃）。"""
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "working")
    core.ingest(ev("old1", binding_version=1, ts=1.0))
    core.ingest(ev("old2", binding_version=1, ts=2.0))
    r = core.set_active_binding("s1", 2)
    assert r["cleared_pending"] == 2
    ids = sorted(e.event_id for e in r["cleared_pending_events"])
    assert ids == ["old1", "old2"]        # 事件本体可移交（排空/转投）
    assert core.pending("s1") == []       # 核心内已清，但调用方持有移交清单


def test_adapter_called_outside_lock_no_deadlock():
    """adapter 在核心锁外调用：内部再调核心方法不死锁。"""
    seen = {}

    class ReentrantAdapter:
        def deliver(self, scope, version, events):
            seen["pending_inside"] = [e.event_id for e in core.pending(scope)]
            return True

    core = dd.DeferredDeliveryCore(ReentrantAdapter())
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "ready")
    core.ingest(ev("e1"))
    assert seen["pending_inside"] == ["e1"]  # 若 adapter 在锁内调用会死锁


def test_scope_isolation_under_blocking_adapter():
    """s1 的阻塞 adapter 不阻塞 s2 的 ingest/状态操作（多 scope 不互阻塞）。"""
    released = threading.Event()

    class BlockingAdapter:
        def deliver(self, scope, version, events):
            if scope == "s1":
                released.wait(timeout=5)
            return True

    core = dd.DeferredDeliveryCore(BlockingAdapter())
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "ready")
    core.ingest(ev("e1"))                 # s1 adapter 阻塞中

    import time
    t0 = time.monotonic()
    core.set_active_binding("s2", 1)
    core.set_target_status("s2", "working")
    r = core.ingest(ev("e2", scope="s2"))
    elapsed = time.monotonic() - t0
    released.set()
    assert r["queued"] is True
    assert elapsed < 0.5                  # 不等待 s1 adapter


# ── R3（#1759）：真锁外 adapter / generation 收尾 / 防御性复制 ──────────

class BlockingAdapter:
    """可阻塞的 adapter：release 事件控制 deliver 返回；记录调用。"""

    def __init__(self, accept=True):
        self.release = threading.Event()
        self.calls = []
        self.accept = accept
        self.deliver_started = threading.Event()

    def deliver(self, scope, binding_version, events):
        self.calls.append((scope, binding_version, [e.event_id for e in events]))
        self.deliver_started.set()
        self.release.wait(timeout=10)
        return self.accept


def ev(eid, scope="s1", binding_version=1, summary=None, ts=0.0):
    return dd.DeferredEvent(
        event_id=eid, scope=scope, binding_version=binding_version,
        summary=summary or {"t": eid}, created_ts=ts,
    )


def test_adapter_truly_outside_lock_other_thread_not_blocked():
    """真锁外证明：adapter 阻塞期间，**另一线程**获取核心锁立即成功
    （若外层持锁，其他线程会被 RLock 阻塞——RLock 重入不是证明）。"""
    adapter = BlockingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "ready")

    holder_blocked = threading.Event()
    other_returned = threading.Event()
    other_elapsed = []

    def holder():
        holder_blocked.set()
        core.ingest(ev("e1"))  # adapter 阻塞中

    def other():
        holder_blocked.wait(timeout=5)
        adapter.deliver_started.wait(timeout=5)  # 确认 deliver 正在执行
        t0 = time.monotonic()
        core.pending("s1")   # 需获取 _lock
        core.state("s1")
        other_elapsed.append(time.monotonic() - t0)
        other_returned.set()

    t_holder = threading.Thread(target=holder)
    t_other = threading.Thread(target=other)
    t_other.start()
    t_holder.start()
    other_returned.wait(timeout=5)
    adapter.release.set()
    t_holder.join(timeout=5)
    t_other.join(timeout=5)
    assert other_returned.is_set()
    assert other_elapsed and other_elapsed[0] < 0.5  # 锁外：其他线程不阻塞


def test_blocking_adapter_does_not_block_other_scope_timing_immediate():
    """s1 adapter 阻塞（deliver 中）时，s2 的 set/ingest <0.5s 返回——证明
    核心锁在 s1 deliver 期间已释放（立即计时，不先等 5s）。s2 置 working 以
    不触发自身 deliver（避免与 s1 共享阻塞 adapter 的干扰），纯粹检验锁隔离。"""
    adapter = BlockingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_active_binding("s2", 1)
    core.set_target_status("s1", "ready")
    core.set_target_status("s2", "working")  # s2 非 eligible：ingest 只排队不投递

    holder_blocked = threading.Event()

    def holder():
        holder_blocked.set()
        core.ingest(ev("e1", scope="s1"))

    t = threading.Thread(target=holder)
    t.start()
    holder_blocked.wait(timeout=5)
    adapter.deliver_started.wait(timeout=5)
    t0 = time.monotonic()  # 立即计时（不先等）
    r_set = core.set_target_status("s2", "working")
    r_ing = core.ingest(ev("e2", scope="s2", binding_version=1))
    elapsed = time.monotonic() - t0
    adapter.release.set()
    t.join(timeout=5)
    assert elapsed < 0.5
    assert r_ing["queued"] is True
    assert r_ing["delivered"] is False  # s2 working：排队未投递


def test_switch_during_deliver_handoff_no_loss_no_dup():
    """同 scope switch 发生在 deliver 阻塞期间：释放后 accepted → 已投旧
    target + handoff 转投新 target；事件零丢零重。"""
    adapter = BlockingAdapter(accept=True)
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "ready")

    holder_blocked = threading.Event()

    def holder():
        holder_blocked.set()
        core.ingest(ev("e1"))

    t = threading.Thread(target=holder)
    t.start()
    holder_blocked.wait(timeout=5)
    adapter.deliver_started.wait(timeout=5)
    # switch 发生在 deliver 阻塞期间（新 generation）
    core.set_active_binding("s1", 2)
    adapter.release.set()
    t.join(timeout=5)
    # accepted（旧 target 已投）+ handoff：事件转投新 target
    late = core.late_results("s1")
    assert len(late) == 1
    assert late[0]["category"] == "delivered_stale_target"
    assert late[0]["handed_off"] == ["e1"]
    # handoff 后事件回到 pending（等 eligible 重投新 target）
    core.set_target_status("s1", "ready")
    assert core.pending("s1") == []  # 重投成功后清空
    assert core.state("s1")["delivered_count"] == 1
    # 零重：同 event_id 不再重复投递
    adapter2_calls = adapter.calls
    assert sum(c[2].count("e1") for c in adapter2_calls) == 2  # 旧+新各一次


def test_rejected_during_switch_requeues_into_new_generation():
    """deliver 拒绝 + generation 已切换：事件重排入新 generation，零丢。"""
    adapter = BlockingAdapter(accept=False)
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "ready")

    holder_blocked = threading.Event()

    def holder():
        holder_blocked.set()
        core.ingest(ev("e1"))

    t = threading.Thread(target=holder)
    t.start()
    holder_blocked.wait(timeout=5)
    adapter.deliver_started.wait(timeout=5)
    core.set_active_binding("s1", 2)
    adapter.release.set()
    t.join(timeout=5)
    late = core.late_results("s1")
    assert len(late) == 1
    assert late[0]["category"] == "requeued"
    # 事件回到 pending（新 generation）
    pend = core.pending("s1")
    assert len(pend) == 1 and pend[0].event_id == "e1"
    assert pend[0].binding_version == 2  # 归入新 generation


def test_defensive_copy_summary_isolation():
    """DeferredEvent.summary 防御性复制：调用方改传入/查询副本不影响核心。"""
    adapter = RecordingAdapter()
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "working")
    mutable = {"v": 1, "nested": {"x": 1}}
    core.ingest(ev("e1", summary=mutable))
    mutable["v"] = 999
    mutable["nested"]["x"] = 999
    stored = core.pending("s1")[0]
    assert stored.summary == {"v": 1, "nested": {"x": 1}}
    # 改查询副本不影响核心
    stored.summary["v"] = 888
    assert core.pending("s1")[0].summary["v"] == 1


def test_handoff_zero_loss_zero_dup_new_target_delivers_once():
    """handoff 零丢零重：switch 发生在 deliver 阻塞期间，释放后事件转投新
    target；新 target 恰好投递一次（不重复），旧+新各一次共两次。"""
    adapter = BlockingAdapter(accept=True)
    core = dd.DeferredDeliveryCore(adapter)
    core.set_active_binding("s1", 1)
    core.set_target_status("s1", "ready")

    holder_blocked = threading.Event()

    def holder():
        holder_blocked.set()
        core.ingest(ev("e1"))

    t = threading.Thread(target=holder)
    t.start()
    holder_blocked.wait(timeout=5)
    adapter.deliver_started.wait(timeout=5)
    core.set_active_binding("s1", 2)   # switch 发生在 deliver 阻塞期间
    adapter.release.set()             # 释放 → handoff 转投新 generation
    t.join(timeout=5)
    # handoff 后 e1 回到新 generation pending
    assert [e.event_id for e in core.pending("s1")] == ["e1"]
    # 新 target 投递一次（零重：同 generation 不重复）
    core.set_target_status("s1", "ready")
    core.flush("s1")
    assert core.pending("s1") == []
    assert core.state("s1")["delivered_count"] == 1
    # e1 共投递两次：旧 target（holder）+ 新 target（flush），各一次
    assert sum(c[2].count("e1") for c in adapter.calls) == 2
