"""Activate a reserved writer lease and the matching C1 pending claim."""
from __future__ import annotations

import hashlib
import json
import secrets

from . import operation_store as operation_mod
from . import workspace_execution_store as exec_mod
from . import workspace_work_store as work_mod


class ClaimActivationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise ClaimActivationError(code)


def _sha(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _map(exc: BaseException) -> str:
    code = getattr(exc, "code", "")
    if code in {
        "invalid_argument", "idempotency_conflict", "stale_revision",
        "stale_generation", "claim_not_active", "lease_not_active",
        "preparation_not_found", "work_item_not_found", "execution_terminal",
        "reconcile_required", "runtime_identity_unverified", "claim_conflict",
    }:
        return code
    if code.startswith("schema_") or code.startswith("store_"):
        return code
    return "operation_journal_unavailable"


def _recover_journal(
    projection: dict[str, object],
) -> tuple[str | None, int | None, int | None, str | None]:
    operation = projection.get("operation")
    attempts = projection.get("attempts")
    receipts = projection.get("receipts")
    steps = projection.get("steps")
    if not isinstance(operation, dict):
        _fail("operation_journal_unavailable")
    status = operation.get("status")
    if status == "succeeded":
        return None, None, None, None
    if (
        status != "running"
        or not isinstance(attempts, list)
        or not isinstance(receipts, list)
        or not isinstance(steps, list)
        or len(attempts) != 1
        or not isinstance(attempts[0], dict)
    ):
        _fail("operation_journal_unavailable")
    attempt = attempts[0]
    execution_id = attempt.get("step_execution_id")
    op_revision = operation.get("revision")
    if (
        attempt.get("step_id") != "activate-lease"
        or attempt.get("mode") != "execute"
        or not isinstance(execution_id, str)
        or execution_id == ""
        or type(op_revision) is not int
    ):
        _fail("operation_journal_unavailable")
    step = None
    for item in steps:
        if isinstance(item, dict) and item.get("step_id") == "activate-lease":
            if step is not None:
                _fail("operation_journal_unavailable")
            step = item
    if step is None or type(step.get("revision")) is not int:
        _fail("operation_journal_unavailable")
    matching = [
        item for item in receipts
        if isinstance(item, dict) and item.get("step_execution_id") == execution_id
    ]
    attempt_status = attempt.get("status")
    if attempt_status == "dispatched":
        if matching:
            _fail("operation_journal_unavailable")
        return execution_id, op_revision, step["revision"], None
    if attempt_status == "succeeded" and len(matching) == 1:
        receipt = matching[0]
        receipt_id = receipt.get("receipt_id")
        if (
            isinstance(receipt_id, str)
            and receipt_id != ""
            and receipt.get("outcome") == "succeeded"
            and receipt.get("receipt_type") == "provider_outcome"
        ):
            return execution_id, op_revision, step["revision"], receipt_id
    _fail("operation_journal_unavailable")


class ClaimActivator:
    def __init__(self, *, execution, work, operations) -> None:
        self.execution = execution
        self.work = work
        self.operations = operations

    def activate(
        self, context: dict[str, object], pending_claim: dict[str, object],
        *, idempotency_key: str,
    ) -> dict[str, object]:
        try:
            project_id = str(context["project_id"])
            workspace_id = str(context["workspace_id"])
            work_item_id = str(context["work_item_id"])
            claim = pending_claim.get("claim", pending_claim)
            claim_id = str(claim["claim_id"])
            request = {
                "attachment_id": context["attachment_id"],
                "claim_id": claim_id,
                "generation": context["generation"],
                "identity_id": context["identity_id"],
                "work_item_id": work_item_id,
            }
            created = self.operations.create_operation(
                scope="workspace-claim-activate.v1",
                idempotency_key=idempotency_key,
                request=request,
                kind="claim.activate",
                subject_type="lease",
                subject_id=claim_id,
                plan_digest=_sha(request),
                approval_required=False,
                project_id=project_id,
                workspace_id=workspace_id,
                steps=(operation_mod.Step("activate-lease", "claim.activate"),),
            )
        except (KeyError, TypeError, ValueError):
            _fail("invalid_argument")
        except operation_mod.OperationError:
            _fail("operation_journal_unavailable")
        operation_id = created.operation_id
        revision = int(created.projection["operation"]["revision"])
        try:
            if created.projection["operation"]["status"] == "planned":
                self.operations.transition(
                    operation_id, expected_operation_revision=revision,
                    status="running",
                )
                prepared = self.operations.prepare_attempt(
                    operation_id, "activate-lease",
                    expected_operation_revision=revision + 1,
                    expected_step_revision=1, mode="execute",
                    provider_kind="execution_store",
                )
                self.operations.dispatch_attempt(
                    operation_id, prepared.step_execution_id,
                    expected_operation_revision=revision + 2,
                )
                step_execution_id = prepared.step_execution_id
                outcome_revision = revision + 3
                step_revision = 2
                receipt_id = None
            else:
                step_execution_id, outcome_revision, step_revision, receipt_id = (
                    _recover_journal(created.projection)
                )
            lease = self.execution.activate_claim_lease(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
                expected_preparation_revision=context["expected_preparation_revision"],
                expected_lease_revision=context["expected_lease_revision"],
                attachment_id=context["attachment_id"],
                identity_id=context["identity_id"],
                generation=context["generation"],
                claim_id=claim_id,
                idempotency_key=idempotency_key + ":lease",
            )
            if step_execution_id is not None:
                if receipt_id is None:
                    receipt_id = "rcp_" + secrets.token_hex(16)
                self.operations.record_attempt_outcome(
                    operation_id, step_execution_id,
                    expected_operation_revision=outcome_revision,
                    expected_step_revision=step_revision,
                    receipt_id=receipt_id,
                    receipt_type="provider_outcome",
                    outcome="succeeded",
                    evidence_kind="opaque_digest",
                    evidence_digest=_sha({"lease": lease["lease"]}),
                )
                current = self.operations.get_operation(operation_id)
                if current is None:
                    _fail("operation_journal_unavailable")
                status = current["operation"]["status"]
                if status == "running":
                    self.operations.transition(
                        operation_id,
                        expected_operation_revision=int(
                            current["operation"]["revision"]
                        ),
                        status="succeeded",
                    )
                elif status != "succeeded":
                    _fail("operation_journal_unavailable")
        except (exec_mod.WorkspaceExecutionError, operation_mod.OperationError) as exc:
            _fail(_map(exc))
        try:
            activated = self.work.activate_claim(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, claim_id=claim_id,
                identity_id=context["identity_id"],
                generation=context["generation"],
                expected_claim_revision=claim["revision"],
                expected_work_revision=context["expected_work_revision"],
                idempotency_key=idempotency_key + ":claim",
            )
        except work_mod.WorkspaceWorkError as exc:
            _fail(_map(exc))
        return {
            "lease": lease["lease"],
            "claim": activated["claim"],
            "work_item": activated["work_item"],
            "root_message": activated["root_message"],
        }
