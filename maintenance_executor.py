"""Dormant executor for one prepared-generation maintenance window."""

from __future__ import annotations

import os
import re
import stat
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import generation_switch
import maintenance_controller
import maintenance_services
import upgrade_journal


EVIDENCE_DIR_NAME = "schema-evidence"
SNAPSHOT_DIR_NAME = "upgrade-snapshots"
READY_URL = "http://127.0.0.1:8790/health/ready"
READY_TIMEOUT_SECONDS = 30.0
READY_REQUEST_TIMEOUT_SECONDS = 2.0
_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_REQUEST_RE = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
_CURRENT_RE = re.compile(r"generations/([0-9a-f]{40})-([0-9a-f]{64})\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_DIRECTORY", 0
) | getattr(os, "O_NOFOLLOW", 0)

Runner = Callable[..., Any]
ReadyProbe = Callable[[str, float], tuple[int, Mapping[str, Any]]]
ActivateBinding = Callable[[str, str], None]
ReadBinding = Callable[[str, str], "EvidenceBinding"]


class MaintenanceExecutorError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _RolledBackError(MaintenanceExecutorError):
    pass


@dataclass(frozen=True)
class MaintenanceRequest:
    plan: maintenance_controller.ControllerPlan
    request_id: str
    target_version: str
    target: generation_switch.GenerationIdentity
    previous_version: str
    previous: generation_switch.GenerationIdentity
    target_root: Path
    snapshot_root: Path
    evidence_path: Path
    ready_url: str = READY_URL


@dataclass(frozen=True)
class EvidenceBinding:
    request_id: str
    role: str
    identity: generation_switch.GenerationIdentity
    evidence_path: Path
    evidence_sha256: str


PrepareTarget = Callable[[MaintenanceRequest], None]


def _fail(code: str) -> None:
    raise MaintenanceExecutorError(code)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate(request: MaintenanceRequest) -> None:
    if not isinstance(request, MaintenanceRequest):
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
    if (
        request.target_root
        != plan.deploy_root / "generations" / request.target.generation_id
        or request.snapshot_root
        != plan.state_root / SNAPSHOT_DIR_NAME / request.request_id
        or request.evidence_path
        != plan.state_root
        / EVIDENCE_DIR_NAME
        / request.request_id
        / "target.json"
        or request.ready_url != READY_URL
        or any(".." in path.parts for path in paths)
        or _inside(request.snapshot_root, plan.deploy_root)
        or _inside(request.evidence_path, plan.deploy_root)
    ):
        _fail("request_invalid")


def inspect_current_generation(
    plan: maintenance_controller.ControllerPlan,
) -> generation_switch.GenerationIdentity:
    """Pure-read, strict reconciliation seam for the canonical current link."""
    if (
        not isinstance(plan, maintenance_controller.ControllerPlan)
        or plan.current != plan.deploy_root / "current"
    ):
        _fail("current_invalid")
    generation_fd = -1
    try:
        before = plan.current.lstat()
        target = os.readlink(plan.current)
        after = plan.current.lstat()
        signature = lambda info: (info.st_dev, info.st_ino, info.st_uid, info.st_mode)
        match = _CURRENT_RE.fullmatch(target)
        if (
            not stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.getuid()
            or signature(before) != signature(after)
            or match is None
        ):
            _fail("current_invalid")
        identity = generation_switch.GenerationIdentity(match[1], match[2])
        generation_path = plan.deploy_root / target
        generation_before = generation_path.lstat()
        generation_fd = os.open(generation_path, _DIR_FLAGS)
        generation_opened = os.fstat(generation_fd)
        generation_after = generation_path.lstat()
        if (
            not stat.S_ISDIR(generation_opened.st_mode)
            or generation_opened.st_uid != os.getuid()
            or stat.S_IMODE(generation_opened.st_mode) & 0o022
            or signature(generation_before) != signature(generation_opened)
            or signature(generation_opened) != signature(generation_after)
            or os.readlink(plan.current) != target
            or signature(plan.current.lstat()) != signature(after)
        ):
            _fail("current_invalid")
        return identity
    except MaintenanceExecutorError:
        raise
    except (OSError, ValueError, TypeError, generation_switch.GenerationSwitchError):
        _fail("current_invalid")
    finally:
        if generation_fd >= 0:
            os.close(generation_fd)


def _read_binding(
    request: MaintenanceRequest, role: str, reader: ReadBinding
) -> EvidenceBinding:
    try:
        binding = reader(request.request_id, role)
    except Exception:
        _fail("binding_unavailable")
    expected = request.previous if role == "previous" else request.target
    expected_path = (
        request.plan.state_root
        / EVIDENCE_DIR_NAME
        / request.request_id
        / f"{role}.json"
    )
    if (
        type(binding) is not EvidenceBinding
        or binding.request_id != request.request_id
        or binding.role != role
        or binding.identity != expected
        or binding.evidence_path != expected_path
        or not binding.evidence_path.is_absolute()
        or type(binding.evidence_sha256) is not str
        or _SHA256_RE.fullmatch(binding.evidence_sha256) is None
        or _inside(binding.evidence_path, request.plan.deploy_root)
    ):
        _fail("binding_invalid")
    return binding


def _read_active_binding(
    request: MaintenanceRequest, expected_role: str, reader: ReadBinding
) -> EvidenceBinding:
    try:
        binding = reader(request.request_id, "active")
    except Exception:
        _fail("binding_unavailable")
    expected = request.previous if expected_role == "previous" else request.target
    expected_path = (
        request.plan.state_root
        / EVIDENCE_DIR_NAME
        / request.request_id
        / f"{expected_role}.json"
    )
    if (
        type(binding) is not EvidenceBinding
        or binding.request_id != request.request_id
        or binding.role != expected_role
        or binding.identity != expected
        or binding.evidence_path != expected_path
        or type(binding.evidence_sha256) is not str
        or _SHA256_RE.fullmatch(binding.evidence_sha256) is None
    ):
        _fail("binding_invalid")
    return binding


def _activate_binding(
    request: MaintenanceRequest,
    role: str,
    activator: ActivateBinding,
    reader: ReadBinding,
) -> EvidenceBinding:
    try:
        activator(request.request_id, role)
    except Exception:
        try:
            return _read_active_binding(request, role, reader)
        except MaintenanceExecutorError:
            _fail("binding_activation_failed")
    return _read_active_binding(request, role, reader)


def _common(request: MaintenanceRequest) -> dict[str, Any]:
    return {
        "root": request.plan.journal_root,
        "request_id": request.request_id,
        "target_digest": request.target.artifact_digest,
    }


def _load_matching(request: MaintenanceRequest) -> dict[str, Any]:
    try:
        value = upgrade_journal.load_journal(root=request.plan.journal_root)
    except upgrade_journal.UpgradeJournalError:
        _fail("journal_failed")
    if (
        value.get("request_id") != request.request_id
        or value.get("target_digest") != request.target.artifact_digest
    ):
        _fail("journal_failed")
    return value


def _load_existing(request: MaintenanceRequest) -> dict[str, Any] | None:
    try:
        value = upgrade_journal.load_journal(root=request.plan.journal_root)
    except upgrade_journal.UpgradeJournalError as exc:
        if exc.code == "journal_missing":
            return None
        _fail("journal_failed")
    if (
        value.get("request_id") != request.request_id
        or value.get("target_digest") != request.target.artifact_digest
    ):
        _fail("journal_failed")
    bound = (
        value.get("target_source_sha"),
        value.get("target_generation"),
        value.get("previous_generation"),
    )
    if bound != (None, None, None) and bound != (
        request.target.source_sha,
        request.target.generation_id,
        request.previous.generation_id,
    ):
        _fail("journal_failed")
    return value


def _journal(
    request: MaintenanceRequest,
    operation: Callable[..., dict[str, Any]],
    matches: Callable[[dict[str, Any]], bool],
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        return operation(**_common(request), **kwargs)
    except upgrade_journal.UpgradeJournalError:
        value = _load_matching(request)
        if matches(value):
            return value
        _fail("journal_failed")


def _create_journal(request: MaintenanceRequest) -> dict[str, Any]:
    return _journal(
        request,
        upgrade_journal.create_journal,
        lambda value: value.get("stage") == "prepared"
        and value.get("intent") == upgrade_journal.INTENT_STOP_SERVICES,
    )


def _advance(request: MaintenanceRequest, stage: str) -> dict[str, Any]:
    return _journal(
        request,
        upgrade_journal.advance_journal,
        lambda value: value.get("stage") == stage,
        stage=stage,
    )


def _record_intent(
    request: MaintenanceRequest, intent: str, primary_error_code: str | None = None
) -> dict[str, Any]:
    return _journal(
        request,
        upgrade_journal.record_intent,
        lambda value: value.get("intent") == intent
        and value.get("primary_error_code") == primary_error_code,
        intent=intent,
        primary_error_code=primary_error_code,
    )


def _record_switch(request: MaintenanceRequest) -> dict[str, Any]:
    return _journal(
        request,
        upgrade_journal.record_switch_intent,
        lambda value: value.get("intent") == upgrade_journal.INTENT_SWITCH_CURRENT
        and value.get("target_source_sha") == request.target.source_sha
        and value.get("previous_generation") == request.previous.generation_id,
        target_source_sha=request.target.source_sha,
        previous_generation=request.previous.generation_id,
    )


def _record_rollback_error(request: MaintenanceRequest) -> None:
    _journal(
        request,
        upgrade_journal.record_rollback_error,
        lambda value: value.get("rollback_error_code") == "rollback_failed",
        rollback_error_code="rollback_failed",
    )


def _begin_rollback_retry(request: MaintenanceRequest) -> dict[str, Any]:
    return _journal(
        request,
        upgrade_journal.begin_rollback_retry,
        lambda value: value.get("intent") == upgrade_journal.INTENT_ROLLBACK
        and value.get("rollback_error_code") is None,
    )


def _run_service_command(runner: Runner, argv: list[str]) -> Any:
    try:
        result = runner(
            argv,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=maintenance_services.COMMAND_TIMEOUT_SECONDS,
        )
    except Exception:
        _fail("service_truth_unknown")
    if type(getattr(result, "returncode", None)) is not int or result.returncode != 0:
        _fail("service_truth_unknown")
    return result


def _unit_state(runner: Runner, unit: str) -> str:
    result = _run_service_command(
        runner,
        [
            maintenance_services.SYSTEMCTL,
            "--user",
            "show",
            unit,
            "--property=ActiveState",
            "--value",
        ],
    )
    state = getattr(result, "stdout", None)
    if type(state) is not str or state.strip() not in {"active", "inactive"}:
        _fail("service_truth_unknown")
    return state.strip()


def _service_truth(runner: Runner) -> dict[str, str]:
    return {
        unit: _unit_state(runner, unit)
        for unit in maintenance_services.STOP_ORDER
    }


def _mutate_unit(runner: Runner, action: str, unit: str) -> None:
    expected = "active" if action == "start" else "inactive"
    try:
        _run_service_command(
            runner,
            [maintenance_services.SYSTEMCTL, "--user", action, unit],
        )
    except MaintenanceExecutorError:
        if _unit_state(runner, unit) == expected:
            return
        _fail(f"{action}_services_failed")
    if _unit_state(runner, unit) != expected:
        _fail("service_truth_unknown")


def _valid_service_failure(
    error: maintenance_services.ServiceMutationError,
    order: tuple[str, ...],
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> bool:
    affected = error.affected
    completed = error.completed
    return (
        affected == order[: len(affected)]
        and completed == affected[: len(completed)]
        and all(
            before[unit] == after[unit] or unit in affected
            for unit in order
        )
    )


def _ensure_services(runner: Runner, desired: str) -> dict[str, str]:
    if desired not in {"active", "inactive"}:
        _fail("service_truth_unknown")
    action = "start" if desired == "active" else "stop"
    order = (
        maintenance_services.START_ORDER
        if action == "start"
        else maintenance_services.STOP_ORDER
    )
    before = _service_truth(runner)
    needed = tuple(unit for unit in order if before[unit] != desired)
    if needed == order:
        operation = (
            maintenance_services.start_services
            if action == "start"
            else maintenance_services.stop_services
        )
        try:
            operation(runner=runner)
        except maintenance_services.ServiceMutationError as exc:
            after = _service_truth(runner)
            if not _valid_service_failure(exc, order, before, after):
                _fail("service_truth_unknown")
            if all(after[unit] == desired for unit in order):
                return after
            raise
    else:
        for unit in needed:
            _mutate_unit(runner, action, unit)
    after = _service_truth(runner)
    if any(after[unit] != desired for unit in order):
        _fail("service_truth_unknown")
    return after


def _assert_services(runner: Runner, expected: str) -> dict[str, str]:
    truth = _service_truth(runner)
    if any(state != expected for state in truth.values()):
        _fail("service_state_conflict")
    return truth


def _wait_ready(
    request: MaintenanceRequest,
    version: str,
    identity: generation_switch.GenerationIdentity,
    ready_probe: ReadyProbe,
) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            status, payload = ready_probe(
                request.ready_url, READY_REQUEST_TIMEOUT_SECONDS
            )
        except Exception:
            payload = {}
            status = 0
        actual = payload.get("identity") if isinstance(payload, Mapping) else None
        if (
            status == 200
            and payload.get("ready") is True
            and payload.get("status") == "ready"
            and isinstance(actual, Mapping)
            and actual.get("version") == version
            and actual.get("source_sha") == identity.source_sha
            and actual.get("edition") == "server"
            and type(actual.get("instance_id")) is str
            and actual.get("instance_id")
        ):
            return
        time.sleep(0.1)
    _fail("health_failed")


def _rollback(
    request: MaintenanceRequest,
    primary_error_code: str,
    runner: Runner,
    ready_probe: ReadyProbe,
    activate_binding: ActivateBinding,
    read_binding: ReadBinding,
) -> None:
    journal = _load_matching(request)
    if journal.get("intent") != upgrade_journal.INTENT_ROLLBACK:
        journal = _record_intent(
            request, upgrade_journal.INTENT_ROLLBACK, primary_error_code
        )
    if journal.get("rollback_error_code") is not None:
        _begin_rollback_retry(request)
    try:
        _ensure_services(runner, "inactive")
        current = inspect_current_generation(request.plan)
        if current == request.target:
            try:
                generation_switch.rollback_generation(
                    request.plan.deploy_root,
                    journal_previous=request.previous,
                    expected_current=request.target,
                )
            except generation_switch.GenerationSwitchError:
                if inspect_current_generation(request.plan) != request.previous:
                    raise
        elif current != request.previous:
            _fail("current_invalid")
        if inspect_current_generation(request.plan) != request.previous:
            _fail("current_invalid")
        _assert_services(runner, "inactive")
        _activate_binding(request, "previous", activate_binding, read_binding)
        _ensure_services(runner, "active")
        _wait_ready(request, request.previous_version, request.previous, ready_probe)
    except Exception:
        try:
            _record_rollback_error(request)
        except MaintenanceExecutorError:
            pass
        _fail("rollback_failed")
    _advance(request, "rolled_back")


def _entry_journal(request: MaintenanceRequest) -> dict[str, Any] | None:
    journal = _load_existing(request)
    if journal is None:
        if inspect_current_generation(request.plan) != request.previous:
            _fail("current_drift")
        return None
    if journal["stage"] in upgrade_journal.TERMINAL_STAGES:
        return journal
    current = inspect_current_generation(request.plan)
    stage = journal["stage"]
    intent = journal["intent"]
    if intent == upgrade_journal.INTENT_ROLLBACK:
        if current not in {request.previous, request.target}:
            _fail("current_drift")
    elif (
        stage == "services_stopped"
        and intent == upgrade_journal.INTENT_SWITCH_CURRENT
    ):
        if current not in {request.previous, request.target}:
            _fail("current_drift")
    else:
        expected = (
            request.previous
            if stage in {"prepared", "services_stopped"}
            else request.target
        )
        if current != expected:
            _fail("current_drift")
    return journal


def _drive(
    request: MaintenanceRequest,
    journal: dict[str, Any],
    *,
    runner: Runner,
    ready_probe: ReadyProbe,
    prepare_target: PrepareTarget,
    activate_binding: ActivateBinding,
    read_binding: ReadBinding,
) -> dict[str, Any]:
    while True:
        stage = journal["stage"]
        intent = journal["intent"]
        if intent == upgrade_journal.INTENT_ROLLBACK:
            primary = journal.get("primary_error_code")
            if type(primary) is not str:
                _fail("journal_failed")
            _rollback(
                request,
                primary,
                runner,
                ready_probe,
                activate_binding,
                read_binding,
            )
            raise _RolledBackError(primary)

        if stage == "prepared":
            if intent != upgrade_journal.INTENT_STOP_SERVICES:
                _fail("journal_failed")
            if inspect_current_generation(request.plan) != request.previous:
                _fail("current_drift")
            _read_active_binding(request, "previous", read_binding)
            try:
                _ensure_services(runner, "inactive")
            except maintenance_services.ServiceMutationError:
                _fail("stop_services_failed")
            journal = _advance(request, "services_stopped")
            continue

        if stage == "services_stopped":
            _assert_services(runner, "inactive")
            current = inspect_current_generation(request.plan)
            if intent is None:
                if current != request.previous:
                    _fail("current_drift")
                _read_active_binding(request, "previous", read_binding)
                try:
                    prepare_target(request)
                except Exception:
                    _read_binding(request, "target", read_binding)
                _read_binding(request, "target", read_binding)
                journal = _record_switch(request)
                continue
            if intent != upgrade_journal.INTENT_SWITCH_CURRENT:
                _fail("journal_failed")
            _read_binding(request, "target", read_binding)
            if current == request.previous:
                try:
                    generation_switch.activate_generation(
                        request.plan.deploy_root,
                        request.target,
                        expected_previous=request.previous,
                    )
                except generation_switch.GenerationSwitchError:
                    if inspect_current_generation(request.plan) != request.target:
                        _fail("switch_failed")
            elif current != request.target:
                _fail("current_drift")
            if inspect_current_generation(request.plan) != request.target:
                _fail("switch_failed")
            journal = _advance(request, "switched")
            continue

        if stage == "switched":
            if inspect_current_generation(request.plan) != request.target:
                _fail("current_drift")
            if intent is None:
                journal = _record_intent(
                    request, upgrade_journal.INTENT_START_SERVICES
                )
                continue
            if intent != upgrade_journal.INTENT_START_SERVICES:
                _fail("journal_failed")
            _read_binding(request, "target", read_binding)
            _activate_binding(request, "target", activate_binding, read_binding)
            try:
                _ensure_services(runner, "active")
            except maintenance_services.ServiceMutationError:
                _fail("start_services_failed")
            journal = _advance(request, "services_started")
            continue

        if stage == "services_started":
            if inspect_current_generation(request.plan) != request.target:
                _fail("current_drift")
            _assert_services(runner, "active")
            _read_active_binding(request, "target", read_binding)
            if intent is None:
                _wait_ready(request, request.target_version, request.target, ready_probe)
                journal = _record_intent(request, upgrade_journal.INTENT_COMMIT)
                continue
            if intent != upgrade_journal.INTENT_COMMIT:
                _fail("journal_failed")
            return _advance(request, "committed")

        _fail("journal_failed")


def execute_prepared_generation(
    request: MaintenanceRequest,
    *,
    runner: Runner | None = None,
    ready_probe: ReadyProbe | None = None,
    prepare_target: PrepareTarget | None = None,
    activate_binding: ActivateBinding | None = None,
    read_binding: ReadBinding | None = None,
) -> dict[str, Any]:
    """Execute one prepared generation; production wiring is intentionally absent."""
    if (
        not callable(runner)
        or not callable(ready_probe)
        or not callable(prepare_target)
        or not callable(activate_binding)
        or not callable(read_binding)
        or not sys.platform.startswith("linux")
    ):
        _fail("wiring_unavailable")
    _validate(request)
    try:
        with maintenance_controller.controller_lock(request.plan):
            journal = _entry_journal(request)
            if journal is not None and journal["stage"] == "committed":
                target_binding = _read_binding(request, "target", read_binding)
                return {
                    "journal": journal,
                    "evidence_path": target_binding.evidence_path,
                    "evidence_sha256": target_binding.evidence_sha256,
                }
            if journal is not None and journal["stage"] == "rolled_back":
                primary = journal.get("primary_error_code")
                if type(primary) is not str:
                    _fail("journal_failed")
                raise _RolledBackError(primary)
            _read_binding(request, "previous", read_binding)
            if journal is None:
                _read_active_binding(request, "previous", read_binding)
                journal = _create_journal(request)
            try:
                journal = _drive(
                    request,
                    journal,
                    runner=runner,
                    ready_probe=ready_probe,
                    prepare_target=prepare_target,
                    activate_binding=activate_binding,
                    read_binding=read_binding,
                )
                target_binding = _read_binding(request, "target", read_binding)
                return {
                    "journal": journal,
                    "evidence_path": target_binding.evidence_path,
                    "evidence_sha256": target_binding.evidence_sha256,
                }
            except Exception as exc:
                if isinstance(exc, _RolledBackError):
                    raise
                if isinstance(exc, MaintenanceExecutorError) and exc.code in {
                    "current_drift",
                    "journal_failed",
                    "service_truth_unknown",
                }:
                    raise
                code = exc.code if isinstance(exc, MaintenanceExecutorError) else (
                    "maintenance_failed"
                )
                _rollback(
                    request,
                    code,
                    runner,
                    ready_probe,
                    activate_binding,
                    read_binding,
                )
                _fail(code)
    except maintenance_controller.ControllerPreflightError as exc:
        _fail(exc.code)
    except MaintenanceExecutorError:
        raise
    except Exception:
        _fail("maintenance_failed")


__all__ = [
    "EvidenceBinding",
    "MaintenanceExecutorError",
    "MaintenanceRequest",
    "execute_prepared_generation",
    "inspect_current_generation",
]
