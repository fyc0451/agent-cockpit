from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from agent_cockpit import source_upgrade
from agent_cockpit import upgrade_core
from agent_cockpit import upgrade_service


def _auth() -> dict[str, str]:
    return {"authorization": "Bearer source-upgrade-token"}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "source-upgrade-token")
    monkeypatch.setenv("COCKPIT_NEXT_PROFILE", "dev")
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    return TestClient(server.app)


def test_source_runtime_requires_dev_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COCKPIT_NEXT_PROFILE", raising=False)
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    assert source_upgrade.is_source_runtime() is False
    monkeypatch.setenv("COCKPIT_NEXT_PROFILE", "dev")
    assert source_upgrade.is_source_runtime() is True


def test_source_status_and_start(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "job_id": None,
        "state": "idle",
        "engine": source_upgrade.ENGINE,
        "target_version": None,
        "from_version": None,
        "phase": None,
        "error_code": None,
        "error_message": None,
        "active": False,
        "available": True,
        "reason": None,
        "worker_running": False,
    }
    receipt = {
        "accepted": True,
        "job_id": "source-upgrade-1",
        "target_version": "0.3.7",
        "target_tag": "agent-cockpit-v0.3.7",
        "engine": source_upgrade.ENGINE,
    }
    monkeypatch.setattr(server.source_upgrade, "get_status", lambda: payload)
    monkeypatch.setattr(server.source_upgrade, "start_latest", lambda: receipt)
    monkeypatch.setattr(
        server.upgrade_service,
        "start_latest",
        lambda: pytest.fail("V2 must not run on source 8790"),
    )

    status = client.get("/api/upgrade/status", headers=_auth())
    assert status.status_code == 200
    assert status.json() == payload

    started = client.post("/api/upgrade", headers=_auth())
    assert started.status_code == 202
    assert started.json() == receipt


def test_source_start_maps_stable_error(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        raise source_upgrade.SourceUpgradeError("precheck_dirty")

    monkeypatch.setattr(server.source_upgrade, "start_latest", fail)
    response = client.post("/api/upgrade", headers=_auth())
    assert response.status_code == 409
    assert response.json() == {"detail": {"error_code": "precheck_dirty"}}


def test_v2_flag_still_wins_over_source(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.upgrade_service, "is_enabled", lambda: True)
    monkeypatch.setattr(
        server.upgrade_service,
        "get_status",
        lambda: {
            "active": False,
            "available": True,
            "engine": upgrade_service.ENGINE,
            "journal": None,
            "reason": None,
            "state": "idle",
        },
    )
    monkeypatch.setattr(
        server.source_upgrade,
        "get_status",
        lambda: pytest.fail("source path must not run when V2 is enabled"),
    )
    response = client.get("/api/upgrade/status", headers=_auth())
    assert response.status_code == 200
    assert response.json()["engine"] == upgrade_service.ENGINE


def test_retired_contract_without_dev_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "source-upgrade-token")
    monkeypatch.delenv("COCKPIT_NEXT_PROFILE", raising=False)
    client = TestClient(server.app)
    assert client.get("/api/upgrade/status", headers=_auth()).json() == (
        upgrade_core.retired_status()
    )
    assert client.post("/api/upgrade", headers=_auth()).json() == (
        upgrade_core.retired_start_response()
    )


def test_start_latest_accepts_clean_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COCKPIT_NEXT_PROFILE", "dev")
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.setattr(source_upgrade, "_latest_version", lambda: "0.3.7")
    monkeypatch.setattr(source_upgrade.version, "read_current_version", lambda: "0.3.6")
    monkeypatch.setattr(source_upgrade, "precheck_install_dir", lambda install_dir=None: {})
    monkeypatch.setattr(source_upgrade, "spawn_worker", lambda job_id: 4242)
    monkeypatch.setattr(source_upgrade, "_store_dir", lambda: tmp_path)
    monkeypatch.setattr(source_upgrade, "public_status", lambda state=None: {
        "active": False,
        "available": True,
        "state": "idle",
    })

    result = source_upgrade.start_latest()
    assert result["accepted"] is True
    assert result["target_version"] == "0.3.7"
    assert result["target_tag"] == "agent-cockpit-v0.3.7"
    assert Path(tmp_path / "source-state.json").is_file()
