import json
import os
import subprocess
import sys
from pathlib import Path

import herdr_client
import pytest
import server

from agent_mail_commands import mail_identity_inject


ROOT = Path(__file__).resolve().parents[1]
INSTANCE = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
NEW_INSTANCE = "i-bbbbbbbbbbbbbbbbbbbbbbbbbb"


def _secure_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _identity_fixture(monkeypatch, tmp_path: Path, *, instance: str = INSTANCE):
    home = tmp_path / "home"
    project = tmp_path / "project"
    workdir = tmp_path / "worktree"
    session_dir = home / ".config" / "herdr" / "sessions" / "demo"
    for path in (home, project, workdir, session_dir):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptors = home / "dashboard-data" / "launch-descriptors.json"
    bindings = home / "dashboard-data" / "mail-projects.json"
    registry = home / ".agent-mail" / "registry"
    _secure_json(bindings, {"sessions": {"demo": {
        "session_dir": str(session_dir), "project": str(project),
    }}})
    _secure_json(descriptors, {"schema": 2, "descriptors": {
        f"instance|{instance}": {
            "session": "demo", "pane_id": "w1:p1", "agent": "zcode",
            "kind": "opencode", "instance_id": instance, "name": instance,
            "display_name": "同名", "state": "active", "args": [],
            "workdir": str(workdir),
        },
    }})
    registry_file = (
        registry / mail_identity_inject.slugify(str(project))
        / f"zcode--{instance}.json"
    )
    _secure_json(registry_file, {
        "project_key": str(project), "agent": "zcode", "instance": instance,
        "name": "ZCodeMailbox", "status": "active",
    })
    monkeypatch.setattr(mail_identity_inject.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(mail_identity_inject, "MAIL_PROJECTS_PATH", bindings)
    monkeypatch.setattr(mail_identity_inject, "DESCRIPTORS_PATH", descriptors)
    monkeypatch.setattr(mail_identity_inject, "REGISTRY_DIR", registry)
    monkeypatch.setenv("HERDR_SESSION", "demo")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(session_dir / "herdr.sock"))
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        mail_identity_inject, "_live_identity_matches", lambda *_args: True,
        raising=False,
    )
    monkeypatch.setenv("COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(descriptors))
    return home, project, workdir, descriptors, registry_file


def test_zcode_is_attach_only_and_cwd_binary_cannot_launch(monkeypatch, tmp_path):
    executable = tmp_path / "zcode"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(herdr_client.shutil, "which", lambda _name: None)
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_snapshot_session",
        lambda _session: (_ for _ in ()).throw(AssertionError("不得读取 snapshot")),
    )

    assert herdr_client.normalize_agent_kind("zcode") == "opencode"
    assert herdr_client._find_agent_bin("zcode") == ""
    result = herdr_client.start_agent("demo", str(tmp_path), "zcode")
    assert result["error_code"] == "attach_only_agent"
    assert "zcode" not in server.VALID_AGENTS


def test_zcode_restart_rejects_before_runtime_mutation(monkeypatch, tmp_path):
    _identity_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "require_herdr_capabilities", lambda: None)
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda _session: {
        "panes": [{"pane_id": "w1:p1", "agent": "opencode"}],
        "agents": [{
            "pane_id": "w1:p1", "agent": "opencode", "name": INSTANCE,
        }],
    })
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("attach-only restart不得修改 runtime")
        ),
    )

    result = herdr_client.restart_pane("demo", "w1:p1")

    assert result["error_code"] == "attach_only_agent"
    assert result["preserved"] is True


@pytest.mark.parametrize("mutation", ["home_mode", "schema", "workdir", "retired"])
def test_identity_reader_rejects_invalid_security_binding(
    monkeypatch, tmp_path, mutation,
):
    home, project, workdir, descriptors, registry_file = _identity_fixture(
        monkeypatch, tmp_path,
    )
    if mutation == "home_mode":
        home.chmod(0o777)
    elif mutation == "schema":
        value = json.loads(descriptors.read_text(encoding="utf-8"))
        value["schema"] = 999
        _secure_json(descriptors, value)
    elif mutation == "workdir":
        value = json.loads(descriptors.read_text(encoding="utf-8"))
        value["descriptors"][f"instance|{INSTANCE}"]["workdir"] = str(
            workdir / ".." / "worktree"
        )
        _secure_json(descriptors, value)
    else:
        value = json.loads(registry_file.read_text(encoding="utf-8"))
        value.update({"status": "retired", "retired_at": "2026-08-12T00:00:00Z"})
        _secure_json(registry_file, value)

    assert mail_identity_inject.resolve_managed_identity() is None


def test_identity_reader_requires_exact_live_pane_not_stale_registry(
    monkeypatch, tmp_path,
):
    _identity_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        mail_identity_inject, "_live_identity_matches", lambda *_args: False,
        raising=False,
    )

    assert mail_identity_inject.resolve_managed_identity() is None


def test_live_pane_match_rejects_stale_same_display_name(monkeypatch, tmp_path):
    workdir = tmp_path.resolve()
    monkeypatch.setattr(mail_identity_inject, "_snapshot", lambda _session: {
        "panes": [{
            "pane_id": "w1:p1", "cwd": str(workdir), "agent": "opencode",
            "label": "ZCode",
        }],
        "agents": [{
            "pane_id": "w1:p1", "agent": "opencode",
            "name": "stale-luna-display-name",
        }],
    })

    assert not mail_identity_inject._live_identity_matches(
        "demo", "w1:p1", str(workdir), "opencode", INSTANCE,
    )


def test_live_pane_match_accepts_exact_opaque_runtime(monkeypatch, tmp_path):
    workdir = tmp_path.resolve()
    monkeypatch.setattr(mail_identity_inject, "_snapshot", lambda _session: {
        "panes": [{"pane_id": "w1:p1", "cwd": str(workdir)}],
        "agents": [{
            "pane_id": "w1:p1", "agent": "opencode", "name": INSTANCE,
        }],
    })

    assert mail_identity_inject._live_identity_matches(
        "demo", "w1:p1", str(workdir), "opencode", INSTANCE,
    )


def test_clean_installer_places_plugin_dependencies_on_actual_lookup_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [str(ROOT / "install-agent-mail-tools.sh"), str(ROOT)],
        env={**os.environ, "HOME": str(home)}, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    for name in ("mail-identity-inject", "mail-hook-check"):
        link = home / ".local" / "bin" / name
        assert link.is_symlink()
        assert link.resolve() == ROOT / "agent-mail-tools" / name
    plugin = home / ".config" / "opencode" / "plugins" / "agent-mail.js"
    assert plugin.is_symlink()
    source = plugin.read_text(encoding="utf-8")
    assert 'path.join(os.homedir(), ".local", "bin")' in source


def test_installer_preserves_existing_regular_plugin(tmp_path):
    home = tmp_path / "home"
    plugin = home / ".config" / "opencode" / "plugins" / "agent-mail.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("user owned\n", encoding="utf-8")
    plugin.chmod(0o600)

    result = subprocess.run(
        [str(ROOT / "install-agent-mail-tools.sh"), str(ROOT)],
        env={**os.environ, "HOME": str(home)}, capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert plugin.is_file() and not plugin.is_symlink()
    assert plugin.read_text(encoding="utf-8") == "user owned\n"


def test_session_created_injects_context_through_opencode_client(tmp_path):
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    inject = bin_dir / "mail-identity-inject"
    inject.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"hookSpecificOutput\":"
        "{\"hookEventName\":\"SessionStart\","
        "\"additionalContext\":\"trusted identity\"}}'\n",
        encoding="utf-8",
    )
    inject.chmod(0o700)
    check = bin_dir / "mail-hook-check"
    check.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    check.chmod(0o700)
    plugin = tmp_path / "plugin.mjs"
    plugin.write_text(
        (ROOT / "agent-mail-tools" / "agent-mail.opencode-plugin.js").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    runner = tmp_path / "runner.mjs"
    runner.write_text(
        f'import {{ AgentMailPlugin }} from {json.dumps(plugin.as_uri())};\n'
        "const calls=[];\n"
        "const hooks=await AgentMailPlugin({client:{session:{prompt:async (v)=>calls.push(v)}}});\n"
        "await hooks.event({event:{type:'session.created',properties:{info:{id:'s1'}}}});\n"
        "console.log(JSON.stringify(calls));\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(runner)], env={**os.environ, "HOME": str(home)},
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [{
        "path": {"id": "s1"},
        "body": {
            "noReply": True,
            "parts": [{"type": "text", "text": "trusted identity"}],
        },
    }]


def test_mail_hook_check_rejects_external_identity_arguments():
    result = subprocess.run(
        [str(ROOT / "agent-mail-tools" / "mail-hook-check"), "zcode", INSTANCE],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert result.stdout == ""


def test_mail_hook_check_uses_only_resolved_exact_identity(monkeypatch):
    from agent_mail_commands import mail_hook_check

    resolved = mail_identity_inject.ManagedIdentity(
        "/project", "zcode", INSTANCE, {"name": "FreshMailbox"},
    )
    monkeypatch.setattr(
        mail_hook_check.mail_identity_inject,
        "resolve_managed_identity",
        lambda: resolved,
    )
    seen = []
    monkeypatch.setattr(
        mail_hook_check.mail_recv,
        "main",
        lambda argv: seen.append(argv) or print("--- #1 [-] sender @ now"),
    )

    assert mail_hook_check.main([]) == 0
    assert seen == [[
        "--agent", "zcode", "--instance", INSTANCE,
        "--project", "/project", "--unread",
    ]]


def test_restart_retire_and_same_name_rebuild_keep_mailboxes_isolated(
    monkeypatch, tmp_path,
):
    _, project, workdir, descriptors, old_registry = _identity_fixture(
        monkeypatch, tmp_path,
    )
    assert mail_identity_inject.resolve_managed_identity().instance_id == INSTANCE

    # External runtime restart updates only location and preserves the lifecycle ID.
    updated = herdr_client.update_launch_descriptor_by_instance(
        INSTANCE, pane_id="w1:p2",
    )
    assert updated["instance_id"] == INSTANCE
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p2")
    assert mail_identity_inject.resolve_managed_identity().instance_id == INSTANCE

    # Real descriptor retirement state transition makes the old pane unusable.
    pending = herdr_client.mark_launch_descriptor_retirement_pending("demo", "w1:p2")
    assert pending["instance_ids"] == [INSTANCE]
    retired = herdr_client.finalize_launch_descriptor_retirement(INSTANCE)
    assert retired["state"] == "retired"
    registry_value = json.loads(old_registry.read_text(encoding="utf-8"))
    registry_value.update({"status": "retired", "retired_at": "2026-08-12T00:00:00Z"})
    _secure_json(old_registry, registry_value)
    assert mail_identity_inject.resolve_managed_identity() is None

    # Same display name rebuild receives a new opaque ID and never selects stale registry.
    created = herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p3", name=NEW_INSTANCE, kind="opencode",
        args=[], agent="zcode", workdir=str(workdir), instance_id=NEW_INSTANCE,
        display_name="同名",
    )
    assert created["display_name"] == retired["display_name"] == "同名"
    new_registry = (
        old_registry.parent / f"zcode--{NEW_INSTANCE}.json"
    )
    _secure_json(new_registry, {
        "project_key": str(project), "agent": "zcode", "instance": NEW_INSTANCE,
        "name": "FreshMailbox", "status": "active",
    })
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p3")
    resolved = mail_identity_inject.resolve_managed_identity()
    assert resolved.instance_id == NEW_INSTANCE
    assert resolved.identity["name"] == "FreshMailbox"
    assert old_registry.exists()
