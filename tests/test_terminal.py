"""terminal.py 生命周期与并发安全测试。

会真实 fork bash PTY;每个用例结束后统一清理,避免泄漏。
"""
import os
import shlex
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

import terminal


@pytest.fixture(autouse=True)
def cleanup_terms():
    yield
    for t in terminal.list_terms():
        terminal.kill_term(t["id"])
    with terminal._lock:
        terminal._superseded_terms.clear()


def _create(**kw):
    return terminal.create_term(**kw)["id"]


def _read_until(tid: str, needle: bytes, timeout: float = 8.0) -> bytes:
    """轮询读 PTY 输出直到出现 needle 或超时。"""
    out = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out += terminal.read_output(tid, 0.1)
        if needle in out:
            return out
    return out


def _wait_dead(tid: str, timeout: float = 8.0) -> bytes:
    """等子进程退出,返回期间读到的输出。

    等待期间必须像 server pump 一样持续读:macOS 上控制终端输出未排干时,
    会话首进程会卡在 exiting 状态,waitpid 探测不到退出(Linux 无此问题)。
    """
    out = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and terminal.is_alive(tid):
        out += terminal.read_output(tid, 0.05)
    return out


def _start_file_receiver(tid: str, target, ready) -> None:
    """启动确定性 stdin 接收器；ready 出现后才允许发送测试 payload。

    子进程 exec 可能早于交互 shell 完成 tcsetpgrp；如果在成为前台
    进程组前立即 read(0)，内核会发 SIGTTIN 将它暂停。因此 ready 必须
    在 tcgetpgrp 确认前台权后才创建，否则仍存在极小启动竞态。
    """
    code = "\n".join((
        "import os, pathlib, sys, time",
        "deadline = time.monotonic() + 5",
        "while os.tcgetpgrp(0) != os.getpgrp():",
        "    if time.monotonic() >= deadline:",
        "        raise TimeoutError('receiver did not become foreground')",
        "    time.sleep(0.001)",
        f"pathlib.Path({str(ready)!r}).touch()",
        f"pathlib.Path({str(target)!r}).write_bytes(sys.stdin.buffer.read())",
    ))
    terminal.write_term(tid, f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}\n")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not ready.exists():
        terminal.read_output(tid, 0.05)
    assert ready.exists(), "PTY stdin 接收器未及时启动"


def _wait_file_size(path, size: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size >= size:
            return
        time.sleep(0.05)
    actual = path.stat().st_size if path.exists() else 0
    raise AssertionError(f"文件未及时写完: {actual}/{size}")


# ── 参数校验与上限 ──────────────────────────────────────────────

def test_create_rejects_invalid_dims():
    for cols, rows in [(0, 24), (80, 0), (-1, 24), (80, 10 ** 6),
                       (10 ** 6, 24), ("abc", 24), (None, 24)]:
        with pytest.raises(ValueError):
            terminal.create_term(cols=cols, rows=rows)


def test_create_stores_safe_display_label():
    result = terminal.create_term(label=" agent-cockpit ")
    assert result["label"] == "agent-cockpit"
    listed = next(t for t in terminal.list_terms() if t["id"] == result["id"])
    assert listed["label"] == "agent-cockpit"

    for label in ("", " ", "x" * 65, "bad\nname", "bad\x7fname"):
        with pytest.raises(ValueError, match="名称"):
            terminal.create_term(label=label)


def test_create_can_exec_direct_pty_command_without_login_shell():
    code = "import os; print('direct-pty-ok'); print(os.getcwd())"
    term_id = terminal.create_term(
        cwd="/tmp", command=[sys.executable, "-c", code],
    )["id"]

    output = _wait_dead(term_id)

    assert b"direct-pty-ok" in output
    assert b"/tmp" in output


def test_create_rejects_unsafe_direct_pty_command():
    for command in ([], ["relative"], ["/bin/echo", ""], ["/bin/echo", "bad\0arg"]):
        with pytest.raises(ValueError, match="PTY command"):
            terminal.create_term(command=command)


def test_replace_labeled_term_removes_all_previous_instances():
    first = terminal.create_term(label="demo")["id"]
    second = terminal.create_term(label="demo")["id"]

    replacement = terminal.replace_labeled_term(label="demo")

    matching = [t for t in terminal.list_terms() if t["label"] == "demo"]
    assert [t["id"] for t in matching] == [replacement["id"]]
    assert terminal.is_alive(first) is False
    assert terminal.is_alive(second) is False
    assert terminal.was_superseded(first) is True
    assert terminal.was_superseded(second) is True


def test_concurrent_labeled_replacements_are_serialized(monkeypatch):
    barrier = threading.Barrier(3)
    results = []
    state = {"active": 0, "max_active": 0, "next_id": 0}
    state_lock = threading.Lock()

    def fake_create(cwd, cols, rows, label):
        with state_lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["next_id"] += 1
            term_id = f"term-{state['next_id']}"
        time.sleep(0.05)
        with state_lock:
            state["active"] -= 1
        return {"id": term_id, "pid": 123, "label": label}

    monkeypatch.setattr(terminal, "create_term", fake_create)

    def replace():
        barrier.wait()
        results.append(terminal.replace_labeled_term(label="demo")["id"])

    workers = [threading.Thread(target=replace) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=8)

    assert all(not worker.is_alive() for worker in workers)
    assert sorted(results) == ["term-1", "term-2"]
    assert state["max_active"] == 1


def test_replace_labeled_term_requires_label():
    with pytest.raises(ValueError, match="名称"):
        terminal.replace_labeled_term()


def test_manual_kill_is_not_marked_as_superseded():
    term_id = _create(label="demo")
    terminal.kill_term(term_id)
    assert terminal.was_superseded(term_id) is False


def test_create_marks_master_fd_close_on_exec():
    tid = _create()
    with terminal._lock:
        master_fd = terminal._terms[tid]["master_fd"]
    assert os.get_inheritable(master_fd) is False


def test_create_enforces_max_terms(monkeypatch):
    monkeypatch.setattr(terminal, "MAX_TERMS", 1)
    _create()
    with pytest.raises(RuntimeError, match="上限"):
        _create()


# ── 基本读写与写完整性 ──────────────────────────────────────────

def test_echo_roundtrip():
    tid = _create()
    terminal.write_term(tid, "echo cockpit-$((40+2))\n")
    out = _read_until(tid, b"cockpit-42")
    assert b"cockpit-42" in out
    assert terminal.is_alive(tid)


def test_read_available_waits_once_and_coalesces_ready_chunks(monkeypatch):
    """macOS 的连续小块 PTY 输出应合并，不能每块都走一次 WebSocket。"""
    state = {"lock": threading.Lock()}
    chunks = iter((b"a" * 1024, b"b" * 1024, b"c" * 1024, b""))
    timeouts = []
    monkeypatch.setattr(terminal, "_get", lambda term_id: state)

    def fake_read(current, timeout):
        assert current is state
        timeouts.append(timeout)
        return next(chunks)

    monkeypatch.setattr(terminal, "_read_fd", fake_read)

    data = terminal.read_available("term", timeout=0.02, max_bytes=4096)

    assert data == b"a" * 1024 + b"b" * 1024 + b"c" * 1024
    assert timeouts == [0.02, 0, 0, 0]


def test_read_available_obeys_burst_soft_limit(monkeypatch):
    state = {"lock": threading.Lock()}
    chunks = iter((b"a" * 1024, b"b" * 1024, b"c" * 1024))
    monkeypatch.setattr(terminal, "_get", lambda term_id: state)
    monkeypatch.setattr(terminal, "_read_fd", lambda current, timeout: next(chunks))

    assert terminal.read_available("term", max_bytes=2048) == (
        b"a" * 1024 + b"b" * 1024
    )


def test_output_history_is_bounded_and_keeps_latest_bytes(monkeypatch):
    monkeypatch.setattr(terminal, "OUTPUT_HISTORY_MAX", 8)
    state = {"output_history": bytearray()}

    terminal._remember_output(state, b"abcdef")
    terminal._remember_output(state, b"ghijkl")

    assert bytes(state["output_history"]) == b"efghijkl"


def test_read_fd_records_output_for_browser_replay(monkeypatch):
    state = {"master_fd": 9, "output_history": bytearray()}
    monkeypatch.setattr(terminal.select, "select", lambda *args: ([9], [], []))
    monkeypatch.setattr(terminal.os, "read", lambda fd, size: b"current screen")

    assert terminal._read_fd(state, 0) == b"current screen"
    assert bytes(state["output_history"]) == b"current screen"


def test_websocket_pump_uses_bursts_without_per_chunk_sleep():
    source = (Path(__file__).resolve().parents[1] / "server.py").read_text()
    pump = source.split("async def pump_out():", 1)[1].split(
        "pump_task = asyncio.create_task", 1
    )[0]

    assert "terminal.read_available" in pump
    assert "TERM_READ_WAIT" in pump
    assert "TERM_READ_BURST" in pump
    assert "asyncio.sleep(0.02)" not in pump


def test_large_write_full_integrity(tmp_path, monkeypatch):
    """大体积写:接收器并发消费时,全部字节必须完整到达,不丢尾。"""
    # CI runner 负载高时 drainer 消费慢,2s 默认超时会把本用例打成 flake;
    # 放宽到与下方完整性等待相同的 10s(只放宽本用例,不改产品默认值)。
    monkeypatch.setattr(terminal, "WRITE_TIMEOUT", 10.0)
    tid = _create()
    target = tmp_path / "payload.txt"
    _start_file_receiver(tid, target, tmp_path / "receiver-ready")
    # 模拟 server pump 持续读走回显:canonical 模式输入会回显到 master,
    # 没人读时 macOS 的小 PTY 缓冲会反压输入(Linux 缓冲大,不显性)
    stop = threading.Event()
    def _drain():
        while not stop.is_set():
            terminal.read_output(tid, 0.05)
    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()
    try:
        # 每行 < 4096(canonical MAX_CANON),总量超过 PTY 缓冲,触发短写循环
        payload = "".join(f"{i:05d}" + "x" * 95 + "\n" for i in range(2000))
        terminal.write_term(tid, payload)   # 不得 TimeoutError
        terminal.write_term(tid, "\x04")    # Ctrl-D EOF
        _wait_file_size(target, len(payload))
        assert target.read_text() == payload
    finally:
        stop.set()
        drainer.join(timeout=2)


def test_concurrent_writes_preserve_message_boundaries(tmp_path, monkeypatch):
    """两个 WebSocket/重连并发写同一 PTY 时，每条消息不得字节级交错。"""
    monkeypatch.setattr(terminal, "WRITE_TIMEOUT", 10.0)
    tid = _create()
    target = tmp_path / "concurrent.txt"
    _start_file_receiver(tid, target, tmp_path / "concurrent-ready")

    real_write = os.write

    def one_byte_at_a_time(fd, data):
        time.sleep(0.0001)
        return real_write(fd, data[:1])

    monkeypatch.setattr(os, "write", one_byte_at_a_time)
    payloads = ("A" * 512 + "\n", "B" * 512 + "\n")
    barrier = threading.Barrier(3)
    errors = []

    def writer(payload):
        try:
            barrier.wait()
            terminal.write_term(tid, payload)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(payload,)) for payload in payloads]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(10)
    assert not errors
    assert not any(thread.is_alive() for thread in threads)

    terminal.write_term(tid, "\x04")
    _wait_file_size(target, sum(len(payload) for payload in payloads))
    data = target.read_text()
    assert data in (payloads[0] + payloads[1], payloads[1] + payloads[0])


def test_write_timeout_raises_and_reports(monkeypatch):
    """对端不消费时,写超时必须抛 TimeoutError(调用方可感知),而非静默丢尾。"""
    tid = _create()
    monkeypatch.setattr(terminal, "WRITE_TIMEOUT", 0.2)

    def always_block(fd, data):
        raise BlockingIOError()

    monkeypatch.setattr(os, "write", always_block)
    with pytest.raises(TimeoutError, match="已写入"):
        terminal.write_term(tid, "y" * 1000)


def test_ops_on_missing_term_are_noop():
    terminal.write_term("no-such-id", "x")
    assert terminal.read_output("no-such-id") == b""
    assert terminal.drain_output("no-such-id") == b""
    assert not terminal.is_alive("no-such-id")
    terminal.kill_term("no-such-id")  # 不抛异常
    terminal.resize_term("no-such-id", 80, 24)


# ── kill 与回收 ─────────────────────────────────────────────────

def test_kill_reaps_process():
    tid = _create()
    pid = next(t["pid"] for t in terminal.list_terms() if t["id"] == tid)
    terminal.kill_term(tid)
    assert not terminal.is_alive(tid)
    assert all(t["id"] != tid for t in terminal.list_terms())
    # 已 waitpid 回收:再 waitpid 应 ChildProcessError, kill 应 ProcessLookupError
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_kill_child_skips_signal_when_already_reaped(monkeypatch):
    """PID 复用防护:子进程已被回收时,_kill_child 只关 fd,绝不发信号。"""
    calls = []
    monkeypatch.setattr(os, "waitpid",
                        lambda *a, **k: (_ for _ in ()).throw(ChildProcessError()))
    monkeypatch.setattr(os, "killpg", lambda p, s: calls.append(("killpg", p, s)))
    monkeypatch.setattr(os, "kill", lambda p, s: calls.append(("kill", p, s)))
    fd = os.open("/dev/null", os.O_RDONLY)
    terminal._kill_child(999999, fd)
    assert calls == []
    with pytest.raises(OSError):  # fd 已关闭
        os.fstat(fd)


@pytest.mark.parametrize("pid", [1, True, None])
def test_kill_child_rejects_unsafe_pid_without_waiting_or_signalling(monkeypatch, pid):
    """无效 PID 只关 fd，不能让 killpg(1) 退化成 kill(-1) 误杀进程。"""
    monkeypatch.setattr(
        os, "waitpid", lambda *a, **k: (_ for _ in ()).throw(AssertionError("waitpid"))
    )
    monkeypatch.setattr(
        os, "killpg", lambda *a, **k: (_ for _ in ()).throw(AssertionError("killpg"))
    )
    monkeypatch.setattr(
        os, "kill", lambda *a, **k: (_ for _ in ()).throw(AssertionError("kill"))
    )
    fd = os.open("/dev/null", os.O_RDONLY)

    terminal._kill_child(pid, fd)

    with pytest.raises(OSError):
        os.fstat(fd)


def test_kill_child_signals_running_child(monkeypatch):
    """子进程仍在运行(waitpid 返回 0)时,正常 killpg + 等待回收。"""
    calls = []
    states = iter([(0, 0), (12345, 0)])
    monkeypatch.setattr(os, "waitpid", lambda *a, **k: next(states))
    monkeypatch.setattr(os, "killpg", lambda p, s: calls.append(("killpg", p, s)))
    fd = os.open("/dev/null", os.O_RDONLY)
    terminal._kill_child(12345, fd)
    assert calls == [("killpg", 12345, signal.SIGKILL)]
    with pytest.raises(OSError):
        os.fstat(fd)


def test_kill_wins_race_with_concurrent_io():
    # 读写线程与 kill 并发:不得抛异常(fd 竞态由 per-term 锁保护)
    tid = _create()
    stop = threading.Event()
    errors = []

    def io_loop():
        try:
            while not stop.is_set():
                terminal.read_output(tid, 0.02)
                terminal.write_term(tid, "")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=io_loop) for _ in range(4)]
    for th in threads:
        th.start()
    time.sleep(0.2)
    terminal.kill_term(tid)
    stop.set()
    for th in threads:
        th.join(5)
    assert not errors


def test_kill_waits_for_inflight_write(monkeypatch):
    """close 必须等完整消息写完，不能让 fd 关闭/复用污染剩余输入。"""
    tid = _create()
    real_write = os.write
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def paused_write(fd, data):
        entered.set()
        assert release.wait(5)
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", paused_write)

    def writer():
        try:
            terminal.write_term(tid, "printf x\n")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    write_thread = threading.Thread(target=writer)
    write_thread.start()
    assert entered.wait(5)
    kill_thread = threading.Thread(target=terminal.kill_term, args=(tid,))
    kill_thread.start()
    time.sleep(0.05)
    assert kill_thread.is_alive(), "kill 未等待正在写入的完整消息"
    release.set()
    write_thread.join(5)
    kill_thread.join(5)

    assert not errors
    assert not write_thread.is_alive()
    assert not kill_thread.is_alive()
    assert not terminal.is_alive(tid)


# ── 空闲/死进程回收(支持 WS 断开重连) ─────────────────────────

def test_sweep_reaps_idle_but_keeps_fresh():
    idle_tid = _create()
    fresh_tid = _create()
    with terminal._lock:
        terminal._terms[idle_tid]["last_active"] -= terminal.IDLE_TTL + 10
    reaped = terminal.sweep_idle()
    assert reaped >= 1
    remaining = {t["id"] for t in terminal.list_terms()}
    assert idle_tid not in remaining
    assert fresh_tid in remaining
    assert terminal.is_alive(fresh_tid)


def test_sweep_reaps_exited_children_promptly(monkeypatch):
    """已退出的子进程(没有 WS 调 is_alive)由 sweep 主动探测并回收,不等 IDLE_TTL。"""
    monkeypatch.setattr(terminal, "DEAD_GRACE", 0)
    tid = _create()
    terminal.write_term(tid, "exit\n")
    _wait_dead(tid)
    assert not terminal.is_alive(tid)
    # max_idle 很大:存活终端不受影响,但死进程必须被回收
    assert terminal.sweep_idle(max_idle=10 ** 9) >= 1
    assert all(t["id"] != tid for t in terminal.list_terms())


def test_list_terms_refreshes_cached_child_liveness(monkeypatch):
    state = {
        "pid": 4321, "alive": True, "dead_ts": None,
        "last_active": terminal.time.monotonic(), "label": "demo",
        "lock": threading.Lock(),
    }
    monkeypatch.setattr(terminal.os, "waitpid", lambda pid, flags: (pid, 0))
    with terminal._lock:
        terminal._terms["stale"] = state
    try:
        listed = terminal.list_terms()
    finally:
        with terminal._lock:
            terminal._terms.pop("stale", None)

    assert listed[0]["id"] == "stale"
    assert listed[0]["alive"] is False


def test_sweep_respects_custom_ttl():
    tid = _create()
    assert terminal.sweep_idle(max_idle=3600) == 0  # 刚创建,不应被回收
    assert terminal.is_alive(tid)
    assert terminal.sweep_idle(max_idle=0) >= 1     # TTL=0 全部回收
    assert not terminal.is_alive(tid)


# ── 尾输出 drain 契约 ───────────────────────────────────────────

def test_drain_reads_tail_after_exit():
    """短命进程:alive=False 后仍能从 master fd 读到最后一屏(直到 EIO)。"""
    tid = _create()
    terminal.write_term(tid, "printf 'tail-marker-xyz'; exit\n")
    out = _wait_dead(tid)  # 泵读等待;读到的头部与 drain 的尾部合并校验
    assert not terminal.is_alive(tid)
    out += terminal.drain_output(tid)
    assert b"tail-marker-xyz" in out
    # 读尽后再读返回空(EIO/EOF)
    assert terminal.read_output(tid, 0.1) == b""


def test_drain_output_is_bounded_when_background_writer_never_closes(monkeypatch):
    tid = _create()
    calls = 0

    def endless_output(term, timeout):
        nonlocal calls
        calls += 1
        return b"x" * 65536

    monkeypatch.setattr(terminal, "_read_fd", endless_output)
    monkeypatch.setattr(terminal, "DRAIN_MAX_SECONDS", 0.01)
    monkeypatch.setattr(terminal, "DRAIN_MAX_BYTES", 128 * 1024)
    started = time.monotonic()

    output = terminal.drain_output(tid, timeout=0)

    assert time.monotonic() - started < 0.5
    assert calls > 1
    assert len(output) == 128 * 1024


def test_resize_ignores_invalid_dims():
    tid = _create()
    terminal.resize_term(tid, -5, 24)   # 不抛异常
    terminal.resize_term(tid, 80, 10 ** 9)
    terminal.resize_term(tid, 100, 40)  # 合法值正常生效
    assert terminal.is_alive(tid)



# ── dead_ts 宽限与写错误感知(第二轮) ──────────────────────────

def test_sweep_grace_counts_from_death_not_last_active():
    """空闲很久才退出的 shell:宽限从退出时刻(dead_ts)起算,不能退出即清 fd。"""
    tid = _create()
    with terminal._lock:
        terminal._terms[tid]["last_active"] -= 10 ** 6  # 模拟长期空闲
    terminal.write_term(tid, "exit\n")
    _wait_dead(tid)
    assert not terminal.is_alive(tid)
    # idle_for 巨大但刚退出:DEAD_GRACE 内不得回收(pump 还要 drain)
    assert terminal.sweep_idle(max_idle=10 ** 9) == 0
    assert any(t["id"] == tid for t in terminal.list_terms())
    # 过了宽限才回收
    with terminal._lock:
        terminal._terms[tid]["dead_ts"] -= terminal.DEAD_GRACE + 1
    assert terminal.sweep_idle(max_idle=10 ** 9) >= 1
    assert all(t["id"] != tid for t in terminal.list_terms())


def test_write_oserror_raises_connectionerror(monkeypatch):
    """非阻塞写之外的 OSError(如 EIO):有剩余 buf 时必须抛 ConnectionError。"""
    tid = _create()

    def always_fail(fd, data):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "write", always_fail)
    with pytest.raises(ConnectionError, match="已写入"):
        terminal.write_term(tid, "z" * 100)


def test_write_term_dups_fd_under_lock_and_closes_copy(monkeypatch):
    """锁外写入必须用锁内 dup 的副本,写完关闭副本,避免 fd 复用误写。

    write_term 在锁内只取 master_fd 引用,锁外另一线程 kill_term 会关 fd,
    该 fd 号可能被系统复用于新文件,后续 os.write 就写到错误对象上。
    """
    tid = _create()
    with terminal._lock:
        master_fd = terminal._terms[tid]["master_fd"]
    dup_fd = os.dup(master_fd)

    dup_calls = []
    monkeypatch.setattr(os, "dup", lambda source: dup_calls.append(source) or dup_fd)
    written = []
    real_write = os.write
    monkeypatch.setattr(
        os, "write",
        lambda fd, data: written.append(fd) or real_write(fd, data),
    )
    closed = []
    real_close = os.close
    monkeypatch.setattr(
        os, "close",
        lambda fd: closed.append(fd) or real_close(fd),
    )

    terminal.write_term(tid, "echo dup-fd-ok\n")
    out = _read_until(tid, b"dup-fd-ok")
    assert b"dup-fd-ok" in out
    assert dup_calls == [master_fd]
    assert written and all(fd == dup_fd for fd in written)
    assert dup_fd in closed
    # master fd 本身未被关闭(副本关闭不影响它)
    os.fstat(master_fd)
