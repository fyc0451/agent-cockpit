"""Orchestrate B1 prepare/attach/detach. SQLite is not a Git/Herdr transaction."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import git_checkout_provider as checkout_mod
from . import local_codex_harness as harness_mod
from . import operation_store as operation_mod
from . import workspace_execution_store as exec_store
from . import workspace_work_store as work_store


class ExecutionServiceError(RuntimeError):
    def __init__(
        self, code: str, pane_id: str | None = None, instance_id: str | None = None,
        unknown: bool = False,
    ):
        self.code = code
        self.pane_id = pane_id
        self.instance_id = instance_id
        self.unknown = unknown
        super().__init__(code)


def _fail(
    code: str, pane_id: str | None = None, instance_id: str | None = None,
    unknown: bool = False,
) -> None:
    raise ExecutionServiceError(
        code, pane_id=pane_id, instance_id=instance_id, unknown=unknown,
    )


def _sha(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ExecutionService:
    registry_provider: Callable[[], Any]
    work_provider: Callable[[], work_store.WorkspaceWorkStore]
    store: exec_store.WorkspaceExecutionStore
    operations: operation_mod.OperationStore
    checkout: checkout_mod.GitCheckoutProvider
    harness: harness_mod.AgentHarnessAdapter
    worktrees_root: Path
    session_name: str = "cockpit-b-readonly"

    def list_members(self, project_id: str, workspace_id: str):
        self._require_scope(project_id, workspace_id, write=False)
        return self.store.list_identities(
            project_id=project_id, workspace_id=workspace_id,
        )

    def create_member(
        self, project_id: str, workspace_id: str, *, display_name: object,
        idempotency_key: object,
    ):
        self._require_scope(project_id, workspace_id, write=True)
        return self.store.create_identity(
            project_id=project_id, workspace_id=workspace_id,
            display_name=display_name, idempotency_key=idempotency_key,
        )

    def get_preparation(self, project_id: str, workspace_id: str, work_item_id: str):
        self._require_scope(project_id, workspace_id, write=False)
        self._work_item(project_id, workspace_id, work_item_id)
        item = self.store.get_preparation(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        )
        if item is None:
            _fail("preparation_not_found")
        return item

    def prepare(
        self, project_id: str, workspace_id: str, work_item_id: str, *,
        identity_id: object, idempotency_key: object,
    ):
        self._require_scope(project_id, workspace_id, write=True)
        self._work_item(project_id, workspace_id, work_item_id)
        if not isinstance(identity_id, str):
            _fail("invalid_argument")
        request = {"identity_id": identity_id, "work_item_id": work_item_id}
        replay = self.store.replay(
            project_id=project_id, workspace_id=workspace_id,
            scope="preparation.create", idempotency_key=idempotency_key,
            request=request,
        )
        if replay is not None:
            return replay
        identity = self.store.get_identity(
            project_id=project_id, workspace_id=workspace_id,
            identity_id=identity_id,
        )
        if identity is None:
            _fail("identity_not_found")
        source = self._source_path(project_id, workspace_id)
        try:
            exact = self.checkout.inspect_source(source)
        except checkout_mod.CheckoutError as exc:
            _fail(exc.code)
        root = Path(self.worktrees_root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        claim = self.store.claim_prepare(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id, identity_id=identity_id,
        )
        if claim.status == "ready" and claim.item is not None:
            payload = claim.item.public_dict()
            self.store.remember(
                project_id=project_id, workspace_id=workspace_id,
                scope="preparation.create", idempotency_key=idempotency_key,
                request=request, response=payload,
            )
            return payload
        if claim.status == "pending":
            payload = self._await_prepared(
                project_id, workspace_id, work_item_id,
            )
            self.store.remember(
                project_id=project_id, workspace_id=workspace_id,
                scope="preparation.create", idempotency_key=idempotency_key,
                request=request, response=payload,
            )
            return payload
        dest = root / "managed-checkouts" / str(claim.checkout_id)
        try:
            created = self._saga(
                kind="checkout.create",
                subject_type="work_item",
                subject_id=work_item_id,
                project_id=project_id,
                workspace_id=workspace_id,
                request=request,
                provider_kind="git",
                step_id="create-worktree",
                action=lambda: self.checkout.create_checkout(
                    source_path=source, checkout_path=dest, expected_head=exact.head,
                ),
            )
            view = self.store.complete_preparation(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, identity_id=identity_id,
                source_head=created.head, source_tree=created.tree,
                internal_path=created.path, operation_id=None,
            )
        except (
            checkout_mod.CheckoutError, exec_store.WorkspaceExecutionError,
            ExecutionServiceError,
        ):
            self.store.abort_prepare(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, checkout_id=str(claim.checkout_id),
            )
            checkout_mod._discard_unregistered(source, dest)
            raise
        payload = view.public_dict()
        self.store.remember(
            project_id=project_id, workspace_id=workspace_id,
            scope="preparation.create", idempotency_key=idempotency_key,
            request=request, response=payload,
        )
        return payload

    def attach(
        self, project_id: str, workspace_id: str, work_item_id: str, *,
        expected_revision: object, idempotency_key: object,
    ):
        self._require_scope(project_id, workspace_id, write=True)
        self._work_item(project_id, workspace_id, work_item_id)
        if type(expected_revision) is not int:
            _fail("invalid_argument")
        request = {
            "expected_revision": expected_revision, "work_item_id": work_item_id,
        }
        replay = self.store.replay(
            project_id=project_id, workspace_id=workspace_id,
            scope="preparation.attach", idempotency_key=idempotency_key,
            request=request,
        )
        if replay is not None:
            return self._replay_result(replay)
        current = self.store.get_preparation(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        )
        if (
            current is not None
            and current.state == "attaching"
            and current.revision == expected_revision
        ):
            current = self.store.fail_attach(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, expected_revision=expected_revision,
            )
            expected_revision = current.revision
        view, attachment, checkout = self.store.begin_attach(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id, expected_revision=expected_revision,
            session_name=self.session_name,
        )
        if view.state == "connected_readonly" and attachment.status == "connected_readonly":
            payload = view.public_dict()
            self.store.remember(
                project_id=project_id, workspace_id=workspace_id,
                scope="preparation.attach", idempotency_key=idempotency_key,
                request=request, response=payload,
            )
            return payload
        spec = self.harness.build_launch_spec(Path(checkout.internal_path))
        spec.assert_readonly()
        if attachment.pane_id:
            try:
                self.harness.detach(
                    session=attachment.session_name or self.session_name,
                    pane_id=attachment.pane_id,
                )
            except harness_mod.HarnessError as exc:
                if exc.code != "process_exited":
                    unknown = self.store.mark_unknown(
                        project_id=project_id, workspace_id=workspace_id,
                        work_item_id=work_item_id, expected_revision=view.revision,
                        pane_id=attachment.pane_id,
                        instance_id=attachment.instance_id,
                    )
                    self._remember_error(
                        project_id, workspace_id, "preparation.attach",
                        idempotency_key, request, exc.code, unknown,
                    )
                    _fail(exc.code, pane_id=attachment.pane_id)
            except Exception:
                unknown = self.store.mark_unknown(
                    project_id=project_id, workspace_id=workspace_id,
                    work_item_id=work_item_id, expected_revision=view.revision,
                    pane_id=attachment.pane_id,
                    instance_id=attachment.instance_id,
                )
                self._remember_error(
                    project_id, workspace_id, "preparation.attach",
                    idempotency_key, request, "runtime_unavailable", unknown,
                )
                _fail("runtime_unavailable", pane_id=attachment.pane_id)
        try:
            evidence = self._saga(
                kind="runtime.attach",
                subject_type="attachment",
                subject_id=attachment.attachment_id,
                project_id=project_id,
                workspace_id=workspace_id,
                request=request,
                provider_kind="herdr",
                step_id="attach-readonly",
                action=lambda: self.harness.attach_readonly(
                    session=self.session_name,
                    checkout_path=Path(checkout.internal_path),
                    display_name=view.identity.display_name,
                ),
            )
        except ExecutionServiceError as exc:
            self._recover_attach(
                project_id, workspace_id, work_item_id, view.revision,
                idempotency_key, request, exc,
            )
            raise
        if evidence.identity_verified is not True:
            self.store.fail_attach(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, expected_revision=view.revision,
            )
            _fail("runtime_identity_unverified")
        finished = self.store.finish_attach(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id, expected_revision=view.revision,
            pane_id=evidence.pane_id, instance_id=evidence.instance_id,
            identity_verified=True,
            native_receipt=_sha({
                "session": evidence.session, "cwd": evidence.cwd,
            }),
        )
        payload = finished.public_dict()
        self.store.remember(
            project_id=project_id, workspace_id=workspace_id,
            scope="preparation.attach", idempotency_key=idempotency_key,
            request=request, response=payload,
        )
        return payload

    def detach(
        self, project_id: str, workspace_id: str, work_item_id: str, *,
        expected_revision: object, idempotency_key: object,
    ):
        self._require_scope(project_id, workspace_id, write=True)
        self._work_item(project_id, workspace_id, work_item_id)
        if type(expected_revision) is not int:
            _fail("invalid_argument")
        request = {
            "expected_revision": expected_revision, "work_item_id": work_item_id,
        }
        replay = self.store.replay(
            project_id=project_id, workspace_id=workspace_id,
            scope="preparation.detach", idempotency_key=idempotency_key,
            request=request,
        )
        if replay is not None:
            return self._replay_result(replay)
        current = self.store.get_preparation(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        )
        if current is not None and current.state == "outcome_unknown":
            if current.revision != expected_revision:
                _fail("stale_revision")
            self._remember_error(
                project_id, workspace_id, "preparation.detach",
                idempotency_key, request, "runtime_unavailable", current,
            )
            _fail("runtime_unavailable")
        view, attachment, checkout = self.store.begin_detach(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id, expected_revision=expected_revision,
        )
        try:
            self._saga(
                kind="runtime.detach",
                subject_type="attachment",
                subject_id=attachment.attachment_id,
                project_id=project_id,
                workspace_id=workspace_id,
                request=request,
                provider_kind="herdr",
                step_id="close-pane",
                action=lambda: self._close(attachment, checkout),
            )
        except ExecutionServiceError as exc:
            if exc.unknown:
                unknown = self.store.mark_unknown(
                    project_id=project_id, workspace_id=workspace_id,
                    work_item_id=work_item_id, expected_revision=view.revision,
                    pane_id=attachment.pane_id, instance_id=attachment.instance_id,
                )
                self._remember_error(
                    project_id, workspace_id, "preparation.detach",
                    idempotency_key, request, "runtime_unavailable", unknown,
                )
                _fail("runtime_unavailable", unknown=True)
            restored = self.store.restore_connected(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, expected_revision=view.revision,
            )
            self._remember_error(
                project_id, workspace_id, "preparation.detach",
                idempotency_key, request, exc.code, restored,
            )
            _fail(exc.code)
        finished = self.store.finish_detach(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id, expected_revision=view.revision,
        )
        payload = finished.public_dict()
        self.store.remember(
            project_id=project_id, workspace_id=workspace_id,
            scope="preparation.detach", idempotency_key=idempotency_key,
            request=request, response=payload,
        )
        return payload

    def _replay_result(self, replay: object):
        if isinstance(replay, dict) and replay.get("ok") is False:
            code = replay.get("code")
            if not isinstance(code, str):
                _fail("store_read_failed")
            _fail(code)
        return replay

    def _remember_error(
        self, project_id: str, workspace_id: str, scope: str,
        idempotency_key: object, request: object, code: str, item,
    ) -> None:
        self.store.remember(
            project_id=project_id, workspace_id=workspace_id, scope=scope,
            idempotency_key=idempotency_key, request=request,
            response={"ok": False, "code": code, "item": item.public_dict()},
        )

    def _recover_attach(
        self, project_id: str, workspace_id: str, work_item_id: str,
        expected_revision: int, idempotency_key: object, request: object,
        exc: ExecutionServiceError,
    ) -> None:
        latest = self.store.get_preparation(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        )
        if latest is None or latest.state != "attaching":
            return
        if exc.pane_id or exc.unknown:
            unknown = self.store.mark_unknown(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, expected_revision=expected_revision,
                pane_id=exc.pane_id, instance_id=exc.instance_id,
            )
            self._remember_error(
                project_id, workspace_id, "preparation.attach",
                idempotency_key, request, exc.code, unknown,
            )
            return
        prepared = self.store.fail_attach(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id, expected_revision=expected_revision,
        )
        self._remember_error(
            project_id, workspace_id, "preparation.attach",
            idempotency_key, request, exc.code, prepared,
        )

    def _await_prepared(
        self, project_id: str, workspace_id: str, work_item_id: str,
    ) -> dict[str, object]:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            item = self.store.get_preparation(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            if item is not None and item.state == "prepared":
                return item.public_dict()
            if item is None:
                _fail("checkout_conflict")
            time.sleep(0.02)
        _fail("store_write_failed")
        raise AssertionError("unreachable")

    def _close(self, attachment, checkout) -> dict[str, object]:
        if not attachment.pane_id or not attachment.session_name:
            _fail("runtime_unavailable")
        self.harness.detach(
            session=attachment.session_name, pane_id=attachment.pane_id,
        )
        return {"closed": True, "checkout_id": checkout.checkout_id}

    def _require_scope(self, project_id: str, workspace_id: str, *, write: bool) -> Any:
        registry = self.registry_provider()
        snapshot = registry.get_project_by_id(project_id)
        if snapshot is None:
            _fail("project_not_found")
        project = getattr(snapshot, "project", None)
        if project is None:
            _fail("project_not_found")
        workspace = registry.get_workspace(project_id, workspace_id)
        if (
            workspace is None
            or getattr(workspace, "project_id", None) != project_id
            or getattr(workspace, "workspace_id", None) != workspace_id
        ):
            _fail("workspace_not_found")
        if write and (
            getattr(project, "lifecycle", None) != "active"
            or getattr(workspace, "lifecycle", None) != "active"
        ):
            _fail("workspace_not_active")
        return workspace

    def _source_path(self, project_id: str, workspace_id: str) -> Path:
        registry = self.registry_provider()
        snapshot = registry.get_project_by_id(project_id)
        workspace = registry.get_workspace(project_id, workspace_id)
        if snapshot is None or workspace is None:
            _fail("workspace_not_found")
        locations = getattr(snapshot, "repo_locations", ())
        location = next(
            (
                item for item in locations
                if getattr(item, "repo_location_id", None)
                == getattr(workspace, "repo_location_id", None)
            ),
            None,
        )
        if location is None:
            _fail("source_not_git")
        if getattr(location, "lifecycle", None) != "active":
            _fail("workspace_not_active")
        if getattr(location, "vcs_kind", None) != "git":
            _fail("source_not_git")
        path = getattr(location, "canonical_path", None)
        if not isinstance(path, str):
            _fail("source_not_git")
        return Path(path)

    def _work_item(self, project_id: str, workspace_id: str, work_item_id: str):
        item = self.work_provider().get_work_item(
            project_id=project_id, workspace_id=workspace_id,
            work_item_id=work_item_id,
        )
        if item is None:
            _fail("work_item_not_found")
        status = item.work_item.get("status")
        if status != "unassigned":
            _fail("invalid_argument")
        return item

    def _saga(
        self, *, kind: str, subject_type: str, subject_id: str, project_id: str,
        workspace_id: str, request: object, provider_kind: str, step_id: str,
        action, on_unknown=None,
    ):
        key = "saga-" + secrets.token_hex(16)
        try:
            created = self.operations.create_operation(
                scope="workspace-execution.v1",
                idempotency_key=key,
                request=request,
                kind=kind,
                subject_type=subject_type,
                subject_id=subject_id,
                plan_digest=_sha({"kind": kind, "subject_id": subject_id}),
                approval_required=False,
                project_id=project_id,
                workspace_id=workspace_id,
                steps=(operation_mod.Step(step_id, kind),),
            )
        except operation_mod.OperationError:
            _fail("store_write_failed")
        operation_id = created.operation_id
        revision = int(created.projection["operation"]["revision"])
        try:
            self.operations.transition(
                operation_id, expected_operation_revision=revision, status="running",
            )
            prepared = self.operations.prepare_attempt(
                operation_id, step_id, expected_operation_revision=revision + 1,
                expected_step_revision=1, mode="execute",
                provider_kind=provider_kind,
            )
            self.operations.dispatch_attempt(
                operation_id, prepared.step_execution_id,
                expected_operation_revision=revision + 2,
            )
        except operation_mod.OperationError:
            _fail("store_write_failed")
        try:
            result = action()
        except (
            checkout_mod.CheckoutError, harness_mod.HarnessError, ExecutionServiceError,
        ) as exc:
            code = getattr(exc, "code", "store_write_failed")
            self._record_outcome(
                operation_id, prepared.step_execution_id, revision + 3, 2,
                failed=True, code=code,
            )
            _fail(
                code,
                pane_id=getattr(exc, "pane_id", None),
                instance_id=getattr(exc, "instance_id", None),
            )
        except Exception:
            self._record_outcome(
                operation_id, prepared.step_execution_id, revision + 3, 2,
                unknown=True,
            )
            if on_unknown is not None:
                on_unknown()
            _fail("runtime_unavailable", unknown=True)
        self._record_outcome(
            operation_id, prepared.step_execution_id, revision + 3, 2,
        )
        return result

    def _record_outcome(
        self, operation_id: str, execution_id: str, operation_revision: int,
        step_revision: int, *, failed: bool = False, unknown: bool = False,
        code: str = "store_write_failed",
    ) -> None:
        try:
            if unknown:
                self.operations.record_attempt_outcome(
                    operation_id, execution_id,
                    expected_operation_revision=operation_revision,
                    expected_step_revision=step_revision,
                    receipt_id="rcp_" + secrets.token_hex(8),
                    receipt_type="provider_response_lost",
                    outcome="outcome_unknown",
                    evidence_kind="opaque_digest",
                    evidence_digest=_sha({"operation_id": operation_id}),
                )
                return
            if failed:
                self.operations.record_attempt_outcome(
                    operation_id, execution_id,
                    expected_operation_revision=operation_revision,
                    expected_step_revision=step_revision,
                    receipt_id="rcp_" + secrets.token_hex(8),
                    receipt_type="provider_outcome",
                    outcome="failed",
                    evidence_kind="opaque_digest",
                    evidence_digest=_sha({"operation_id": operation_id, "code": code}),
                    failure_code=code,
                )
                return
            self.operations.record_attempt_outcome(
                operation_id, execution_id,
                expected_operation_revision=operation_revision,
                expected_step_revision=step_revision,
                receipt_id="rcp_" + secrets.token_hex(8),
                receipt_type="provider_outcome",
                outcome="succeeded",
                evidence_kind="opaque_digest",
                evidence_digest=_sha({"operation_id": operation_id}),
            )
        except operation_mod.OperationError:
            return
