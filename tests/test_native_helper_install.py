from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_cockpit import native_helper_install


COMMANDS = tuple(native_helper_install.HELPER_COMMANDS)
TARGET = "../current/bin/agent-cockpit"


def _deploy(tmp_path: Path) -> Path:
    deploy = tmp_path / "deploy"
    deploy.mkdir(mode=0o700)
    return deploy


def _receipt(deploy: Path) -> dict:
    path = deploy / "helpers" / native_helper_install.RECEIPT_NAME
    return json.loads(path.read_text(encoding="ascii"))


def test_install_creates_all_relative_links_and_ownership_receipt(tmp_path):
    deploy = _deploy(tmp_path)

    result = native_helper_install.install_helper_links(deploy)

    helpers = deploy / "helpers"
    assert result.managed == COMMANDS
    assert result.preserved == ()
    for command in COMMANDS:
        link = helpers / command
        assert link.is_symlink()
        assert os.readlink(link) == TARGET
    receipt_path = helpers / native_helper_install.RECEIPT_NAME
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert _receipt(deploy) == {
        "schema_version": 1,
        "owner": "agent-cockpit",
        "links": {command: TARGET for command in COMMANDS},
    }


def test_install_is_idempotent(tmp_path):
    deploy = _deploy(tmp_path)
    native_helper_install.install_helper_links(deploy)
    before = _receipt(deploy)

    result = native_helper_install.install_helper_links(deploy)

    assert result.managed == COMMANDS
    assert result.preserved == ()
    assert _receipt(deploy) == before


def test_only_exact_receipt_owned_link_is_updated(tmp_path):
    deploy = _deploy(tmp_path)
    helpers = deploy / "helpers"
    helpers.mkdir()
    old_target = "../old/bin/agent-cockpit"
    owned = helpers / COMMANDS[0]
    owned.symlink_to(old_target)
    receipt = helpers / native_helper_install.RECEIPT_NAME
    receipt.write_text(json.dumps({
        "schema_version": 1,
        "owner": "agent-cockpit",
        "links": {COMMANDS[0]: old_target},
    }), encoding="ascii")
    receipt.chmod(0o600)

    native_helper_install.install_helper_links(deploy)

    assert os.readlink(owned) == TARGET


def test_legacy_file_and_unowned_symlink_are_preserved(tmp_path):
    deploy = _deploy(tmp_path)
    helpers = deploy / "helpers"
    helpers.mkdir()
    legacy_file = helpers / COMMANDS[0]
    legacy_file.write_text("legacy\n", encoding="ascii")
    legacy_target = tmp_path / "legacy-helper"
    legacy_target.write_text("legacy\n", encoding="ascii")
    legacy_link = helpers / COMMANDS[1]
    legacy_link.symlink_to(legacy_target)

    result = native_helper_install.install_helper_links(deploy)

    assert legacy_file.read_text(encoding="ascii") == "legacy\n"
    assert legacy_link.resolve() == legacy_target
    assert result.preserved == COMMANDS[:2]
    assert set(_receipt(deploy)["links"]) == set(COMMANDS[2:])


def test_replaced_owned_link_is_preserved_and_removed_from_receipt(tmp_path):
    deploy = _deploy(tmp_path)
    native_helper_install.install_helper_links(deploy)
    command = COMMANDS[0]
    link = deploy / "helpers" / command
    link.unlink()
    link.symlink_to("../custom/helper")

    result = native_helper_install.install_helper_links(deploy)

    assert os.readlink(link) == "../custom/helper"
    assert result.preserved == (command,)
    assert command not in _receipt(deploy)["links"]


def test_corrupt_receipt_fails_before_links_change(tmp_path):
    deploy = _deploy(tmp_path)
    helpers = deploy / "helpers"
    helpers.mkdir()
    receipt = helpers / native_helper_install.RECEIPT_NAME
    receipt.write_text("not-json\n", encoding="ascii")
    receipt.chmod(0o600)

    with pytest.raises(native_helper_install.HelperInstallError, match="receipt_invalid"):
        native_helper_install.install_helper_links(deploy)

    assert sorted(path.name for path in helpers.iterdir()) == [
        native_helper_install.RECEIPT_NAME
    ]


def test_receipt_write_failure_rolls_back_new_links(tmp_path, monkeypatch):
    deploy = _deploy(tmp_path)
    monkeypatch.setattr(
        native_helper_install,
        "_write_receipt",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(native_helper_install.HelperInstallError, match="install_failed"):
        native_helper_install.install_helper_links(deploy)

    assert list((deploy / "helpers").iterdir()) == []


@pytest.mark.parametrize("kind", ["relative", "missing", "file", "symlink"])
def test_invalid_deploy_root_is_rejected(tmp_path, kind):
    if kind == "relative":
        deploy = Path("relative")
    elif kind == "missing":
        deploy = tmp_path / "missing"
    elif kind == "file":
        deploy = tmp_path / "file"
        deploy.write_text("not-dir", encoding="ascii")
    else:
        real = _deploy(tmp_path)
        deploy = tmp_path / "link"
        deploy.symlink_to(real, target_is_directory=True)

    with pytest.raises(native_helper_install.HelperInstallError, match="deploy_root_invalid"):
        native_helper_install.install_helper_links(deploy)
