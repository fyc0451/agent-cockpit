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


def test_health_payload_requires_five_strict_greens():
    """成功 health：status 严格 ok 且 db/herdr/hub/push 均为 bool True。"""
    green = {"status": "ok", "db": True, "herdr": True, "hub": True, "push": True}
    assert upgrade_core.health_payload_is_green(green) is True

    # 仅 status=ok
    assert upgrade_core.health_payload_is_green({"status": "ok"}) is False
    # 缺任一键
    for key in ("db", "herdr", "hub", "push"):
        body = dict(green)
        del body[key]
        assert upgrade_core.health_payload_is_green(body) is False, key
    # false
    for key in ("db", "herdr", "hub", "push"):
        body = dict(green)
        body[key] = False
        assert upgrade_core.health_payload_is_green(body) is False, key
    # 字符串 truthy 不算
    for key in ("db", "herdr", "hub", "push"):
        body = dict(green)
        body[key] = "true"
        assert upgrade_core.health_payload_is_green(body) is False, key
    # 数值 1 也不算
    for key in ("db", "herdr", "hub", "push"):
        body = dict(green)
        body[key] = 1
        assert upgrade_core.health_payload_is_green(body) is False, key
    body = dict(green)
    body["status"] = "OK"  # 大小写
    assert upgrade_core.health_payload_is_green(body) is False
    body = dict(green)
    body["status"] = True
    assert upgrade_core.health_payload_is_green(body) is False
    assert upgrade_core.health_payload_is_green(None) is False
    assert upgrade_core.health_payload_is_green([]) is False
    assert upgrade_core.health_payload_is_green("ok") is False


def test_health_check_uses_strict_payload(install_tree, monkeypatch):
    """health_check HTTP 路径：不全绿失败；仅 status ok 失败；全绿成功。"""
    upgrade_core.clear_hooks()
    monkeypatch.setattr(upgrade_core.time, "sleep", lambda s: None)
    seq: list = []

    class FakeResp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return FakeResp(seq.pop(0) if seq else {"status": "ok"})

    monkeypatch.setattr(upgrade_core.httpx, "Client", FakeClient)

    # 依赖红灯但仍 status=ok → 失败
    seq[:] = [
        {"status": "ok", "db": False, "herdr": True, "hub": True, "push": True},
    ]
    assert upgrade_core.health_check(timeout_s=0.05) is False

    # 缺 push → 失败
    seq[:] = [{"status": "ok", "db": True, "herdr": True, "hub": True}]
    assert upgrade_core.health_check(timeout_s=0.05) is False

    # 字符串 truthy → 失败
    seq[:] = [
        {
            "status": "ok",
            "db": True,
            "herdr": True,
            "hub": True,
            "push": "true",
        }
    ]
    assert upgrade_core.health_check(timeout_s=0.05) is False

    # 先红后绿 → 重试后成功
    seq[:] = [
        {"status": "ok", "db": False, "herdr": True, "hub": True, "push": True},
        {"status": "ok", "db": True, "herdr": True, "hub": True, "push": True},
    ]
    assert upgrade_core.health_check(timeout_s=3.0) is True


def test_api_upgrade_requires_cockpit_token_not_public(install_tree, monkeypatch):
    """POST /api/upgrade 非 PUBLIC：无认证与错误 token 均 401；正确 token 仍走契约。

    证明共享 COCKPIT_TOKEN 即本机管理员鉴权，升级路由受 protect_api 保护。
    """
    import server

    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret-token-xyz", raising=False)
    # 升级路由不得出现在 PUBLIC_PATHS
    assert "/api/upgrade" not in server.PUBLIC_PATHS
    assert "/api/upgrade/status" not in server.PUBLIC_PATHS

    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        preflight_supervisor=lambda: True,
        fetch_release=lambda tag: (_ for _ in ()).throw(ValueError("release_unavailable")),
    )
    client = TestClient(server.app)
    body = {"target": "0.3.0"}

    r_none = client.post("/api/upgrade", json=body)
    assert r_none.status_code == 401, r_none.text
    assert "未认证" in r_none.json().get("detail", "")

    r_bad = client.post(
        "/api/upgrade",
        headers={"authorization": "Bearer wrong-token"},
        json=body,
    )
    assert r_bad.status_code == 401, r_bad.text
    assert "未认证" in r_bad.json().get("detail", "")

    # 正确 token：仍走现有契约（此处 hook 抛 release_unavailable → 400）
    r_ok = client.post(
        "/api/upgrade",
        headers={"authorization": "Bearer secret-token-xyz"},
        json=body,
    )
    assert r_ok.status_code == 400
    detail = r_ok.json().get("detail", "")
    assert "Release" in detail or "官方" in detail


def test_preflight_supervisor_no_bus(install_tree, monkeypatch):
    """bus 不可达 → fail closed（不得因 KillMode/文件存在而 PASS）。"""
    upgrade_core.clear_hooks()
    upgrade_core.configure_hooks(skip_venv_check=lambda: True)
    monkeypatch.setattr(upgrade_core.sys, "platform", "linux")
    monkeypatch.setattr(upgrade_core, "_user_systemd_bus_ok", lambda: False)
    monkeypatch.setattr(upgrade_core, "_unit_file_killmode_process", lambda: True)
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
    monkeypatch.setattr(upgrade_core, "_linux_unit_killmode_process_live", lambda: True)
    upgrade_core.clear_hooks()
    pid = upgrade_core.spawn_worker(
        "j", install_tree["root"], install_tree["data"] / "upgrade" / "logs" / "t.log",
    )
    assert pid == 111
    assert seen["kwargs"].get("start_new_session") is True


# ── R1–R4 二轮阻断回归 ──────────────────────────────────────────


def test_r1_merge_spawn_identity_never_clobber_with_zero(install_tree):
    """systemd-run 返回 pid<=0 时不得覆盖 worker 已写身份/阶段。"""
    root = install_tree["root"]
    job_id = "job-r1"
    # 模拟 worker 已先写 identity + 进入 prechecking
    st = upgrade_core._default_state()
    st.update({
        "job_id": job_id,
        "state": "prechecking",
        "phase": "precheck",
        "worker_pid": 4242,
        "worker_started_at": "99",
        "worker_start_boot_id": "boot-x",
        "created_at": upgrade_core._utc_iso(),
        "install_dir": str(root),
    })
    upgrade_core.write_state(st)

    # API 侧 merge：spawn 返回 0 / -1 都不得回退
    for bad in (0, -1, None):
        out = upgrade_core.merge_spawn_identity(job_id, bad)
        assert out["worker_pid"] == 4242
        assert out["state"] == "prechecking"
        assert out["phase"] == "precheck"

    loaded = upgrade_core.read_state()
    assert loaded["worker_pid"] == 4242
    assert loaded["state"] == "prechecking"
    # 非空身份存在时 public_status 不得因 pid=0 历史判死
    upgrade_core.configure_hooks(worker_alive=lambda *a: True)
    pub = upgrade_core.public_status()
    assert pub["state"] == "prechecking"
    assert pub["active"] is True


def test_r1_spawn_zero_then_worker_identity_wins(install_tree):
    """spawn 返回 0 后 worker 回写真实 pid；merge 不得写 0。"""
    job_id = "job-r1b"
    st = upgrade_core._default_state()
    st.update({
        "job_id": job_id,
        "state": "queued",
        "phase": "spawn_worker",
        "worker_pid": None,
        "created_at": upgrade_core._utc_iso(),
    })
    upgrade_core.write_state(st)
    # API 先 merge 0：不得写入 0
    out = upgrade_core.merge_spawn_identity(job_id, 0)
    assert out.get("worker_pid") in (None, 0) or out.get("worker_pid") is None
    loaded = upgrade_core.read_state()
    assert loaded.get("worker_pid") in (None,)  # 未写入 0
    # worker 写真实身份
    loaded["worker_pid"] = 7777
    loaded["state"] = "prechecking"
    loaded["phase"] = "precheck"
    upgrade_core.write_state(loaded)
    out2 = upgrade_core.merge_spawn_identity(job_id, 0)
    assert out2["worker_pid"] == 7777
    assert out2["state"] == "prechecking"


def test_r2_install_fail_after_checkout_rolls_back(install_tree):
    """checkout 成功后 install 失败必须回滚 HEAD=from_sha。"""
    root = install_tree["root"]
    base_sha = install_tree["base_sha"]
    rel_sha = install_tree["rel_sha"]
    stops: list[int] = []

    def spawn(job_id, install_dir, log_path):
        upgrade_core.run_job(job_id, install_dir=install_dir)
        return os.getpid()

    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        preflight_supervisor=lambda: True,
        fetch_release=lambda tag: _rel(rel_sha),
        verify_tag_sha=lambda *a, **k: None,
        fetch_and_checkout=lambda d, t, s: _git(d, "checkout", "-f", s),
        install_deps_staging=lambda d: (_ for _ in ()).throw(ValueError("install_failed")),
        atomic_switch_venv=lambda d: None,
        restart_cockpit=lambda: None,
        health_check=lambda: True,
        stop_cockpit=lambda: stops.append(1),
        spawn_worker=spawn,
        proc_start_time=lambda pid: "t",
        boot_id=lambda: "b",
    )
    r = upgrade_core.start_upgrade("0.3.0", install_dir=root)
    assert r["accepted"] is True
    st = upgrade_core.read_state()
    assert st["state"] == "rolled_back", st
    assert _git(root, "rev-parse", "HEAD") == base_sha
    assert stops, "rollback must stop cockpit"


def test_r2_switch_second_rename_fail_restores_live(install_tree, monkeypatch):
    """atomic_switch 第二步 rename 失败时尽量把 prev 迁回 live，并走统一 rollback。"""
    root = install_tree["root"]
    base_sha = install_tree["base_sha"]
    rel_sha = install_tree["rel_sha"]
    live = root / upgrade_core.VENV_LIVE
    staging = root / upgrade_core.VENV_STAGING
    prev = root / upgrade_core.VENV_PREV
    # 真实目录布局
    (live / "bin").mkdir(parents=True, exist_ok=True)
    (live / "marker").write_text("live-orig", encoding="utf-8")

    rename_calls: list[tuple] = []
    real_rename = os.rename

    def flaky_rename(src, dst):
        rename_calls.append((str(src), str(dst)))
        # 允许 live→prev；staging→live 失败
        if Path(src) == staging and Path(dst) == live:
            raise OSError("simulated rename fail")
        return real_rename(src, dst)

    def spawn(job_id, install_dir, log_path):
        upgrade_core.run_job(job_id, install_dir=install_dir)
        return os.getpid()

    def do_install(d):
        staging.mkdir(exist_ok=True)
        (staging / "ok").write_text("1", encoding="utf-8")
        return staging

    upgrade_core.clear_hooks()
    # 不 hook atomic_switch，走真实实现 + flaky rename
    monkeypatch.setattr(os, "rename", flaky_rename)
    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        preflight_supervisor=lambda: True,
        fetch_release=lambda tag: _rel(rel_sha),
        verify_tag_sha=lambda *a, **k: None,
        fetch_and_checkout=lambda d, t, s: _git(d, "checkout", "-f", s),
        install_deps_staging=do_install,
        restart_cockpit=lambda: None,
        health_check=lambda: True,
        stop_cockpit=lambda: None,
        spawn_worker=spawn,
        proc_start_time=lambda pid: "t",
        boot_id=lambda: "b",
    )
    r = upgrade_core.start_upgrade("0.3.0", install_dir=root)
    assert r["accepted"] is True
    st = upgrade_core.read_state()
    assert st["state"] in ("rolled_back", "failed"), st
    assert _git(root, "rev-parse", "HEAD") == base_sha
    # live 应被恢复（prev 迁回或原本恢复）
    assert live.exists(), "live venv must be restored after switch fail"


def test_r3_stop_fail_closed_not_rolled_back(install_tree):
    """stop 失败 fail-closed：不得标 rolled_back。"""
    root = install_tree["root"]
    rel_sha = install_tree["rel_sha"]

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
        health_check=lambda: False,  # 触发 rollback
        stop_cockpit=lambda: False,  # stop 明确失败
        spawn_worker=spawn,
        proc_start_time=lambda pid: "t",
        boot_id=lambda: "b",
    )
    r = upgrade_core.start_upgrade("0.3.0", install_dir=root)
    assert r["accepted"] is True
    st = upgrade_core.read_state()
    assert st["state"] == "failed"
    assert st["error_code"] in ("stop_failed", "rollback_failed")
    assert st["state"] != "rolled_back"


def test_r3_restore_code_fail_not_rolled_back(install_tree):
    """restore_code 失败不得误报 rolled_back。"""
    root = install_tree["root"]
    rel_sha = install_tree["rel_sha"]

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
        health_check=lambda: False,
        stop_cockpit=lambda: None,
        git_checkout=lambda d, s: (_ for _ in ()).throw(RuntimeError("checkout boom")),
        spawn_worker=spawn,
        proc_start_time=lambda pid: "t",
        boot_id=lambda: "b",
    )
    r = upgrade_core.start_upgrade("0.3.0", install_dir=root)
    assert r["accepted"] is True
    st = upgrade_core.read_state()
    assert st["state"] == "failed"
    assert st["error_code"] == "rollback_failed"
    assert st["phase"] == "restore_code_failed"


def test_r3_verify_head_mismatch_not_rolled_back(install_tree):
    """回滚后 HEAD 校验失败不得 rolled_back。"""
    root = install_tree["root"]
    rel_sha = install_tree["rel_sha"]

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
        health_check=lambda: False,
        stop_cockpit=lambda: None,
        verify_rolled_back=lambda root, sha: False,
        spawn_worker=spawn,
        proc_start_time=lambda pid: "t",
        boot_id=lambda: "b",
    )
    r = upgrade_core.start_upgrade("0.3.0", install_dir=root)
    assert r["accepted"] is True
    st = upgrade_core.read_state()
    assert st["state"] == "failed"
    assert st["error_code"] == "rollback_failed"
    assert st["phase"] == "verify_head_failed"


def test_r4_dead_worker_after_checkout_spawns_rollback(install_tree):
    """dead worker + code_mutated 必须触发 rollback-only 恢复，而非仅 failed。"""
    root = install_tree["root"]
    base_sha = install_tree["base_sha"]
    rel_sha = install_tree["rel_sha"]
    # 模拟半成品：HEAD 已在 release，worker 已死
    _git(root, "checkout", "-f", rel_sha)
    st = upgrade_core._default_state()
    st.update({
        "job_id": "dead-half",
        "state": "installing",
        "phase": "venv_staging",
        "worker_pid": 999999,
        "worker_started_at": "1",
        "worker_start_boot_id": "x",
        "created_at": upgrade_core._utc_iso(),
        "from_sha": base_sha,
        "target_sha": rel_sha,
        "target_tag": "v0.3.0",
        "code_mutated": True,
        "backup_id": None,
        "install_dir": str(root),
        "log_path": str(install_tree["data"] / "upgrade" / "logs" / "dead-half.log"),
    })
    upgrade_core.write_state(st)

    spawned: list[str] = []

    def rb_spawn(job_id, install_dir, log_path):
        spawned.append(job_id)
        # 同步执行 rollback-only
        st2 = upgrade_core.read_state()
        st2["rollback_only"] = True
        st2["rollback_requested"] = True
        upgrade_core.write_state(st2)
        upgrade_core.run_job(job_id, install_dir=install_dir)
        return os.getpid()

    upgrade_core.configure_hooks(
        spawn_rollback_worker=rb_spawn,
        stop_cockpit=lambda: None,
        restart_cockpit=lambda: None,
        health_check=lambda: True,
        worker_alive=lambda *a: False,
        proc_start_time=lambda pid: "1",
        boot_id=lambda: "x",
    )
    pub = upgrade_core.public_status()
    assert spawned, "must auto-spawn rollback-only worker"
    st_final = upgrade_core.read_state()
    assert st_final["state"] == "rolled_back", st_final
    assert _git(root, "rev-parse", "HEAD") == base_sha
    assert pub["state"] == "rolled_back"
    assert int(st_final.get("rollback_spawn_count") or 0) >= 1


def test_r4_spawn_passes_rollback_only_flag(install_tree, monkeypatch):
    """spawn_worker(rollback_only=True) 必须带 --rollback-only。"""
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["cmd"] = cmd
            seen["env"] = kwargs.get("env") or {}
            self.pid = 222

    monkeypatch.setattr(upgrade_core.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(upgrade_core.sys, "platform", "linux")
    monkeypatch.setattr(upgrade_core.shutil, "which", lambda n: None)
    monkeypatch.setattr(upgrade_core, "_linux_unit_killmode_process_live", lambda: True)
    monkeypatch.setenv("COCKPIT_PORT", "9876")
    monkeypatch.setenv("COCKPIT_HOST", "127.0.0.1")
    upgrade_core.clear_hooks()
    pid = upgrade_core.spawn_worker(
        "j", install_tree["root"], install_tree["data"] / "upgrade" / "logs" / "t.log",
        rollback_only=True,
    )
    assert pid == 222
    assert "--rollback-only" in seen["cmd"]
    assert seen["env"].get("COCKPIT_PORT") == "9876"


# ── R5–R8 第三轮 / 第四轮发布阻断回归 ──────────────────────────


def test_r5_bus_down_fail_closed_no_direct_kill(install_tree, monkeypatch):
    """R5：bus 不可达时 preflight/stop/restart 均 fail-closed，禁止 direct kill。"""
    upgrade_core.clear_hooks()
    monkeypatch.setattr(upgrade_core.sys, "platform", "linux")
    monkeypatch.setattr(upgrade_core, "_user_systemd_bus_ok", lambda: False)
    kills: list[int] = []

    def boom_kill(pid, sig=None):
        kills.append(pid)
        raise AssertionError("must not kill under bus-down")

    monkeypatch.setattr(os, "kill", boom_kill)
    monkeypatch.setattr(upgrade_core, "_find_cockpit_pids", lambda: [2454415])
    with pytest.raises(ValueError, match="precheck_supervisor"):
        upgrade_core.preflight_supervisor()
    with pytest.raises(ValueError, match="stop_failed"):
        upgrade_core.stop_cockpit_for_restore()
    with pytest.raises(ValueError, match="restart_failed"):
        upgrade_core.restart_cockpit_only()
    assert kills == [], kills


def test_r5_preflight_requires_active_our_mainpid_and_killmode(install_tree, monkeypatch):
    """R5：bus 在但 service 未 active / MainPID 非本安装 / KillMode 非 process → 拒绝。"""
    upgrade_core.clear_hooks()
    monkeypatch.setattr(upgrade_core.sys, "platform", "linux")
    monkeypatch.setattr(upgrade_core, "_user_systemd_bus_ok", lambda: True)
    monkeypatch.setattr(upgrade_core, "INSTALL_DIR", install_tree["root"])

    # inactive
    monkeypatch.setattr(
        upgrade_core,
        "_systemctl_user_show",
        lambda props, unit="agent-cockpit.service": {
            "ActiveState": "inactive", "MainPID": "0", "KillMode": "process",
        },
    )
    with pytest.raises(ValueError, match="precheck_supervisor"):
        upgrade_core.preflight_supervisor()

    # active but wrong MainPID (not our server)
    monkeypatch.setattr(
        upgrade_core,
        "_systemctl_user_show",
        lambda props, unit="agent-cockpit.service": {
            "ActiveState": "active", "MainPID": "999", "KillMode": "process",
        },
    )
    monkeypatch.setattr(upgrade_core, "_pid_is_our_cockpit", lambda pid: False)
    with pytest.raises(ValueError, match="precheck_supervisor"):
        upgrade_core.preflight_supervisor()

    # our MainPID but KillMode=control-group (旧 unit)
    monkeypatch.setattr(
        upgrade_core,
        "_systemctl_user_show",
        lambda props, unit="agent-cockpit.service": {
            "ActiveState": "active", "MainPID": "100", "KillMode": "control-group",
        },
    )
    monkeypatch.setattr(upgrade_core, "_pid_is_our_cockpit", lambda pid: pid == 100)
    monkeypatch.setattr(upgrade_core, "_unit_file_killmode_process", lambda: False)
    with pytest.raises(ValueError, match="precheck_supervisor"):
        upgrade_core.preflight_supervisor()

    # happy path
    monkeypatch.setattr(
        upgrade_core,
        "_systemctl_user_show",
        lambda props, unit="agent-cockpit.service": {
            "ActiveState": "active", "MainPID": "100", "KillMode": "process",
        },
    )
    upgrade_core.preflight_supervisor()


def test_r5_stop_restart_postconditions(install_tree, monkeypatch):
    """R5：stop 后必须 inactive；restart 后必须 active+our MainPID。"""
    upgrade_core.clear_hooks()
    monkeypatch.setattr(upgrade_core.sys, "platform", "linux")
    monkeypatch.setattr(upgrade_core, "_user_systemd_bus_ok", lambda: True)
    monkeypatch.setattr(upgrade_core.shutil, "which", lambda n: "/bin/systemctl")

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(upgrade_core.subprocess, "run", fake_run)
    # stop postcondition fails if still active our pid
    monkeypatch.setattr(
        upgrade_core,
        "_systemctl_user_show",
        lambda props, unit="agent-cockpit.service": {
            "ActiveState": "active", "MainPID": "50", "KillMode": "process",
        },
    )
    monkeypatch.setattr(upgrade_core, "_pid_is_our_cockpit", lambda pid: True)
    monkeypatch.setattr(upgrade_core, "_find_cockpit_pids", lambda: [50])
    monkeypatch.setattr(upgrade_core.time, "sleep", lambda s: None)
    t = {"n": 0.0}
    monkeypatch.setattr(upgrade_core.time, "time", lambda: t.__setitem__("n", t["n"] + 5) or t["n"])
    with pytest.raises(ValueError, match="stop_failed"):
        upgrade_core.stop_cockpit_for_restore()

    # stop success
    monkeypatch.setattr(
        upgrade_core,
        "_systemctl_user_show",
        lambda props, unit="agent-cockpit.service": {
            "ActiveState": "inactive", "MainPID": "0", "KillMode": "process",
        },
    )
    monkeypatch.setattr(upgrade_core, "_find_cockpit_pids", lambda: [])
    t["n"] = 0.0
    upgrade_core.stop_cockpit_for_restore()

    # restart requires identity ok
    monkeypatch.setattr(upgrade_core, "_linux_supervisor_identity_ok", lambda: False)
    t["n"] = 0.0
    with pytest.raises(ValueError, match="restart_failed"):
        upgrade_core.restart_cockpit_only()
    monkeypatch.setattr(upgrade_core, "_linux_supervisor_identity_ok", lambda: True)
    t["n"] = 0.0
    upgrade_core.restart_cockpit_only()


def test_r5_systemd_run_fail_without_killmode_spawn_failed(install_tree, monkeypatch):
    """R5：systemd-run 失败且 unit KillMode 非 process → spawn_failed，禁止 setsid。"""
    upgrade_core.clear_hooks()
    monkeypatch.setattr(upgrade_core.sys, "platform", "linux")
    monkeypatch.setattr(upgrade_core, "_user_systemd_bus_ok", lambda: True)
    monkeypatch.setattr(
        upgrade_core.shutil, "which",
        lambda n: "/bin/systemd-run" if n == "systemd-run" else "/bin/systemctl",
    )

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = "fail"
        return R()

    monkeypatch.setattr(upgrade_core.subprocess, "run", fake_run)
    monkeypatch.setattr(upgrade_core, "_linux_unit_killmode_process_live", lambda: False)
    popen = []

    class FakePopen:
        def __init__(self, *a, **k):
            popen.append(1)
            self.pid = 1

    monkeypatch.setattr(upgrade_core.subprocess, "Popen", FakePopen)
    with pytest.raises(RuntimeError, match="spawn_failed"):
        upgrade_core.spawn_worker(
            "j", install_tree["root"], install_tree["data"] / "upgrade" / "logs" / "z.log",
        )
    assert popen == []


def test_r5_supervisor_respawn_scenario_documented(install_tree, monkeypatch):
    """R5：PID 被 supervisor 自动重生时，orphan kill 无法形成稳定窗口 → stop_failed。"""
    upgrade_core.clear_hooks()
    live = {"pid": 100}

    def find():
        return [live["pid"]] if live["pid"] else []

    def fake_kill(pid, sig=None):
        live["pid"] = pid + 1

    monkeypatch.setattr(upgrade_core, "_find_cockpit_pids", find)
    monkeypatch.setattr(upgrade_core, "_foreign_listener_on_port", lambda: False)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(upgrade_core.time, "sleep", lambda s: None)
    t0 = {"n": 0.0}

    def fake_time():
        t0["n"] += 5.0
        return t0["n"]

    monkeypatch.setattr(upgrade_core.time, "time", fake_time)
    with pytest.raises(ValueError, match="stop_failed"):
        upgrade_core._stop_orphaned_cockpit_pids()


def test_r6_concurrent_reconcile_single_rollback_spawn(install_tree):
    """R6：双线程 public_status/reconcile 只补投 1 次 rollback worker。"""
    root = install_tree["root"]
    st = upgrade_core._default_state()
    st.update({
        "job_id": "race1",
        "state": "installing",
        "phase": "venv_staging",
        "worker_pid": 999999,
        "worker_started_at": "1",
        "worker_start_boot_id": "x",
        "created_at": upgrade_core._utc_iso(),
        "from_sha": install_tree["base_sha"],
        "code_mutated": True,
        "install_dir": str(root),
        "log_path": str(install_tree["data"] / "upgrade" / "logs" / "race1.log"),
    })
    upgrade_core.write_state(st)
    barrier = threading.Barrier(2)
    spawned: list[int] = []
    lock = threading.Lock()

    def rb_spawn(job_id, install_dir, log_path):
        with lock:
            spawned.append(1)
        # 模拟慢 spawn，放大竞态窗
        time.sleep(0.05)
        return 1

    upgrade_core.configure_hooks(
        spawn_rollback_worker=rb_spawn,
        worker_alive=lambda *a: False,
    )

    def worker():
        barrier.wait(timeout=3)
        upgrade_core.reconcile_stale_state()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert len(spawned) == 1, f"concurrent_reconcile_spawns={len(spawned)}"
    st2 = upgrade_core.read_state()
    assert int(st2.get("rollback_spawn_count") or 0) == 1
    assert st2["state"] == "rolling_back"
    assert st2.get("rollback_requested") is True


def test_r7_rollback_worker_death_retries_then_exhausted(install_tree):
    """R7：rollback worker 再死可再补投；耗尽后明确 rollback_exhausted。"""
    root = install_tree["root"]
    st = upgrade_core._default_state()
    st.update({
        "job_id": "rb-dead",
        "state": "rolling_back",
        "phase": "rollback",
        "worker_pid": 888888,
        "worker_started_at": "1",
        "worker_start_boot_id": "x",
        "created_at": upgrade_core._utc_iso(),
        "from_sha": install_tree["base_sha"],
        "code_mutated": True,
        "rollback_requested": True,
        "rollback_only": True,
        "rollback_spawn_count": 1,
        "install_dir": str(root),
        "log_path": str(install_tree["data"] / "upgrade" / "logs" / "rb-dead.log"),
    })
    upgrade_core.write_state(st)
    spawned: list[int] = []

    def rb_spawn(job_id, install_dir, log_path):
        spawned.append(1)
        return 1

    upgrade_core.configure_hooks(
        spawn_rollback_worker=rb_spawn,
        worker_alive=lambda *a: False,
    )
    # 第 2 次补投
    out = upgrade_core.reconcile_stale_state()
    assert len(spawned) == 1
    assert out["state"] == "rolling_back"
    assert int(out["rollback_spawn_count"]) == 2
    # 再死
    st = upgrade_core.read_state()
    st["worker_pid"] = 777777
    upgrade_core.write_state(st)
    out2 = upgrade_core.reconcile_stale_state()
    assert len(spawned) == 2
    assert int(out2["rollback_spawn_count"]) == 3
    # 第 4 次应耗尽（MAX=3）
    st = upgrade_core.read_state()
    st["worker_pid"] = 666666
    upgrade_core.write_state(st)
    out3 = upgrade_core.reconcile_stale_state()
    assert len(spawned) == 2  # 不再 spawn
    assert out3["state"] == "failed"
    assert out3["error_code"] == "rollback_failed"
    assert out3["phase"] == "rollback_exhausted"


def test_r8_darwin_preflight_requires_main_label(install_tree, monkeypatch):
    """R8：macOS preflight 必须 print 主 service label，仅 domain 不够。"""
    upgrade_core.clear_hooks()
    monkeypatch.setattr(upgrade_core.sys, "platform", "darwin")
    monkeypatch.setattr(upgrade_core.shutil, "which", lambda n: "/bin/launchctl" if n == "launchctl" else None)
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        class R:
            returncode = 1 if "print" in cmd and upgrade_core.DARWIN_MAIN_LABEL in " ".join(cmd) else 0
            stdout = ""
            stderr = "Could not find service"
        # 主 label print 失败
        if len(cmd) >= 3 and cmd[1] == "print" and upgrade_core.DARWIN_MAIN_LABEL in cmd[2]:
            R.returncode = 1
        else:
            R.returncode = 0
        return R()

    monkeypatch.setattr(upgrade_core.subprocess, "run", fake_run)
    # launchd.sh / plist 在真实 INSTALL 可能存在；monkeypatch INSTALL 到 fixture
    monkeypatch.setattr(upgrade_core, "INSTALL_DIR", install_tree["root"])
    (install_tree["root"] / "launchd.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (install_tree["root"] / "agent-cockpit.plist").write_text("<plist/>\n", encoding="utf-8")
    with pytest.raises(ValueError, match="precheck_supervisor"):
        upgrade_core.preflight_supervisor()
    assert any(upgrade_core.DARWIN_MAIN_LABEL in " ".join(c) for c in seen)


def test_r8_darwin_stop_not_loaded_but_pid_alive_fails(install_tree, monkeypatch):
    """R8：bootout 报 not loaded 但本安装进程仍活且杀不掉 → stop_failed。"""
    upgrade_core.clear_hooks()
    monkeypatch.setattr(upgrade_core.sys, "platform", "darwin")

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = "Could not find service"
        return R()

    monkeypatch.setattr(upgrade_core.subprocess, "run", fake_run)
    monkeypatch.setattr(upgrade_core, "_find_cockpit_pids", lambda: [4242])
    monkeypatch.setattr(upgrade_core, "_foreign_listener_on_port", lambda: False)
    monkeypatch.setattr(os, "kill", lambda *a, **k: None)  # kill 无效，PID 仍在
    with pytest.raises(ValueError, match="stop_failed"):
        upgrade_core.stop_cockpit_for_restore()


def test_r8_find_pids_requires_cwd_and_server_py(install_tree, monkeypatch):
    """R8：同端口异进程不得进入 kill 列表；必须 cwd+command 核验。"""
    upgrade_core.clear_hooks()
    monkeypatch.setattr(upgrade_core, "INSTALL_DIR", install_tree["root"])
    monkeypatch.setattr(upgrade_core, "_listen_pids_on_port", lambda port: [111, 222])
    monkeypatch.setattr(
        upgrade_core,
        "_pid_command",
        lambda pid: "python server.py" if pid == 111 else "nginx: master",
    )
    monkeypatch.setattr(
        upgrade_core,
        "_pid_cwd",
        lambda pid: str(install_tree["root"]) if pid == 111 else "/other/app",
    )
    # force non-/proc path
    monkeypatch.setattr(upgrade_core.sys, "platform", "darwin")
    ours = upgrade_core._find_cockpit_pids()
    assert ours == [111]
    assert upgrade_core._foreign_listener_on_port() is True
    with pytest.raises(ValueError, match="stop_failed"):
        upgrade_core._stop_orphaned_cockpit_pids()


def test_r8_darwin_oneshot_env_and_cleanup_on_fail(install_tree, monkeypatch, tmp_path):
    """R8：oneshot 传 COCKPIT_*；bootstrap 失败则清理 plist。"""
    upgrade_core.clear_hooks()
    monkeypatch.setattr(upgrade_core.sys, "platform", "darwin")
    monkeypatch.setattr(upgrade_core.shutil, "which", lambda n: "/bin/launchctl")
    monkeypatch.setenv("COCKPIT_PORT", "9123")
    monkeypatch.setenv("COCKPIT_HOST", "127.0.0.1")
    home = tmp_path / "home"
    la = home / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    monkeypatch.setattr(upgrade_core.Path, "home", lambda: home)

    bootstraps: list = []
    cleanups: list = []

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        c = list(cmd)
        if len(c) >= 2 and c[0] == "launchctl" and c[1] == "bootstrap":
            bootstraps.append(c)
            R.returncode = 1
            R.stderr = "bootstrap failed"
        if len(c) >= 2 and c[0] == "launchctl" and c[1] == "bootout":
            cleanups.append(c)
        return R()

    monkeypatch.setattr(upgrade_core.subprocess, "run", fake_run)
    log = install_tree["data"] / "upgrade" / "logs" / "os.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    pid = upgrade_core._spawn_darwin_launchd_oneshot(
        "jobx", install_tree["root"], log, ["--rollback-only"],
    )
    assert pid is None
    left = list(la.glob("*.plist"))
    assert left == [], left
    assert cleanups, "must bootout on failure"

    def fake_run_ok(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        c = list(cmd)
        if len(c) >= 2 and c[0] == "launchctl" and c[1] == "bootstrap":
            plist = Path(c[3])
            assert plist.is_file()
            text = plist.read_text(encoding="utf-8")
            assert "COCKPIT_PORT" in text and "9123" in text
            assert "COCKPIT_HOST" in text
            bootstraps.append("ok")
        return R()

    monkeypatch.setattr(upgrade_core.subprocess, "run", fake_run_ok)
    pid2 = upgrade_core._spawn_darwin_launchd_oneshot(
        "joby", install_tree["root"], log, [],
    )
    assert pid2 == -1
    assert "ok" in bootstraps
    # 成功路径保留 plist 供 worker 结束清理
    left_ok = list(la.glob("*.plist"))
    assert len(left_ok) == 1
    text = left_ok[0].read_text(encoding="utf-8")
    assert "COCKPIT_UPGRADE_LAUNCHD_LABEL" in text
    assert "COCKPIT_UPGRADE_LAUNCHD_PLIST" in text


def test_r8_darwin_oneshot_fail_no_setsid_fallback(install_tree, monkeypatch):
    """R8：Darwin oneshot 失败必须 spawn_failed，禁止 start_new_session 回退。"""
    upgrade_core.clear_hooks()
    monkeypatch.setattr(upgrade_core.sys, "platform", "darwin")
    monkeypatch.setattr(
        upgrade_core, "_spawn_darwin_launchd_oneshot", lambda *a, **k: None
    )
    popen_calls: list = []

    class FakePopen:
        def __init__(self, *a, **k):
            popen_calls.append(1)
            self.pid = 1

    monkeypatch.setattr(upgrade_core.subprocess, "Popen", FakePopen)
    with pytest.raises(RuntimeError, match="spawn_failed"):
        upgrade_core.spawn_worker(
            "j", install_tree["root"], install_tree["data"] / "upgrade" / "logs" / "x.log",
        )
    assert popen_calls == []


def test_r8_cleanup_darwin_upgrade_job(install_tree, monkeypatch, tmp_path):
    """R8：cleanup 先删 plist，再 detached bootout。"""
    upgrade_core.clear_hooks()
    la = tmp_path / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    plist = la / "io.github.fyc0451.agent-cockpit.upgrade.abc.plist"
    plist.write_text("<plist/>\n", encoding="utf-8")
    monkeypatch.setenv(
        "COCKPIT_UPGRADE_LAUNCHD_LABEL", "io.github.fyc0451.agent-cockpit.upgrade.abc"
    )
    monkeypatch.setenv("COCKPIT_UPGRADE_LAUNCHD_PLIST", str(plist))
    detached: list = []

    def fake_detached(service, pl):
        # 此时 plist 应已删除
        assert not Path(pl).exists()
        detached.append(service)

    upgrade_core._hooks["darwin_cleanup_detached"] = fake_detached
    assert plist.exists()
    upgrade_core.cleanup_darwin_upgrade_job()
    assert not plist.exists()
    assert len(detached) == 1
    assert "upgrade.abc" in detached[0]


def test_r8_cleanup_plist_deleted_even_if_detached_popen_fails(
    install_tree, monkeypatch, tmp_path,
):
    """R8：detached Popen 失败时 plist 仍已先删（不依赖 cleaner 成功）。"""
    upgrade_core.clear_hooks()
    la = tmp_path / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    plist = la / "io.github.fyc0451.agent-cockpit.upgrade.fail.plist"
    plist.write_text("<plist/>\n", encoding="utf-8")
    monkeypatch.setenv(
        "COCKPIT_UPGRADE_LAUNCHD_LABEL", "io.github.fyc0451.agent-cockpit.upgrade.fail"
    )
    monkeypatch.setenv("COCKPIT_UPGRADE_LAUNCHD_PLIST", str(plist))

    bootouts: list[list[str]] = []

    def boom_popen(*a, **k):
        # 证明：调用 Popen 前 plist 必须已消失
        assert not plist.exists(), "plist must be unlinked before detached Popen"
        raise OSError("popen denied")

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        if list(cmd)[:2] == ["launchctl", "bootout"]:
            bootouts.append(list(cmd))
            # fallback _cleanup_darwin_oneshot 路径
            assert not plist.exists()
        return R()

    monkeypatch.setattr(upgrade_core.subprocess, "Popen", boom_popen)
    monkeypatch.setattr(upgrade_core.subprocess, "run", fake_run)
    assert plist.exists()
    upgrade_core.cleanup_darwin_upgrade_job()
    assert not plist.exists()
    assert bootouts, "fallback bootout after Popen failure"
    assert "upgrade.fail" in bootouts[0][-1]
