import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "agent-mail-tools"
PROJECT = "/tmp/project"


def _load_mail_send():
    path = TOOLS / "mail-send"
    loader = importlib.machinery.SourceFileLoader("cockpit_mail_send", str(path))
    spec = importlib.util.spec_from_file_location(
        "cockpit_mail_send", str(path), loader=loader
    )
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_bound_session_routes_to_unique_pane_even_when_cwd_differs():
    module = _load_mail_send()
    candidates = [
        ("unrelated", "w1:p1", "/other", False),
        ("bound", "w1:p2", "/worktree", True),
    ]

    assert module._select_notify_targets(candidates, PROJECT) == [
        ("bound", "w1:p2", "/worktree", False)
    ]


def test_multiple_panes_bound_to_same_mailbox_are_not_all_woken():
    module = _load_mail_send()

    assert module._select_notify_targets([
        ("one", "w1:p1", "/a", True),
        ("two", "w1:p2", "/b", True),
    ], PROJECT) == []


def test_multiple_exact_cwd_matches_are_not_all_woken():
    module = _load_mail_send()

    assert module._select_notify_targets([
        ("one", "w1:p1", PROJECT, False),
        ("two", "w1:p2", PROJECT, False),
    ], PROJECT) == []


def test_different_session_binding_overrides_matching_pane_cwd():
    module = _load_mail_send()

    assert module._select_notify_targets([
        ("other", "w1:p1", PROJECT, False, True),
    ], PROJECT) == []


def test_notification_uses_real_project_and_packaged_receiver():
    module = _load_mail_send()
    text = module._notify_text(7, "review", "codex", PROJECT, "/worktree", False)

    assert PROJECT in text
    assert str(TOOLS / "mail-recv") in text
    assert "当前 pane cwd=/worktree" in text


def test_session_binding_requires_matching_session_directory(tmp_path):
    module = _load_mail_send()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    bindings = {"demo": {"session_dir": str(first), "project": PROJECT}}

    assert module._session_bound_to(
        bindings, {"name": "demo", "directory": str(first)}, PROJECT
    ) is True
    assert module._session_bound_to(
        bindings, {"name": "demo", "directory": str(second)}, PROJECT
    ) is False


def test_mail_send_source_uses_loaded_identity_project_key():
    source = (TOOLS / "mail-send").read_text(encoding="utf-8")

    assert 'project_key = identity["project_key"]' in source
    assert 'delivery.get("project")' not in source


def test_db_path_explicit_value_wins_even_when_missing(monkeypatch, tmp_path):
    module = _load_mail_send()
    missing = tmp_path / "missing.sqlite3"
    monkeypatch.setenv("AGENT_MAIL_DB_PATH", str(missing))

    assert module._agent_mail_db_path() == str(missing)


def test_db_path_prefers_xdg_then_keeps_legacy_compatible(monkeypatch, tmp_path):
    module = _load_mail_send()
    xdg = tmp_path / "xdg"
    new = xdg / "mcp_agent_mail" / "storage.sqlite3"
    legacy = tmp_path / "mcp_agent_mail" / "storage.sqlite3"
    new.parent.mkdir(parents=True)
    legacy.parent.mkdir(parents=True)
    new.touch()
    legacy.touch()
    monkeypatch.delenv("AGENT_MAIL_DB_PATH", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert module._agent_mail_db_path() == str(new)
    new.unlink()
    assert module._agent_mail_db_path() == str(legacy)


def test_init_project_registers_claude_and_qoder_command_identity():
    source = (TOOLS / "am-init-project").read_text(encoding="utf-8")

    assert '"claude|claude-code|unknown"' in source
    assert '"qodercn|qoder-cn|unknown"' in source


def test_qoder_notification_uses_registered_command_identity():
    module = _load_mail_send()

    assert module.PROG_TO_AGENT["qoder-cn"] == "qodercn"
    assert module.PROG_TO_AGENT["qodercli"] == "qodercn"
    assert module.PROG_TO_AGENT["qodercn"] == "qodercn"


def test_register_does_not_fabricate_agent_main_name():
    source = (TOOLS / "am-register").read_text(encoding="utf-8")

    assert 'f"{args.agent}-{args.instance}"' not in source
    assert 'if args.name:' in source
    assert "已有身份无效或已 retired；不会自动覆盖" in source
    assert "--force" in source


def test_packaged_tools_do_not_contain_client_credentials():
    contents = "\n".join(
        path.read_text(encoding="utf-8")
        for path in TOOLS.iterdir() if path.is_file()
    )

    assert "100.66.1.5" not in contents
    assert "registration_token=" not in contents
