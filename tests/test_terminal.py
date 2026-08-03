"""terminal.py 生命周期与并发安全测试。

会真实 fork bash PTY;每个用例结束后统一清理,避免泄漏。
"""
import os
import signal
import threading
import time

import pytest

import terminal


@pytest.fixture(autouse=True)
def cleanup_terms():
    yield
    for t in terminal.list_terms():
        terminal.kill_term(t["id"])


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


def test_large_write_full_integrity(tmp_path):
    """大体积写:对端(cat)并发消费时,全部字节必须完整到达,不丢尾。"""
    tid = _create()
    target = tmp_path / "payload.txt"
    terminal.write_term(tid, f"cat > {target}\n")
    _read_until(tid, b"cat >", 5)
    time.sleep(0.5)  # 等 cat 进入读取态
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
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if target.exists() and target.stat().st_size >= len(payload):
                break
            time.sleep(0.1)
        assert target.read_text() == payload
    finally:
        stop.set()
        drainer.join(timeout=2)


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
