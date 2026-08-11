from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

import maintenance_controller
import upgrade_layout


def _installed(tmp_path: Path) -> tuple[upgrade_layout.UpgradeLayout, bytes]:
    layout = upgrade_layout.default_upgrade_layout(home=tmp_path)
    layout.deploy_root.mkdir(parents=True, mode=0o700)
    layout.controller_launcher.parent.mkdir(parents=True, mode=0o700)
    layout.controller_launcher.write_bytes(b"\x7fELFcontroller")
    layout.controller_launcher.chmod(0o700)
    key = bytes(range(32))
    layout.public_key_path.write_bytes(key)
    layout.public_key_path.chmod(0o600)
    return layout, key


def test_default_layout_is_fixed_release_external_and_pure(tmp_path: Path) -> None:
    before = tuple(tmp_path.iterdir())

    layout = upgrade_layout.default_upgrade_layout(home=tmp_path)

    assert layout == upgrade_layout.UpgradeLayout(
        state_root=tmp_path / ".local/state/agent-cockpit/upgrade-v2",
        deploy_root=tmp_path / ".local/share/agent-cockpit-server",
        current=tmp_path / ".local/share/agent-cockpit-server/current",
        controller_root=tmp_path / ".local/share/agent-cockpit-controller",
        controller_launcher=(
            tmp_path / ".local/share/agent-cockpit-controller/bin/agent-cockpit"
        ),
        public_key_path=(
            tmp_path
            / ".local/share/agent-cockpit-controller/release-public-key.bin"
        ),
    )
    assert not layout.controller_root.is_relative_to(layout.deploy_root)
    assert tuple(tmp_path.iterdir()) == before


def test_build_plan_preserves_exact_layout_without_creating_state(tmp_path: Path) -> None:
    layout = upgrade_layout.default_upgrade_layout(home=tmp_path)

    plan = upgrade_layout.build_controller_plan(layout)

    assert plan == maintenance_controller.ControllerPlan(
        state_root=layout.state_root,
        journal_root=layout.state_root / maintenance_controller.JOURNAL_DIR_NAME,
        deploy_root=layout.deploy_root,
        current=layout.current,
        controller_root=layout.controller_root,
    )
    assert tuple(tmp_path.iterdir()) == ()


def test_loads_exact_owned_key_and_controller_launcher(tmp_path: Path) -> None:
    layout, key = _installed(tmp_path)

    assert upgrade_layout.load_release_public_key(layout) == key
    assert upgrade_layout.validate_controller_launcher(layout) == (
        layout.controller_launcher
    )


@pytest.mark.parametrize("damage", ["missing", "wide", "short", "symlink", "launcher"])
def test_rejects_unavailable_or_drifted_install(tmp_path: Path, damage: str) -> None:
    layout, _key = _installed(tmp_path)
    expected = "controller_unavailable" if damage == "launcher" else "trust_unavailable"
    if damage == "missing":
        layout.public_key_path.unlink()
    elif damage == "wide":
        layout.public_key_path.chmod(0o644)
    elif damage == "short":
        layout.public_key_path.write_bytes(b"short")
    elif damage == "symlink":
        real = tmp_path / "outside-key"
        real.write_bytes(bytes(range(32)))
        layout.public_key_path.unlink()
        layout.public_key_path.symlink_to(real)
    else:
        layout.controller_launcher.chmod(0o600)

    operation = (
        upgrade_layout.validate_controller_launcher
        if damage == "launcher"
        else upgrade_layout.load_release_public_key
    )
    with pytest.raises(upgrade_layout.UpgradeLayoutError) as exc:
        operation(layout)
    assert exc.value.code == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("current", Path("/wrong/current")),
        ("controller_launcher", Path("relative")),
        ("public_key_path", Path("/wrong/key")),
    ],
)
def test_rejects_forged_layout_before_file_access(
    tmp_path: Path, field: str, value: Path,
) -> None:
    layout = replace(upgrade_layout.default_upgrade_layout(home=tmp_path), **{field: value})

    with pytest.raises(upgrade_layout.UpgradeLayoutError) as exc:
        upgrade_layout.load_release_public_key(layout)

    assert exc.value.code == "layout_invalid"
    assert tuple(tmp_path.iterdir()) == ()


def test_default_layout_rejects_relative_or_root_home() -> None:
    for home in (Path("relative"), Path("/")):
        with pytest.raises(upgrade_layout.UpgradeLayoutError, match="layout_invalid"):
            upgrade_layout.default_upgrade_layout(home=home)


def test_installed_files_are_owned_single_link_exact_modes(tmp_path: Path) -> None:
    layout, _key = _installed(tmp_path)
    launcher = layout.controller_launcher.stat()
    key = layout.public_key_path.stat()
    assert launcher.st_uid == key.st_uid == os.getuid()
    assert launcher.st_nlink == key.st_nlink == 1
