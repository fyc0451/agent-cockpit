from __future__ import annotations

import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import generation_prepare
import generation_switch
import maintenance_controller
import maintenance_ipc


TARGET = generation_switch.GenerationIdentity("a" * 40, "1" * 64)
PREVIOUS = generation_switch.GenerationIdentity("b" * 40, "2" * 64)


def _layout(
    tmp_path: Path,
) -> tuple[
    maintenance_controller.ControllerPlan,
    generation_prepare.PreparedGeneration,
    Path,
]:
    deploy = tmp_path / "deploy"
    generations = deploy / "generations"
    target = generations / TARGET.generation_id
    previous = generations / PREVIOUS.generation_id
    for root, version in ((target, "2.0.0"), (previous, "1.0.0")):
        (root / "bin").mkdir(parents=True, mode=0o700)
        version_path = root / "VERSION"
        version_path.write_text(version + "\n", encoding="ascii")
        version_path.chmod(0o600)
    target_launcher = target / "bin/agent-cockpit"
    target_launcher.write_bytes(b"\x7fELF" + b"native" * 100)
    target_launcher.chmod(0o700)
    (deploy / "current").symlink_to(Path("generations") / PREVIOUS.generation_id)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    controller_root = tmp_path / "controller"
    controller_root.mkdir(mode=0o700)
    controller_launcher = controller_root / "maintenance-controller"
    controller_launcher.write_bytes(b"controller")
    controller_launcher.chmod(0o700)
    plan = maintenance_controller.build_controller_plan(
        state_root=state,
        deploy_root=deploy,
        current=deploy / "current",
        controller_root=controller_root,
    )
    prepared = generation_prepare.PreparedGeneration(
        version="2.0.0",
        source_sha=TARGET.source_sha,
        artifact_digest=TARGET.artifact_digest,
        generation_id=TARGET.generation_id,
        generation_path=target,
        launcher_path=target_launcher,
    )
    return plan, prepared, controller_launcher


def _expected_argv(
    plan: maintenance_controller.ControllerPlan,
    prepared: generation_prepare.PreparedGeneration,
    controller_launcher: Path,
) -> tuple[str, ...]:
    return (
        str(controller_launcher),
        "maintenance-controller",
        "execute",
        "--state-root", str(plan.state_root),
        "--deploy-root", str(plan.deploy_root),
        "--current", str(plan.current),
        "--controller-root", str(plan.controller_root),
        "--request-id", "request-1",
        "--version", prepared.version,
        "--source-sha", prepared.source_sha,
        "--artifact-digest", prepared.artifact_digest,
        "--generation-id", prepared.generation_id,
        "--generation-path", str(prepared.generation_path),
        "--launcher-path", str(prepared.launcher_path),
    )


def test_spawns_detached_canonical_controller_with_fixed_argv(tmp_path: Path) -> None:
    plan, prepared, launcher = _layout(tmp_path)
    calls: list[tuple[tuple[str, ...], dict]] = []

    class Process:
        pid = 4321

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    receipt = maintenance_ipc.spawn_maintenance_controller(
        plan=plan,
        prepared=prepared,
        request_id="request-1",
        controller_launcher=launcher,
        popen=popen,
    )

    assert receipt == maintenance_ipc.ControllerAccepted(pid=4321, accepted=True)
    assert calls == [(
        _expected_argv(plan, prepared, launcher),
        {
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
            "start_new_session": True,
            "shell": False,
        },
    )]
    assert "env" not in calls[0][1]
    assert "cwd" not in calls[0][1]
    with pytest.raises(FrozenInstanceError):
        receipt.pid = 9  # type: ignore[misc]


def test_fixed_argv_composes_with_l8_native_dispatch_contract(tmp_path: Path) -> None:
    plan, prepared, launcher = _layout(tmp_path)
    calls: list[tuple[str, ...]] = []

    maintenance_ipc.spawn_maintenance_controller(
        plan=plan,
        prepared=prepared,
        request_id="request-1",
        controller_launcher=launcher,
        popen=lambda argv, **_kwargs: (
            calls.append(argv) or type("Process", (), {"pid": 123})()
        ),
    )

    assert calls[0][:3] == (
        str(launcher), "maintenance-controller", "execute",
    )


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("plan", "plan_invalid"),
        ("prepared", "request_invalid"),
        ("request", "request_invalid"),
        ("launcher", "launcher_invalid"),
        ("launcher_mode", "launcher_invalid"),
        ("launcher_symlink", "launcher_invalid"),
        ("launcher_hardlink", "launcher_invalid"),
    ],
)
def test_rejects_invalid_inputs_before_spawn(
    tmp_path: Path, mutation: str, code: str,
) -> None:
    plan, prepared, launcher = _layout(tmp_path)
    request_id = "request-1"
    if mutation == "plan":
        plan = maintenance_controller.ControllerPlan(
            **{**plan.__dict__, "current": plan.deploy_root / "other"}
        )
    elif mutation == "prepared":
        prepared = generation_prepare.PreparedGeneration(
            **{**prepared.__dict__, "generation_id": "c" * 104}
        )
    elif mutation == "request":
        request_id = "../escape"
    else:
        if mutation == "launcher":
            launcher = tmp_path / "outside-controller"
            launcher.write_bytes(b"outside")
            launcher.chmod(0o700)
        elif mutation == "launcher_mode":
            launcher.chmod(0o722)
        elif mutation == "launcher_symlink":
            real = launcher.with_name("real-controller")
            launcher.rename(real)
            launcher.symlink_to(real)
        else:
            hardlink = launcher.with_name("hardlinked-controller")
            hardlink.hardlink_to(launcher)

    with pytest.raises(maintenance_ipc.MaintenanceIpcError, match=code):
        maintenance_ipc.spawn_maintenance_controller(
            plan=plan,
            prepared=prepared,
            request_id=request_id,
            controller_launcher=launcher,
            popen=lambda *_args, **_kwargs: pytest.fail("spawn must not run"),
        )


def test_maps_spawn_failure_and_rejects_invalid_pid(tmp_path: Path) -> None:
    plan, prepared, launcher = _layout(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("spawn failed")

    with pytest.raises(maintenance_ipc.MaintenanceIpcError, match="spawn_failed"):
        maintenance_ipc.spawn_maintenance_controller(
            plan=plan,
            prepared=prepared,
            request_id="request-1",
            controller_launcher=launcher,
            popen=fail,
        )

    with pytest.raises(maintenance_ipc.MaintenanceIpcError, match="spawn_result_invalid"):
        maintenance_ipc.spawn_maintenance_controller(
            plan=plan,
            prepared=prepared,
            request_id="request-1",
            controller_launcher=launcher,
            popen=lambda *_args, **_kwargs: type("Process", (), {"pid": True})(),
        )
