"""terminal.py — Web 终端的 PTY 管理。

每个终端会话 = 一个 pty.fork 出的 bash 子进程。
FastAPI WebSocket 双向桥接:浏览器击键→PTY,PTY 输出→浏览器。

支持:多开、resize、Esc/方向键/vim 等完整交互(因为是真 PTY)。
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import select
import signal
import struct
import termios
import threading
import uuid
from typing import Any

# 终端会话池:term_id -> {master_fd, pid, busy}
_terms: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()

SHELL = os.environ.get("SHELL", "/bin/bash")
HOME = os.path.expanduser("~")


def create_term(cwd: str | None = None, cols: int = 80, rows: int = 24) -> dict[str, Any]:
    """创建一个新终端会话。返回 {id, pid}。"""
    term_id = uuid.uuid4().hex[:12]
    workdir = cwd or HOME

    # pty.fork:子进程返回 0,父进程返回 master_fd + pid
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
    # ── 父进程 ──
    # 设非阻塞,便于 select 轮询
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    _set_size(master_fd, cols, rows)
    with _lock:
        _terms[term_id] = {"master_fd": master_fd, "pid": pid, "alive": True}
    return {"id": term_id, "pid": pid}


def _set_size(fd: int, cols: int, rows: int) -> None:
    """设 PTY 窗口大小。"""
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def resize_term(term_id: str, cols: int, rows: int) -> None:
    """改终端窗口大小。"""
    with _lock:
        t = _terms.get(term_id)
    if t:
        _set_size(t["master_fd"], cols, rows)


def write_term(term_id: str, data: str) -> None:
    """往终端写击键(浏览器→PTY)。"""
    with _lock:
        t = _terms.get(term_id)
    if t:
        try:
            os.write(t["master_fd"], data.encode("utf-8"))
        except OSError:
            pass


def read_output(term_id: str, timeout: float = 0.1) -> bytes:
    """非阻塞读 PTY 输出(PTY→浏览器)。无数据返回 b''。"""
    with _lock:
        t = _terms.get(term_id)
    if not t:
        return b""
    fd = t["master_fd"]
    try:
        r, _, _ = select.select([fd], [], [], timeout)
        if r:
            return os.read(fd, 65536)
    except OSError:
        pass
    return b""


def is_alive(term_id: str) -> bool:
    """终端子进程是否还在。"""
    with _lock:
        t = _terms.get(term_id)
    if not t:
        return False
    pid = t["pid"]
    try:
        # waitpid WNOHANG:0=还在,>0=已退出
        result, _ = os.waitpid(pid, os.WNOHANG)
        if result != 0:
            with _lock:
                t["alive"] = False
            return False
        return True
    except ChildProcessError:
        with _lock:
            t["alive"] = False
        return False


def kill_term(term_id: str) -> None:
    """关闭终端:kill 子进程 + 关 fd + 移出池。"""
    with _lock:
        t = _terms.pop(term_id, None)
    if not t:
        return
    try:
        os.kill(t["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.close(t["master_fd"])
    except OSError:
        pass


def list_terms() -> list[dict[str, Any]]:
    """列出所有终端会话。"""
    with _lock:
        return [
            {"id": tid, "pid": t["pid"], "alive": t.get("alive", False)}
            for tid, t in _terms.items()
        ]
