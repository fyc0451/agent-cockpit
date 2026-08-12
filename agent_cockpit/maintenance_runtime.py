"""Dormant composition adapter for one maintenance-window upgrade."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import generation_switch
from . import maintenance_controller
from . import maintenance_evidence
from . import maintenance_executor
from . import upgrade_snapshot


TargetProbe = Callable[
    [maintenance_executor.MaintenanceRequest, Path, str], dict[str, object]
]

_REQUEST_RE = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
_VERSION_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class MaintenanceRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise MaintenanceRuntimeError(code)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_request(request: maintenance_executor.MaintenanceRequest) -> None:
    if not isinstance(request, maintenance_executor.MaintenanceRequest):
        _fail("request_invalid")
    plan = request.plan
    paths = (request.target_root, request.snapshot_root, request.evidence_path)
    if (
        not isinstance(plan, maintenance_controller.ControllerPlan)
        or type(request.request_id) is not str
        or _REQUEST_RE.fullmatch(request.request_id) is None
        or type(request.target_version) is not str
        or _VERSION_RE.fullmatch(request.target_version) is None
        or type(request.previous_version) is not str
        or _VERSION_RE.fullmatch(request.previous_version) is None
        or not isinstance(request.target, generation_switch.GenerationIdentity)
        or not isinstance(request.previous, generation_switch.GenerationIdentity)
        or request.target == request.previous
        or any(not isinstance(path, Path) or not path.is_absolute() for path in paths)
    ):
        _fail("request_invalid")
    try:
        maintenance_evidence.validate_evidence_plan(plan)
        expected_evidence = maintenance_evidence.evidence_binding_path(
            plan=plan,
            request_id=request.request_id,
            role="target",
            generation=request.target,
        )
    except maintenance_evidence.EvidenceEnvironmentError:
        _fail("request_invalid")
    if (
        request.target_root
        != plan.deploy_root / "generations" / request.target.generation_id
        or request.snapshot_root
        != plan.state_root
        / maintenance_executor.SNAPSHOT_DIR_NAME
        / request.request_id
        or request.evidence_path != expected_evidence
        or request.ready_url != maintenance_executor.READY_URL
        or any(".." in path.parts for path in paths)
        or _inside(request.snapshot_root, plan.deploy_root)
        or _inside(request.evidence_path, plan.deploy_root)
    ):
        _fail("request_invalid")


def _to_executor_binding(
    binding: maintenance_evidence.EvidenceBinding,
) -> maintenance_executor.EvidenceBinding:
    return maintenance_executor.EvidenceBinding(
        request_id=binding.request_id,
        role=binding.role,
        identity=binding.generation,
        evidence_path=binding.path,
        evidence_sha256=binding.sha256,
    )


def execute_maintenance(
    request: maintenance_executor.MaintenanceRequest,
    *,
    runner: maintenance_executor.Runner,
    ready_probe: maintenance_executor.ReadyProbe,
    target_probe: TargetProbe,
) -> dict[str, Any]:
    """Compose accepted primitives under one controller lease; no production wiring."""
    if not callable(runner) or not callable(ready_probe) or not callable(target_probe):
        _fail("wiring_unavailable")
    _validate_request(request)

    def identity_for(role: str) -> tuple[str, generation_switch.GenerationIdentity, Path]:
        if role == "previous":
            return request.previous_version, request.previous, (
                request.plan.deploy_root
                / "generations"
                / request.previous.generation_id
            )
        if role == "target":
            return request.target_version, request.target, request.target_root
        _fail("binding_invalid")

    def load_binding(role: str) -> maintenance_evidence.EvidenceBinding:
        version, generation, artifact_root = identity_for(role)
        return maintenance_evidence.load_schema_evidence(
            plan=request.plan,
            request_id=request.request_id,
            role=role,
            expected_version=version,
            expected_generation=generation,
            artifact_root=artifact_root,
        )

    def read_binding(request_id: str, role: str) -> maintenance_executor.EvidenceBinding:
        if request_id != request.request_id:
            _fail("binding_invalid")
        if role == "active":
            current = maintenance_executor.inspect_current_generation(request.plan)
            if current == request.previous:
                expected_role = "previous"
            elif current == request.target:
                expected_role = "target"
            else:
                _fail("current_drift")
            version, generation, artifact_root = identity_for(expected_role)
            binding = maintenance_evidence.read_active_server_evidence(
                plan=request.plan,
                expected_request_id=request.request_id,
                expected_role=expected_role,
                expected_version=version,
                expected_generation=generation,
                artifact_root=artifact_root,
            )
        else:
            binding = load_binding(role)
        return _to_executor_binding(binding)

    def activate_binding(request_id: str, role: str) -> None:
        if request_id != request.request_id:
            _fail("binding_invalid")
        version, generation, artifact_root = identity_for(role)
        maintenance_evidence.activate_server_evidence(
            plan=request.plan,
            binding=load_binding(role),
            expected_request_id=request.request_id,
            expected_role=role,
            expected_version=version,
            expected_generation=generation,
            artifact_root=artifact_root,
        )

    def prepare_target(
        prepared: maintenance_executor.MaintenanceRequest,
    ) -> None:
        if prepared != request:
            _fail("request_invalid")
        try:
            load_binding("target")
        except maintenance_evidence.EvidenceEnvironmentError as exc:
            if exc.code != "evidence_missing":
                raise
        else:
            return
        request.snapshot_root.parent.mkdir(mode=0o700, exist_ok=True)
        snapshot = upgrade_snapshot.create_backup_snapshot(
            snapshot_root=request.snapshot_root,
            snapshot_id=request.request_id,
            request_id=request.request_id,
            source_sha=request.previous.source_sha,
            target_digest=request.target.artifact_digest,
        )
        if not isinstance(snapshot, dict):
            _fail("snapshot_invalid")
        inventory_path = snapshot.get("inventory_path")
        inventory_sha256 = snapshot.get("inventory_sha256")
        if (
            not isinstance(inventory_path, Path)
            or inventory_path
            != request.snapshot_root / upgrade_snapshot.INVENTORY_NAME
            or type(inventory_sha256) is not str
            or _SHA256_RE.fullmatch(inventory_sha256) is None
        ):
            _fail("snapshot_invalid")
        evidence = target_probe(request, inventory_path, inventory_sha256)
        if not isinstance(evidence, dict):
            _fail("target_probe_invalid")
        maintenance_evidence.publish_schema_evidence(
            plan=request.plan,
            request_id=request.request_id,
            role="target",
            expected_version=request.target_version,
            expected_generation=request.target,
            artifact_root=request.target_root,
            evidence=evidence,
        )

    def initialize_previous(
        lease: maintenance_controller.ControllerLease,
    ) -> None:
        previous_root = (
            request.plan.deploy_root / "generations" / request.previous.generation_id
        )
        try:
            existing = load_binding("previous")
        except maintenance_evidence.EvidenceEnvironmentError as exc:
            if exc.code != "evidence_missing":
                raise
            existing = None
        if existing is not None:
            try:
                maintenance_evidence.read_active_server_evidence(
                    plan=request.plan,
                    expected_request_id=request.request_id,
                    expected_role="previous",
                    expected_version=request.previous_version,
                    expected_generation=request.previous,
                    artifact_root=previous_root,
                )
                frozen = existing
            except maintenance_evidence.EvidenceEnvironmentError:
                frozen = maintenance_evidence.freeze_active_server_evidence_under_lease(
                    controller_lease=lease,
                    plan=request.plan,
                    request_id=request.request_id,
                    expected_version=request.previous_version,
                    expected_generation=request.previous,
                    artifact_root=previous_root,
                )
        else:
            frozen = maintenance_evidence.freeze_active_server_evidence_under_lease(
                controller_lease=lease,
                plan=request.plan,
                request_id=request.request_id,
                expected_version=request.previous_version,
                expected_generation=request.previous,
                artifact_root=previous_root,
            )
        maintenance_evidence.activate_server_evidence(
            plan=request.plan,
            binding=frozen,
            expected_request_id=request.request_id,
            expected_role="previous",
            expected_version=request.previous_version,
            expected_generation=request.previous,
            artifact_root=previous_root,
        )

    try:
        with maintenance_controller.controller_lock(request.plan) as lease:
            status = maintenance_controller.read_controller_status(request.plan)
            if status["state"] == "idle":
                initialize_previous(lease)
            return maintenance_executor.execute_prepared_generation(
                request,
                runner=runner,
                ready_probe=ready_probe,
                prepare_target=prepare_target,
                activate_binding=activate_binding,
                read_binding=read_binding,
                controller_lease=lease,
            )
    except maintenance_controller.ControllerPreflightError as exc:
        _fail(exc.code)


__all__ = [
    "MaintenanceRuntimeError",
    "TargetProbe",
    "execute_maintenance",
]
