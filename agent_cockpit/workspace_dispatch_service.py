"""Dispatch one prepared WorkItem through a fixed, body-free wakeup."""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from . import local_codex_harness as harness_mod
from . import operation_store as operation_mod
from . import workspace_execution_store as execution_mod
from . import workspace_work_store as work_mod


class DispatchError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise DispatchError(code)


def _sha(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass
class DispatchService:
    registry_provider: Callable[[], Any]
    work_provider: Callable[[], work_mod.WorkspaceWorkStore]
    execution: execution_mod.WorkspaceExecutionStore
    operations: operation_mod.OperationStore
    harness: Any
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def dispatch(
        self, project_id: str, workspace_id: str, work_item_id: str, *,
        expected_work_revision: object, expected_preparation_revision: object,
        idempotency_key: object,
    ) -> dict[str, object]:
        if (
            type(expected_work_revision) is not int
            or expected_work_revision < 1
            or type(expected_preparation_revision) is not int
            or expected_preparation_revision < 1
            or not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= 128
            or any(ord(char) < 33 or ord(char) == 127 for char in idempotency_key)
        ):
            _fail("invalid_argument")
        with self._lock:
            return self._dispatch_locked(
                project_id, workspace_id, work_item_id,
                expected_work_revision=expected_work_revision,
                expected_preparation_revision=expected_preparation_revision,
                idempotency_key=idempotency_key,
            )

    def _dispatch_locked(
        self, project_id: str, workspace_id: str, work_item_id: str, *,
        expected_work_revision: int, expected_preparation_revision: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        self._require_scope(project_id, workspace_id)
        work = self.work_provider()
        detail = work.get_work_item_detail(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        )
        if detail is None:
            _fail("work_item_not_found")
        item = detail["work_item"]
        if not isinstance(item, dict) or item.get("status") != "unassigned":
            _fail("execution_terminal")
        preparation = self.execution.get_preparation(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        )
        if preparation is None:
            _fail("preparation_not_found")
        if preparation.revision != expected_preparation_revision:
            _fail("stale_revision")
        if preparation.state != "connected_readonly":
            _fail("runtime_unavailable")
        attachment = preparation.attachment
        lease = preparation.lease
        if (
            attachment is None
            or attachment.status != "connected_readonly"
            or attachment.identity_verified is not True
            or lease is None
            or lease.status != "reserved"
            or attachment.generation != lease.generation
            or attachment.generation != preparation.principal.get("generation")
        ):
            _fail("runtime_unavailable")
        request = {
            "expected_preparation_revision": expected_preparation_revision,
            "expected_work_revision": expected_work_revision,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "work_item_id": work_item_id,
        }
        intent_digest = _sha({"kind": "dispatch.intent", **request})
        intent = work.record_delivery(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id, expected_revision=expected_work_revision,
            outcome="intent", evidence_digest=intent_digest,
            idempotency_key=idempotency_key,
        )
        try:
            created = self.operations.create_operation(
                scope="workspace-work.dispatch.v1",
                idempotency_key=idempotency_key, request=request,
                kind="workspace.dispatch", project_id=project_id,
                workspace_id=workspace_id, subject_type="work_item",
                subject_id=work_item_id,
                plan_digest=_sha({"kind": "workspace.dispatch", **request}),
                approval_required=False,
                preconditions=(
                    operation_mod.Precondition(
                        "work.revision", "work_item", work_item_id,
                        expected_revision=expected_work_revision,
                    ),
                    operation_mod.Precondition(
                        "preparation.revision", "work_item", work_item_id,
                        expected_revision=expected_preparation_revision,
                    ),
                ),
                steps=(operation_mod.Step("fixed-wakeup", "runtime.wakeup"),),
            )
        except operation_mod.OperationError as exc:
            if exc.code == "idempotency_conflict":
                _fail("idempotency_conflict")
            _fail("operation_journal_unavailable")
        return self._run_operation(
            created.projection, created.operation_id, attachment.attachment_id,
            work, project_id, workspace_id, work_item_id,
            int(intent["revision"]), idempotency_key,
        )

    def _run_operation(
        self, projection: dict[str, object], operation_id: str,
        attachment_id: str, work: work_mod.WorkspaceWorkStore,
        project_id: str, workspace_id: str, work_item_id: str,
        final_expected_revision: int, idempotency_key: str,
    ) -> dict[str, object]:
        operation = projection["operation"]
        assert isinstance(operation, dict)
        status = operation["status"]
        attempts = projection["attempts"]
        assert isinstance(attempts, list)
        if status == "succeeded":
            return self._finish(
                work, project_id, workspace_id, work_item_id,
                final_expected_revision, idempotency_key, operation_id,
                attachment_id, "succeeded", harness_mod.WAKEUP_DIGEST,
            )
        if status == "needs_attention" or any(
            item.get("status") == "outcome_unknown"
            for item in attempts if isinstance(item, dict)
        ):
            self._finish_unknown(
                work, project_id, workspace_id, work_item_id,
                final_expected_revision, idempotency_key, operation_id,
                attachment_id,
            )
        if status == "failed":
            code = operation.get("failure_code")
            _fail(code if isinstance(code, str) else "runtime_unavailable")
        revision = int(operation["revision"])
        try:
            if status == "planned":
                projection = self.operations.transition(
                    operation_id, expected_operation_revision=revision,
                    status="running",
                )
                revision = int(projection["operation"]["revision"])
            attempts = projection["attempts"]
            if not attempts:
                prepared = self.operations.prepare_attempt(
                    operation_id, "fixed-wakeup",
                    expected_operation_revision=revision,
                    expected_step_revision=1, mode="execute",
                    provider_kind="local_herdr",
                )
                projection = prepared.projection
                revision = int(projection["operation"]["revision"])
                execution_id = prepared.step_execution_id
                step_revision = 2
            else:
                attempt = attempts[-1]
                assert isinstance(attempt, dict)
                execution_id = str(attempt["step_execution_id"])
                step = projection["steps"][0]
                assert isinstance(step, dict)
                step_revision = int(step["revision"])
                if attempt["status"] == "dispatched":
                    self._record_unknown(
                        operation_id, execution_id, revision, step_revision,
                        attachment_id,
                    )
                    self._finish_unknown(
                        work, project_id, workspace_id, work_item_id,
                        final_expected_revision, idempotency_key, operation_id,
                        attachment_id,
                    )
                if attempt["status"] == "succeeded":
                    projection = self.operations.transition(
                        operation_id, expected_operation_revision=revision,
                        status="succeeded",
                    )
                    return self._finish(
                        work, project_id, workspace_id, work_item_id,
                        final_expected_revision, idempotency_key, operation_id,
                        attachment_id, "succeeded", harness_mod.WAKEUP_DIGEST,
                    )
            projection = self.operations.dispatch_attempt(
                operation_id, execution_id,
                expected_operation_revision=revision,
                provider_operation_ref=attachment_id,
            )
            revision = int(projection["operation"]["revision"])
        except operation_mod.OperationError:
            _fail("operation_journal_unavailable")
        try:
            self.harness.wakeup(attachment_id)
        except harness_mod.HarnessError as exc:
            if exc.unknown:
                self._record_unknown(
                    operation_id, execution_id, revision, step_revision,
                    attachment_id,
                )
                self._finish_unknown(
                    work, project_id, workspace_id, work_item_id,
                    final_expected_revision, idempotency_key, operation_id,
                    attachment_id,
                )
            self._record_failed(
                operation_id, execution_id, revision, step_revision,
                attachment_id, exc.code,
            )
            _fail(exc.code)
        except Exception:
            self._record_unknown(
                operation_id, execution_id, revision, step_revision,
                attachment_id,
            )
            self._finish_unknown(
                work, project_id, workspace_id, work_item_id,
                final_expected_revision, idempotency_key, operation_id,
                attachment_id,
            )
        try:
            projection = self.operations.record_attempt_outcome(
                operation_id, execution_id,
                expected_operation_revision=revision,
                expected_step_revision=step_revision,
                receipt_id="rcp_" + secrets.token_hex(16),
                receipt_type="provider_outcome", outcome="succeeded",
                evidence_kind="opaque_digest",
                evidence_digest=harness_mod.WAKEUP_DIGEST,
            )
            revision = int(projection["operation"]["revision"])
            self.operations.transition(
                operation_id, expected_operation_revision=revision,
                status="succeeded",
            )
        except operation_mod.OperationError:
            self._finish_unknown(
                work, project_id, workspace_id, work_item_id,
                final_expected_revision, idempotency_key, operation_id,
                attachment_id,
            )
        return self._finish(
            work, project_id, workspace_id, work_item_id,
            final_expected_revision, idempotency_key, operation_id,
            attachment_id, "succeeded", harness_mod.WAKEUP_DIGEST,
        )

    def _record_unknown(
        self, operation_id: str, execution_id: str, operation_revision: int,
        step_revision: int, attachment_id: str,
    ) -> None:
        try:
            self.operations.record_attempt_outcome(
                operation_id, execution_id,
                expected_operation_revision=operation_revision,
                expected_step_revision=step_revision,
                receipt_id="rcp_" + secrets.token_hex(16),
                receipt_type="provider_response_lost", outcome="outcome_unknown",
                evidence_kind="opaque_digest",
                evidence_digest=_sha({
                    "attachment_id": attachment_id,
                    "operation_id": operation_id,
                    "outcome": "unknown",
                }),
            )
        except operation_mod.OperationError:
            pass

    def _record_failed(
        self, operation_id: str, execution_id: str, operation_revision: int,
        step_revision: int, attachment_id: str, code: str,
    ) -> None:
        try:
            self.operations.record_attempt_outcome(
                operation_id, execution_id,
                expected_operation_revision=operation_revision,
                expected_step_revision=step_revision,
                receipt_id="rcp_" + secrets.token_hex(16),
                receipt_type="provider_outcome", outcome="failed",
                evidence_kind="opaque_digest",
                evidence_digest=_sha({
                    "attachment_id": attachment_id,
                    "code": code, "operation_id": operation_id,
                }), failure_code=code,
            )
        except operation_mod.OperationError:
            pass

    def _finish_unknown(
        self, work: work_mod.WorkspaceWorkStore, project_id: str,
        workspace_id: str, work_item_id: str, expected_revision: int,
        idempotency_key: str, operation_id: str, attachment_id: str,
    ) -> None:
        self._finish(
            work, project_id, workspace_id, work_item_id, expected_revision,
            idempotency_key, operation_id, attachment_id, "outcome_unknown",
            _sha({
                "attachment_id": attachment_id,
                "operation_id": operation_id,
                "outcome": "unknown",
            }),
        )
        _fail("wakeup_outcome_unknown")

    @staticmethod
    def _finish(
        work: work_mod.WorkspaceWorkStore, project_id: str,
        workspace_id: str, work_item_id: str, expected_revision: int,
        idempotency_key: str, operation_id: str, attachment_id: str,
        outcome: str, evidence_digest: str,
    ) -> dict[str, object]:
        receipt = work.record_delivery(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id, expected_revision=expected_revision,
            outcome=outcome, evidence_digest=evidence_digest,
            idempotency_key=idempotency_key,
        )
        return {
            "operation_id": operation_id,
            "work_item_id": work_item_id,
            "attachment_id": attachment_id,
            "outcome": outcome,
            "work_revision": receipt["revision"],
        }

    def _require_scope(self, project_id: str, workspace_id: str) -> None:
        registry = self.registry_provider()
        snapshot = registry.get_project_by_id(project_id)
        if snapshot is None:
            _fail("project_not_found")
        project = getattr(snapshot, "project", None)
        workspace = registry.get_workspace(project_id, workspace_id)
        if (
            project is None
            or workspace is None
            or getattr(workspace, "project_id", None) != project_id
            or getattr(workspace, "workspace_id", None) != workspace_id
        ):
            _fail("workspace_not_found")
        if (
            getattr(project, "lifecycle", None) != "active"
            or getattr(workspace, "lifecycle", None) != "active"
        ):
            _fail("workspace_not_active")
