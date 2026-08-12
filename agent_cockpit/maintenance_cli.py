"""Release-external CLI for the short maintenance-window controller."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import generation_prepare
from . import maintenance_controller
from . import maintenance_executor
from . import maintenance_request
from . import maintenance_runtime
from . import maintenance_schema_probe


MAX_READY_BYTES = 64 * 1024
_ERROR_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class MaintenanceCliError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise MaintenanceCliError(code)


def probe_ready(
    url: str,
    timeout: float,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[int, Mapping[str, Any]]:
    """Read one bounded JSON response from the fixed local readiness endpoint."""
    if (
        url != maintenance_executor.READY_URL
        or type(timeout) not in {int, float}
        or timeout <= 0
        or not callable(opener)
    ):
        _fail("ready_url_invalid")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        try:
            response = opener(request, timeout=float(timeout))
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            status = getattr(response, "status", getattr(response, "code", None))
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except (TypeError, ValueError):
                    _fail("ready_payload_invalid")
                if declared < 0 or declared > MAX_READY_BYTES:
                    _fail("ready_payload_invalid")
            else:
                declared = None
            raw = response.read(MAX_READY_BYTES + 1)
    except MaintenanceCliError:
        raise
    except Exception:
        _fail("ready_unavailable")
    if (
        type(status) is not int
        or not isinstance(raw, bytes)
        or not raw
        or len(raw) > MAX_READY_BYTES
        or (declared is not None and declared != len(raw))
    ):
        _fail("ready_payload_invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError):
        _fail("ready_payload_invalid")
    if not isinstance(payload, dict):
        _fail("ready_payload_invalid")
    return status, payload


def probe_target_schema(
    request: maintenance_executor.MaintenanceRequest,
    inventory_path: Path,
    inventory_sha256: str,
) -> dict[str, object]:
    return maintenance_schema_probe.probe_target_schema(
        request, inventory_path, inventory_sha256
    )


def execute_prepared(
    *,
    plan: maintenance_controller.ControllerPlan,
    prepared: generation_prepare.PreparedGeneration,
    request_id: str,
    runner: maintenance_executor.Runner = subprocess.run,
    ready_probe: maintenance_executor.ReadyProbe = probe_ready,
    target_probe: maintenance_runtime.TargetProbe = probe_target_schema,
) -> dict[str, Any]:
    request = maintenance_request.build_maintenance_request(
        plan=plan,
        prepared=prepared,
        request_id=request_id,
    )
    return maintenance_runtime.execute_maintenance(
        request,
        runner=runner,
        ready_probe=ready_probe,
        target_probe=target_probe,
    )


def _add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--deploy-root", required=True, type=Path)
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--controller-root", required=True, type=Path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    _add_plan_arguments(status)
    execute = commands.add_parser("execute")
    _add_plan_arguments(execute)
    execute.add_argument("--request-id", required=True)
    execute.add_argument("--version", required=True)
    execute.add_argument("--source-sha", required=True)
    execute.add_argument("--artifact-digest", required=True)
    execute.add_argument("--generation-id", required=True)
    execute.add_argument("--generation-path", required=True, type=Path)
    execute.add_argument("--launcher-path", required=True, type=Path)
    return parser


def _plan(args: argparse.Namespace) -> maintenance_controller.ControllerPlan:
    return maintenance_controller.build_controller_plan(
        state_root=args.state_root,
        deploy_root=args.deploy_root,
        current=args.current,
        controller_root=args.controller_root,
    )


def _prepared(args: argparse.Namespace) -> generation_prepare.PreparedGeneration:
    return generation_prepare.PreparedGeneration(
        version=args.version,
        source_sha=args.source_sha,
        artifact_digest=args.artifact_digest,
        generation_id=args.generation_id,
        generation_path=args.generation_path,
        launcher_path=args.launcher_path,
    )


def _write(payload: dict[str, object]) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if type(code) is str and _ERROR_RE.fullmatch(code):
        return code
    return "controller_failed"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = _plan(args)
        if args.command == "status":
            status = maintenance_controller.read_controller_status(plan)
            _write({"ok": True, **status})
            return 0
        result = execute_prepared(
            plan=plan,
            prepared=_prepared(args),
            request_id=args.request_id,
        )
        journal = result.get("journal")
        if not isinstance(journal, dict):
            _fail("controller_result_invalid")
        state = journal.get("stage")
        request_id = journal.get("request_id")
        if type(state) is not str or type(request_id) is not str:
            _fail("controller_result_invalid")
        _write({"ok": True, "request_id": request_id, "state": state})
        return 0
    except Exception as exc:
        _write({"error_code": _error_code(exc), "ok": False})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_READY_BYTES",
    "MaintenanceCliError",
    "execute_prepared",
    "main",
    "probe_ready",
    "probe_target_schema",
]
