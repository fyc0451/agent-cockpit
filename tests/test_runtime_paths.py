"""Wiki13 J1B1:runtime_paths 行为测试。

覆盖合同(#1929):invalid-input fail-closed、全模块隔离 HOME import 零侧效、
默认路径与 J0 逐字节兼容、多 profile env 覆盖不重叠、inspect 纯读判定。
"""
import os
import subprocess
import sys
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
    for var in ("COCKPIT_DATA_DIR", "COCKPIT_CONFIG_DIR", "COCKPIT_STATE_DIR",
                "COCKPIT_UPLOADS_DIR", "COCKPIT_COORDINATION_DB"):
        monkeypatch.delenv(var, raising=False)
    runtime_paths.reset_cache()
    return home


# ── invalid-input fail-closed ─────────────────────────────────


class TestInvalidInput:
    def test_relative_coordination_env_falls_back(self, fake_home, monkeypatch):
        monkeypatch.setenv("COCKPIT_COORDINATION_DB", "rel/x.db")
        assert runtime_paths.store("coordination") == (
            fake_home / "dashboard-data" / "coordination.sqlite3"
        )
        assert any(d["reason"] == "relative_path" for d in runtime_paths.diagnostics())

    def test_nul_rejected(self):
        with pytest.raises(runtime_paths.PathResolutionError) as exc:
            runtime_paths.canonicalize("/tmp/a\x00b", env_name="X")
        assert exc.value.reason == "nul_or_invalid"

    def test_empty_env_falls_back(self, fake_home, monkeypatch):
        monkeypatch.setenv("COCKPIT_DATA_DIR", "   ")
        assert runtime_paths.data_root() == fake_home / "dashboard-data"

    def test_absolute_coordination_env_honored(self, fake_home, monkeypatch, tmp_path):
        target = tmp_path / "alt" / "c.sqlite3"
        monkeypatch.setenv("COCKPIT_COORDINATION_DB", str(target))
        assert runtime_paths.store("coordination") == target.resolve()

    def test_data_root_override_honored(self, fake_home, monkeypatch, tmp_path):
        alt = tmp_path / "alt-data"
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(alt))
        assert runtime_paths.data_root() == alt.resolve()
        assert runtime_paths.store("settings") == alt.resolve() / "settings.json"

    def test_bundle_path_rejected(self, fake_home, monkeypatch):
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(runtime_paths.INSTALL_ROOT / "x"))
        assert runtime_paths.data_root() == fake_home / "dashboard-data"
        assert any(d["reason"] == "bundle_path" for d in runtime_paths.diagnostics())

    def test_symlink_escape_rejected(self, fake_home, monkeypatch, tmp_path):
        real = tmp_path / "elsewhere"
        real.mkdir()
        link = fake_home / "link-data"
        link.symlink_to(real)
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(link))
        assert runtime_paths.data_root() == fake_home / "dashboard-data"
        assert any(d["reason"] == "symlink_escape" for d in runtime_paths.diagnostics())

    def test_overlap_roots_fall_back(self, fake_home, monkeypatch, tmp_path):
        alt = tmp_path / "shared"
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(alt))
        monkeypatch.setenv("COCKPIT_UPLOADS_DIR", str(alt / "sub"))
        assert runtime_paths.data_root() == alt.resolve()
        assert runtime_paths.uploads_root() == fake_home / "dashboard-uploads"
        assert any(d["reason"] == "store_overlap" for d in runtime_paths.diagnostics())


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

    def test_module_constants_follow_resolver(self, fake_home):
        import importlib
        for mod in ("settings", "tasks", "web_push", "mail_projects",
                    "team_sessions", "uploads", "coordination"):
            importlib.reload(importlib.import_module(mod))
        import settings, tasks, web_push, mail_projects, team_sessions
        import uploads, coordination
        assert settings.DATA_DIR == fake_home / "dashboard-data"
        assert settings.SETTINGS_PATH == fake_home / "dashboard-data" / "settings.json"
        assert tasks.TASKS_DB == fake_home / "dashboard-data" / "tasks.sqlite3"
        assert tasks.WORKTREE_ROOT == fake_home / "dashboard-data" / "worktrees"
        assert web_push.DB_PATH == fake_home / "dashboard-data" / "push.sqlite3"
        assert web_push.KEY_PATH == fake_home / "dashboard-data" / "vapid-private.pem"
        assert mail_projects.STATE_PATH == fake_home / "dashboard-data" / "mail-projects.json"
        assert team_sessions.STATE_PATH == fake_home / "dashboard-data" / "team-sessions.json"
        assert uploads.UPLOAD_DIR == fake_home / "dashboard-uploads"
        assert coordination.DB_PATH == fake_home / "dashboard-data" / "coordination.sqlite3"


# ── 多 profile 不重叠 ─────────────────────────────────────────


class TestMultiProfile:
    def test_distinct_roots_no_overlap(self, fake_home, monkeypatch, tmp_path):
        a, b = tmp_path / "profile-a", tmp_path / "profile-b"
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(a))
        monkeypatch.setenv("COCKPIT_UPLOADS_DIR", str(b))
        assert runtime_paths.store("tasks") == a.resolve() / "tasks.sqlite3"
        assert runtime_paths.uploads_root() == b.resolve()
        assert not runtime_paths.diagnostics()

    def test_nested_override_rejected_both_ways(self, fake_home, monkeypatch, tmp_path):
        outer = tmp_path / "outer"
        monkeypatch.setenv("COCKPIT_UPLOADS_DIR", str(outer))
        monkeypatch.setenv("COCKPIT_DATA_DIR", str(outer / "inner"))
        # uploads 先解析(顺序 data<config<state<uploads?data 先)→data 生效,
        # uploads 与 data 重叠则回落;无论哪侧回落,二者绝不嵌套
        d, u = runtime_paths.data_root(), runtime_paths.uploads_root()
        assert not str(u).startswith(str(d) + os.sep) or d == u
        assert runtime_paths.diagnostics()


# ── inspect 纯读判定 ──────────────────────────────────────────


class TestInspect:
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
        # tasks 应为 file,放一个目录 → type_mismatch
        (fake_home / "dashboard-data").mkdir(parents=True)
        (fake_home / "dashboard-data" / "tasks.sqlite3").mkdir()
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "tasks")
        assert entry["ready"] is False
        assert entry["reason"] == "type_mismatch_dir_not_file"
        assert snap["ready"] is False

    def test_world_writable_parent_not_ready(self, fake_home):
        data = fake_home / "dashboard-data"
        data.mkdir()
        data.chmod(0o777)
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "settings")
        assert entry["ready"] is False
        assert entry["reason"] == "parent_world_writable"

    def test_existing_store_readable_writable(self, fake_home):
        data = fake_home / "dashboard-data"
        data.mkdir(parents=True)
        (data / "settings.json").write_text("{}", encoding="utf-8")
        snap = runtime_paths.inspect()
        entry = next(s for s in snap["stores"] if s["name"] == "settings")
        assert entry["exists"] is True and entry["ready"] is True
        assert entry["reason"] == "ok"


# ── 全模块隔离 HOME import 零侧效 ─────────────────────────────


@pytest.mark.parametrize("mod", MODULES)
def test_import_zero_side_effects(mod, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ, "HOME": str(home), "PYTHONPATH": REPO_ROOT,
    }
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
