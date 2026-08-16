from __future__ import annotations

from pathlib import Path

import pytest

from agent_cockpit import operation_store as operation_mod
from agent_cockpit import workspace_claim_activation as activate_mod
from agent_cockpit import workspace_execution_store as exec_mod
from agent_cockpit import workspace_work_store as work_mod


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
SENTINEL = "BOSS-CLAIM-SENTINEL-71a9"
KEY = "activate"


class _Count:
    def __init__(self, inner: operation_mod.OperationStore) -> None:
        self.inner = inner
        self.prepare = 0
        self.dispatch = 0

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def prepare_attempt(self, *args, **kwargs):
        self.prepare += 1
        return self.inner.prepare_attempt(*args, **kwargs)

    def dispatch_attempt(self, *args, **kwargs):
        self.dispatch += 1
        return self.inner.dispatch_attempt(*args, **kwargs)


class _StaleRevision:
    def __init__(self, inner: operation_mod.OperationStore) -> None:
        self.inner = inner

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def create_operation(self, **kwargs):
        result = self.inner.create_operation(**kwargs)
        operation = dict(result.projection["operation"])
        operation["revision"] = 1
        projection = dict(result.projection)
        projection["operation"] = operation
        return operation_mod.CreateResult(
            result.operation_id, result.request_digest, result.replayed, projection,
        )


def _world(tmp_path: Path):
    execution = exec_mod.initialize(tmp_path / "workspace-execution.sqlite3")
    work = work_mod.initialize(tmp_path / "workspace-work.sqlite3")
    operations = operation_mod.initialize(tmp_path / "operation.sqlite3")
    member = execution.create_identity(
        project_id=PROJECT, workspace_id=WORKSPACE, display_name="Atlas",
        idempotency_key="member",
    ).item
    created = work.create_work_item(
        project_id=PROJECT, workspace_id=WORKSPACE, body=SENTINEL,
        acceptance="done", constraints="local", idempotency_key="create",
    )
    work_item_id = created.item.work_item["work_item_id"]
    dest = tmp_path / "checkout"
    dest.mkdir()
    prepared = execution.complete_preparation(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        identity_id=member.identity_id, source_head="a" * 40,
        source_tree="b" * 40, internal_path=str(dest), operation_id=None,
    )
    attaching, attachment, _checkout = execution.begin_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_revision=prepared.revision, session_name="s",
    )
    connected = execution.finish_attach(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        expected_revision=attaching.revision, pane_id="pane-1",
        instance_id="inst-1", native_receipt="secret", identity_verified=True,
    )
    reserved = work.reserve_claim(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
        identity_id=member.identity_id, generation=1,
        expected_revision=1, idempotency_key="reserve",
    )
    context = {
        "project_id": PROJECT,
        "workspace_id": WORKSPACE,
        "work_item_id": work_item_id,
        "expected_preparation_revision": connected.revision,
        "expected_lease_revision": connected.lease.revision,
        "expected_work_revision": 1,
        "attachment_id": attachment.attachment_id,
        "identity_id": member.identity_id,
        "generation": 1,
    }
    return execution, work, operations, reserved, context


def _request(context: dict[str, object], claim_id: str) -> dict[str, object]:
    return {
        "attachment_id": context["attachment_id"],
        "claim_id": claim_id,
        "generation": context["generation"],
        "identity_id": context["identity_id"],
        "work_item_id": context["work_item_id"],
    }


def _crash_running(tmp_path: Path, *, record_outcome: bool = False):
    execution, work, operations, reserved, context = _world(tmp_path)
    claim_id = str(reserved["claim"]["claim_id"])
    request = _request(context, claim_id)
    created = operations.create_operation(
        scope="workspace-claim-activate.v1",
        idempotency_key=KEY,
        request=request,
        kind="claim.activate",
        subject_type="lease",
        subject_id=claim_id,
        plan_digest=activate_mod._sha(request),
        approval_required=False,
        project_id=PROJECT,
        workspace_id=WORKSPACE,
        steps=(operation_mod.Step("activate-lease", "claim.activate"),),
    )
    revision = int(created.projection["operation"]["revision"])
    operations.transition(
        created.operation_id, expected_operation_revision=revision, status="running",
    )
    prepared = operations.prepare_attempt(
        created.operation_id, "activate-lease",
        expected_operation_revision=revision + 1,
        expected_step_revision=1, mode="execute",
        provider_kind="execution_store",
    )
    operations.dispatch_attempt(
        created.operation_id, prepared.step_execution_id,
        expected_operation_revision=revision + 2,
    )
    lease = execution.activate_claim_lease(
        project_id=PROJECT, workspace_id=WORKSPACE,
        work_item_id=str(context["work_item_id"]),
        expected_preparation_revision=context["expected_preparation_revision"],
        expected_lease_revision=context["expected_lease_revision"],
        attachment_id=context["attachment_id"],
        identity_id=context["identity_id"],
        generation=context["generation"],
        claim_id=claim_id,
        idempotency_key=KEY + ":lease",
    )
    receipt_id = None
    if record_outcome:
        receipt_id = "rcp_" + "ab" * 16
        operations.record_attempt_outcome(
            created.operation_id, prepared.step_execution_id,
            expected_operation_revision=revision + 3,
            expected_step_revision=2,
            receipt_id=receipt_id,
            receipt_type="provider_outcome",
            outcome="succeeded",
            evidence_kind="opaque_digest",
            evidence_digest=activate_mod._sha({"lease": lease["lease"]}),
        )
    return {
        "execution": execution,
        "work": work,
        "operations": operations,
        "reserved": reserved,
        "context": context,
        "operation_id": created.operation_id,
        "step_execution_id": prepared.step_execution_id,
        "receipt_id": receipt_id,
        "revision": revision,
    }


def _detail(work, work_item_id: str):
    return work.get_work_item_detail(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_item_id,
    )


def _claim_state(detail) -> str:
    claim = detail["claim"]
    assert isinstance(claim, dict)
    return str(claim["state"])


def test_first_activate_terminals_journal_on_single_attempt(tmp_path: Path) -> None:
    execution, work, operations, reserved, context = _world(tmp_path)
    activated = activate_mod.ClaimActivator(
        execution=execution, work=work, operations=operations,
    ).activate(context, reserved, idempotency_key=KEY)
    assert activated["lease"]["status"] == "active"
    assert activated["claim"]["state"] == "active"
    assert activated["work_item"]["status"] == "working"
    assert activated["root_message"]["body"] == SENTINEL
    created = operations.create_operation(
        scope="workspace-claim-activate.v1",
        idempotency_key=KEY,
        request=_request(context, str(reserved["claim"]["claim_id"])),
        kind="claim.activate",
        subject_type="lease",
        subject_id=str(reserved["claim"]["claim_id"]),
        plan_digest=activate_mod._sha(
            _request(context, str(reserved["claim"]["claim_id"]))
        ),
        approval_required=False,
        project_id=PROJECT,
        workspace_id=WORKSPACE,
        steps=(operation_mod.Step("activate-lease", "claim.activate"),),
    )
    projection = created.projection
    assert projection["operation"]["status"] == "succeeded"
    assert len(projection["attempts"]) == 1
    assert projection["attempts"][0]["status"] == "succeeded"
    assert len(projection["receipts"]) == 1
    assert projection["receipts"][0]["outcome"] == "succeeded"
    assert projection["receipts"][0]["step_execution_id"] == (
        projection["attempts"][0]["step_execution_id"]
    )


def test_crash_running_journal_recovers_same_attempt_and_claim(
    tmp_path: Path,
) -> None:
    crashed = _crash_running(tmp_path)
    before = crashed["operations"].get_operation(crashed["operation_id"])
    assert before is not None
    assert before["operation"]["status"] == "running"
    assert [item["status"] for item in before["attempts"]] == ["dispatched"]
    assert before["receipts"] == []
    detail = _detail(crashed["work"], str(crashed["context"]["work_item_id"]))
    assert detail is not None
    assert detail["work_item"]["status"] == "unassigned"
    assert _claim_state(detail) == "pending_gate"
    spy = _Count(crashed["operations"])
    recovered = activate_mod.ClaimActivator(
        execution=crashed["execution"], work=crashed["work"], operations=spy,
    ).activate(crashed["context"], crashed["reserved"], idempotency_key=KEY)
    assert spy.prepare == 0
    assert spy.dispatch == 0
    assert recovered["lease"]["status"] == "active"
    assert recovered["claim"]["state"] == "active"
    assert recovered["work_item"]["status"] == "working"
    assert recovered["root_message"]["body"] == SENTINEL
    after = crashed["operations"].get_operation(crashed["operation_id"])
    assert after is not None
    assert after["operation"]["status"] == "succeeded"
    assert len(after["attempts"]) == 1
    attempt = after["attempts"][0]
    assert attempt["step_execution_id"] == crashed["step_execution_id"]
    assert attempt["attempt_no"] == 1
    assert attempt["status"] == "succeeded"
    assert len(after["receipts"]) == 1
    assert after["receipts"][0]["step_execution_id"] == crashed["step_execution_id"]
    assert after["receipts"][0]["outcome"] == "succeeded"
    replay = activate_mod.ClaimActivator(
        execution=crashed["execution"], work=crashed["work"], operations=spy,
    ).activate(crashed["context"], crashed["reserved"], idempotency_key=KEY)
    assert spy.prepare == 0
    assert spy.dispatch == 0
    assert replay["claim"]["claim_id"] == recovered["claim"]["claim_id"]
    again = crashed["operations"].get_operation(crashed["operation_id"])
    assert again is not None
    assert again["receipts"][0]["receipt_id"] == after["receipts"][0]["receipt_id"]
    assert len(again["attempts"]) == 1
    assert len(again["receipts"]) == 1


def test_crash_after_receipt_reuses_same_receipt(tmp_path: Path) -> None:
    crashed = _crash_running(tmp_path, record_outcome=True)
    before = crashed["operations"].get_operation(crashed["operation_id"])
    assert before is not None
    assert before["operation"]["status"] == "running"
    assert before["attempts"][0]["status"] == "succeeded"
    assert before["receipts"][0]["receipt_id"] == crashed["receipt_id"]
    spy = _Count(crashed["operations"])
    recovered = activate_mod.ClaimActivator(
        execution=crashed["execution"], work=crashed["work"], operations=spy,
    ).activate(crashed["context"], crashed["reserved"], idempotency_key=KEY)
    assert spy.prepare == 0
    assert spy.dispatch == 0
    assert recovered["claim"]["state"] == "active"
    after = crashed["operations"].get_operation(crashed["operation_id"])
    assert after is not None
    assert after["operation"]["status"] == "succeeded"
    assert [item["receipt_id"] for item in after["receipts"]] == [
        crashed["receipt_id"]
    ]
    assert after["attempts"][0]["step_execution_id"] == crashed["step_execution_id"]


def test_unknown_outcome_fails_closed_without_redispatch(tmp_path: Path) -> None:
    crashed = _crash_running(tmp_path)
    crashed["operations"].record_attempt_outcome(
        crashed["operation_id"], crashed["step_execution_id"],
        expected_operation_revision=crashed["revision"] + 3,
        expected_step_revision=2,
        receipt_id="rcp_" + "cd" * 16,
        receipt_type="provider_response_lost",
        outcome="outcome_unknown",
        evidence_kind="opaque_digest",
        evidence_digest=activate_mod._sha({"lost": True}),
    )
    spy = _Count(crashed["operations"])
    with pytest.raises(activate_mod.ClaimActivationError) as error:
        activate_mod.ClaimActivator(
            execution=crashed["execution"], work=crashed["work"], operations=spy,
        ).activate(crashed["context"], crashed["reserved"], idempotency_key=KEY)
    assert error.value.code == "operation_journal_unavailable"
    assert spy.prepare == 0
    assert spy.dispatch == 0
    stuck = crashed["operations"].get_operation(crashed["operation_id"])
    assert stuck is not None
    assert stuck["operation"]["status"] == "needs_attention"
    assert [item["status"] for item in stuck["attempts"]] == ["outcome_unknown"]
    assert len(stuck["attempts"]) == 1
    detail = _detail(crashed["work"], str(crashed["context"]["work_item_id"]))
    assert detail is not None
    assert detail["work_item"]["status"] == "unassigned"
    assert _claim_state(detail) == "pending_gate"


def test_stale_cas_fails_closed_without_redispatch(tmp_path: Path) -> None:
    crashed = _crash_running(tmp_path)
    spy = _Count(_StaleRevision(crashed["operations"]))
    with pytest.raises(activate_mod.ClaimActivationError) as error:
        activate_mod.ClaimActivator(
            execution=crashed["execution"], work=crashed["work"], operations=spy,
        ).activate(crashed["context"], crashed["reserved"], idempotency_key=KEY)
    assert error.value.code == "operation_journal_unavailable"
    assert spy.prepare == 0
    assert spy.dispatch == 0
    stuck = crashed["operations"].get_operation(crashed["operation_id"])
    assert stuck is not None
    assert stuck["operation"]["status"] == "running"
    assert [item["status"] for item in stuck["attempts"]] == ["dispatched"]
    assert stuck["receipts"] == []
    detail = _detail(crashed["work"], str(crashed["context"]["work_item_id"]))
    assert detail is not None
    assert detail["work_item"]["status"] == "unassigned"
    assert _claim_state(detail) == "pending_gate"
