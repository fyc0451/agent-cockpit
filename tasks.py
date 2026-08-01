"""tasks.py — 任务执行层:codex exec subprocess + 状态机 + 输出流捕获。

每个任务是独立的 codex exec 进程,后台异步跑,输出实时捕获到内存缓冲 + tasks.sqlite。
前端通过轮询或 SSE 看进度。

状态机:pending → running → done | failed
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# dashboard 自己的状态库,与 hub 隔离
DATA_DIR = Path.home() / "dashboard-data"
TASKS_DB = DATA_DIR / "tasks.sqlite3"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 内存中的输出缓冲(每个任务一个 list,worker 线程写,API 线程读)
_output_buffers: dict[str, list[str]] = {}
_tasks_lock = threading.Lock()

# codex 二进制:优先环境变量,其次 PATH 探测
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(TASKS_DB)
    con.row_factory = sqlite3.Row
    return con


def _init_db() -> None:
    with _db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                workdir TEXT NOT NULL,
                prompt TEXT NOT NULL,
                images TEXT,
                model TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                pid INTEGER,
                exit_code INTEGER,
                created_ts REAL NOT NULL,
                started_ts REAL,
                finished_ts REAL,
                output_tail TEXT
            )"""
        )
        con.commit()


_init_db()


def list_tasks(limit: int = 50) -> list[dict[str, Any]]:
    with _db() as con:
        rows = con.execute(
            "SELECT id, workdir, prompt, model, status, pid, exit_code, "
            "created_ts, started_ts, finished_ts FROM tasks "
            "ORDER BY created_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = [dict(r) for r in rows]
    # 附上内存缓冲的实时输出长度
    for t in out:
        buf = _output_buffers.get(t["id"], [])
        t["output_lines"] = len(buf)
    return out


def get_task(task_id: str) -> dict[str, Any] | None:
    with _db() as con:
        row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        t = dict(row)
    t["output"] = list(_output_buffers.get(task_id, []))
    return t


def start_task(
    workdir: str,
    prompt: str,
    images: list[str] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """创建并启动一个 codex exec 任务。返回 task info。"""
    task_id = uuid.uuid4().hex[:12]
    workdir_path = Path(workdir).expanduser().resolve()
    if not workdir_path.is_dir():
        raise ValueError(f"工作目录不存在: {workdir_path}")

    now = time.time()
    with _db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, prompt, images, model, status, created_ts) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (task_id, str(workdir_path), prompt,
             json.dumps(images or []), model, now),
        )
        con.commit()
    _output_buffers[task_id] = []

    # 后台线程跑 codex
    thread = threading.Thread(
        target=_run_codex, args=(task_id, str(workdir_path), prompt, images or [], model),
        daemon=True,
    )
    thread.start()
    return {"id": task_id, "status": "pending", "workdir": str(workdir_path)}


def _run_codex(
    task_id: str, workdir: str, prompt: str, images: list[str], model: str | None
) -> None:
    """worker 线程:跑 codex exec,捕获输出。"""
    cmd = [CODEX_BIN, "exec", "-s", "workspace-write"]
    if model:
        cmd += ["-m", model]
    for img in images:
        cmd += ["-i", img]
    # prompt 用 stdin(codex exec 读 stdin),避免长 prompt 的 shell 转义问题
    # 这里用位置参数传 prompt(短),长 prompt 走 stdin
    if "\n" in prompt or len(prompt) > 200:
        stdin_data = prompt
    else:
        cmd.append(prompt)
        stdin_data = None

    def _emit(line: str) -> None:
        with _tasks_lock:
            buf = _output_buffers.setdefault(task_id, [])
            buf.append(line)
            # 只保留最近 2000 行,防爆内存
            if len(buf) > 2000:
                del buf[: len(buf) - 2000]

    started = time.time()
    with _db() as con:
        con.execute(
            "UPDATE tasks SET status='running', started_ts=? WHERE id=?", (started, task_id)
        )
        con.commit()

    exit_code = None
    try:
        proc = subprocess.Popen(
            cmd, cwd=workdir, stdin=subprocess.PIPE if stdin_data else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1,  # 行缓冲
        )
        with _db() as con:
            con.execute("UPDATE tasks SET pid=? WHERE id=?", (proc.pid, task_id))
            con.commit()
        if stdin_data and proc.stdin:
            try:
                proc.stdin.write(stdin_data)
                proc.stdin.close()
            except BrokenPipeError:
                pass
        assert proc.stdout is not None
        for line in proc.stdout:
            _emit(line.rstrip("\n"))
        proc.wait()
        exit_code = proc.returncode
    except FileNotFoundError:
        _emit(f"[ERROR] codex 未找到: {CODEX_BIN}")
        exit_code = 127
    except Exception as e:
        _emit(f"[ERROR] {type(e).__name__}: {e}")
        exit_code = 1

    finished = time.time()
    status = "done" if exit_code == 0 else "failed"
    with _tasks_lock:
        tail = "\n".join(_output_buffers.get(task_id, [])[-50:])
    with _db() as con:
        con.execute(
            "UPDATE tasks SET status=?, exit_code=?, finished_ts=?, output_tail=? WHERE id=?",
            (status, exit_code, finished, tail, task_id),
        )
        con.commit()


def task_diff(task_id: str) -> dict[str, Any]:
    """取任务工作目录的 git diff。"""
    t = get_task(task_id)
    if not t:
        raise ValueError("任务不存在")
    workdir = t["workdir"]
    try:
        diff = subprocess.run(
            ["git", "diff"], cwd=workdir, capture_output=True, text=True, timeout=30,
        ).stdout
        staged = subprocess.run(
            ["git", "diff", "--cached"], cwd=workdir, capture_output=True, text=True, timeout=30,
        ).stdout
        status = subprocess.run(
            ["git", "status", "--short"], cwd=workdir, capture_output=True, text=True, timeout=30,
        ).stdout
    except subprocess.TimeoutExpired:
        return {"error": "git 操作超时"}
    return {"diff": diff + staged, "status": status, "workdir": workdir}


def task_apply(task_id: str, action: str) -> dict[str, Any]:
    """对任务工作目录执行 git 操作。action: apply(add+commit) / stash(撤销) / checkout。"""
    t = get_task(task_id)
    if not t:
        raise ValueError("任务不存在")
    workdir = t["workdir"]
    if action == "apply":
        # add 全部改动并 commit
        subprocess.run(["git", "add", "-A"], cwd=workdir, capture_output=True, timeout=30)
        r = subprocess.run(
            ["git", "commit", "-m", f"dashboard apply: task {task_id}"],
            cwd=workdir, capture_output=True, text=True, timeout=30,
        )
        return {"result": r.stdout + r.stderr, "action": "apply"}
    elif action == "stash":
        r = subprocess.run(
            ["git", "stash"], cwd=workdir, capture_output=True, text=True, timeout=30
        )
        return {"result": r.stdout + r.stderr, "action": "stash"}
    elif action == "checkout":
        r = subprocess.run(
            ["git", "checkout", "--", "."], cwd=workdir, capture_output=True, text=True, timeout=30
        )
        return {"result": r.stdout + r.stderr, "action": "checkout"}
    raise ValueError(f"未知 action: {action}")
