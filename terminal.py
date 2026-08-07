"""terminal.py — Web 终端的 PTY 管理。

每个终端会话 = 一个 pty.fork 出的 bash 子进程。
FastAPI WebSocket 双向桥接:浏览器击键→PTY,PTY 输出→浏览器。

支持:多开、resize、Esc/方向键/vim 等完整交互(因为是真 PTY)。

生命周期与并发模型:
  - 创建前校验 cols/rows,活跃终端数有上限(MAX_TERMS),防资源耗尽。
  - 每个会话用状态锁保护 read/resize/close，再用独立 write_lock 串行完整输入消息；
    避免双 WebSocket/重连并发写发生字节级交错，同时不阻塞输出泵排干回显。
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
import json
import os
import pty
import select
import signal
import struct
import tempfile
import termios
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# 终端会话池:term_id -> {master_fd, pid, alive, lock, created_ts, last_active}
_terms: dict[str, dict[str, Any]] = {}
# 被“较新页面接管”替换的 term_id 临时保留原因，供仍连接旧 ID 的 WebSocket
# 返回 taken-over 而不是普通失效；否则旧版页面会自动重建并与新页面反复争抢。
_superseded_terms: dict[str, float] = {}
# 浏览器经 Web PTY 向某 Herdr session 最近一次发送用户输入的时间。
_session_user_input: dict[str, float] = {}
# 同一份状态的落盘副本(墙钟时间),供 mail-send 等独立进程在注入
# pane 通知前避让正在输入的用户。限频写,30s 窗口下秒级延迟无影响。
_TYPING_STATE_PATH = (
    Path.home() / ".local" / "state" / "agent-cockpit" / "typing.json"
)
_typing_state_last_write = 0.0
_lock = threading.Lock()
# pty.fork 先创建 master fd 再返回父进程；串行化 fork→FD_CLOEXEC，
# 避免并发创建时另一个 shell 在标记前继承该 master fd。
_fork_lock = threading.Lock()
# 同一 Herdr session 的 Web PTY 由 label 标识。跨浏览器显式打开时必须把
# “关闭旧 PTY + 创建新 PTY”串行化，否则两个页面会基于各自缓存各建一份。
_replace_label_lock = threading.Lock()

SHELL = os.environ.get("SHELL", "/bin/bash")
HOME = os.path.expanduser("~")

# 窗口尺寸合法范围(防 ioctl 异常值/资源消耗)
MIN_COLS, MAX_COLS = 1, 500
MIN_ROWS, MAX_ROWS = 1, 300
# 最大活跃终端数(默认值;设置页 term.max_terms 可覆盖)
MAX_TERMS = 16
# 空闲回收阈值(秒):WS 断开后 PTY 保留供重连,超过才回收(设置页 term.idle_ttl 可覆盖)
IDLE_TTL = 1800.0
# 已退出子进程的回收宽限(秒):留给 server pump drain 尾输出的窗口
DEAD_GRACE = 60.0
SUPERSEDED_TTL = 3600.0
# 单次写 PTY 的最长等待(对端不消费时抛 TimeoutError;设置页 term.write_timeout 可覆盖)
WRITE_TIMEOUT = 2.0
# 单次 WebSocket 输出合并上限；macOS PTY 常以约 1 KiB 返回，若逐块转发会把
# 服务端调度和浏览器重绘放大成明显的逐行加载。
READ_BURST_MAX = 256 * 1024
# 新 xterm 在浏览器刷新后需要重放输出才能恢复当前屏幕；每个 PTY 只保留尾部。
OUTPUT_HISTORY_MAX = 1024 * 1024
# 退出后的 PTY 可能仍有后台进程持续写；drain 必须同时限制内存和总时长。
DRAIN_MAX_BYTES = 1024 * 1024
DRAIN_MAX_SECONDS = 2.0
USER_TYPING_WINDOW = 30.0


def _term_cfg(key: str, default: float) -> float:
    """读设置页的终端参数;settings 不可用时用模块常量兜底。"""
    try:
        import settings
        return settings.term_setting(key, default)
    except Exception:
        return default


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
    command: list[str] | None = None,
) -> dict[str, Any]:
    """创建一个新终端会话。返回 {id, pid, label}。

    command 仅供服务端内部直接启动需要 PTY 的程序，避免自动化依赖用户 shell
    初始化提示；普通 Web 终端仍启动登录 shell。尺寸非法抛 ValueError；活跃终端
    达上限抛 RuntimeError。
    """
    cols, rows = _valid_dims(cols, rows)
    label = _valid_label(label)
    if command is not None:
        if (
            not isinstance(command, list) or not command
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command)
            or not os.path.isabs(command[0])
        ):
            raise ValueError("PTY command 必须是非空参数列表，且可执行文件使用绝对路径")
        command = list(command)
    workdir = cwd or HOME
    max_terms = int(_term_cfg("max_terms", MAX_TERMS))
    # 顺路回收空闲/已死终端,再检查上限
    sweep_idle()
    if _active_count() >= max_terms:
        raise RuntimeError(f"活跃终端数已达上限 {max_terms}")

    # pty.fork:子进程返回 0,父进程返回 master_fd + pid
    with _fork_lock:
        pid, master_fd = pty.fork()
        if pid == 0:
            # ── 子进程:exec 登录 shell 或服务端指定的交互程序 ──
            try:
                os.chdir(workdir)
            except OSError:
                os.chdir(HOME)
            os.environ["TERM"] = "xterm-256color"
            argv = command or [SHELL, "-l"]
            os.execv(argv[0], argv)
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
        if sum(1 for t in _terms.values() if t.get("alive")) >= max_terms:
            over = True
        else:
            over = False
            _terms[term_id] = {
                "master_fd": master_fd, "pid": pid, "alive": True,
                "lock": threading.Lock(), "write_lock": threading.Lock(),
                "created_ts": now, "last_active": now,
                "dead_ts": None, "label": label, "output_history": bytearray(),
            }
    if over:
        _kill_child(pid, master_fd)
        raise RuntimeError(f"活跃终端数已达上限 {max_terms}")
    return {"id": term_id, "pid": pid, "label": label}


def replace_labeled_term(
    cwd: str | None = None,
    cols: int = 80,
    rows: int = 24,
    label: str | None = None,
) -> dict[str, Any]:
    """串行替换同 label 的 PTY，供 Herdr session 显式接管使用。"""
    normalized = _valid_label(label)
    if normalized is None:
        raise ValueError("替换终端必须提供名称")
    with _replace_label_lock:
        with _lock:
            victims = [
                term_id
                for term_id, state in _terms.items()
                if state.get("label") == normalized
            ]
        for term_id in victims:
            kill_term(term_id, superseded=True)
        return create_term(cwd, cols, rows, normalized)


def _kill_child(pid: int, master_fd: int) -> None:
    """终止子进程并回收:杀进程组 + 关 fd + waitpid。

    先 waitpid(WNOHANG) 确认仍是未退出的子进程才发信号——
    若已退出/已被回收(如 _check_alive),PID/PGID 可能被系统复用,
    此时只关 fd,绝不 kill,防误杀无关进程。
    PID 必须是大于 1 的原生 int；拒绝 bool/Mock/1，避免 killpg(1)
    变成 kill(-1) 后误杀当前用户可达的全部进程。
    """
    can_signal_group = type(pid) is int and pid > 1
    running = False
    if can_signal_group:
        try:
            result, _ = os.waitpid(pid, os.WNOHANG)
            running = result == 0
        except ChildProcessError:
            pass
    if running:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    # 先关 master fd 再阻塞回收:macOS 的会话首进程退出时要等控制终端
    # 输出排干,master 开着且无人读时子进程会卡在 exiting 状态,
    # waitpid 随之永久阻塞(Linux 无此行为)
    try:
        os.close(master_fd)
    except OSError:
        pass
    if running:
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
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

    并发安全:锁内 dup 一份 fd 副本,锁外写副本,写毕关闭。若直接持有
    master_fd 引用,另一线程 kill_term 关 fd 后该 fd 号可能被系统复用于
    新文件,锁外 os.write 就写到了错误对象上;副本杜绝 fd 复用误写。
    """
    t = _get(term_id)
    if not t:
        return
    buf = data.encode("utf-8")
    if not buf:
        return
    total = len(buf)
    with t["write_lock"]:
        write_timeout = float(_term_cfg("write_timeout", WRITE_TIMEOUT))
        deadline = time.monotonic() + write_timeout
        with t["lock"]:
            if not t.get("alive"):
                return
            # dup 一份 fd 副本:锁外写副本。若直接持有 master_fd 引用，另一线程
            # kill_term 关 fd 后该 fd 号可能被系统复用于新文件，锁外 os.write 就
            # 写到了错误对象上；副本杜绝 fd 复用误写。
            fd = os.dup(t["master_fd"])
        try:
            # 写循环不持有 t["lock"]:canonical 模式输入会回显到 master,macOS 的
            # PTY 缓冲小,写入被回显反压时必须让读端(pump)能并发排干,否则大体积
            # 写必然等到超时;write_lock 只串行写者，不阻塞读端。
            while buf:
                try:
                    n = os.write(fd, buf)
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"write_term 超时({write_timeout}s):"
                            f"已写入 {total - len(buf)}/{total} 字节,对端未及时消费"
                        )
                    try:
                        select.select([], [fd], [], min(0.1, remaining))
                    except OSError:
                        raise TimeoutError(
                            f"write_term 等待可写失败:"
                            f"已写入 {total - len(buf)}/{total} 字节"
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
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


def _remember_output(t: dict[str, Any], data: bytes) -> None:
    if not data:
        return
    history = t.setdefault("output_history", bytearray())
    history.extend(data)
    if OUTPUT_HISTORY_MAX <= 0:
        history.clear()
    elif len(history) > OUTPUT_HISTORY_MAX:
        del history[:-OUTPUT_HISTORY_MAX]


def _read_fd(t: dict[str, Any], timeout: float) -> bytes:
    """在 t['lock'] 内从 master fd 读一次。alive=False 后也允许读(尾输出)。"""
    fd = t["master_fd"]
    try:
        r, _, _ = select.select([fd], [], [], timeout)
        if r:
            data = os.read(fd, 65536)
            t["last_active"] = time.monotonic()
            _remember_output(t, data)
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


def output_history(term_id: str) -> bytes:
    """返回浏览器新 xterm 重建屏幕所需的有界尾输出。"""
    t = _get(term_id)
    if not t:
        return b""
    with t["lock"]:
        return bytes(t.get("output_history") or b"")


def read_available(
    term_id: str,
    timeout: float = 0.02,
    max_bytes: int = READ_BURST_MAX,
) -> bytes:
    """等待第一块输出后，立即合并当前已就绪的数据。

    macOS PTY 即使一次写入很大，也常拆成约 1 KiB 的连续 read。这里仅对第一块
    使用 timeout，后续用零等待排空并设置软上限，减少 WebSocket 消息和前端
    xterm.write 次数；全程持状态锁，避免 kill/close 后读取复用 fd。
    """
    t = _get(term_id)
    if not t or max_bytes <= 0:
        return b""
    chunks: list[bytes] = []
    total = 0
    with t["lock"]:
        data = _read_fd(t, timeout)
        if not data:
            return b""
        chunks.append(data)
        total += len(data)
        while total < max_bytes:
            data = _read_fd(t, 0)
            if not data:
                break
            chunks.append(data)
            total += len(data)
    return b"".join(chunks)


def drain_output(term_id: str, timeout: float = 0.5) -> bytes:
    """读干 PTY 残余输出(最后一屏),直到 EIO/EOF 或 timeout 无新数据。

    契约:server pump 在 is_alive 返回 False 后应立即调用本函数取尾输出。
    sweep_idle 对已退出进程留有 DEAD_GRACE 宽限;宽限过后 fd 关闭,无法再读。
    """
    t = _get(term_id)
    if not t:
        return b""
    output = bytearray()
    deadline = time.monotonic() + DRAIN_MAX_SECONDS
    with t["lock"]:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            data = _read_fd(t, min(timeout, max(0.0, remaining)))
            if not data:
                break
            output.extend(data)
            overflow = len(output) - DRAIN_MAX_BYTES
            if overflow > 0:
                del output[:overflow]
    return bytes(output)


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


def kill_term(term_id: str, *, superseded: bool = False) -> None:
    """关闭终端:杀进程组 + 关 fd + waitpid 回收 + 移出池。"""
    with _lock:
        t = _terms.pop(term_id, None)
        if t and superseded:
            _superseded_terms[term_id] = time.monotonic()
    if not t:
        return
    # 与 write_term 相同的锁序:先写锁、后状态锁。close 等当前完整消息写完，
    # 防止 master fd 在短写循环中途关闭并被系统复用到无关文件。
    with t["write_lock"]:
        with t["lock"]:
            t["alive"] = False
            _kill_child(t["pid"], t["master_fd"])


def was_superseded(term_id: str) -> bool:
    """终端是否因同 label 的较新显式打开而被替换。"""
    now = time.monotonic()
    with _lock:
        replaced_at = _superseded_terms.get(term_id)
        if replaced_at is None:
            return False
        if now - replaced_at > SUPERSEDED_TTL:
            _superseded_terms.pop(term_id, None)
            return False
        return True


def sweep_idle(max_idle: float | None = None) -> int:
    """回收终端,返回回收数量。

    max_idle 为 None 时读设置页 term.idle_ttl(默认 IDLE_TTL)。

    两类回收对象:
      - 已退出的子进程:每次 sweep 主动 waitpid 探测(不等 WS 调 is_alive),
        按 dead_ts(退出时刻)过 DEAD_GRACE 宽限即回收,宽限留给 pump drain 尾输出;
      - 空闲超过 max_idle 秒的存活终端。
    不在单次 WS 断开时调用——断开后 PTY 保留,浏览器可重连;
    由 create_term 顺路触发,也可由外部定时任务调用。
    """
    if max_idle is None:
        max_idle = float(_term_cfg("idle_ttl", IDLE_TTL))
    now = time.monotonic()
    with _lock:
        expired_superseded = [
            term_id
            for term_id, replaced_at in _superseded_terms.items()
            if now - replaced_at > SUPERSEDED_TTL
        ]
        for term_id in expired_superseded:
            _superseded_terms.pop(term_id, None)
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


def _persist_typing_state(session: str) -> None:
    """把本次击键的墙钟时间写入状态文件(限频,原子替换)。"""
    global _typing_state_last_write
    now_mono = time.monotonic()
    if now_mono - _typing_state_last_write < 1.0:
        return
    _typing_state_last_write = now_mono
    try:
        _TYPING_STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data: dict[str, float] = {}
        try:
            loaded = json.loads(_TYPING_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = {str(k): float(v) for k, v in loaded.items()}
        except (OSError, ValueError, TypeError):
            data = {}
        wall = time.time()
        data[session] = wall
        # 清理过期项,避免文件长期增长
        data = {k: v for k, v in data.items() if wall - v < 24 * 3600}
        fd, tmp_name = tempfile.mkstemp(
            prefix=".typing.", suffix=".tmp", dir=str(_TYPING_STATE_PATH.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_name, _TYPING_STATE_PATH)
    except OSError:
        pass


def note_user_input(term_id: str) -> str | None:
    """记录 Web PTY 用户输入；返回对应 Herdr session，无标签则 None。"""
    now = time.monotonic()
    with _lock:
        state = _terms.get(term_id)
        label = state.get("label") if state else None
        if not label:
            return None
        _session_user_input[label] = now
        stale = [
            session for session, ts in _session_user_input.items()
            if now - ts >= USER_TYPING_WINDOW
        ]
        for session in stale:
            _session_user_input.pop(session, None)
    _persist_typing_state(label)
    return label


def user_typing_recently(session: str) -> bool:
    """该 Herdr session 在避让窗口内是否收到过 Web PTY 用户输入。"""
    now = time.monotonic()
    with _lock:
        ts = _session_user_input.get(session)
        if ts is None:
            return False
        if now - ts < USER_TYPING_WINDOW:
            return True
        _session_user_input.pop(session, None)
        return False


def list_terms() -> list[dict[str, Any]]:
    """列出所有终端会话。"""
    now = time.monotonic()
    with _lock:
        items = list(_terms.items())
    result = []
    for tid, t in items:
        with t["lock"]:
            alive = _check_alive(t)
            result.append({
                "id": tid, "pid": t["pid"], "alive": alive,
                "idle": round(now - t.get("last_active", now), 1),
                "label": t.get("label"),
            })
    return result
