from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import generation_prepare
import generation_switch
import maintenance_cli
import maintenance_controller
import maintenance_executor


TARGET = generation_switch.GenerationIdentity("a" * 40, "1" * 64)
PREVIOUS = generation_switch.GenerationIdentity("b" * 40, "2" * 64)


def _layout(
    tmp_path: Path,
) -> tuple[maintenance_controller.ControllerPlan, generation_prepare.PreparedGeneration]:
    deploy = tmp_path / "deploy"
    generations = deploy / "generations"
    target = generations / TARGET.generation_id
    previous = generations / PREVIOUS.generation_id
    for root, version in ((target, "2.0.0"), (previous, "1.0.0")):
        (root / "bin").mkdir(parents=True, mode=0o700)
        (root / "VERSION").write_text(version + "\n", encoding="ascii")
        (root / "VERSION").chmod(0o600)
    launcher = target / "bin" / "agent-cockpit"
    launcher.write_bytes(b"\x7fELF" + b"native" * 100)
    launcher.chmod(0o700)
    (deploy / "current").symlink_to(Path("generations") / PREVIOUS.generation_id)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    controller_root = tmp_path / "controller"
    controller_root.mkdir(mode=0o700)
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
        launcher_path=launcher,
    )
    return plan, prepared


def _plan_argv(plan: maintenance_controller.ControllerPlan) -> list[str]:
    return [
        "--state-root", str(plan.state_root),
        "--deploy-root", str(plan.deploy_root),
        "--current", str(plan.current),
        "--controller-root", str(plan.controller_root),
    ]


def _execute_argv(
    plan: maintenance_controller.ControllerPlan,
    prepared: generation_prepare.PreparedGeneration,
) -> list[str]:
    return [
        "execute", *_plan_argv(plan),
        "--request-id", "request-1",
        "--version", prepared.version,
        "--source-sha", prepared.source_sha,
        "--artifact-digest", prepared.artifact_digest,
        "--generation-id", prepared.generation_id,
        "--generation-path", str(prepared.generation_path),
        "--launcher-path", str(prepared.launcher_path),
    ]


def test_execute_rebuilds_canonical_request_and_calls_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, prepared = _layout(tmp_path)
    captured: dict[str, Any] = {}

    def runtime(request: maintenance_executor.MaintenanceRequest, **kwargs: Any):
        captured["request"] = request
        captured.update(kwargs)
        return {"journal": {"stage": "committed", "request_id": request.request_id}}

    monkeypatch.setattr(maintenance_cli.maintenance_runtime, "execute_maintenance", runtime)
    runner = object()
    ready_probe = object()
    target_probe = object()

    result = maintenance_cli.execute_prepared(
        plan=plan,
        prepared=prepared,
        request_id="request-1",
        runner=runner,  # type: ignore[arg-type]
        ready_probe=ready_probe,  # type: ignore[arg-type]
        target_probe=target_probe,  # type: ignore[arg-type]
    )

    assert result["journal"]["stage"] == "committed"
    request = captured["request"]
    assert request.target == TARGET
    assert request.previous == PREVIOUS
    assert request.target_version == "2.0.0"
    assert request.previous_version == "1.0.0"
    assert captured["runner"] is runner
    assert captured["ready_probe"] is ready_probe
    assert captured["target_probe"] is target_probe


def test_status_is_pure_read_and_canonical_json(tmp_path: Path, capsys) -> None:
    plan, _prepared = _layout(tmp_path)
    before = tuple(plan.state_root.iterdir())

    assert maintenance_cli.main(["status", *_plan_argv(plan)]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "journal": None,
        "ok": True,
        "state": "idle",
    }
    assert tuple(plan.state_root.iterdir()) == before


def test_execute_cli_emits_only_allowlisted_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    plan, prepared = _layout(tmp_path)
    monkeypatch.setattr(
        maintenance_cli,
        "execute_prepared",
        lambda **_kwargs: {
            "journal": {
                "stage": "committed",
                "request_id": "request-1",
                "private": "/private/path",
            },
            "evidence_path": Path("/private/evidence"),
        },
    )

    assert maintenance_cli.main(_execute_argv(plan, prepared)) == 0

    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "request_id": "request-1",
        "state": "committed",
    }


def test_execute_cli_sanitizes_known_and_unknown_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    plan, prepared = _layout(tmp_path)

    def known(**_kwargs: Any) -> None:
        raise maintenance_executor.MaintenanceExecutorError("health_failed")

    monkeypatch.setattr(maintenance_cli, "execute_prepared", known)
    assert maintenance_cli.main(_execute_argv(plan, prepared)) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error_code": "health_failed",
        "ok": False,
    }

    def unknown(**_kwargs: Any) -> None:
        raise RuntimeError("private /path and token")

    monkeypatch.setattr(maintenance_cli, "execute_prepared", unknown)
    assert maintenance_cli.main(_execute_argv(plan, prepared)) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error_code": "controller_failed",
        "ok": False,
    }


class _Response:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers = {"content-length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.payload[:size]


def test_ready_probe_uses_fixed_local_url_timeout_and_bounded_json() -> None:
    calls: list[tuple[Any, float]] = []
    payload = json.dumps({"ready": True, "status": "ready"}).encode("ascii")

    def opener(request: Any, *, timeout: float):
        calls.append((request, timeout))
        return _Response(payload)

    assert maintenance_cli.probe_ready(
        maintenance_executor.READY_URL, 1.25, opener=opener,
    ) == (200, {"ready": True, "status": "ready"})
    assert calls[0][0].full_url == maintenance_executor.READY_URL
    assert calls[0][0].headers["Accept"] == "application/json"
    assert calls[0][1] == 1.25

    with pytest.raises(maintenance_cli.MaintenanceCliError, match="ready_url_invalid"):
        maintenance_cli.probe_ready("https://example.com", 1.0, opener=opener)
    with pytest.raises(maintenance_cli.MaintenanceCliError, match="ready_payload_invalid"):
        maintenance_cli.probe_ready(
            maintenance_executor.READY_URL,
            1.0,
            opener=lambda *_args, **_kwargs: _Response(
                b"x" * (maintenance_cli.MAX_READY_BYTES + 1)
            ),
        )


def test_default_execution_dependencies_are_real_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, prepared = _layout(tmp_path)
    captured: dict[str, Any] = {}

    def runtime(_request: maintenance_executor.MaintenanceRequest, **kwargs: Any):
        captured.update(kwargs)
        return {"journal": {"stage": "committed"}}

    monkeypatch.setattr(maintenance_cli.maintenance_runtime, "execute_maintenance", runtime)
    maintenance_cli.execute_prepared(
        plan=plan, prepared=prepared, request_id="request-1"
    )

    assert captured["runner"] is subprocess.run
    assert captured["ready_probe"] is maintenance_cli.probe_ready
    assert captured["target_probe"] is maintenance_cli.probe_target_schema
