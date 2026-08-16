from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import operation_store
from agent_cockpit import workspace_dispatch_service as dispatch_mod
from agent_cockpit import workspace_execution_store
from agent_cockpit import workspace_runtime_api as api
from agent_cockpit import workspace_work_store


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
WORK = "wrk_" + "c" * 32
SENTINEL = "BOSS-DISPATCH-SENTINEL-5e08"
PUBLIC = {"operation_id", "outcome"}
INTERNAL = {
    "work_item_id", "attachment_id", "work_revision", "attempt", "wakeup",
}


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


class _FatService(_Service):
    def dispatch(self, project_id, workspace_id, work_item_id, **kwargs):
        if kwargs["expected_preparation_revision"] == 99:
            raise dispatch_mod.DispatchError("stale_revision")
        return {
            "operation_id": "op_fixed",
            "work_item_id": work_item_id,
            "attachment_id": "att_leaked",
            "outcome": "succeeded",
            "work_revision": 3,
            "attempt": {"n": 1},
            "wakeup": {"digest": "secret"},
        }


class _Registry:
    def get_project_by_id(self, project_id: str):
        if project_id != PROJECT:
            return None
        project = type("Project", (), {"lifecycle": "active"})()
        return type("Snapshot", (), {"project": project})()

    def get_workspace(self, project_id: str, workspace_id: str):
        if (project_id, workspace_id) != (PROJECT, WORKSPACE):
            return None
        return type("Workspace", (), {
            "project_id": PROJECT,
            "workspace_id": WORKSPACE,
            "lifecycle": "active",
        })()


class _Wakeup:
    def wakeup(self, *args: object, **kwargs: object) -> dict[str, str]:
        return {"digest": "sha256:" + "c" * 64, "text": "COCKPIT_WAKEUP_V1"}


def _w2_accepts(data: object) -> bool:
    return (
        isinstance(data, dict)
        and set(data) == PUBLIC
        and isinstance(data.get("operation_id"), str)
        and data.get("operation_id") != ""
        and data.get("outcome") == "succeeded"
    )


def _world(tmp_path: Path):
    work = workspace_work_store.initialize(tmp_path / "workspace-work.sqlite3")
    created = work.create_work_item(
        project_id=PROJECT, workspace_id=WORKSPACE, body=SENTINEL,
        acceptance="done", constraints="no leak", idempotency_key="create",
    )
    work_id = created.item.work_item["work_item_id"]
    execution = workspace_execution_store.initialize(
        tmp_path / "workspace-execution.sqlite3"
    )
    identity = execution.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE,
        display_name="Atlas", idempotency_key="member",
    ).item
    execution.claim_prepare(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=identity.identity_id,
    )
    prepared = execution.complete_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        identity_id=identity.identity_id, source_head="1" * 40,
        source_tree="2" * 40, internal_path=str(tmp_path / "checkout"),
        operation_id=None,
    )
    attaching, _attachment, _checkout = execution.begin_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        expected_revision=prepared.revision, session_name="session-fixed",
    )
    connected = execution.finish_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        expected_revision=attaching.revision, pane_id="pane-fixed",
        instance_id="i-fixed", native_receipt="sha256:" + "d" * 64,
        identity_verified=True,
    )
    service = dispatch_mod.DispatchService(
        registry_provider=lambda: _Registry(), work_provider=lambda: work,
        execution=execution,
        operations=operation_store.initialize(tmp_path / "ops" / "operation.sqlite3"),
        harness=_Wakeup(),
    )
    return service, work_id, connected.revision


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


def test_http_projects_real_service_five_fields_to_w2_dto(tmp_path: Path) -> None:
    service, work_id, prep_revision = _world(tmp_path)
    raw = service.dispatch(
        PROJECT, WORKSPACE, work_id, expected_work_revision=1,
        expected_preparation_revision=prep_revision, idempotency_key="raw",
    )
    assert set(raw) == {
        "operation_id", "work_item_id", "attachment_id", "outcome",
        "work_revision",
    }
    assert not _w2_accepts(raw)

    app = FastAPI()
    api.install(app, service)
    http = TestClient(app)
    ok = http.post(
        f"/api/projects/{PROJECT}/workspaces/{WORKSPACE}/work-items/{work_id}/dispatch",
        json={
            "expected_work_revision": 1,
            "expected_preparation_revision": prep_revision,
        },
        headers={"Idempotency-Key": "raw"},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert set(body) == {"data", "meta"}
    assert _w2_accepts(body["data"])
    assert INTERNAL.isdisjoint(body["data"])
    assert SENTINEL not in ok.text
    assert "meta" in body and body["meta"]["partial"] is False
    stale = http.post(
        f"/api/projects/{PROJECT}/workspaces/{WORKSPACE}/work-items/{work_id}/dispatch",
        json={
            "expected_work_revision": 1,
            "expected_preparation_revision": 99,
        },
        headers={"Idempotency-Key": "stale-real"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"


def test_http_projects_fat_fake_and_keeps_typed_errors() -> None:
    service = _FatService()
    app = FastAPI()
    api.install(app, service)
    http = TestClient(app)
    ok = http.post(
        _path(), json={
            "expected_work_revision": 1, "expected_preparation_revision": 2,
        }, headers={"Idempotency-Key": "fat"},
    )
    assert ok.status_code == 200
    assert _w2_accepts(ok.json()["data"])
    assert INTERNAL.isdisjoint(ok.json()["data"])
    stale = http.post(
        _path(), json={
            "expected_work_revision": 1, "expected_preparation_revision": 99,
        }, headers={"Idempotency-Key": "stale-fat"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"
