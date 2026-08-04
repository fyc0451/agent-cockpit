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


def _load_mail_recv():
    path = TOOLS / "mail-recv"
    loader = importlib.machinery.SourceFileLoader("cockpit_mail_recv", str(path))
    spec = importlib.util.spec_from_file_location(
        "cockpit_mail_recv", str(path), loader=loader
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
    assert "--message 7" in text


def test_blocking_notification_requires_safe_checkpoint_then_complete():
    module = _load_mail_send()
    text = module._notify_text(
        9, "blocker", "codex", PROJECT, PROJECT, True, "blocking"
    )

    assert "打断请求" in text
    assert "安全停手" in text
    assert "checkpoint" in text


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


def test_mail_recv_ack_failure_keeps_processed_receipt_and_retry_suppresses_body(
    monkeypatch, tmp_path, capsys,
):
    import coordination

    module = _load_mail_recv()
    monkeypatch.setattr(coordination, "DB_PATH", tmp_path / "coordination.sqlite3")
    (tmp_path / "lead").mkdir()
    (tmp_path / "dev").mkdir()
    coordination.start_run(
        project_key=str(tmp_path), session="demo", session_dir=str(tmp_path),
        participants=[
            {
                "id": "lead", "agent": "codex", "mail_name": "codex-main",
                "pane_id": "w1:p1", "role": "lead", "task": "实现",
                "workdir": str(tmp_path / "lead"),
            },
            {
                "id": "dev", "agent": "kimi", "mail_name": "kimi-main",
                "pane_id": "w1:p2", "role": "developer", "task": "验证",
                "workdir": str(tmp_path / "dev"),
            },
        ], now=100,
    )
    meta, _ = coordination.prepare_metadata(
        project_key=str(tmp_path), sender="codex-main", recipients=["kimi-main"],
        intent="blocking", now=101,
    )
    message = {
        "id": 41, "from": "codex-main", "subject": "复核",
        "body_md": coordination.add_metadata("只执行一次", meta),
        "importance": "high", "created_at": "2026-08-04T06:41:57.890908+00:00",
    }
    identity = {
        "project_key": str(tmp_path), "name": "kimi-main",
        "registration_token": "test-token",
    }
    monkeypatch.setattr(module, "load_identity", lambda *_: (identity, "hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_args, **_kwargs: {})
    ack_fails = {"value": True}

    def tool(_hub, _token, name, _args):
        if name == "fetch_inbox":
            return [message]
        if name == "acknowledge_message" and ack_fails["value"]:
            raise SystemExit("offline")
        return {}

    monkeypatch.setattr(module, "mcp_tool", tool)
    base = [
        "mail-recv", "--agent", "kimi", "--project", str(tmp_path),
    ]
    monkeypatch.setattr(module.sys, "argv", [*base, "--unread"])
    module.main()
    assert "只执行一次" in capsys.readouterr().out

    claim_token = coordination.receipt(
        str(tmp_path), "kimi-main", 41
    )["claim_token"]
    monkeypatch.setattr(
        module.sys, "argv",
        [*base, "--complete", "41", "--claim-token", claim_token],
    )
    module.main()
    completed = capsys.readouterr()
    assert '"acked": false' in completed.out.lower()
    assert coordination.receipt(str(tmp_path), "kimi-main", 41)["state"] == "processed"

    ack_fails["value"] = False
    monkeypatch.setattr(module.sys, "argv", [*base, "--unread"])
    module.main()
    retried = capsys.readouterr().out
    assert "只执行一次" not in retried
    assert "no actionable messages" in retried
    assert coordination.receipt(str(tmp_path), "kimi-main", 41)["ack_pending"] == 0
