"""Capability-bound private claim tool for a connected workspace agent."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import local_codex_harness as harness_mod
from . import workspace_execution_store as execution_mod
from . import workspace_work_store as work_mod


class ClaimToolError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise ClaimToolError(code)


@dataclass(frozen=True)
class ClaimContext:
    project_id: str
    workspace_id: str
    work_item_id: str
    identity_id: str
    generation: int
    attachment_id: str
    preparation_revision: int
    work_revision: int
    lease_id: str
    lease_revision: int
    fence_digest: str


class ClaimActivator(Protocol):
    def activate(
        self, context: ClaimContext, pending_claim: dict[str, object], *,
        idempotency_key: str,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class WorkspaceClaimTools:
    work: work_mod.WorkspaceWorkStore
    execution: execution_mod.WorkspaceExecutionStore
    activator: ClaimActivator

    def claim_current(self, capability_file: Path) -> dict[str, object]:
        record = self._capability(capability_file)
        context = self._context(record)
        key_digest = hashlib.sha256(
            (str(record["token"]) + context.work_item_id).encode()
        ).hexdigest()
        pending = self.work.reserve_claim(
            project_id=context.project_id,
            workspace_id=context.workspace_id,
            work_item_id=context.work_item_id,
            identity_id=context.identity_id,
            generation=context.generation,
            expected_revision=context.work_revision,
            idempotency_key="claim-reserve-" + key_digest,
        )
        activated = self.activator.activate(
            context, pending, idempotency_key="claim-activate-" + key_digest,
        )
        claim = activated.get("claim") if isinstance(activated, dict) else None
        work = activated.get("work_item") if isinstance(activated, dict) else None
        root = activated.get("root_message") if isinstance(activated, dict) else None
        if (
            not isinstance(claim, dict)
            or claim.get("state") != "active"
            or not isinstance(work, dict)
            or work.get("status") != "working"
            or not isinstance(root, dict)
            or not isinstance(root.get("body"), str)
        ):
            _fail("claim_not_active")
        return activated

    @staticmethod
    def _capability(path: Path) -> dict[str, Any]:
        try:
            record = harness_mod._read_capability(Path(path))
            generation = record.get("generation")
            if type(generation) is not int or generation < 1:
                _fail("runtime_capability_invalid")
            if harness_mod.current_generation(Path(path)) != generation:
                _fail("stale_generation")
            if (
                not isinstance(record.get("token"), str)
                or len(record["token"]) != 64
            ):
                _fail("runtime_capability_invalid")
            harness_mod.attachment_id_text(record.get("attachment_id"))
            return record
        except ClaimToolError:
            raise
        except (harness_mod.HarnessError, OSError, TypeError):
            _fail("runtime_capability_invalid")
        raise AssertionError("unreachable")

    def _context(self, record: dict[str, Any]) -> ClaimContext:
        attachment_id = str(record["attachment_id"])
        try:
            internal = self.execution.attachment_internal(attachment_id)
            if internal is None:
                _fail("runtime_capability_invalid")
            connection = execution_mod._connect(self.execution.path, write=False)
            try:
                execution_mod._require_current_schema(connection)
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT p.project_id,p.workspace_id,p.work_item_id,"
                    "p.identity_id,p.generation,p.state,p.revision,"
                    "p.lease_id,p.attachment_id,l.status AS lease_status,"
                    "l.generation AS lease_generation,l.revision AS lease_revision,"
                    "l.fence_digest,a.identity_id AS attachment_identity,"
                    "a.generation AS attachment_generation,"
                    "a.status AS attachment_status,a.pane_id,a.session_name,"
                    "a.native_receipt FROM work_item_preparations p "
                    "JOIN writer_leases l ON l.lease_id=p.lease_id "
                    "JOIN runtime_attachments a ON a.attachment_id=p.attachment_id "
                    "WHERE p.attachment_id=?",
                    (attachment_id,),
                ).fetchone()
            finally:
                connection.close()
        except ClaimToolError:
            raise
        except (execution_mod.WorkspaceExecutionError, sqlite3.Error):
            _fail("runtime_capability_invalid")
        if row is None:
            _fail("runtime_capability_invalid")
        generation = int(row["generation"])
        if (
            row["state"] != "connected_readonly"
            or row["lease_status"] != "reserved"
            or row["attachment_status"] != "connected_readonly"
            or row["native_receipt"] is None
            or row["identity_id"] != row["attachment_identity"]
            or generation != int(row["lease_generation"])
            or generation != int(row["attachment_generation"])
            or record.get("identity_id") != row["identity_id"]
            or record.get("generation") != generation
            or record.get("fence") != row["fence_digest"]
            or record.get("session") != row["session_name"]
            or record.get("pane_id") != row["pane_id"]
            or internal.generation != generation
            or internal.status != "connected_readonly"
            or internal.session_name != row["session_name"]
            or internal.pane_id != row["pane_id"]
        ):
            _fail("runtime_capability_invalid")
        detail = self.work.get_work_item_detail(
            project_id=row["project_id"], workspace_id=row["workspace_id"],
            work_item_id=row["work_item_id"],
        )
        if detail is None:
            _fail("work_item_not_found")
        work = detail.get("work_item")
        claim = detail.get("claim")
        if not isinstance(work, dict):
            _fail("claim_conflict")
        work_revision = int(work["revision"])
        if work.get("status") == "working":
            if (
                not isinstance(claim, dict)
                or claim.get("state") != "active"
                or claim.get("identity_id") != row["identity_id"]
                or claim.get("generation") != generation
                or work_revision < 2
            ):
                _fail("claim_conflict")
            work_revision -= 1
        elif work.get("status") != "unassigned":
            _fail("claim_conflict")
        preparation = self.execution.get_preparation(
            project_id=row["project_id"], workspace_id=row["workspace_id"],
            work_item_id=row["work_item_id"],
        )
        if (
            preparation is None
            or preparation.revision != int(row["revision"])
            or preparation.principal != {
                "identity_id": row["identity_id"], "generation": generation,
            }
            or preparation.attachment is None
            or preparation.attachment.attachment_id != attachment_id
            or preparation.attachment.identity_verified is not True
            or preparation.lease is None
            or preparation.lease.lease_id != row["lease_id"]
        ):
            _fail("runtime_capability_invalid")
        return ClaimContext(
            project_id=row["project_id"], workspace_id=row["workspace_id"],
            work_item_id=row["work_item_id"], identity_id=row["identity_id"],
            generation=generation, attachment_id=attachment_id,
            preparation_revision=int(row["revision"]),
            work_revision=work_revision, lease_id=row["lease_id"],
            lease_revision=int(row["lease_revision"]),
            fence_digest=row["fence_digest"],
        )
