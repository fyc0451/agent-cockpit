from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from agent_cockpit import generation_prepare
from agent_cockpit import generation_switch
from agent_cockpit import native_controller_install
from agent_cockpit import upgrade_layout


def _prepared(
    tmp_path: Path,
) -> tuple[upgrade_layout.UpgradeLayout, generation_prepare.PreparedGeneration]:
    layout = upgrade_layout.default_upgrade_layout(home=tmp_path)
    layout.deploy_root.mkdir(parents=True, mode=0o700)
    identity = generation_switch.GenerationIdentity("a" * 40, "b" * 64)
    generation = layout.deploy_root / "generations" / identity.generation_id
    internal = generation / "bin" / "_internal"
    internal.mkdir(parents=True, mode=0o700)
    launcher = generation / "bin" / "agent-cockpit"
    launcher.write_bytes(b"\x7fELFcontroller")
    launcher.chmod(0o700)
    runtime = internal / "runtime.dat"
    runtime.write_bytes(b"runtime")
    runtime.chmod(0o600)
    (generation / "VERSION").write_text("1.2.3\n", encoding="ascii")
    (generation / "VERSION").chmod(0o600)
    return layout, generation_prepare.PreparedGeneration(
        version="1.2.3",
        source_sha=identity.source_sha,
        artifact_digest=identity.artifact_digest,
        generation_id=identity.generation_id,
        generation_path=generation,
        launcher_path=launcher,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_first_install_copies_only_onedir_and_explicit_key(tmp_path: Path) -> None:
    layout, prepared = _prepared(tmp_path)
    key = bytes(range(32))

    installed = native_controller_install.install_native_controller(
        layout, prepared, key,
    )

    assert installed == layout.controller_root
    assert sorted(path.name for path in installed.iterdir()) == [
        "bin", "release-public-key.bin",
    ]
    assert layout.controller_launcher.read_bytes() == prepared.launcher_path.read_bytes()
    assert (installed / "bin/_internal/runtime.dat").read_bytes() == b"runtime"
    assert layout.public_key_path.read_bytes() == key
    assert _mode(installed) == _mode(installed / "bin") == 0o700
    assert _mode(layout.controller_launcher) == 0o700
    assert _mode(layout.public_key_path) == 0o600
    assert not (installed / "VERSION").exists()
    assert upgrade_layout.validate_controller_launcher(layout) == (
        layout.controller_launcher
    )
    assert upgrade_layout.load_release_public_key(layout) == key
    assert not list(installed.parent.glob(f".{installed.name}.tmp-*"))


def test_fsyncs_tree_before_atomic_rename_and_parent_after(
    tmp_path: Path, monkeypatch,
) -> None:
    layout, prepared = _prepared(tmp_path)
    events = []
    real_fsync = native_controller_install.os.fsync
    real_rename = native_controller_install.os.rename

    def track_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def track_rename(source, destination):
        events.append("rename")
        return real_rename(source, destination)

    monkeypatch.setattr(native_controller_install.os, "fsync", track_fsync)
    monkeypatch.setattr(native_controller_install.os, "rename", track_rename)

    native_controller_install.install_native_controller(
        layout, prepared, bytes(range(32)),
    )

    rename_index = events.index("rename")
    assert "fsync" in events[:rename_index]
    assert events[rename_index + 1:] == ["fsync"]


def test_existing_controller_root_fails_closed_without_changes(tmp_path: Path) -> None:
    layout, prepared = _prepared(tmp_path)
    layout.controller_root.mkdir(mode=0o700)
    marker = layout.controller_root / "keep"
    marker.write_text("existing\n", encoding="ascii")

    with pytest.raises(
        native_controller_install.NativeControllerInstallError,
        match="controller_exists",
    ):
        native_controller_install.install_native_controller(
            layout, prepared, bytes(range(32)),
        )

    assert marker.read_text(encoding="ascii") == "existing\n"
    assert sorted(layout.controller_root.iterdir()) == [marker]


@pytest.mark.parametrize("key", [b"short", bytearray(range(32)), True])
def test_invalid_public_key_fails_before_creating_temp(tmp_path: Path, key) -> None:
    layout, prepared = _prepared(tmp_path)

    with pytest.raises(
        native_controller_install.NativeControllerInstallError,
        match="public_key_invalid",
    ):
        native_controller_install.install_native_controller(layout, prepared, key)

    assert not os.path.lexists(layout.controller_root)
    assert not list(layout.controller_root.parent.glob(".*.tmp-*"))


def test_forged_prepared_receipt_fails_before_copy(tmp_path: Path) -> None:
    layout, prepared = _prepared(tmp_path)
    forged = replace(prepared, launcher_path=tmp_path / "outside-launcher")

    with pytest.raises(
        native_controller_install.NativeControllerInstallError,
        match="prepared_invalid",
    ):
        native_controller_install.install_native_controller(
            layout, forged, bytes(range(32)),
        )

    assert not os.path.lexists(layout.controller_root)


def test_source_symlink_is_rejected_without_following_it(tmp_path: Path) -> None:
    layout, prepared = _prepared(tmp_path)
    runtime = prepared.generation_path / "bin/_internal/runtime.dat"
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    runtime.unlink()
    runtime.symlink_to(outside)

    with pytest.raises(
        native_controller_install.NativeControllerInstallError,
        match="generation_invalid",
    ):
        native_controller_install.install_native_controller(
            layout, prepared, bytes(range(32)),
        )

    assert outside.read_bytes() == b"outside"
    assert not os.path.lexists(layout.controller_root)


def test_copy_failure_cleans_only_this_calls_temp(
    tmp_path: Path, monkeypatch,
) -> None:
    layout, prepared = _prepared(tmp_path)
    keep = layout.controller_root.parent / "keep"
    keep.write_text("keep\n", encoding="ascii")

    def fail_copy(_source, destination, **_kwargs):
        destination.mkdir()
        raise OSError("copy failed")

    monkeypatch.setattr(native_controller_install.shutil, "copytree", fail_copy)

    with pytest.raises(
        native_controller_install.NativeControllerInstallError,
        match="install_failed",
    ):
        native_controller_install.install_native_controller(
            layout, prepared, bytes(range(32)),
        )

    assert keep.read_text(encoding="ascii") == "keep\n"
    assert not os.path.lexists(layout.controller_root)
    assert not list(layout.controller_root.parent.glob(".*.tmp-*"))
