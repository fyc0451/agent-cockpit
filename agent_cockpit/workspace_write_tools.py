"""MCP checkout writes and Handoff publication. IDs come from capability."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from . import local_codex_harness as harness_mod
from . import operation_store as operation_mod
from . import workspace_execution_store as exec_mod
from . import workspace_delivery_service as delivery_service_mod
from . import workspace_work_store as work_mod
from .workspace_write_gate import WorkspaceWriteGate, WriteGateError


MAX_PATCH_BYTES = 262144
MAX_PATCH_FILES = 16
_DIFF_GIT = re.compile(r"^diff --git a/(.+) b/(.+)$")
_PLUS_FILE = re.compile(r"^\+\+\+ (?:b/)?(.+)$")
_MINUS_FILE = re.compile(r"^--- (?:a/)?(.+)$")
_FORBIDDEN = (
    "rename from ", "rename to ", "copy from ", "copy to ",
    "GIT binary patch", "new file mode 120000", "new file mode 160000",
    "old mode 120000",
)


class WriteToolError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise WriteToolError(code)


def _sha(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _map(exc: BaseException) -> str:
    code = getattr(exc, "code", "")
    if code in {
        "invalid_argument", "patch_invalid", "runtime_capability_invalid",
        "work_item_not_found", "preparation_not_found", "idempotency_conflict",
        "stale_revision", "stale_generation", "claim_not_active",
        "lease_not_active", "fence_rejected", "checkout_untrusted",
        "reply_conflict", "execution_terminal", "reconcile_required",
        "operation_journal_unavailable", "patch_outcome_unknown",
        "claim_conflict",
        "path_outside_allowed_scope",
        "review_authority_unavailable", "handoff_conflict",
        "review_authority_required",
        "checkout_changed", "handoff_digest_mismatch",
        "handoff_outcome_unknown",
        "invalid_changed_path", "git_unavailable", "git_command_failed",
    }:
        return code
    if isinstance(code, str) and (
        code.startswith("schema_") or code.startswith("store_")
    ):
        return code
    return "operation_journal_unavailable"


def _capability(path: Path) -> dict[str, object]:
    try:
        return harness_mod._read_capability(path)
    except harness_mod.HarnessError:
        _fail("runtime_capability_invalid")
    raise AssertionError("unreachable")


def _validate_patch(patch: object) -> tuple[bytes, tuple[str, ...]]:
    if not isinstance(patch, str):
        _fail("patch_invalid")
    if "\x00" in patch:
        _fail("patch_invalid")
    raw = patch.encode("utf-8")
    if not raw or len(raw) > MAX_PATCH_BYTES:
        _fail("patch_invalid")
    files: list[str] = []
    for line in patch.splitlines():
        lowered = line.lower()
        if any(token in line or token in lowered for token in _FORBIDDEN):
            _fail("patch_invalid")
        match = _DIFF_GIT.match(line) or _PLUS_FILE.match(line) or _MINUS_FILE.match(line)
        if match is None:
            continue
        for path in match.groups():
            if path in {"/dev/null", "dev/null"}:
                continue
            if path.startswith("/") or path.startswith("\\") or ":" in path[:2]:
                _fail("patch_invalid")
            parts = Path(path).parts
            if ".." in parts or parts[:1] == ("/",) or not parts:
                _fail("patch_invalid")
            files.append(path)
    unique = []
    for path in files:
        if path not in unique:
            unique.append(path)
    if not unique or len(unique) > MAX_PATCH_FILES:
        _fail("patch_invalid")
    return raw, tuple(unique)


def _git_apply(checkout: Path, patch: bytes, *, check: bool) -> None:
    git = shutil.which("git")
    if git is None:
        _fail("patch_invalid")
    args = [git, "-C", str(checkout), "apply", "--whitespace=nowarn"]
    if check:
        args.append("--check")
    try:
        completed = subprocess.run(
            args, input=patch, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=15,
        )
    except subprocess.TimeoutExpired:
        _fail("patch_outcome_unknown")
    except OSError:
        _fail("patch_invalid")
    if completed.returncode != 0:
        _fail("patch_invalid")


class WorkspaceWriteTools:
    def __init__(
        self, *, execution, work, operations, gate=None, delivery_service=None,
    ) -> None:
        self.execution = execution
        self.work = work
        self.operations = operations
        self.gate = gate if gate is not None else WorkspaceWriteGate()
        self.delivery_service = delivery_service

    def apply_patch(
        self, *, capability_path: Path, claim_revision: object,
        lease_revision: object, patch: object, idempotency_key: str,
    ) -> dict[str, object]:
        record = _capability(capability_path)
        raw, changed_paths = _validate_patch(patch)
        if type(claim_revision) is not int or type(lease_revision) is not int:
            _fail("invalid_argument")
        attachment_id = str(record["attachment_id"])
        identity_id = str(record["identity_id"])
        generation = int(record["generation"])
        fence = str(record["fence"])
        prep = self.execution.find_preparation_for_attachment(
            attachment_id=attachment_id,
        )
        if prep is None:
            _fail("preparation_not_found")
        work_item_id = prep.work_item_id
        identity = prep.identity.identity_id
        try:
            connection_ids = self.execution.lookup_attachment_scope(
                attachment_id=attachment_id,
            )
        except exec_mod.WorkspaceExecutionError as exc:
            _fail(_map(exc))
        checkout = Path(self.execution.checkout_internal_path(
            project_id=connection_ids[0], workspace_id=connection_ids[1],
            work_item_id=work_item_id,
        ))
        try:
            allowed = self.work.get_allowed_paths(
                project_id=connection_ids[0], workspace_id=connection_ids[1],
                work_item_id=work_item_id,
            )
        except work_mod.WorkspaceWorkError as exc:
            _fail(_map(exc))
        if allowed is not None and any(
            not work_mod.path_is_allowed(path, allowed) for path in changed_paths
        ):
            _fail("path_outside_allowed_scope")
        try:
            self.gate.authorize(
                attachment_id=attachment_id, identity_id=identity_id,
                generation=generation, fence=fence,
                capability_path=capability_path, checkout_path=checkout,
                execution=self.execution, project_id=connection_ids[0],
                workspace_id=connection_ids[1], work_item_id=work_item_id,
                expected_lease_revision=lease_revision,
                claim_id=None if prep.lease is None else prep.lease.claim_id,
            )
        except WriteGateError as exc:
            _fail(_map(exc))
        if identity != identity_id:
            _fail("runtime_capability_invalid")
        if prep.lease is None or prep.lease.claim_id is None:
            _fail("lease_not_active")
        claim_id = prep.lease.claim_id
        request = {
            "attachment_id": attachment_id,
            "claim_id": claim_id,
            "digest": hashlib.sha256(raw).hexdigest(),
        }
        try:
            created = self.operations.create_operation(
                scope="workspace-apply-patch.v1",
                idempotency_key=idempotency_key,
                request=request,
                kind="checkout.patch",
                subject_type="lease",
                subject_id=prep.lease.lease_id,
                plan_digest=_sha(request),
                approval_required=False,
                project_id=connection_ids[0],
                workspace_id=connection_ids[1],
                steps=(operation_mod.Step("apply-patch", "checkout.patch"),),
            )
        except operation_mod.OperationError:
            _fail("operation_journal_unavailable")
        operation_id = created.operation_id
        digest = created.request_digest
        revision = int(created.projection["operation"]["revision"])
        try:
            if created.projection["operation"]["status"] == "planned":
                self.operations.transition(
                    operation_id, expected_operation_revision=revision,
                    status="running",
                )
                prepared = self.operations.prepare_attempt(
                    operation_id, "apply-patch",
                    expected_operation_revision=revision + 1,
                    expected_step_revision=1, mode="execute",
                    provider_kind="git",
                )
                self.operations.dispatch_attempt(
                    operation_id, prepared.step_execution_id,
                    expected_operation_revision=revision + 2,
                )
                step_execution_id = prepared.step_execution_id
                outcome_revision = revision + 3
            else:
                step_execution_id = None
                outcome_revision = revision
            self.execution.begin_tool(
                project_id=connection_ids[0], workspace_id=connection_ids[1],
                work_item_id=work_item_id,
                expected_preparation_revision=prep.revision,
                expected_lease_revision=lease_revision,
                attachment_id=attachment_id, identity_id=identity_id,
                generation=generation, claim_id=claim_id,
                operation_id=operation_id, operation_digest=digest,
                idempotency_key=idempotency_key + ":begin",
            )
        except (exec_mod.WorkspaceExecutionError, operation_mod.OperationError) as exc:
            _fail(_map(exc))
        try:
            _git_apply(checkout, raw, check=True)
            _git_apply(checkout, raw, check=False)
            outcome = "succeeded"
            receipt = "provider_outcome"
        except WriteToolError as exc:
            if exc.code == "patch_outcome_unknown":
                outcome = "outcome_unknown"
                receipt = "provider_response_lost"
            else:
                outcome = "failed"
                receipt = "provider_outcome"
            try:
                if step_execution_id is not None:
                    self.operations.record_attempt_outcome(
                        operation_id, step_execution_id,
                        expected_operation_revision=outcome_revision,
                        expected_step_revision=2,
                        receipt_id="rcp_" + secrets.token_hex(16),
                        receipt_type=receipt,
                        outcome=outcome,
                        evidence_kind="opaque_digest",
                        evidence_digest=_sha({"code": exc.code}),
                        failure_code=None if outcome != "failed" else exc.code,
                    )
                self.execution.finish_tool(
                    project_id=connection_ids[0], workspace_id=connection_ids[1],
                    work_item_id=work_item_id,
                    expected_preparation_revision=prep.revision,
                    expected_lease_revision=lease_revision + 1,
                    attachment_id=attachment_id, identity_id=identity_id,
                    generation=generation, claim_id=claim_id,
                    operation_id=operation_id, outcome=outcome,
                    idempotency_key=idempotency_key + ":finish",
                )
            except (exec_mod.WorkspaceExecutionError, operation_mod.OperationError):
                if outcome == "outcome_unknown":
                    _fail("patch_outcome_unknown")
                _fail("reconcile_required")
            if outcome == "outcome_unknown":
                _fail("patch_outcome_unknown")
            _fail(exc.code)
        try:
            if step_execution_id is not None:
                self.operations.record_attempt_outcome(
                    operation_id, step_execution_id,
                    expected_operation_revision=outcome_revision,
                    expected_step_revision=2,
                    receipt_id="rcp_" + secrets.token_hex(16),
                    receipt_type="provider_outcome",
                    outcome="succeeded",
                    evidence_kind="opaque_digest",
                    evidence_digest=_sha({"digest": hashlib.sha256(raw).hexdigest()}),
                )
                self.operations.transition(
                    operation_id, expected_operation_revision=outcome_revision + 1,
                    status="succeeded",
                )
            finished = self.execution.finish_tool(
                project_id=connection_ids[0], workspace_id=connection_ids[1],
                work_item_id=work_item_id,
                expected_preparation_revision=prep.revision,
                expected_lease_revision=lease_revision + 1,
                attachment_id=attachment_id, identity_id=identity_id,
                generation=generation, claim_id=claim_id,
                operation_id=operation_id, outcome="succeeded",
                idempotency_key=idempotency_key + ":finish",
            )
        except (exec_mod.WorkspaceExecutionError, operation_mod.OperationError) as exc:
            _fail(_map(exc))
        return {"lease": finished["lease"], "applied": True}

    def reply_complete(
        self, *, capability_path: Path, claim_revision: object,
        lease_revision: object, body: object, idempotency_key: str,
    ) -> dict[str, object]:
        record = _capability(capability_path)
        if type(claim_revision) is not int or type(lease_revision) is not int:
            _fail("invalid_argument")
        attachment_id = str(record["attachment_id"])
        identity_id = str(record["identity_id"])
        generation = int(record["generation"])
        fence = str(record["fence"])
        prep = self.execution.find_preparation_for_attachment(
            attachment_id=attachment_id,
        )
        if prep is None or prep.lease is None or prep.lease.claim_id is None:
            _fail("lease_not_active")
        try:
            project_id, workspace_id, work_item_id = (
                self.execution.lookup_attachment_scope(attachment_id=attachment_id)
            )
        except exec_mod.WorkspaceExecutionError as exc:
            _fail(_map(exc))
        try:
            if self.work.get_allowed_paths(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            ) is not None:
                _fail("review_authority_required")
        except work_mod.WorkspaceWorkError as exc:
            _fail(_map(exc))
        checkout = Path(self.execution.checkout_internal_path(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        ))
        claim_id = prep.lease.claim_id
        if prep.lease.status not in {"revoking", "revoked"}:
            try:
                self.gate.authorize(
                    attachment_id=attachment_id, identity_id=identity_id,
                    generation=generation, fence=fence,
                    capability_path=capability_path, checkout_path=checkout,
                    execution=self.execution, project_id=project_id,
                    workspace_id=workspace_id, work_item_id=work_item_id,
                    expected_lease_revision=lease_revision,
                    claim_id=claim_id,
                )
            except WriteGateError as exc:
                _fail(_map(exc))
        if prep.lease.status == "revoked":
            detail = self.work.get_work_item_detail(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            if detail is None or detail["claim"] is None:
                _fail("reply_conflict")
            reply = next(
                (
                    message for message in detail["thread"]["messages"]
                    if message["message_kind"] == "reply"
                ),
                None,
            )
            if reply is None:
                _fail("reply_conflict")
            return {
                "lease": prep.lease.public_dict(),
                "claim": detail["claim"],
                "work_item": detail["work_item"],
                "reply_message": {
                    key: reply[key]
                    for key in (
                        "message_id", "ordinal", "message_kind", "author_kind", "body",
                    )
                },
            }
        try:
            begun = self.execution.begin_reply(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
                expected_preparation_revision=prep.revision,
                expected_lease_revision=lease_revision,
                attachment_id=attachment_id, identity_id=identity_id,
                generation=generation, claim_id=claim_id,
                idempotency_key=idempotency_key + ":begin",
            )
            begun_revision = begun["lease"]["revision"]
        except exec_mod.WorkspaceExecutionError as exc:
            _fail(_map(exc))
        try:
            replied = self.work.reply_complete(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, claim_id=claim_id,
                identity_id=identity_id, generation=generation,
                expected_claim_revision=claim_revision,
                expected_work_revision=self._work_revision(
                    project_id, workspace_id, work_item_id,
                ),
                body=body, idempotency_key=idempotency_key + ":reply",
            )
        except work_mod.WorkspaceWorkError as exc:
            _fail(_map(exc) if exc.code != "claim_conflict" else "reply_conflict")
        try:
            finished = self.execution.finish_reply(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
                expected_preparation_revision=prep.revision,
                expected_lease_revision=begun_revision,
                attachment_id=attachment_id, identity_id=identity_id,
                generation=generation, claim_id=claim_id,
                idempotency_key=idempotency_key + ":finish",
            )
        except exec_mod.WorkspaceExecutionError as exc:
            _fail(_map(exc))
        return {
            "lease": finished["lease"],
            "claim": replied["claim"],
            "work_item": replied["work_item"],
            "reply_message": {
                key: replied["reply_message"][key]
                for key in (
                    "message_id", "ordinal", "message_kind", "author_kind", "body",
                )
            },
        }

    def submit_handoff(
        self, *, capability_path: Path, claim_revision: object,
        lease_revision: object, summary: object, test_evidence: object,
        idempotency_key: str,
    ) -> dict[str, object]:
        if self.delivery_service is None:
            _fail("review_authority_unavailable")
        record = _capability(capability_path)
        if type(claim_revision) is not int or type(lease_revision) is not int:
            _fail("invalid_argument")
        attachment_id = str(record["attachment_id"])
        identity_id = str(record["identity_id"])
        generation = int(record["generation"])
        fence = str(record["fence"])
        prep = self.execution.find_preparation_for_attachment(
            attachment_id=attachment_id,
        )
        if prep is None or prep.lease is None or prep.lease.claim_id is None:
            _fail("lease_not_active")
        try:
            project_id, workspace_id, work_item_id = (
                self.execution.lookup_attachment_scope(attachment_id=attachment_id)
            )
            if prep.lease.status == "revoked":
                return self.delivery_service.publish_handoff(
                    project_id=project_id, workspace_id=workspace_id,
                    work_item_id=work_item_id, attachment_id=attachment_id,
                    identity_id=identity_id, generation=generation,
                    expected_claim_revision=claim_revision,
                    expected_work_revision=self._work_revision(
                        project_id, workspace_id, work_item_id,
                    ),
                    expected_lease_revision=lease_revision, summary=summary,
                    test_evidence=test_evidence, idempotency_key=idempotency_key,
                )
            checkout = Path(self.execution.checkout_internal_path(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            ))
            self.gate.authorize(
                attachment_id=attachment_id, identity_id=identity_id,
                generation=generation, fence=fence,
                capability_path=capability_path, checkout_path=checkout,
                execution=self.execution, project_id=project_id,
                workspace_id=workspace_id, work_item_id=work_item_id,
                expected_lease_revision=lease_revision,
                claim_id=prep.lease.claim_id,
            )
            return self.delivery_service.publish_handoff(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, attachment_id=attachment_id,
                identity_id=identity_id, generation=generation,
                expected_claim_revision=claim_revision,
                expected_work_revision=self._work_revision(
                    project_id, workspace_id, work_item_id,
                ),
                expected_lease_revision=lease_revision, summary=summary,
                test_evidence=test_evidence, idempotency_key=idempotency_key,
            )
        except WriteGateError as exc:
            _fail(_map(exc))
        except (exec_mod.WorkspaceExecutionError, delivery_service_mod.WorkspaceDeliveryServiceError) as exc:
            _fail(_map(exc))
        raise AssertionError("unreachable")

    def _work_revision(
        self, project_id: str, workspace_id: str, work_item_id: str,
    ) -> int:
        detail = self.work.get_work_item_detail(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        )
        if detail is None:
            _fail("work_item_not_found")
        revision = detail["work_item"].get("revision")
        if type(revision) is not int:
            _fail("work_item_not_found")
        return revision
