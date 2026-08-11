from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

import native_launcher
import native_helper_install
from agent_mail_commands import common


COMMANDS = {
    "am-register",
    "am-retire",
    "am-init-project",
    "mail-send",
    "mail-recv",
    "mail-identity-inject",
    "task-report",
}


def _record_dispatch(monkeypatch):
    calls = []

    def fake_dispatch(command, argv):
        calls.append((command, argv))
        return 17

    monkeypatch.setattr(native_launcher, "dispatch_helper", fake_dispatch)
    return calls


def _record_schema_dispatch(monkeypatch):
    calls = []

    def fake_dispatch(argv):
        calls.append(argv)
        return 19

    monkeypatch.setattr(native_launcher, "dispatch_schema_probe", fake_dispatch)
    return calls


def _record_maintenance_dispatch(monkeypatch):
    calls = []

    def fake_dispatch(argv):
        calls.append(argv)
        return 29

    monkeypatch.setattr(
        native_launcher, "dispatch_maintenance_controller", fake_dispatch,
    )
    return calls


def test_fixed_helper_command_set():
    assert set(native_launcher.HELPER_COMMANDS) == COMMANDS


def test_explicit_helper_dispatch_preserves_arguments(monkeypatch):
    calls = _record_dispatch(monkeypatch)
    argv = [
        "helper", "mail-send", "--agent", "codex", "--instance", "i-opaque",
        "--project", "/tmp/project", "--body", "hello",
    ]

    assert native_launcher.main(argv, program="agent-cockpit") == 17
    assert calls == [("mail-send", argv[2:])]


def test_explicit_schema_probe_dispatch_preserves_exact_arguments(monkeypatch):
    calls = _record_schema_dispatch(monkeypatch)
    argv = [
        "schema-probe",
        "--snapshot-root", "/state/snapshot",
        "--artifact-root", "/deploy/generation",
        "--version", "2.0.0",
        "--source-sha", "a" * 40,
        "--backup-inventory-path", "/state/snapshot/backup-inventory.json",
        "--backup-inventory-sha256", "b" * 64,
    ]

    assert native_launcher.main(argv, program="agent-cockpit") == 19
    assert calls == [argv[1:]]


def test_explicit_maintenance_controller_dispatch_preserves_exact_arguments(
    monkeypatch,
):
    calls = _record_maintenance_dispatch(monkeypatch)
    argv = [
        "maintenance-controller", "status",
        "--state-root", "/state",
        "--deploy-root", "/deploy",
        "--current", "/deploy/current",
        "--controller-root", "/run/controller",
    ]

    assert native_launcher.main(argv, program="agent-cockpit") == 29
    assert calls == [argv[1:]]


@pytest.mark.parametrize(("result", "expected"), [(None, 0), (23, 23)])
def test_schema_probe_dispatch_calls_release_readiness_main_with_exact_argv(
    monkeypatch, result, expected,
):
    calls = []
    imports = []

    class Probe:
        @staticmethod
        def main(argv):
            calls.append(argv)
            return result

    def fake_import(name):
        imports.append(name)
        return Probe

    monkeypatch.setattr(native_launcher.importlib, "import_module", fake_import)

    assert native_launcher.dispatch_schema_probe(["--version", "2.0.0"]) == expected
    assert imports == ["release_readiness"]
    assert calls == [["--version", "2.0.0"]]


@pytest.mark.parametrize(("result", "expected"), [(None, 0), (31, 31)])
def test_maintenance_dispatch_calls_cli_main_with_exact_argv(
    monkeypatch, result, expected,
):
    calls = []
    imports = []

    class Controller:
        @staticmethod
        def main(argv):
            calls.append(argv)
            return result

    def fake_import(name):
        imports.append(name)
        return Controller

    monkeypatch.setattr(native_launcher.importlib, "import_module", fake_import)

    assert native_launcher.dispatch_maintenance_controller(["status"]) == expected
    assert imports == ["maintenance_cli"]
    assert calls == [["status"]]


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_multicall_basename_dispatch_preserves_arguments(monkeypatch, command):
    calls = _record_dispatch(monkeypatch)
    argv = ["--agent", "codex", "--instance", "i-opaque", "--project", "/tmp/p"]

    assert native_launcher.main(argv, program=f"/opt/bin/{command}") == 17
    assert calls == [(command, argv)]


def test_basename_never_supplies_identity_defaults(monkeypatch):
    calls = _record_dispatch(monkeypatch)

    assert native_launcher.main(
        ["--agent", "codex", "--project", "/tmp/project"],
        program="/opt/bin/mail-recv",
    ) == 17
    assert calls == [("mail-recv", ["--agent", "codex", "--project", "/tmp/project"])]


def test_schema_probe_basename_is_not_a_multicall_alias(monkeypatch):
    calls = _record_schema_dispatch(monkeypatch)

    assert native_launcher.main(
        ["--version", "2.0.0"], program="/opt/bin/schema-probe"
    ) is None
    assert calls == []


def test_dispatch_calls_importable_command_main_with_exact_argv(monkeypatch):
    calls = []

    class Command:
        @staticmethod
        def main(argv):
            calls.append(argv)
            return None

    monkeypatch.setattr(native_launcher.importlib, "import_module", lambda name: Command)

    assert native_launcher.dispatch_helper("mail-recv", ["--instance", "i-opaque"]) == 0
    assert calls == [["--instance", "i-opaque"]]


def test_install_helpers_dispatches_frozen_installer(tmp_path):
    deploy = tmp_path / "deploy"
    deploy.mkdir()

    assert native_launcher.main(
        ["install-helpers", "--deploy-root", str(deploy)],
        program="agent-cockpit",
    ) == 0

    helpers = deploy / "helpers"
    for command in native_helper_install.HELPER_COMMANDS:
        assert os.readlink(helpers / command) == native_helper_install.HELPER_TARGET


def test_install_helpers_requires_explicit_deploy_root():
    with pytest.raises(SystemExit, match="2"):
        native_launcher.main(["install-helpers"], program="agent-cockpit")


def test_frozen_helper_command_uses_multicall_alias_from_path(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "resolve_artifact_root", lambda: tmp_path / "generation")
    monkeypatch.setattr(common.shutil, "which", lambda name: f"/usr/local/bin/{name}")

    assert common.helper_command("mail-recv") == "/usr/local/bin/mail-recv"


@pytest.mark.parametrize("argv", [[], ["serve"], ["--host", "127.0.0.1"]])
def test_non_helper_arguments_are_left_for_server_launcher(argv, capsys):
    assert native_launcher.main(argv, program="agent-cockpit") is None
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("argv", [["helper"], ["helper", "unknown"]])
def test_incomplete_or_unknown_helper_fails(argv, capsys):
    assert native_launcher.main(argv, program="agent-cockpit") == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_source_wrappers_run_from_unrelated_cwd(command, tmp_path):
    wrapper = Path(__file__).resolve().parents[1] / "agent-mail-tools" / command
    result = subprocess.run(
        [str(wrapper), "--help"], cwd=tmp_path, text=True, capture_output=True,
        env=os.environ.copy(), check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_am_common_legacy_import_bootstraps_package_from_unrelated_cwd(tmp_path):
    tools = Path(__file__).resolve().parents[1] / "agent-mail-tools"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tools)
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import am_common; from agent_mail_commands import common; "
            "assert am_common is common; print('ok')",
        ],
        cwd=tmp_path, text=True, capture_output=True, env=env, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_frozen_mail_send_reads_only_canonical_deploy_env(monkeypatch, tmp_path):
    import agent_mail_commands.mail_send as mail_send

    deploy = tmp_path / "deploy"
    generation_id = "a" * 40 + "-" + "b" * 64
    generation = deploy / "generations" / generation_id
    launcher = generation / "bin" / "agent-cockpit"
    bundle = tmp_path / "bundle"
    cwd = tmp_path / "cwd"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"launcher")
    bundle.mkdir()
    cwd.mkdir()
    (deploy / ".env").write_text("COCKPIT_PORT=9813\n", encoding="ascii")
    (generation / ".env").write_text("COCKPIT_PORT=9811\n", encoding="ascii")
    (bundle / ".env").write_text("COCKPIT_PORT=9812\n", encoding="ascii")
    (cwd / ".env").write_text("COCKPIT_PORT=9814\n", encoding="ascii")

    with monkeypatch.context() as patch:
        patch.setattr(sys, "frozen", True, raising=False)
        patch.setattr(sys, "executable", str(launcher))
        patch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
        patch.delenv("COCKPIT_PORT", raising=False)
        patch.chdir(cwd)
        importlib.reload(mail_send)

        assert mail_send.INSTALL_ROOT == str(generation.resolve())
        assert mail_send._team_reply_url() == "http://127.0.0.1:9813/api/agent/team-reply"

    importlib.reload(mail_send)


@pytest.mark.parametrize(
    "generation_id",
    [
        "release-1",
        "A" * 40 + "-" + "b" * 64,
        "a" * 39 + "-" + "b" * 64,
    ],
)
def test_frozen_mail_send_ignores_env_files_for_invalid_generation_id(
    monkeypatch, tmp_path, generation_id,
):
    import agent_mail_commands.mail_send as mail_send

    deploy = tmp_path / "deploy"
    generation = deploy / "generations" / generation_id
    launcher = generation / "bin" / "agent-cockpit"
    bundle = tmp_path / "bundle"
    cwd = tmp_path / "cwd"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"launcher")
    bundle.mkdir()
    cwd.mkdir()
    for root, port in ((deploy, 9820), (generation, 9821), (bundle, 9822), (cwd, 9823)):
        (root / ".env").write_text(f"COCKPIT_PORT={port}\n", encoding="ascii")

    with monkeypatch.context() as patch:
        patch.setattr(sys, "frozen", True, raising=False)
        patch.setattr(sys, "executable", str(launcher))
        patch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
        patch.delenv("COCKPIT_PORT", raising=False)
        patch.chdir(cwd)
        importlib.reload(mail_send)

        assert mail_send._team_reply_url() == "http://127.0.0.1:8790/api/agent/team-reply"
        patch.setenv("COCKPIT_PORT", "9824")
        assert mail_send._team_reply_url() == "http://127.0.0.1:9824/api/agent/team-reply"

    importlib.reload(mail_send)
