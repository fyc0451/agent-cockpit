"""Wiki13 J1B1 R2:runtime_paths 行为测试。

覆盖合同(#1933 A-F):
- A 非空显式 override 原子 fail-closed:非法直接抛 PathResolutionError,
  绝不回落默认;仅 unset/全空回默认;错误不含完整敏感路径。
- B 最终四根整体校验:拒绝 /、HOME、install root 及祖先/内部、双向嵌套。
- C COCKPIT_COORDINATION_DB 过 bundle+过宽根+全命名 store 碰撞门。
- D inspect 真实写语义:父目录可写可执行、敏感 store 声明 mode、
  group/world 不安全、类型错位、首装 creatable 纯读。
- E 自定义 profile 不暴露 legacy home 根。
- F 定向矩阵 + 默认兼容 + 13 模块隔离 HOME import 零写盘 +
  tasks 首次并发 _db barrier。
"""
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import runtime_paths

MODULES = [
    "runtime_paths", "settings", "tasks", "coordination", "web_push",
    "mail_projects", "team_sessions", "team_inbox_router", "terminal",
    "uploads", "files", "db", "server",
]

REPO_ROOT = str(Path(__file__).resolve().parent.parent)


@pytest.fixture(autouse=True)
def _reset_resolver():
    runtime_paths.reset_cache()
    yield
    runtime_paths.reset_cache()


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        runtime_paths, "_ROOT_DEFAULTS", {
            "data": home / "dashboard-data",
            "config": home / ".config" / "agent-cockpit",
            "state": home / ".local" / "state" / "agent-cockpit",
            "uploads": home / "dashboard-uploads",
        },
    )
    monkeypatch.setattr(runtime_paths, "_HOME_ROOT", home.resolve())
    for var in ("COCKPIT_DATA_DIR", "COCKPIT_CONFIG_DIR", "COCKPIT_STATE_DIR",
                "COCKPIT_UPLOADS_DIR", "COCKPIT_COORDINATION_DB"):
        monkeypatch.delenv(var, raising=False)
    runtime_paths.reset_cache()
    return home


def _expect_error(fn, reason):
    with pytest.raises(runtime_paths.PathResolutionError) as exc:
        fn()
    assert exc.value.reason == reason
    # R2-A:错误信息只含 reason+env 名,不回显完整敏感路径
    msg = str(exc.value)
    assert "/home" not in msg and "tmp" not in msg and str(reason) in msg
    return exc.value


# ── A 原子 fail-closed ────────────────────────────────────────


class TestAtomicFailClosed:
    def test_relative_root_override_raises(self, fake_home, monkeypatch):
        monkeypatch.setenv("COCKPIT_DATA_DIR", "rel/data")
        _expect_error(runtime_paths.data_root, "relative_path")

    def test_relative_coordination_override_raises(self, fake_home, monkeypatch):
        monkeypatch.setenv("COCKPIT_COORDINATION_DB", "rel/x.db")
        _expect_error(lambda: runtime_paths.store("coordination"), "relative_path")

    def test_nul_raises(self):
        with pytest.raises(runtime_paths.PathResolutionError) as exc:
            runtime_paths.canonicalize("/tmp/a\x00b", env_name="X")
        assert exc.value.reason == "nul_or_invalid"

    def test_symlink_escape_raises(self, fake_home, monkeypatch, tmp_path):
        real = tmp_path / "elsewhere"
        real.mkdir()
        link = fake_home / "link-data"
        link.symlink_to(real)
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(link))
        _expect_error(runtime_paths.data_root, "symlink_escape")

    def test_empty_and_unset_fall_back_to_default(self, fake_home, monkeypatch):
        monkeypatch.setenv("COCKPIT_DATA_DIR", "   ")
        assert runtime_paths.data_root() == fake_home / "dashboard-data"
        monkeypatch.delenv("COCKPIT_DATA_DIR")
        runtime_paths.reset_cache()
        assert runtime_paths.data_root() == fake_home / "dashboard-data"

    def test_absolute_override_honored_no_silent_swap(self, fake_home, monkeypatch, tmp_path):
        alt = tmp_path / "alt-data"
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(alt))
        assert runtime_paths.data_root() == alt.resolve()
        assert runtime_paths.store("settings") == alt.resolve() / "settings.json"

    def test_diagnostics_do_not_leak_paths(self, fake_home, monkeypatch):
        monkeypatch.setenv("COCKPIT_DATA_DIR", "rel/data")
        with pytest.raises(runtime_paths.PathResolutionError):
            runtime_paths.data_root()
        for d in runtime_paths.diagnostics():
            assert str(fake_home) not in str(d)


# ── B 全根集合校验 ────────────────────────────────────────────


class TestRootSetValidation:
    def test_data_eq_home_rejected(self, fake_home, monkeypatch):
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(fake_home))
        _expect_error(runtime_paths.data_root, "broad_root")

    def test_root_slash_rejected(self, fake_home, monkeypatch):
        monkeypatch.setenv("COCKPIT_DATA_DIR", "/")
        _expect_error(runtime_paths.data_root, "broad_root")

    def test_bundle_root_rejected(self, fake_home, monkeypatch):
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(runtime_paths.INSTALL_ROOT / "x"))
        _expect_error(runtime_paths.data_root, "bundle_path")

    def test_install_root_ancestor_rejected(self, fake_home, monkeypatch):
        # install root 的祖先作为持久根:bundle 落在根内
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(runtime_paths.INSTALL_ROOT.parent))
        _expect_error(runtime_paths.data_root, "broad_root")

    def test_nested_roots_rejected(self, fake_home, monkeypatch, tmp_path):
        outer = tmp_path / "outer"
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(outer))
        monkeypatch.setenv("COCKPIT_UPLOADS_DIR", str(outer / "inner"))
        _expect_error(runtime_paths.uploads_root, "root_nested")

    def test_equal_roots_rejected(self, fake_home, monkeypatch, tmp_path):
        same = tmp_path / "same"
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(same))
        monkeypatch.setenv("COCKPIT_UPLOADS_DIR", str(same))
        _expect_error(runtime_paths.uploads_root, "root_nested")


# ── C coordination 碰撞门 ─────────────────────────────────────


class TestCoordinationGates:
    def test_coordination_equals_tasks_store_rejected(self, fake_home, monkeypatch):
        monkeypatch.setenv(
            "COCKPIT_COORDINATION_DB",
            str(fake_home / "dashboard-data" / "tasks.sqlite3"),
        )
        _expect_error(lambda: runtime_paths.store("coordination"), "store_collision")

    def test_coordination_equals_push_store_rejected(self, fake_home, monkeypatch):
        monkeypatch.setenv(
            "COCKPIT_COORDINATION_DB",
            str(fake_home / "dashboard-data" / "push.sqlite3"),
        )
        _expect_error(lambda: runtime_paths.store("coordination"), "store_collision")

    def test_coordination_equals_root_rejected(self, fake_home, monkeypatch):
        monkeypatch.setenv("COCKPIT_COORDINATION_DB", str(fake_home / "dashboard-data"))
        _expect_error(lambda: runtime_paths.store("coordination"), "store_collision")

    def test_coordination_in_bundle_rejected(self, fake_home, monkeypatch):
        monkeypatch.setenv(
            "COCKPIT_COORDINATION_DB",
            str(runtime_paths.INSTALL_ROOT / "c.sqlite3"),
        )
        _expect_error(lambda: runtime_paths.store("coordination"), "bundle_path")

    def test_coordination_external_safe_path_ok(self, fake_home, monkeypatch, tmp_path):
        ext = tmp_path / "ext" / "c.sqlite3"
        monkeypatch.setenv("COCKPIT_COORDINATION_DB", str(ext))
        assert runtime_paths.store("coordination") == ext.resolve()

    def test_coordination_relative_rejected(self, fake_home, monkeypatch):
        monkeypatch.setenv("COCKPIT_COORDINATION_DB", "c.db")
        _expect_error(lambda: runtime_paths.store("coordination"), "relative_path")


# ── D inspect 写语义 ──────────────────────────────────────────


class TestInspectWriteSemantics:
    def test_fresh_home_ready_creatable(self, fake_home):
        snap = runtime_paths.inspect()
        assert snap["ready"] is True
        for s in snap["stores"]:
            assert s["exists"] is False
            assert s["reason"] == "creatable"

    def test_inspect_creates_nothing(self, fake_home):
        runtime_paths.inspect()
        assert list(fake_home.iterdir()) == []

    def test_type_mismatch_not_ready(self, fake_home):
        (fake_home / "dashboard-data").mkdir(parents=True)
        (fake_home / "dashboard-data" / "tasks.sqlite3").mkdir()
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "tasks")
        assert entry["ready"] is False
        assert entry["reason"] == "type_mismatch_dir_not_file"
        assert snap["ready"] is False

    def test_vapid_0644_rejected_by_declared_mode(self, fake_home):
        data = fake_home / "dashboard-data"
        data.mkdir()
        key = data / "vapid-private.pem"
        key.write_text("pem")
        key.chmod(0o644)
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "vapid")
        assert entry["ready"] is False and entry["reason"] == "insecure_mode"

    def test_vapid_0600_ok(self, fake_home):
        data = fake_home / "dashboard-data"
        data.mkdir()
        key = data / "vapid-private.pem"
        key.write_text("pem")
        key.chmod(0o600)
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "vapid")
        assert entry["ready"] is True and entry["reason"] == "ok"

    def test_settings_writable_but_parent_0500_rejected(self, fake_home):
        data = fake_home / "dashboard-data"
        data.mkdir()
        s = data / "settings.json"
        s.write_text("{}")
        s.chmod(0o600)
        data.chmod(0o500)  # 父目录只读:原子替换不可行
        try:
            snap = runtime_paths.inspect()
            entry = next(x for x in snap["stores"] if x["name"] == "settings")
            assert entry["ready"] is False
            assert entry["reason"] == "parent_not_writable"
            assert snap["ready"] is False
        finally:
            data.chmod(0o755)

    def test_world_writable_parent_not_ready(self, fake_home):
        data = fake_home / "dashboard-data"
        data.mkdir()
        data.chmod(0o777)
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "settings")
        assert entry["ready"] is False
        assert entry["reason"] == "parent_insecure_mode"

    def test_group_writable_existing_store_not_ready(self, fake_home):
        data = fake_home / "dashboard-data"
        data.mkdir()
        db = data / "tasks.sqlite3"
        db.write_bytes(b"")
        db.chmod(0o664)  # group 可写
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "tasks")
        assert entry["ready"] is False and entry["reason"] == "insecure_mode"

    def test_existing_store_readable_writable_ok(self, fake_home):
        data = fake_home / "dashboard-data"
        data.mkdir()
        s = data / "settings.json"
        s.write_text("{}", encoding="utf-8")
        s.chmod(0o600)
        snap = runtime_paths.inspect()
        entry = next(x for x in snap["stores"] if x["name"] == "settings")
        assert entry["exists"] is True and entry["ready"] is True
        assert entry["reason"] == "ok"


# ── E profile 隔离 ────────────────────────────────────────────


class TestProfileIsolation:
    def test_custom_profile_does_not_expose_legacy_roots(
        self, fake_home, monkeypatch, tmp_path,
    ):
        import files
        alt_data, alt_up = tmp_path / "p-data", tmp_path / "p-uploads"
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(alt_data))
        monkeypatch.setenv("COCKPIT_UPLOADS_DIR", str(alt_up))
        runtime_paths.reset_cache()
        monkeypatch.setattr(files, "_HOME", fake_home.resolve())
        roots = files._system_roots()
        assert alt_data.resolve() in roots
        assert alt_up.resolve() in roots
        # legacy 默认 home 存储不得出现在自定义 profile 白名单
        assert (fake_home / "dashboard-data").resolve() not in roots
        assert (fake_home / "dashboard-uploads").resolve() not in roots
        # agent-mail-tools 兼容根保留
        assert (fake_home / "agent-mail-tools").resolve() in roots


# ── 默认兼容(J0 逐字节)──────────────────────────────────────


class TestDefaultCompat:
    def test_all_defaults_match_j0_layout(self, fake_home):
        expect = {
            "settings": "dashboard-data/settings.json",
            "tasks": "dashboard-data/tasks.sqlite3",
            "worktrees": "dashboard-data/worktrees",
            "coordination": "dashboard-data/coordination.sqlite3",
            "push": "dashboard-data/push.sqlite3",
            "vapid": "dashboard-data/vapid-private.pem",
            "mail_projects": "dashboard-data/mail-projects.json",
            "team_sessions": "dashboard-data/team-sessions.json",
            "inbox_route": "dashboard-data/team-inbox-route.json",
            "upgrade": "dashboard-data/upgrade",
            "typing": ".local/state/agent-cockpit/typing.json",
            "file_roots": ".config/agent-cockpit/file-roots.json",
        }
        for name, rel in expect.items():
            assert runtime_paths.store(name) == fake_home / rel, name


# ── tasks 首次并发 _db barrier ────────────────────────────────


# ── R3-A 目录 store containment ───────────────────────────────


class TestR3CoordinationContainment:
    def test_coordination_inside_worktrees_rejected(self, fake_home, monkeypatch):
        monkeypatch.setenv(
            "COCKPIT_COORDINATION_DB",
            str(fake_home / "dashboard-data" / "worktrees" / "coordination.sqlite3"),
        )
        _expect_error(lambda: runtime_paths.store("coordination"), "store_collision")

    def test_coordination_inside_upgrade_rejected(self, fake_home, monkeypatch):
        monkeypatch.setenv(
            "COCKPIT_COORDINATION_DB",
            str(fake_home / "dashboard-data" / "upgrade" / "coordination.sqlite3"),
        )
        _expect_error(lambda: runtime_paths.store("coordination"), "store_collision")

    def test_coordination_external_safe_still_ok(self, fake_home, monkeypatch, tmp_path):
        ext = tmp_path / "ext" / "c.sqlite3"
        monkeypatch.setenv("COCKPIT_COORDINATION_DB", str(ext))
        assert runtime_paths.store("coordination") == ext.resolve()


# ── R3-B symlink 逃逸 fail-closed ─────────────────────────────


class TestR3SymlinkEscape:
    def _data(self, fake_home):
        data = fake_home / "dashboard-data"
        data.mkdir(exist_ok=True)
        return data

    def test_file_symlink_to_outside_existing_not_ready(self, fake_home, tmp_path):
        data = self._data(fake_home)
        outside = tmp_path / "outside.sqlite3"
        outside.write_bytes(b"x")
        (data / "tasks.sqlite3").symlink_to(outside)
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "tasks")
        assert entry["ready"] is False and entry["reason"] == "symlink_escape"
        assert snap["ready"] is False

    def test_dangling_file_symlink_not_ready(self, fake_home):
        data = self._data(fake_home)
        (data / "tasks.sqlite3").symlink_to(tmp_target := fake_home / "ghost-nowhere")
        assert not tmp_target.exists()
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "tasks")
        assert entry["ready"] is False and entry["reason"] == "symlink_escape"

    def test_dir_symlink_to_outside_not_ready(self, fake_home, tmp_path):
        data = self._data(fake_home)
        outside_dir = tmp_path / "outside-dir"
        outside_dir.mkdir()
        (data / "worktrees").symlink_to(outside_dir)
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "worktrees")
        assert entry["ready"] is False and entry["reason"] == "symlink_escape"
        assert snap["ready"] is False

    def test_internal_regular_file_still_ok(self, fake_home):
        data = self._data(fake_home)
        db = data / "tasks.sqlite3"
        db.write_bytes(b"")
        db.chmod(0o644)
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "tasks")
        assert entry["ready"] is True and entry["reason"] == "ok"

    def test_external_coordination_non_symlink_inspect_ok(self, fake_home, monkeypatch, tmp_path):
        ext = tmp_path / "ext"
        ext.mkdir()
        db = ext / "c.sqlite3"
        db.write_bytes(b"")
        db.chmod(0o644)
        monkeypatch.setenv("COCKPIT_COORDINATION_DB", str(db))
        runtime_paths.reset_cache()
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "coordination")
        assert entry["ready"] is True and entry["reason"] == "ok"

    def test_writer_guard_raises_before_ddl(self, fake_home, tmp_path, monkeypatch):
        import tasks
        data = self._data(fake_home)
        outside = tmp_path / "victim.sqlite3"
        link = data / "tasks.sqlite3"
        link.symlink_to(outside)
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(data))
        runtime_paths.reset_cache()
        monkeypatch.setattr(tasks, "TASKS_DB", link)
        monkeypatch.setattr(tasks, "_db_swept", False)
        _expect_error(tasks._db, "symlink_escape")
        # fail-closed:未建库、未写受害者文件
        assert not outside.exists()

    def test_validate_store_error_message_no_path_leak(self, fake_home, tmp_path):
        data = self._data(fake_home)
        outside = tmp_path / "leak.sqlite3"
        (data / "settings.json").symlink_to(outside)
        with pytest.raises(runtime_paths.PathResolutionError) as exc:
            runtime_paths.validate_store("settings")
        msg = str(exc.value)
        assert str(outside) not in msg and str(data) not in msg
        assert "symlink_escape" in msg

    def test_intermediate_root_symlink_not_ready(self, fake_home, tmp_path):
        # root 本身被替换成指向外部的 symlink:解析期即 fail-closed
        import shutil
        data = self._data(fake_home)
        outside = tmp_path / "outside-data"
        outside.mkdir()
        shutil.rmtree(data)
        data.symlink_to(outside)
        runtime_paths.reset_cache()
        with pytest.raises(runtime_paths.PathResolutionError) as exc:
            runtime_paths.inspect()
        assert exc.value.reason == "symlink_escape"

    def test_intermediate_component_symlink_rejected(self, fake_home, tmp_path):
        # state 根路径的中间组件(~/.local)是链接:非 final 层也必须拒绝
        outside = tmp_path / "dot-local-outside"
        outside.mkdir()
        (fake_home / ".local").symlink_to(outside)
        runtime_paths.reset_cache()
        _expect_error(runtime_paths.state_root, "symlink_escape")


# ── R3 worktree 逃逸 barrier(真实 git 仓库)─────────────────


class TestR3WorktreeEscape:
    @pytest.fixture
    def git_source(self, tmp_path):
        import subprocess
        src = tmp_path / "src-repo"
        src.mkdir()
        subprocess.run(["git", "init", "-q", str(src)], check=True)
        (src / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(src), "add", "f.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "init"],
            check=True,
        )
        return src

    def test_create_worktree_fails_closed_before_git(
        self, fake_home, tmp_path, monkeypatch, git_source,
    ):
        import tasks
        data = fake_home / "dashboard-data"
        data.mkdir(exist_ok=True)
        victim = tmp_path / "victim-outside"
        (data / "worktrees").symlink_to(victim)
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(data))
        runtime_paths.reset_cache()
        monkeypatch.setattr(tasks, "WORKTREE_ROOT", data / "worktrees")
        _expect_error(
            lambda: tasks._create_worktree(git_source, "escape"),
            "symlink_escape",
        )
        # 未调用 git、未创建受害者目录及其内容
        assert not victim.exists()
        r = subprocess.run(
            ["git", "-C", str(git_source), "worktree", "list", "--porcelain"],
            capture_output=True, text=True,
        )
        assert "escape" not in r.stdout

    def test_remove_worktree_fails_closed_before_rmtree(
        self, fake_home, tmp_path, monkeypatch,
    ):
        import tasks
        data = fake_home / "dashboard-data"
        data.mkdir(exist_ok=True)
        victim = tmp_path / "victim-dir"
        victim.mkdir()
        (victim / "precious.txt").write_text("keep")
        (data / "worktrees").symlink_to(victim)
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(data))
        runtime_paths.reset_cache()
        monkeypatch.setattr(tasks, "WORKTREE_ROOT", data / "worktrees")
        with pytest.raises(runtime_paths.PathResolutionError) as exc:
            tasks._remove_worktree(None, data / "worktrees" / "t1")
        assert exc.value.reason == "symlink_escape"
        # 受害者目录与内容未被删除
        assert (victim / "precious.txt").read_text() == "keep"

    def test_validate_worktree_path_rejects_symlink_leaf(
        self, fake_home, tmp_path, monkeypatch,
    ):
        import tasks
        data = fake_home / "dashboard-data"
        (data / "worktrees").mkdir(parents=True)
        target = tmp_path / "real-dir"
        target.mkdir()
        link = data / "worktrees" / "linked"
        link.symlink_to(target)
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(data))
        runtime_paths.reset_cache()
        monkeypatch.setattr(tasks, "WORKTREE_ROOT", data / "worktrees")
        with pytest.raises(ValueError):
            tasks._validate_worktree_path(link)


# ── tasks 首次并发 _db barrier ────────────────────────────────


class TestTasksConcurrentFirstUse:
    def test_first_db_concurrent_barrier(self, tmp_path, monkeypatch):
        import tasks
        monkeypatch.setattr(tasks, "TASKS_DB", tmp_path / "tasks.sqlite3")
        monkeypatch.setattr(tasks, "_db_swept", False)
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker():
            try:
                barrier.wait(5)
                con = tasks._db()
                con.execute("SELECT count(*) FROM tasks").fetchone()
                cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
                assert {"source_workdir", "base_sha", "run_workdir", "preview_hash"} <= cols
                con.close()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert not errors, errors


# ── 全模块隔离 HOME import 零侧效 ─────────────────────────────


@pytest.mark.parametrize("mod", MODULES)
def test_import_zero_side_effects(mod, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": REPO_ROOT}
    for var in ("COCKPIT_DATA_DIR", "COCKPIT_CONFIG_DIR", "COCKPIT_STATE_DIR",
                "COCKPIT_UPLOADS_DIR", "COCKPIT_COORDINATION_DB", "XDG_DATA_HOME"):
        env.pop(var, None)
    proc = subprocess.run(
        [sys.executable, "-c", f"import {mod}"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    created = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert created == [], f"import {mod} 创建了文件: {created}"
