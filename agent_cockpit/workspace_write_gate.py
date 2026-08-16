"""Synchronous write-boundary stub. C2 never becomes an active writer."""
from __future__ import annotations

from pathlib import Path

from . import local_codex_harness as harness_mod


class WriteGateError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise WriteGateError(code)


class WorkspaceWriteGate:
    def authorize(
        self, *, attachment_id: str, identity_id: str, generation: int,
        fence: str, capability_path: Path, checkout_path: Path,
        execution=None, project_id: str | None = None,
        workspace_id: str | None = None, work_item_id: str | None = None,
        expected_lease_revision: int | None = None, claim_id: str | None = None,
    ) -> None:
        path = Path(capability_path)
        if type(generation) is not int or generation < 1:
            _fail("invalid_argument")
        try:
            attachment_id = harness_mod.attachment_id_text(attachment_id)
        except harness_mod.HarnessError:
            _fail("invalid_argument")
        try:
            record = harness_mod._read_capability(path)
        except harness_mod.HarnessError:
            _fail("runtime_capability_invalid")
        if (
            record.get("attachment_id") != attachment_id
            or record.get("identity_id") != identity_id
        ):
            _fail("runtime_capability_invalid")
        stored = record.get("generation")
        if type(stored) is not int or stored != generation:
            _fail("stale_generation")
        try:
            current = harness_mod.current_generation(path)
        except harness_mod.HarnessError as exc:
            if exc.code == "stale_generation":
                _fail("stale_generation")
            _fail("runtime_capability_invalid")
        if current != generation:
            _fail("stale_generation")
        if record.get("fence") != fence:
            _fail("fence_rejected")
        if not Path(checkout_path).is_dir():
            _fail("invalid_argument")
        if execution is None:
            _fail("lease_not_active")
        try:
            prep = execution.get_preparation(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            managed = Path(execution.checkout_internal_path(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            ))
        except Exception as exc:
            code = getattr(exc, "code", "")
            if code == "preparation_not_found":
                _fail("preparation_not_found")
            if code == "reconcile_required":
                _fail("reconcile_required")
            _fail("lease_not_active")
        if prep is None:
            _fail("preparation_not_found")
        lease = prep.lease
        if lease is None:
            _fail("lease_not_active")
        if lease.status == "uncertain":
            _fail("reconcile_required")
        if lease.status != "active":
            _fail("lease_not_active")
        if (
            expected_lease_revision is not None
            and lease.revision != expected_lease_revision
        ):
            _fail("stale_revision")
        if claim_id is not None and lease.claim_id != claim_id:
            _fail("claim_not_active")
        if prep.attachment is None or prep.attachment.attachment_id != attachment_id:
            _fail("runtime_capability_invalid")
        if prep.identity.identity_id != identity_id:
            _fail("runtime_capability_invalid")
        if prep.principal.get("generation") != generation:
            _fail("stale_generation")
        try:
            wanted = Path(checkout_path)
            if (
                not managed.is_absolute()
                or not wanted.is_absolute()
                or managed.resolve() != wanted.resolve()
            ):
                _fail("checkout_untrusted")
        except OSError:
            _fail("checkout_untrusted")
