from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import pytest

import generation_switch
import maintenance_controller
import maintenance_evidence
import maintenance_executor
import maintenance_runtime
import maintenance_services
import upgrade_journal
import upgrade_snapshot


@pytest.fixture(autouse=True)
def _pin_linux_platform_for_runtime_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """execute() requires linux; pure fakes must pin under darwin runners."""
    monkeypatch.setattr(maintenance_executor.sys, "platform", "linux")
    monkeypatch.setattr(sys, "platform", "linux")


TARGET = generation_switch.GenerationIdentity("a" * 40, "1" * 64)
PREVIOUS = generation_switch.GenerationIdentity("b" * 40, "2" * 64)


def _request(tmp_path: Path) -> maintenance_executor.MaintenanceRequest:
    deploy = tmp_path / "deploy"
    generations = deploy / "generations"
    generations.mkdir(parents=True, mode=0o700)
    for identity in (TARGET, PREVIOUS):
        (generations / identity.generation_id).mkdir(mode=0o700)
    current = deploy / "current"
    current.symlink_to(Path("generations") / PREVIOUS.generation_id)
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
        previous=PREVIOUS,
        target_root=generations / TARGET.generation_id,
        snapshot_root=state / maintenance_executor.SNAPSHOT_DIR_NAME / "request-1",
        evidence_path=maintenance_evidence.evidence_binding_path(
            plan=plan,
            request_id="request-1",
            role="target",
            generation=TARGET,
        ),
    )


class Harness:
    def __init__(
        self,
        request: maintenance_executor.MaintenanceRequest,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.request = request
        self.events: list[tuple[str, object]] = []
        self.states = {
            maintenance_services.COCKPIT_UNIT: "active",
            maintenance_services.MAIL_UNIT: "active",
        }
        self.bindings: dict[str, maintenance_evidence.EvidenceBinding] = {}
        self.active_role = "target"
        self.probe_error = False
        self.probe_invalid = False
        self.snapshot_override: object | None = None
        self.lock_entries = 0

        real_lock = maintenance_controller.controller_lock

        @contextmanager
        def counted_lock(
            plan: maintenance_controller.ControllerPlan,
        ) -> Iterator[maintenance_controller.ControllerLease]:
            self.lock_entries += 1
            self.events.append(("lock", "enter"))
            with real_lock(plan) as lease:
                yield lease
            self.events.append(("lock", "exit"))

        monkeypatch.setattr(
            maintenance_runtime.maintenance_controller,
            "controller_lock",
            counted_lock,
        )
        monkeypatch.setattr(
            maintenance_runtime.maintenance_evidence,
            "freeze_active_server_evidence_under_lease",
            self.freeze,
        )
        monkeypatch.setattr(
            maintenance_runtime.maintenance_evidence,
            "load_schema_evidence",
            self.load,
        )
        monkeypatch.setattr(
            maintenance_runtime.maintenance_evidence,
            "publish_schema_evidence",
            self.publish,
        )
        monkeypatch.setattr(
            maintenance_runtime.maintenance_evidence,
            "activate_server_evidence",
            self.activate,
        )
        monkeypatch.setattr(
            maintenance_runtime.maintenance_evidence,
            "read_active_server_evidence",
            self.read_active,
        )
        monkeypatch.setattr(
            maintenance_runtime.upgrade_snapshot,
            "create_backup_snapshot",
            self.snapshot,
        )

    def binding(self, role: str) -> maintenance_evidence.EvidenceBinding:
        identity = self.request.previous if role == "previous" else self.request.target
        version = (
            self.request.previous_version
            if role == "previous"
            else self.request.target_version
        )
        return maintenance_evidence.EvidenceBinding(
            path=maintenance_evidence.evidence_binding_path(
                plan=self.request.plan,
                request_id=self.request.request_id,
                role=role,
                generation=identity,
            ),
            sha256=("d" if role == "previous" else "e") * 64,
            request_id=self.request.request_id,
            role=role,
            version=version,
            generation=identity,
        )

    def freeze(self, **kwargs: object) -> maintenance_evidence.EvidenceBinding:
        maintenance_controller.require_controller_lease(
            plan=self.request.plan,
            lease=kwargs["controller_lease"],  # type: ignore[arg-type]
        )
        assert self.active_role == "target"
        assert kwargs["expected_version"] == self.request.previous_version
        assert kwargs["expected_generation"] == self.request.previous
        assert kwargs["artifact_root"] == (
            self.request.plan.deploy_root
            / "generations"
            / self.request.previous.generation_id
        )
        self.events.append(("freeze", kwargs["request_id"]))
        binding = self.binding("previous")
        self.bindings["previous"] = binding
        return binding

    def load(self, **kwargs: object) -> maintenance_evidence.EvidenceBinding:
        role = kwargs["role"]
        expected = self.binding(role)  # type: ignore[arg-type]
        assert kwargs["expected_version"] == expected.version
        assert kwargs["expected_generation"] == expected.generation
        assert kwargs["artifact_root"] == (
            self.request.target_root
            if role == "target"
            else self.request.plan.deploy_root
            / "generations"
            / self.request.previous.generation_id
        )
        self.events.append(("read", role))
        try:
            return self.bindings[role]  # type: ignore[index]
        except KeyError:
            raise maintenance_evidence.EvidenceEnvironmentError("evidence_missing")

    def publish(self, **kwargs: object) -> maintenance_evidence.EvidenceBinding:
        assert kwargs["evidence"] == {
            "target": {
                "version": self.request.target_version,
                "source_sha": self.request.target.source_sha,
                "edition": "server",
            }
        }
        assert kwargs["expected_version"] == self.request.target_version
        assert kwargs["expected_generation"] == self.request.target
        assert kwargs["artifact_root"] == self.request.target_root
        self.events.append(("publish", kwargs["role"]))
        binding = self.binding("target")
        self.bindings["target"] = binding
        return binding

    def activate(self, **kwargs: object) -> Path:
        binding = kwargs["binding"]
        assert isinstance(binding, maintenance_evidence.EvidenceBinding)
        assert kwargs["expected_role"] == binding.role
        assert kwargs["expected_version"] == binding.version
        assert kwargs["expected_generation"] == binding.generation
        self.events.append(("activate", binding.role))
        self.active_role = binding.role
        return self.request.plan.state_root / maintenance_evidence.ACTIVE_ENV_NAME

    def read_active(self, **kwargs: object) -> maintenance_evidence.EvidenceBinding:
        expected_role = kwargs["expected_role"]
        self.events.append(("active", expected_role))
        if expected_role != self.active_role:
            raise maintenance_evidence.EvidenceEnvironmentError(
                "evidence_binding_invalid"
            )
        binding = self.bindings[expected_role]  # type: ignore[index]
        assert kwargs["expected_version"] == binding.version
        assert kwargs["expected_generation"] == binding.generation
        return binding

    def snapshot(self, **kwargs: object) -> dict[str, object]:
        assert set(self.states.values()) == {"inactive"}
        assert kwargs == {
            "snapshot_root": self.request.snapshot_root,
            "snapshot_id": self.request.request_id,
            "request_id": self.request.request_id,
            "source_sha": self.request.previous.source_sha,
            "target_digest": self.request.target.artifact_digest,
        }
        self.events.append(("snapshot", None))
        if self.snapshot_override is not None:
            return self.snapshot_override  # type: ignore[return-value]
        return {
            "snapshot_root": self.request.snapshot_root,
            "inventory_path": self.request.snapshot_root
            / upgrade_snapshot.INVENTORY_NAME,
            "inventory_sha256": "f" * 64,
        }

    def target_probe(
        self,
        request: maintenance_executor.MaintenanceRequest,
        inventory_path: Path,
        inventory_sha256: str,
    ) -> dict[str, object]:
        assert request == self.request
        assert inventory_path == request.snapshot_root / upgrade_snapshot.INVENTORY_NAME
        assert inventory_sha256 == "f" * 64
        assert set(self.states.values()) == {"inactive"}
        self.events.append(("probe", None))
        if self.probe_error:
            raise RuntimeError("probe failed")
        if self.probe_invalid:
            return []  # type: ignore[return-value]
        return {
            "target": {
                "version": request.target_version,
                "source_sha": request.target.source_sha,
                "edition": "server",
            }
        }

    def runner(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        action, unit = argv[2], argv[3]
        self.events.append((action, unit))
        if action == "show":
            return subprocess.CompletedProcess(argv, 0, self.states[unit] + "\n", "")
        self.states[unit] = "active" if action == "start" else "inactive"
        return subprocess.CompletedProcess(argv, 0, "", "")

    def ready(self, _url: str, _timeout: float) -> tuple[int, dict[str, object]]:
        identity = maintenance_executor.inspect_current_generation(self.request.plan)
        version = (
            self.request.target_version
            if identity == self.request.target
            else self.request.previous_version
        )
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

    def execute(self) -> dict[str, Any]:
        return maintenance_runtime.execute_maintenance(
            self.request,
            runner=self.runner,
            ready_probe=self.ready,
            target_probe=self.target_probe,
        )


def _event_index(events: list[tuple[str, object]], value: tuple[str, object]) -> int:
    return events.index(value)


def _seed_services_stopped(
    request: maintenance_executor.MaintenanceRequest,
) -> None:
    common = {
        "root": request.plan.journal_root,
        "request_id": request.request_id,
        "target_digest": request.target.artifact_digest,
    }
    upgrade_journal.create_journal(**common)
    upgrade_journal.advance_journal(**common, stage="services_stopped")


def _seed_switched(request: maintenance_executor.MaintenanceRequest) -> None:
    _seed_services_stopped(request)
    common = {
        "root": request.plan.journal_root,
        "request_id": request.request_id,
        "target_digest": request.target.artifact_digest,
    }
    upgrade_journal.record_switch_intent(
        **common,
        target_source_sha=request.target.source_sha,
        previous_generation=request.previous.generation_id,
    )
    generation_switch.activate_generation(
        request.plan.deploy_root,
        request.target,
        expected_previous=request.previous,
    )
    upgrade_journal.advance_journal(**common, stage="switched")


def test_initial_runtime_holds_one_lock_and_prepares_only_while_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    harness = Harness(request, monkeypatch)

    result = harness.execute()

    assert result["journal"]["stage"] == "committed"
    assert harness.lock_entries == 1
    assert _event_index(harness.events, ("lock", "enter")) < _event_index(
        harness.events, ("freeze", request.request_id)
    )
    assert _event_index(harness.events, ("freeze", request.request_id)) < _event_index(
        harness.events, ("activate", "previous")
    )
    assert _event_index(harness.events, ("stop", maintenance_services.COCKPIT_UNIT)) < _event_index(
        harness.events, ("snapshot", None)
    )
    assert _event_index(harness.events, ("snapshot", None)) < _event_index(
        harness.events, ("probe", None)
    ) < _event_index(harness.events, ("publish", "target"))
    assert harness.events[-1] == ("lock", "exit")


def test_committed_reentry_skips_freeze_and_all_target_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    harness = Harness(request, monkeypatch)
    harness.execute()
    harness.events.clear()

    result = harness.execute()

    assert result["journal"]["stage"] == "committed"
    assert not any(name in {"freeze", "snapshot", "probe", "publish"} for name, _ in harness.events)
    assert harness.active_role == "target"


def test_idle_reentry_after_previous_activation_reuses_binding_without_refreeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    harness = Harness(request, monkeypatch)
    harness.bindings["previous"] = harness.binding("previous")
    harness.active_role = "previous"

    assert harness.execute()["journal"]["stage"] == "committed"

    assert ("freeze", request.request_id) not in harness.events
    assert ("active", "previous") in harness.events


def test_idle_reentry_after_previous_freeze_finishes_selector_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    harness = Harness(request, monkeypatch)
    harness.bindings["previous"] = harness.binding("previous")

    assert harness.execute()["journal"]["stage"] == "committed"

    assert ("freeze", request.request_id) in harness.events
    assert _event_index(harness.events, ("freeze", request.request_id)) < _event_index(
        harness.events, ("activate", "previous")
    )


def test_services_stopped_reentry_reuses_existing_immutable_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    harness = Harness(request, monkeypatch)
    harness.bindings = {
        "previous": harness.binding("previous"),
        "target": harness.binding("target"),
    }
    harness.active_role = "previous"
    harness.states = {unit: "inactive" for unit in maintenance_services.STOP_ORDER}
    _seed_services_stopped(request)

    assert harness.execute()["journal"]["stage"] == "committed"

    assert not any(name in {"freeze", "snapshot", "probe", "publish"} for name, _ in harness.events)


def test_switched_reentry_reads_active_target_without_refreezing_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    harness = Harness(request, monkeypatch)
    harness.bindings = {
        "previous": harness.binding("previous"),
        "target": harness.binding("target"),
    }
    harness.active_role = "target"
    harness.states = {unit: "inactive" for unit in maintenance_services.STOP_ORDER}
    _seed_switched(request)

    assert harness.execute()["journal"]["stage"] == "committed"

    assert ("freeze", request.request_id) not in harness.events
    assert ("active", "target") in harness.events


def test_target_probe_failure_uses_executor_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    harness = Harness(request, monkeypatch)
    harness.probe_error = True

    with pytest.raises(maintenance_executor.MaintenanceExecutorError) as exc:
        harness.execute()

    assert exc.value.code == "binding_unavailable"
    assert upgrade_journal.load_journal(root=request.plan.journal_root)["stage"] == "rolled_back"
    assert maintenance_executor.inspect_current_generation(request.plan) == request.previous
    assert set(harness.states.values()) == {"active"}
    assert harness.active_role == "previous"


def test_invalid_target_probe_result_uses_executor_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    harness = Harness(request, monkeypatch)
    harness.probe_invalid = True

    with pytest.raises(maintenance_executor.MaintenanceExecutorError) as exc:
        harness.execute()

    assert exc.value.code == "binding_unavailable"
    assert upgrade_journal.load_journal(root=request.plan.journal_root)["stage"] == "rolled_back"
    assert maintenance_executor.inspect_current_generation(request.plan) == request.previous


@pytest.mark.parametrize(
    "snapshot_result",
    [
        [],
        {},
        {"inventory_path": Path("/tmp/wrong.json"), "inventory_sha256": "f" * 64},
        {
            "inventory_path": Path("/placeholder"),
            "inventory_sha256": "invalid",
        },
    ],
)
def test_invalid_snapshot_metadata_uses_executor_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_result: object,
) -> None:
    request = _request(tmp_path)
    harness = Harness(request, monkeypatch)
    if (
        isinstance(snapshot_result, dict)
        and snapshot_result.get("inventory_path") == Path("/placeholder")
    ):
        snapshot_result = {
            **snapshot_result,
            "inventory_path": request.snapshot_root / upgrade_snapshot.INVENTORY_NAME,
        }
    harness.snapshot_override = snapshot_result

    with pytest.raises(maintenance_executor.MaintenanceExecutorError) as exc:
        harness.execute()

    assert exc.value.code == "binding_unavailable"
    assert upgrade_journal.load_journal(root=request.plan.journal_root)["stage"] == "rolled_back"
    assert not any(name in {"probe", "publish"} for name, _ in harness.events)


@pytest.mark.parametrize("invalid", [None, False, 1, "probe"])
def test_invalid_target_probe_fails_before_lock_or_state_mutation(
    tmp_path: Path, invalid: object
) -> None:
    request = _request(tmp_path)

    with pytest.raises(maintenance_runtime.MaintenanceRuntimeError) as exc:
        maintenance_runtime.execute_maintenance(
            request,
            runner=lambda *_args, **_kwargs: None,
            ready_probe=lambda *_args, **_kwargs: (200, {}),
            target_probe=invalid,  # type: ignore[arg-type]
        )

    assert exc.value.code == "wiring_unavailable"
    assert tuple(request.plan.state_root.iterdir()) == ()


def test_invalid_request_fails_before_callback_or_state_access(tmp_path: Path) -> None:
    request = _request(tmp_path)

    with pytest.raises(maintenance_runtime.MaintenanceRuntimeError) as exc:
        maintenance_runtime.execute_maintenance(  # type: ignore[arg-type]
            object(),
            runner=lambda *_args, **_kwargs: None,
            ready_probe=lambda *_args, **_kwargs: (200, {}),
            target_probe=lambda *_args, **_kwargs: {},
        )

    assert exc.value.code == "request_invalid"
    assert tuple(request.plan.state_root.iterdir()) == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_version", "invalid"),
        ("snapshot_root", Path("relative")),
        ("evidence_path", Path("/tmp/wrong-evidence.json")),
    ],
)
def test_malformed_runtime_request_fails_before_lock_or_mutation(
    tmp_path: Path, field: str, value: object
) -> None:
    request = _request(tmp_path)
    malformed = replace(request, **{field: value})

    with pytest.raises(maintenance_runtime.MaintenanceRuntimeError) as exc:
        maintenance_runtime.execute_maintenance(
            malformed,
            runner=lambda *_args, **_kwargs: None,
            ready_probe=lambda *_args, **_kwargs: (200, {}),
            target_probe=lambda *_args, **_kwargs: {},
        )

    assert exc.value.code == "request_invalid"
    assert tuple(request.plan.state_root.iterdir()) == ()
