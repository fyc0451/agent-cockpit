"""B0-PREP C1：deferred delivery 核心——事件聚合/延迟投递纯逻辑测试。

不接 server/Hub/Herdr；用 RecordingAdapter 验证状态机、幂等、绑定版本守卫、
聚合窗口与 adapter accept/失败语义。
"""
import threading

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
