"""B0 one-shot source→native migration: plan/preflight + injectable execute."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import generation_prepare
import generation_switch
import source_native_migrate
import supervisor_adapter
from generation_switch import GenerationIdentity
from release_index import canonical_bytes


SOURCE_SHA = "a" * 40
DIGEST = "b" * 64
TAG = "agent-cockpit-v0.3.0"


def _identity() -> GenerationIdentity:
    return GenerationIdentity(source_sha=SOURCE_SHA, artifact_digest=DIGEST)


def _prepared(tmp_path: Path) -> generation_prepare.PreparedGeneration:
    identity = _identity()
    gen = tmp_path / "deploy" / "generations" / identity.generation_id
    launcher = gen / "bin" / "agent-cockpit"
    launcher.parent.mkdir(parents=True, mode=0o700)
    launcher.write_bytes(b"\x7fELFnative")
    launcher.chmod(0o700)
    gen.chmod(0o700)
    (gen / "bin").chmod(0o700)
    return generation_prepare.PreparedGeneration(
        version="0.3.0",
        source_sha=SOURCE_SHA,
        artifact_digest=DIGEST,
        generation_id=identity.generation_id,
        generation_path=gen,
        launcher_path=launcher,
    )


def _inputs(tmp_path: Path) -> source_native_migrate.MigrationInputs:
    home = tmp_path / "home"
    home.mkdir()
    # Native root = frozen default_upgrade_layout only.
    layout = source_native_migrate.native_layout(home=home)
    deploy = layout.deploy_root
    deploy.mkdir(parents=True, mode=0o700)
    # Source/rollback tree is separate; not native current/generations.
    source_tree = home / source_native_migrate.SOURCE_DEPLOYMENTS_DIR_NAME
    source_deploy = source_tree / "fullscreen-old"
    source_deploy.mkdir(parents=True, mode=0o700)
    source_env = source_deploy / ".env"
    # Distinct secret-like payload; tests assert bytes identity without printing.
    source_env.write_bytes(b"SECRET_TOKEN=unit-test-only\nCOCKPIT_UPGRADE_V2_ENABLED=0\n")
    source_env.chmod(0o600)
    unit = home / ".config" / "systemd" / "user" / "agent-cockpit.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(
        "[Unit]\nDescription=source\n\n[Service]\n"
        "KillMode=process\n"
        f"WorkingDirectory={source_deploy}\n"
        "ExecStart=/usr/bin/env python server.py\n",
        encoding="utf-8",
    )
    unit.chmod(0o600)
    key = tmp_path / "release.pub"
    key.write_bytes(b"\x11" * 32)
    key.chmod(0o600)
    index = tmp_path / "index.json"
    index.write_text("{}", encoding="utf-8")
    sig = tmp_path / "index.sig"
    sig.write_bytes(b"\x22" * 64)
    env = home / ".config" / "agent-cockpit" / "server.env"
    env.parent.mkdir(parents=True)
    return source_native_migrate.MigrationInputs(
        release_tag=TAG,
        public_key_path=key,
        index_path=index,
        signature_path=sig,
        deploy_root=deploy,
        source_unit_path=unit,
        source_env_path=source_env,
        persistent_env_path=env,
        diagnostics_dir=tmp_path / "diag",
        platform="linux",
        arch="x86_64",
        cockpit_unit="agent-cockpit.service",
        mail_unit="agent-mail.service",
        ready_url="http://127.0.0.1:8790/health/live",
        expected_source_sha=SOURCE_SHA,
        home=home,
    )


@dataclass
class FakeServiceOps:
    """Records ordered (op, unit) actions for systemd-order assertions."""

    actions: list[tuple[str, str]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    reloads: int = 0
    active: dict[str, bool] = field(default_factory=dict)
    fail_stop: str | None = None
    # If set, only the N-th stop of this unit fails (1-based); others succeed.
    fail_stop_on_call: dict[str, int] = field(default_factory=dict)
    _stop_counts: dict[str, int] = field(default_factory=dict)

    def stop(self, unit: str) -> None:
        count = self._stop_counts.get(unit, 0) + 1
        self._stop_counts[unit] = count
        if self.fail_stop == unit:
            raise source_native_migrate.MigrationError("service_stop_failed")
        if self.fail_stop_on_call.get(unit) == count:
            raise source_native_migrate.MigrationError("service_stop_failed")
        self.actions.append(("stop", unit))
        self.stopped.append(unit)
        self.active[unit] = False

    def start(self, unit: str) -> None:
        self.actions.append(("start", unit))
        self.started.append(unit)
        self.active[unit] = True

    def daemon_reload(self) -> None:
        self.actions.append(("reload", ""))
        self.reloads += 1

    def is_active(self, unit: str) -> bool:
        return self.active.get(unit, False)


def _assert_health_mismatch_service_order(actions: list[tuple[str, str]]) -> None:
    """Forward stop→start then rollback stop→reload→start (systemd restart)."""
    expected = [
        # maintenance
        ("stop", "agent-cockpit.service"),
        ("stop", "agent-mail.service"),
        ("reload", ""),
        ("start", "agent-mail.service"),
        ("start", "agent-cockpit.service"),
        # post-native health fail → quiesce active native stack first
        ("stop", "agent-cockpit.service"),
        ("stop", "agent-mail.service"),
        ("reload", ""),
        ("start", "agent-mail.service"),
        ("start", "agent-cockpit.service"),
    ]
    assert actions == expected, f"service order mismatch:\n  got={actions!r}\n  want={expected!r}"


@dataclass
class FakeHealth:
    sha: str = SOURCE_SHA
    fail_once: bool = False
    _calls: int = 0

    def live_source_sha(self) -> str:
        self._calls += 1
        if self.fail_once and self._calls == 1:
            raise source_native_migrate.MigrationError("health_unavailable")
        return self.sha


def test_plan_is_default_and_does_not_mutate(tmp_path: Path):
    inputs = _inputs(tmp_path)
    before = inputs.source_unit_path.read_bytes()
    layout = source_native_migrate.native_layout(home=inputs.home)

    plan = source_native_migrate.build_plan(inputs)

    assert plan.release_tag == TAG
    assert plan.deploy_root == str(layout.deploy_root)
    assert plan.deploy_root == str(inputs.deploy_root)
    assert plan.controller_root == str(layout.controller_root)
    assert plan.native_unit_path == str(inputs.source_unit_path)
    assert plan.current_path == str(layout.current)
    assert plan.helpers_dir == str(layout.deploy_root / "helpers")
    assert plan.persistent_env_path == str(inputs.persistent_env_path)
    assert plan.cockpit_unit == "agent-cockpit.service"
    assert plan.mail_unit == "agent-mail.service"
    assert plan.expected_source_sha == SOURCE_SHA
    assert "plan_only" in plan.notes
    assert "native_layout_default_upgrade_layout" in plan.notes
    assert inputs.source_unit_path.read_bytes() == before
    assert not (inputs.deploy_root / "current").exists()
    assert not inputs.diagnostics_dir.exists()
    # Source tree must remain distinct from native deploy root.
    assert source_native_migrate.SOURCE_DEPLOYMENTS_DIR_NAME in str(
        inputs.source_env_path
    )
    assert not str(inputs.source_env_path).startswith(str(layout.deploy_root))


def test_preflight_requires_existing_key_index_signature_and_source_unit(tmp_path: Path):
    inputs = _inputs(tmp_path)
    inputs.public_key_path.unlink()

    with pytest.raises(source_native_migrate.MigrationError, match="public_key_missing"):
        source_native_migrate.preflight(inputs)


def test_preflight_rejects_wrong_public_key_size(tmp_path: Path):
    inputs = _inputs(tmp_path)
    inputs.public_key_path.write_bytes(b"short")

    with pytest.raises(source_native_migrate.MigrationError, match="public_key_invalid"):
        source_native_migrate.preflight(inputs)


def test_preflight_validates_source_env_mode_without_leaking_values(tmp_path: Path):
    inputs = _inputs(tmp_path)
    inputs.source_env_path.chmod(0o644)

    with pytest.raises(source_native_migrate.MigrationError, match="source_env_mode_invalid") as exc:
        source_native_migrate.preflight(inputs)
    assert "SECRET_TOKEN" not in str(exc.value)
    assert "unit-test-only" not in str(exc.value)


def test_preflight_path_b_accepts_missing_source_env_with_provisioned_server_env(
    tmp_path: Path, capsys,
):
    """Production: EnvironmentFile=-…/.env but file absent; use pre-provisioned server.env."""
    inputs = _inputs(tmp_path)
    secret = b"COCKPIT_UPGRADE_V2_ENABLED=0\nCOCKPIT_B0_MODE=on\nPROVISIONED=1\n"
    inputs.source_env_path.unlink()
    assert not inputs.source_env_path.exists()
    inputs.persistent_env_path.write_bytes(secret)
    inputs.persistent_env_path.chmod(0o600)

    plan = source_native_migrate.preflight(inputs)
    dumped = json.dumps(
        {
            "mode": plan.mode,
            "notes": list(plan.notes),
            "source_env_path": plan.source_env_path,
            "persistent_env_path": plan.persistent_env_path,
        },
        ensure_ascii=True,
    )
    assert plan.mode == "preflight"
    assert "env_strategy_reuse_provisioned" in plan.notes
    assert "PROVISIONED" not in dumped
    assert "COCKPIT_B0_MODE=on" not in dumped
    assert "SECRET_TOKEN" not in dumped


def test_preflight_fails_when_neither_source_nor_persistent_env(tmp_path: Path):
    inputs = _inputs(tmp_path)
    inputs.source_env_path.unlink()
    assert not inputs.persistent_env_path.exists()

    with pytest.raises(source_native_migrate.MigrationError, match="env_unavailable"):
        source_native_migrate.preflight(inputs)


def test_preflight_fails_when_both_env_files_differ(tmp_path: Path):
    inputs = _inputs(tmp_path)
    inputs.persistent_env_path.write_bytes(b"OTHER=1\n")
    inputs.persistent_env_path.chmod(0o600)

    with pytest.raises(source_native_migrate.MigrationError, match="env_content_mismatch") as exc:
        source_native_migrate.preflight(inputs)
    assert "SECRET_TOKEN" not in str(exc.value)
    assert "OTHER=1" not in str(exc.value)


def test_cli_default_is_plan_not_execute(tmp_path: Path, capsys):
    inputs = _inputs(tmp_path)
    assert inputs.home is not None
    argv = [
        "plan",
        "--release-tag", TAG,
        "--public-key", str(inputs.public_key_path),
        "--index", str(inputs.index_path),
        "--signature", str(inputs.signature_path),
        "--home", str(inputs.home),
        "--deploy-root", str(inputs.deploy_root),
        "--source-unit", str(inputs.source_unit_path),
        "--source-env", str(inputs.source_env_path),
        "--persistent-env", str(inputs.persistent_env_path),
        "--diagnostics-dir", str(inputs.diagnostics_dir),
        "--expected-source-sha", SOURCE_SHA,
    ]
    code = source_native_migrate.main(argv)
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["release_tag"] == TAG
    assert payload["mode"] == "plan"
    # Plan must never echo secret env contents.
    assert "SECRET_TOKEN" not in out
    assert "unit-test-only" not in out
    assert payload["source_env_path"] == str(inputs.source_env_path)
    assert payload["persistent_env_path"] == str(inputs.persistent_env_path)
    assert payload["deploy_root"] == str(inputs.deploy_root)
    assert payload["controller_root"] == str(
        source_native_migrate.native_layout(home=inputs.home).controller_root
    )


def test_execute_requires_explicit_confirm_flag(tmp_path: Path):
    inputs = _inputs(tmp_path)
    plan = source_native_migrate.preflight(inputs)

    with pytest.raises(source_native_migrate.MigrationError, match="confirm_required"):
        source_native_migrate.execute(
            inputs,
            plan,
            confirm=False,
            allow_live_service_ops=False,
            service_ops=FakeServiceOps(),
            health=FakeHealth(),
            prepare=lambda **_k: _prepared(tmp_path),
        )


def test_execute_without_live_ops_flag_rejects_even_with_confirm(tmp_path: Path):
    """Production safety: execute still refuses real service wiring without opt-in."""
    inputs = _inputs(tmp_path)
    plan = source_native_migrate.preflight(inputs)

    with pytest.raises(source_native_migrate.MigrationError, match="live_ops_required"):
        source_native_migrate.execute(
            inputs,
            plan,
            confirm=True,
            allow_live_service_ops=False,
            service_ops=None,
            health=FakeHealth(),
            prepare=lambda **_k: _prepared(tmp_path),
        )


def test_execute_happy_path_with_injected_ops(tmp_path: Path, monkeypatch):
    inputs = _inputs(tmp_path)
    plan = source_native_migrate.preflight(inputs)
    prepared = _prepared(tmp_path)
    # place generation under real deploy_root for activate_generation
    identity = _identity()
    real_gen = inputs.deploy_root / "generations" / identity.generation_id
    real_gen.mkdir(parents=True, mode=0o700)
    launcher = real_gen / "bin" / "agent-cockpit"
    launcher.parent.mkdir(parents=True, mode=0o700)
    launcher.write_bytes(b"\x7fELFnative")
    launcher.chmod(0o700)
    prepared = generation_prepare.PreparedGeneration(
        version="0.3.0",
        source_sha=SOURCE_SHA,
        artifact_digest=DIGEST,
        generation_id=identity.generation_id,
        generation_path=real_gen,
        launcher_path=launcher,
    )

    def fake_prepare(**_kwargs):
        return prepared

    def fake_install_controller(layout, prepared_arg, release_public_key):
        layout.controller_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        launcher_path = layout.controller_launcher
        launcher_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        launcher_path.write_bytes(b"\x7fELFctl")
        launcher_path.chmod(0o700)
        key = layout.public_key_path
        key.write_bytes(release_public_key)
        key.chmod(0o600)
        return layout.controller_root

    ops = FakeServiceOps(active={
        "agent-cockpit.service": True,
        "agent-mail.service": True,
    })
    health = FakeHealth(sha=SOURCE_SHA)

    result = source_native_migrate.execute(
        inputs,
        plan,
        confirm=True,
        allow_live_service_ops=True,
        service_ops=ops,
        health=health,
        prepare=fake_prepare,
        install_controller=fake_install_controller,
    )

    assert result.ok is True
    assert result.source_sha == SOURCE_SHA
    # Strict stop Cockpit → Mail; reload; start Mail → Cockpit (no rollback).
    assert ops.actions == [
        ("stop", "agent-cockpit.service"),
        ("stop", "agent-mail.service"),
        ("reload", ""),
        ("start", "agent-mail.service"),
        ("start", "agent-cockpit.service"),
    ]
    assert ops.reloads >= 1
    # unit backed up
    backup = inputs.diagnostics_dir / "agent-cockpit.service.source.bak"
    assert backup.is_file()
    # current points at generation
    assert (inputs.deploy_root / "current").is_symlink()
    # helpers installed
    helper = inputs.deploy_root / "helpers" / "mail-send"
    assert helper.is_symlink()
    assert os.readlink(helper) == "../current/bin/agent-cockpit"
    # native unit points at current launcher + release-external server.env only
    unit_text = inputs.source_unit_path.read_text(encoding="utf-8")
    assert "KillMode=process" in unit_text
    assert str(inputs.deploy_root / "current") in unit_text
    assert "bin/agent-cockpit" in unit_text
    assert "server.py" not in unit_text
    assert f"EnvironmentFile=-{inputs.persistent_env_path.as_posix()}" in unit_text
    assert f"{inputs.deploy_root.as_posix()}/current/.env" not in unit_text
    assert "SECRET_TOKEN" not in unit_text
    # source .env exact bytes migrated; source path untouched
    expected_env = b"SECRET_TOKEN=unit-test-only\nCOCKPIT_UPGRADE_V2_ENABLED=0\n"
    assert inputs.persistent_env_path.read_bytes() == expected_env
    assert inputs.source_env_path.read_bytes() == expected_env
    assert stat.S_IMODE(inputs.persistent_env_path.stat().st_mode) == 0o600
    # never wrote .env into generation
    gen_env = (inputs.deploy_root / "current").resolve() / ".env"
    assert not gen_env.exists()
    # herdr never stopped
    assert all("herdr" not in u for u in ops.stopped)


def test_execute_failure_restores_source_unit_and_starts_old_stack(tmp_path: Path):
    inputs = _inputs(tmp_path)
    plan = source_native_migrate.preflight(inputs)
    original = inputs.source_unit_path.read_bytes()
    identity = _identity()
    real_gen = inputs.deploy_root / "generations" / identity.generation_id
    real_gen.mkdir(parents=True, mode=0o700)
    launcher = real_gen / "bin" / "agent-cockpit"
    launcher.parent.mkdir(parents=True, mode=0o700)
    launcher.write_bytes(b"\x7fELFnative")
    launcher.chmod(0o700)
    prepared = generation_prepare.PreparedGeneration(
        version="0.3.0",
        source_sha=SOURCE_SHA,
        artifact_digest=DIGEST,
        generation_id=identity.generation_id,
        generation_path=real_gen,
        launcher_path=launcher,
    )

    def fake_prepare(**_kwargs):
        return prepared

    def fake_install_controller(layout, prepared_arg, release_public_key):
        layout.controller_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        layout.controller_launcher.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        layout.controller_launcher.write_bytes(b"\x7fELFctl")
        layout.controller_launcher.chmod(0o700)
        layout.public_key_path.write_bytes(release_public_key)
        layout.public_key_path.chmod(0o600)
        return layout.controller_root

    ops = FakeServiceOps(active={
        "agent-cockpit.service": True,
        "agent-mail.service": True,
    })
    # prior health + post-native mismatch + post-rollback old health
    health = SequenceHealth([SOURCE_SHA, "0" * 40, SOURCE_SHA])

    with pytest.raises(source_native_migrate.MigrationError, match="health_sha_mismatch"):
        source_native_migrate.execute(
            inputs,
            plan,
            confirm=True,
            allow_live_service_ops=True,
            service_ops=ops,
            health=health,
            prepare=fake_prepare,
            install_controller=fake_install_controller,
        )

    assert inputs.source_unit_path.read_bytes() == original
    # prior current was missing → must not leave current on failed generation
    assert not os.path.lexists(inputs.deploy_root / "current")
    # Must stop active native stack before unit restore, then Mail→Cockpit start.
    _assert_health_mismatch_service_order(ops.actions)
    assert (inputs.diagnostics_dir / "agent-cockpit.service.source.bak").is_file()
    assert (inputs.diagnostics_dir / "failure.json").is_file()
    failure = json.loads(
        (inputs.diagnostics_dir / "failure.json").read_text(encoding="utf-8")
    )
    assert failure["cockpit_stopped"] is True
    assert failure["mail_stopped"] is True
    # generation left for diagnosis
    assert real_gen.is_dir()
    # source .env never deleted/overwritten by rollback
    assert inputs.source_env_path.is_file()
    assert inputs.source_env_path.read_bytes() == (
        b"SECRET_TOKEN=unit-test-only\nCOCKPIT_UPGRADE_V2_ENABLED=0\n"
    )


def _prepared_under_deploy(inputs: source_native_migrate.MigrationInputs) -> generation_prepare.PreparedGeneration:
    identity = _identity()
    real_gen = inputs.deploy_root / "generations" / identity.generation_id
    real_gen.mkdir(parents=True, mode=0o700)
    launcher = real_gen / "bin" / "agent-cockpit"
    launcher.parent.mkdir(parents=True, mode=0o700)
    launcher.write_bytes(b"\x7fELFnative")
    launcher.chmod(0o700)
    return generation_prepare.PreparedGeneration(
        version="0.3.0",
        source_sha=SOURCE_SHA,
        artifact_digest=DIGEST,
        generation_id=identity.generation_id,
        generation_path=real_gen,
        launcher_path=launcher,
    )


def _fake_install_controller(layout, prepared_arg, release_public_key):
    layout.controller_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    layout.controller_launcher.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    layout.controller_launcher.write_bytes(b"\x7fELFctl")
    layout.controller_launcher.chmod(0o700)
    layout.public_key_path.write_bytes(release_public_key)
    layout.public_key_path.chmod(0o600)
    return layout.controller_root


def test_partial_stop_still_restores_source_stack(tmp_path: Path):
    """If Cockpit stopped and Mail stop fails, quiesce then start Mail→Cockpit."""
    inputs = _inputs(tmp_path)
    plan = source_native_migrate.preflight(inputs)
    original = inputs.source_unit_path.read_bytes()
    prepared = _prepared_under_deploy(inputs)
    ops = FakeServiceOps(
        active={"agent-cockpit.service": True, "agent-mail.service": True},
        # First Mail stop (maintenance) fails; rollback Mail stop succeeds.
        fail_stop_on_call={"agent-mail.service": 1},
    )

    with pytest.raises(source_native_migrate.MigrationError, match="service_stop_failed"):
        source_native_migrate.execute(
            inputs,
            plan,
            confirm=True,
            allow_live_service_ops=True,
            service_ops=ops,
            health=FakeHealth(),
            prepare=lambda **_k: prepared,
            install_controller=_fake_install_controller,
        )

    assert inputs.source_unit_path.read_bytes() == original
    assert ops.actions == [
        ("stop", "agent-cockpit.service"),
        # maintenance mail stop failed (not recorded)
        # rollback quiesce then restart
        ("stop", "agent-cockpit.service"),
        ("stop", "agent-mail.service"),
        ("reload", ""),
        ("start", "agent-mail.service"),
        ("start", "agent-cockpit.service"),
    ]
    failure = json.loads(
        (inputs.diagnostics_dir / "failure.json").read_text(encoding="utf-8")
    )
    assert failure["cockpit_stopped"] is True
    assert failure["mail_stopped"] is False


def test_execute_path_b_reuses_preprovisioned_server_env_without_scraping_unit(
    tmp_path: Path,
):
    """Missing source .env + provisioned server.env; native unit uses EnvironmentFile only."""
    inputs = _inputs(tmp_path)
    provisioned = (
        b"COCKPIT_EDITION=source\n"
        b"COCKPIT_UPGRADE_V2_ENABLED=0\n"
        b"COCKPIT_HERDR_STATE_MODE=canary\n"
        b"COCKPIT_B0_MODE=on\n"
    )
    inputs.source_env_path.unlink()
    inputs.persistent_env_path.write_bytes(provisioned)
    inputs.persistent_env_path.chmod(0o600)
    # Source unit has Environment= lines (production shape) — must not be scraped.
    inputs.source_unit_path.write_text(
        "[Unit]\nDescription=source\n\n[Service]\n"
        "KillMode=process\n"
        f"WorkingDirectory={inputs.source_env_path.parent}\n"
        "ExecStart=/usr/bin/env python server.py\n"
        "Environment=COCKPIT_EDITION=source\n"
        "Environment=COCKPIT_UPGRADE_V2_ENABLED=0\n"
        "Environment=COCKPIT_B0_MODE=on\n"
        f"EnvironmentFile=-{inputs.source_env_path.as_posix()}\n",
        encoding="utf-8",
    )
    inputs.source_unit_path.chmod(0o600)

    plan = source_native_migrate.preflight(inputs)
    prepared = _prepared_under_deploy(inputs)
    ops = FakeServiceOps(
        active={"agent-cockpit.service": True, "agent-mail.service": True},
    )
    result = source_native_migrate.execute(
        inputs,
        plan,
        confirm=True,
        allow_live_service_ops=True,
        service_ops=ops,
        health=FakeHealth(),
        prepare=lambda **_k: prepared,
        install_controller=_fake_install_controller,
    )
    assert result.ok is True
    assert inputs.persistent_env_path.read_bytes() == provisioned
    assert not inputs.source_env_path.exists()
    unit_text = inputs.source_unit_path.read_text(encoding="utf-8")
    assert f"EnvironmentFile=-{inputs.persistent_env_path.as_posix()}" in unit_text
    # Must not invent/copy production Environment= config keys into unit.
    assert "Environment=COCKPIT_EDITION=" not in unit_text
    assert "Environment=COCKPIT_B0_MODE=" not in unit_text
    assert "Environment=COCKPIT_UPGRADE_V2_ENABLED=" not in unit_text
    assert "server.py" not in unit_text
    assert ops.actions == [
        ("stop", "agent-cockpit.service"),
        ("stop", "agent-mail.service"),
        ("reload", ""),
        ("start", "agent-mail.service"),
        ("start", "agent-cockpit.service"),
    ]
    assert not (inputs.diagnostics_dir / "failure.json").exists()


def test_half_complete_retry_when_current_already_target(tmp_path: Path):
    """After prior failure left current→target, re-execute must be idempotent."""
    inputs = _inputs(tmp_path)
    plan = source_native_migrate.preflight(inputs)
    prepared = _prepared_under_deploy(inputs)
    # Simulate half-complete: generation + current already pointed at target;
    # source unit still source; persistent env may already exist with same bytes.
    current = inputs.deploy_root / "current"
    current.symlink_to(f"generations/{prepared.generation_id}")
    expected_env = inputs.source_env_path.read_bytes()
    inputs.persistent_env_path.write_bytes(expected_env)
    inputs.persistent_env_path.chmod(0o600)

    ops = FakeServiceOps(
        active={"agent-cockpit.service": True, "agent-mail.service": True},
    )
    result = source_native_migrate.execute(
        inputs,
        plan,
        confirm=True,
        allow_live_service_ops=True,
        service_ops=ops,
        health=FakeHealth(),
        prepare=lambda **_k: prepared,
        install_controller=_fake_install_controller,
    )

    assert result.ok is True
    assert ops.actions == [
        ("stop", "agent-cockpit.service"),
        ("stop", "agent-mail.service"),
        ("reload", ""),
        ("start", "agent-mail.service"),
        ("start", "agent-cockpit.service"),
    ]
    assert os.readlink(inputs.deploy_root / "current") == (
        f"generations/{prepared.generation_id}"
    )
    assert inputs.persistent_env_path.read_bytes() == expected_env
    unit_text = inputs.source_unit_path.read_text(encoding="utf-8")
    assert f"EnvironmentFile=-{inputs.persistent_env_path.as_posix()}" in unit_text
    assert "server.py" not in unit_text


def test_default_deploy_root_is_frozen_default_upgrade_layout(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    layout = source_native_migrate.native_layout(home=home)
    root = source_native_migrate.default_deploy_root(home=home)
    assert root == layout.deploy_root
    assert root == home / ".local" / "share" / "agent-cockpit-server"
    assert layout.controller_root == (
        home / ".local" / "share" / "agent-cockpit-controller"
    )


def test_preflight_rejects_legacy_agent_cockpit_deployments_as_native_root(
    tmp_path: Path,
):
    inputs = _inputs(tmp_path)
    assert inputs.home is not None
    # Wrong: pass source tree as native deploy_root.
    bad = replace(
        inputs,
        deploy_root=inputs.home / source_native_migrate.SOURCE_DEPLOYMENTS_DIR_NAME,
    )
    with pytest.raises(
        source_native_migrate.MigrationError, match="deploy_root_mismatch"
    ):
        source_native_migrate.preflight(bad)


def test_execute_does_not_stop_herdr(tmp_path: Path):
    inputs = _inputs(tmp_path)
    plan = source_native_migrate.preflight(inputs)
    identity = _identity()
    real_gen = inputs.deploy_root / "generations" / identity.generation_id
    real_gen.mkdir(parents=True, mode=0o700)
    launcher = real_gen / "bin" / "agent-cockpit"
    launcher.parent.mkdir(parents=True, mode=0o700)
    launcher.write_bytes(b"\x7fELFnative")
    launcher.chmod(0o700)
    prepared = generation_prepare.PreparedGeneration(
        version="0.3.0",
        source_sha=SOURCE_SHA,
        artifact_digest=DIGEST,
        generation_id=identity.generation_id,
        generation_path=real_gen,
        launcher_path=launcher,
    )
    ops = FakeServiceOps()
    def fake_install_controller(layout, prepared_arg, release_public_key):
        layout.controller_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        layout.controller_launcher.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        layout.controller_launcher.write_bytes(b"\x7fELFctl")
        layout.controller_launcher.chmod(0o700)
        layout.public_key_path.write_bytes(release_public_key)
        layout.public_key_path.chmod(0o600)
        return layout.controller_root

    source_native_migrate.execute(
        inputs,
        plan,
        confirm=True,
        allow_live_service_ops=True,
        service_ops=ops,
        health=FakeHealth(),
        prepare=lambda **_k: prepared,
        install_controller=fake_install_controller,
    )
    assert "herdr" not in "".join(u for op, u in ops.actions if op == "stop").lower()
    assert ops.actions == [
        ("stop", "agent-cockpit.service"),
        ("stop", "agent-mail.service"),
        ("reload", ""),
        ("start", "agent-mail.service"),
        ("start", "agent-cockpit.service"),
    ]


# ── REVIEW_BLOCK 7be80615 four-group regressions ───────────────────────────


@dataclass
class SequenceHealth:
    """prior capture, post-native check, post-rollback check."""

    values: list[str]
    _i: int = 0

    def live_source_sha(self) -> str:
        if self._i >= len(self.values):
            return self.values[-1]
        value = self.values[self._i]
        self._i += 1
        return value


def _signed_index_and_generation(
    *,
    deploy_root: Path,
    launcher: bytes,
) -> tuple[bytes, bytes, bytes, str, Path, Path]:
    """Build signed release index + pre-seeded exact generation (reuse without download).

    Returns (index_bytes, signature, public_key, artifact_digest, generation_path, launcher_path).
    Uses the same Ed25519/canonical_bytes fixture pattern as test_generation_prepare.
    """
    version_bytes = b"0.3.0\n"
    static_bytes = b"ready\n"
    runtime_bytes = b"pyinstaller-runtime\n"
    manifest_bytes = json.dumps({
        "version": "0.3.0",
        "source_sha": SOURCE_SHA,
        "edition": "server",
        "digests": {
            "VERSION": hashlib.sha256(version_bytes).hexdigest(),
            "static/index.html": hashlib.sha256(static_bytes).hexdigest(),
        },
    }, sort_keys=True).encode("ascii")
    files = (
        ("bin/agent-cockpit", launcher),
        ("bin/_internal/runtime.dat", runtime_bytes),
        ("VERSION", version_bytes),
        ("release-manifest.json", manifest_bytes),
        ("static/index.html", static_bytes),
    )
    archive_buf = io.BytesIO()
    with tarfile.open(fileobj=archive_buf, mode="w:gz") as archive:
        for name in ("bin/", "bin/_internal/", "static/"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        for name, payload in files:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    archive_bytes = archive_buf.getvalue()
    artifact_digest = hashlib.sha256(archive_bytes).hexdigest()
    launcher_digest = hashlib.sha256(launcher).hexdigest()
    asset_name = "agent-cockpit-server-0.3.0-linux-x86_64.tar.gz"
    index = {
        "schema_version": 2,
        "tag": TAG,
        "version": "0.3.0",
        "source_sha": SOURCE_SHA,
        "draft": False,
        "prerelease": False,
        "assets": [{
            "name": asset_name,
            "edition": "server",
            "platform": "linux",
            "arch": "x86_64",
            "size": len(archive_bytes),
            "sha256": artifact_digest,
            "launcher": {
                "path": "bin/agent-cockpit",
                "size": len(launcher),
                "sha256": launcher_digest,
                "format": "elf",
            },
        }],
    }
    index_bytes = canonical_bytes(index)
    key = Ed25519PrivateKey.generate()
    signature = key.sign(index_bytes)
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    generation_id = f"{SOURCE_SHA}-{artifact_digest}"
    generation_path = deploy_root / "generations" / generation_id
    generation_path.mkdir(parents=True, mode=0o700)
    (generation_path / "bin").mkdir(mode=0o700)
    (generation_path / "bin" / "_internal").mkdir(mode=0o700)
    (generation_path / "static").mkdir(mode=0o700)
    for name, payload in files:
        path = generation_path / name
        path.write_bytes(payload)
        path.chmod(0o700 if name == "bin/agent-cockpit" else 0o600)
    launcher_path = generation_path / "bin" / "agent-cockpit"
    cache = deploy_root / "artifact-cache"
    cache.mkdir(mode=0o700)
    artifact = cache / artifact_digest
    artifact.write_bytes(archive_bytes)
    artifact.chmod(0o600)
    return (
        index_bytes,
        signature,
        public,
        artifact_digest,
        generation_path,
        launcher_path,
    )


def test_block1_diagnostics_write_failure_still_rollbacks_unit(tmp_path: Path, monkeypatch):
    """failure.json write OSError must not skip source unit/service restore."""
    inputs = _inputs(tmp_path)
    plan = source_native_migrate.preflight(inputs)
    original = inputs.source_unit_path.read_bytes()
    prepared = _prepared_under_deploy(inputs)
    ops = FakeServiceOps(
        active={"agent-cockpit.service": True, "agent-mail.service": True},
    )
    # prior=SOURCE_SHA, post-native=bad, post-rollback=SOURCE_SHA
    health = SequenceHealth([SOURCE_SHA, "0" * 40, SOURCE_SHA])

    real_atomic = source_native_migrate._atomic_write_bytes

    def flaky_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
        if path.name == "failure.json":
            raise OSError("diagnostics_unavailable")
        return real_atomic(path, data, mode)

    monkeypatch.setattr(source_native_migrate, "_atomic_write_bytes", flaky_atomic)

    with pytest.raises(
        source_native_migrate.MigrationError, match="health_sha_mismatch"
    ):
        source_native_migrate.execute(
            inputs,
            plan,
            confirm=True,
            allow_live_service_ops=True,
            service_ops=ops,
            health=health,
            prepare=lambda **_k: prepared,
            install_controller=_fake_install_controller,
        )

    # diagnostics missing, but unit/current/services still restored
    assert not (inputs.diagnostics_dir / "failure.json").is_file()
    assert inputs.source_unit_path.read_bytes() == original
    assert not os.path.lexists(inputs.deploy_root / "current")
    _assert_health_mismatch_service_order(ops.actions)
    assert ops.reloads >= 2


def test_block2_failure_restores_prior_current_and_old_health(tmp_path: Path):
    """Save prior current; on failure restore exact symlink + old health."""
    inputs = _inputs(tmp_path)
    plan = source_native_migrate.preflight(inputs)
    original = inputs.source_unit_path.read_bytes()
    prepared = _prepared_under_deploy(inputs)
    # prior current points at an older generation — must restore after failure
    prior_id = "c" * 40 + "-" + "d" * 64
    prior_gen = inputs.deploy_root / "generations" / prior_id
    prior_gen.mkdir(parents=True, mode=0o700)
    (inputs.deploy_root / "current").symlink_to(f"generations/{prior_id}")

    ops = FakeServiceOps(
        active={"agent-cockpit.service": True, "agent-mail.service": True},
    )
    # prior health, then rollback health verify (activate fails before native health)
    health = SequenceHealth([SOURCE_SHA, SOURCE_SHA])

    with pytest.raises(
        source_native_migrate.MigrationError, match="current_unexpected"
    ):
        source_native_migrate.execute(
            inputs,
            plan,
            confirm=True,
            allow_live_service_ops=True,
            service_ops=ops,
            health=health,
            prepare=lambda **_k: prepared,
            install_controller=_fake_install_controller,
        )

    assert inputs.source_unit_path.read_bytes() == original
    assert os.path.lexists(inputs.deploy_root / "current")
    assert os.readlink(inputs.deploy_root / "current") == f"generations/{prior_id}"
    # prior capture + post-rollback health
    assert health._i >= 2
    # maintenance stops only; no native start; rollback quiesce+restart
    assert ops.actions == [
        ("stop", "agent-cockpit.service"),
        ("stop", "agent-mail.service"),
        ("stop", "agent-cockpit.service"),
        ("stop", "agent-mail.service"),
        ("reload", ""),
        ("start", "agent-mail.service"),
        ("start", "agent-cockpit.service"),
    ]


def test_block3_unit_install_and_rollback_use_atomic_replace(tmp_path: Path, monkeypatch):
    """Unit install AND failure rollback both use same-dir temp + os.replace."""
    inputs = _inputs(tmp_path)
    plan = source_native_migrate.preflight(inputs)
    prepared = _prepared_under_deploy(inputs)
    original = inputs.source_unit_path.read_bytes()
    ops = FakeServiceOps(
        active={"agent-cockpit.service": True, "agent-mail.service": True},
    )
    replaces: list[tuple[str, str]] = []
    real_replace = os.replace

    def tracking_replace(src, dst, *args, **kwargs):
        replaces.append((str(src), str(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", tracking_replace)

    # Happy path: install uses atomic replace
    source_native_migrate.execute(
        inputs,
        plan,
        confirm=True,
        allow_live_service_ops=True,
        service_ops=ops,
        health=FakeHealth(),
        prepare=lambda **_k: prepared,
        install_controller=_fake_install_controller,
    )
    unit_dst = str(inputs.source_unit_path)
    install_replaces = [pair for pair in replaces if pair[1] == unit_dst]
    assert install_replaces, "native unit install must os.replace into unit path"
    src_tmp, _dst = install_replaces[-1]
    assert Path(src_tmp).parent == inputs.source_unit_path.parent
    assert ".tmp-" in Path(src_tmp).name
    native_unit_bytes = inputs.source_unit_path.read_bytes()
    assert native_unit_bytes != original
    assert b"server.py" not in native_unit_bytes
    assert ops.actions == [
        ("stop", "agent-cockpit.service"),
        ("stop", "agent-mail.service"),
        ("reload", ""),
        ("start", "agent-mail.service"),
        ("start", "agent-cockpit.service"),
    ]

    # Failure path: rewrite source unit, fail health after switch → rollback replace
    inputs.source_unit_path.write_bytes(original)
    inputs.source_unit_path.chmod(0o600)
    # remove current so activate is first-time again
    current = inputs.deploy_root / "current"
    if current.is_symlink() or current.exists():
        current.unlink()
    replaces.clear()
    ops2 = FakeServiceOps(
        active={"agent-cockpit.service": True, "agent-mail.service": True},
    )
    health = SequenceHealth([SOURCE_SHA, "0" * 40, SOURCE_SHA])
    with pytest.raises(
        source_native_migrate.MigrationError, match="health_sha_mismatch"
    ):
        source_native_migrate.execute(
            inputs,
            plan,
            confirm=True,
            allow_live_service_ops=True,
            service_ops=ops2,
            health=health,
            prepare=lambda **_k: prepared,
            install_controller=_fake_install_controller,
        )
    rollback_replaces = [pair for pair in replaces if pair[1] == unit_dst]
    # at least install + rollback restores
    assert len(rollback_replaces) >= 2, (
        f"expected install+rollback unit replaces, got {rollback_replaces!r}"
    )
    for src_tmp, _dst in rollback_replaces:
        assert Path(src_tmp).parent == inputs.source_unit_path.parent
        assert ".tmp-" in Path(src_tmp).name
    assert inputs.source_unit_path.read_bytes() == original
    _assert_health_mismatch_service_order(ops2.actions)


def test_block4_execute_default_prepare_controller_reuse_and_tamper(tmp_path: Path):
    """Gate 4: execute default prepare/controller path only — no injects.

    Forbidden in this proof: prepare=/install_controller=/activate= kwargs,
    hand-built PreparedGeneration, or fake install_controller.

    Setup is disk-only signed index + pre-seeded generation (same Ed25519/
    canonical_bytes pattern as test_generation_prepare). First execute installs
    controller via real native_controller_install; second execute must reuse both
    without re-extract/reinstall; tamper fail-closes before service stop.
    """
    inputs = _inputs(tmp_path)
    launcher_bytes = b"\x7fELFreuse-exact-native"
    (
        index_bytes,
        signature,
        public,
        artifact_digest,
        generation_path,
        launcher_path,
    ) = _signed_index_and_generation(
        deploy_root=inputs.deploy_root,
        launcher=launcher_bytes,
    )
    inputs.public_key_path.write_bytes(public)
    inputs.public_key_path.chmod(0o600)
    inputs.index_path.write_bytes(index_bytes)
    inputs.signature_path.write_bytes(signature)

    layout = source_native_migrate.require_native_layout(inputs)
    # Parent only — controller must be created by execute default install path.
    layout.controller_root.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    assert not os.path.lexists(layout.controller_root)

    plan = source_native_migrate.preflight(inputs)
    gen_id = f"{SOURCE_SHA}-{artifact_digest}"

    # Pass 1: reuse pre-seeded generation + first-install controller (real default).
    ops1 = FakeServiceOps(
        active={"agent-cockpit.service": True, "agent-mail.service": True},
    )
    result1 = source_native_migrate.execute(
        inputs,
        plan,
        confirm=True,
        allow_live_service_ops=True,
        service_ops=ops1,
        health=FakeHealth(sha=SOURCE_SHA),
        # intentionally omit prepare / install_controller / activate
    )
    assert result1.ok is True
    assert result1.source_sha == SOURCE_SHA
    assert result1.generation_id == gen_id
    assert layout.controller_root.is_dir()
    assert layout.public_key_path.read_bytes() == public
    assert launcher_path.read_bytes() == launcher_bytes
    gen_launcher_mtime = launcher_path.stat().st_mtime_ns
    controller_mtime = layout.controller_launcher.stat().st_mtime_ns
    assert os.readlink(inputs.deploy_root / "current") == f"generations/{gen_id}"

    # Pass 2: exact existing generation+controller reused (mtime frozen).
    ops2 = FakeServiceOps(
        active={"agent-cockpit.service": True, "agent-mail.service": True},
    )
    result2 = source_native_migrate.execute(
        inputs,
        plan,
        confirm=True,
        allow_live_service_ops=True,
        service_ops=ops2,
        health=FakeHealth(sha=SOURCE_SHA),
    )
    assert result2.ok is True
    assert result2.generation_id == gen_id
    assert launcher_path.read_bytes() == launcher_bytes
    assert launcher_path.stat().st_mtime_ns == gen_launcher_mtime
    assert layout.controller_launcher.stat().st_mtime_ns == controller_mtime
    assert layout.public_key_path.read_bytes() == public

    # Pass 3: tamper generation launcher → fail-closed before maintenance stop.
    launcher_path.write_bytes(b"\x7fELFtampered-fail-closed")
    launcher_path.chmod(0o700)
    ops3 = FakeServiceOps(
        active={"agent-cockpit.service": True, "agent-mail.service": True},
    )
    with pytest.raises(
        source_native_migrate.MigrationError, match="generation_inconsistent"
    ):
        source_native_migrate.execute(
            inputs,
            plan,
            confirm=True,
            allow_live_service_ops=True,
            service_ops=ops3,
            health=FakeHealth(sha=SOURCE_SHA),
        )
    assert ops3.actions == []
    assert ops3.stopped == []


@pytest.mark.parametrize(
    "tamper",
    [
        "single_receipt", "index_bytes", "receipt_hardlink",
        "manifest_extra", "static", "tree_hardlink", "extra",
    ],
)
def test_default_execute_generation_reuse_rejects_full_tree_tamper(
    tmp_path: Path, tamper: str,
):
    inputs = _inputs(tmp_path)
    launcher = b"\x7fELFstrict-generation"
    index, signature, public, _digest, generation, _launcher = (
        _signed_index_and_generation(deploy_root=inputs.deploy_root, launcher=launcher)
    )
    inputs.public_key_path.write_bytes(public)
    inputs.index_path.write_bytes(index)
    inputs.signature_path.write_bytes(signature)
    source_native_migrate.require_native_layout(inputs).controller_root.parent.mkdir(
        parents=True, mode=0o700, exist_ok=True,
    )
    plan = source_native_migrate.preflight(inputs)
    source_native_migrate.execute(
        inputs, plan, confirm=True, allow_live_service_ops=True,
        service_ops=FakeServiceOps(), health=FakeHealth(),
    )
    persisted_index = generation / source_native_migrate.PERSISTED_INDEX_NAME
    persisted_signature = generation / source_native_migrate.PERSISTED_SIGNATURE_NAME
    if tamper == "single_receipt":
        persisted_signature.unlink()
    elif tamper == "index_bytes":
        persisted_index.write_bytes(b"tampered")
    elif tamper == "receipt_hardlink":
        outside = tmp_path / "outside-index"
        persisted_index.rename(outside)
        os.link(outside, persisted_index)
    elif tamper == "manifest_extra":
        path = generation / "release-manifest.json"
        manifest = json.loads(path.read_text("ascii"))
        manifest["extra"] = True
        path.write_text(json.dumps(manifest), encoding="ascii")
    elif tamper == "static":
        (generation / "static/index.html").write_bytes(b"tampered\n")
    elif tamper == "tree_hardlink":
        managed = generation / "static/index.html"
        outside = tmp_path / "outside-static"
        managed.rename(outside)
        os.link(outside, managed)
    else:
        extra = generation / "unexpected"
        extra.write_bytes(b"extra")
        extra.chmod(0o600)
    ops = FakeServiceOps()
    with pytest.raises(source_native_migrate.MigrationError, match="generation_inconsistent"):
        source_native_migrate.execute(
            inputs, plan, confirm=True, allow_live_service_ops=True,
            service_ops=ops, health=FakeHealth(),
        )
    assert ops.actions == []


def test_default_execute_generation_reuse_repairs_only_both_missing_receipts(
    tmp_path: Path,
):
    inputs = _inputs(tmp_path)
    index, signature, public, _digest, generation, _launcher = (
        _signed_index_and_generation(
            deploy_root=inputs.deploy_root, launcher=b"\x7fELFreceipt-repair",
        )
    )
    inputs.public_key_path.write_bytes(public)
    inputs.index_path.write_bytes(index)
    inputs.signature_path.write_bytes(signature)
    source_native_migrate.require_native_layout(inputs).controller_root.parent.mkdir(
        parents=True, mode=0o700, exist_ok=True,
    )
    result = source_native_migrate.execute(
        inputs, source_native_migrate.preflight(inputs), confirm=True,
        allow_live_service_ops=True, service_ops=FakeServiceOps(), health=FakeHealth(),
    )
    assert result.ok is True
    assert (generation / source_native_migrate.PERSISTED_INDEX_NAME).read_bytes() == index
    assert (
        generation / source_native_migrate.PERSISTED_SIGNATURE_NAME
    ).read_bytes() == signature


def test_default_execute_extract_failure_preserves_only_its_forensic_tree(
    tmp_path: Path, monkeypatch,
):
    inputs = _inputs(tmp_path)
    index, signature, public, _digest, _generation, _launcher = (
        _signed_index_and_generation(
            deploy_root=inputs.deploy_root, launcher=b"\x7fELFextract-forensic",
        )
    )
    inputs.public_key_path.write_bytes(public)
    inputs.index_path.write_bytes(index)
    inputs.signature_path.write_bytes(signature)
    generations = inputs.deploy_root / "generations"
    sentinel = generations / ".reuse-verify-existing"
    sentinel.mkdir(mode=0o700)
    marker = sentinel / "keep"
    marker.write_bytes(b"keep")
    marker.chmod(0o600)

    def fail_extract(_artifact, _asset, destination):
        destination.mkdir(mode=0o700)
        evidence = destination / "partial"
        evidence.write_bytes(b"forensic")
        evidence.chmod(0o600)
        raise RuntimeError("path-bearing extractor detail must not escape")

    monkeypatch.setattr(source_native_migrate, "extract_verified_tarball", fail_extract)
    ops = FakeServiceOps()
    with pytest.raises(
        source_native_migrate.MigrationError, match=r"^generation_inconsistent$"
    ):
        source_native_migrate.execute(
            inputs, source_native_migrate.preflight(inputs), confirm=True,
            allow_live_service_ops=True, service_ops=ops, health=FakeHealth(),
        )
    forensic = [
        path for path in generations.glob(".reuse-verify-*") if path != sentinel
    ]
    assert len(forensic) == 1
    assert (forensic[0] / "partial").read_bytes() == b"forensic"
    assert marker.read_bytes() == b"keep"
    assert ops.actions == []


@pytest.mark.parametrize(
    "tamper", ["missing_internal", "extra", "runtime", "runtime_hardlink"],
)
def test_default_execute_controller_reuse_rejects_full_bin_tamper(
    tmp_path: Path, tamper: str,
):
    inputs = _inputs(tmp_path)
    index, signature, public, _digest, _generation, _launcher = (
        _signed_index_and_generation(
            deploy_root=inputs.deploy_root, launcher=b"\x7fELFstrict-controller",
        )
    )
    inputs.public_key_path.write_bytes(public)
    inputs.index_path.write_bytes(index)
    inputs.signature_path.write_bytes(signature)
    layout = source_native_migrate.require_native_layout(inputs)
    layout.controller_root.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    plan = source_native_migrate.preflight(inputs)
    source_native_migrate.execute(
        inputs, plan, confirm=True, allow_live_service_ops=True,
        service_ops=FakeServiceOps(), health=FakeHealth(),
    )
    runtime = layout.controller_root / "bin/_internal/runtime.dat"
    if tamper == "missing_internal":
        runtime.unlink()
        runtime.parent.rmdir()
    elif tamper == "extra":
        extra = layout.controller_root / "extra"
        extra.write_bytes(b"extra")
        extra.chmod(0o600)
    elif tamper == "runtime":
        runtime.write_bytes(b"tampered")
    else:
        outside = tmp_path / "outside-runtime"
        runtime.rename(outside)
        os.link(outside, runtime)
    ops = FakeServiceOps()
    with pytest.raises(source_native_migrate.MigrationError, match="controller_inconsistent"):
        source_native_migrate.execute(
            inputs, plan, confirm=True, allow_live_service_ops=True,
            service_ops=ops, health=FakeHealth(),
        )
    assert ops.actions == []
