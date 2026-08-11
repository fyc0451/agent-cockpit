from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import generation_switch
import maintenance_controller
import maintenance_evidence
import maintenance_executor as executor
import maintenance_services
import upgrade_journal


TARGET = generation_switch.GenerationIdentity("a" * 40, "1" * 64)
PREVIOUS = generation_switch.GenerationIdentity("b" * 40, "2" * 64)


def _request(tmp_path: Path, request_id: str = "request-1") -> executor.MaintenanceRequest:
    deploy = tmp_path / "deploy"
    generations = deploy / "generations"
    generations.mkdir(parents=True, mode=0o700)
    for identity in (TARGET, PREVIOUS):
        (generations / identity.generation_id).mkdir(mode=0o700)
    (deploy / "current").symlink_to(Path("generations") / PREVIOUS.generation_id)
    state = tmp_path / "controller-state"
    state.mkdir(mode=0o700)
    controller_root = tmp_path / "controller-install"
    controller_root.mkdir(mode=0o700)
    plan = maintenance_controller.build_controller_plan(
        state_root=state,
        deploy_root=deploy,
        current=deploy / "current",
        controller_root=controller_root,
    )
    return executor.MaintenanceRequest(
        plan=plan,
        request_id=request_id,
        target_version="2.0.0",
        target=TARGET,
        previous_version="1.0.0",
        previous=PREVIOUS,
        target_root=generations / TARGET.generation_id,
        snapshot_root=state / executor.SNAPSHOT_DIR_NAME / request_id,
        evidence_path=maintenance_evidence.evidence_binding_path(
            plan=plan,
            request_id=request_id,
            role="target",
            generation=TARGET,
        ),
    )


class Harness:
    def __init__(self, request: executor.MaintenanceRequest) -> None:
        self.request = request
        self.calls: list[list[str]] = []
        self.events: list[tuple[str, object]] = []
        self.states = {
            maintenance_services.COCKPIT_UNIT: "active",
            maintenance_services.MAIL_UNIT: "active",
        }
        self.fail_once: tuple[str, str] | None = None
        self.fail_after_mutation = False
        self.reject_target_ready = False
        self.binding_calls: list[tuple[str, str | None]] = []
        self.active_role = "previous"
        self.active_override: executor.EvidenceBinding | None = None
        self.invalid_binding = False
        self.fail_prepare_after_persist = False
        self.fail_activate_after_persist = False
        self.prepare_observations: list[dict[str, object]] = []
        self.bindings = {
            "previous": executor.EvidenceBinding(
                request.request_id,
                "previous",
                request.previous,
                maintenance_evidence.evidence_binding_path(
                    plan=request.plan,
                    request_id=request.request_id,
                    role="previous",
                    generation=request.previous,
                ),
                "d" * 64,
            ),
            "target": executor.EvidenceBinding(
                request.request_id,
                "target",
                request.target,
                maintenance_evidence.evidence_binding_path(
                    plan=request.plan,
                    request_id=request.request_id,
                    role="target",
                    generation=request.target,
                ),
                "e" * 64,
            ),
        }

    def runner(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        action = argv[2]
        unit = argv[3]
        if action == "show":
            return subprocess.CompletedProcess(argv, 0, self.states[unit] + "\n", "")
        assert action in {"start", "stop"}
        if self.fail_once == (action, unit):
            self.fail_once = None
            if self.fail_after_mutation:
                self.states[unit] = "active" if action == "start" else "inactive"
                self.events.append((action, unit))
            return subprocess.CompletedProcess(argv, 1, "", "private stderr")
        self.states[unit] = "active" if action == "start" else "inactive"
        self.events.append((action, unit))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def ready(self, _url: str, _timeout: float) -> tuple[int, dict[str, Any]]:
        generation = executor.inspect_current_generation(self.request.plan)
        if generation == self.request.target:
            if self.reject_target_ready:
                return 503, {"ready": False}
            version, identity = self.request.target_version, self.request.target
        else:
            version, identity = self.request.previous_version, self.request.previous
        return 200, {
            "status": "ready",
            "ready": True,
            "identity": {
                "version": version,
                "source_sha": identity.source_sha,
                "edition": "server",
                "instance_id": "instance-1",
            },
        }

    def prepare_target(self, request: executor.MaintenanceRequest) -> None:
        assert request == self.request
        self.binding_calls.append(("prepare_target", None))
        self.events.append(("prepare_target", None))
        journal = upgrade_journal.load_journal(root=self.request.plan.journal_root)
        self.prepare_observations.append(
            {
                "states": dict(self.states),
                "stage": journal["stage"],
                "intent": journal["intent"],
                "current": executor.inspect_current_generation(self.request.plan),
            }
        )
        if self.fail_prepare_after_persist:
            raise OSError("fsync after durable binding")

    def activate(self, request_id: str, role: str) -> None:
        assert request_id == self.request.request_id
        self.binding_calls.append(("activate", role))
        self.active_role = role
        if self.fail_activate_after_persist:
            raise OSError("fsync after durable selector")

    def read(self, request_id: str, role: str) -> executor.EvidenceBinding:
        assert request_id == self.request.request_id
        self.binding_calls.append(("read", role))
        if role == "active" and self.active_override is not None:
            binding = self.active_override
            self.active_override = None
            return binding
        selected = self.active_role if role == "active" else role
        binding = self.bindings[selected]
        if self.invalid_binding:
            return executor.EvidenceBinding(
                binding.request_id,
                binding.role,
                TARGET if binding.identity == PREVIOUS else PREVIOUS,
                binding.evidence_path,
                binding.evidence_sha256,
            )
        return binding

    def mutations(self) -> list[tuple[str, str]]:
        return [
            (argv[2], argv[3])
            for argv in self.calls
            if argv[2] in {"start", "stop"}
        ]


def _execute(
    request: executor.MaintenanceRequest,
    harness: Harness,
) -> dict[str, Any]:
    return executor.execute_prepared_generation(
        request,
        runner=harness.runner,
        ready_probe=harness.ready,
        prepare_target=harness.prepare_target,
        activate_binding=harness.activate,
        read_binding=harness.read,
    )


def _activate_target(request: executor.MaintenanceRequest) -> None:
    generation_switch.activate_generation(
        request.plan.deploy_root,
        request.target,
        expected_previous=request.previous,
    )


def _journal_at(
    request: executor.MaintenanceRequest,
    stage: str,
    intent: str | None,
) -> dict[str, Any]:
    common = {
        "root": request.plan.journal_root,
        "request_id": request.request_id,
        "target_digest": request.target.artifact_digest,
    }
    journal = upgrade_journal.create_journal(**common)
    if stage == "prepared":
        return journal
    journal = upgrade_journal.advance_journal(**common, stage="services_stopped")
    if stage == "services_stopped" and intent is None:
        return journal
    journal = upgrade_journal.record_switch_intent(
        **common,
        target_source_sha=request.target.source_sha,
        previous_generation=request.previous.generation_id,
    )
    if stage == "services_stopped":
        return journal
    _activate_target(request)
    journal = upgrade_journal.advance_journal(**common, stage="switched")
    if stage == "switched" and intent is None:
        return journal
    journal = upgrade_journal.record_intent(
        **common, intent=upgrade_journal.INTENT_START_SERVICES
    )
    if stage == "switched":
        return journal
    journal = upgrade_journal.advance_journal(**common, stage="services_started")
    if intent == upgrade_journal.INTENT_COMMIT:
        journal = upgrade_journal.record_intent(
            **common, intent=upgrade_journal.INTENT_COMMIT
        )
    return journal


def test_default_wiring_fails_before_request_access_or_side_effect() -> None:
    class ExplosiveRequest:
        def __getattribute__(self, _name: str) -> Any:
            raise AssertionError("request must not be accessed while unwired")

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        executor.execute_prepared_generation(ExplosiveRequest())  # type: ignore[arg-type]
    assert exc.value.code == "wiring_unavailable"


def test_fresh_current_drift_is_zero_journal_runner_or_binding_mutation(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request.plan.current.unlink()
    request.plan.current.symlink_to(Path("generations") / request.target.generation_id)
    harness = Harness(request)

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "current_drift"
    assert not request.plan.journal_root.exists()
    assert harness.calls == []
    assert harness.binding_calls == []


@pytest.mark.parametrize("runner", [None, False, 1, "runner"])
def test_noncallable_runner_is_unwired_without_lock(
    tmp_path: Path, runner: object
) -> None:
    request = _request(tmp_path)
    harness = Harness(request)
    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        executor.execute_prepared_generation(
            request,
            runner=runner,  # type: ignore[arg-type]
            ready_probe=harness.ready,
            prepare_target=harness.prepare_target,
            activate_binding=harness.activate,
            read_binding=harness.read,
        )
    assert exc.value.code == "wiring_unavailable"
    assert tuple(request.plan.state_root.iterdir()) == ()


def test_invalid_binding_fails_before_service_truth_or_mutation(tmp_path: Path) -> None:
    request = _request(tmp_path)
    harness = Harness(request)
    harness.invalid_binding = True

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "binding_invalid"
    assert harness.calls == []
    assert executor.inspect_current_generation(request.plan) == PREVIOUS


@pytest.mark.parametrize("damage", ["path", "digest", "request_id"])
def test_target_binding_path_digest_and_request_are_exact_then_roll_back(
    tmp_path: Path, damage: str
) -> None:
    request = _request(tmp_path)
    harness = Harness(request)
    binding = harness.bindings["target"]
    values = dict(binding.__dict__)
    values[damage if damage != "digest" else "evidence_sha256"] = (
        tmp_path / "wrong.json"
        if damage == "path"
        else "bad"
        if damage == "digest"
        else "other-request"
    )
    if damage == "path":
        values["evidence_path"] = values.pop("path")
    harness.bindings["target"] = executor.EvidenceBinding(**values)

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "binding_invalid"
    assert upgrade_journal.load_journal(root=request.plan.journal_root)["stage"] == (
        "rolled_back"
    )
    assert executor.inspect_current_generation(request.plan) == PREVIOUS
    assert harness.states == {
        maintenance_services.COCKPIT_UNIT: "active",
        maintenance_services.MAIL_UNIT: "active",
    }
    assert harness.prepare_observations[0]["states"] == {
        maintenance_services.COCKPIT_UNIT: "inactive",
        maintenance_services.MAIL_UNIT: "inactive",
    }


def test_happy_path_commits_with_fixed_service_order(tmp_path: Path) -> None:
    request = _request(tmp_path)
    harness = Harness(request)

    result = _execute(request, harness)

    assert result["journal"]["stage"] == "committed"
    assert result["evidence_path"] == request.evidence_path
    assert result["evidence_sha256"] == "e" * 64
    assert executor.inspect_current_generation(request.plan) == TARGET
    assert harness.active_role == "target"
    assert harness.mutations() == [
        ("stop", maintenance_services.COCKPIT_UNIT),
        ("stop", maintenance_services.MAIL_UNIT),
        ("start", maintenance_services.MAIL_UNIT),
        ("start", maintenance_services.COCKPIT_UNIT),
    ]


def test_real_flat_binding_paths_are_accepted(tmp_path: Path) -> None:
    request = _request(tmp_path)

    def flat(role: str, identity: generation_switch.GenerationIdentity) -> Path:
        name = (
            hashlib.sha256(request.request_id.encode("utf-8")).hexdigest()
            + f"-{role}-{identity.generation_id}.json"
        )
        return request.plan.state_root / executor.EVIDENCE_DIR_NAME / name

    request = replace(request, evidence_path=flat("target", request.target))
    harness = Harness(request)
    harness.bindings["previous"] = replace(
        harness.bindings["previous"],
        evidence_path=flat("previous", request.previous),
    )
    harness.bindings["target"] = replace(
        harness.bindings["target"],
        evidence_path=request.evidence_path,
    )

    assert _execute(request, harness)["journal"]["stage"] == "committed"


def test_legacy_nested_binding_paths_are_rejected(tmp_path: Path) -> None:
    request = _request(tmp_path)
    nested_root = request.plan.state_root / executor.EVIDENCE_DIR_NAME / request.request_id
    legacy = replace(request, evidence_path=nested_root / "target.json")
    harness = Harness(legacy)
    harness.bindings["previous"] = replace(
        harness.bindings["previous"], evidence_path=nested_root / "previous.json"
    )
    harness.bindings["target"] = replace(
        harness.bindings["target"], evidence_path=nested_root / "target.json"
    )

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(legacy, harness)

    assert exc.value.code == "request_invalid"
    assert not legacy.plan.journal_root.exists()
    assert harness.calls == []
    assert harness.binding_calls == []


def test_legacy_nested_previous_binding_is_rejected_before_journal(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    harness = Harness(request)
    previous = harness.bindings["previous"]
    harness.bindings["previous"] = replace(
        previous,
        evidence_path=request.plan.state_root
        / executor.EVIDENCE_DIR_NAME
        / request.request_id
        / "previous.json",
    )

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "binding_invalid"
    assert not request.plan.journal_root.exists()
    assert harness.calls == []


def test_legacy_nested_target_binding_is_rejected_and_rolled_back(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    harness = Harness(request)
    target = harness.bindings["target"]
    harness.bindings["target"] = replace(
        target,
        evidence_path=request.plan.state_root
        / executor.EVIDENCE_DIR_NAME
        / request.request_id
        / "target.json",
    )

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "binding_invalid"
    assert upgrade_journal.load_journal(root=request.plan.journal_root)["stage"] == (
        "rolled_back"
    )
    assert executor.inspect_current_generation(request.plan) == request.previous


def test_legacy_nested_active_binding_is_rejected_and_rolled_back(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    _journal_at(request, "services_started", None)
    harness = Harness(request)
    harness.active_role = "target"
    target = harness.bindings["target"]
    harness.active_override = replace(
        target,
        evidence_path=request.plan.state_root
        / executor.EVIDENCE_DIR_NAME
        / request.request_id
        / "target.json",
    )

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "binding_invalid"
    assert upgrade_journal.load_journal(root=request.plan.journal_root)["stage"] == (
        "rolled_back"
    )
    assert executor.inspect_current_generation(request.plan) == request.previous


def test_cold_snapshot_and_target_gate_only_after_both_services_inactive(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    harness = Harness(request)

    assert _execute(request, harness)["journal"]["stage"] == "committed"

    assert harness.prepare_observations == [
        {
            "states": {
                maintenance_services.COCKPIT_UNIT: "inactive",
                maintenance_services.MAIL_UNIT: "inactive",
            },
            "stage": "services_stopped",
            "intent": None,
            "current": PREVIOUS,
        }
    ]
    assert harness.events[:3] == [
        ("stop", maintenance_services.COCKPIT_UNIT),
        ("stop", maintenance_services.MAIL_UNIT),
        ("prepare_target", None),
    ]


@pytest.mark.parametrize("failure", ["prepare", "activate"])
def test_binding_durable_write_then_error_reconciles_without_repeating(
    tmp_path: Path, failure: str
) -> None:
    request = _request(tmp_path)
    harness = Harness(request)
    if failure == "prepare":
        harness.fail_prepare_after_persist = True
    else:
        harness.fail_activate_after_persist = True

    result = _execute(request, harness)

    assert result["journal"]["stage"] == "committed"
    assert harness.active_role == "target"


@pytest.mark.parametrize(
    ("stage", "intent", "states", "active_role"),
    [
        ("prepared", upgrade_journal.INTENT_STOP_SERVICES, "active", "previous"),
        ("services_stopped", None, "inactive", "previous"),
        (
            "services_stopped",
            upgrade_journal.INTENT_SWITCH_CURRENT,
            "inactive",
            "previous",
        ),
        ("switched", None, "inactive", "previous"),
        ("switched", upgrade_journal.INTENT_START_SERVICES, "inactive", "target"),
        ("services_started", None, "active", "target"),
        (
            "services_started",
            upgrade_journal.INTENT_COMMIT,
            "active",
            "target",
        ),
    ],
)
def test_each_durable_boundary_crash_resumes_to_commit(
    tmp_path: Path,
    stage: str,
    intent: str | None,
    states: str,
    active_role: str,
) -> None:
    request = _request(tmp_path)
    _journal_at(request, stage, intent)
    harness = Harness(request)
    harness.states = {unit: states for unit in maintenance_services.STOP_ORDER}
    harness.active_role = active_role

    result = _execute(request, harness)

    assert result["journal"]["stage"] == "committed"
    assert executor.inspect_current_generation(request.plan) == TARGET
    assert harness.active_role == "target"


def test_switch_intent_with_already_changed_current_does_not_switch_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    _journal_at(
        request, "services_stopped", upgrade_journal.INTENT_SWITCH_CURRENT
    )
    _activate_target(request)
    harness = Harness(request)
    harness.states = {unit: "inactive" for unit in maintenance_services.STOP_ORDER}
    monkeypatch.setattr(
        executor.generation_switch,
        "activate_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must reconcile current without a second switch")
        ),
    )

    assert _execute(request, harness)["journal"]["stage"] == "committed"


def test_prepared_partial_stop_resume_only_stops_active_unit(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _journal_at(request, "prepared", upgrade_journal.INTENT_STOP_SERVICES)
    harness = Harness(request)
    harness.states[maintenance_services.COCKPIT_UNIT] = "inactive"

    assert _execute(request, harness)["journal"]["stage"] == "committed"
    assert harness.mutations()[0] == ("stop", maintenance_services.MAIL_UNIT)
    assert ("stop", maintenance_services.COCKPIT_UNIT) not in harness.mutations()


def test_subset_stop_response_loss_reconciles_truth_without_replaying_unit(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    _journal_at(request, "prepared", upgrade_journal.INTENT_STOP_SERVICES)
    harness = Harness(request)
    harness.states[maintenance_services.COCKPIT_UNIT] = "inactive"
    harness.fail_once = ("stop", maintenance_services.MAIL_UNIT)
    harness.fail_after_mutation = True

    assert _execute(request, harness)["journal"]["stage"] == "committed"
    assert harness.mutations().count(
        ("stop", maintenance_services.MAIL_UNIT)
    ) == 1
    assert ("stop", maintenance_services.COCKPIT_UNIT) not in harness.mutations()


def test_subset_start_response_loss_reconciles_truth_without_replaying_unit(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    _journal_at(request, "switched", upgrade_journal.INTENT_START_SERVICES)
    harness = Harness(request)
    harness.states = {
        maintenance_services.MAIL_UNIT: "active",
        maintenance_services.COCKPIT_UNIT: "inactive",
    }
    harness.active_role = "target"
    harness.fail_once = ("start", maintenance_services.COCKPIT_UNIT)
    harness.fail_after_mutation = True

    assert _execute(request, harness)["journal"]["stage"] == "committed"
    assert harness.mutations().count(
        ("start", maintenance_services.COCKPIT_UNIT)
    ) == 1
    assert ("start", maintenance_services.MAIL_UNIT) not in harness.mutations()


def test_switched_partial_start_resume_only_starts_inactive_unit(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _journal_at(request, "switched", upgrade_journal.INTENT_START_SERVICES)
    harness = Harness(request)
    harness.states = {
        maintenance_services.MAIL_UNIT: "active",
        maintenance_services.COCKPIT_UNIT: "inactive",
    }
    harness.active_role = "target"

    assert _execute(request, harness)["journal"]["stage"] == "committed"
    assert harness.mutations() == [("start", maintenance_services.COCKPIT_UNIT)]


def test_partial_stop_failure_uses_affected_truth_for_exact_compensation(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    harness = Harness(request)
    harness.fail_once = ("stop", maintenance_services.MAIL_UNIT)

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "stop_services_failed"
    journal = upgrade_journal.load_journal(root=request.plan.journal_root)
    assert journal["stage"] == "rolled_back"
    assert journal["primary_error_code"] == "stop_services_failed"
    assert harness.states == {
        maintenance_services.COCKPIT_UNIT: "active",
        maintenance_services.MAIL_UNIT: "active",
    }
    assert harness.mutations() == [
        ("stop", maintenance_services.COCKPIT_UNIT),
        ("stop", maintenance_services.MAIL_UNIT),
        ("stop", maintenance_services.MAIL_UNIT),
        ("start", maintenance_services.MAIL_UNIT),
        ("start", maintenance_services.COCKPIT_UNIT),
    ]


def test_ambiguous_start_failure_reconciles_truth_then_rolls_back(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    harness = Harness(request)
    harness.fail_once = ("start", maintenance_services.MAIL_UNIT)
    harness.fail_after_mutation = True

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "start_services_failed"
    journal = upgrade_journal.load_journal(root=request.plan.journal_root)
    assert (journal["stage"], journal["primary_error_code"]) == (
        "rolled_back",
        "start_services_failed",
    )
    assert executor.inspect_current_generation(request.plan) == PREVIOUS
    assert harness.active_role == "previous"


def test_unknown_service_truth_never_starts_services(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _journal_at(request, "switched", upgrade_journal.INTENT_START_SERVICES)
    harness = Harness(request)
    harness.states[maintenance_services.MAIL_UNIT] = "activating"

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "service_truth_unknown"
    assert not any(action == "start" for action, _unit in harness.mutations())


def test_existing_rollback_intent_converges_target_to_previous(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _journal_at(request, "switched", upgrade_journal.INTENT_START_SERVICES)
    common = {
        "root": request.plan.journal_root,
        "request_id": request.request_id,
        "target_digest": request.target.artifact_digest,
    }
    upgrade_journal.record_intent(
        **common,
        intent=upgrade_journal.INTENT_ROLLBACK,
        primary_error_code="start_services_failed",
    )
    harness = Harness(request)
    harness.states = {unit: "inactive" for unit in maintenance_services.STOP_ORDER}
    harness.active_role = "target"

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "start_services_failed"
    assert upgrade_journal.load_journal(root=request.plan.journal_root)["stage"] == (
        "rolled_back"
    )
    assert executor.inspect_current_generation(request.plan) == PREVIOUS
    assert harness.active_role == "previous"


def test_existing_rollback_intent_with_previous_current_never_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    _journal_at(request, "prepared", upgrade_journal.INTENT_STOP_SERVICES)
    common = {
        "root": request.plan.journal_root,
        "request_id": request.request_id,
        "target_digest": request.target.artifact_digest,
    }
    upgrade_journal.record_intent(
        **common,
        intent=upgrade_journal.INTENT_ROLLBACK,
        primary_error_code="stop_services_failed",
    )
    harness = Harness(request)
    monkeypatch.setattr(
        executor.generation_switch,
        "rollback_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("previous current must not be switched")
        ),
    )

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "stop_services_failed"
    assert executor.inspect_current_generation(request.plan) == PREVIOUS


@pytest.mark.parametrize("damage", ["missing", "invalid"])
def test_existing_rollback_uses_frozen_previous_when_target_binding_missing(
    tmp_path: Path, damage: str
) -> None:
    request = _request(tmp_path)
    _journal_at(request, "switched", upgrade_journal.INTENT_START_SERVICES)
    common = {
        "root": request.plan.journal_root,
        "request_id": request.request_id,
        "target_digest": request.target.artifact_digest,
    }
    upgrade_journal.record_intent(
        **common,
        intent=upgrade_journal.INTENT_ROLLBACK,
        primary_error_code="start_services_failed",
    )
    harness = Harness(request)
    harness.states = {unit: "inactive" for unit in maintenance_services.STOP_ORDER}
    harness.active_role = "target"
    if damage == "missing":
        del harness.bindings["target"]
    else:
        target = harness.bindings["target"]
        harness.bindings["target"] = replace(target, evidence_sha256="invalid")

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "start_services_failed"
    assert upgrade_journal.load_journal(root=request.plan.journal_root)["stage"] == (
        "rolled_back"
    )
    assert executor.inspect_current_generation(request.plan) == PREVIOUS
    assert harness.active_role == "previous"
    assert ("prepare_target", None) not in harness.binding_calls
    assert ("read", "target") not in harness.binding_calls


@pytest.mark.parametrize("stage", ["committed", "rolled_back"])
def test_terminal_response_loss_reentry_is_idempotent_without_mutation(
    tmp_path: Path, stage: str
) -> None:
    request = _request(tmp_path)
    common = {
        "root": request.plan.journal_root,
        "request_id": request.request_id,
        "target_digest": request.target.artifact_digest,
    }
    if stage == "committed":
        _journal_at(request, "services_started", upgrade_journal.INTENT_COMMIT)
        terminal = upgrade_journal.advance_journal(**common, stage="committed")
    else:
        _journal_at(request, "prepared", upgrade_journal.INTENT_STOP_SERVICES)
        upgrade_journal.record_intent(
            **common,
            intent=upgrade_journal.INTENT_ROLLBACK,
            primary_error_code="stop_services_failed",
        )
        terminal = upgrade_journal.advance_journal(**common, stage="rolled_back")
    harness = Harness(request)
    harness.states = {
        unit: "active" for unit in maintenance_services.STOP_ORDER
    }
    harness.active_role = "target" if stage == "committed" else "previous"
    before_current = executor.inspect_current_generation(request.plan)

    if stage == "committed":
        result = _execute(request, harness)
        assert result["journal"] == terminal
        assert result["evidence_path"] == request.evidence_path
    else:
        with pytest.raises(executor.MaintenanceExecutorError) as exc:
            _execute(request, harness)
        assert exc.value.code == "stop_services_failed"

    assert executor.inspect_current_generation(request.plan) == before_current
    assert harness.calls == []
    assert ("prepare_target", None) not in harness.binding_calls
    assert not any(call[0] == "activate" for call in harness.binding_calls)


def test_previous_binding_precheck_precedes_fresh_journal_and_external_mutation(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    harness = Harness(request)
    previous = harness.bindings["previous"]
    harness.bindings["previous"] = replace(previous, evidence_sha256="invalid")

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "binding_invalid"
    assert not request.plan.journal_root.exists()
    assert harness.calls == []
    assert harness.mutations() == []
    assert executor.inspect_current_generation(request.plan) == PREVIOUS
    assert ("prepare_target", None) not in harness.binding_calls
    assert not any(call[0] == "activate" for call in harness.binding_calls)


def _execute_with_lease(
    request: executor.MaintenanceRequest,
    harness: Harness,
    lease: maintenance_controller.ControllerLease,
) -> dict[str, Any]:
    return executor.execute_prepared_generation(
        request,
        runner=harness.runner,
        ready_probe=harness.ready,
        prepare_target=harness.prepare_target,
        activate_binding=harness.activate,
        read_binding=harness.read,
        controller_lease=lease,
    )


def test_injected_controller_lease_happy_path_never_reacquires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    harness = Harness(request)

    with maintenance_controller.controller_lock(request.plan) as lease:
        monkeypatch.setattr(
            executor.maintenance_controller,
            "controller_lock",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("executor must reuse the injected lease")
            ),
        )
        result = _execute_with_lease(request, harness, lease)
        maintenance_controller.require_controller_lease(
            plan=request.plan, lease=lease
        )

    assert result["journal"]["stage"] == "committed"


def test_injected_controller_lease_existing_rollback_never_reacquires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    _journal_at(request, "prepared", upgrade_journal.INTENT_STOP_SERVICES)
    common = {
        "root": request.plan.journal_root,
        "request_id": request.request_id,
        "target_digest": request.target.artifact_digest,
    }
    upgrade_journal.record_intent(
        **common,
        intent=upgrade_journal.INTENT_ROLLBACK,
        primary_error_code="stop_services_failed",
    )
    harness = Harness(request)

    with maintenance_controller.controller_lock(request.plan) as lease:
        monkeypatch.setattr(
            executor.maintenance_controller,
            "controller_lock",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("executor must reuse the injected lease")
            ),
        )
        with pytest.raises(executor.MaintenanceExecutorError) as exc:
            _execute_with_lease(request, harness, lease)
        maintenance_controller.require_controller_lease(
            plan=request.plan, lease=lease
        )

    assert exc.value.code == "stop_services_failed"
    assert upgrade_journal.load_journal(root=request.plan.journal_root)["stage"] == (
        "rolled_back"
    )


@pytest.mark.parametrize("stage", ["committed", "rolled_back"])
def test_injected_controller_lease_terminal_reentry_never_reacquires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    request = _request(tmp_path)
    common = {
        "root": request.plan.journal_root,
        "request_id": request.request_id,
        "target_digest": request.target.artifact_digest,
    }
    if stage == "committed":
        _journal_at(request, "services_started", upgrade_journal.INTENT_COMMIT)
        upgrade_journal.advance_journal(**common, stage="committed")
    else:
        _journal_at(request, "prepared", upgrade_journal.INTENT_STOP_SERVICES)
        upgrade_journal.record_intent(
            **common,
            intent=upgrade_journal.INTENT_ROLLBACK,
            primary_error_code="stop_services_failed",
        )
        upgrade_journal.advance_journal(**common, stage="rolled_back")
    harness = Harness(request)
    harness.active_role = "target" if stage == "committed" else "previous"

    with maintenance_controller.controller_lock(request.plan) as lease:
        monkeypatch.setattr(
            executor.maintenance_controller,
            "controller_lock",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("executor must reuse the injected lease")
            ),
        )
        if stage == "committed":
            assert _execute_with_lease(request, harness, lease)["journal"][
                "stage"
            ] == "committed"
        else:
            with pytest.raises(executor.MaintenanceExecutorError) as exc:
                _execute_with_lease(request, harness, lease)
            assert exc.value.code == "stop_services_failed"
        maintenance_controller.require_controller_lease(
            plan=request.plan, lease=lease
        )


def test_expired_controller_lease_fails_before_executor_side_effects(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    harness = Harness(request)
    with maintenance_controller.controller_lock(request.plan) as lease:
        pass

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute_with_lease(request, harness, lease)

    assert exc.value.code == "controller_lease_invalid"
    assert not request.plan.journal_root.exists()
    assert harness.calls == []
    assert harness.binding_calls == []


def test_wrong_plan_controller_lease_fails_before_executor_side_effects(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path / "request")
    other = _request(tmp_path / "other", request_id="other-request")
    harness = Harness(request)

    with maintenance_controller.controller_lock(other.plan) as lease:
        with pytest.raises(executor.MaintenanceExecutorError) as exc:
            _execute_with_lease(request, harness, lease)

    assert exc.value.code == "controller_lease_invalid"
    assert not request.plan.journal_root.exists()
    assert harness.calls == []
    assert harness.binding_calls == []


def test_mismatched_existing_journal_has_zero_runner_or_binding_calls(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    upgrade_journal.create_journal(
        root=request.plan.journal_root,
        request_id="other-request",
        target_digest=request.target.artifact_digest,
    )
    harness = Harness(request)

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(request, harness)

    assert exc.value.code == "journal_failed"
    assert harness.calls == []
    assert harness.binding_calls == []


def test_request_bound_paths_allow_a_second_upgrade_without_collision(
    tmp_path: Path,
) -> None:
    first = _request(tmp_path, "request-1")
    second = replace(
        first,
        request_id="request-2",
        snapshot_root=first.plan.state_root
        / executor.SNAPSHOT_DIR_NAME
        / "request-2",
        evidence_path=maintenance_evidence.evidence_binding_path(
            plan=first.plan,
            request_id="request-2",
            role="target",
            generation=first.target,
        ),
    )

    assert first.evidence_path != second.evidence_path
    assert first.evidence_path.parent == second.evidence_path.parent
    assert first.evidence_path.name.split("-", 1)[0] != second.evidence_path.name.split(
        "-", 1
    )[0]
    assert Harness(first).bindings["target"].evidence_path == first.evidence_path
    assert Harness(second).bindings["target"].evidence_path == second.evidence_path


def test_executor_does_not_implement_the_external_evidence_writer() -> None:
    source = Path(executor.__file__).read_text()
    assert "upgrade_snapshot" not in source
    assert "release_readiness" not in source
    assert "os.replace" not in source
    assert "write_evidence" not in source


@pytest.mark.parametrize("field", ["snapshot_root", "evidence_path", "target_root"])
def test_request_paths_are_exactly_bound_without_mutation(
    tmp_path: Path, field: str
) -> None:
    request = _request(tmp_path)
    values = dict(request.__dict__)
    values[field] = tmp_path / "attacker"
    forged = executor.MaintenanceRequest(**values)
    harness = Harness(request)

    with pytest.raises(executor.MaintenanceExecutorError) as exc:
        _execute(forged, harness)

    assert exc.value.code == "request_invalid"
    assert tuple(request.plan.state_root.iterdir()) == ()
