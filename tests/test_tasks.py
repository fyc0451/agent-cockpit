"""tests/test_tasks.py — worktree 隔离回归测试。

覆盖:
  - worktree 创建 / 移除
  - task_diff(staging + hash)
  - task_apply(校验链:状态 / hash / clean / HEAD)
  - stash / checkout(仅丢弃 worktree)
  - schema 兼容迁移
  - allowed_roots 校验
  - cleanup_worktrees
  - start_task 集成
"""
from __future__ import annotations

import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tasks


# ── Fixtures ────────────────────────────────────────────────────

@pytest.fixture
def temp_data(tmp_path, monkeypatch):
    """将 tasks 数据目录和 DB 重定向到 tmp_path。"""
    monkeypatch.setattr(tasks, "DATA_DIR", tmp_path)
    monkeypatch.setattr(tasks, "TASKS_DB", tmp_path / "tasks.sqlite3")
    monkeypatch.setattr(tasks, "WORKTREE_ROOT", tmp_path / "worktrees")
    tasks._output_buffers.clear()
    tasks._init_db()
    return tmp_path


@pytest.fixture
def git_repo(tmp_path):
    """创建一个有初始提交的临时 git 仓库。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, capture_output=True
    )
    (repo / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, capture_output=True
    )
    return repo


@pytest.fixture
def task_with_worktree(temp_data, git_repo, monkeypatch):
    """创建一个带活跃 worktree 的已完成任务记录(不跑 codex)。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)
    task_id = "test1234abcd"
    source = git_repo.resolve()
    run_wt, base_sha = tasks._create_worktree(source, task_id)
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, source_workdir, base_sha, run_workdir, "
            "prompt, images, model, status, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'done', ?)",
            (task_id, str(source), str(source), base_sha, str(run_wt),
             "test prompt", "[]", None, now),
        )
        con.commit()
    return {
        "id": task_id,
        "source": source,
        "run_workdir": run_wt,
        "base_sha": base_sha,
    }


@pytest.fixture
def running_task_with_worktree(temp_data, git_repo, monkeypatch):
    """创建一个带活跃 worktree 的 running 状态任务(不跑 codex)。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)
    task_id = "runn5678efgh"
    source = git_repo.resolve()
    run_wt, base_sha = tasks._create_worktree(source, task_id)
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, source_workdir, base_sha, run_workdir, "
            "prompt, images, model, status, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)",
            (task_id, str(source), str(source), base_sha, str(run_wt),
             "test prompt", "[]", None, now),
        )
        con.commit()
    return {
        "id": task_id,
        "source": source,
        "run_workdir": run_wt,
        "base_sha": base_sha,
    }


# ── Worktree 创建 / 移除 ────────────────────────────────────────

def test_create_worktree_success(git_repo, temp_data):
    """_create_worktree 从 HEAD 创建 detached worktree。"""
    task_id = "abc123def456"
    wt, base_sha = tasks._create_worktree(git_repo, task_id)
    assert wt.exists()
    assert wt.is_dir()
    assert len(base_sha) == 40  # full SHA
    # worktree 包含源仓库的文件
    assert (wt / "README.md").exists()
    # git 已注册该 worktree
    r = subprocess.run(
        ["git", "worktree", "list"], cwd=git_repo, capture_output=True, text=True
    )
    assert str(wt) in r.stdout


def test_create_worktree_not_git_repo(tmp_path, temp_data):
    """_create_worktree 在非 git 目录上失败。"""
    not_repo = tmp_path / "notrepo"
    not_repo.mkdir()
    with pytest.raises(ValueError, match="git"):
        tasks._create_worktree(not_repo, "xxx111xxx222")


def test_remove_worktree(git_repo, temp_data):
    """_remove_worktree 移除 worktree 并清理 git 元数据。"""
    task_id = "removetest1234"
    wt, _ = tasks._create_worktree(git_repo, task_id)
    assert wt.exists()
    tasks._remove_worktree(git_repo, wt)
    assert not wt.exists()
    r = subprocess.run(
        ["git", "worktree", "list"], cwd=git_repo, capture_output=True, text=True
    )
    assert str(wt) not in r.stdout


def test_remove_worktree_no_source(temp_data, tmp_path):
    """_remove_worktree 在 source 不存在时仍能删除目录(需在 WORKTREE_ROOT 下)。"""
    wt = tasks._worktree_dir("orphan_test_wt")
    wt.parent.mkdir(parents=True, exist_ok=True)
    wt.mkdir()
    (wt / "file.txt").write_text("data")
    tasks._remove_worktree(None, wt)
    assert not wt.exists()


# ── Schema 兼容迁移 ─────────────────────────────────────────────

def test_migrate_db_adds_columns(temp_data):
    """_migrate_db 添加 worktree 隔离字段。"""
    with tasks._db() as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)").fetchall()}
    for expected in ("source_workdir", "base_sha", "run_workdir", "preview_hash"):
        assert expected in cols, f"缺少列: {expected}"


def test_old_tasks_still_readable(temp_data):
    """迁移前的旧任务(无 worktree 字段)仍可列出和读取。"""
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, prompt, status, created_ts) "
            "VALUES ('oldtask001', '/tmp/fake', 'old task', 'done', ?)",
            (now,),
        )
        con.commit()
    tl = tasks.list_tasks()
    assert any(t["id"] == "oldtask001" for t in tl)
    t = tasks.get_task("oldtask001")
    assert t is not None
    assert t["workdir"] == "/tmp/fake"
    assert t.get("run_workdir") is None


def test_migrate_idempotent(temp_data):
    """多次调用 _migrate_db 不报错(幂等)。"""
    with tasks._db() as con:
        tasks._migrate_db(con)
        tasks._migrate_db(con)
        con.commit()
    # 表仍可用
    tl = tasks.list_tasks()
    assert isinstance(tl, list)


def test_init_db_marks_interrupted_tasks_failed(temp_data):
    now = time.time()
    with tasks._db() as con:
        for task_id, status in (("pending-old", "pending"), ("running-old", "running")):
            con.execute(
                "INSERT INTO tasks (id, workdir, prompt, status, created_ts) "
                "VALUES (?, '/tmp/fake', 'old task', ?, ?)",
                (task_id, status, now),
            )
        con.commit()

    tasks._init_db()

    for task_id in ("pending-old", "running-old"):
        task = tasks.get_task(task_id)
        assert task["status"] == "failed"
        assert task["exit_code"] == -1
        assert "服务重启" in task["output_tail"]


# ── task_diff ───────────────────────────────────────────────────

def test_task_diff_stages_and_hashes(task_with_worktree):
    """task_diff 暂存所有改动并保存预览 hash。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    (wt / "new_file.txt").write_text("hello world")
    (wt / "README.md").write_text("# Modified")

    result = tasks.task_diff(tid)
    assert "diff" in result
    assert "new_file.txt" in result["diff"]
    assert result["preview_hash"]
    assert len(result["preview_hash"]) == 64  # SHA-256 hex

    t = tasks.get_task(tid)
    assert t["preview_hash"] == result["preview_hash"]


def test_task_diff_includes_untracked(task_with_worktree):
    """task_diff 包含未跟踪文件(via git add -A)。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    (wt / "untracked.bin").write_bytes(b"\x00\x01\x02")

    result = tasks.task_diff(tid)
    assert "untracked.bin" in result["diff"]
    assert result["preview_hash"]


def test_task_diff_no_worktree(temp_data):
    """worktree 不存在时 task_diff 报错。"""
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, prompt, status, created_ts) "
            "VALUES ('diffnotest', '/tmp/fake', 'test', 'done', ?)",
            (now,),
        )
        con.commit()
    with pytest.raises(ValueError, match="worktree"):
        tasks.task_diff("diffnotest")


def test_task_diff_rejects_running_task(running_task_with_worktree):
    with pytest.raises(ValueError, match="正在运行"):
        tasks.task_diff(running_task_with_worktree["id"])


def test_task_diff_hash_consistent(task_with_worktree):
    """相同改动产生相同 hash。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    (wt / "file.txt").write_text("content")

    r1 = tasks.task_diff(tid)
    r2 = tasks.task_diff(tid)
    assert r1["preview_hash"] == r2["preview_hash"]


def test_task_diff_empty(task_with_worktree):
    """无改动时 task_diff 返回空 diff 但仍有 hash。"""
    tid = task_with_worktree["id"]
    result = tasks.task_diff(tid)
    assert result["preview_hash"]
    assert result["diff"].strip() == ""


# ── task_apply — apply ──────────────────────────────────────────

def test_apply_success(task_with_worktree):
    """成功 apply:worktree 提交 + cherry-pick 到源仓库。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    source = task_with_worktree["source"]
    (wt / "applied.txt").write_text("applied content")
    tasks.task_diff(tid)

    result = tasks.task_apply(tid, "apply")
    assert result["action"] == "apply"
    assert result["commit_sha"]
    # 改动已进入源仓库
    assert (source / "applied.txt").exists()
    assert (source / "applied.txt").read_text() == "applied content"


def test_apply_no_changes(task_with_worktree):
    """无改动时 apply 清理 worktree 并清空 DB run_workdir。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    tasks.task_diff(tid)
    assert wt.exists()
    result = tasks.task_apply(tid, "apply")
    assert "无改动" in result["result"]
    # worktree 被清理
    assert not wt.exists()
    # DB run_workdir 被清空
    t = tasks.get_task(tid)
    assert t["run_workdir"] is None


def test_apply_requires_completion(running_task_with_worktree):
    """未完成任务不允许 apply。"""
    tid = running_task_with_worktree["id"]
    wt = running_task_with_worktree["run_workdir"]
    (wt / "file.txt").write_text("content")
    with pytest.raises(ValueError, match="未完成"):
        tasks.task_apply(tid, "apply")


def test_apply_requires_preview(task_with_worktree):
    """未预览 diff 的任务不允许 apply。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    (wt / "file.txt").write_text("content")
    with pytest.raises(ValueError, match="尚未预览"):
        tasks.task_apply(tid, "apply")


def test_apply_hash_mismatch(task_with_worktree):
    """预览后 worktree 再次变动,hash 不匹配时 apply 失败。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    (wt / "file1.txt").write_text("content1")
    tasks.task_diff(tid)
    # 预览后追加新文件,不再重新预览
    (wt / "file2.txt").write_text("content2")
    with pytest.raises(ValueError, match="hash 不匹配"):
        tasks.task_apply(tid, "apply")


def test_apply_source_not_clean(task_with_worktree):
    """源仓库有未提交改动时 apply 失败。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    source = task_with_worktree["source"]
    (wt / "new.txt").write_text("new")
    tasks.task_diff(tid)
    # 在源仓库制造脏改动
    (source / "dirty.txt").write_text("dirty")
    with pytest.raises(ValueError, match="源仓库有未提交改动"):
        tasks.task_apply(tid, "apply")


def test_apply_source_head_changed(task_with_worktree):
    """源仓库 HEAD 移动后 apply 失败。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    source = task_with_worktree["source"]
    (wt / "new.txt").write_text("new")
    tasks.task_diff(tid)
    # 在源仓库创建新提交,移动 HEAD
    (source / "extra.txt").write_text("extra")
    subprocess.run(["git", "add", "-A"], cwd=source, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "extra"], cwd=source, capture_output=True
    )
    with pytest.raises(ValueError, match="HEAD 已变化"):
        tasks.task_apply(tid, "apply")


def test_apply_cleans_up_worktree(task_with_worktree):
    """apply 成功后 worktree 被清理。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    source = task_with_worktree["source"]
    (wt / "file.txt").write_text("content")
    tasks.task_diff(tid)
    tasks.task_apply(tid, "apply")
    assert not wt.exists()
    t = tasks.get_task(tid)
    assert t["run_workdir"] is None


def test_apply_does_not_touch_source_on_failure(task_with_worktree):
    """apply 失败时源仓库改动不丢失。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    source = task_with_worktree["source"]
    (wt / "new.txt").write_text("new")
    tasks.task_diff(tid)
    # 让源仓库变脏,触发 apply 失败
    (source / "user_change.txt").write_text("user data")
    with pytest.raises(ValueError, match="源仓库有未提交改动"):
        tasks.task_apply(tid, "apply")
    # 用户改动仍在
    assert (source / "user_change.txt").exists()
    assert (source / "user_change.txt").read_text() == "user data"


# ── task_apply — stash / checkout (discard) ─────────────────────

def test_stash_discards_worktree(task_with_worktree):
    """stash 丢弃 worktree,不影响源工作区。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    source = task_with_worktree["source"]
    assert wt.exists()
    (wt / "temp.txt").write_text("temp")
    result = tasks.task_apply(tid, "stash")
    assert "丢弃" in result["result"]
    assert not wt.exists()
    # 源仓库不受影响
    assert not (source / "temp.txt").exists()


def test_checkout_same_as_stash(task_with_worktree):
    """checkout 兼容旧接口,同样丢弃 worktree。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    result = tasks.task_apply(tid, "checkout")
    assert "丢弃" in result["result"]
    assert not wt.exists()


def test_discard_source_remains_clean(task_with_worktree):
    """丢弃 worktree 后源仓库仍 clean。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    source = task_with_worktree["source"]
    (wt / "temp.txt").write_text("temp")
    tasks.task_apply(tid, "stash")
    clean, _ = tasks._source_is_clean(source)
    assert clean


def test_discard_already_removed(task_with_worktree):
    """worktree 目录已删除时 discard 清理 stale DB run_workdir。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    tasks._remove_worktree(task_with_worktree["source"], wt)
    # DB still has stale run_workdir
    t_before = tasks.get_task(tid)
    assert t_before["run_workdir"] is not None
    result = tasks.task_apply(tid, "stash")
    assert "stale" in result["result"]
    # stale run_workdir cleared
    t_after = tasks.get_task(tid)
    assert t_after["run_workdir"] is None


# ── allowed_roots 校验 ──────────────────────────────────────────

def test_check_workdir_allowed_within_roots(tmp_path, monkeypatch):
    """allowed_roots 内的路径通过校验。"""
    import files
    monkeypatch.setattr(files, "_load_roots", lambda: [tmp_path.resolve()])
    d = tmp_path / "project"
    d.mkdir()
    tasks._check_workdir_allowed(d.resolve())


def test_check_workdir_allowed_outside_roots(tmp_path, monkeypatch):
    """allowed_roots 外的路径被拒绝。"""
    import files
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(files, "_load_roots", lambda: [allowed.resolve()])
    with pytest.raises(ValueError, match="不在允许范围"):
        tasks._check_workdir_allowed(outside.resolve())


# ── cleanup_worktrees ───────────────────────────────────────────

def test_cleanup_removes_stale(temp_data, git_repo):
    """cleanup_worktrees 删除过期 worktree。"""
    task_id = "stale0001test"
    wt, base_sha = tasks._create_worktree(git_repo, task_id)
    old = time.time() - 72 * 3600  # 3 天前
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, base_sha, run_workdir, prompt, status, "
            "created_ts, finished_ts) VALUES (?, ?, ?, ?, 'test', 'done', ?, ?)",
            (task_id, str(git_repo), base_sha, str(wt), old, old),
        )
        con.commit()
    assert wt.exists()
    result = tasks.cleanup_worktrees(max_age_hours=48)
    assert result["count"] >= 1
    assert not wt.exists()


def test_cleanup_keeps_recent(temp_data, git_repo):
    """cleanup_worktrees 保留近期 worktree。"""
    task_id = "fresh0001test"
    wt, base_sha = tasks._create_worktree(git_repo, task_id)
    recent = time.time() - 3600  # 1 小时前
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, base_sha, run_workdir, prompt, status, "
            "created_ts, finished_ts) VALUES (?, ?, ?, ?, 'test', 'done', ?, ?)",
            (task_id, str(git_repo), base_sha, str(wt), recent, recent),
        )
        con.commit()
    result = tasks.cleanup_worktrees(max_age_hours=48)
    assert result["count"] == 0
    assert wt.exists()


# ── start_task 集成 ─────────────────────────────────────────────

def test_start_task_creates_worktree(temp_data, git_repo, monkeypatch):
    """start_task 创建隔离 worktree 并在其中运行 codex。"""
    done_event = threading.Event()

    def mock_run(task_id, workdir, prompt, images, model):
        try:
            (Path(workdir) / "output.txt").write_text("codex was here")
            finished = time.time()
            with tasks._db() as con:
                con.execute(
                    "UPDATE tasks SET status='done', exit_code=0, "
                    "finished_ts=? WHERE id=?",
                    (finished, task_id),
                )
                con.commit()
        finally:
            done_event.set()

    monkeypatch.setattr(tasks, "_run_codex", mock_run)
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)

    result = tasks.start_task(workdir=str(git_repo), prompt="test prompt")
    assert result["status"] == "pending"
    assert result["run_workdir"]
    assert result["base_sha"]
    assert Path(result["run_workdir"]).exists()

    done_event.wait(timeout=5)
    time.sleep(0.1)

    t = tasks.get_task(result["id"])
    assert t["status"] == "done"
    assert t["source_workdir"] == str(git_repo.resolve())
    assert t["base_sha"] == result["base_sha"]
    assert t["run_workdir"] == result["run_workdir"]
    # codex 的输出在 worktree 中,不在源仓库
    assert (Path(result["run_workdir"]) / "output.txt").exists()
    assert not (git_repo / "output.txt").exists()


def test_start_task_rejects_disallowed_workdir(temp_data, git_repo, monkeypatch):
    """start_task 拒绝 allowed_roots 外的工作目录。"""
    def deny(w):
        raise ValueError("工作目录不在允许范围内")

    monkeypatch.setattr(tasks, "_check_workdir_allowed", deny)
    with pytest.raises(ValueError, match="不在允许范围"):
        tasks.start_task(workdir=str(git_repo), prompt="test")


def test_start_task_rejects_nonexistent_dir(temp_data, monkeypatch):
    """start_task 拒绝不存在的目录。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)
    with pytest.raises(ValueError, match="不存在"):
        tasks.start_task(workdir="/nonexistent/path/xyz", prompt="test")


def test_start_task_rejects_non_git(temp_data, tmp_path, monkeypatch):
    """start_task 拒绝非 git 仓库。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)
    d = tmp_path / "notgit"
    d.mkdir()
    with pytest.raises(ValueError, match="git"):
        tasks.start_task(workdir=str(d), prompt="test")


# ── start_task 失败清理 ─────────────────────────────────────────

def test_start_task_cleans_up_on_db_failure(temp_data, git_repo, monkeypatch):
    """DB insert 失败时清理 worktree/DB/缓冲。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)

    def always_fail():
        raise sqlite3.OperationalError("simulated DB failure")

    monkeypatch.setattr(tasks, "_db", always_fail)

    with pytest.raises(sqlite3.OperationalError):
        tasks.start_task(workdir=str(git_repo), prompt="test")

    # worktree 不应残留
    wts = list(tasks.WORKTREE_ROOT.iterdir()) if tasks.WORKTREE_ROOT.exists() else []
    assert len(wts) == 0


def test_start_task_cleans_up_on_thread_failure(temp_data, git_repo, monkeypatch):
    """Thread 构造或 start 失败时清理 worktree/DB/缓冲。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)

    class FailingThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("Thread.start failed")

    monkeypatch.setattr(tasks.threading, "Thread", FailingThread)

    with pytest.raises(RuntimeError, match="Thread.start failed"):
        tasks.start_task(workdir=str(git_repo), prompt="test")

    # worktree 不应残留
    wts = list(tasks.WORKTREE_ROOT.iterdir()) if tasks.WORKTREE_ROOT.exists() else []
    assert len(wts) == 0
    # DB 中不应有任务记录
    tl = tasks.list_tasks()
    assert len(tl) == 0
    # 缓冲不应残留
    assert len(tasks._output_buffers) == 0


# ── _stage_and_diff returncode 检查 ─────────────────────────────

def test_stage_and_diff_checks_add_returncode(task_with_worktree, monkeypatch):
    """_stage_and_diff 在 git add 失败时报错。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    (wt / "file.txt").write_text("content")

    original_git = tasks._git

    def mock_git(args, cwd, **kwargs):
        if args and args[0] == "add":
            r = original_git(args, cwd, **kwargs)
            r.returncode = 1
            r.stderr = "mocked add failure"
            return r
        return original_git(args, cwd, **kwargs)

    monkeypatch.setattr(tasks, "_git", mock_git)
    with pytest.raises(ValueError, match="git add 失败"):
        tasks.task_diff(tid)


def test_stage_and_diff_checks_diff_returncode(task_with_worktree, monkeypatch):
    """_stage_and_diff 在 git diff 失败时报错。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    (wt / "file.txt").write_text("content")

    original_git = tasks._git

    def mock_git(args, cwd, **kwargs):
        if args and args[0] == "diff":
            r = original_git(args, cwd, **kwargs)
            r.returncode = 1
            r.stderr = "mocked diff failure"
            return r
        return original_git(args, cwd, **kwargs)

    monkeypatch.setattr(tasks, "_git", mock_git)
    with pytest.raises(ValueError, match="git diff --cached 失败"):
        tasks.task_diff(tid)


def test_task_diff_checks_status_returncode(task_with_worktree, monkeypatch):
    """task_diff 在 git status 失败时报错。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    (wt / "file.txt").write_text("content")

    original_git = tasks._git

    def mock_git(args, cwd, **kwargs):
        if args and args[0] == "status":
            r = original_git(args, cwd, **kwargs)
            r.returncode = 1
            r.stderr = "mocked status failure"
            return r
        return original_git(args, cwd, **kwargs)

    monkeypatch.setattr(tasks, "_git", mock_git)
    with pytest.raises(ValueError, match="git status 失败"):
        tasks.task_diff(tid)


# ── per-source apply 串行锁 ─────────────────────────────────────

def test_apply_serialized_same_source(temp_data, git_repo, monkeypatch):
    """同一 source 的两个任务 apply 串行:第一个成功后 HEAD 移动,第二个失败。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)
    source = git_repo.resolve()

    # 创建两个任务的 worktree
    tid_a = "ser_task_aaaa"
    tid_b = "ser_task_bbbb"
    wt_a, sha_a = tasks._create_worktree(source, tid_a)
    wt_b, sha_b = tasks._create_worktree(source, tid_b)
    now = time.time()

    for tid, wt, sha in [(tid_a, wt_a, sha_a), (tid_b, wt_b, sha_b)]:
        with tasks._db() as con:
            con.execute(
                "INSERT INTO tasks (id, workdir, source_workdir, base_sha, run_workdir, "
                "prompt, images, model, status, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'done', ?)",
                (tid, str(source), str(source), sha, str(wt),
                 "test", "[]", None, now),
            )
            con.commit()
        # 各自在 worktree 中创建改动并预览
        (wt / f"{tid}_output.txt").write_text(f"output of {tid}")
        tasks.task_diff(tid)

    # 第一个 apply 成功
    result_a = tasks.task_apply(tid_a, "apply")
    assert result_a["action"] == "apply"
    assert (source / f"{tid_a}_output.txt").exists()

    # 第二个 apply 应失败(源 HEAD 已被第一个移动)
    with pytest.raises(ValueError, match="HEAD 已变化"):
        tasks.task_apply(tid_b, "apply")


# ── image 路径校验 ──────────────────────────────────────────────

def test_validate_image_paths_valid(tmp_path, monkeypatch):
    """有效上传目录下的文件通过校验。"""
    import uploads
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(uploads, "UPLOAD_DIR", upload_dir)

    f = upload_dir / "img.png"
    f.write_bytes(b"fake png")
    result = tasks._validate_image_paths([str(f)])
    assert len(result) == 1
    assert Path(result[0]).resolve() == f.resolve()


def test_validate_image_paths_rejects_outside(tmp_path, monkeypatch):
    """上传目录外的路径被拒绝。"""
    import uploads
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "evil.png"
    f.write_bytes(b"evil")
    monkeypatch.setattr(uploads, "UPLOAD_DIR", upload_dir)

    with pytest.raises(ValueError, match="不在上传目录范围"):
        tasks._validate_image_paths([str(f)])


def test_validate_image_paths_rejects_nonexistent(tmp_path, monkeypatch):
    """不存在的文件被拒绝。"""
    import uploads
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(uploads, "UPLOAD_DIR", upload_dir)

    with pytest.raises(ValueError, match="不存在"):
        tasks._validate_image_paths([str(upload_dir / "ghost.png")])


def test_validate_image_paths_rejects_directory(tmp_path, monkeypatch):
    """目录(非普通文件)被拒绝。"""
    import uploads
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    sub = upload_dir / "subdir"
    sub.mkdir()
    monkeypatch.setattr(uploads, "UPLOAD_DIR", upload_dir)

    with pytest.raises(ValueError, match="不是普通文件|不存在"):
        tasks._validate_image_paths([str(sub)])


def test_start_task_rejects_bad_image(temp_data, git_repo, monkeypatch):
    """start_task 拒绝非法 image 路径。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)
    import uploads
    monkeypatch.setattr(uploads, "UPLOAD_DIR", temp_data / "uploads")
    with pytest.raises(ValueError, match="不在上传目录范围|不存在"):
        tasks.start_task(
            workdir=str(git_repo), prompt="test",
            images=["/etc/passwd"],
        )


# ── _remove_worktree 失败处理 ───────────────────────────────────

def test_remove_worktree_raises_on_residual(temp_data, git_repo, monkeypatch):
    """_remove_worktree 在目录仍存在时报错,不伪装成功。"""
    task_id = "failrm001test"
    wt, _ = tasks._create_worktree(git_repo, task_id)
    assert wt.exists()

    # Mock shutil.rmtree 和 _git worktree remove 都不删目录
    monkeypatch.setattr(tasks.shutil, "rmtree", lambda p, **kw: None)
    # _git 已经被 capture 不会报错但也不会删(因为 rmtree 被mock了)
    # 实际上 _remove_worktree 先 git worktree remove(可能成功)再 rmtree
    # 要让目录残留,需要 git worktree remove 也失败
    original_git = tasks._git

    def mock_git(args, cwd, **kwargs):
        if args and args[0] == "worktree" and len(args) > 1 and args[1] == "remove":
            r = MagicMock()
            r.returncode = 1
            r.stderr = "permission denied"
            r.stdout = ""
            return r
        return original_git(args, cwd, **kwargs)

    monkeypatch.setattr(tasks, "_git", mock_git)
    with pytest.raises(ValueError, match="无法移除 worktree"):
        tasks._remove_worktree(git_repo, wt)


def test_discard_does_not_clear_db_on_remove_failure(
    task_with_worktree, monkeypatch
):
    """_remove_worktree 失败时 discard 不清空 DB run_workdir。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    source = task_with_worktree["source"]

    def fail_remove(source, worktree):
        raise ValueError("无法移除 worktree: permission denied")

    monkeypatch.setattr(tasks, "_remove_worktree", fail_remove)
    with pytest.raises(ValueError, match="无法移除"):
        tasks.task_apply(tid, "stash")

    # DB run_workdir 仍保留(未被清空)
    t = tasks.get_task(tid)
    assert t["run_workdir"] is not None


# ── task_diff status failure edge case ─────────────────────────

def test_task_diff_status_empty_when_clean(task_with_worktree):
    """task_diff 在 clean worktree 上 status 返回空字符串。"""
    tid = task_with_worktree["id"]
    result = tasks.task_diff(tid)
    assert result["status"].strip() == ""


# ── _remove_worktree 路径校验 ──────────────────────────────────

def test_remove_worktree_rejects_external_path(temp_data, tmp_path):
    """_remove_worktree 拒绝删除 WORKTREE_ROOT 外部的目录。"""
    external = tmp_path / "evil_dir"
    external.mkdir()
    (external / "secret.txt").write_text("secret")
    with pytest.raises(ValueError, match="不在"):
        tasks._remove_worktree(None, external)
    # 外部目录未被删除
    assert external.exists()
    assert (external / "secret.txt").exists()


def test_remove_worktree_rejects_root_itself(temp_data):
    """_remove_worktree 拒绝删除 WORKTREE_ROOT 本身。"""
    tasks.WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="根目录"):
        tasks._remove_worktree(None, tasks.WORKTREE_ROOT)


def test_validate_worktree_path_accepts_child(temp_data):
    """_validate_worktree_path 接受 WORKTREE_ROOT 的直接子目录。"""
    child = tasks._worktree_dir("valid_child")
    child.mkdir(parents=True, exist_ok=True)
    tasks._validate_worktree_path(child)  # should not raise


def test_validate_worktree_path_rejects_parent(temp_data, tmp_path):
    """_validate_worktree_path 拒绝父目录(DATA_DIR)。"""
    data_dir = tasks.DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError):
        tasks._validate_worktree_path(data_dir)


# ── stash/checkout 状态守卫 ─────────────────────────────────────

def test_stash_rejects_pending_task(temp_data, git_repo, monkeypatch):
    """stash 拒绝 pending 状态任务(codex 可能仍在运行)。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)
    task_id = "pend0001test"
    source = git_repo.resolve()
    run_wt, base_sha = tasks._create_worktree(source, task_id)
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, source_workdir, base_sha, run_workdir, "
            "prompt, images, model, status, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (task_id, str(source), str(source), base_sha, str(run_wt),
             "test", "[]", None, now),
        )
        con.commit()
    with pytest.raises(ValueError, match="运行中"):
        tasks.task_apply(task_id, "stash")
    # worktree 未被删除
    assert run_wt.exists()


def test_stash_rejects_running_task(temp_data, git_repo, monkeypatch):
    """stash 拒绝 running 状态任务。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)
    task_id = "runn0002test"
    source = git_repo.resolve()
    run_wt, base_sha = tasks._create_worktree(source, task_id)
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, source_workdir, base_sha, run_workdir, "
            "prompt, images, model, status, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)",
            (task_id, str(source), str(source), base_sha, str(run_wt),
             "test", "[]", None, now),
        )
        con.commit()
    with pytest.raises(ValueError, match="运行中"):
        tasks.task_apply(task_id, "checkout")
    assert run_wt.exists()


def test_stash_allows_failed_task(temp_data, git_repo, monkeypatch):
    """stash 允许 failed 状态任务。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)
    task_id = "fail0003test"
    source = git_repo.resolve()
    run_wt, base_sha = tasks._create_worktree(source, task_id)
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, source_workdir, base_sha, run_workdir, "
            "prompt, images, model, status, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'failed', ?)",
            (task_id, str(source), str(source), base_sha, str(run_wt),
             "test", "[]", None, now),
        )
        con.commit()
    result = tasks.task_apply(task_id, "stash")
    assert "丢弃" in result["result"]
    assert not run_wt.exists()


# ── cleanup_worktrees 清 DB ─────────────────────────────────────

def test_cleanup_clears_db_run_workdir(temp_data, git_repo):
    """cleanup_worktrees 成功删除后清空 DB run_workdir。"""
    task_id = "clru0001test"
    wt, base_sha = tasks._create_worktree(git_repo, task_id)
    old = time.time() - 72 * 3600
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, base_sha, run_workdir, prompt, status, "
            "created_ts, finished_ts) VALUES (?, ?, ?, ?, 'test', 'done', ?, ?)",
            (task_id, str(git_repo), base_sha, str(wt), old, old),
        )
        con.commit()
    # DB has run_workdir
    t_before = tasks.get_task(task_id)
    assert t_before["run_workdir"] is not None

    result = tasks.cleanup_worktrees(max_age_hours=48)
    assert result["count"] == 1
    assert not wt.exists()
    # DB run_workdir cleared
    t_after = tasks.get_task(task_id)
    assert t_after["run_workdir"] is None


# ── cherry-pick --abort 失败处理 ────────────────────────────────

def test_cherry_pick_failure_restores_worktree_for_retry(
    task_with_worktree, monkeypatch
):
    """cherry-pick 失败后恢复 staged diff，重试不得把已提交改动当成空 diff。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    source = task_with_worktree["source"]
    (wt / "retry.txt").write_text("must survive")
    preview = tasks.task_diff(tid)

    original_git = tasks._git
    fail_once = True

    def mock_git(args, cwd, **kwargs):
        nonlocal fail_once
        if len(args) >= 2 and args[0] == "cherry-pick" and args[1] == "--abort":
            result = MagicMock(returncode=0, stdout="", stderr="")
            return result
        if len(args) >= 2 and args[0] == "cherry-pick" and fail_once:
            fail_once = False
            result = MagicMock(returncode=1, stdout="", stderr="conflict")
            return result
        return original_git(args, cwd, **kwargs)

    monkeypatch.setattr(tasks, "_git", mock_git)
    with pytest.raises(ValueError, match="cherry-pick"):
        tasks.task_apply(tid, "apply")

    # 失败后仍是原 base，且原预览 diff 可直接重试。
    assert original_git(["rev-parse", "HEAD"], wt).stdout.strip() == task_with_worktree["base_sha"]
    assert tasks.task_diff(tid)["preview_hash"] == preview["preview_hash"]

    result = tasks.task_apply(tid, "apply")
    assert result["action"] == "apply"
    assert (source / "retry.txt").read_text() == "must survive"


def test_cherry_pick_abort_failure_reports_manual_intervention(
    task_with_worktree, monkeypatch
):
    """cherry-pick 失败且 --abort 也失败时,报源仓库需人工处理。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    source = task_with_worktree["source"]
    (wt / "new.txt").write_text("content")
    tasks.task_diff(tid)

    original_git = tasks._git
    call_log: list[list[str]] = []

    def mock_git(args, cwd, **kwargs):
        call_log.append(list(args))
        # cherry-pick succeeds (commit creation)
        # but cherry-pick <sha> fails and --abort also fails
        if len(args) >= 2 and args[0] == "cherry-pick" and args[1] == "--abort":
            r = MagicMock()
            r.returncode = 1
            r.stderr = "abort failed: index conflict"
            r.stdout = ""
            return r
        if len(args) >= 2 and args[0] == "cherry-pick":
            r = MagicMock()
            r.returncode = 1
            r.stderr = "cherry-pick conflict"
            r.stdout = ""
            return r
        return original_git(args, cwd, **kwargs)

    monkeypatch.setattr(tasks, "_git", mock_git)
    with pytest.raises(ValueError, match="人工处理"):
        tasks.task_apply(tid, "apply")


def test_cherry_pick_timeout_aborts_and_restores_worktree(
    task_with_worktree, monkeypatch
):
    """cherry-pick 超时也必须 abort，并恢复 staged diff 供重试。"""
    tid = task_with_worktree["id"]
    wt = task_with_worktree["run_workdir"]
    base_sha = task_with_worktree["base_sha"]
    (wt / "new.txt").write_text("content")
    tasks.task_diff(tid)

    original_git = tasks._git
    calls: list[list[str]] = []

    def mock_git(args, cwd, **kwargs):
        calls.append(list(args))
        if len(args) >= 2 and args[0] == "cherry-pick" and args[1] != "--abort":
            raise ValueError("git cherry-pick 超时(>60s)")
        if args == ["cherry-pick", "--abort"]:
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result
        return original_git(args, cwd, **kwargs)

    monkeypatch.setattr(tasks, "_git", mock_git)

    with pytest.raises(ValueError, match="超时"):
        tasks.task_apply(tid, "apply")

    assert ["cherry-pick", "--abort"] in calls
    assert tasks._git_ok(["rev-parse", "HEAD"], wt)[1] == base_sha
    assert "new.txt" in tasks._git(["diff", "--cached", "--name-only"], wt).stdout


# ── _run_codex 子进程清理 ───────────────────────────────────────

def test_run_codex_terminates_proc_on_exception(temp_data, monkeypatch):
    """_run_codex 在 Popen 后异常时 terminate→kill 子进程。"""
    task_id = "proc_test_001"
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, prompt, status, created_ts) "
            "VALUES (?, '/tmp/fake', 'test', 'running', ?)",
            (task_id, now),
        )
        con.commit()
    tasks._output_buffers[task_id] = []

    # Mock Popen that simulates a long-running process
    mock_proc = MagicMock()
    mock_proc.pid = 99999
    mock_proc.poll.return_value = None  # process is running
    mock_proc.stdout = iter([])  # empty output
    mock_proc.stdin = None
    mock_proc.wait.return_value = 0
    mock_proc.returncode = 0

    call_count = [0]

    original_popen = subprocess.Popen

    def mock_popen(*args, **kwargs):
        call_count[0] += 1
        # Return mock proc, but next DB call will fail
        return mock_proc

    # Make the DB update after Popen fail
    original_db = tasks._db

    def failing_db():
        con = original_db()
        # Fail on the 3rd call (pid update)
        return con

    monkeypatch.setattr(tasks.subprocess, "Popen", mock_popen)

    # Make pid DB update raise to trigger exception after Popen
    db_calls = [0]
    orig_db = tasks._db

    def fail_on_pid_update():
        db_calls[0] += 1
        con = orig_db()
        if db_calls[0] == 2:  # second _db() call is the pid update
            con.close()
            raise sqlite3.OperationalError("simulated pid update failure")
        return con

    monkeypatch.setattr(tasks, "_db", fail_on_pid_update)
    monkeypatch.setattr(tasks, "CODEX_BIN", "/fake/codex")

    tasks._run_codex(task_id, "/tmp/fake", "test", [], None)

    # proc.terminate or proc.kill was called
    assert mock_proc.terminate.called or mock_proc.kill.called


def test_ensure_proc_terminated_already_exited():
    """_ensure_proc_terminated 对已退出的进程不做操作。"""
    proc = MagicMock()
    proc.poll.return_value = 0  # already exited
    tasks._ensure_proc_terminated(proc)
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


def test_ensure_proc_terminated_terminates_running():
    """_ensure_proc_terminated 对运行中进程先 terminate。"""
    proc = MagicMock()
    proc.poll.return_value = None  # running
    proc.wait.return_value = 0  # terminate succeeds
    tasks._ensure_proc_terminated(proc)
    proc.terminate.assert_called_once()
    proc.kill.assert_not_called()


def test_ensure_proc_terminated_kills_on_terminate_timeout():
    """_ensure_proc_terminated 在 terminate 超时后 kill。"""
    proc = MagicMock()
    proc.poll.return_value = None  # running
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="x", timeout=5), 0]
    tasks._ensure_proc_terminated(proc)
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


def test_get_task_uses_persisted_tail_after_buffer_reclaimed(temp_data):
    """完成任务的内存缓冲回收后仍能从 DB 返回最后输出。"""
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, prompt, status, created_ts, output_tail) "
            "VALUES ('tail_task_01', '/tmp/fake', 'test', 'done', ?, ?)",
            (now, "line one\nline two"),
        )
        con.commit()

    task = tasks.get_task("tail_task_01")

    assert task is not None
    assert task["output"] == ["line one", "line two"]


def test_run_codex_reclaims_completed_output_buffer(temp_data, monkeypatch):
    """worker 完成后持久化 tail 并释放对应内存缓冲。"""
    task_id = "reclaim_task"
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, prompt, status, created_ts) "
            "VALUES (?, '/tmp/fake', 'test', 'pending', ?)",
            (task_id, now),
        )
        con.commit()
    tasks._output_buffers[task_id] = []

    proc = MagicMock()
    proc.pid = 12345
    proc.stdout = iter(["last line\n"])
    proc.stdin = None
    proc.returncode = 0
    proc.poll.return_value = 0
    proc.wait.return_value = 0
    monkeypatch.setattr(tasks.subprocess, "Popen", lambda *args, **kwargs: proc)

    tasks._run_codex(task_id, "/tmp/fake", "test", [], None)

    assert task_id not in tasks._output_buffers
    task = tasks.get_task(task_id)
    assert task is not None
    assert task["output"] == ["last line"]


def test_run_codex_uses_bounded_process_wait(temp_data, monkeypatch):
    """stdout 关闭后进程不退出时进入已有 terminate 清理路径。"""
    task_id = "wait_timeout"
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, prompt, status, created_ts) "
            "VALUES (?, '/tmp/fake', 'test', 'pending', ?)",
            (task_id, now),
        )
        con.commit()
    tasks._output_buffers[task_id] = []

    proc = MagicMock()
    proc.pid = 12345
    proc.stdout = iter([])
    proc.stdin = None
    proc.returncode = None
    proc.poll.return_value = None
    proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="codex", timeout=5),
        0,
    ]
    monkeypatch.setattr(tasks.subprocess, "Popen", lambda *args, **kwargs: proc)

    tasks._run_codex(task_id, "/tmp/fake", "test", [], None)

    assert proc.wait.call_args_list[0].kwargs == {"timeout": 5}
    proc.terminate.assert_called_once()
    task = tasks.get_task(task_id)
    assert task is not None
    assert task["status"] == "failed"


def test_run_codex_reports_missing_stdout_explicitly(temp_data, monkeypatch):
    """Popen 未提供 stdout 时返回稳定错误，不依赖可被优化掉的 assert。"""
    task_id = "missing_stdout"
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, prompt, status, created_ts) "
            "VALUES (?, '/tmp/fake', 'test', 'pending', ?)",
            (task_id, now),
        )
        con.commit()
    tasks._output_buffers[task_id] = []

    proc = MagicMock()
    proc.pid = 12345
    proc.stdout = None
    proc.stdin = None
    proc.poll.return_value = 0
    monkeypatch.setattr(tasks.subprocess, "Popen", lambda *args, **kwargs: proc)

    tasks._run_codex(task_id, "/tmp/fake", "test", [], None)

    task = tasks.get_task(task_id)
    assert task is not None
    assert any("RuntimeError" in line and "stdout" in line for line in task["output"])


# ── 并发：discard/diff/apply 同源互斥 ───────────────────────────

def test_discard_waits_for_apply_lock(temp_data, git_repo, monkeypatch):
    """discard 等待同一 source 的 apply lock,不在 apply 持锁期间删除 worktree。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)
    task_id = "race001test01"
    source = git_repo.resolve()
    run_wt, base_sha = tasks._create_worktree(source, task_id)
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, source_workdir, base_sha, run_workdir, "
            "prompt, images, model, status, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'done', ?)",
            (task_id, str(source), str(source), base_sha, str(run_wt),
             "test", "[]", None, now),
        )
        con.commit()
    (run_wt / "new.txt").write_text("content")
    tasks.task_diff(task_id)

    apply_in_lock = threading.Event()
    apply_can_finish = threading.Event()

    original_stage = tasks._stage_and_diff

    def blocking_stage(run_workdir):
        apply_in_lock.set()
        apply_can_finish.wait(timeout=5)
        return original_stage(run_workdir)

    monkeypatch.setattr(tasks, "_stage_and_diff", blocking_stage)

    apply_result = [None]
    apply_error = [None]

    def run_apply():
        try:
            apply_result[0] = tasks.task_apply(task_id, "apply")
        except Exception as e:
            apply_error[0] = e

    t_apply = threading.Thread(target=run_apply)
    t_apply.start()

    # 等 apply 进入锁内
    assert apply_in_lock.wait(timeout=5)

    # 此时发 discard,应该被阻塞
    discard_result = [None]
    discard_done = threading.Event()

    def run_discard():
        try:
            discard_result[0] = tasks.task_apply(task_id, "stash")
        except Exception as e:
            discard_result[0] = e
        finally:
            discard_done.set()

    t_discard = threading.Thread(target=run_discard)
    t_discard.start()

    # discard 未完成(在等锁)
    assert not discard_done.wait(timeout=0.5)
    # worktree 仍存在(apply 尚未完成,discard 未执行)
    assert run_wt.exists()

    # 放行 apply
    apply_can_finish.set()
    t_apply.join(timeout=10)

    # discard 现在应完成
    assert discard_done.wait(timeout=5)
    t_discard.join(timeout=5)

    # apply 成功
    assert apply_error[0] is None, f"apply failed: {apply_error[0]}"
    assert apply_result[0] is not None
    assert apply_result[0]["action"] == "apply"
    # 改动已进入源仓库
    assert (source / "new.txt").exists()

    # discard 在 apply 之后执行,worktree 已被 apply 清理
    # discard 在锁内重新读取发现 run_workdir 已清空
    assert discard_result[0] is not None


def test_diff_waits_for_discard_lock(temp_data, git_repo, monkeypatch):
    """task_diff 等待同一 source 的 discard lock,不并发操作 worktree。"""
    monkeypatch.setattr(tasks, "_check_workdir_allowed", lambda w: None)
    task_id = "race002test02"
    source = git_repo.resolve()
    run_wt, base_sha = tasks._create_worktree(source, task_id)
    now = time.time()
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks (id, workdir, source_workdir, base_sha, run_workdir, "
            "prompt, images, model, status, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'done', ?)",
            (task_id, str(source), str(source), base_sha, str(run_wt),
             "test", "[]", None, now),
        )
        con.commit()
    (run_wt / "file.txt").write_text("content")

    discard_in_lock = threading.Event()
    discard_can_finish = threading.Event()

    original_remove = tasks._remove_worktree

    def blocking_remove(src, wt):
        discard_in_lock.set()
        discard_can_finish.wait(timeout=5)
        original_remove(src, wt)

    monkeypatch.setattr(tasks, "_remove_worktree", blocking_remove)

    discard_done = threading.Event()
    discard_result = [None]

    def run_discard():
        try:
            discard_result[0] = tasks.task_apply(task_id, "stash")
        except Exception as e:
            discard_result[0] = e
        finally:
            discard_done.set()

    t_discard = threading.Thread(target=run_discard)
    t_discard.start()

    # 等 discard 进入锁内
    assert discard_in_lock.wait(timeout=5)

    # 此时发 task_diff,应被阻塞
    diff_result = [None]
    diff_error = [None]
    diff_done = threading.Event()

    def run_diff():
        try:
            diff_result[0] = tasks.task_diff(task_id)
        except Exception as e:
            diff_error[0] = e
        finally:
            diff_done.set()

    t_diff = threading.Thread(target=run_diff)
    t_diff.start()

    # diff 未完成(在等锁)
    assert not diff_done.wait(timeout=0.5)

    # 放行 discard
    discard_can_finish.set()
    assert discard_done.wait(timeout=5)
    t_discard.join(timeout=5)

    # diff 现在应完成(但 worktree 已被 discard 删除)
    assert diff_done.wait(timeout=5)
    t_diff.join(timeout=5)

    # diff 应报 worktree 不存在
    assert diff_error[0] is not None
    assert "worktree" in str(diff_error[0])
