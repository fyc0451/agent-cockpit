"""Dormant fixed-argv adapter for a target generation's schema probe."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import generation_switch
from . import maintenance_controller
from . import maintenance_executor
from . import release_readiness
from . import upgrade_snapshot


SCHEMA_PROBE_TIMEOUT_SECONDS = 30
SCHEMA_PROBE_RELATIVE_PATH = Path("bin") / "agent-cockpit"

Runner = Callable[..., subprocess.CompletedProcess[bytes]]

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_VERSION_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)


class SchemaProbeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise SchemaProbeError(code)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _parse_canonical(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > release_readiness.MAX_EVIDENCE_BYTES:
        _fail("schema_probe_output_invalid")
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, ValueError, TypeError):
        _fail("schema_probe_output_invalid")
    if not isinstance(value, dict):
        _fail("schema_probe_output_invalid")
    try:
        canonical = release_readiness.canonical_evidence_bytes(value)
    except release_readiness.ReadinessEvidenceError:
        _fail("schema_probe_output_invalid")
    if canonical != raw:
        _fail("schema_probe_output_invalid")
    return value


def _validate_request(
    request: maintenance_executor.MaintenanceRequest,
    inventory_path: Path,
    inventory_sha256: str,
) -> None:
    if (
        not isinstance(request, maintenance_executor.MaintenanceRequest)
        or not isinstance(request.plan, maintenance_controller.ControllerPlan)
        or not isinstance(request.target, generation_switch.GenerationIdentity)
        or not isinstance(request.target_root, Path)
        or not request.target_root.is_absolute()
        or not isinstance(request.snapshot_root, Path)
        or not request.snapshot_root.is_absolute()
        or type(request.target_version) is not str
        or _VERSION_RE.fullmatch(request.target_version) is None
        or type(request.request_id) is not str
        or not request.request_id
        or not isinstance(inventory_path, Path)
        or type(inventory_sha256) is not str
        or _SHA256_RE.fullmatch(inventory_sha256) is None
    ):
        _fail("schema_probe_request_invalid")
    if (
        request.target_root
        != request.plan.deploy_root
        / "generations"
        / request.target.generation_id
        or request.snapshot_root
        != request.plan.state_root
        / maintenance_executor.SNAPSHOT_DIR_NAME
        / request.request_id
        or inventory_path
        != request.snapshot_root / upgrade_snapshot.INVENTORY_NAME
    ):
        _fail("schema_probe_request_invalid")


def probe_target_schema(
    request: maintenance_executor.MaintenanceRequest,
    inventory_path: Path,
    inventory_sha256: str,
    *,
    runner: Runner | None = None,
) -> dict[str, object]:
    """Run the target binary without a shell and accept only canonical evidence."""
    _validate_request(request, inventory_path, inventory_sha256)
    if runner is not None and not callable(runner):
        _fail("schema_probe_request_invalid")
    execute = subprocess.run if runner is None else runner
    argv = [
        str(request.target_root / SCHEMA_PROBE_RELATIVE_PATH),
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
    ]
    try:
        result = execute(
            argv,
            capture_output=True,
            check=False,
            shell=False,
            text=False,
            timeout=SCHEMA_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        _fail("schema_probe_timeout")
    except OSError:
        _fail("schema_probe_unavailable")
    except Exception:
        _fail("schema_probe_runner_error")
    if type(getattr(result, "returncode", None)) is not int:
        _fail("schema_probe_runner_error")
    if result.returncode != 0:
        _fail("schema_probe_failed")
    raw = getattr(result, "stdout", None)
    if not isinstance(raw, bytes):
        _fail("schema_probe_output_invalid")
    evidence = _parse_canonical(raw)
    if (
        evidence.get("target")
        != {
            "version": request.target_version,
            "source_sha": request.target.source_sha,
            "edition": "server",
        }
        or evidence.get("backup_inventory_sha256") != inventory_sha256
    ):
        _fail("schema_probe_output_invalid")
    return evidence


__all__ = [
    "SCHEMA_PROBE_RELATIVE_PATH",
    "SCHEMA_PROBE_TIMEOUT_SECONDS",
    "SchemaProbeError",
    "probe_target_schema",
]
