"""test_upgrade_retired.py — Wiki13 J0：V1 升级引擎 fail-closed 退役测试。

覆盖：API POST/GET fail-closed 契约、认证保持、start/worker hooks 零调用、
status 纯只读无副作用、worker/shell 入口拒绝、无效输入、docs 受管边界。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import server
import upgrade_core


def _auth(token: str = "secret-token-xyz") -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture()
def client(monkeypatch: Any) -> TestClient:
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret-token-xyz", raising=False)
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# POST /api/upgrade fail-closed
# ---------------------------------------------------------------------------

class TestPostUpgrade:
    def test_auth_still_required(self, client: TestClient) -> None:
        r_none = client.post("/api/upgrade", json={"target": "0.3.0"})
        assert r_none.status_code == 401
        r_bad = client.post(
            "/api/upgrade", headers=_auth("wrong"), json={"target": "0.3.0"},
        )
        assert r_bad.status_code == 401

    def test_fail_closed_contract(self, client: TestClient) -> None:
        r = client.post(
            "/api/upgrade", headers=_auth(), json={"target": "0.3.0"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["accepted"] is False
        assert body["reason"] == "upgrade_engine_retired"
        status = body["status"]
        assert status["state"] == "retired"
        assert status["error_code"] == "upgrade_engine_retired"
        assert status["active"] is False
        assert status["worker_running"] is False

    def test_start_and_worker_hooks_zero_calls(self, client: TestClient, monkeypatch: Any) -> None:
        calls: dict[str, int] = {
            "start_upgrade": 0, "spawn_worker": 0,
            "spawn_rollback_worker": 0, "reconcile_stale_state": 0,
        }

        def record(name: str) -> Any:
            def fn(*args: Any, **kwargs: Any) -> Any:
                calls[name] += 1
                return {}
            return fn

        monkeypatch.setattr(upgrade_core, "start_upgrade", record("start_upgrade"))
        monkeypatch.setattr(upgrade_core, "spawn_worker", record("spawn_worker"))
        monkeypatch.setattr(
            upgrade_core, "spawn_rollback_worker", record("spawn_rollback_worker"),
        )
        monkeypatch.setattr(
            upgrade_core, "reconcile_stale_state", record("reconcile_stale_state"),
        )
        r = client.post(
            "/api/upgrade", headers=_auth(), json={"target": "0.3.0"},
        )
        assert r.status_code == 200
        assert calls == {
            "start_upgrade": 0, "spawn_worker": 0,
            "spawn_rollback_worker": 0, "reconcile_stale_state": 0,
        }

    def test_invalid_target_still_validated(self, client: TestClient) -> None:
        r = client.post("/api/upgrade", headers=_auth(), json={"target": "x" * 64})
        assert r.status_code == 422  # UpgradeReq 校验保留（稳定无效输入契约）


# ---------------------------------------------------------------------------
# GET /api/upgrade/status 只读 retired 契约
# ---------------------------------------------------------------------------

class TestUpgradeStatus:
    def test_auth_still_required(self, client: TestClient) -> None:
        assert client.get("/api/upgrade/status").status_code == 401

    def test_readonly_retired_contract(self, client: TestClient) -> None:
        r = client.get("/api/upgrade/status", headers=_auth())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "retired"
        assert body["error_code"] == "upgrade_engine_retired"
        assert body["active"] is False
        assert body["worker_running"] is False
        assert body["job_id"] is None

    def test_no_reconcile_no_worker_no_state_side_effects(
        self, client: TestClient, monkeypatch: Any,
    ) -> None:
        calls: dict[str, int] = {
            "reconcile_stale_state": 0, "_worker_alive": 0, "write_state": 0,
        }

        def record(name: str) -> Any:
            def fn(*args: Any, **kwargs: Any) -> Any:
                calls[name] += 1
                return None
            return fn

        monkeypatch.setattr(
            upgrade_core, "reconcile_stale_state", record("reconcile_stale_state"),
        )
        monkeypatch.setattr(upgrade_core, "_worker_alive", record("_worker_alive"))
        monkeypatch.setattr(upgrade_core, "write_state", record("write_state"))
        r1 = client.get("/api/upgrade/status", headers=_auth())
        r2 = client.get("/api/upgrade/status", headers=_auth())
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json() == r2.json()  # 稳定契约，无状态漂移
        assert calls == {
            "reconcile_stale_state": 0, "_worker_alive": 0, "write_state": 0,
        }

    def test_retired_status_has_no_secrets(self) -> None:
        payload = str(upgrade_core.retired_status())
        for secret in ("token", "password", "secret", "BEGIN", "api_key"):
            assert secret.lower() not in payload.lower()


# ---------------------------------------------------------------------------
# worker / shell 入口拒绝
# ---------------------------------------------------------------------------

class TestEntryPoints:
    def test_worker_entry_refuses(self, tmp_path: Path) -> None:
        worker = Path(__file__).resolve().parent.parent / "cockpit-upgrade-worker.py"
        r = subprocess.run(
            [sys.executable, str(worker), "--job-id", "job-1"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 1
        assert "upgrade_engine_retired" in r.stderr

    def test_upgrade_sh_refuses(self, tmp_path: Path) -> None:
        script = Path(__file__).resolve().parent.parent / "upgrade.sh"
        r = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 1
        assert "upgrade_engine_retired" in r.stderr
        # 旧执行旁路已删除：脚本不得再包含实际执行命令
        content = script.read_text(encoding="utf-8")
        for forbidden in ("git -C", "pip install", "systemctl", "install-agent-mail-hub"):
            assert forbidden not in content


# ---------------------------------------------------------------------------
# docs 受管发布边界
# ---------------------------------------------------------------------------

class TestDocs:
    def test_readme_no_one_click_upgrade(self) -> None:
        text = Path("README.md").read_text(encoding="utf-8")
        assert "已退役" in text
        assert "受管人工发布" in text
        assert "升级后自动重启 systemd" not in text

    def test_user_guide_no_one_click_upgrade(self) -> None:
        text = Path("docs/USER-GUIDE.md").read_text(encoding="utf-8")
        assert "拉代码 + 装依赖" not in text
        assert "已退役" in text
