from __future__ import annotations

import hashlib
import errno
import json
import os
import stat
from pathlib import Path

import pytest
from fastapi import HTTPException

import generation_switch
import maintenance_controller
import maintenance_evidence
import release_identity
import release_readiness
import server
import store_schema


TARGET = generation_switch.GenerationIdentity("a" * 40, "b" * 64)
PREVIOUS = generation_switch.GenerationIdentity("c" * 40, "d" * 64)


def _plan(tmp_path: Path) -> maintenance_controller.ControllerPlan:
    deploy = tmp_path / "deploy"
    generation = deploy / "generations" / PREVIOUS.generation_id
    generation.mkdir(parents=True, mode=0o700)
    (deploy / "current").symlink_to(Path("generations") / generation.name)
    state = tmp_path / "controller-state"
    state.mkdir(mode=0o700)
    controller = tmp_path / "controller"
    controller.mkdir(mode=0o700)
    return maintenance_controller.build_controller_plan(
        state_root=state,
        deploy_root=deploy,
        current=deploy / "current",
        controller_root=controller,
    )


def _artifact(root: Path, identity: dict[str, str]) -> Path:
    static = root / "static"
    static.mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text(identity["version"] + "\n", encoding="ascii")
    (static / "index.html").write_text("ready\n", encoding="ascii")
    digests = {
        rel: hashlib.sha256((root / rel).read_bytes()).hexdigest()
        for rel in store_schema.required_manifest_digest_paths(root)
    }
    (root / "release-manifest.json").write_text(
        json.dumps(
            {
                "version": identity["version"],
                "source_sha": identity["source_sha"],
                "edition": "server",
                "digests": digests,
            },
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n",
        encoding="ascii",
    )
    return root


def _evidence(artifact: Path, identity: dict[str, str]) -> dict[str, object]:
    manifest_digest = hashlib.sha256(
        (artifact / "release-manifest.json").read_bytes()
    ).hexdigest()
    stores = [
        {
            "name": name,
            "compat_family": store_schema.COMPAT_FAMILY,
            "state": "absent",
            "reason": store_schema.REASON_MISSING_CREATABLE,
        }
        for name in store_schema._APP_OWNED_STORES
    ]
    return {
        "schema_version": 1,
        "compat_family": store_schema.COMPAT_FAMILY,
        "target": {
            "version": identity["version"],
            "source_sha": identity["source_sha"],
            "edition": "server",
        },
        "release_manifest_sha256": manifest_digest,
        "backup_inventory_sha256": "e" * 64,
        "stores": stores,
    }


def _publish(
    tmp_path: Path,
    plan: maintenance_controller.ControllerPlan,
    generation: generation_switch.GenerationIdentity = TARGET,
    request_id: str = "request-target",
    role: str = "target",
) -> tuple[maintenance_evidence.EvidenceBinding, Path, dict[str, str]]:
    identity = {
        "version": "1.2.3",
        "source_sha": generation.source_sha,
        "edition": "server",
    }
    artifact = plan.deploy_root / "generations" / generation.generation_id
    artifact.mkdir(parents=True, mode=0o700, exist_ok=True)
    artifact = _artifact(artifact, identity)
    binding = maintenance_evidence.publish_schema_evidence(
        plan=plan,
        request_id=request_id,
        role=role,
        expected_version=identity["version"],
        expected_generation=generation,
        artifact_root=artifact,
        evidence=_evidence(artifact, identity),
    )
    return binding, artifact, identity


def _activate(
    plan: maintenance_controller.ControllerPlan,
    binding: maintenance_evidence.EvidenceBinding,
    artifact: Path,
) -> Path:
    return maintenance_evidence.activate_server_evidence(
        plan=plan,
        binding=binding,
        expected_request_id=binding.request_id,
        expected_role=binding.role,
        expected_version=binding.version,
        expected_generation=binding.generation,
        artifact_root=artifact,
    )


def _read(
    plan: maintenance_controller.ControllerPlan,
    binding: maintenance_evidence.EvidenceBinding,
    artifact: Path,
) -> maintenance_evidence.EvidenceBinding:
    return maintenance_evidence.read_active_server_evidence(
        plan=plan,
        expected_request_id=binding.request_id,
        expected_role=binding.role,
        expected_version=binding.version,
        expected_generation=binding.generation,
        artifact_root=artifact,
    )


def test_publish_is_release_external_private_durable_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    real_fsync = maintenance_evidence.os.fsync
    synced: list[int] = []

    def track(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(maintenance_evidence.os, "fsync", track)
    binding, _artifact_root, _identity = _publish(tmp_path, plan)
    again, _, _ = _publish(tmp_path, plan)

    assert again == binding
    assert binding.path.parent == plan.state_root / maintenance_evidence.EVIDENCE_DIR_NAME
    assert binding.path.name == (
        hashlib.sha256(b"request-target").hexdigest()
        + f"-target-{TARGET.generation_id}.json"
    )
    assert not binding.path.is_relative_to(plan.deploy_root)
    info = binding.path.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_uid == os.getuid()
    assert info.st_nlink == 1
    assert binding.sha256 == hashlib.sha256(binding.path.read_bytes()).hexdigest()
    assert len(synced) >= 2


def test_same_request_role_generation_rejects_different_content(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    binding, artifact, identity = _publish(tmp_path, plan)
    changed = _evidence(artifact, identity)
    changed["backup_inventory_sha256"] = "f" * 64

    with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as exc:
        maintenance_evidence.publish_schema_evidence(
            plan=plan,
            request_id="request-target",
            role="target",
            expected_version=identity["version"],
            expected_generation=TARGET,
            artifact_root=artifact,
            evidence=changed,
        )

    assert exc.value.code == "evidence_conflict"
    assert hashlib.sha256(binding.path.read_bytes()).hexdigest() == binding.sha256


def test_deterministic_load_recovers_previous_without_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    binding, artifact, identity = _publish(
        tmp_path,
        plan,
        PREVIOUS,
        request_id="recover-request",
        role="previous",
    )
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda _self: (_ for _ in ()).throw(AssertionError("must not scan")),
    )

    recovered = maintenance_evidence.load_schema_evidence(
        plan=plan,
        request_id="recover-request",
        role="previous",
        expected_version=identity["version"],
        expected_generation=PREVIOUS,
        artifact_root=artifact,
    )

    assert recovered == binding


@pytest.mark.parametrize("mutation", ["chmod", "hardlink", "replace"])
def test_read_at_rejects_post_open_metadata_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    plan = _plan(tmp_path)
    binding, _, _ = _publish(tmp_path, plan)
    directory_fd = os.open(binding.path.parent, maintenance_evidence._DIR_FLAGS)
    real_read = maintenance_evidence.os.read
    mutated = False

    def race(fd: int, size: int) -> bytes:
        nonlocal mutated
        if not mutated and os.fstat(fd).st_ino == binding.path.stat().st_ino:
            mutated = True
            if mutation == "chmod":
                binding.path.chmod(0o644)
            elif mutation == "hardlink":
                os.link(binding.path, binding.path.with_suffix(".link"))
            else:
                replacement = binding.path.with_suffix(".replacement")
                replacement.write_bytes(binding.path.read_bytes())
                replacement.chmod(0o600)
                os.replace(replacement, binding.path)
        return real_read(fd, size)

    monkeypatch.setattr(maintenance_evidence.os, "read", race)
    try:
        with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as exc:
            maintenance_evidence._read_at(
                directory_fd,
                binding.path.name,
                missing_code="evidence_missing",
                unsafe_code="evidence_unsafe",
                max_bytes=release_readiness.MAX_EVIDENCE_BYTES,
            )
    finally:
        os.close(directory_fd)
    assert exc.value.code == "evidence_unsafe"


def test_immutable_publish_never_uses_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    identity = {
        "version": "1.2.3",
        "source_sha": TARGET.source_sha,
        "edition": "server",
    }
    artifact = plan.deploy_root / "generations" / TARGET.generation_id
    artifact.mkdir(parents=True, mode=0o700)
    _artifact(artifact, identity)
    monkeypatch.setattr(
        maintenance_evidence.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("immutable publish must not replace")
        ),
    )

    binding = maintenance_evidence.publish_schema_evidence(
        plan=plan,
        request_id="no-replace",
        role="target",
        expected_version=identity["version"],
        expected_generation=TARGET,
        artifact_root=artifact,
        evidence=_evidence(artifact, identity),
    )

    assert binding.path.is_file()


@pytest.mark.parametrize("failure", ["exists", "response_lost"])
def test_immutable_publish_reconciles_same_content_link_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    plan = _plan(tmp_path)
    identity = {
        "version": "1.2.3",
        "source_sha": TARGET.source_sha,
        "edition": "server",
    }
    artifact = plan.deploy_root / "generations" / TARGET.generation_id
    artifact.mkdir(parents=True, mode=0o700)
    _artifact(artifact, identity)
    real_link = maintenance_evidence.os.link

    def linked_then_failed(*args: object, **kwargs: object) -> None:
        real_link(*args, **kwargs)
        if failure == "exists":
            raise FileExistsError(errno.EEXIST, "competitor won")
        raise OSError(errno.EIO, "link response lost")

    monkeypatch.setattr(maintenance_evidence.os, "link", linked_then_failed)
    binding = maintenance_evidence.publish_schema_evidence(
        plan=plan,
        request_id="same-race",
        role="target",
        expected_version=identity["version"],
        expected_generation=TARGET,
        artifact_root=artifact,
        evidence=_evidence(artifact, identity),
    )

    assert binding.path.stat().st_nlink == 1
    assert not any(path.name.endswith(".tmp") for path in binding.path.parent.iterdir())


def test_immutable_publish_preserves_different_content_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    identity = {
        "version": "1.2.3",
        "source_sha": TARGET.source_sha,
        "edition": "server",
    }
    artifact = plan.deploy_root / "generations" / TARGET.generation_id
    artifact.mkdir(parents=True, mode=0o700)
    _artifact(artifact, identity)
    evidence = _evidence(artifact, identity)
    different = dict(evidence)
    different["backup_inventory_sha256"] = "f" * 64
    winner = release_readiness.canonical_evidence_bytes(different)

    def competing_link(
        _src: str,
        dst: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        assert src_dir_fd == dst_dir_fd
        assert follow_symlinks is False
        fd = os.open(
            dst,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        try:
            os.write(fd, winner)
            os.fsync(fd)
        finally:
            os.close(fd)
        raise FileExistsError(errno.EEXIST, "competitor won")

    monkeypatch.setattr(maintenance_evidence.os, "link", competing_link)
    with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as exc:
        maintenance_evidence.publish_schema_evidence(
            plan=plan,
            request_id="different-race",
            role="target",
            expected_version=identity["version"],
            expected_generation=TARGET,
            artifact_root=artifact,
            evidence=evidence,
        )

    assert exc.value.code == "evidence_conflict"
    final = next(
        path for path in (plan.state_root / maintenance_evidence.EVIDENCE_DIR_NAME).iterdir()
        if not path.name.startswith(".")
    )
    assert final.read_bytes() == winner


def test_freeze_active_target_as_request_previous_holds_controller_lock(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    active, artifact, identity = _publish(
        tmp_path, plan, request_id="old-request", role="target",
    )
    _activate(plan, active, artifact)

    with maintenance_evidence.freeze_active_server_evidence(
        plan=plan,
        request_id="new-request",
        expected_version=identity["version"],
        expected_generation=TARGET,
        artifact_root=artifact,
    ) as frozen:
        assert frozen.request_id == "new-request"
        assert frozen.role == "previous"
        assert frozen.path.read_bytes() == active.path.read_bytes()
        with pytest.raises(maintenance_controller.ControllerPreflightError) as exc:
            with maintenance_controller.controller_lock(plan):
                pass
        assert exc.value.code == "controller_locked"

    recovered = maintenance_evidence.load_schema_evidence(
        plan=plan,
        request_id="new-request",
        role="previous",
        expected_version=identity["version"],
        expected_generation=TARGET,
        artifact_root=artifact,
    )
    assert recovered == frozen


def test_freeze_under_lease_reuses_owner_lock_without_acquiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    active, artifact, identity = _publish(
        tmp_path, plan, request_id="old-request", role="target",
    )
    _activate(plan, active, artifact)

    with maintenance_controller.controller_lock(plan) as lease:
        monkeypatch.setattr(
            maintenance_evidence.maintenance_controller,
            "controller_lock",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("under-lease freeze must not acquire")
            ),
        )
        frozen = maintenance_evidence.freeze_active_server_evidence_under_lease(
            controller_lease=lease,
            plan=plan,
            request_id="new-request",
            expected_version=identity["version"],
            expected_generation=TARGET,
            artifact_root=artifact,
        )

    assert frozen.request_id == "new-request"
    assert frozen.role == "previous"
    assert frozen.path.read_bytes() == active.path.read_bytes()


@pytest.mark.parametrize("lease_state", ["expired", "wrong_plan"])
def test_freeze_under_invalid_lease_preserves_selector_and_evidence(
    tmp_path: Path, lease_state: str
) -> None:
    plan = _plan(tmp_path / "target")
    active, artifact, identity = _publish(
        tmp_path, plan, request_id="old-request", role="target",
    )
    selector = _activate(plan, active, artifact)
    evidence_dir = plan.state_root / maintenance_evidence.EVIDENCE_DIR_NAME

    def assert_rejected(
        lease: maintenance_controller.ControllerLease,
    ) -> None:
        selector_before = selector.read_bytes()
        evidence_before = tuple(
            sorted(
                (path.name, path.read_bytes())
                for path in evidence_dir.iterdir()
            )
        )
        with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as exc:
            maintenance_evidence.freeze_active_server_evidence_under_lease(
                controller_lease=lease,
                plan=plan,
                request_id="new-request",
                expected_version=identity["version"],
                expected_generation=TARGET,
                artifact_root=artifact,
            )
        assert exc.value.code == "controller_lease_invalid"
        assert selector.read_bytes() == selector_before
        assert tuple(
            sorted(
                (path.name, path.read_bytes())
                for path in evidence_dir.iterdir()
            )
        ) == evidence_before

    if lease_state == "expired":
        with maintenance_controller.controller_lock(plan) as lease:
            pass
        assert_rejected(lease)
    else:
        other = _plan(tmp_path / "other")
        with maintenance_controller.controller_lock(other) as lease:
            assert_rejected(lease)


def test_freeze_rejects_non_target_active_without_new_request_binding(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    active, artifact, identity = _publish(
        tmp_path, plan, request_id="old-request", role="previous",
    )
    _activate(plan, active, artifact)

    with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as exc:
        with maintenance_evidence.freeze_active_server_evidence(
            plan=plan,
            request_id="new-request",
            expected_version=identity["version"],
            expected_generation=TARGET,
            artifact_root=artifact,
        ):
            pass

    assert exc.value.code == "evidence_binding_invalid"
    expected_name = (
        hashlib.sha256(b"new-request").hexdigest()
        + f"-previous-{TARGET.generation_id}.json"
    )
    assert not (plan.state_root / maintenance_evidence.EVIDENCE_DIR_NAME / expected_name).exists()


def test_binding_request_id_is_verified_before_selector_mutation(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    binding, artifact, _ = _publish(tmp_path, plan)
    forged = maintenance_evidence.EvidenceBinding(
        path=binding.path,
        sha256=binding.sha256,
        request_id="other-request",
        role=binding.role,
        version=binding.version,
        generation=binding.generation,
    )

    with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as exc:
        maintenance_evidence.activate_server_evidence(
            plan=plan,
            binding=forged,
            expected_request_id="other-request",
            expected_role=forged.role,
            expected_version=forged.version,
            expected_generation=forged.generation,
            artifact_root=artifact,
        )

    assert exc.value.code == "evidence_path_invalid"
    assert not (plan.state_root / maintenance_evidence.ACTIVE_ENV_NAME).exists()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("request_id", "", "request_id_invalid"),
        ("request_id", None, "request_id_invalid"),
        ("expected_version", "1.2", "evidence_expected_invalid"),
        ("expected_generation", object(), "evidence_expected_invalid"),
        ("artifact_root", Path("/tmp/outside"), "evidence_expected_invalid"),
    ],
)
def test_freeze_invalid_inputs_fail_before_lock_mutation(
    tmp_path: Path,
    field: str,
    value: object,
    code: str,
) -> None:
    plan = _plan(tmp_path)
    values: dict[str, object] = {
        "plan": plan,
        "request_id": "request-new",
        "expected_version": "1.2.3",
        "expected_generation": TARGET,
        "artifact_root": (
            plan.deploy_root / "generations" / TARGET.generation_id
        ),
    }
    values[field] = value

    with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as exc:
        with maintenance_evidence.freeze_active_server_evidence(**values):  # type: ignore[arg-type]
            pass

    assert exc.value.code == code
    assert not (plan.state_root / maintenance_controller.LOCK_NAME).exists()


@pytest.mark.parametrize("component", ["bad path", "bad#path", "bad%path", 'bad"path'])
def test_plan_rejects_systemd_ambiguous_path_before_evidence_mutation(
    tmp_path: Path, component: str,
) -> None:
    plan = _plan(tmp_path / component)

    with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as exc:
        maintenance_evidence.validate_evidence_plan(plan)

    assert exc.value.code == "environment_path_unsupported"
    assert not (plan.state_root / maintenance_evidence.EVIDENCE_DIR_NAME).exists()


def test_target_and_previous_switch_only_replace_release_external_selector(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    target, target_artifact, _ = _publish(tmp_path, plan)
    previous, previous_artifact, _ = _publish(
        tmp_path,
        plan,
        PREVIOUS,
        request_id="request-previous",
        role="previous",
    )
    generation_before = {
        path: (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in (target.path, previous.path, plan.current.resolve())
    }

    env_path = _activate(plan, target, target_artifact)
    assert _read(plan, target, target_artifact) == target
    _activate(plan, previous, previous_artifact)
    assert _read(plan, previous, previous_artifact) == previous
    assert env_path == plan.state_root / maintenance_evidence.ACTIVE_ENV_NAME
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert generation_before == {
        path: (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in (target.path, previous.path, plan.current.resolve())
    }


def test_active_selector_requires_exact_expected_role_and_generation(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    target, target_artifact, _ = _publish(tmp_path, plan)
    previous, previous_artifact, _ = _publish(
        tmp_path, plan, PREVIOUS, role="previous",
    )
    _activate(plan, target, target_artifact)

    with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as exc:
        maintenance_evidence.read_active_server_evidence(
            plan=plan,
            expected_request_id=previous.request_id,
            expected_role="previous",
            expected_version=previous.version,
            expected_generation=previous.generation,
            artifact_root=previous_artifact,
        )

    assert exc.value.code == "evidence_binding_invalid"
    assert _read(plan, target, target_artifact) == target


@pytest.mark.parametrize("mutation", ["missing", "tampered", "wide", "symlink"])
def test_activation_rejects_missing_tampered_or_unsafe_evidence(
    tmp_path: Path, mutation: str,
) -> None:
    plan = _plan(tmp_path)
    binding, artifact, _ = _publish(tmp_path, plan)
    if mutation == "missing":
        binding.path.unlink()
    elif mutation == "tampered":
        binding.path.write_bytes(binding.path.read_bytes() + b" ")
    elif mutation == "wide":
        binding.path.chmod(0o644)
    else:
        real = binding.path.with_suffix(".real")
        binding.path.rename(real)
        binding.path.symlink_to(real)

    with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as exc:
        _activate(plan, binding, artifact)
    assert exc.value.code in {"evidence_missing", "evidence_unsafe", "evidence_tampered"}
    assert not (plan.state_root / maintenance_evidence.ACTIVE_ENV_NAME).exists()


def test_activation_rejects_path_escape_and_wrong_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    binding, artifact, _ = _publish(tmp_path, plan)
    escaped = maintenance_evidence.EvidenceBinding(
        path=tmp_path / "outside.json",
        sha256=binding.sha256,
        request_id=binding.request_id,
        role=binding.role,
        version=binding.version,
        generation=TARGET,
    )
    with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as escape:
        _activate(plan, escaped, artifact)
    assert escape.value.code == "evidence_path_invalid"

    real_secure = maintenance_evidence._secure_file

    def wrong_owner(info: os.stat_result) -> bool:
        return False if stat.S_ISREG(info.st_mode) else real_secure(info)

    monkeypatch.setattr(maintenance_evidence, "_secure_file", wrong_owner)
    with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as owner:
        _activate(plan, binding, artifact)
    assert owner.value.code == "evidence_unsafe"


@pytest.mark.parametrize("mutation", ["missing", "wide", "symlink", "malformed"])
def test_active_selector_rejects_missing_or_unsafe_content(
    tmp_path: Path, mutation: str,
) -> None:
    plan = _plan(tmp_path)
    binding, artifact, _ = _publish(tmp_path, plan)
    selector = _activate(plan, binding, artifact)
    if mutation == "missing":
        selector.unlink()
    elif mutation == "wide":
        selector.chmod(0o644)
    elif mutation == "symlink":
        real = selector.with_suffix(".real")
        selector.rename(real)
        selector.symlink_to(real)
    else:
        selector.write_text("COCKPIT_SCHEMA_EVIDENCE_PATH=/tmp/evil\n")

    with pytest.raises(maintenance_evidence.EvidenceEnvironmentError) as exc:
        _read(plan, binding, artifact)
    assert exc.value.code in {
        "environment_missing",
        "environment_unsafe",
        "environment_invalid",
        "plan_invalid",
    }


def test_real_server_health_ready_reads_activated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    binding, artifact, identity = _publish(tmp_path, plan)
    _activate(plan, binding, artifact)
    environ = maintenance_evidence.environment_mapping(
        _read(plan, binding, artifact)
    )
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        release_identity,
        "get_release_identity",
        lambda: {**identity, "instance_id": "server-test", "pid": 123},
    )
    monkeypatch.setattr(release_readiness, "__file__", str(artifact / "probe.py"))
    monkeypatch.setattr(store_schema, "__file__", str(artifact / "schema.py"))

    body = server.health_ready()

    assert body["status"] == "ready"
    assert body["ready"] is True

    binding.path.write_bytes(binding.path.read_bytes() + b" ")
    with pytest.raises(HTTPException) as exc:
        server.health_ready()
    assert exc.value.status_code == 503
    assert "schema_evidence_digest_mismatch" in str(exc.value.detail)


def test_real_server_health_ready_fails_when_environment_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plan(tmp_path)
    identity = {"version": "1.2.3", "source_sha": "a" * 40, "edition": "server"}
    artifact = _artifact(tmp_path / "artifact-missing", identity)
    monkeypatch.delenv(release_readiness.EVIDENCE_PATH_ENV, raising=False)
    monkeypatch.delenv(release_readiness.EVIDENCE_SHA256_ENV, raising=False)
    monkeypatch.setattr(
        release_identity,
        "get_release_identity",
        lambda: {**identity, "instance_id": "server-test", "pid": 123},
    )
    monkeypatch.setattr(release_readiness, "__file__", str(artifact / "probe.py"))
    monkeypatch.setattr(store_schema, "__file__", str(artifact / "schema.py"))

    with pytest.raises(HTTPException) as exc:
        server.health_ready()
    assert exc.value.status_code == 503
    assert "schema_evidence_missing" in str(exc.value.detail)
