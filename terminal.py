"""terminal.py — Web 终端的 PTY 管理。

每个终端会话 = 一个 pty.fork 出的 bash 子进程。
FastAPI WebSocket 双向桥接:浏览器击键→PTY,PTY 输出→浏览器。

支持:多开、resize、Esc/方向键/vim 等完整交互(因为是真 PTY)。

生命周期与并发模型:
  - 创建前校验 cols/rows,活跃终端数有上限(MAX_TERMS),防资源耗尽。
  - 每个会话一把 per-term 锁,read/write/close 都在锁内完成,
    避免 kill 与 read/write 的 fd 竞态(fd 被关闭/复用后误操作)。
  - fd 是非阻塞的:读用 select 轮询;写循环处理短写与 BlockingIOError,
    超过 WRITE_TIMEOUT 抛 TimeoutError(调用方可感知丢失,不静默丢尾)。
  - kill 杀整个进程组(pty.fork 的子进程是新 session leader)+ 关 fd
    (触发 SIGHUP)+ waitpid 回收僵尸。_kill_child 先 waitpid(WNOHANG)
    确认仍是未退出子进程才发信号,防 PID/PGID 复用误杀无关进程。
  - WS 断开不立即杀终端(支持浏览器重连接管);空闲超过 IDLE_TTL 的
    终端由 sweep_idle 回收(create_term 时顺路触发,也可外部定时调用);
    已退出子进程在 DEAD_GRACE 宽限后由 sweep 主动回收,不等 IDLE_TTL。
  - 尾输出契约:进程退出后内核仍保留 PTY 缓冲,read_output/drain_output
    在 alive=False 后仍可读到 EIO;server pump 检测到退出应立即 drain,
    DEAD_GRACE 过后 fd 被 sweep 关闭就无法再读。
"""
from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import termios
import threading
import time
import uuid
from typing import Any

# 终端会话池:term_id -> {master_fd, pid, alive, lock, created_ts, last_active}
_terms: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
# pty.fork 先创建 master fd 再返回父进程；串行化 fork→FD_CLOEXEC，
# 避免并发创建时另一个 shell 在标记前继承该 master fd。
_fork_lock = threading.Lock()

SHELL = os.environ.get("SHELL", "/bin/bash")
HOME = os.path.expanduser("~")

# 窗口尺寸合法范围(防 ioctl 异常值/资源消耗)
MIN_COLS, MAX_COLS = 1, 500
MIN_ROWS, MAX_ROWS = 1, 300
# 最大活跃终端数
MAX_TERMS = 16
# 空闲回收阈值(秒):WS 断开后 PTY 保留供重连,超过才回收
IDLE_TTL = 1800.0
# 已退出子进程的回收宽限(秒):留给 server pump drain 尾输出的窗口
DEAD_GRACE = 60.0
# 单次写 PTY 的最长等待(对端不消费时抛 TimeoutError)
WRITE_TIMEOUT = 2.0


def _valid_dims(cols: Any, rows: Any) -> tuple[int, int]:
    """校验窗口尺寸,非法抛 ValueError。"""
    try:
        c, r = int(cols), int(rows)
    except (TypeError, ValueError):
        raise ValueError(f"cols/rows 必须是整数: {cols!r}/{rows!r}")
    if not (MIN_COLS <= c <= MAX_COLS and MIN_ROWS <= r <= MAX_ROWS):
        raise ValueError(
            f"cols/rows 超出范围({MIN_COLS}-{MAX_COLS}/{MIN_ROWS}-{MAX_ROWS}): {c}/{r}"
        )
    return c, r


def _valid_label(label: Any) -> str | None:
    if label is None:
        return None
    value = str(label).strip()
    if not value or len(value) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("终端名称必须为 1-64 个可见字符")
    return value


def _get(term_id: str) -> dict[str, Any] | None:
    with _lock:
        return _terms.get(term_id)


def _active_count() -> int:
    with _lock:
        return sum(1 for t in _terms.values() if t.get("alive"))


def create_term(
    cwd: str | None = None,
    cols: int = 80,
    rows: int = 24,
    label: str | None = None,
) -> dict[str, Any]:
    """创建一个新终端会话。返回 {id, pid, label}。

    尺寸非法抛 ValueError;活跃终端达上限抛 RuntimeError。
    """
    cols, rows = _valid_dims(cols, rows)
    label = _valid_label(label)
    workdir = cwd or HOME
    # 顺路回收空闲/已死终端,再检查上限
    sweep_idle()
    if _active_count() >= MAX_TERMS:
        raise RuntimeError(f"活跃终端数已达上限 {MAX_TERMS}")

    # pty.fork:子进程返回 0,父进程返回 master_fd + pid
    with _fork_lock:
        pid, master_fd = pty.fork()
        if pid == 0:
            # ── 子进程:exec bash ──
            try:
                os.chdir(workdir)
            except OSError:
                os.chdir(HOME)
            os.environ["TERM"] = "xterm-256color"
            os.execv(SHELL, [SHELL, "-l"])
            os._exit(127)  # execv 失败才到这
        try:
            os.set_inheritable(master_fd, False)
        except OSError:
            _kill_child(pid, master_fd)
            raise
    # ── 父进程 ──
    try:
        # 设非阻塞,便于 select 轮询
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        _set_size(master_fd, cols, rows)
    except OSError:
        _kill_child(pid, master_fd)
        raise
    term_id = uuid.uuid4().hex[:12]
    now = time.monotonic()
    with _lock:
        # fork 耗时内可能有并发创建,复查上限
        if sum(1 for t in _terms.values() if t.get("alive")) >= MAX_TERMS:
            over = True
        else:
            over = False
            _terms[term_id] = {
                "master_fd": master_fd, "pid": pid, "alive": True,
                "lock": threading.Lock(), "created_ts": now, "last_active": now,
                "dead_ts": None, "label": label,
            }
    if over:
        _kill_child(pid, master_fd)
        raise RuntimeError(f"活跃终端数已达上限 {MAX_TERMS}")
    return {"id": term_id, "pid": pid, "label": label}


def _kill_child(pid: int, master_fd: int) -> None:
    """终止子进程并回收:杀进程组 + 关 fd + waitpid。

    先 waitpid(WNOHANG) 确认仍是未退出的子进程才发信号——
    若已退出/已被回收(如 _check_alive),PID/PGID 可能被系统复用,
    此时只关 fd,绝不 kill,防误杀无关进程。
    """
    try:
        result, _ = os.waitpid(pid, os.WNOHANG)
        running = result == 0
    except ChildProcessError:
        running = False
    if running:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass
    try:
        os.close(master_fd)
    except OSError:
        pass


def _set_size(fd: int, cols: int, rows: int) -> None:
    """设 PTY 窗口大小。"""
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def resize_term(term_id: str, cols: int, rows: int) -> None:
    """改终端窗口大小。尺寸非法时忽略(WS 控制消息不应打崩连接)。"""
    try:
        cols, rows = _valid_dims(cols, rows)
    except ValueError:
        return
    t = _get(term_id)
    if not t:
        return
    with t["lock"]:
        if t.get("alive"):
            _set_size(t["master_fd"], cols, rows)


def write_term(term_id: str, data: str) -> None:
    """往终端写击键(浏览器→PTY)。

    fd 非阻塞:循环处理短写,BlockingIOError 时 select 等待可写。
    超过 WRITE_TIMEOUT 抛 TimeoutError——调用方必须能感知尾部未写入,
    不静默丢数据;server WS 侧用 asyncio.to_thread 调用以免阻塞事件循环。
    """
    t = _get(term_id)
    if not t:
        return
    buf = data.encode("utf-8")
    total = len(buf)
    deadline = time.monotonic() + WRITE_TIMEOUT
    with t["lock"]:
        if not t.get("alive"):
            return
        fd = t["master_fd"]
        while buf:
            try:
                n = os.write(fd, buf)
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"write_term 超时({WRITE_TIMEOUT}s):"
                        f"已写入 {total - len(buf)}/{total} 字节,对端未及时消费"
                    )
                try:
                    select.select([], [fd], [], min(0.1, remaining))
                except OSError:
                    raise TimeoutError(
                        f"write_term 等待可写失败:已写入 {total - len(buf)}/{total} 字节"
                    )
                continue
            except OSError as e:
                # 非阻塞写之外的 OSError(如 EIO:终端已关闭):
                # buf 必有剩余,不能静默丢尾,抛可感知异常
                raise ConnectionError(
                    f"write_term 失败(终端可能已关闭):"
                    f"已写入 {total - len(buf)}/{total} 字节: {e}"
                ) from e
            buf = buf[n:]
            t["last_active"] = time.monotonic()


def _read_fd(t: dict[str, Any], timeout: float) -> bytes:
    """在 t['lock'] 内从 master fd 读一次。alive=False 后也允许读(尾输出)。"""
    fd = t["master_fd"]
    try:
        r, _, _ = select.select([fd], [], [], timeout)
        if r:
            data = os.read(fd, 65536)
            t["last_active"] = time.monotonic()
            return data
    except OSError:  # EIO 等:slave 端已全部关闭
        pass
    return b""


def read_output(term_id: str, timeout: float = 0.1) -> bytes:
    """非阻塞读 PTY 输出(PTY→浏览器)。无数据返回 b''。

    进程退出(alive=False)后仍可读:内核保留 PTY 缓冲,直到 EIO 读尽。
    """
    t = _get(term_id)
    if not t:
        return b""
    with t["lock"]:
        return _read_fd(t, timeout)


def drain_output(term_id: str, timeout: float = 0.5) -> bytes:
    """读干 PTY 残余输出(最后一屏),直到 EIO/EOF 或 timeout 无新数据。

    契约:server pump 在 is_alive 返回 False 后应立即调用本函数取尾输出。
    sweep_idle 对已退出进程留有 DEAD_GRACE 宽限;宽限过后 fd 关闭,无法再读。
    """
    t = _get(term_id)
    if not t:
        return b""
    chunks: list[bytes] = []
    with t["lock"]:
        while True:
            data = _read_fd(t, timeout)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks)


def _check_alive(t: dict[str, Any]) -> bool:
    """waitpid WNOHANG 检查子进程。调用方须持有 t['lock']。

    首次从 alive→dead 时记录 dead_ts(单调钟),sweep 按 dead_ts
    计算 DEAD_GRACE 宽限——不按 last_active,否则空闲很久才退出的
    shell 会在退出瞬间被立即清 fd,pump 来不及 drain 尾输出。
    """
    if not t.get("alive"):
        return False
    try:
        result, _ = os.waitpid(t["pid"], os.WNOHANG)
        if result != 0:
            t["alive"] = False
            t["dead_ts"] = time.monotonic()
            return False
        return True
    except ChildProcessError:
        t["alive"] = False
        t["dead_ts"] = time.monotonic()
        return False


def is_alive(term_id: str) -> bool:
    """终端子进程是否还在。"""
    t = _get(term_id)
    if not t:
        return False
    with t["lock"]:
        return _check_alive(t)


def kill_term(term_id: str) -> None:
    """关闭终端:杀进程组 + 关 fd + waitpid 回收 + 移出池。"""
    with _lock:
        t = _terms.pop(term_id, None)
    if not t:
        return
    with t["lock"]:
        t["alive"] = False
        _kill_child(t["pid"], t["master_fd"])


def sweep_idle(max_idle: float = IDLE_TTL) -> int:
    """回收终端,返回回收数量。

    两类回收对象:
      - 已退出的子进程:每次 sweep 主动 waitpid 探测(不等 WS 调 is_alive),
        按 dead_ts(退出时刻)过 DEAD_GRACE 宽限即回收,宽限留给 pump drain 尾输出;
      - 空闲超过 max_idle 秒的存活终端。
    不在单次 WS 断开时调用——断开后 PTY 保留,浏览器可重连;
    由 create_term 顺路触发,也可由外部定时任务调用。
    """
    now = time.monotonic()
    with _lock:
        items = list(_terms.items())
    victims = []
    for tid, t in items:
        with t["lock"]:
            alive = _check_alive(t)
            idle_for = now - t.get("last_active", now)
            dead_ts = t.get("dead_ts")
            dead_for = (now - dead_ts) if dead_ts is not None else None
        if idle_for > max_idle and alive:
            victims.append(tid)
        elif not alive and (dead_for is None or dead_for > DEAD_GRACE):
            victims.append(tid)
    for tid in victims:
        kill_term(tid)
    return len(victims)


def list_terms() -> list[dict[str, Any]]:
    """列出所有终端会话。"""
    now = time.monotonic()
    with _lock:
        return [
            {
                "id": tid, "pid": t["pid"], "alive": t.get("alive", False),
                "idle": round(now - t.get("last_active", now), 1),
                "label": t.get("label"),
            }
            for tid, t in _terms.items()
        ]
