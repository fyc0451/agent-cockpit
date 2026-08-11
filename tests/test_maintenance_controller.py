from __future__ import annotations

import ast
import fcntl
import os
import stat
from pathlib import Path

import pytest

import maintenance_controller as controller
import upgrade_journal


def _layout(tmp_path: Path) -> tuple[controller.ControllerPlan, Path]:
    deploy = tmp_path / "deploy"
    generation = deploy / "generations" / ("a" * 40 + "-" + "1" * 64)
    generation.mkdir(parents=True, mode=0o700)
    (deploy / "current").symlink_to(Path("generations") / generation.name)
    state = tmp_path / "controller-state"
    state.mkdir(mode=0o700)
    controller_root = tmp_path / "controller-install"
    controller_root.mkdir(mode=0o700)
    plan = controller.build_controller_plan(
        state_root=state,
        deploy_root=deploy,
        current=deploy / "current",
        controller_root=controller_root,
    )
    return plan, state


def test_builds_fixed_release_external_plan(tmp_path: Path) -> None:
    plan, state = _layout(tmp_path)

    assert plan.state_root == state
    assert plan.journal_root == state / controller.JOURNAL_DIR_NAME
    assert plan.current == plan.deploy_root / "current"
    assert plan.engine == upgrade_journal.ENGINE
    assert plan.schema_version == upgrade_journal.SCHEMA_VERSION

    missing = tmp_path / "installer-will-create"
    missing_plan = controller.build_controller_plan(
        state_root=missing,
        deploy_root=plan.deploy_root,
        current=plan.current,
        controller_root=plan.controller_root,
    )
    assert missing_plan.state_root == missing
    assert not missing.exists()


@pytest.mark.parametrize("mutation", ["relative", "wrong_current", "state_in_release", "wrong_type"])
def test_plan_rejects_noncanonical_or_release_internal_paths(
    tmp_path: Path, mutation: str
) -> None:
    plan, _ = _layout(tmp_path)
    values = {
        "state_root": plan.state_root,
        "deploy_root": plan.deploy_root,
        "current": plan.current,
        "controller_root": plan.controller_root,
    }
    if mutation == "relative":
        values["state_root"] = Path("relative")
    elif mutation == "wrong_current":
        values["current"] = plan.deploy_root / "other"
    elif mutation == "state_in_release":
        inside = plan.deploy_root / "state"
        inside.mkdir(mode=0o700)
        values["state_root"] = inside
    else:
        values["state_root"] = str(plan.state_root)  # type: ignore[assignment]

    with pytest.raises(controller.ControllerPreflightError) as exc:
        controller.build_controller_plan(**values)
    assert exc.value.code in {"plan_invalid", "state_unsafe"}

    forged = controller.ControllerPlan(
        **{
            **plan.__dict__,
            "journal_root": plan.state_root / "caller-chosen",
        }
    )
    with pytest.raises(controller.ControllerPreflightError, match="plan_invalid"):
        controller.read_controller_status(forged)


@pytest.mark.parametrize("mutation", ["wide", "symlink_parent", "wrong_owner"])
def test_status_revalidates_unsafe_existing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    plan, state = _layout(tmp_path)
    if mutation == "wide":
        state.chmod(0o755)
    elif mutation == "symlink_parent":
        moved = tmp_path / "moved-state"
        state.rename(moved)
        state.symlink_to(moved, target_is_directory=True)
    else:
        monkeypatch.setattr(controller.os, "getuid", lambda: state.stat().st_uid + 1)

    with pytest.raises(controller.ControllerPreflightError) as exc:
        controller.read_controller_status(plan)
    assert exc.value.code == "state_unsafe"


@pytest.mark.parametrize("mutation", ["relative", "state_in_release", "journal_leaf"])
def test_public_entries_reject_forged_plan(tmp_path: Path, mutation: str) -> None:
    plan, _ = _layout(tmp_path)
    values = dict(plan.__dict__)
    if mutation == "relative":
        values["state_root"] = Path("relative")
        values["journal_root"] = Path("relative") / controller.JOURNAL_DIR_NAME
    elif mutation == "state_in_release":
        values["state_root"] = plan.deploy_root / "state"
        values["journal_root"] = values["state_root"] / controller.JOURNAL_DIR_NAME
    else:
        values["journal_root"] = plan.state_root / "caller-chosen"
    forged = controller.ControllerPlan(**values)

    with pytest.raises(controller.ControllerPreflightError, match="plan_invalid"):
        controller.read_controller_status(forged)
    with pytest.raises(controller.ControllerPreflightError, match="plan_invalid"):
        with controller.controller_lock(forged):
            pass


def test_lock_create_is_private_durable_and_persistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _ = _layout(tmp_path)
    real_fsync = controller.os.fsync
    synced: list[int] = []

    def track(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(controller.os, "fsync", track)
    with controller.controller_lock(plan):
        lock = plan.state_root / controller.LOCK_NAME
        info = lock.stat()
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_nlink == 1
        assert synced
    assert lock.is_file()


def test_lock_requires_installer_created_state(tmp_path: Path) -> None:
    plan, _ = _layout(tmp_path)
    missing = tmp_path / "missing-controller-state"
    missing_plan = controller.build_controller_plan(
        state_root=missing,
        deploy_root=plan.deploy_root,
        current=plan.current,
        controller_root=plan.controller_root,
    )

    with pytest.raises(controller.ControllerPreflightError, match="state_unsafe"):
        with controller.controller_lock(missing_plan):
            pass
    assert not missing.exists()


@pytest.mark.parametrize("mutation", ["symlink", "directory", "hardlink", "wide"])
def test_existing_unsafe_lock_is_rejected(
    tmp_path: Path, mutation: str
) -> None:
    plan, _ = _layout(tmp_path)
    lock = plan.state_root / controller.LOCK_NAME
    if mutation == "symlink":
        target = plan.state_root / "target"
        target.write_text("x")
        lock.symlink_to(target)
    elif mutation == "directory":
        lock.mkdir()
    else:
        lock.write_text("x")
        lock.chmod(0o600 if mutation == "hardlink" else 0o644)
        if mutation == "hardlink":
            os.link(lock, plan.state_root / "other")

    with pytest.raises(controller.ControllerPreflightError) as exc:
        with controller.controller_lock(plan):
            pass
    assert exc.value.code == "lock_unsafe"


def test_lock_contention_is_nonblocking_and_never_unlinks(tmp_path: Path) -> None:
    plan, _ = _layout(tmp_path)
    lock = plan.state_root / controller.LOCK_NAME
    lock.write_text("")
    lock.chmod(0o600)
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(controller.ControllerPreflightError) as exc:
            with controller.controller_lock(plan):
                pass
        assert exc.value.code == "controller_locked"
        assert lock.is_file()
    finally:
        os.close(fd)


def test_missing_status_is_byte_for_byte_read_only(tmp_path: Path) -> None:
    plan, state = _layout(tmp_path)
    before = (state.stat().st_mtime_ns, tuple(state.iterdir()))

    assert controller.read_controller_status(plan) == {"state": "idle", "journal": None}

    assert not plan.journal_root.exists()
    assert (state.stat().st_mtime_ns, tuple(state.iterdir())) == before

    missing = tmp_path / "missing-state"
    missing_plan = controller.build_controller_plan(
        state_root=missing,
        deploy_root=plan.deploy_root,
        current=plan.current,
        controller_root=plan.controller_root,
    )
    parent_before = (tmp_path.stat().st_mtime_ns, tuple(tmp_path.iterdir()))
    assert controller.read_controller_status(missing_plan) == {
        "state": "idle",
        "journal": None,
    }
    assert not missing.exists()
    assert (tmp_path.stat().st_mtime_ns, tuple(tmp_path.iterdir())) == parent_before


def test_valid_status_projects_allowlisted_fields_without_writes(tmp_path: Path) -> None:
    plan, _ = _layout(tmp_path)
    upgrade_journal.create_journal(
        root=plan.journal_root,
        request_id="request-1",
        target_digest="d" * 64,
    )
    journal_path = plan.journal_root / upgrade_journal.JOURNAL_NAME
    before = (journal_path.read_bytes(), journal_path.stat().st_mtime_ns)

    status = controller.read_controller_status(plan)

    assert status["state"] == "prepared"
    assert set(status["journal"]) == set(controller._STATUS_FIELDS)  # type: ignore[arg-type]
    assert (journal_path.read_bytes(), journal_path.stat().st_mtime_ns) == before


def test_corrupt_journal_is_not_repaired_or_cleaned(tmp_path: Path) -> None:
    plan, _ = _layout(tmp_path)
    plan.journal_root.mkdir(mode=0o700)
    journal_path = plan.journal_root / upgrade_journal.JOURNAL_NAME
    journal_path.write_bytes(b"not-json\n")
    journal_path.chmod(0o600)
    before = journal_path.read_bytes()

    with pytest.raises(controller.ControllerPreflightError) as exc:
        controller.read_controller_status(plan)
    assert exc.value.code == "journal_invalid"
    assert journal_path.read_bytes() == before


def test_source_has_no_upgrade_mutation_or_execution_capability() -> None:
    path = Path(controller.__file__)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert not imports & {"subprocess", "socket", "urllib", "httpx", "requests"}
    for forbidden in (
        "systemctl",
        "launchctl",
        "activate_generation",
        "rollback_generation",
        "create_backup_snapshot",
        "record_intent",
        "advance_journal",
        "os.replace",
    ):
        assert forbidden not in source
