from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

import generation_prepare
import generation_switch
import maintenance_controller
import maintenance_evidence
import maintenance_executor
import maintenance_request


TARGET = generation_switch.GenerationIdentity("a" * 40, "1" * 64)
PREVIOUS = generation_switch.GenerationIdentity("b" * 40, "2" * 64)


def _layout(
    tmp_path: Path,
) -> tuple[maintenance_controller.ControllerPlan, generation_prepare.PreparedGeneration]:
    deploy = tmp_path / "deploy"
    generations = deploy / "generations"
    target_root = generations / TARGET.generation_id
    previous_root = generations / PREVIOUS.generation_id
    for root, release_version in ((target_root, "2.0.0"), (previous_root, "1.0.0")):
        (root / "bin").mkdir(parents=True, mode=0o700)
        (root / "VERSION").write_text(release_version + "\n", encoding="ascii")
        (root / "VERSION").chmod(0o600)
    launcher = target_root / "bin" / "agent-cockpit"
    launcher.write_bytes(b"\x7fELFtarget")
    launcher.chmod(0o700)
    (deploy / "current").symlink_to(Path("generations") / PREVIOUS.generation_id)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    controller = tmp_path / "controller"
    controller.mkdir(mode=0o700)
    plan = maintenance_controller.build_controller_plan(
        state_root=state,
        deploy_root=deploy,
        current=deploy / "current",
        controller_root=controller,
    )
    prepared = generation_prepare.PreparedGeneration(
        version="2.0.0",
        source_sha=TARGET.source_sha,
        artifact_digest=TARGET.artifact_digest,
        generation_id=TARGET.generation_id,
        generation_path=target_root,
        launcher_path=launcher,
    )
    return plan, prepared


def test_builds_canonical_request_without_creating_state(tmp_path: Path) -> None:
    plan, prepared = _layout(tmp_path)
    before = tuple(sorted(path.name for path in plan.state_root.iterdir()))

    request = maintenance_request.build_maintenance_request(
        plan=plan,
        prepared=prepared,
        request_id="request-1",
    )

    assert request == maintenance_executor.MaintenanceRequest(
        plan=plan,
        request_id="request-1",
        target_version="2.0.0",
        target=TARGET,
        previous_version="1.0.0",
        previous=PREVIOUS,
        target_root=prepared.generation_path,
        snapshot_root=plan.state_root / "upgrade-snapshots" / "request-1",
        evidence_path=maintenance_evidence.evidence_binding_path(
            plan=plan,
            request_id="request-1",
            role="target",
            generation=TARGET,
        ),
    )
    assert tuple(sorted(path.name for path in plan.state_root.iterdir())) == before
    assert not request.snapshot_root.exists()
    assert not request.evidence_path.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "02.0.0"),
        ("source_sha", "A" * 40),
        ("artifact_digest", "1" * 63),
        ("generation_id", "wrong"),
        ("generation_path", Path("relative")),
        ("launcher_path", Path("relative")),
    ],
)
def test_rejects_forged_prepared_receipt_before_current_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    plan, prepared = _layout(tmp_path)
    forged = replace(prepared, **{field: value})
    monkeypatch.setattr(
        maintenance_request.maintenance_executor,
        "inspect_current_generation",
        lambda _plan: pytest.fail("invalid receipt must fail before current probe"),
    )

    with pytest.raises(maintenance_request.MaintenanceRequestBuildError) as exc:
        maintenance_request.build_maintenance_request(
            plan=plan,
            prepared=forged,
            request_id="request-1",
        )

    assert exc.value.code == "prepared_invalid"


@pytest.mark.parametrize("request_id", ["", "has space", "../escape", "x" * 201, None])
def test_rejects_invalid_request_id_before_current_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_id: object,
) -> None:
    plan, prepared = _layout(tmp_path)
    monkeypatch.setattr(
        maintenance_request.maintenance_executor,
        "inspect_current_generation",
        lambda _plan: pytest.fail("invalid request must fail before current probe"),
    )

    with pytest.raises(maintenance_request.MaintenanceRequestBuildError) as exc:
        maintenance_request.build_maintenance_request(
            plan=plan,
            prepared=prepared,
            request_id=request_id,  # type: ignore[arg-type]
        )

    assert exc.value.code == "request_invalid"


def test_rejects_target_equal_to_current(tmp_path: Path) -> None:
    plan, prepared = _layout(tmp_path)
    (plan.current).unlink()
    plan.current.symlink_to(Path("generations") / TARGET.generation_id)

    with pytest.raises(maintenance_request.MaintenanceRequestBuildError) as exc:
        maintenance_request.build_maintenance_request(
            plan=plan, prepared=prepared, request_id="request-1"
        )

    assert exc.value.code == "already_current"


@pytest.mark.parametrize("damage", ["target_version", "previous_version", "launcher_mode"])
def test_rejects_generation_file_drift(tmp_path: Path, damage: str) -> None:
    plan, prepared = _layout(tmp_path)
    if damage == "target_version":
        (prepared.generation_path / "VERSION").write_text("9.9.9\n", encoding="ascii")
    elif damage == "previous_version":
        previous_root = plan.deploy_root / "generations" / PREVIOUS.generation_id
        (previous_root / "VERSION").write_text("not-semver\n", encoding="ascii")
    else:
        prepared.launcher_path.chmod(0o755)

    with pytest.raises(maintenance_request.MaintenanceRequestBuildError) as exc:
        maintenance_request.build_maintenance_request(
            plan=plan, prepared=prepared, request_id="request-1"
        )

    assert exc.value.code == "generation_invalid"


def test_rejects_launcher_symlink(tmp_path: Path) -> None:
    plan, prepared = _layout(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"launcher")
    prepared.launcher_path.unlink()
    prepared.launcher_path.symlink_to(outside)

    with pytest.raises(maintenance_request.MaintenanceRequestBuildError) as exc:
        maintenance_request.build_maintenance_request(
            plan=plan, prepared=prepared, request_id="request-1"
        )

    assert exc.value.code == "generation_invalid"


def test_current_probe_error_is_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan, prepared = _layout(tmp_path)

    def fail(_plan: maintenance_controller.ControllerPlan) -> None:
        raise maintenance_executor.MaintenanceExecutorError("private-current-detail")

    monkeypatch.setattr(
        maintenance_request.maintenance_executor, "inspect_current_generation", fail
    )

    with pytest.raises(maintenance_request.MaintenanceRequestBuildError) as exc:
        maintenance_request.build_maintenance_request(
            plan=plan, prepared=prepared, request_id="request-1"
        )

    assert exc.value.code == "current_invalid"
    assert "private-current-detail" not in str(exc.value)


def test_target_launcher_is_owned_regular_exact_mode(tmp_path: Path) -> None:
    plan, prepared = _layout(tmp_path)
    prepared.launcher_path.write_bytes(b"\x7fELF" + b"target" * 100)
    info = prepared.launcher_path.stat()
    assert info.st_uid == os.getuid()
    assert info.st_nlink == 1

    assert maintenance_request.build_maintenance_request(
        plan=plan, prepared=prepared, request_id="request-1"
    ).target == TARGET
