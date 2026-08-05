"""tasks.py — 任务执行层:codex exec subprocess + 状态机 + 输出流捕获。

每个任务是独立的 codex exec 进程,在隔离的 git worktree 中运行,
后台异步跑,输出实时捕获到内存缓冲 + tasks.sqlite。
前端通过轮询或 SSE 看进度。

状态机:pending → running → done | failed

Worktree 隔离模型:
  - 每个任务在 ~/dashboard-data/worktrees/<task_id> 的 detached git worktree 中运行
  - codex 不直接修改用户的活工作区(source workdir)
  - task_diff:在隔离 worktree 中 git add -A 后输出 cached --binary diff,保存预览 hash
  - task_apply:验证 hash 一致 + 源仓库 clean + HEAD 未变后,在 worktree 生成提交
    (core.hooksPath=/dev/null)再 cherry-pick 到源仓库;失败不影响源工作区
    同一 source 的 apply 串行执行(per-source lock),避免竞争
  - stash/checkout:仅丢弃隔离 worktree,不操作源工作区
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
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
WORKTREE_ROOT = DATA_DIR / "worktrees"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 内存中的输出缓冲(每个任务一个 list,worker 线程写,API 线程读)
_output_buffers: dict[str, list[str]] = {}
_tasks_lock = threading.Lock()

# per-source 操作串行锁(apply / discard / diff / cleanup 同一源仓库互斥)
_apply_locks: dict[str, threading.Lock] = {}
_apply_locks_guard = threading.Lock()

# codex 二进制:优先环境变量,其次 PATH 探测
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
try:
    TASK_TIMEOUT_SECONDS = float(os.environ.get("COCKPIT_TASK_TIMEOUT", "3600"))
except ValueError:
    TASK_TIMEOUT_SECONDS = 3600.0

# 只保存本进程启动的子进程；重启后的 pending/running 由 _init_db 标为失败。
_active_processes: dict[str, subprocess.Popen] = {}
_cancel_requested: set[str] = set()


# ── DB ──────────────────────────────────────────────────────────

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
        _migrate_db(con)
        interrupted = "[ERROR] Agent Cockpit 服务重启，任务进程已中断"
        con.execute(
            "UPDATE tasks SET status='failed', exit_code=-1, finished_ts=?, "
            "output_tail=CASE WHEN output_tail IS NULL OR output_tail='' THEN ? "
            "ELSE output_tail || char(10) || ? END "
            "WHERE status IN ('pending', 'running')",
            (time.time(), interrupted, interrupted),
        )
        con.commit()


def _migrate_db(con: sqlite3.Connection) -> None:
    """Schema 兼容迁移:添加 worktree 隔离字段。"""
    cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)").fetchall()}
    for col in ("source_workdir", "base_sha", "run_workdir", "preview_hash"):
        if col not in cols:
            con.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")


_init_db()


def _clear_run_workdir(task_id: str) -> None:
    """清空 DB 中任务的 run_workdir 字段。"""
    with _db() as con:
        con.execute("UPDATE tasks SET run_workdir=NULL WHERE id=?", (task_id,))
        con.commit()


def _delete_task(task_id: str) -> None:
    """从 DB 中删除任务记录。"""
    with _db() as con:
        con.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        con.commit()


# ── Git helpers ─────────────────────────────────────────────────

def _git(
    args: list[str], cwd: Path, *, text: bool = True, timeout: int = 30
) -> subprocess.CompletedProcess:
    """执行 git 命令,返回 CompletedProcess。"""
    try:
        return subprocess.run(
            ["git"] + args, cwd=str(cwd), capture_output=True, text=text, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise ValueError(f"git {' '.join(args)} 超时(>{timeout}s)") from e


def _git_ok(args: list[str], cwd: Path) -> tuple[bool, str]:
    """执行 git,返回 (success, stdout_stripped)。"""
    r = _git(args, cwd)
    return r.returncode == 0, r.stdout.strip()


# ── Per-source lock ─────────────────────────────────────────────

def _get_source_lock(source: Path) -> threading.Lock:
    """获取(或创建)源仓库对应的串行锁(apply/discard/diff/cleanup 共用)。"""
    key = str(source)
    with _apply_locks_guard:
        if key not in _apply_locks:
            _apply_locks[key] = threading.Lock()
        return _apply_locks[key]


# ── Path validation ────────────────────────────────────────────

def _check_workdir_allowed(workdir: Path) -> None:
    """校验 workdir 在 files.allowed_roots 白名单范围内。

    延迟导入 files 模块以避免循环依赖;若 files 不可用则回退到 home 目录检查。
    """
    try:
        import files
        files._resolve(str(workdir))
    except ImportError:
        home = Path.home().resolve()
        try:
            workdir.relative_to(home)
        except ValueError:
            raise ValueError(f"工作目录不在允许范围内: {workdir}")


def _validate_image_paths(images: list[str]) -> list[str]:
    """校验 image 路径列表:仅允许 uploads.UPLOAD_DIR 下的已存在普通文件。

    返回 resolve 后的路径列表。延迟导入 uploads 以避免循环依赖。
    """
    try:
        import uploads
        upload_dir = uploads.UPLOAD_DIR.resolve()
    except ImportError:
        upload_dir = (Path.home() / "dashboard-uploads").resolve()

    validated: list[str] = []
    for img in images:
        img_path = Path(img).expanduser().resolve()
        try:
            img_path.relative_to(upload_dir)
        except ValueError:
            raise ValueError(f"image 路径不在上传目录范围内: {img}")
        if not img_path.is_file():
            raise ValueError(f"image 文件不存在或不是普通文件: {img}")
        validated.append(str(img_path))
    return validated


# ── Worktree management ─────────────────────────────────────────

def _worktree_dir(task_id: str) -> Path:
    """隔离 worktree 的路径:~/dashboard-data/worktrees/<task_id>"""
    return WORKTREE_ROOT / task_id


def _create_worktree(source: Path, task_id: str) -> tuple[Path, str]:
    """在 source 仓库的 HEAD 创建 detached worktree。

    返回 (worktree_path, base_sha)。
    """
    ok, base_sha = _git_ok(["rev-parse", "HEAD"], source)
    if not ok or not base_sha:
        raise ValueError(f"源目录不是有效的 git 仓库或无提交: {source}")

    wt = _worktree_dir(task_id)
    wt.parent.mkdir(parents=True, exist_ok=True)

    r = _git(["worktree", "add", "--detach", str(wt), "HEAD"], source, timeout=60)
    if r.returncode != 0:
        raise ValueError(f"创建 worktree 失败: {r.stderr.strip()}")
    return wt, base_sha


def _validate_worktree_path(worktree: Path) -> None:
    """验证 worktree 路径是 WORKTREE_ROOT 的直接子目录。

    拒绝:根目录本身、外部路径、软链逃逸。
    """
    root = WORKTREE_ROOT.resolve()
    resolved = worktree.resolve(strict=False)
    if resolved == root:
        raise ValueError(f"拒绝操作 worktree 根目录: {worktree}")
    if resolved.parent != root:
        raise ValueError(f"worktree 路径不在 {root} 下: {worktree}")


def _remove_worktree(source: Path | None, worktree: Path) -> None:
    """移除隔离 worktree(不影响源工作区)。

    若所有方式都无法删除目录则抛 ValueError,不伪装成功。
    调用方仅在方法正常返回后才清空 DB run_workdir。
    """
    _validate_worktree_path(worktree)
    # 1. git worktree remove (preferred, cleans git metadata)
    if source and source.exists():
        _git(["worktree", "remove", "--force", str(worktree)], source, timeout=30)
    # 2. Fallback: rmtree if directory still exists
    if worktree.exists():
        try:
            shutil.rmtree(worktree)
        except OSError as e:
            if worktree.exists():
                raise ValueError(f"无法移除 worktree 目录: {worktree} ({e})")
    # 3. Prune git metadata after fallback rmtree
    if source and source.exists():
        _git(["worktree", "prune"], source, timeout=30)
    # 4. 最终检查:目录确实已删除
    if worktree.exists():
        raise ValueError(f"无法移除 worktree 目录: {worktree}")


def _compute_diff_hash(diff_bytes: bytes) -> str:
    """计算 diff 的 SHA-256 hash,用于预览一致性校验。"""
    return hashlib.sha256(diff_bytes).hexdigest()


def _stage_and_diff(run_workdir: Path) -> bytes:
    """在隔离 worktree 中 git add -A 后返回 cached --binary diff(含未跟踪文件)。

    检查每步 returncode,失败时抛 ValueError。
    """
    r_add = _git(["add", "-A"], run_workdir)
    if r_add.returncode != 0:
        raise ValueError(f"git add 失败: {r_add.stderr.strip()}")
    r_diff = _git(
        ["diff", "--cached", "--binary"], run_workdir, text=False, timeout=60
    )
    if r_diff.returncode != 0:
        raise ValueError(f"git diff --cached 失败: {r_diff.stderr.strip()}")
    return r_diff.stdout


def _source_is_clean(source: Path) -> tuple[bool, str]:
    """检查源仓库是否 clean(无未提交改动)。返回 (is_clean, status_output)。"""
    r = _git(["status", "--short"], source)
    return (r.returncode == 0 and r.stdout.strip() == ""), r.stdout


def cleanup_worktrees(max_age_hours: float = 48) -> dict[str, Any]:
    """清理过期的隔离 worktree。

    删除超过 max_age_hours 的已完成/失败任务的 worktree。
    单个 worktree 移除失败不影响其他。
    """
    cutoff = time.time() - max_age_hours * 3600
    removed: list[str] = []
    errors: list[str] = []
    with _db() as con:
        rows = con.execute(
            "SELECT id, workdir, run_workdir FROM tasks "
            "WHERE run_workdir IS NOT NULL AND finished_ts IS NOT NULL "
            "AND finished_ts < ?",
            (cutoff,),
        ).fetchall()
    for row in rows:
        wt = Path(row["run_workdir"])
        source = Path(row["workdir"])
        if wt.exists():
            lock = _get_source_lock(source)
            with lock:
                if not wt.exists():
                    continue
                try:
                    _remove_worktree(source, wt)
                    _clear_run_workdir(row["id"])
                    removed.append(str(wt))
                except (ValueError, OSError) as e:
                    errors.append(str(e))
    return {"removed": removed, "count": len(removed), "errors": errors}


# ── Public API ──────────────────────────────────────────────────

def list_tasks(limit: int = 50) -> list[dict[str, Any]]:
    with _db() as con:
        rows = con.execute(
            "SELECT id, workdir, source_workdir, base_sha, run_workdir, preview_hash, "
            "prompt, model, status, pid, exit_code, "
            "created_ts, started_ts, finished_ts FROM tasks "
            "ORDER BY created_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = [dict(r) for r in rows]
    # 附上内存缓冲的实时输出长度
    with _tasks_lock:
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
    with _tasks_lock:
        buf = _output_buffers.get(task_id)
        t["output"] = list(buf) if buf is not None else (t.get("output_tail") or "").splitlines()
    return t


def start_task(
    workdir: str,
    prompt: str,
    images: list[str] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """创建并启动一个 codex exec 任务(在隔离 worktree 中运行)。返回 task info。"""
    task_id = uuid.uuid4().hex[:12]
    workdir_path = Path(workdir).expanduser().resolve()
    if not workdir_path.is_dir():
        raise ValueError(f"工作目录不存在: {workdir_path}")
    _check_workdir_allowed(workdir_path)

    validated_images = _validate_image_paths(images or [])

    # 创建隔离 worktree(在 codex 启动前)
    run_workdir, base_sha = _create_worktree(workdir_path, task_id)

    # worktree 创建成功后,DB insert / Thread / start 任一失败都需清理
    try:
        now = time.time()
        with _db() as con:
            con.execute(
                "INSERT INTO tasks (id, workdir, source_workdir, base_sha, run_workdir, "
                "prompt, images, model, status, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (task_id, str(workdir_path), str(workdir_path), base_sha,
                 str(run_workdir), prompt, json.dumps(validated_images), model, now),
            )
            con.commit()
        with _tasks_lock:
            _output_buffers[task_id] = []

        thread = threading.Thread(
            target=_run_codex,
            args=(task_id, str(run_workdir), prompt, validated_images, model),
            daemon=True,
        )
        thread.start()
    except Exception:
        try:
            _remove_worktree(workdir_path, run_workdir)
        except Exception:
            pass
        try:
            _delete_task(task_id)
        except Exception:
            pass
        with _tasks_lock:
            _output_buffers.pop(task_id, None)
        raise

    return {
        "id": task_id,
        "status": "pending",
        "workdir": str(workdir_path),
        "source_workdir": str(workdir_path),
        "base_sha": base_sha,
        "run_workdir": str(run_workdir),
    }


def _ensure_proc_terminated(proc: subprocess.Popen) -> None:
    """确保子进程已终止:terminate → 超时 kill → wait。"""
    if proc.poll() is not None:
        return
    pid = getattr(proc, "pid", None)
    can_signal_group = type(pid) is int and pid > 1
    if can_signal_group:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            proc.terminate()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
        return
    except Exception:
        pass
    if can_signal_group:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            proc.kill()
    else:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def cancel_task(task_id: str) -> dict[str, Any]:
    """请求取消 pending/running 任务，并终止其进程组。"""
    task = get_task(task_id)
    if not task:
        raise ValueError("任务不存在")
    if task["status"] not in ("pending", "running"):
        raise ValueError(f"任务已结束(状态: {task['status']}),不能取消")
    with _tasks_lock:
        _cancel_requested.add(task_id)
        proc = _active_processes.get(task_id)
    if proc is not None:
        _ensure_proc_terminated(proc)
    return {"id": task_id, "cancel_requested": True}


def _run_codex(
    task_id: str, workdir: str, prompt: str, images: list[str], model: str | None
) -> None:
    """worker 线程:在隔离 worktree 中跑 codex exec,捕获输出。"""
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
    proc = None
    timer = None
    timed_out = threading.Event()
    with _tasks_lock:
        cancelled = task_id in _cancel_requested
    try:
        if cancelled:
            _emit("[CANCELLED] 任务在进程启动前已取消")
            exit_code = 130
        else:
            try:
                proc = subprocess.Popen(
                    cmd, cwd=workdir, stdin=subprocess.PIPE if stdin_data else None,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    bufsize=1, start_new_session=True,  # 行缓冲 + 独立进程组供取消
                )
                with _tasks_lock:
                    _active_processes[task_id] = proc
                    cancelled = task_id in _cancel_requested
                with _db() as con:
                    con.execute("UPDATE tasks SET pid=? WHERE id=?", (proc.pid, task_id))
                    con.commit()
                if cancelled:
                    _ensure_proc_terminated(proc)
                elif TASK_TIMEOUT_SECONDS > 0:
                    def expire() -> None:
                        if proc is not None and proc.poll() is None:
                            timed_out.set()
                            _emit(
                                f"[ERROR] 任务超过总时限 "
                                f"{TASK_TIMEOUT_SECONDS:g} 秒，已终止"
                            )
                            _ensure_proc_terminated(proc)

                    timer = threading.Timer(TASK_TIMEOUT_SECONDS, expire)
                    timer.daemon = True
                    timer.start()
                if stdin_data and proc.stdin:
                    try:
                        proc.stdin.write(stdin_data)
                        proc.stdin.close()
                    except BrokenPipeError:
                        pass
                if proc.stdout is None:
                    raise RuntimeError("codex stdout pipe unavailable")
                for line in proc.stdout:
                    _emit(line.rstrip("\n"))
                proc.wait(timeout=5)
                exit_code = proc.returncode
            except FileNotFoundError:
                _emit(f"[ERROR] codex 未找到: {CODEX_BIN}")
                exit_code = 127
            except Exception as e:
                _emit(f"[ERROR] {type(e).__name__}: {e}")
                exit_code = 1
    finally:
        if timer is not None:
            timer.cancel()
        if proc is not None:
            _ensure_proc_terminated(proc)
        with _tasks_lock:
            _active_processes.pop(task_id, None)
            cancelled = task_id in _cancel_requested
            _cancel_requested.discard(task_id)

    finished = time.time()
    if timed_out.is_set():
        exit_code, status = 124, "failed"
    elif cancelled:
        exit_code, status = 130, "cancelled"
    else:
        status = "done" if exit_code == 0 else "failed"
    with _tasks_lock:
        tail = "\n".join(_output_buffers.get(task_id, [])[-50:])
    with _db() as con:
        con.execute(
            "UPDATE tasks SET status=?, exit_code=?, finished_ts=?, output_tail=? WHERE id=?",
            (status, exit_code, finished, tail, task_id),
        )
        con.commit()
    with _tasks_lock:
        _output_buffers.pop(task_id, None)


def task_diff(task_id: str) -> dict[str, Any]:
    """在隔离 worktree 中暂存所有改动并生成 binary diff + 预览 hash。

    流程:git add -A → git diff --cached --binary → 计算 SHA-256 hash → 存入 DB。
    stage+hash+DB preview 在 source lock 内执行,避免与 apply/discard 并发。
    """
    t = get_task(task_id)
    if not t:
        raise ValueError("任务不存在")
    if t["status"] in ("pending", "running"):
        raise ValueError(f"任务正在运行中(状态: {t['status']}),禁止预览 diff")
    run_workdir = t.get("run_workdir")
    if not run_workdir or not Path(run_workdir).is_dir():
        raise ValueError("任务 worktree 不存在(可能已被丢弃或未创建)")

    source = Path(t["workdir"])
    lock = _get_source_lock(source)
    with lock:
        # 锁内重新读取(apply/discard 可能已改变状态)
        t = get_task(task_id)
        if not t:
            raise ValueError("任务不存在")
        if t["status"] in ("pending", "running"):
            raise ValueError(f"任务正在运行中(状态: {t['status']}),禁止预览 diff")
        run_workdir = t.get("run_workdir")
        if not run_workdir or not Path(run_workdir).is_dir():
            raise ValueError("任务 worktree 不存在(可能已被丢弃或未创建)")

        diff_bytes = _stage_and_diff(Path(run_workdir))
        preview_hash = _compute_diff_hash(diff_bytes)

        with _db() as con:
            con.execute(
                "UPDATE tasks SET preview_hash=? WHERE id=?", (preview_hash, task_id)
            )
            con.commit()

        status_r = _git(["status", "--short"], Path(run_workdir))
        if status_r.returncode != 0:
            raise ValueError(f"git status 失败: {status_r.stderr.strip()}")

    return {
        "diff": diff_bytes.decode("utf-8", errors="replace"),
        "status": status_r.stdout,
        "workdir": t["workdir"],
        "run_workdir": run_workdir,
        "preview_hash": preview_hash,
    }


def task_apply(task_id: str, action: str) -> dict[str, Any]:
    """对任务 worktree 执行操作。

    action:
      apply   — 验证后将 worktree 提交安全应用到源仓库
      stash   — 丢弃隔离 worktree(不影响源工作区)
      checkout— 丢弃隔离 worktree(同 stash,兼容旧接口)
    """
    t = get_task(task_id)
    if not t:
        raise ValueError("任务不存在")

    source = Path(t["workdir"])
    run_workdir = Path(t["run_workdir"]) if t.get("run_workdir") else None

    if action == "apply":
        return _apply_to_source(task_id, source, run_workdir)
    elif action in ("stash", "checkout"):
        if t["status"] in ("pending", "running"):
            raise ValueError(
                f"任务正在运行中(状态: {t['status']}),禁止丢弃"
            )
        return _discard_worktree(task_id, source, run_workdir)
    raise ValueError(f"未知 action: {action}")


def _discard_worktree(
    task_id: str, source: Path, run_workdir: Path | None
) -> dict[str, Any]:
    """丢弃隔离 worktree(不影响源工作区)。

    在 source lock 内执行,避免与 apply/diff 并发。
    锁内重新读取任务状态;若 run_workdir 已被 apply 清空则做 stale 清理。
    """
    lock = _get_source_lock(source)
    with lock:
        t = get_task(task_id)
        if not t:
            raise ValueError("任务不存在")
        current_rw = t.get("run_workdir")
        if current_rw and Path(current_rw).is_dir():
            _remove_worktree(source, Path(current_rw))
            _clear_run_workdir(task_id)
            return {"result": "隔离 worktree 已丢弃", "action": "discard"}
        # 目录已不存在但 DB 仍有 stale run_workdir
        if current_rw:
            _clear_run_workdir(task_id)
        return {"result": "worktree 不存在(stale 记录已清理)", "action": "discard"}


def _apply_to_source(
    task_id: str, source: Path, run_workdir: Path | None
) -> dict[str, Any]:
    """安全应用 worktree 改动到源仓库。

    同一 source 的 apply 通过 per-source lock 串行执行,避免竞争。
    锁内重新读取任务状态、校验条件,然后 cherry-pick。
    失败时 abort cherry-pick,不影响源工作区。

    校验链:
      1. worktree 存在
      2. 任务状态为 done
      3. 已有 preview_hash(已调用过 task_diff)
      4. 重新计算 hash 与存储一致
      5. 源仓库 clean(无未提交改动)
      6. 源仓库 HEAD 仍等于 base_sha
    然后在 worktree 中用 core.hooksPath=/dev/null 生成提交,cherry-pick 到源仓库。
    """
    # 前置检查(锁外快速失败)
    if not run_workdir or not run_workdir.is_dir():
        raise ValueError("任务 worktree 不存在(可能已被丢弃)")

    lock = _get_source_lock(source)
    with lock:
        # 锁内重新读取任务(状态可能已被其他 apply 改变)
        t = get_task(task_id)
        if not t:
            raise ValueError("任务不存在")

        # 2. 任务必须已完成
        if t["status"] != "done":
            raise ValueError(
                f"任务未完成(当前状态: {t['status']}),仅允许应用已完成的任务"
            )

        # 3. 必须已预览 diff
        stored_hash = t.get("preview_hash")
        if not stored_hash:
            raise ValueError("任务尚未预览 diff,请先调用 task_diff")

        # 4. worktree 仍存在(锁内再次确认)
        current_rw = t.get("run_workdir")
        if not current_rw:
            raise ValueError("任务 worktree 已被丢弃")
        current_rw_path = Path(current_rw)
        if not current_rw_path.is_dir():
            raise ValueError("任务 worktree 不存在(可能已被丢弃)")

        # 5. 重新计算 hash,必须一致
        diff_bytes = _stage_and_diff(current_rw_path)
        current_hash = _compute_diff_hash(diff_bytes)
        if current_hash != stored_hash:
            raise ValueError(
                "diff hash 不匹配:worktree 内容自上次预览后已变化,请重新调用 task_diff"
            )

        # 6. 无改动:清理 worktree 并清空 DB run_workdir
        if not diff_bytes.strip():
            _remove_worktree(source, current_rw_path)
            _clear_run_workdir(task_id)
            return {
                "result": "无改动可应用,worktree 已清理",
                "action": "apply",
                "preview_hash": current_hash,
            }

        # 7. 源仓库必须 clean
        clean, dirty_status = _source_is_clean(source)
        if not clean:
            raise ValueError(f"源仓库有未提交改动,请先处理:\n{dirty_status}")

        # 8. 源仓库 HEAD 必须等于 base_sha
        base_sha = t.get("base_sha")
        ok, head_sha = _git_ok(["rev-parse", "HEAD"], source)
        if not ok or head_sha != base_sha:
            raise ValueError(
                f"源仓库 HEAD 已变化(base={base_sha}, current={head_sha}),"
                f"请重新创建任务"
            )

        # 9. 在 worktree 中生成提交(hooks 禁用)
        r = _git(
            ["-c", "core.hooksPath=/dev/null", "commit",
             "-m", f"dashboard apply: task {task_id}"],
            current_rw_path, timeout=30,
        )
        if r.returncode != 0:
            raise ValueError(f"worktree 提交失败: {r.stderr.strip()}")

        # 获取提交 SHA
        ok, commit_sha = _git_ok(["rev-parse", "HEAD"], current_rw_path)
        if not ok or not commit_sha:
            raise ValueError("无法获取 worktree 提交 SHA")

        # 10. Cherry-pick 到源仓库(安全应用)
        try:
            r = _git(["cherry-pick", commit_sha], source, timeout=60)
            cherry_pick_error = r.stderr.strip()
        except ValueError as e:
            r = None
            cherry_pick_error = str(e)
        if r is None or r.returncode != 0:
            # 失败:尝试 abort cherry-pick,不影响源工作区
            try:
                abort_r = _git(["cherry-pick", "--abort"], source, timeout=30)
            except ValueError as e:
                raise ValueError(
                    f"cherry-pick 失败且 --abort 超时,源仓库需人工处理: {e}"
                ) from e
            if abort_r.returncode != 0:
                raise ValueError(
                    f"cherry-pick 失败且 --abort 也失败,"
                    f"源仓库需人工处理: "
                    f"cherry-pick error={cherry_pick_error}, "
                    f"abort error={abort_r.stderr.strip()}"
                )
            # worktree 的提交尚未进入源仓库。回到原 base 并保留 staged diff，
            # 让用户可以原样重试；否则下一次预览会看到空 diff 并可能误删 worktree。
            try:
                reset_r = _git(["reset", "--soft", base_sha], current_rw_path, timeout=30)
            except ValueError as e:
                raise ValueError(
                    f"cherry-pick 已 abort,但恢复 worktree 失败；提交 {commit_sha} 仍保留在 "
                    f"{current_rw_path}: {e}"
                ) from e
            if reset_r.returncode != 0:
                raise ValueError(
                    f"cherry-pick 已 abort,但恢复 worktree 失败；提交 {commit_sha} 仍保留在 "
                    f"{current_rw_path}: {reset_r.stderr.strip()}"
                )
            raise ValueError(f"应用到源仓库失败(cherry-pick): {cherry_pick_error}")

        # 11. 清理 worktree
        _remove_worktree(source, current_rw_path)
        _clear_run_workdir(task_id)

        return {
            "result": r.stdout + r.stderr,
            "action": "apply",
            "commit_sha": commit_sha,
            "preview_hash": current_hash,
        }
