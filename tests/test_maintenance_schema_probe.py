from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import generation_switch
import maintenance_controller
import maintenance_evidence
import maintenance_executor
import maintenance_schema_probe
import release_readiness
import store_schema
import upgrade_snapshot


TARGET = generation_switch.GenerationIdentity("a" * 40, "1" * 64)


def _request(tmp_path: Path) -> maintenance_executor.MaintenanceRequest:
    deploy = tmp_path / "deploy"
    generations = deploy / "generations"
    target_root = generations / TARGET.generation_id
    previous = generation_switch.GenerationIdentity("b" * 40, "2" * 64)
    for identity in (TARGET, previous):
        (generations / identity.generation_id).mkdir(parents=True, mode=0o700)
    current = deploy / "current"
    current.symlink_to(Path("generations") / previous.generation_id)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    controller_root = tmp_path / "controller"
    controller_root.mkdir(mode=0o700)
    plan = maintenance_controller.build_controller_plan(
        state_root=state,
        deploy_root=deploy,
        current=current,
        controller_root=controller_root,
    )
    return maintenance_executor.MaintenanceRequest(
        plan=plan,
        request_id="request-1",
        target_version="2.0.0",
        target=TARGET,
        previous_version="1.0.0",
        previous=previous,
        target_root=target_root,
        snapshot_root=state / maintenance_executor.SNAPSHOT_DIR_NAME / "request-1",
        evidence_path=maintenance_evidence.evidence_binding_path(
            plan=plan,
            request_id="request-1",
            role="target",
            generation=TARGET,
        ),
    )


def _evidence(inventory_sha256: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "compat_family": store_schema.COMPAT_FAMILY,
        "target": {
            "version": "2.0.0",
            "source_sha": TARGET.source_sha,
            "edition": "server",
        },
        "release_manifest_sha256": "d" * 64,
        "backup_inventory_sha256": inventory_sha256,
        "stores": [],
    }


class Runner:
    def __init__(self, result: subprocess.CompletedProcess[bytes]) -> None:
        self.result = result
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, kwargs))
        return self.result


def test_probe_uses_fixed_argv_timeout_and_canonical_stdout(tmp_path: Path) -> None:
    request = _request(tmp_path)
    inventory_path = request.snapshot_root / upgrade_snapshot.INVENTORY_NAME
    inventory_sha256 = "f" * 64
    evidence = _evidence(inventory_sha256)
    runner = Runner(
        subprocess.CompletedProcess(
            [], 0, release_readiness.canonical_evidence_bytes(evidence), b""
        )
    )

    result = maintenance_schema_probe.probe_target_schema(
        request,
        inventory_path,
        inventory_sha256,
        runner=runner,
    )

    assert result == evidence
    assert runner.calls == [
        (
            [
                str(request.target_root / "bin" / "agent-cockpit"),
                "schema-probe",
                "--snapshot-root",
                str(request.snapshot_root),
                "--artifact-root",
                str(request.target_root),
                "--version",
                request.target_version,
                "--source-sha",
                request.target.source_sha,
                "--backup-inventory-path",
                str(inventory_path),
                "--backup-inventory-sha256",
                inventory_sha256,
            ],
            {
                "capture_output": True,
                "check": False,
                "shell": False,
                "text": False,
                "timeout": maintenance_schema_probe.SCHEMA_PROBE_TIMEOUT_SECONDS,
            },
        )
    ]


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(_evidence("f" * 64)).encode("ascii"),
        b'{"backup_inventory_sha256":"' + b"f" * 64 + b'","target":{},"target":{}}\n',
        b'{"value":NaN}\n',
        b"not-json\n",
    ],
)
def test_probe_rejects_noncanonical_or_ambiguous_stdout(
    tmp_path: Path, raw: bytes
) -> None:
    request = _request(tmp_path)
    runner = Runner(subprocess.CompletedProcess([], 0, raw, b""))

    with pytest.raises(maintenance_schema_probe.SchemaProbeError) as exc:
        maintenance_schema_probe.probe_target_schema(
            request,
            request.snapshot_root / upgrade_snapshot.INVENTORY_NAME,
            "f" * 64,
            runner=runner,
        )

    assert exc.value.code == "schema_probe_output_invalid"


@pytest.mark.parametrize("damage", ["target", "inventory"])
def test_probe_rejects_mismatched_output_identity(tmp_path: Path, damage: str) -> None:
    request = _request(tmp_path)
    evidence = _evidence("e" * 64 if damage == "inventory" else "f" * 64)
    if damage == "target":
        evidence["target"] = {
            "version": "9.9.9",
            "source_sha": TARGET.source_sha,
            "edition": "server",
        }
    runner = Runner(
        subprocess.CompletedProcess(
            [], 0, release_readiness.canonical_evidence_bytes(evidence), b""
        )
    )

    with pytest.raises(maintenance_schema_probe.SchemaProbeError) as exc:
        maintenance_schema_probe.probe_target_schema(
            request,
            request.snapshot_root / upgrade_snapshot.INVENTORY_NAME,
            "f" * 64,
            runner=runner,
        )

    assert exc.value.code == "schema_probe_output_invalid"


def test_probe_maps_nonzero_and_timeout_to_stable_errors(tmp_path: Path) -> None:
    request = _request(tmp_path)
    inventory_path = request.snapshot_root / upgrade_snapshot.INVENTORY_NAME
    failed = Runner(subprocess.CompletedProcess([], 1, b"", b"private detail"))

    with pytest.raises(maintenance_schema_probe.SchemaProbeError) as exc:
        maintenance_schema_probe.probe_target_schema(
            request, inventory_path, "f" * 64, runner=failed
        )
    assert exc.value.code == "schema_probe_failed"

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired([], 30)

    with pytest.raises(maintenance_schema_probe.SchemaProbeError) as exc:
        maintenance_schema_probe.probe_target_schema(
            request, inventory_path, "f" * 64, runner=timeout
        )
    assert exc.value.code == "schema_probe_timeout"


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (OSError("missing"), "schema_probe_unavailable"),
        (RuntimeError("runner"), "schema_probe_runner_error"),
    ],
)
def test_probe_maps_runner_failures_to_stable_errors(
    tmp_path: Path, failure: Exception, code: str
) -> None:
    request = _request(tmp_path)

    def failed(*_args: object, **_kwargs: object) -> None:
        raise failure

    with pytest.raises(maintenance_schema_probe.SchemaProbeError) as exc:
        maintenance_schema_probe.probe_target_schema(
            request,
            request.snapshot_root / upgrade_snapshot.INVENTORY_NAME,
            "f" * 64,
            runner=failed,
        )

    assert exc.value.code == code


@pytest.mark.parametrize("stdout", ["{}\n", b"x" * (release_readiness.MAX_EVIDENCE_BYTES + 1)])
def test_probe_rejects_nonbytes_or_oversized_stdout(
    tmp_path: Path, stdout: object
) -> None:
    request = _request(tmp_path)
    runner = Runner(subprocess.CompletedProcess([], 0, stdout, b""))  # type: ignore[arg-type]

    with pytest.raises(maintenance_schema_probe.SchemaProbeError) as exc:
        maintenance_schema_probe.probe_target_schema(
            request,
            request.snapshot_root / upgrade_snapshot.INVENTORY_NAME,
            "f" * 64,
            runner=runner,
        )

    assert exc.value.code == "schema_probe_output_invalid"


@pytest.mark.parametrize(
    ("inventory_path", "inventory_sha256"),
    [
        (Path("relative.json"), "f" * 64),
        (Path("/tmp/wrong.json"), "f" * 64),
        (Path("/placeholder"), "invalid"),
    ],
)
def test_probe_rejects_invalid_inventory_arguments_before_runner(
    tmp_path: Path, inventory_path: Path, inventory_sha256: str
) -> None:
    request = _request(tmp_path)
    if inventory_path == Path("/placeholder"):
        inventory_path = request.snapshot_root / upgrade_snapshot.INVENTORY_NAME
    runner = Runner(subprocess.CompletedProcess([], 0, b"{}\n", b""))

    with pytest.raises(maintenance_schema_probe.SchemaProbeError) as exc:
        maintenance_schema_probe.probe_target_schema(
            request, inventory_path, inventory_sha256, runner=runner
        )

    assert exc.value.code == "schema_probe_request_invalid"
    assert runner.calls == []


def test_probe_rejects_malformed_request_before_runner(tmp_path: Path) -> None:
    request = _request(tmp_path)
    malformed = replace(request, plan=object())  # type: ignore[arg-type]
    runner = Runner(subprocess.CompletedProcess([], 0, b"{}\n", b""))

    with pytest.raises(maintenance_schema_probe.SchemaProbeError) as exc:
        maintenance_schema_probe.probe_target_schema(
            malformed,
            request.snapshot_root / upgrade_snapshot.INVENTORY_NAME,
            "f" * 64,
            runner=runner,
        )

    assert exc.value.code == "schema_probe_request_invalid"
    assert runner.calls == []
