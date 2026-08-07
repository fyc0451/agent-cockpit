"""U1b：服务外升级核心 — 发布阻断回归。"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import upgrade_core


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True, check=True,
    )
    return r.stdout.strip()


@pytest.fixture
def install_tree(tmp_path, monkeypatch):
    root = tmp_path / "install"
    root.mkdir()
    (root / "server.py").write_text("# stub\n", encoding="utf-8")
    (root / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    (root / "requirements.txt").write_text("fastapi==0.128.0\n", encoding="utf-8")
    (root / ".venv" / "bin").mkdir(parents=True)
    for name in ("pip", "python"):
        p = root / ".venv" / "bin" / name
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    base_sha = _git(root, "rev-parse", "HEAD")
    (root / "VERSION").write_text("0.3.0\n", encoding="utf-8")
    _git(root, "add", "VERSION")
    _git(root, "commit", "-m", "v0.3.0")
    rel_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "tag", "v0.3.0")
    _git(root, "checkout", "-f", base_sha)
    (root / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    # origin/main 指向 release tip 供 ancestor 校验
    _git(root, "update-ref", "refs/remotes/origin/main", rel_sha)

    data = tmp_path / "dashboard-data"
    data.mkdir()
    monkeypatch.setattr(upgrade_core.settings, "DATA_DIR", data)
    monkeypatch.setattr(upgrade_core, "UPGRADE_DIR", data / "upgrade")
    monkeypatch.setattr(upgrade_core, "STATE_PATH", data / "upgrade" / "state.json")
    monkeypatch.setattr(upgrade_core, "LOCK_PATH", data / "upgrade" / "upgrade.lock")
    monkeypatch.setattr(upgrade_core, "LOG_DIR", data / "upgrade" / "logs")
    monkeypatch.setattr(upgrade_core, "BACKUP_ROOT", data / "upgrade" / "backups")
    monkeypatch.setattr(upgrade_core, "INSTALL_DIR", root)

    upgrade_core.clear_hooks()
    yield {"root": root, "base_sha": base_sha, "rel_sha": rel_sha, "data": data}
    upgrade_core.clear_hooks()


def _rel(tag_sha):
    return {
        "version": "0.3.0",
        "tag": "v0.3.0",
        "sha": tag_sha,
        "url": "https://github.com/fyc0451/agent-cockpit/releases/tag/v0.3.0",
        "name": "v0.3.0",
    }


def _hooks_success(root, rel_sha, events=None):
    events = events if events is not None else []

    def fetch_and_checkout(install_dir, tag, sha):
        events.append("checkout")
        _git(install_dir, "checkout", "-f", sha)
        # 模拟 tag 指向
        _git(install_dir, "tag", "-f", "v0.3.0", sha)

    def install_staging(install_dir):
        events.append("staging")
        staging = install_dir / upgrade_core.VENV_STAGING
        staging.mkdir(exist_ok=True)
        (staging / "ok").write_text("1", encoding="utf-8")
        return staging

    def switch(install_dir):
        events.append("switch")
        live = install_dir / upgrade_core.VENV_LIVE
        staging = install_dir / upgrade_core.VENV_STAGING
        prev = install_dir / upgrade_core.VENV_PREV
        if prev.exists():
            import shutil
            shutil.rmtree(prev, ignore_errors=True)
        if live.exists():
            os.rename(live, prev)
        os.rename(staging, live)

    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        preflight_supervisor=lambda: True,
        fetch_release=lambda tag: _rel(rel_sha),
        verify_tag_sha=lambda *a, **k: None,
        fetch_and_checkout=fetch_and_checkout,
        install_deps_staging=install_staging,
        atomic_switch_venv=switch,
        restart_cockpit=lambda: events.append("restart"),
        health_check=lambda: (events.append("health") or True),
        stop_cockpit=lambda: events.append("stop"),
    )
    return events


def test_toctou_exactly_one_accepted(install_tree):
    """获锁后 reconcile：确定性双请求恰好 1 accepted。"""
    root = install_tree["root"]
    rel_sha = install_tree["rel_sha"]
    first_spawned = threading.Event()
    hold = threading.Event()

    def slow_spawn(job_id, install_dir, log_path):
        first_spawned.set()
        hold.wait(timeout=5)
        st = upgrade_core.read_state()
        st["state"] = "installing"
        st["worker_pid"] = os.getpid()
        st["worker_started_at"] = "1"
        st["worker_start_boot_id"] = "boot"
        upgrade_core.write_state(st)
        return os.getpid()

    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        preflight_supervisor=lambda: True,
        fetch_release=lambda tag: _rel(rel_sha),
        spawn_worker=slow_spawn,
    )
    results: list[dict] = []

    def worker():
        results.append(upgrade_core.start_upgrade("0.3.0", install_dir=root))

    t1 = threading.Thread(target=worker)
    t1.start()
    assert first_spawned.wait(timeout=3), "first spawn did not start"
    # 第一请求已写 queued 并进入 spawn；第二请求必须被拒绝
    t2 = threading.Thread(target=worker)
    t2.start()
    t2.join(timeout=5)
    hold.set()
    t1.join(timeout=5)
    accepted = [r for r in results if r.get("accepted") is True]
    rejected = [r for r in results if r.get("accepted") is False]
    assert len(results) == 2, results
    assert len(accepted) == 1, results
    assert len(rejected) == 1, results


def test_run_job_refuses_without_lock(install_tree):
    """外部持锁时 run_job 必须 rc=99，且不得进入事务。"""
    root = install_tree["root"]
    entered = {"n": 0}
    real = upgrade_core._run_job_locked

    def wrap(job_id, root_path):
        entered["n"] += 1
        return real(job_id, root_path)

    upgrade_core._run_job_locked = wrap  # type: ignore
    try:
        lock = upgrade_core.UpgradeLock()
        assert lock.acquire(blocking=False)
        st = upgrade_core._default_state()
        st.update({"job_id": "job1", "state": "queued", "target_tag": "v0.3.0",
                   "target_sha": "a" * 40, "from_sha": "b" * 40})
        upgrade_core.write_state(st)
        rc = upgrade_core.run_job("job1", install_dir=root)
        assert rc == 99
        assert entered["n"] == 0
        lock.release()
    finally:
        upgrade_core._run_job_locked = real  # type: ignore


def test_stale_worker_reconcile_allows_retry(install_tree):
    st = upgrade_core._default_state()
    st.update({
        "job_id": "dead1",
        "state": "installing",
        "worker_pid": 999999,  # 不存在
        "worker_started_at": "1",
        "worker_start_boot_id": "x",
        "created_at": upgrade_core._utc_iso(),
    })
    upgrade_core.write_state(st)
    pub = upgrade_core.public_status()
    assert pub["state"] == "failed"
    assert pub["error_code"] == "stale_worker"
    assert pub["active"] is False
    # 可再次 start
    root = install_tree["root"]
    rel_sha = install_tree["rel_sha"]
    events = _hooks_success(root, rel_sha)

    def spawn(job_id, install_dir, log_path):
        upgrade_core.run_job(job_id, install_dir=install_dir)
        return os.getpid()

    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        preflight_supervisor=lambda: True,
        fetch_release=lambda tag: _rel(rel_sha),
        verify_tag_sha=lambda *a, **k: None,
        fetch_and_checkout=lambda d, t, s: _git(d, "checkout", "-f", s),
        install_deps_staging=lambda d: (d / ".venv.upgrade-staging").mkdir(exist_ok=True) or (d / ".venv.upgrade-staging"),
        atomic_switch_venv=lambda d: None,
        restart_cockpit=lambda: None,
        health_check=lambda: True,
        stop_cockpit=lambda: None,
        spawn_worker=spawn,
        proc_start_time=lambda pid: "1",
        boot_id=lambda: "boot",
        worker_alive=lambda *a: True,
    )
    # after stale, state failed — start should accept
    r = upgrade_core.start_upgrade("0.3.0", install_dir=root)
    assert r["accepted"] is True


def test_successful_upgrade_via_hooks(install_tree):
    root = install_tree["root"]
    rel_sha = install_tree["rel_sha"]
    events: list[str] = []
    _hooks_success(root, rel_sha, events)

    def spawn(job_id, install_dir, log_path):
        upgrade_core.run_job(job_id, install_dir=install_dir)
        return os.getpid()

    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        preflight_supervisor=lambda: True,
        fetch_release=lambda tag: _rel(rel_sha),
        verify_tag_sha=lambda *a, **k: None,
        fetch_and_checkout=lambda d, t, s: (events.append("checkout") or _git(d, "checkout", "-f", s)),
        install_deps_staging=lambda d: (
            events.append("staging")
            or (d / upgrade_core.VENV_STAGING).mkdir(exist_ok=True)
            or (d / upgrade_core.VENV_STAGING)
        ),
        atomic_switch_venv=lambda d: events.append("switch"),
        restart_cockpit=lambda: events.append("restart"),
        health_check=lambda: (events.append("health") or True),
        stop_cockpit=lambda: events.append("stop"),
        spawn_worker=spawn,
        proc_start_time=lambda pid: "tick1",
        boot_id=lambda: "boot1",
    )
    r = upgrade_core.start_upgrade("0.3.0", install_dir=root)
    assert r["accepted"] is True
    st = upgrade_core.read_state()
    assert st["state"] == "succeeded"
    assert st.get("error_code") is None
    assert "checkout" in events and "staging" in events and "health" in events


def test_health_failure_rolls_back_with_stop(install_tree):
    root = install_tree["root"]
    base_sha = install_tree["base_sha"]
    rel_sha = install_tree["rel_sha"]
    health_n = {"n": 0}
    stops = []

    def health():
        health_n["n"] += 1
        return health_n["n"] >= 2

    def spawn(job_id, install_dir, log_path):
        upgrade_core.run_job(job_id, install_dir=install_dir)
        return os.getpid()

    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        preflight_supervisor=lambda: True,
        fetch_release=lambda tag: _rel(rel_sha),
        verify_tag_sha=lambda *a, **k: None,
        fetch_and_checkout=lambda d, t, s: _git(d, "checkout", "-f", s),
        install_deps_staging=lambda d: (d / upgrade_core.VENV_STAGING).mkdir(exist_ok=True) or (d / upgrade_core.VENV_STAGING),
        atomic_switch_venv=lambda d: None,
        restart_cockpit=lambda: None,
        health_check=health,
        stop_cockpit=lambda: stops.append(1),
        spawn_worker=spawn,
        proc_start_time=lambda pid: "t",
        boot_id=lambda: "b",
    )
    r = upgrade_core.start_upgrade("v0.3.0", install_dir=root)
    assert r["accepted"] is True
    st = upgrade_core.read_state()
    assert st["state"] in ("rolled_back", "failed")
    assert stops, "rollback must stop cockpit before data restore"
    assert _git(root, "rev-parse", "HEAD") == base_sha


def test_sqlite_backup_fail_closed(install_tree, monkeypatch, tmp_path):
    root = install_tree["root"]
    data = install_tree["data"]
    db = data / "tasks.sqlite3"
    # create a real sqlite file
    import sqlite3
    con = sqlite3.connect(str(db))
    con.execute("create table t(x int)")
    con.commit()
    con.close()

    def boom_backup(src, dest):
        raise ValueError("backup_failed")

    monkeypatch.setattr(upgrade_core, "_sqlite_backup_strict", boom_backup)
    with pytest.raises(ValueError, match="backup_failed"):
        upgrade_core.create_backup(root, "j1")


def test_public_error_sanitized_no_paths(install_tree, monkeypatch):
    st = upgrade_core._default_state()
    st.update({
        "state": "failed",
        "error_code": "precheck_dirty",
        "job_id": "x",
    })
    # 旧字段 error 若存在也应被 write 丢掉
    st["error"] = "/home/fyc/secret/path PermissionError token=abc"
    upgrade_core.write_state(st)
    loaded = upgrade_core.read_state()
    assert "error" not in loaded or loaded.get("error") is None
    pub = upgrade_core.public_status(loaded)
    assert pub["error_code"] == "precheck_dirty"
    assert pub["error_message"]
    assert "/home/" not in str(pub)
    assert "token=" not in str(pub)
    assert "PermissionError" not in str(pub)


def test_api_maps_error_codes_without_secrets(install_tree, monkeypatch):
    import server
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret-token-xyz", raising=False)
    monkeypatch.setattr(upgrade_core, "INSTALL_DIR", install_tree["root"])
    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        preflight_supervisor=lambda: True,
        fetch_release=lambda tag: (_ for _ in ()).throw(ValueError("release_unavailable")),
    )
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret-token-xyz"}
    r = client.post("/api/upgrade", headers=headers, json={"target": "0.3.0"})
    assert r.status_code == 400
    assert "secret-token" not in r.text
    assert "/home/" not in r.text
    # 中文稳定文案
    assert "Release" in r.json()["detail"] or "官方" in r.json()["detail"]


def test_preflight_supervisor_no_bus(install_tree, monkeypatch):
    upgrade_core.clear_hooks()
    upgrade_core.configure_hooks(skip_venv_check=lambda: True)

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = "Failed to connect to bus: No such file or directory"
        return R()

    monkeypatch.setattr(upgrade_core.subprocess, "run", fake_run)
    monkeypatch.setattr(upgrade_core.sys, "platform", "linux")
    monkeypatch.setattr(upgrade_core.shutil, "which", lambda n: "/bin/systemctl" if n == "systemctl" else None)
    with pytest.raises(ValueError, match="precheck_supervisor"):
        upgrade_core.preflight_supervisor()


def test_sha_must_be_40_hex(install_tree):
    with pytest.raises(ValueError):
        upgrade_core._require_sha40("abc1234")
    assert len(upgrade_core._require_sha40("a" * 40)) == 40


def test_spawn_prefers_start_new_session(install_tree, monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["kwargs"] = kwargs
            seen["cmd"] = cmd
            self.pid = 111

    monkeypatch.setattr(upgrade_core.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(upgrade_core.sys, "platform", "linux")
    monkeypatch.setattr(upgrade_core.shutil, "which", lambda n: None)
    upgrade_core.clear_hooks()
    pid = upgrade_core.spawn_worker(
        "j", install_tree["root"], install_tree["data"] / "upgrade" / "logs" / "t.log",
    )
    assert pid == 111
    assert seen["kwargs"].get("start_new_session") is True
