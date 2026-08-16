from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_cockpit import local_codex_harness as harness_mod
from agent_cockpit import operation_store
from agent_cockpit import workspace_dispatch_service as dispatch_mod
from agent_cockpit import workspace_execution_store
from agent_cockpit import workspace_work_store


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
SENTINEL = "BOSS-DISPATCH-SENTINEL-5e08"


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
    def __init__(self, *, unknown: bool = False) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.unknown = unknown

    def wakeup(self, *args: object, **kwargs: object) -> dict[str, str]:
        self.calls.append(args + (kwargs,))
        if self.unknown:
            raise RuntimeError("response lost after fixed wakeup")
        return {"digest": "sha256:" + "c" * 64, "text": "COCKPIT_WAKEUP_V1"}


def _world(tmp_path: Path, *, unknown: bool = False):
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
    attaching, attachment, _checkout = execution.begin_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        expected_revision=prepared.revision, session_name="session-fixed",
    )
    connected = execution.finish_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
        expected_revision=attaching.revision, pane_id="pane-fixed",
        instance_id="i-fixed", native_receipt="sha256:" + "d" * 64,
        identity_verified=True,
    )
    wakeup = _Wakeup(unknown=unknown)
    operations = operation_store.initialize(tmp_path / "ops" / "operation.sqlite3")
    service = dispatch_mod.DispatchService(
        registry_provider=lambda: _Registry(), work_provider=lambda: work,
        execution=execution, operations=operations, harness=wakeup,
    )
    return service, work, operations, wakeup, work_id, connected.revision


def test_dispatch_is_idempotent_fixed_wakeup_and_never_persists_boss_body(
    tmp_path: Path,
) -> None:
    service, work, operations, wakeup, work_id, prep_revision = _world(tmp_path)
    result = service.dispatch(
        PROJECT, WORKSPACE, work_id, expected_work_revision=1,
        expected_preparation_revision=prep_revision, idempotency_key="dispatch-1",
    )
    replay = service.dispatch(
        PROJECT, WORKSPACE, work_id, expected_work_revision=1,
        expected_preparation_revision=prep_revision, idempotency_key="dispatch-1",
    )
    assert replay == result
    assert result["outcome"] == "succeeded"
    assert wakeup.calls == [(result["attachment_id"], {})]
    detail = work.get_work_item_detail(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
    )
    assert detail is not None
    assert detail["work_item"]["status"] == "unassigned"
    assert detail["work_item"]["revision"] == 3
    assert [item["outcome"] for item in detail["receipts"]] == [
        "intent", "succeeded",
    ]
    with sqlite3.connect(operations.path) as connection:
        operation_dump = json.dumps(list(connection.iterdump()))
    with sqlite3.connect(work.path) as connection:
        receipt_dump = json.dumps(list(connection.execute(
            "SELECT kind,outcome,reason,evidence_digest FROM message_receipts"
        )))
    assert SENTINEL not in operation_dump
    assert SENTINEL not in receipt_dump


def test_response_lost_is_stable_unknown_and_never_rewakes(tmp_path: Path) -> None:
    service, work, _operations, wakeup, work_id, prep_revision = _world(
        tmp_path, unknown=True,
    )
    for _ in range(2):
        with pytest.raises(dispatch_mod.DispatchError) as error:
            service.dispatch(
                PROJECT, WORKSPACE, work_id, expected_work_revision=1,
                expected_preparation_revision=prep_revision,
                idempotency_key="dispatch-unknown",
            )
        assert error.value.code == "wakeup_outcome_unknown"
    assert len(wakeup.calls) == 1
    detail = work.get_work_item_detail(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
    )
    assert detail is not None
    assert detail["work_item"]["status"] == "unassigned"
    assert [item["outcome"] for item in detail["receipts"]] == [
        "intent", "outcome_unknown",
    ]


def test_unproven_wakeup_does_not_record_delivery_succeeded(
    tmp_path: Path,
) -> None:
    class Stall:
        def wakeup(self, *args: object, **kwargs: object) -> dict[str, str]:
            raise harness_mod.HarnessError("runtime_unavailable")

    service, work, _operations, _wakeup, work_id, prep_revision = _world(tmp_path)
    service.harness = Stall()
    with pytest.raises(dispatch_mod.DispatchError) as error:
        service.dispatch(
            PROJECT, WORKSPACE, work_id, expected_work_revision=1,
            expected_preparation_revision=prep_revision,
            idempotency_key="dispatch-stalled",
        )
    assert error.value.code == "runtime_unavailable"
    detail = work.get_work_item_detail(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
    )
    assert detail is not None
    assert [item["outcome"] for item in detail["receipts"]] == ["intent"]
    assert SENTINEL not in json.dumps(detail["receipts"])


def test_concurrent_same_key_dispatch_wakes_once(tmp_path: Path) -> None:
    service, _work, _operations, wakeup, work_id, prep_revision = _world(tmp_path)

    def dispatch():
        return service.dispatch(
            PROJECT, WORKSPACE, work_id, expected_work_revision=1,
            expected_preparation_revision=prep_revision,
            idempotency_key="dispatch-race",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [future.result() for future in [
            pool.submit(dispatch) for _ in range(8)
        ]]
    assert all(item == results[0] for item in results)
    assert len(wakeup.calls) == 1


def test_delivery_receipt_and_revision_are_one_transaction(
    tmp_path: Path, monkeypatch,
) -> None:
    work = workspace_work_store.initialize(tmp_path / "workspace-work.sqlite3")
    created = work.create_work_item(
        project_id=PROJECT, workspace_id=WORKSPACE, body=SENTINEL,
        acceptance=None, constraints=None, idempotency_key="create",
    )
    work_id = created.item.work_item["work_item_id"]

    def fail(*_args, **_kwargs):
        raise workspace_work_store.WorkspaceWorkError("store_write_failed")

    monkeypatch.setattr(workspace_work_store, "_remember", fail)
    with pytest.raises(workspace_work_store.WorkspaceWorkError) as error:
        work.record_delivery(
            project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
            expected_revision=1, outcome="intent",
            evidence_digest="sha256:" + "a" * 64,
            idempotency_key="delivery",
        )
    assert error.value.code == "store_write_failed"
    with sqlite3.connect(work.path) as connection:
        assert connection.execute(
            "SELECT revision FROM work_items"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM message_receipts"
        ).fetchone()[0] == 0
