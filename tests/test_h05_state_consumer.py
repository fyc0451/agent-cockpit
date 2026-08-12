"""H0.5:稳态消费者切换到 H0.4 socket 状态客户端缓存的契约测试。

复审修订(#1683 四项 blocking)后覆盖:lifespan 启停、缓存读取、显式
degraded/unavailable、discovery fail-closed(含 list_sessions 真实吞错
路径与 is_available=False)、per-session 无空窗增删/换 socket、稳态零
fork(轮询 + team router 10 pending)、SSE sig 纳入 degraded/state_status、
OpenCode 颜色字节注入清债。
R3 复审修订(#1727 两项 HIGH):running/epoch CAS 屏障与 swap timeout 持久
degraded→retry 原子换入清降级。
R4 复审修订(#1745 REVIEW_BLOCK):_state_inflight 候选登记、ownership 防
双停、stop 摘取 published+inflight、ready wait 每轮观察取消。
R5 复审修订(#1763 REVIEW_BLOCK):不可变 owner record(epoch/name/token/
client 身份比较,同名跳过复用,旧 worker 摘不到新候选)、start 移出锁
(HerdrStateClient lifecycle lock/request_stop 线性化,stop 先取得则
start 拒绝生线程)、两阶段 stop(先广播 request_stop 再共享 deadline
join)、survivor 显式返回不静默;覆盖同 epoch 双 worker、跨 epoch 同名、
锁外 blocked start、A/B signal 顺序、deadline survivor、线程/FD 基线。
R6 复审修订(#1780 REVIEW_BLOCK):retiring ownership——removed/swap-old/
timeout 候选在同一锁临界区 identity-safe 转移进 _state_retiring(真引用
受管,request_stop+join 成功才摘);global stop 快照 published+inflight+
retiring+survivors 先广播再共享 deadline join;survivor 真引用入
_state_survivors 可重复 reaper 回收,诊断序列化与引用分离;lifespan 遇
survivor raise 非正常完成。三类 retiring 真实命名线程/FD barrier、两
survivor 重试回收、正常 stop/restart。
R7 复审修订(#1810 REVIEW_BLOCK):reopen 门禁——open 先有界 reap 未决
retiring/survivors、锁内确认全空才开新 epoch,否则 fail-closed 拒绝;
reconcile 注册/发布/换入 CAS 全局拒绝未决 ownership;_STATE_REAP_LOCK
串行 stop/reaper/to_reap/重复 stop,survivor 转移锁内身份 CAS 防幽灵
复活/重复报告;覆盖 survivor 未回收→open/reconcile 拒绝→回收后 restart、
双 stop 交错无幽灵。
R9 复审修订(#1841 REVIEW_BLOCK):start False/异常/partial-start 统一
identity-safe 收尾(_start_candidate,单候选失败不炸 reconcile),
HerdrStateClient.start partial 失败回收已生线程再抛;stop 总预算从
函数入口起算、等 REAP 锁计时、timed acquire 耗尽 deferred 不重置窗口。
R10 复审修订(#1853 REVIEW_BLOCK):shutdown intent ticket——stop 入口
(尝试 REAP 前)登记,deferred 持久;open/reconcile 注册/发布/换入 CAS
被 ticket 门禁拒绝;完成的 stop 仅消费 <= 自己 ticket;覆盖前一 stop/
reaper/open 持锁三场景的 deferred→拒绝→retry→放行与真实线程零残留。
R11 复审修订(#1874 REVIEW_BLOCK):absolute deadline 为函数首个时序
动作;ticket 登记改经独立 _STATE_TICKET_LOCK 严格短临界区(不依赖可能
长持有的 CLIENT 锁),open/reconcile 读取与完成消费经同一锁同步、CAS
check+commit 与登记线性化;CLIENT timed acquire 耗尽→总预算内 deferred
(client_lock_timeout)、ticket 持久、running/ownership 零部分变更;
CLIENT-lock barrier(有/无 published client)wall-clock<=budget+容差、
释放后 open 仍 False、retry 后 open True;较早完成不清更晚 ticket。
R15:按 ADR 把 ticket 持久登记定义为唯一预算例外，登记后立即建立共享
deadline；phase2 后 bounded CLIENT 锁内读取全局 ownership，retiring 未清
或完成消费 TICKET 超时均保留 intent 并明确 deferred；REAP 单一 finally。
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
        self.stop_requested = False
        self.stop_calls = 0
        FakeStateClient.instances.append(self)

    def start(self):
        if self.stop_requested:
            return False  # stop 先取得所有权:不生线程
        self.started = True
        return True

    def request_stop(self):
        self.stop_requested = True

    def stop(self, join_timeout=5.0):
        self.request_stop()
        self.stopped = True
        self.stop_calls += 1
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
    monkeypatch.setattr(server, "H0_STATE_MODE", "on")
    monkeypatch.setattr(server, "_state_clients", {})
    monkeypatch.setattr(server, "_state_sessions_meta", {})
    monkeypatch.setattr(server, "_state_discovery_ok", False)
    monkeypatch.setattr(server, "_state_discovery_reason", "test reset")
    monkeypatch.setattr(server, "_state_running", True)
    monkeypatch.setattr(server, "_state_epoch", 1)
    monkeypatch.setattr(server, "_state_swap_pending", {})
    monkeypatch.setattr(server, "_state_inflight", {})
    monkeypatch.setattr(server, "_state_retiring", {})
    monkeypatch.setattr(server, "_state_survivors", {})
    monkeypatch.setattr(server, "_state_stop_tickets", set())
    monkeypatch.setattr(server, "STATE_SWAP_READY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(
        server.herdr_state, "HerdrStateClient", FakeStateClient
    )
    yield
    monkeypatch.setattr(server, "_state_clients", {})
    monkeypatch.setattr(server, "_state_sessions_meta", {})
    monkeypatch.setattr(server, "_state_retiring", {})
    monkeypatch.setattr(server, "_state_survivors", {})


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


# ── R4 复审修订(#1745):inflight 屏障 + 共享 stop deadline ──────


def _patch_start_calls_stop(monkeypatch, reopen=False):
    """候选 start 期间模拟 lifespan stop 抢先(锁外 start 窗口)。"""
    real_start = FakeStateClient.start

    def start_then_stop(self):
        result = real_start(self)
        server._stop_state_client()
        if reopen:
            server._open_state_clients()
        return result

    monkeypatch.setattr(FakeStateClient, "start", start_then_stop)


def test_stop_during_added_start_cas_window(monkeypatch):
    """added start→CAS 窗口:candidate 登记后、start 期间 stop 抢先。
    候选被 stop 摘取停止,不得 publish;所有 map 空。"""
    _install(monkeypatch)
    _patch_start_calls_stop(monkeypatch)
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo.sock", "late": "/tmp/late.sock"})
    server._reconcile_state_client()
    assert server._state_clients == {}
    assert server._state_inflight == {}
    assert server._state_sessions_meta == {}
    assert server._state_swap_pending == {}
    late = FakeStateClient.instances[-1]
    assert late.sessions == {"late": "/tmp/late.sock"}
    assert late.stopped is True  # stop 拥有并停止,未复活未双停


def test_stop_during_swap_ready_wait(monkeypatch):
    """候选 start 后、ready wait 中 stop:等待立即退出,stop 摘取并停止
    候选,reconcile 不再 stop/publish(ownership 单一)。"""
    clients = _install(monkeypatch)
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo-new.sock"})

    def ready_calls_stop(client, name):
        server._stop_state_client()  # wait 第一轮观察点之间抢先
        return False

    monkeypatch.setattr(server, "_client_ready", ready_calls_stop)
    server._reconcile_state_client()
    candidate = FakeStateClient.instances[-1]
    assert candidate is not clients["demo"]
    assert candidate.stopped is True  # stop 摘取 inflight 并停止
    assert server._state_clients == {}
    assert server._state_inflight == {}
    assert server._state_sessions_meta == {}
    assert server._state_swap_pending == {}  # 取消路径不留 pending


def test_stop_after_ready_before_cas(monkeypatch):
    """ready 后、换入 CAS 前 stop:不得换入,候选由 stop 停止。"""
    clients = _install(monkeypatch)
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo-new.sock"})

    def ready_then_stop(client, name):
        server._stop_state_client()
        return True  # ready 成立,但 CAS 前已被 stop

    monkeypatch.setattr(server, "_client_ready", ready_then_stop)
    server._reconcile_state_client()
    candidate = FakeStateClient.instances[-1]
    assert candidate is not clients["demo"]
    assert candidate.stopped is True
    assert server._state_clients == {}  # 未换入
    assert server._state_inflight == {}
    assert server._state_sessions_meta == {}


def test_cross_restart_late_worker_not_published(monkeypatch):
    """跨 restart:候选 start 期间 stop+reopen(新 epoch),late worker
    不得在新 lifespan publish;候选仍由 stop 停止。"""
    _install(monkeypatch)
    _patch_start_calls_stop(monkeypatch, reopen=True)
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo.sock", "late": "/tmp/late.sock"})
    server._reconcile_state_client()
    assert "late" not in server._state_clients
    assert server._state_clients == {}
    assert server._state_inflight == {}
    assert FakeStateClient.instances[-1].stopped is True


def test_reconcile_repeated_is_idempotent(monkeypatch):
    """重复 reconcile(发现结果不变):零新客户端、零停止、零 meta 漂移。"""
    clients = _install(monkeypatch)
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo.sock"})
    server._reconcile_state_client()
    server._reconcile_state_client()
    assert len(FakeStateClient.instances) == 1
    assert clients["demo"].stopped is False
    assert server._state_sessions_meta["demo"]["socket"] == "/tmp/demo.sock"


def test_same_process_restart_after_stop(monkeypatch):
    """同进程新 lifespan:stop 清空后 open+reconcile,全新客户端正常上线。"""
    clients = _install(monkeypatch)
    server._stop_state_client()
    assert server._state_clients == {}
    assert server._state_inflight == {}
    assert server._state_retiring == {}
    assert server._state_survivors == {}
    assert server._state_sessions_meta == {}
    assert server._state_swap_pending == {}
    assert server._state_discovery_ok is False
    server._open_state_clients()
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo.sock"})
    server._reconcile_state_client()
    new_client = server._state_clients["demo"]
    assert new_client is not clients["demo"]
    assert new_client.started and not new_client.stopped
    snap = server._state_client_snapshot()
    assert snap["available"] is True
    assert snap["degraded"] is False
    assert snap["sessions"][0]["state_status"] == "subscribed"


def test_stale_epoch_publish_rejected_across_restart(monkeypatch):
    """swap 路径跨 restart:wait 中 stop+reopen,新 epoch 下不得换入/留 pending。"""
    _install(monkeypatch)
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo-new.sock"})

    def ready_stop_reopen(client, name):
        server._stop_state_client()
        server._open_state_clients()
        return True

    monkeypatch.setattr(server, "_client_ready", ready_stop_reopen)
    server._reconcile_state_client()
    assert server._state_clients == {}
    assert server._state_inflight == {}
    assert server._state_swap_pending == {}
    assert FakeStateClient.instances[-1].stopped is True


def test_socket_swap_timeout_persistent_degraded_then_recovers(monkeypatch):
    """swap timeout:旧缓存可读但持久 degraded(state_status/reason 可见);
    retry ready 后原子换入、清 degraded、meta 推进。"""
    clients = _install(monkeypatch)
    FakeStateClient.ready_next = False
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo-dead.sock"})
    server._reconcile_state_client()
    snap = server._state_client_snapshot()
    assert snap["available"] is True  # 旧缓存仍可服务
    assert snap["degraded"] is True
    assert "swap pending" in snap["reason"]
    entry = snap["sessions"][0]
    assert entry["state_status"] == "swap_pending"
    assert "swap not ready" in entry["state_reason"]
    assert entry["panes"][0]["pane_id"] == "w1:p1"  # 旧 pane 可读
    assert server._state_clients["demo"] is clients["demo"]
    assert clients["demo"].stopped is False
    # retry ready:原子换入、清降级
    FakeStateClient.ready_next = True
    server._reconcile_state_client()
    assert server._state_clients["demo"] is not clients["demo"]
    assert clients["demo"].stopped is True
    assert server._state_sessions_meta["demo"]["socket"] == "/tmp/demo-dead.sock"
    assert server._state_swap_pending == {}
    snap2 = server._state_client_snapshot()
    assert snap2["degraded"] is False
    assert "reason" not in snap2
    assert snap2["sessions"][0]["state_status"] == "subscribed"


def test_stop_shared_deadline_multi_session(monkeypatch):
    """多 session stop 共享总 deadline:join_timeout 逐个递减而非每个叠加。"""
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.25)
    join_timeouts: list[float] = []

    def slow_stop(self, join_timeout=5.0):
        join_timeouts.append(join_timeout)
        self.stopped = True
        time.sleep(0.2)  # 每个 client 消耗 0.2s 预算
        return True

    monkeypatch.setattr(FakeStateClient, "stop", slow_stop)
    _install(monkeypatch, names=("a", "b", "c"))
    server._stop_state_client()
    assert len(join_timeouts) == 3
    assert join_timeouts[0] == pytest.approx(0.25, abs=0.05)
    assert join_timeouts[1] < 0.15  # 共享递减:不是每个都拿满 0.25
    assert join_timeouts[2] < 0.05
    assert server._state_clients == {}


# ── R5 复审修订(#1763):owner 身份安全 + 两阶段 stop ─────────────


def test_same_epoch_changed_double_worker_skipped(monkeypatch):
    """同 epoch 同名 changed 双 reconcile:第二个候选登记时跳过复用,
    全程只启动一个 swap worker。"""
    clients = _install(monkeypatch, names=("demo",))
    FakeStateClient.ready_next = False
    monkeypatch.setattr(server, "STATE_SWAP_READY_TIMEOUT_S", 1.0)
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo-new.sock"})
    first_wait = threading.Event()
    real_cancelled = server._candidate_cancelled

    def cancel_spy(name, owner):
        first_wait.set()  # worker1 已登记并进入 ready wait
        return real_cancelled(name, owner)

    monkeypatch.setattr(server, "_candidate_cancelled", cancel_spy)
    t1 = threading.Thread(target=server._reconcile_state_client)
    t2 = threading.Thread(target=server._reconcile_state_client)
    t1.start()
    assert first_wait.wait(5)
    t2.start()
    t1.join(5)
    t2.join(5)
    assert not t1.is_alive() and not t2.is_alive()
    candidates = [
        i for i in FakeStateClient.instances
        if i is not clients["demo"] and i.started
    ]
    assert len(candidates) == 1  # 双 worker 被注册跳过拦截(第二个从未启动)


def test_cross_epoch_same_name_old_worker_cannot_evict_new(monkeypatch):
    """old/new epoch 同名:旧 worker 迟到收尾不得摘除/停止新 epoch 候选,
    不得覆盖新 publish。"""
    clients = _install(monkeypatch, names=("demo",))
    FakeStateClient.ready_next = False
    monkeypatch.setattr(server, "STATE_SWAP_READY_TIMEOUT_S", 30.0)
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo-new.sock"})
    first_wait = threading.Event()
    real_cancelled = server._candidate_cancelled

    def cancel_spy(name, owner):
        first_wait.set()
        return real_cancelled(name, owner)

    monkeypatch.setattr(server, "_candidate_cancelled", cancel_spy)
    t1 = threading.Thread(target=server._reconcile_state_client)
    t1.start()
    assert first_wait.wait(5)  # 旧 epoch worker 在 ready wait 中
    # stop+reopen:旧候选被 stop 摘取停止;新 epoch 重新 reconcile 上线
    survivors = server._stop_state_client()
    assert survivors == []
    server._open_state_clients()
    server._reconcile_state_client()  # added 路径直接 publish 新客户端
    new_client = server._state_clients["demo"]
    old_candidate = FakeStateClient.instances[1]
    assert new_client is not clients["demo"] and new_client is not old_candidate
    t1.join(5)
    assert not t1.is_alive()
    # 旧 worker 收尾后:新客户端仍在且未被停止(身份比较防误删/双停)
    assert server._state_clients["demo"] is new_client
    assert new_client.stopped is False
    assert old_candidate.stopped is True


def test_stop_signals_all_before_any_join(monkeypatch):
    """两阶段 stop:所有 client 先收 request_stop,再逐个 join——A 未耗尽
    前 B 已被 signal。"""
    events: list[str] = []
    real_req = FakeStateClient.request_stop
    real_stop = FakeStateClient.stop

    def req(self):
        events.append(f"req:{next(iter(self.sessions))}")
        return real_req(self)

    def stp(self, join_timeout=5.0):
        events.append(f"stop:{next(iter(self.sessions))}")
        time.sleep(0.1)  # A 的 join 耗时期间 B 必须已被 signal
        return real_stop(self, join_timeout)

    monkeypatch.setattr(FakeStateClient, "request_stop", req)
    monkeypatch.setattr(FakeStateClient, "stop", stp)
    _install(monkeypatch, names=("a", "b"))
    assert server._stop_state_client() == []
    assert events.index("req:b") < events.index("stop:a")


def test_stop_deadline_survivor_reported_not_silent(monkeypatch):
    """deadline 耗尽仍存活:不静默成功、不丢引用——返回 survivor 诊断。"""
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.1)

    class ZombieClient(FakeStateClient):
        def stop(self, join_timeout=5.0):
            self.request_stop()
            self.stopped = True
            return False  # 线程拒不退场

    zombie = ZombieClient({"z": "/tmp/z.sock"})
    zombie.start()
    monkeypatch.setattr(server, "_state_clients", {"z": zombie})
    monkeypatch.setattr(server, "_state_sessions_meta", {
        "z": {"socket": "/tmp/z.sock", "directory": "/d"},
    })
    survivors = server._stop_state_client()
    assert server._state_clients == {}
    assert len(survivors) == 1
    assert survivors[0]["sessions"] == ["z"]
    assert repr(zombie) in survivors[0]["client"]  # 序列化诊断
    # 真实引用与 ownership 保留在 _state_survivors(与诊断分离)
    assert [o.client for o in server._state_survivors.values()] == [zombie]
    assert server._state_retiring == {}


def test_two_survivors_retry_reaped(monkeypatch):
    """两个 survivor:重复 request_stop/join 重试回收,成功后才释放真引用。"""
    class FlakyZombie(FakeStateClient):
        def __init__(self, sessions):
            super().__init__(sessions)
            self.fail_stop = True

        def stop(self, join_timeout=5.0):
            self.request_stop()
            if self.fail_stop:
                return False
            self.stopped = True
            return True

    z1 = FlakyZombie({"z1": "/tmp/z1.sock"})
    z1.start()
    z2 = FlakyZombie({"z2": "/tmp/z2.sock"})
    z2.start()
    monkeypatch.setattr(server, "_state_clients", {"z1": z1, "z2": z2})
    monkeypatch.setattr(server, "_state_sessions_meta", {
        n: {"socket": f"/tmp/{n}.sock", "directory": "/d"} for n in ("z1", "z2")
    })
    survivors = server._stop_state_client()
    assert len(survivors) == 2
    assert {o.client for o in server._state_survivors.values()} == {z1, z2}
    assert server._state_retiring == {}
    # 恢复可停后 reaper 重试回收,真引用释放
    z1.fail_stop = False
    z2.fail_stop = False
    server._reap_retired_clients()
    assert server._state_survivors == {}
    assert z1.stopped and z2.stopped
    assert server._stop_state_client() == []  # 幂等


def test_lifespan_raises_on_stop_survivors(monkeypatch):
    """lifespan 超时不得仅 logger.error 后正常完成:survivor 非空即 raise。"""
    monkeypatch.setattr(
        server, "_stop_state_client", lambda: [{"client": "zombie"}],
    )

    async def _noop_loop():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(server, "_reconcile_state_client", lambda: None)
    monkeypatch.setattr(server, "_poll_live_state", _noop_loop)
    monkeypatch.setattr(server, "_poll_message_state", _noop_loop)
    monkeypatch.setattr(server, "_worktree_cleanup_loop", _noop_loop)
    monkeypatch.setattr(server, "_release_all_zoom_leases", lambda: None)

    from fastapi.testclient import TestClient

    with pytest.raises(RuntimeError, match="survived stop"):
        with TestClient(server.app):
            pass


# ── R7 复审修订(#1810):reopen 门禁 + reap 串行防幽灵 ────────────


class _FlakyZombie(FakeStateClient):
    """stop 前 fail_stop=True 时永不退场;转 False 后可回收。"""

    def __init__(self, sessions):
        super().__init__(sessions)
        self.fail_stop = True

    def stop(self, join_timeout=5.0):
        self.request_stop()
        if self.fail_stop:
            return False
        self.stopped = True
        return True


def test_open_refused_until_survivor_reaped_then_restart(monkeypatch):
    """survivor 未回收→open 有界 reap 后 fail-closed 拒绝新 epoch、reconcile
    不发布;回收后 open 放行、restart 正常上线。"""
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.2)
    z = _FlakyZombie({"z": "/tmp/z.sock"})
    z.start()
    monkeypatch.setattr(server, "_state_clients", {"z": z})
    monkeypatch.setattr(server, "_state_sessions_meta", {
        "z": {"socket": "/tmp/z.sock", "directory": "/d"},
    })
    survivors = server._stop_state_client()
    assert len(survivors) == 1
    # 未决 ownership:open 拒绝(有界 reap 失败后 fail-closed)
    assert server._open_state_clients() is False
    assert server._state_running is False
    assert server._state_survivors  # 真引用仍受管
    _fake_discovery(monkeypatch, {"z": "/tmp/z.sock"})
    server._reconcile_state_client()
    assert server._state_clients == {}  # 旧线程未退,绝不发布新同名 client
    # 回收后允许 restart
    z.fail_stop = False
    assert server._open_state_clients() is True
    assert server._state_survivors == {}
    assert server._state_retiring == {}
    server._reconcile_state_client()
    assert "z" in server._state_clients
    assert server._state_clients["z"] is not z


def test_reconcile_refuses_publish_with_pending_ownership(monkeypatch):
    """running 但有未决 retiring:added 注册/发布被门禁拒绝;reaper 回收后
    下轮放行。"""
    z = _FlakyZombie({"old": "/tmp/old.sock"})
    z.start()
    with server._STATE_CLIENT_LOCK:
        server._retire_client_locked("old", server._state_epoch, z)
    _fake_discovery(monkeypatch, {"new": "/tmp/new.sock"})
    server._reconcile_state_client()
    assert "new" not in server._state_clients  # 门禁拒绝
    assert server._state_inflight == {}
    assert server._state_retiring  # 未决 ownership 保留
    z.fail_stop = False
    server._reconcile_state_client()  # reaper 回收→放行
    assert server._state_retiring == {}
    assert "new" in server._state_clients


def test_double_stop_interleave_no_ghost_survivor(monkeypatch):
    """两次 global stop + reaper 交错(REAP 锁串行):同一 owner 不重复
    报告、成功摘除后不复活、最终零 survivor。"""
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.2)
    z = _FlakyZombie({"z": "/tmp/z.sock"})
    z.start()
    monkeypatch.setattr(server, "_state_clients", {"z": z})
    monkeypatch.setattr(server, "_state_sessions_meta", {
        "z": {"socket": "/tmp/z.sock", "directory": "/d"},
    })
    r1 = server._stop_state_client()
    r2 = server._stop_state_client()  # 重复 stop:同一 owner 重试
    assert len(r1) == 1 and len(r2) == 1
    assert r1[0]["token"] == r2[0]["token"]  # 同一 owner,无双 owner
    assert len(server._state_survivors) == 1
    z.fail_stop = False
    server._reap_retired_clients()  # reaper 成功摘除
    assert server._state_survivors == {}
    assert server._state_retiring == {}
    # 幽灵检查:摘除后的 stop 不得再报告该 owner
    assert server._stop_state_client() == []


# ── R6 复审修订(#1780):retiring 真实线程/FD barrier ─────────────


def _build_real_client(name, socket_path):
    return _REAL_STATE_CLIENT(
        {name: socket_path},
        reconnect_base_delay=0.05,
        reconnect_max_delay=0.1,
        health_check_interval=None,
    )


def _alive_state_threads(name):
    return [
        t for t in threading.enumerate()
        if t.name.startswith(f"cockpit-state-{name}") and t.is_alive()
    ]


def _fd_dir() -> str:
    """列出当前进程 fd 的目录：Linux 用 /proc/self/fd，macOS 等回退 /dev/fd。"""
    import os

    for path in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(path):
            return path
    raise RuntimeError("no process fd directory (/proc/self/fd or /dev/fd)")


def _fd_count() -> int:
    import os

    return len(os.listdir(_fd_dir()))


def _wait_fd_at_most(baseline, timeout=3.0):
    """FD 数有瞬态(握手临时 socket),有界等待回落到基线内。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _fd_count() <= baseline:
            return True
        time.sleep(0.05)
    return False


# ── R8 复审修订(#1829):added 拒绝统一收尾 + open/stop 线性化 ──────


def _inject_retiring_ghost():
    ghost = FakeStateClient({"ghost": "/tmp/ghost.sock"})
    with server._STATE_CLIENT_LOCK:
        server._retire_client_locked("ghost", server._state_epoch, ghost)


def _inject_survivor_ghost():
    ghost = FakeStateClient({"ghost": "/tmp/ghost.sock"})
    with server._STATE_CLIENT_LOCK:
        owner = server._retire_client_locked("ghost", server._state_epoch, ghost)
        del server._state_retiring[owner.token]
        server._state_survivors[owner.token] = owner


def _inject_epoch_bump():
    assert server._open_state_clients() is True


def _added_blocked_real_cleanup(monkeypatch, tmp_path, inject, tag):
    """added start 后、publish CAS 前注入阻断:候选必须 identity-safe 统一
    收尾(inflight→retiring→REAP 回收),真实线程零残留,下轮恢复发布。"""
    from test_herdr_state import FakeHerdrServer

    srv = FakeHerdrServer(tmp_path, name=tag).start()
    real_start = _REAL_STATE_CLIENT.start

    fired = {"done": False}

    def start_then_inject(self):
        result = real_start(self)
        if not fired["done"]:
            fired["done"] = True
            inject()
        return result

    monkeypatch.setattr(_REAL_STATE_CLIENT, "start", start_then_inject)
    monkeypatch.setattr(server, "_build_session_client", _build_real_client)
    _fake_discovery(monkeypatch, {tag: str(srv.path)})
    server._reconcile_state_client()
    assert tag not in server._state_clients  # 门禁拒绝发布
    assert server._state_inflight == {}  # 无僵尸 inflight(同名不饥饿)
    assert _alive_state_threads(tag) == []  # 真实线程已回收
    server._reconcile_state_client()  # 阻断消失后下一轮恢复
    assert tag in server._state_clients
    assert _wait_client_ready(tag)
    assert server._state_retiring == {}
    assert server._state_survivors == {}
    server._stop_state_client()  # 清理,防线程泄漏
    srv.stop()


def test_added_publish_blocked_by_retiring_cleanup(monkeypatch, tmp_path):
    _added_blocked_real_cleanup(
        monkeypatch, tmp_path, _inject_retiring_ghost, "r8a",
    )


def test_added_publish_blocked_by_survivor_cleanup(monkeypatch, tmp_path):
    _added_blocked_real_cleanup(
        monkeypatch, tmp_path, _inject_survivor_ghost, "r8b",
    )


def test_added_publish_blocked_by_epoch_cleanup(monkeypatch, tmp_path):
    _added_blocked_real_cleanup(
        monkeypatch, tmp_path, _inject_epoch_bump, "r8c",
    )


def test_open_while_stop_join_blocked_serializes(monkeypatch):
    """stop 阶段二 join 占 REAP 期间 open 到达:open 等锁,stop 先完整结束;
    open 随后开新 epoch——最终 running=True 由锁内顺序决定。"""

    class SlowStop(FakeStateClient):
        def stop(self, join_timeout=5.0):
            self.request_stop()
            time.sleep(0.2)
            self.stopped = True
            return True

    c = SlowStop({"s": "/tmp/s.sock"})
    c.start()
    monkeypatch.setattr(server, "_state_clients", {"s": c})
    monkeypatch.setattr(server, "_state_sessions_meta", {
        "s": {"socket": "/tmp/s.sock", "directory": "/d"},
    })
    out: dict[str, object] = {}
    t = threading.Thread(
        target=lambda: out.setdefault("stop", server._stop_state_client())
    )
    t.start()
    time.sleep(0.05)  # stop 已持 REAP 进入 join
    r = server._open_state_clients()  # 必须等 stop 完成
    t.join(5)
    assert out["stop"] == []  # open 返回时 stop 已完整结束
    assert r is True
    assert server._state_running is True


def test_stop_while_open_reap_blocked_serializes(monkeypatch):
    """open 有界 reap 占 REAP 期间 stop 到达:stop 等锁;open 的 reap 可完成,
    但 R10 起 stop 已在入口登记 shutdown ticket——open 终检被 intent 拒绝
    (返回 False),stop 随后完成关闸;最终 running=False,ticket 清空后
    新 open 才放行。

    根因(macOS/3.14 时序):原 sleep(0.05)+Timer(0.15) 不能保证 open 先持
    REAP;stop 可能 REAP 超时 deferred 且不关 running。用 Event 屏障:open
    进入 zombie.stop 后才启动 stop,确认 ticket 登记后再放行 reap。
    """
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.5)
    in_reap = threading.Event()
    allow_finish = threading.Event()

    class ControlledZombie(FakeStateClient):
        """stop 先发 in_reap,阻塞到 allow_finish;fail_stop 控制成败。"""

        def __init__(self, sessions):
            super().__init__(sessions)
            self.fail_stop = True

        def stop(self, join_timeout=5.0):
            self.request_stop()
            in_reap.set()
            allow_finish.wait(5)
            if self.fail_stop:
                return False
            self.stopped = True
            return True

    z = ControlledZombie({"z": "/tmp/z.sock"})
    z.start()
    with server._STATE_CLIENT_LOCK:
        server._retire_client_locked("z", server._state_epoch, z)
    out: dict[str, object] = {}
    open_t = threading.Thread(
        target=lambda: out.setdefault("open", server._open_state_clients())
    )
    open_t.start()
    assert in_reap.wait(5)  # open 已持 REAP 并进入 _reap_owner→client.stop
    stop_out: dict[str, object] = {}
    stop_t = threading.Thread(
        target=lambda: stop_out.setdefault("r", server._stop_state_client())
    )
    stop_t.start()
    # 等 stop 入口登记 ticket(阻塞在 REAP 上)再放行 open 的 reap
    ticket_deadline = time.monotonic() + 2.0
    while time.monotonic() < ticket_deadline and not server._state_stop_tickets:
        time.sleep(0.01)
    assert server._state_stop_tickets
    z.fail_stop = False
    allow_finish.set()
    open_t.join(5)
    stop_t.join(5)
    assert out["open"] is False  # R10:stop 的 shutdown intent 拒绝 open
    assert server._state_running is False  # stop 后完成,最终关闸
    assert stop_out["r"] == []  # zombie 已被 open 回收,stop 无 survivor
    assert not server._state_stop_tickets  # 完成即消费 intent
    assert server._open_state_clients() is True  # ticket 清空后放行


def test_double_open_serialized_epochs(monkeypatch):
    """双 open 并发:REAP 锁串行,各增一次 epoch,均返回 True。"""
    epoch0 = server._state_epoch
    results: list[bool] = []
    threads = [
        threading.Thread(target=lambda: results.append(server._open_state_clients()))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == [True, True]
    assert server._state_epoch == epoch0 + 2
    assert server._state_running is True


# ── R9 复审修订(#1841):start 异常统一收尾 + stop 绝对 deadline ──


def test_added_start_exception_unified_cleanup(monkeypatch):
    """added start 抛异常:reconcile 不炸,候选 identity-safe 收尾(无僵尸
    inflight、无活线程假象),下轮恢复发布。"""
    real_start = FakeStateClient.start
    calls = {"n": 0}

    def start_boom_once(self):
        calls["n"] += 1
        if calls["n"] == 2:  # instances[0]=_install 的 demo;第二个是候选
            raise RuntimeError("start boom")
        return real_start(self)

    monkeypatch.setattr(FakeStateClient, "start", start_boom_once)
    _install(monkeypatch)
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo.sock", "bad": "/tmp/bad.sock"})
    server._reconcile_state_client()  # 不抛
    assert server._state_inflight == {}
    assert "bad" not in server._state_clients
    assert server._state_retiring == {}  # 已经统一 REAP 回收
    assert server._state_survivors == {}
    assert server._state_clients["demo"].stopped is False  # 健康 session 不动
    server._reconcile_state_client()  # 恢复轮
    assert "bad" in server._state_clients


def test_added_start_false_unified_cleanup(monkeypatch):
    """added start 返回 False(stop 先取得所有权语义):统一收尾不双停。"""
    _install(monkeypatch)
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo.sock", "late": "/tmp/late.sock"})
    real_start = FakeStateClient.start

    def start_stop_first(self):
        server._stop_state_client()  # stop 在 start 前取得所有权
        return real_start(self)  # stop_requested 已置 → False

    monkeypatch.setattr(FakeStateClient, "start", start_stop_first)
    server._reconcile_state_client()
    candidate = FakeStateClient.instances[-1]
    assert server._state_inflight == {}
    assert candidate.stop_calls == 1  # stop 摘取后只停一次,无双停
    assert server._state_clients == {}


def test_changed_start_exception_unified_cleanup(monkeypatch):
    """changed(swap)候选 start 抛异常:旧 client 不受影响继续服务,候选
    统一收尾,下轮重试换入。"""
    clients = _install(monkeypatch)
    calls = {"n": 0}
    real_start = FakeStateClient.start

    def start_boom_once(self):
        calls["n"] += 1
        if calls["n"] == 1:  # patch 在 _install 之后:首个即 swap 候选
            raise RuntimeError("swap start boom")
        return real_start(self)

    monkeypatch.setattr(FakeStateClient, "start", start_boom_once)
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo-new.sock"})
    server._reconcile_state_client()  # 不抛
    assert server._state_inflight == {}
    assert server._state_retiring == {}
    assert server._state_clients["demo"] is clients["demo"]  # 旧 client 保留
    assert clients["demo"].stopped is False
    server._reconcile_state_client()  # 重试成功换入
    assert server._state_clients["demo"] is not clients["demo"]
    assert clients["demo"].stopped is True


def test_changed_start_exception_with_concurrent_stop_no_double(monkeypatch):
    """changed 候选 start 期间并发 stop 摘取 + start 抛异常:不双停、
    不复活、四 maps 全空。"""
    clients = _install(monkeypatch)
    real_start = FakeStateClient.start

    def start_stop_then_boom(self):
        server._stop_state_client()
        raise RuntimeError("boom after stop")

    monkeypatch.setattr(FakeStateClient, "start", start_stop_then_boom)
    _fake_discovery(monkeypatch, {"demo": "/tmp/demo-new.sock"})
    server._reconcile_state_client()  # 不抛
    candidate = FakeStateClient.instances[-1]
    assert candidate is not clients["demo"]
    assert candidate.stop_calls == 1  # 仅 stop 摘取时停一次
    assert server._state_clients == {}
    assert server._state_inflight == {}
    assert server._state_retiring == {}
    assert server._state_survivors == {}


def test_stop_lock_wait_counts_toward_budget(monkeypatch):
    """stop 总预算含等 REAP 锁:前一 stop 占锁 join 时,排队 stop 的
    wall-clock 不超过 budget+调度容差,且 join 只用 remaining。"""
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.3)

    class SlowStop(FakeStateClient):
        def stop(self, join_timeout=5.0):
            self.request_stop()
            time.sleep(0.2)
            self.stopped = True
            return True

    c = SlowStop({"s": "/tmp/s.sock"})
    c.start()
    monkeypatch.setattr(server, "_state_clients", {"s": c})
    monkeypatch.setattr(server, "_state_sessions_meta", {
        "s": {"socket": "/tmp/s.sock", "directory": "/d"},
    })
    out: dict[str, object] = {}
    t = threading.Thread(
        target=lambda: out.setdefault("first", server._stop_state_client())
    )
    t.start()
    time.sleep(0.05)  # 前一 stop 持 REAP  join 中
    t0 = time.monotonic()
    r = server._stop_state_client()  # 排队 stop:等锁计入预算
    elapsed = time.monotonic() - t0
    t.join(5)
    assert out["first"] == []
    assert r == []  # 前者已收干净,本 stop 无剩余工作
    assert elapsed < 0.3 + 0.3  # budget + 合理调度容差


def test_stop_budget_exhausted_by_lock_wait_defers(monkeypatch):
    """等锁耗尽预算:deferred 返回,不重置 join 窗口;ownership 原样受管
    (maps 未被本 stop 触碰),running 不被改动。"""
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.15)

    class VerySlowStop(FakeStateClient):
        def stop(self, join_timeout=5.0):
            self.request_stop()
            time.sleep(0.3)
            self.stopped = True
            return True

    c = VerySlowStop({"s": "/tmp/s.sock"})
    c.start()
    monkeypatch.setattr(server, "_state_clients", {"s": c})
    monkeypatch.setattr(server, "_state_sessions_meta", {
        "s": {"socket": "/tmp/s.sock", "directory": "/d"},
    })
    out: dict[str, object] = {}
    t = threading.Thread(
        target=lambda: out.setdefault("first", server._stop_state_client())
    )
    t.start()
    time.sleep(0.05)  # 前一 stop 持 REAP(其 join 0.3s > 本次预算)
    t0 = time.monotonic()
    r = server._stop_state_client()
    elapsed = time.monotonic() - t0
    t.join(5)
    assert r and r[0].get("deferred") is True  # 预算耗尽,明确放弃
    assert elapsed < 0.15 + 0.3  # 不超过预算+容差
    assert out["first"] == []
    # ownership 未丢:first stop 已完成回收,maps 干净;本 stop 未产生幽灵
    assert server._state_retiring == {}
    assert server._state_survivors == {}


# ── R10 复审修订(#1853):deferred shutdown intent 持久门禁 ───────


class _GatedZombie(FakeStateClient):
    """stop 阻塞在 gate 上,由测试主线程控制放行;fail_stop 控制成败。"""

    def __init__(self, sessions, gate):
        super().__init__(sessions)
        self.gate = gate
        self.fail_stop = True

    def stop(self, join_timeout=5.0):
        self.request_stop()
        self.gate.wait(5)
        if self.fail_stop:
            return False
        self.stopped = True
        return True


def test_deferred_stop_intent_blocks_until_retry(monkeypatch):
    """REAP 被前一 stop 持有→本 stop deferred:shutdown intent 持久——
    open=False、reconcile 零新增;含 published client+真实 inflight 候选;
    retry stop 完成后 open 才放行;真实线程最终零。

    D-fix:用 entered+release Events 证明 first 已进入并保持在 stop/REAP,
    主线程见到 entered 后再发 second;second 得 deferred 后才 release first。
    不用 sleep 竞态证明持锁。"""
    # first 用较长预算,second 单独缩预算,保证 second 在 REAP 上明确 deferred
    # 后 first 仍有剩余窗口完成回收。
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 1.0)
    entered, release = threading.Event(), threading.Event()

    class GatedStop(FakeStateClient):
        def stop(self, join_timeout=5.0):
            self.request_stop()
            entered.set()
            assert release.wait(5), "first stop release timed out"
            self.stopped = True
            return True

    slow = GatedStop({"s": "/tmp/s.sock"})
    slow.start()
    monkeypatch.setattr(server, "_state_clients", {"s": slow})
    monkeypatch.setattr(server, "_state_sessions_meta", {
        "s": {"socket": "/tmp/s.sock", "directory": "/d"},
    })
    # 真实 inflight 候选(死 socket,线程在退避循环中)
    cand = _REAL_STATE_CLIENT(
        {"r10": "/nonexistent/r10.sock"},
        reconnect_base_delay=0.05,
        reconnect_max_delay=0.1,
        health_check_interval=None,
    )
    cand.start()
    with server._STATE_CLIENT_LOCK:
        owner = server._InflightOwner(
            epoch=server._state_epoch, name="r10",
            token=next(server._inflight_token), client=cand,
        )
        server._state_inflight["r10"] = owner
    fd_before = _fd_count()
    out: dict[str, object] = {}
    t = threading.Thread(
        target=lambda: out.setdefault("first", server._stop_state_client())
    )
    t.start()
    assert entered.wait(5), "first stop never entered client.stop (REAP held)"
    # first 已持 REAP 并阻塞在 stop;缩短 second 预算以快速明确 deferred
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.1)
    r2 = server._stop_state_client()
    assert r2 and r2[0].get("deferred") is True
    assert r2[0].get("reason") == "reap_lock_timeout"
    assert server._state_stop_tickets  # intent 未丢
    # second 已 deferred:放行 first 完成回收
    release.set()
    t.join(5)
    assert not t.is_alive()
    assert out["first"] == []
    assert slow.stopped and server._state_clients == {}  # 旧 client 已停
    assert _alive_state_threads("r10") == []  # inflight 候选线程已回收
    # intent 未清:直接 open 拒绝、reconcile 零新增/零候选
    assert server._open_state_clients() is False
    monkeypatch.setattr(server, "_state_running", True)  # 单独验 ticket 门禁
    _fake_discovery(monkeypatch, {"new": "/tmp/n.sock"})
    server._reconcile_state_client()
    assert "new" not in server._state_clients
    assert server._state_inflight == {}
    # retry stop 完成(更晚 ticket 覆盖更早 intent)→ open 放行
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 1.0)
    assert server._stop_state_client() == []
    assert not server._state_stop_tickets
    assert server._open_state_clients() is True
    assert _fd_count() <= fd_before


def test_deferred_stop_intent_when_reaper_holds_lock(monkeypatch):
    """REAP 被 reaper 持有(reap 阻塞)→stop deferred,intent 持久门禁。"""
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.15)
    gate = threading.Event()
    z = _GatedZombie({"z": "/tmp/z.sock"}, gate)
    z.start()
    with server._STATE_CLIENT_LOCK:
        server._retire_client_locked("z", server._state_epoch, z)
    out: dict[str, object] = {}
    t = threading.Thread(
        target=lambda: out.setdefault("reap", server._reap_retired_clients())
    )
    t.start()
    time.sleep(0.05)  # reaper 持 REAP,阻塞在 zombie.stop 的 gate 上
    r = server._stop_state_client()
    assert r[0]["deferred"] is True
    assert server._state_stop_tickets
    gate.set()
    t.join(5)
    assert server._open_state_clients() is False  # intent 未清仍拒绝
    z.fail_stop = False
    assert server._stop_state_client() == []  # retry:完成并消费 intent
    assert server._open_state_clients() is True


def test_deferred_stop_intent_when_open_holds_lock(monkeypatch):
    """REAP 被 open 的有界 reap 持有→stop deferred;open 自身也被 intent
    门禁拒绝;retry stop 完成后才允许 open。"""
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.15)
    gate = threading.Event()
    z = _GatedZombie({"z": "/tmp/z.sock"}, gate)
    z.start()
    with server._STATE_CLIENT_LOCK:
        server._retire_client_locked("z", server._state_epoch, z)
    out: dict[str, object] = {}
    t = threading.Thread(
        target=lambda: out.setdefault("open", server._open_state_clients())
    )
    t.start()
    time.sleep(0.05)  # open 持 REAP reap,阻塞在 gate
    r = server._stop_state_client()
    assert r[0]["deferred"] is True
    gate.set()  # 放行 open 的 reap(zombie 仍 fail→open 有界耗尽)
    t.join(5)
    assert out["open"] is False  # retiring 未清+intent 未清:open 拒绝
    z.fail_stop = False
    assert server._stop_state_client() == []  # retry 完成
    assert not server._state_stop_tickets
    assert server._open_state_clients() is True


# ── R11 复审修订(#1874):CLIENT 锁等待计入 absolute deadline ──────


def _hold_client_lock(entered, release):
    with server._STATE_CLIENT_LOCK:
        entered.set()
        release.wait(5)


def test_stop_client_lock_held_defers_with_published_client(monkeypatch):
    """R11:有 published client 时 CLIENT 锁被持有 > budget→stop 在总预算
    内 deferred(client_lock_timeout),wall-clock<=budget+容差;ticket
    持久,running/ownership 零部分变更;释放后 open 仍 False,retry stop
    完成后 open True。"""
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.2)
    c = FakeStateClient({"s": "/tmp/s.sock"})
    c.start()
    monkeypatch.setattr(server, "_state_clients", {"s": c})
    monkeypatch.setattr(server, "_state_sessions_meta", {
        "s": {"socket": "/tmp/s.sock", "directory": "/d"},
    })
    entered, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_client_lock, args=(entered, release))
    t.start()
    assert entered.wait(5)
    t0 = time.monotonic()
    r = server._stop_state_client()
    elapsed = time.monotonic() - t0
    assert r[0]["deferred"] is True
    assert r[0]["reason"] == "client_lock_timeout"
    assert elapsed < 0.2 + 0.3  # CLIENT 等待计入预算,不超 budget+调度容差
    assert server._state_stop_tickets  # intent 持久
    # 零部分变更:running 未动,client 未被摘/未停,无 retiring 残留
    assert server._state_running is True
    assert server._state_clients.get("s") is c
    assert not c.stopped
    assert server._state_retiring == {}
    assert server._state_survivors == {}
    release.set()
    t.join(5)
    assert server._open_state_clients() is False  # intent 未清仍拒绝
    assert server._stop_state_client() == []  # retry:干净完成并消费 intent
    assert c.stopped
    assert not server._state_stop_tickets
    assert server._open_state_clients() is True


def test_stop_client_lock_held_defers_without_client(monkeypatch):
    """R11:无 published client 时 CLIENT 锁被持有 > budget 同样在预算内
    deferred、零部分变更;释放后 open 仍 False,retry 后 open True。"""
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.2)
    entered, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_client_lock, args=(entered, release))
    t.start()
    assert entered.wait(5)
    t0 = time.monotonic()
    r = server._stop_state_client()
    elapsed = time.monotonic() - t0
    assert r[0]["deferred"] is True
    assert r[0]["reason"] == "client_lock_timeout"
    assert elapsed < 0.2 + 0.3
    assert server._state_stop_tickets
    assert server._state_running is True  # 零部分变更
    assert server._state_clients == {}
    release.set()
    t.join(5)
    assert server._open_state_clients() is False
    assert server._stop_state_client() == []
    assert not server._state_stop_tickets
    assert server._open_state_clients() is True


def test_earlier_stop_completion_keeps_later_ticket(monkeypatch):
    """R11:较早 ticket 的 stop 完成只消费 <= 自己 ticket——完成期间登记
    的更晚 ticket 保留,open 仍拒绝;retry stop 完成才放行。"""
    gate = threading.Event()
    z = _GatedZombie({"z": "/tmp/z.sock"}, gate)
    z.fail_stop = False  # 放行后 stop 成功
    z.start()
    monkeypatch.setattr(server, "_state_clients", {"z": z})
    monkeypatch.setattr(server, "_state_sessions_meta", {
        "z": {"socket": "/tmp/z.sock", "directory": "/d"},
    })
    out: dict[str, object] = {}
    t = threading.Thread(
        target=lambda: out.setdefault("first", server._stop_state_client())
    )
    t.start()
    deadline = time.monotonic() + 5
    while not z.stop_requested and time.monotonic() < deadline:
        time.sleep(0.01)  # 等 first stop 进入阶段二 join(持 REAP,阻塞在 gate)
    assert z.stop_requested
    # first stop 事务进行中登记更晚 ticket(REAP 被持→本次 deferred)
    monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.1)
    r2 = server._stop_state_client()
    assert r2[0]["deferred"] is True
    later_ticket = r2[0]["ticket"]
    gate.set()
    t.join(5)
    assert out["first"] == []  # 较早 ticket 的 stop 完成
    # 较早完成绝不清除更晚 ticket
    assert server._state_stop_tickets == {later_ticket}
    assert server._open_state_clients() is False
    assert server._stop_state_client() == []  # retry:消费更晚 ticket
    assert not server._state_stop_tickets
    assert server._open_state_clients() is True


def _wait_client_ready(name, timeout=5.0):
    """等 published 真实 client 完成握手(subscribed),给 FD 基线稳态。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client = server._state_clients.get(name)
        if client is not None and server._client_ready(client, name):
            return True
        time.sleep(0.05)
    return False


def test_retiring_removed_session_real_thread_fd(monkeypatch, tmp_path):
    """removed pop→真实 stop 之间 client 受管于 retiring:真实线程退出、
    FD 回基线、retiring 摘除。"""
    import os

    from test_herdr_state import FakeHerdrServer

    srv = FakeHerdrServer(tmp_path, name="rm").start()
    monkeypatch.setattr(server, "_build_session_client", _build_real_client)
    _fake_discovery(monkeypatch, {"rm": str(srv.path)})
    server._reconcile_state_client()
    assert "rm" in server._state_clients
    fd_before = _fd_count()
    _fake_discovery(monkeypatch, {})  # session 消失
    server._reconcile_state_client()
    assert server._state_clients == {}
    assert server._state_retiring == {}  # 真正停成功才摘
    assert server._state_survivors == {}
    assert _alive_state_threads("rm") == []
    assert _fd_count() <= fd_before
    srv.stop()


def test_retiring_swap_old_client_real_thread_fd(monkeypatch, tmp_path):
    """swap CAS 后旧 client 真实回收:只剩新 client 线程,FD 不增长。"""
    import os

    from test_herdr_state import FakeHerdrServer

    srv_a = FakeHerdrServer(tmp_path, name="swa").start()
    srv_b = FakeHerdrServer(tmp_path, name="swb").start()
    monkeypatch.setattr(server, "_build_session_client", _build_real_client)
    monkeypatch.setattr(server, "STATE_SWAP_READY_TIMEOUT_S", 5.0)
    _fake_discovery(monkeypatch, {"sw": str(srv_a.path)})
    server._reconcile_state_client()
    old = server._state_clients["sw"]
    assert _wait_client_ready("sw")  # 稳态后再取 FD 基线
    fd_before = _fd_count()
    _fake_discovery(monkeypatch, {"sw": str(srv_b.path)})
    server._reconcile_state_client()
    assert server._state_clients["sw"] is not old
    assert server._state_retiring == {}
    assert server._state_survivors == {}
    assert len(_alive_state_threads("sw")) == 1  # 仅新 client 线程存活
    assert _wait_fd_at_most(fd_before)
    assert server._state_sessions_meta["sw"]["socket"] == str(srv_b.path)
    server._stop_state_client()  # 清理:停掉存活的新 client,防线程泄漏
    srv_a.stop()
    srv_b.stop()


def test_retiring_timeout_candidate_real_thread_fd(monkeypatch, tmp_path):
    """swap timeout 候选摘除→真实 stop:旧 client 线程独存,候选无线程/FD。"""
    import os

    from test_herdr_state import FakeHerdrServer

    srv_a = FakeHerdrServer(tmp_path, name="to").start()
    monkeypatch.setattr(server, "_build_session_client", _build_real_client)
    _fake_discovery(monkeypatch, {"to": str(srv_a.path)})
    server._reconcile_state_client()
    old = server._state_clients["to"]
    assert _wait_client_ready("to")  # 稳态后再取 FD 基线
    fd_before = _fd_count()
    _fake_discovery(monkeypatch, {"to": "/nonexistent/to-dead.sock"})
    server._reconcile_state_client()  # 候选永不就绪→timeout 弃新留旧
    assert server._state_clients["to"] is old
    assert server._state_retiring == {}
    assert server._state_survivors == {}
    assert len(_alive_state_threads("to")) == 1  # 仅旧 client
    assert _wait_fd_at_most(fd_before)
    assert server._state_swap_pending  # 持久 degraded 记录保留
    server._stop_state_client()  # 清理:停掉存活的旧 client,防线程泄漏
    srv_a.stop()


def test_real_stop_during_blocked_start_no_thread(monkeypatch):
    """另一线程 stop-during-blocked-start:request_stop 先取得所有权后,
    被放行的 start 必须拒绝生线程;无 survivor、无 publish。"""
    entered = threading.Event()
    release = threading.Event()

    class BlockingStartClient(_REAL_STATE_CLIENT):
        def start(self):
            entered.set()
            release.wait(5)
            return super().start()

    def build_blocking(name, socket_path):
        return BlockingStartClient(
            {name: socket_path},
            reconnect_base_delay=0.05,
            reconnect_max_delay=0.1,
            health_check_interval=None,
        )

    monkeypatch.setattr(server, "_build_session_client", build_blocking)
    _fake_discovery(monkeypatch, {"blk": "/nonexistent/blk.sock"})
    worker = threading.Thread(target=server._reconcile_state_client)
    worker.start()
    assert entered.wait(5)  # reconcile 已登记 owner,阻塞在锁外 start
    survivors = server._stop_state_client()  # stop 在 start 完成前取得所有权
    assert survivors == []
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert server._state_clients == {}
    assert server._state_inflight == {}
    assert server._state_sessions_meta == {}
    assert not [
        t for t in threading.enumerate()
        if t.name.startswith("cockpit-state-blk") and t.is_alive()
    ]


def test_real_inflight_candidate_stop_no_thread_fd_leak(monkeypatch):
    """真实候选在 ready wait 中被 stop:stop 返回瞬间所有 map 空、无存活
    cockpit-state 线程、FD 回基线;reconcile 随后退出且不 publish。"""
    import os

    def build_real(name, socket_path):
        return _REAL_STATE_CLIENT(
            {name: socket_path},
            reconnect_base_delay=0.05,
            reconnect_max_delay=0.1,
            health_check_interval=None,
        )

    monkeypatch.setattr(server, "_build_session_client", build_real)
    _install(monkeypatch, names=("race",))  # 旧 published 为 Fake
    _fake_discovery(monkeypatch, {"race": "/nonexistent/race-new.sock"})
    monkeypatch.setattr(server, "STATE_SWAP_READY_TIMEOUT_S", 30.0)
    fd_before = _fd_count()
    worker = threading.Thread(target=server._reconcile_state_client)
    worker.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not server._state_inflight:
        time.sleep(0.01)
    assert server._state_inflight  # 候选已登记,处于 ready wait
    server._stop_state_client()
    # stop 返回瞬间:全部 map 空、线程/FD 无残留
    assert server._state_clients == {}
    assert server._state_inflight == {}
    assert server._state_sessions_meta == {}
    assert server._state_swap_pending == {}
    assert not [
        t for t in threading.enumerate()
        if t.name.startswith("cockpit-state-race") and t.is_alive()
    ]
    assert _fd_count() <= fd_before
    worker.join(5)
    assert not worker.is_alive()  # wait 循环观察取消立即退出
    assert server._state_clients == {}  # late worker 未 publish


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
    kept = source.count("herdr_client.snapshot(")
    assert kept == 4  # 2 个 mutation 验证 + off/canary 的兼容 snapshot
    for marker in ("H0.5 保留 CLI",):
        assert source.count(marker) == 2


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


class TestPhase2BoundedClientR12:
    """#1883: phase2 CLIENT reacquire bounded by absolute_deadline;另一线程
    持 CLIENT 至 >budget → _reap_owner/survivor 转移在 remaining 内返回
    False(deferred),不阻塞;ownership 保留在 retiring。

    D-fix:acquired Event 证明 holder 已持 CLIENT;RecordingLock 记录
    acquire(timeout=…) 并断言 timeout≤budget;返回时 holder 仍持锁/owner
    仍保留。不用贴边 wall-clock(budget+0.08);宽松 watchdog 仅防挂死。"""

    class _RecordingClientLock:
        """代理 RLock:记录 timeout>=0 的 acquire 实参(RLock.acquire 只读不可补丁)。"""

        def __init__(self):
            self._inner = threading.RLock()
            self.timeouts: list[float] = []

        def acquire(self, blocking=True, timeout=-1):
            if timeout is not None and timeout >= 0:
                self.timeouts.append(float(timeout))
            return self._inner.acquire(blocking=blocking, timeout=timeout)

        def release(self):
            return self._inner.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()
            return False

    def _install_recording_client_lock(self, monkeypatch):
        lock = self._RecordingClientLock()
        monkeypatch.setattr(server, "_STATE_CLIENT_LOCK", lock)
        return lock

    def _hold_client(self, hold_s: float = 5.0):
        """起一线程持 _STATE_CLIENT_LOCK,acquired 表示已进入临界区。"""
        acquired = threading.Event()
        release = threading.Event()

        def hold():
            with server._STATE_CLIENT_LOCK:
                acquired.set()
                release.wait(hold_s)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        return acquired, release, t

    def test_reap_returns_within_budget_when_client_held_stop_true(self, monkeypatch):
        """stop=True 后 CLIENT reacquire 被另一线程持有→_reap_owner 返回
        False(deferred);owner 保留;holder 仍持锁;acquire timeout≤budget。"""
        lock = self._install_recording_client_lock(monkeypatch)
        client = FakeStateClient({"s1": "/tmp/x.sock"})
        with server._STATE_CLIENT_LOCK:
            owner = server._retire_client_locked("s1", server._state_epoch, client)
        acquired, release, holder = self._hold_client()
        assert acquired.wait(5), "holder never acquired CLIENT"
        lock.timeouts.clear()  # 只统计 reacquire 阶段
        budget = 0.1
        deadline = time.monotonic() + budget
        watchdog = budget + 2.0  # hang guard only
        start = time.monotonic()
        result = server._reap_owner(owner, deadline)
        elapsed = time.monotonic() - start
        assert result is False  # deferred
        assert owner.token in server._state_retiring
        assert holder.is_alive()
        assert not server._STATE_CLIENT_LOCK.acquire(blocking=False)
        assert lock.timeouts, "expected CLIENT.acquire(timeout=remaining) during reacquire"
        assert all(t <= budget + 1e-9 for t in lock.timeouts), lock.timeouts
        assert elapsed < watchdog
        release.set()
        holder.join(timeout=2)
        assert not holder.is_alive()

    def test_survivor_transfer_deferred_when_client_held_stop_false(self, monkeypatch):
        """stop=False 后 phase2 survivor 转移用 bounded CLIENT;拿不到→deferred,
        owner 保留在 retiring;acquire timeout≤budget;返回时 holder 仍持锁。"""
        class FailStop(FakeStateClient):
            def stop(self, join_timeout=5.0):
                return False

        lock = self._install_recording_client_lock(monkeypatch)
        client = FailStop({"s1": "/tmp/x.sock"})
        with server._STATE_CLIENT_LOCK:
            owner = server._retire_client_locked("s1", server._state_epoch, client)
        acquired, release, holder = self._hold_client()
        assert acquired.wait(5), "holder never acquired CLIENT"
        lock.timeouts.clear()
        budget = 0.1
        deadline = time.monotonic() + budget
        watchdog = budget + 2.0
        start = time.monotonic()
        survived = []
        # 模拟 phase2 loop（_stop_state_client 内的逻辑）
        if not server._reap_owner(owner, deadline):
            remaining = max(0.0, deadline - time.monotonic())
            assert remaining <= budget + 1e-9
            if server._STATE_CLIENT_LOCK.acquire(timeout=remaining):
                try:
                    if server._state_retiring.get(owner.token) is owner:
                        del server._state_retiring[owner.token]
                        server._state_survivors[owner.token] = owner
                        survived.append(owner)
                finally:
                    server._STATE_CLIENT_LOCK.release()
        elapsed = time.monotonic() - start
        assert len(survived) == 0  # deferred:未转移
        assert owner.token in server._state_retiring  # 保留在 retiring
        assert holder.is_alive()
        assert not server._STATE_CLIENT_LOCK.acquire(blocking=False)
        assert lock.timeouts, "expected CLIENT.acquire(timeout=remaining) for survivor transfer"
        assert all(t <= budget + 1e-9 for t in lock.timeouts), lock.timeouts
        assert elapsed < watchdog
        release.set()
        holder.join(timeout=2)
        assert not holder.is_alive()

    def test_reap_succeeds_after_client_released_retry(self, monkeypatch):
        """retry:CLIENT 释放后 _reap_owner 正常摘除(stop=True)。"""
        self._install_recording_client_lock(monkeypatch)
        client = FakeStateClient({"s1": "/tmp/x.sock"})
        with server._STATE_CLIENT_LOCK:
            owner = server._retire_client_locked("s1", server._state_epoch, client)
        # 第一轮:CLIENT 被持有→deferred
        acquired, release, holder = self._hold_client()
        assert acquired.wait(5), "holder never acquired CLIENT"
        assert not server._reap_owner(owner, time.monotonic() + 0.1)
        assert owner.token in server._state_retiring
        assert holder.is_alive()
        # holder 释放后 retry
        release.set()
        holder.join(timeout=2)
        assert not holder.is_alive()
        assert server._reap_owner(owner, time.monotonic() + 1.0) is True
        assert owner.token not in server._state_retiring  # 摘除成功


class TestDurableStopTicketR15:
    """R15:ticket 登记是预算前的持久前奏；登记后的所有 barrier 都在
    同一 absolute deadline 内明确 deferred，且只有 retry global stop
    完成后才消费 intent。"""

    class Phase2BarrierClient(FakeStateClient):
        def __init__(self, sessions, entered, holder_has, *, stop_ok):
            super().__init__(sessions)
            self.entered = entered
            self.holder_has = holder_has
            self.stop_ok = stop_ok

        def stop(self, join_timeout=5.0):
            self.request_stop()
            self.stop_calls += 1
            self.entered.set()
            assert self.holder_has.wait(5)
            if self.stop_ok:
                self.stopped = True
            return self.stop_ok

    class _RecordingLock:
        """代理 Lock/RLock:记录 timeout>=0 的 acquire(timeout=) 实参。"""

        def __init__(self, *, rlock: bool = False):
            self._inner = threading.RLock() if rlock else threading.Lock()
            self.timeouts: list[float] = []

        def acquire(self, blocking=True, timeout=-1):
            if timeout is not None and timeout >= 0:
                self.timeouts.append(float(timeout))
            return self._inner.acquire(blocking=blocking, timeout=timeout)

        def release(self):
            return self._inner.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()
            return False

    @pytest.mark.parametrize("initial_stop_ok", [True, False])
    def test_phase2_client_barrier_requires_retry_global_stop(
        self, monkeypatch, initial_stop_ok,
    ):
        """完整 stop=True/False phase2 barrier：预算内明确 deferred，
        ticket/owner 持久；仅 reaper 摘 owner 后 direct open 仍拒绝，retry
        global stop identity-safe 完成后才消费并允许 explicit open。

        D2:用 Recording CLIENT lock + holder 仍持锁/ticket 未消费语义，
        替代贴边 wall-clock(budget+0.15)。"""
        budget = 0.1
        monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", budget)
        client_lock = self._RecordingLock(rlock=True)
        monkeypatch.setattr(server, "_STATE_CLIENT_LOCK", client_lock)
        entered, holder_has, release = (
            threading.Event(), threading.Event(), threading.Event()
        )
        client = self.Phase2BarrierClient(
            {"p2": "/tmp/p2.sock"}, entered, holder_has,
            stop_ok=initial_stop_ok,
        )
        client.start()
        monkeypatch.setattr(server, "_state_clients", {"p2": client})
        monkeypatch.setattr(server, "_state_sessions_meta", {
            "p2": {"socket": "/tmp/p2.sock", "directory": "/d"},
        })

        def hold_client_after_stop():
            assert entered.wait(5)
            with server._STATE_CLIENT_LOCK:
                holder_has.set()
                release.wait(5)

        holder = threading.Thread(target=hold_client_after_stop)
        holder.start()
        client_lock.timeouts.clear()
        start = time.monotonic()
        result = server._stop_state_client()
        elapsed = time.monotonic() - start
        assert result and result[0]["deferred"] is True
        assert result[0]["reason"] == "phase2_state_lock_timeout"
        ticket = result[0]["ticket"]
        assert server._state_stop_tickets == {ticket}
        # 语义:返回时 holder 仍持 CLIENT；bounded acquire timeout≤budget
        assert holder.is_alive()
        assert not server._STATE_CLIENT_LOCK.acquire(blocking=False)
        assert client_lock.timeouts, "expected bounded CLIENT.acquire during phase2"
        assert all(t <= budget + 1e-9 for t in client_lock.timeouts), client_lock.timeouts
        assert elapsed < budget + 2.0  # hang guard only
        release.set()
        holder.join(5)
        assert not holder.is_alive()

        # 首轮 CLIENT 超时前 owner 已 identity-safe 摘入 retiring。
        assert len(server._state_retiring) == 1
        assert server._state_survivors == {}
        client.stop_ok = True
        server._reap_retired_clients()
        assert server._state_retiring == {}
        assert server._state_survivors == {}
        # Reaper 只回收 owner，不能替 retry global stop 消费 intent。
        assert server._state_stop_tickets
        assert server._open_state_clients() is False
        assert server._stop_state_client() == []
        assert not server._state_stop_tickets
        assert server._open_state_clients() is True

    def test_ticket_consume_barrier_defers_and_preserves_intent(self, monkeypatch):
        """phase2 已回收后完成消费 TICKET 锁被持有：same deadline 内
        deferred，ticket 持久；释放后仍需 retry global stop。

        D2:Recording TICKET lock + holder 仍持锁/ticket 未消费语义，
        替代贴边 wall-clock(0.1+0.15；macOS 3.14 曾 0.254>0.25)。"""
        budget = 0.1
        monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", budget)
        ticket_lock = self._RecordingLock(rlock=False)
        monkeypatch.setattr(server, "_STATE_TICKET_LOCK", ticket_lock)
        entered, holder_has, release = (
            threading.Event(), threading.Event(), threading.Event()
        )
        client = self.Phase2BarrierClient(
            {"tc": "/tmp/tc.sock"}, entered, holder_has, stop_ok=True,
        )
        client.start()
        monkeypatch.setattr(server, "_state_clients", {"tc": client})
        monkeypatch.setattr(server, "_state_sessions_meta", {
            "tc": {"socket": "/tmp/tc.sock", "directory": "/d"},
        })

        def hold_ticket_after_stop():
            assert entered.wait(5)
            with server._STATE_TICKET_LOCK:
                holder_has.set()
                release.wait(5)

        holder = threading.Thread(target=hold_ticket_after_stop)
        holder.start()
        ticket_lock.timeouts.clear()
        start = time.monotonic()
        result = server._stop_state_client()
        elapsed = time.monotonic() - start
        assert result and result[0]["deferred"] is True
        assert result[0]["reason"] == "ticket_consume_timeout"
        ticket = result[0]["ticket"]
        # 语义:TICKET 未消费；holder 仍持锁；consume 的 timeout≤budget
        assert server._state_stop_tickets == {ticket}
        assert holder.is_alive()
        assert not server._STATE_TICKET_LOCK.acquire(blocking=False)
        assert ticket_lock.timeouts, "expected TICKET.acquire(timeout=remaining) at consume"
        assert all(t <= budget + 1e-9 for t in ticket_lock.timeouts), ticket_lock.timeouts
        assert elapsed < budget + 2.0  # hang guard only
        release.set()
        holder.join(5)
        assert not holder.is_alive()
        assert server._state_stop_tickets == {ticket}
        assert server._state_retiring == {}
        assert server._state_survivors == {}
        assert server._open_state_clients() is False
        assert server._stop_state_client() == []
        assert not server._state_stop_tickets
        assert server._open_state_clients() is True

    def test_ticket_registration_serializes_before_budget_final_stop_wins(
        self, monkeypatch,
    ):
        """入口 TICKET 是唯一预算例外：stop/open 同时排队时先完成持久
        登记，再获得完整 shutdown budget；无论 open 线性化先后，最终 stop
        关闸且 ticket 被最终 stop 消费。"""
        monkeypatch.setattr(server, "STATE_STOP_JOIN_TIMEOUT_S", 0.1)
        held, release = threading.Event(), threading.Event()
        results: dict[str, object] = {}

        class BudgetProbe(FakeStateClient):
            def __init__(self):
                super().__init__({"reg": "/tmp/reg.sock"})
                self.join_timeouts = []

            def stop(self, join_timeout=5.0):
                self.join_timeouts.append(join_timeout)
                return super().stop(join_timeout)

        client = BudgetProbe()
        client.start()
        monkeypatch.setattr(server, "_state_clients", {"reg": client})
        monkeypatch.setattr(server, "_state_sessions_meta", {
            "reg": {"socket": "/tmp/reg.sock", "directory": "/d"},
        })

        def hold_ticket():
            with server._STATE_TICKET_LOCK:
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold_ticket)
        holder.start()
        assert held.wait(5)
        stop_thread = threading.Thread(
            target=lambda: results.setdefault("stop", server._stop_state_client())
        )
        open_thread = threading.Thread(
            target=lambda: results.setdefault("open", server._open_state_clients())
        )
        stop_thread.start()
        time.sleep(0.15)  # 超过 join budget，仍在线性化前奏等待
        open_thread.start()
        time.sleep(0.05)
        assert stop_thread.is_alive()
        release.set()
        holder.join(5)
        stop_thread.join(5)
        open_thread.join(5)
        assert not holder.is_alive()
        assert not stop_thread.is_alive()
        assert not open_thread.is_alive()
        assert results["stop"] == []
        # 登记等待超过 join budget，但 deadline 从登记成功后才开始。
        assert client.join_timeouts and client.join_timeouts[0] > 0.07
        assert server._state_running is False  # final stop wins
        assert not server._state_stop_tickets
