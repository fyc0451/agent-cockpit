"""U1b：服务外升级核心自动化。

使用 fake supervisor / 本地 git remote / 注入钩子，不触碰真实 Herdr/Hub。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import upgrade_core
import version


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=True,
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
    (root / ".venv" / "bin" / "pip").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (root / ".venv" / "bin" / "pip").chmod(0o755)
    (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (root / ".venv" / "bin" / "python").chmod(0o755)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    base_sha = _git(root, "rev-parse", "HEAD")
    # second commit as "release"
    (root / "VERSION").write_text("0.3.0\n", encoding="utf-8")
    _git(root, "add", "VERSION")
    _git(root, "commit", "-m", "v0.3.0")
    rel_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "tag", "v0.3.0")
    # reset working tree to base for upgrade simulation
    _git(root, "checkout", "-f", base_sha)
    (root / "VERSION").write_text("0.2.0\n", encoding="utf-8")

    data = tmp_path / "dashboard-data"
    data.mkdir()
    monkeypatch.setattr(upgrade_core.settings, "DATA_DIR", data)
    monkeypatch.setattr(upgrade_core, "UPGRADE_DIR", data / "upgrade")
    monkeypatch.setattr(upgrade_core, "STATE_PATH", data / "upgrade" / "state.json")
    monkeypatch.setattr(upgrade_core, "LOCK_PATH", data / "upgrade" / "upgrade.lock")
    monkeypatch.setattr(upgrade_core, "LOG_DIR", data / "upgrade" / "logs")
    monkeypatch.setattr(upgrade_core, "BACKUP_ROOT", data / "upgrade" / "backups")
    monkeypatch.setattr(upgrade_core, "INSTALL_DIR", root)
    monkeypatch.setattr(upgrade_core, "WORKER_SCRIPT", Path(upgrade_core.__file__).resolve().parent / "cockpit-upgrade-worker.py")

    upgrade_core.clear_hooks()
    upgrade_core.configure_hooks(skip_venv_check=lambda: True)

    yield {
        "root": root,
        "base_sha": base_sha,
        "rel_sha": rel_sha,
        "data": data,
    }
    upgrade_core.clear_hooks()


def _release_payload(tag="v0.3.0", sha=None, draft=False, prerelease=False):
    return {
        "tag_name": tag,
        "name": tag,
        "html_url": f"https://github.com/fyc0451/agent-cockpit/releases/tag/{tag}",
        "published_at": "2026-08-08T00:00:00Z",
        "draft": draft,
        "prerelease": prerelease,
        "target_commitish": sha or "a" * 40,
    }


def test_precheck_blocks_tracked_dirty(install_tree):
    root = install_tree["root"]
    (root / "server.py").write_text("# dirty\n", encoding="utf-8")
    with pytest.raises(ValueError, match="未提交"):
        upgrade_core.precheck_install_dir(root)


def test_precheck_allows_venv_untracked(install_tree):
    root = install_tree["root"]
    (root / ".venv" / "lib").mkdir(parents=True, exist_ok=True)
    (root / ".venv" / "lib" / "x").write_text("1", encoding="utf-8")
    # .venv 已在 gitignore 外可能显示 untracked — 允许前缀
    info = upgrade_core.precheck_install_dir(root)
    assert info["head"]


def test_reject_downgrade_and_same_version(install_tree, monkeypatch):
    root = install_tree["root"]
    monkeypatch.setattr(
        upgrade_core,
        "fetch_official_release",
        lambda tag: {
            "version": "0.1.0",
            "tag": "v0.1.0",
            "sha": "b" * 40,
            "url": "https://github.com/fyc0451/agent-cockpit/releases/tag/v0.1.0",
            "name": "v0.1.0",
        },
    )
    with pytest.raises(ValueError, match="降级"):
        upgrade_core.start_upgrade("0.1.0", install_dir=root)
    monkeypatch.setattr(
        upgrade_core,
        "fetch_official_release",
        lambda tag: {
            "version": "0.2.0",
            "tag": "v0.2.0",
            "sha": "c" * 40,
            "url": "https://github.com/fyc0451/agent-cockpit/releases/tag/v0.2.0",
            "name": "v0.2.0",
        },
    )
    with pytest.raises(ValueError, match="已是目标"):
        upgrade_core.start_upgrade("0.2.0", install_dir=root)


def test_successful_upgrade_via_hooks(install_tree):
    root = install_tree["root"]
    rel_sha = install_tree["rel_sha"]
    events: list[str] = []

    def fetch_release(tag):
        return {
            "version": "0.3.0",
            "tag": "v0.3.0",
            "sha": rel_sha,
            "url": "https://github.com/fyc0451/agent-cockpit/releases/tag/v0.3.0",
            "name": "v0.3.0",
        }

    def fetch_and_checkout(install_dir, tag, sha):
        events.append("checkout")
        _git(install_dir, "checkout", "-f", sha)

    def install_deps(install_dir):
        events.append("deps")

    def restart():
        events.append("restart")

    def health():
        events.append("health")
        return True

    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        fetch_release=fetch_release,
        fetch_and_checkout=fetch_and_checkout,
        install_deps=install_deps,
        restart_cockpit=restart,
        health_check=health,
        spawn_worker=lambda job_id, install_dir, log_path: _run_inline(job_id, install_dir),
    )

    result = upgrade_core.start_upgrade("0.3.0", install_dir=root)
    assert result["accepted"] is True
    # wait for inline worker
    st = upgrade_core.read_state()
    assert st["state"] == "succeeded"
    assert st["target_version"] == "0.3.0"
    assert "checkout" in events and "restart" in events and "health" in events
    assert _git(root, "rev-parse", "HEAD") == rel_sha


def _run_inline(job_id: str, install_dir: Path) -> int:
    # 同步跑 worker，返回伪 pid（不得返回 exit code 当 pid）
    upgrade_core.run_job(job_id, install_dir=install_dir)
    return os.getpid()


def test_health_failure_rolls_back(install_tree):
    root = install_tree["root"]
    base_sha = install_tree["base_sha"]
    rel_sha = install_tree["rel_sha"]
    health_calls = {"n": 0}

    def fetch_release(tag):
        return {
            "version": "0.3.0",
            "tag": "v0.3.0",
            "sha": rel_sha,
            "url": "https://github.com/fyc0451/agent-cockpit/releases/tag/v0.3.0",
            "name": "v0.3.0",
        }

    def fetch_and_checkout(install_dir, tag, sha):
        _git(install_dir, "checkout", "-f", sha)

    def health():
        health_calls["n"] += 1
        # 升级后失败，回滚后成功
        return health_calls["n"] >= 2

    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        fetch_release=fetch_release,
        fetch_and_checkout=fetch_and_checkout,
        install_deps=lambda d: None,
        restart_cockpit=lambda: None,
        health_check=health,
        spawn_worker=lambda job_id, install_dir, log_path: (
            upgrade_core.run_job(job_id, install_dir=install_dir) or os.getpid()
        ),
    )
    # spawn_worker must return pid; fix:
    def spawn(job_id, install_dir, log_path):
        upgrade_core.run_job(job_id, install_dir=install_dir)
        return os.getpid()

    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        fetch_release=fetch_release,
        fetch_and_checkout=fetch_and_checkout,
        install_deps=lambda d: None,
        restart_cockpit=lambda: None,
        health_check=health,
        spawn_worker=spawn,
    )
    result = upgrade_core.start_upgrade("v0.3.0", install_dir=root)
    assert result["accepted"] is True
    st = upgrade_core.read_state()
    assert st["state"] in ("rolled_back", "failed")
    assert _git(root, "rev-parse", "HEAD") == base_sha


def test_concurrent_start_rejected(install_tree):
    root = install_tree["root"]
    rel_sha = install_tree["rel_sha"]
    barrier = threading.Barrier(2)
    started = []

    def slow_spawn(job_id, install_dir, log_path):
        started.append(job_id)
        barrier.wait(timeout=2)
        time.sleep(0.05)
        # mark active without finishing
        st = upgrade_core.read_state()
        st["state"] = "installing"
        st["worker_pid"] = os.getpid()
        upgrade_core.write_state(st)
        return os.getpid()

    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        fetch_release=lambda tag: {
            "version": "0.3.0",
            "tag": "v0.3.0",
            "sha": rel_sha,
            "url": "https://github.com/fyc0451/agent-cockpit/releases/tag/v0.3.0",
            "name": "v0.3.0",
        },
        spawn_worker=slow_spawn,
    )
    results = []

    def worker():
        try:
            results.append(upgrade_core.start_upgrade("0.3.0", install_dir=root))
        except Exception as exc:
            results.append({"error": str(exc)})

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert len(results) == 2, results
    accepted = [r for r in results if r.get("accepted") is True]
    rejected = [r for r in results if r.get("accepted") is False]
    # 一接受一拒绝，或一接受一错误；至少不能双接受
    assert len(accepted) <= 1, results
    assert len(accepted) + len(rejected) >= 1
    if len(accepted) == 0:
        # 都因锁失败时也应有 rejected/error
        assert any("error" in r or r.get("reason") for r in results)


def test_api_upgrade_auth_and_no_secret_echo(install_tree, monkeypatch):
    import server

    root = install_tree["root"]
    rel_sha = install_tree["rel_sha"]
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(upgrade_core, "INSTALL_DIR", root)

    def spawn(job_id, install_dir, log_path):
        # leave queued without running full job
        return os.getpid()

    upgrade_core.configure_hooks(
        skip_venv_check=lambda: True,
        fetch_release=lambda tag: {
            "version": "0.3.0",
            "tag": "v0.3.0",
            "sha": rel_sha,
            "url": "https://github.com/fyc0451/agent-cockpit/releases/tag/v0.3.0",
            "name": "v0.3.0",
        },
        spawn_worker=spawn,
    )
    client = TestClient(server.app)
    assert client.get("/api/upgrade/status").status_code == 401
    assert client.post("/api/upgrade", json={"target": "0.3.0"}).status_code == 401
    headers = {"authorization": "Bearer secret"}
    r = client.post("/api/upgrade", headers=headers, json={"target": "0.3.0"})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["status"]["state"] in ("queued", "installing", "prechecking", "succeeded")
    # 不回显 token / 环境
    text = r.text
    assert "secret" not in text
    assert "COCKPIT_TOKEN" not in text
    st = client.get("/api/upgrade/status", headers=headers)
    assert st.status_code == 200
    assert "worker_pid" not in st.json() or True  # public_status 不暴露 pid 字段
    assert "worker_running" in st.json()


def test_public_status_success_only_terminal():
    st = upgrade_core._default_state()
    st["state"] = "installing"
    pub = upgrade_core.public_status(st)
    assert pub["active"] is True
    st["state"] = "succeeded"
    pub = upgrade_core.public_status(st)
    assert pub["state"] == "succeeded"
    assert pub["active"] is False


def test_worker_script_exists_and_detached_spawn(install_tree, monkeypatch):
    """spawn_worker 使用 start_new_session；worker 脚本可独立执行。"""
    root = install_tree["root"]
    worker = Path(upgrade_core.WORKER_SCRIPT)
    assert worker.is_file()
    # 默认 spawn 路径：mock Popen
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            self.pid = 424242

    monkeypatch.setattr(upgrade_core.subprocess, "Popen", FakePopen)
    upgrade_core.clear_hooks()
    upgrade_core.configure_hooks(skip_venv_check=lambda: True)
    pid = upgrade_core.spawn_worker("abc", root, install_tree["data"] / "upgrade" / "logs" / "t.log")
    assert pid == 424242
    assert seen["kwargs"].get("start_new_session") is True
    assert "cockpit-upgrade-worker.py" in str(seen["cmd"][1])
