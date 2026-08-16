from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import workspace_dispatch_service as dispatch_mod
from agent_cockpit import workspace_runtime_api as api


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
WORK = "wrk_" + "c" * 32


class _Service:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def dispatch(self, project_id, workspace_id, work_item_id, **kwargs):
        self.calls.append({
            "project_id": project_id, "workspace_id": workspace_id,
            "work_item_id": work_item_id, **kwargs,
        })
        if kwargs["expected_preparation_revision"] == 99:
            raise dispatch_mod.DispatchError("stale_revision")
        return {"operation_id": "op_fixed", "outcome": "succeeded"}


def _path() -> str:
    return (
        f"/api/projects/{PROJECT}/workspaces/{WORKSPACE}"
        f"/work-items/{WORK}/dispatch"
    )


def test_dispatch_http_contract_is_exact_and_sanitized() -> None:
    service = _Service()
    app = FastAPI()
    api.install(app, service)
    http = TestClient(app)
    missing = http.post(_path(), json={
        "expected_work_revision": 1, "expected_preparation_revision": 2,
    })
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "idempotency_key_required"
    extra = http.post(
        _path(), json={
            "expected_work_revision": 1, "expected_preparation_revision": 2,
            "body": "must never cross dispatch",
        }, headers={"Idempotency-Key": "dispatch"},
    )
    assert extra.status_code == 400
    assert extra.json()["error"]["code"] == "invalid_argument"
    ok = http.post(
        _path(), json={
            "expected_work_revision": 1, "expected_preparation_revision": 2,
        }, headers={"Idempotency-Key": "dispatch"},
    )
    assert ok.status_code == 200
    assert ok.json()["data"] == {
        "operation_id": "op_fixed", "outcome": "succeeded",
    }
    assert service.calls == [{
        "project_id": PROJECT, "workspace_id": WORKSPACE, "work_item_id": WORK,
        "expected_work_revision": 1, "expected_preparation_revision": 2,
        "idempotency_key": "dispatch",
    }]
    stale = http.post(
        _path(), json={
            "expected_work_revision": 1, "expected_preparation_revision": 99,
        }, headers={"Idempotency-Key": "stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"
