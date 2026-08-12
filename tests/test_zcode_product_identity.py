import json
import os
import subprocess
import sys
from pathlib import Path

import herdr_client
import pytest
import server

from agent_mail_commands import am_retire, mail_hook_check, mail_identity_inject


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


def _legacy_identity_fixture(
    monkeypatch, tmp_path, *, schema=2, status="active", agent="codex", instance="main",
):
    home = tmp_path / "home"
    project = tmp_path / "project"
    workdir = tmp_path / "worktree"
    session_dir = home / ".config" / "herdr" / "sessions" / "demo"
    for path in (home, project, workdir, session_dir):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    bindings = home / "dashboard-data" / "mail-projects.json"
    descriptors = home / "dashboard-data" / "launch-descriptors.json"
    registry = home / ".agent-mail" / "registry"
    _secure_json(bindings, {"sessions": {"demo": {
        "session_dir": str(session_dir), "project": str(project),
    }}})
    _secure_json(descriptors, {"schema": schema, "descriptors": {
        f"demo|{agent}": {
            "session": "demo", "pane_id": "w1:p1", "name": agent,
            "agent": agent, "kind": mail_identity_inject.PRODUCT_KINDS[agent], "args": [],
        },
        f"instance|{INSTANCE}": {
            "session": "other", "pane_id": "w1:p9", "name": INSTANCE,
            "agent": "zcode", "kind": "opencode", "instance_id": INSTANCE,
            "state": "active", "args": [],
        },
    }})
    _secure_json(
        registry / mail_identity_inject.slugify(str(project)) / f"{agent}--{instance}.json",
        {
            "project_key": str(project), "agent": agent, "instance": instance,
            "name": f"{agent}-{instance}-mailbox", "status": status,
            **({"retired_at": "2026-08-12T00:00:00Z"} if status == "retired" else {}),
        },
    )
    monkeypatch.setattr(mail_identity_inject.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(mail_identity_inject, "MAIL_PROJECTS_PATH", bindings)
    monkeypatch.setattr(mail_identity_inject, "DESCRIPTORS_PATH", descriptors)
    monkeypatch.setattr(mail_identity_inject, "REGISTRY_DIR", registry)
    monkeypatch.setenv("HERDR_SESSION", "demo")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(session_dir / "herdr.sock"))
    monkeypatch.chdir(workdir)
    return descriptors, str(project)


def test_legacy_hook_argument_accepts_mixed_schema2_legacy_descriptor(
    monkeypatch, tmp_path, capsys,
):
    _legacy_identity_fixture(monkeypatch, tmp_path)

    mail_identity_inject.main(["codex"])

    payload = json.loads(capsys.readouterr().out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "codex-main-mailbox" in context
    assert "--agent codex --instance main" in context


def test_legacy_hook_argument_accepts_schema1_descriptor(
    monkeypatch, tmp_path, capsys,
):
    _legacy_identity_fixture(monkeypatch, tmp_path, schema=1)

    mail_identity_inject.main(["codex"])

    assert "codex-main-mailbox" in capsys.readouterr().out


def test_instance_key_without_instance_id_blocks_legacy_fallback(
    monkeypatch, tmp_path, capsys,
):
    descriptors, _ = _legacy_identity_fixture(monkeypatch, tmp_path)
    value = json.loads(descriptors.read_text(encoding="utf-8"))
    value["descriptors"] = {
        f"instance|{INSTANCE}": {
            "session": "demo", "pane_id": "w1:p1", "name": INSTANCE,
            "agent": "zcode", "kind": "opencode", "state": "active", "args": [],
        },
    }
    _secure_json(descriptors, value)

    mail_identity_inject.main(["codex"])

    assert capsys.readouterr().out == ""


def test_unknown_descriptor_schema_blocks_legacy_fallback(
    monkeypatch, tmp_path, capsys,
):
    descriptors, _ = _legacy_identity_fixture(monkeypatch, tmp_path, schema=999)
    assert descriptors.exists()

    mail_identity_inject.main(["codex"])

    assert capsys.readouterr().out == ""


def test_retired_legacy_identity_is_not_injected(monkeypatch, tmp_path, capsys):
    _legacy_identity_fixture(monkeypatch, tmp_path, status="retired")

    mail_identity_inject.main(["codex"])

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("agent", "instance"),
    [("codex", "winjd"), ("claude", "main"), ("opencode", "main")],
)
def test_legacy_hook_arguments_resolve_exact_registry_identity(
    monkeypatch, tmp_path, capsys, agent, instance,
):
    _legacy_identity_fixture(
        monkeypatch, tmp_path, agent=agent, instance=instance,
    )

    mail_identity_inject.main([agent, instance])

    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert f"{agent}-{instance}-mailbox" in context
    assert f"--agent {agent} --instance {instance}" in context


def test_invalid_managed_identity_never_falls_back_to_legacy_main(
    monkeypatch, tmp_path, capsys,
):
    _, project, _, _, exact_registry = _identity_fixture(monkeypatch, tmp_path)
    exact = json.loads(exact_registry.read_text(encoding="utf-8"))
    exact.update({"status": "retired", "retired_at": "2026-08-12T00:00:00Z"})
    _secure_json(exact_registry, exact)
    _secure_json(exact_registry.parent / "zcode--main.json", {
        "project_key": str(project), "agent": "zcode", "instance": "main",
        "name": "StaleLegacyMailbox", "status": "active",
    })

    mail_identity_inject.main(["zcode"])

    assert capsys.readouterr().out == ""


def test_product_kind_aliases_match_herdr_qoder_aliases():
    for alias in ("qoder", "qodercli", "qodercn", "qoderclicn"):
        assert mail_identity_inject.PRODUCT_KINDS[alias] == herdr_client.AGENT_KIND_ALIASES[alias]


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
    assert "ACTIVATION_BLOCK" in result.stderr


def test_session_created_injects_context_through_opencode_client(tmp_path):
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    inject = bin_dir / "mail-identity-inject"
    inject.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$HOME/inject-args\"\n"
        "printf '%s\\n' '{\"hookSpecificOutput\":"
        "{\"hookEventName\":\"SessionStart\","
        "\"additionalContext\":\"trusted identity\"}}'\n",
        encoding="utf-8",
    )
    inject.chmod(0o700)
    check = bin_dir / "mail-hook-check"
    check.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$HOME/check-args\"\n"
        "printf '%s\\n' '{\"hookSpecificOutput\":{\"hookEventName\":"
        "\"UserPromptSubmit\",\"additionalContext\":\"unread\"}}'\n",
        encoding="utf-8",
    )
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
        "await hooks['chat.message']();\n"
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
    assert (home / "inject-args").read_text().splitlines() == ["opencode"]
    assert (home / "check-args").read_text().splitlines() == ["opencode", "main"]


@pytest.mark.parametrize(
    ("agent", "instance"),
    [
        ("codex", INSTANCE),
        ("codex", "同名"),
        ("unknown", "main"),
    ],
)
def test_legacy_hook_rejects_opaque_display_and_unknown_arguments(
    monkeypatch, capsys, agent, instance,
):
    monkeypatch.setattr(mail_identity_inject, "resolve_managed_identity", lambda: None)
    monkeypatch.setattr(mail_identity_inject, "_has_managed_descriptor_candidate", lambda: False)
    monkeypatch.setattr(
        mail_hook_check.mail_recv, "main",
        lambda _argv: (_ for _ in ()).throw(AssertionError("invalid legacy不得查未读")),
    )

    mail_identity_inject.main([agent, instance])
    assert capsys.readouterr().out == ""
    assert mail_hook_check.main([agent, instance]) == 2


def test_mail_hook_check_help_returns_zero(capsys):
    assert mail_hook_check.main(["--help"]) == 0
    assert mail_hook_check.main(["-h"]) == 0
    assert "usage: mail-hook-check" in capsys.readouterr().out


def test_mail_hook_check_uses_only_resolved_exact_identity(monkeypatch):
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

    assert mail_hook_check.main(["claude", "main"]) == 0
    assert seen == [[
        "--agent", "zcode", "--instance", INSTANCE,
        "--project", "/project", "--unread", "--peek",
    ]]


def test_managed_identity_inject_ignores_spoofed_legacy_arguments(monkeypatch, capsys):
    resolved = mail_identity_inject.ManagedIdentity(
        "/project", "zcode", INSTANCE, {"name": "FreshMailbox"},
    )
    monkeypatch.setattr(
        mail_identity_inject, "resolve_managed_identity", lambda: resolved,
    )

    mail_identity_inject.main(["claude", "main"])

    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "FreshMailbox" in context
    assert f"--agent zcode --instance {INSTANCE}" in context


@pytest.mark.parametrize(
    ("agent", "instance"),
    [("codex", "winjd"), ("claude", "main"), ("opencode", "main")],
)
def test_mail_hook_check_restores_legacy_unread_identity(
    monkeypatch, tmp_path, agent, instance,
):
    _, project = _legacy_identity_fixture(
        monkeypatch, tmp_path, agent=agent, instance=instance,
    )
    seen = []
    monkeypatch.setattr(
        mail_hook_check.mail_recv, "main",
        lambda argv: seen.append(argv) or print("--- #1 [-] sender @ now"),
    )

    assert mail_hook_check.main([agent, instance]) == 0
    assert seen == [[
        "--agent", agent, "--instance", instance,
        "--project", project, "--unread", "--peek",
    ]]


def test_invalid_managed_hook_check_never_falls_back_to_legacy(monkeypatch):
    monkeypatch.setattr(mail_identity_inject, "resolve_managed_identity", lambda: None)
    monkeypatch.setattr(mail_identity_inject, "_has_managed_descriptor_candidate", lambda: True)
    monkeypatch.setattr(
        mail_hook_check.mail_recv, "main",
        lambda _argv: (_ for _ in ()).throw(AssertionError("invalid managed不得查legacy")),
    )

    assert mail_hook_check.main(["codex", "winjd"]) == 0


def test_mail_hook_check_peek_does_not_create_or_change_receipt(
    monkeypatch, tmp_path, capsys,
):
    import coordination

    project = tmp_path / "project"
    project.mkdir()
    resolved = mail_identity_inject.ManagedIdentity(
        str(project), "zcode", INSTANCE, {"name": "FreshMailbox"},
    )
    monkeypatch.setattr(
        mail_hook_check.mail_identity_inject,
        "resolve_managed_identity",
        lambda: resolved,
    )
    monkeypatch.setattr(
        mail_hook_check.mail_recv, "load_identity",
        lambda *_args: (
            {
                "project_key": str(project), "name": "FreshMailbox",
                "registration_token": "token",
            },
            "http://hub", "hub-token",
        ),
    )
    monkeypatch.setattr(mail_hook_check.mail_recv, "mcp_call", lambda *_a, **_k: {})
    monkeypatch.setattr(
        mail_hook_check.mail_recv, "mcp_tool",
        lambda *_a, **_k: [{
            "id": 701, "from": "Sender", "subject": "review",
            "importance": "normal", "created_at": "2026-08-12T00:00:00Z",
            "body_md": "must remain unclaimed",
        }],
    )

    assert coordination.receipt(str(project), "FreshMailbox", 701) is None
    assert mail_hook_check.main([]) == 0
    assert coordination.receipt(str(project), "FreshMailbox", 701) is None
    assert "#701" in capsys.readouterr().out


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


def test_server_retire_chain_writes_registry_tombstone_before_descriptor_finalizes(
    monkeypatch, tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    descriptors = tmp_path / "descriptors.json"
    registry_root = tmp_path / "registry"
    monkeypatch.setenv("COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(descriptors))
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p1", name=INSTANCE, kind="opencode",
        args=[], agent="zcode", workdir=str(project), instance_id=INSTANCE,
        display_name="同名",
    )
    herdr_client.update_launch_descriptor_by_instance(
        INSTANCE, mail_agent="zcode", mail_instance=INSTANCE,
        mail_name="OldMailbox", mail_project=str(project),
    )
    herdr_client.mark_launch_descriptor_retirement_pending("demo", "w1:p1")
    registry_file = (
        registry_root / am_retire.slugify(str(project)) / f"zcode--{INSTANCE}.json"
    )
    _secure_json(registry_file, {
        "project_key": str(project), "project_slug": am_retire.slugify(str(project)),
        "agent": "zcode", "instance": INSTANCE, "name": "OldMailbox",
        "registration_token": "registration-token", "program": "zcode",
        "model": "unknown", "hub": "http://hub", "status": "active",
    })
    monkeypatch.setattr(am_retire, "REGISTRY_DIR", registry_root)
    monkeypatch.setattr(am_retire, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(am_retire, "mcp_call", lambda *_a, **_k: {})
    monkeypatch.setattr(
        am_retire, "mcp_tool",
        lambda _hub, _token, name, _args: (
            {"status": "retired", "retired_at": "2026-08-12T00:00:00Z"}
            if name == "retire_agent" else
            {"name": "OldMailbox", "retired_at": "2026-08-12T00:00:00Z"}
        ),
    )
    retire_script = tmp_path / "am-retire"
    retire_script.touch()
    monkeypatch.setattr(server, "AM_RETIRE_SCRIPT", retire_script)

    def run_retire(argv, **_kwargs):
        assert argv[0] == str(retire_script)
        am_retire.main(list(argv[1:]))
        return subprocess.CompletedProcess(argv, 0, "retired", "")

    monkeypatch.setattr(server.subprocess, "run", run_retire)

    assert server._retire_agent_instance(INSTANCE) == {
        "instance_id": INSTANCE, "retired": True,
    }
    tombstone = json.loads(registry_file.read_text(encoding="utf-8"))
    assert tombstone["status"] == "retired"
    assert tombstone["retired_at"]
    descriptor = herdr_client.get_launch_descriptor_by_instance(
        INSTANCE, include_retired=True,
    )
    assert descriptor["state"] == "retired"
