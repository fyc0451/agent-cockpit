from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server
from agent_cockpit import upgrade_core
from agent_cockpit import upgrade_service


def _auth() -> dict[str, str]:
    return {"authorization": "Bearer upgrade-test-token"}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "upgrade-test-token")
    monkeypatch.setattr(server.upgrade_service, "is_enabled", lambda: True)
    return TestClient(server.app)


def test_enabled_routes_remain_authenticated(client: TestClient) -> None:
    assert client.get("/api/upgrade/status").status_code == 401
    assert client.post("/api/upgrade").status_code == 401


def test_enabled_status_returns_pure_service_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "active": False,
        "available": True,
        "engine": "immutable-upgrade-controller",
        "journal": None,
        "reason": None,
        "state": "idle",
    }
    calls: list[str] = []
    monkeypatch.setattr(
        server.upgrade_service,
        "get_status",
        lambda: calls.append("status") or payload,
    )

    response = client.get("/api/upgrade/status", headers=_auth())

    assert response.status_code == 200
    assert response.json() == payload
    assert calls == ["status"]


def test_enabled_start_returns_202_allowlisted_receipt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = {
        "accepted": True,
        "pid": 4321,
        "request_id": "request-1",
        "target_version": "0.3.0",
    }
    calls: list[str] = []
    monkeypatch.setattr(
        server.upgrade_service,
        "start_latest",
        lambda: calls.append("start") or receipt,
    )

    response = client.post(
        "/api/upgrade", headers=_auth(), json={"legacy": "body is ignored"},
    )

    assert response.status_code == 202
    assert response.json() == receipt
    assert calls == ["start"]


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        ("upgrade_busy", 409),
        ("already_current", 409),
        ("request_invalid", 400),
        ("controller_unavailable", 503),
        ("trust_unavailable", 503),
        ("release_unavailable", 503),
        ("platform_unsupported", 503),
        ("unexpected_private_detail", 500),
    ],
)
def test_enabled_start_maps_only_stable_error_code(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status_code: int,
) -> None:
    def fail() -> None:
        raise upgrade_service.UpgradeServiceError(code)

    monkeypatch.setattr(server.upgrade_service, "start_latest", fail)

    response = client.post("/api/upgrade", headers=_auth())

    assert response.status_code == status_code
    if status_code == 500:
        assert response.json() == {"detail": "服务器内部错误，请稍后重试"}
        assert code not in response.text
    else:
        assert response.json() == {"detail": {"error_code": code}}


def test_enabled_route_never_calls_legacy_upgrade_engine(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        upgrade_core,
        "retired_start_response",
        lambda: pytest.fail("legacy engine must not run"),
    )
    monkeypatch.setattr(
        server.upgrade_service,
        "start_latest",
        lambda: {
            "accepted": True,
            "pid": 123,
            "request_id": "request-1",
            "target_version": "0.3.0",
        },
    )

    assert client.post("/api/upgrade", headers=_auth()).status_code == 202


def test_disabled_route_keeps_exact_retired_contract(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server.upgrade_service, "is_enabled", lambda: False)
    monkeypatch.setattr(
        server.upgrade_service,
        "start_latest",
        lambda: pytest.fail("disabled V2 must not run"),
    )
    monkeypatch.setattr(
        server.upgrade_service,
        "get_status",
        lambda: pytest.fail("disabled V2 must not run"),
    )

    assert client.get("/api/upgrade/status", headers=_auth()).json() == (
        upgrade_core.retired_status()
    )
    assert client.post("/api/upgrade", headers=_auth()).json() == (
        upgrade_core.retired_start_response()
    )
