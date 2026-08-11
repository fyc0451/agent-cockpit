import fcntl
import os
from pathlib import Path

import pytest

import generation_switch
from generation_switch import (
    GenerationIdentity,
    GenerationSwitchError,
    activate_generation,
    rollback_generation,
)


SOURCE_A = "a" * 40
SOURCE_B = "b" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64


def _identity(source: str, digest: str) -> GenerationIdentity:
    return GenerationIdentity(source_sha=source, artifact_digest=digest)


def _layout(tmp_path: Path, *identities: GenerationIdentity) -> Path:
    root = tmp_path / "deploy"
    generations = root / "generations"
    generations.mkdir(parents=True, mode=0o700)
    for identity in identities:
        (generations / identity.generation_id).mkdir(mode=0o700)
    return root


def _current(root: Path) -> str | None:
    current = root / "current"
    return os.readlink(current) if current.is_symlink() else None


def _temps(root: Path) -> list[Path]:
    return list(root.glob(".current.tmp-*"))


def test_identity_binds_full_verified_digest_and_source_sha():
    identity = _identity(SOURCE_A, DIGEST_A)
    assert identity.generation_id == f"{SOURCE_A}-{DIGEST_A}"
    with pytest.raises(GenerationSwitchError, match="invalid_source_sha"):
        _identity("../escape", DIGEST_A)
    with pytest.raises(GenerationSwitchError, match="invalid_artifact_digest"):
        _identity(SOURCE_A, "f" * 63)


def test_first_activation_is_atomic_and_returns_exact_previous(tmp_path):
    first = _identity(SOURCE_A, DIGEST_A)
    root = _layout(tmp_path, first)

    result = activate_generation(root, first, expected_previous=None)

    assert result.changed is True
    assert result.previous_target is None
    assert result.current_target == f"generations/{first.generation_id}"
    assert _current(root) == result.current_target
    assert _temps(root) == []


def test_upgrade_and_exact_journal_rollback(tmp_path):
    first = _identity(SOURCE_A, DIGEST_A)
    second = _identity(SOURCE_B, DIGEST_B)
    root = _layout(tmp_path, first, second)
    activate_generation(root, first, expected_previous=None)

    upgraded = activate_generation(root, second, expected_previous=first)
    rolled_back = rollback_generation(
        root, journal_previous=first, expected_current=second
    )

    assert upgraded.previous_target == f"generations/{first.generation_id}"
    assert rolled_back.previous_target == f"generations/{second.generation_id}"
    assert rolled_back.current_target == f"generations/{first.generation_id}"
    assert _current(root) == rolled_back.current_target


def test_repeated_activation_is_explicitly_unchanged(tmp_path):
    first = _identity(SOURCE_A, DIGEST_A)
    root = _layout(tmp_path, first)
    activate_generation(root, first, expected_previous=None)

    repeated = activate_generation(root, first, expected_previous=first)

    assert repeated.changed is False
    assert repeated.previous_target == f"generations/{first.generation_id}"
    assert repeated.current_target == repeated.previous_target


def test_idempotent_fast_path_rejects_expected_previous_mismatch(tmp_path):
    first = _identity(SOURCE_A, DIGEST_A)
    root = _layout(tmp_path, first)
    activate_generation(root, first, expected_previous=None)

    with pytest.raises(GenerationSwitchError, match="current_drift"):
        activate_generation(root, first, expected_previous=None)

    assert _current(root) == f"generations/{first.generation_id}"


def test_rejects_intermediate_deploy_root_symlink(tmp_path):
    target = _identity(SOURCE_A, DIGEST_A)
    real_parent = tmp_path / "real"
    root = _layout(real_parent, target)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(GenerationSwitchError, match="deploy_root_symlink"):
        activate_generation(
            linked_parent / root.relative_to(real_parent),
            target,
            expected_previous=None,
        )

    assert not (root / "current").exists()


def test_same_root_lock_covers_current_compare(monkeypatch, tmp_path):
    target = _identity(SOURCE_A, DIGEST_A)
    root = _layout(tmp_path, target)
    real_read_current = generation_switch._read_current
    checked = False

    def assert_locked(root_fd: int) -> str | None:
        nonlocal checked
        lock_fd = os.open(".generation-switch.lock", os.O_RDWR, dir_fd=root_fd)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            checked = True
        finally:
            os.close(lock_fd)
        return real_read_current(root_fd)

    monkeypatch.setattr(generation_switch, "_read_current", assert_locked)

    activate_generation(root, target, expected_previous=None)

    assert checked is True


def test_rejects_replaced_temp_without_deleting_attacker_link(monkeypatch, tmp_path):
    target = _identity(SOURCE_A, DIGEST_A)
    root = _layout(tmp_path, target)
    real_read_current = generation_switch._read_current
    calls = 0

    def replace_temp(root_fd: int) -> str | None:
        nonlocal calls
        calls += 1
        current = real_read_current(root_fd)
        if calls == 2:
            temp_name = next(
                name
                for name in os.listdir(root_fd)
                if name.startswith(".current.tmp-")
            )
            os.unlink(temp_name, dir_fd=root_fd)
            os.symlink("attacker", temp_name, dir_fd=root_fd)
        return current

    monkeypatch.setattr(generation_switch, "_read_current", replace_temp)

    with pytest.raises(GenerationSwitchError, match="temp_changed"):
        activate_generation(root, target, expected_previous=None)

    residue = _temps(root)
    assert len(residue) == 1
    assert os.readlink(residue[0]) == "attacker"
    assert not (root / "current").exists()


def test_rejects_target_generation_entry_replacement(monkeypatch, tmp_path):
    target = _identity(SOURCE_A, DIGEST_A)
    root = _layout(tmp_path, target)
    generation = root / "generations" / target.generation_id
    held = root / "generations" / "held"
    real_read_current = generation_switch._read_current
    calls = 0

    def replace_target(root_fd: int) -> str | None:
        nonlocal calls
        calls += 1
        current = real_read_current(root_fd)
        if calls == 2:
            generation.rename(held)
            generation.mkdir(mode=0o700)
        return current

    monkeypatch.setattr(generation_switch, "_read_current", replace_target)

    with pytest.raises(GenerationSwitchError, match="generation_changed"):
        activate_generation(root, target, expected_previous=None)

    assert _temps(root) == []
    assert not (root / "current").exists()


def test_activation_rejects_current_drift(tmp_path):
    first = _identity(SOURCE_A, DIGEST_A)
    second = _identity(SOURCE_B, DIGEST_B)
    unexpected = _identity("c" * 40, "3" * 64)
    root = _layout(tmp_path, first, second, unexpected)
    os.symlink(f"generations/{unexpected.generation_id}", root / "current")

    with pytest.raises(GenerationSwitchError, match="current_drift"):
        activate_generation(root, second, expected_previous=first)

    assert _current(root) == f"generations/{unexpected.generation_id}"


@pytest.mark.parametrize("link_target", ["../outside", "/tmp/outside"])
def test_rejects_current_path_escape(tmp_path, link_target):
    target = _identity(SOURCE_A, DIGEST_A)
    root = _layout(tmp_path, target)
    os.symlink(link_target, root / "current")

    with pytest.raises(GenerationSwitchError, match="current_target_invalid"):
        activate_generation(root, target, expected_previous=None)

    assert _current(root) == link_target


def test_rejects_symlink_generation(tmp_path):
    target = _identity(SOURCE_A, DIGEST_A)
    root = _layout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "generations" / target.generation_id)

    with pytest.raises(GenerationSwitchError, match="generation_not_directory"):
        activate_generation(root, target, expected_previous=None)

    assert not (root / "current").exists()


def test_rejects_group_writable_generation(tmp_path):
    target = _identity(SOURCE_A, DIGEST_A)
    root = _layout(tmp_path, target)
    (root / "generations" / target.generation_id).chmod(0o770)

    with pytest.raises(GenerationSwitchError, match="generation_mode_unsafe"):
        activate_generation(root, target, expected_previous=None)


def test_rejects_wrong_owner(monkeypatch, tmp_path):
    target = _identity(SOURCE_A, DIGEST_A)
    root = _layout(tmp_path, target)
    monkeypatch.setattr(generation_switch.os, "getuid", lambda: os.stat(root).st_uid + 1)

    with pytest.raises(GenerationSwitchError, match="deploy_root_owner_unsafe"):
        activate_generation(root, target, expected_previous=None)


def test_replace_failure_keeps_current_and_cleans_created_temp(monkeypatch, tmp_path):
    first = _identity(SOURCE_A, DIGEST_A)
    second = _identity(SOURCE_B, DIGEST_B)
    root = _layout(tmp_path, first, second)
    activate_generation(root, first, expected_previous=None)
    monkeypatch.setattr(
        generation_switch.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("rename failed")),
    )

    with pytest.raises(GenerationSwitchError, match="atomic_replace_failed"):
        activate_generation(root, second, expected_previous=first)

    assert _current(root) == f"generations/{first.generation_id}"
    assert _temps(root) == []


def test_fsync_failure_is_reported_without_temp_residue(monkeypatch, tmp_path):
    target = _identity(SOURCE_A, DIGEST_A)
    root = _layout(tmp_path, target)
    monkeypatch.setattr(
        generation_switch.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("fsync failed")),
    )

    with pytest.raises(GenerationSwitchError, match="parent_fsync_failed"):
        activate_generation(root, target, expected_previous=None)

    assert _current(root) == f"generations/{target.generation_id}"
    assert _temps(root) == []


def test_preexisting_temp_collision_is_preserved(monkeypatch, tmp_path):
    target = _identity(SOURCE_A, DIGEST_A)
    root = _layout(tmp_path, target)
    residue = root / ".current.tmp-fixed"
    os.symlink("attacker", residue)
    monkeypatch.setattr(generation_switch.secrets, "token_hex", lambda _size: "fixed")

    with pytest.raises(GenerationSwitchError, match="temp_create_failed"):
        activate_generation(root, target, expected_previous=None)

    assert os.readlink(residue) == "attacker"
    assert not (root / "current").exists()


def test_rollback_rejects_drift_from_journal_current(tmp_path):
    first = _identity(SOURCE_A, DIGEST_A)
    second = _identity(SOURCE_B, DIGEST_B)
    drift = _identity("c" * 40, "3" * 64)
    root = _layout(tmp_path, first, second, drift)
    os.symlink(f"generations/{drift.generation_id}", root / "current")

    with pytest.raises(GenerationSwitchError, match="current_drift"):
        rollback_generation(root, journal_previous=first, expected_current=second)

    assert _current(root) == f"generations/{drift.generation_id}"
