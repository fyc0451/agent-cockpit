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
from agent_cockpit import upgrade_core


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

    @pytest.mark.parametrize(
        "request_kwargs",
        [
            {},
            {"content": b"null", "headers": {"content-type": "application/json"}},
            {"json": {}},
            {"json": {"target": None}},
            {"json": {"target": 123}},
            {"json": {"target": ["0.3.0"]}},
            {"json": {"target": ""}},
            {"json": {"target": "x" * 64}},
            {"json": {"target": "../../release"}},
            {"json": {"target": "0.3.0\nignored"}},
            {"json": {"anything": "is ignored"}},
            {"content": b'{"target":', "headers": {"content-type": "application/json"}},
        ],
    )
    def test_any_authenticated_body_returns_same_retired_contract(
        self, client: TestClient, request_kwargs: dict[str, Any],
    ) -> None:
        kwargs = dict(request_kwargs)
        headers = {**_auth(), **kwargs.pop("headers", {})}

        response = client.post(
            "/api/upgrade", headers=headers, **kwargs,
        )

        assert response.status_code == 200, response.text
        assert response.json() == upgrade_core.retired_start_response()

    def test_auth_precedes_malformed_body(self, client: TestClient) -> None:
        response = client.post(
            "/api/upgrade",
            content=b'{"target":',
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 401


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


class TestRetiredPythonEntrypoints:
    class _ExplodingBool:
        def __bool__(self) -> bool:
            raise AssertionError("retired entrypoint evaluated a mutable flag")

    @pytest.mark.parametrize("retired_flag", [False, _ExplodingBool()])
    def test_all_internal_entrypoints_refuse_before_side_effects(
        self, monkeypatch: Any, tmp_path: Path, retired_flag: object,
    ) -> None:
        def forbidden(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("retired entrypoint reached a side effect")

        monkeypatch.setattr(
            upgrade_core, "UPGRADE_ENGINE_RETIRED", retired_flag, raising=False,
        )
        for name in (
            "read_state", "write_state", "_ensure_dirs", "fetch_official_release",
            "precheck_install_dir", "_worker_alive", "_run_job_locked",
        ):
            monkeypatch.setattr(upgrade_core, name, forbidden)
        monkeypatch.setattr(upgrade_core.subprocess, "run", forbidden)
        monkeypatch.setattr(upgrade_core.subprocess, "Popen", forbidden)
        monkeypatch.setattr(
            upgrade_core,
            "_hooks",
            {
                "spawn_worker": forbidden,
                "spawn_rollback_worker": forbidden,
                "fetch_release": forbidden,
            },
        )

        assert upgrade_core.start_upgrade("anything") == (
            upgrade_core.retired_start_response()
        )
        assert upgrade_core.public_status({"state": "installing"}) == (
            upgrade_core.retired_status()
        )
        assert upgrade_core.reconcile_stale_state({"state": "installing"}) == (
            upgrade_core.retired_status()
        )
        with pytest.raises(RuntimeError, match="^upgrade_engine_retired$"):
            upgrade_core.spawn_worker("job", tmp_path, tmp_path / "worker.log")
        with pytest.raises(RuntimeError, match="^upgrade_engine_retired$"):
            upgrade_core.spawn_rollback_worker(
                {"job_id": "job", "install_dir": str(tmp_path)},
            )
        assert upgrade_core.run_job("job", install_dir=tmp_path) == 1

    def test_production_modules_do_not_reference_legacy_entrypoints(self) -> None:
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.glob("*.py"):
            if path.name == "upgrade_core.py":
                continue
            if "upgrade_core._legacy_" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        assert offenders == []


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

    def test_upgrade_sh_is_separate_safe_cli_entry(self, tmp_path: Path) -> None:
        script = Path(__file__).resolve().parent.parent / "upgrade.sh"
        content = script.read_text(encoding="utf-8")
        assert "upgrade_engine_retired" not in content
        assert "merge --ff-only" in content
        assert 'bash "$INSTALL_DIR/install.sh"' in content
        assert "reset --hard" in content
        # CLI 不恢复应用内旧 worker/API，也不复制依赖和 supervisor 逻辑。
        for forbidden in ("pip install", "systemctl", "install-agent-mail-hub"):
            assert forbidden not in content


# ---------------------------------------------------------------------------
# docs 受管发布边界
# ---------------------------------------------------------------------------

class TestDocs:
    def test_all_readmes_describe_source_cli_upgrade(self) -> None:
        for name in ("README.md", "README.en.md", "README.ja.md"):
            text = Path(name).read_text(encoding="utf-8")
            assert "./upgrade.sh" in text
        assert "自动回滚" in Path("README.md").read_text(encoding="utf-8")
        assert "automatic rollback" in Path("README.en.md").read_text(encoding="utf-8")
        assert "自動ロールバック" in Path("README.ja.md").read_text(encoding="utf-8")

    def test_user_guide_documents_one_command_upgrade(self) -> None:
        text = Path("docs/USER-GUIDE.md").read_text(encoding="utf-8")
        assert "bash upgrade.sh" in text
        assert "自动回滚" in text

    def test_docs_keep_web_v1_retired_boundary(self) -> None:
        zh = Path("README.md").read_text(encoding="utf-8")
        en = Path("README.en.md").read_text(encoding="utf-8")
        ja = Path("README.ja.md").read_text(encoding="utf-8")
        assert "旧 V1 Web 升级 API 仍保持退役" in zh
        assert "legacy V1 Web upgrade API remains retired" in en
        assert "旧 V1 Web アップグレード API は引き続き退役" in ja
