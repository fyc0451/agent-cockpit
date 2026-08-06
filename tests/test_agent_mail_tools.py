import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import urllib.error

import pytest


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


def _load_task_report():
    path = TOOLS / "task-report"
    loader = importlib.machinery.SourceFileLoader("cockpit_task_report", str(path))
    spec = importlib.util.spec_from_file_location(
        "cockpit_task_report", str(path), loader=loader
    )
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _load_tool(name, module_name):
    path = TOOLS / name
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_file_location(module_name, str(path), loader=loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _load_am_common():
    return _load_tool("am_common.py", "cockpit_am_common")


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
    text = module._notify_text(
        7, "review", "codex", "worker-2", PROJECT, "/worktree", False
    )

    assert PROJECT in text
    assert str(TOOLS / "mail-recv") in text
    assert "--instance worker-2" in text
    assert "当前 pane cwd=/worktree" in text
    assert "--message 7" in text


def test_blocking_notification_requires_safe_checkpoint_then_complete():
    module = _load_mail_send()
    text = module._notify_text(
        9, "blocker", "codex", "main", PROJECT, PROJECT, True, "blocking"
    )

    assert "打断请求" in text
    assert "安全停手" in text
    assert "checkpoint" in text


def test_notification_resolves_registered_instance(tmp_path):
    module = _load_mail_send()
    module.REGISTRY_DIR = tmp_path
    registry = tmp_path / module.slugify(PROJECT)
    registry.mkdir()
    (registry / "codex--worker-2.json").write_text(json.dumps({
        "project_key": PROJECT,
        "name": "BlueLake",
        "agent": "codex",
        "instance": "worker-2",
    }))

    assert module._notification_identity("BlueLake", PROJECT) == (
        "codex", "worker-2"
    )


def test_mcp_call_parses_multiline_sse(monkeypatch):
    module = _load_am_common()
    urls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return (
                b'event: message\n'
                b'data: {"jsonrpc":"2.0",\n'
                b'data: "id":1,"result":{"ok":true}}\n\n'
            )

    def open_request(request, **_kwargs):
        urls.append(request.full_url)
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", open_request)

    assert module.mcp_call("http://hub", "token", "test", {})["result"] == {"ok": True}
    assert urls == ["http://hub/api/"]


def test_mcp_call_reports_network_and_malformed_response(monkeypatch):
    module = _load_am_common()
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )
    with pytest.raises(SystemExit, match="请求失败"):
        module.mcp_call("http://hub", "token", "test", {})

    class BadResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return b"not-json"

    monkeypatch.setattr(
        module.urllib.request, "urlopen", lambda *args, **kwargs: BadResponse()
    )
    with pytest.raises(SystemExit, match="响应解析失败"):
        module.mcp_call("http://hub", "token", "test", {})


def test_mail_recv_strips_terminal_control_sequences():
    module = _load_tool("mail-recv", "cockpit_mail_recv")
    value = "hello\x1b]52;c;c2VjcmV0\x07\nworld\x1b[31m!\x1b[0m\x00"

    assert module._terminal_text(value) == "hello\nworld!"


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
    assert module._agent_types_match("qodercn", "qodercli") is True
    assert module._agent_types_match("qodercn", "qoder") is True
    assert module._agent_types_match("qodercn", "codex") is False


def test_qodercn_notification_prompts_qodercli_pane(monkeypatch, tmp_path):
    module = _load_mail_send()
    herdr = tmp_path / "herdr"
    herdr.touch()
    session_dir = tmp_path / "session"
    project = tmp_path / "project"
    worktree = tmp_path / "worktree"
    for item in (session_dir, project, worktree):
        item.mkdir()
    monkeypatch.setattr(module, "HERDR_BIN", str(herdr))
    monkeypatch.setattr(
        module, "_session_rows",
        lambda _: [{"name": "demo", "running": True, "directory": str(session_dir)}],
    )
    monkeypatch.setattr(module, "_load_bindings", lambda: {"demo": {
        "session_dir": str(session_dir), "project": str(project),
    }})
    prompts = []

    def run(args, **kwargs):
        if args[-2:] == ["api", "snapshot"]:
            return module.subprocess.CompletedProcess(args, 0, json.dumps({
                "result": {"snapshot": {"panes": [{
                    "pane_id": "w1:pA", "agent": "qodercli", "cwd": str(worktree),
                }]}}
            }), "")
        if "prompt" in args:
            prompts.append(args)
            return module.subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(module.subprocess, "run", run)

    module._notify_pane(
        "qodercn", "main", 860, "身份通知链确认", str(project)
    )

    assert len(prompts) == 1
    assert prompts[0][2:6] == ["demo", "agent", "prompt", "w1:pA"]
    assert "--agent qodercn" in prompts[0][-1]
    assert "--message 860" in prompts[0][-1]


def test_identity_hook_uses_validated_herdr_session_binding(monkeypatch, tmp_path):
    module = _load_tool("mail-identity-inject", "cockpit_mail_identity_inject")
    home = tmp_path / "home"
    session_dir = home / ".config" / "herdr" / "sessions" / "demo"
    worktree = tmp_path / "project-worktree"
    project = tmp_path / "project"
    for item in (session_dir, worktree, project):
        item.mkdir(parents=True)
    state = tmp_path / "mail-projects.json"
    state.write_text(json.dumps({"sessions": {"demo": {
        "session_dir": str(session_dir), "project": str(project),
    }}}))
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: home))

    assert module._canonical_project(
        str(worktree), "demo", state, str(session_dir / "herdr.sock")
    ) == str(project.resolve())

    state.write_text(json.dumps({"sessions": {"demo": {
        "session_dir": str(tmp_path / "wrong"), "project": str(project),
    }}}))
    assert module._canonical_project(
        str(worktree), "demo", state, str(session_dir / "herdr.sock")
    ) == str(worktree.resolve())


def test_register_does_not_fabricate_agent_main_name():
    source = (TOOLS / "am-register").read_text(encoding="utf-8")

    assert 'f"{args.agent}-{args.instance}"' not in source
    assert 'if args.name:' in source
    assert 'mcp_tool(hub, token, "whois"' in source
    assert 'mcp_tool(hub, token, "unretire_agent"' in source
    assert "--force" in source


def _load_am_register():
    path = TOOLS / "am-register"
    loader = importlib.machinery.SourceFileLoader("cockpit_am_register", str(path))
    spec = importlib.util.spec_from_file_location(
        "cockpit_am_register", str(path), loader=loader
    )
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_register_rejects_unsafe_agent_component(monkeypatch):
    """agent/instance 直接拼进 registry 路径,必须拒绝路径穿越字符。"""
    module = _load_am_register()
    for bad in ("../evil", "a/b", "a\\b", "a b", ".", "-x", ""):
        with pytest.raises(SystemExit, match="仅允许"):
            module._validate_component(bad, "agent")


def test_register_accepts_safe_component():
    module = _load_am_register()
    assert module._validate_component("codex-main", "agent") == "codex-main"
    assert module._validate_component("qoder-cn.2", "instance") == "qoder-cn.2"


def test_register_atomic_write_0600(tmp_path):
    """身份文件必须原子写且权限 0600,中间态不得暴露 token。"""
    module = _load_am_register()
    target = tmp_path / "identity.json"
    identity = {"name": "demo-main", "registration_token": "secret-token"}
    module._atomic_write_identity(target, identity)

    assert (target.stat().st_mode & 0o777) == 0o600
    assert json.loads(target.read_text()) == identity
    # 目录内不留临时文件
    assert [p.name for p in tmp_path.iterdir()] == ["identity.json"]


def test_register_atomic_write_preserves_existing_on_failure(tmp_path, monkeypatch):
    """原子写中途失败(如 replace 抛错)不得破坏已存在的旧身份。"""
    module = _load_am_register()
    target = tmp_path / "identity.json"
    old = {"name": "old-main", "registration_token": "old-token"}
    target.write_text(json.dumps(old))
    target.chmod(0o600)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        module._atomic_write_identity(target, {"name": "new"})
    # 旧身份原样保留
    assert json.loads(target.read_text()) == old
    assert (target.stat().st_mode & 0o777) == 0o600
    # 临时文件被清理
    assert [p.name for p in tmp_path.iterdir()] == ["identity.json"]


def test_register_reuse_restores_retired_identity(tmp_path, monkeypatch, capsys):
    """registry 中的 token 应自动恢复 retired 身份后再复用。"""
    module = _load_am_register()
    module.REGISTRY_DIR = tmp_path
    project = tmp_path / "proj"
    project.mkdir()
    registry_dir = tmp_path / module.slugify(str(project))
    registry_dir.mkdir()
    registry_file = registry_dir / "codex--default.json"
    registry_file.write_text(json.dumps({
        "project_key": str(project),
        "name": "codex-main",
        "registration_token": "registration-token",
    }))
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_args, **_kwargs: {})
    calls = []

    def tool(_hub, _token, name, args):
        calls.append((name, args))
        if name == "whois":
            return {"name": "codex-main", "retired_at": "2026-08-02T00:00:00Z"}
        if name == "unretire_agent":
            return {"status": "active"}
        if name == "fetch_inbox":
            return []
        raise AssertionError(name)

    monkeypatch.setattr(module, "mcp_tool", tool)
    monkeypatch.setattr(module.sys, "argv", [
        "am-register", "--agent", "codex", "--project", str(project),
    ])

    module.main()

    assert [name for name, _args in calls] == ["whois", "unretire_agent", "fetch_inbox"]
    assert calls[0][1]["include_recent_commits"] is False
    captured = capsys.readouterr()
    assert "已自动恢复 retired 身份 codex-main" in captured.err
    assert "已注册（复用）: codex-main" in captured.out


def test_register_reuse_does_not_unretire_active_identity(tmp_path, monkeypatch):
    module = _load_am_register()
    module.REGISTRY_DIR = tmp_path
    project = tmp_path / "proj"
    project.mkdir()
    registry_dir = tmp_path / module.slugify(str(project))
    registry_dir.mkdir()
    (registry_dir / "codex--default.json").write_text(json.dumps({
        "project_key": str(project),
        "name": "codex-main",
        "registration_token": "registration-token",
    }))
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_args, **_kwargs: {})
    calls = []

    def tool(_hub, _token, name, _args):
        calls.append(name)
        return {"name": "codex-main"} if name == "whois" else []

    monkeypatch.setattr(module, "mcp_tool", tool)
    monkeypatch.setattr(module.sys, "argv", [
        "am-register", "--agent", "codex", "--project", str(project),
    ])

    module.main()

    assert calls == ["whois", "fetch_inbox"]


def test_register_reuse_rejects_malformed_whois(tmp_path, monkeypatch):
    module = _load_am_register()
    module.REGISTRY_DIR = tmp_path
    project = tmp_path / "proj"
    project.mkdir()
    registry_dir = tmp_path / module.slugify(str(project))
    registry_dir.mkdir()
    (registry_dir / "codex--default.json").write_text(json.dumps({
        "project_key": str(project),
        "name": "codex-main",
        "registration_token": "registration-token",
    }))
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "mcp_tool", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module.sys, "argv", [
        "am-register", "--agent", "codex", "--project", str(project),
    ])

    with pytest.raises(SystemExit, match="无法自动恢复"):
        module.main()


def test_register_force_skips_reuse_without_deleting_old(tmp_path, monkeypatch):
    """--force 不能先删旧身份;注册成功才原子覆盖,失败保留旧身份。"""
    import sys

    module = _load_am_register()
    module.REGISTRY_DIR = tmp_path
    project_key = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    slug = module.slugify(project_key)
    (tmp_path / slug).mkdir()
    registry_file = tmp_path / slug / "codex--default.json"
    old = {"project_key": project_key, "name": "old-main"}
    registry_file.write_text(json.dumps(old))
    registry_file.chmod(0o600)

    # force 下直接走新注册;模拟注册成功,验证旧文件被新身份原子覆盖。
    calls = []
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "tok"))
    monkeypatch.setattr(
        module, "mcp_call", lambda *a, **k: calls.append(("call",)) or {}
    )
    fake_tool = {
        "ensure_project": {"slug": "proj", "human_key": project_key},
        "register_agent": {
            "name": "new-main", "program": "codex", "model": "m",
            "registration_token": "new-token",
        },
        "set_contact_policy": {"ok": True},
    }
    monkeypatch.setattr(
        module, "mcp_tool",
        lambda hub, token, name, args: calls.append(("tool", name)) or fake_tool[name],
    )
    argv = [
        "am-register", "--agent", "codex", "--instance", "default",
        "--project", project_key, "--force",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    module.main()

    assert any(c[0] == "tool" and c[1] == "register_agent" for c in calls)
    assert json.loads(registry_file.read_text())["name"] == "new-main"
    assert (registry_file.stat().st_mode & 0o777) == 0o600


def test_register_force_failure_keeps_old_identity(tmp_path, monkeypatch):
    """--force 但重新注册失败(register_agent 抛错):旧身份必须原样保留。"""
    import sys

    module = _load_am_register()
    module.REGISTRY_DIR = tmp_path
    project_key = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    slug = module.slugify(project_key)
    (tmp_path / slug).mkdir()
    registry_file = tmp_path / slug / "codex--default.json"
    old = {"project_key": project_key, "name": "old-main"}
    registry_file.write_text(json.dumps(old))
    registry_file.chmod(0o600)

    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "tok"))
    monkeypatch.setattr(module, "mcp_call", lambda *a, **k: {})
    monkeypatch.setattr(
        module, "mcp_tool",
        lambda hub, token, name, args: (_ for _ in ()).throw(
            SystemExit("MCP error: register failed")
        ),
    )
    argv = [
        "am-register", "--agent", "codex", "--instance", "default",
        "--project", project_key, "--force",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        module.main()

    assert json.loads(registry_file.read_text()) == old
    assert (registry_file.stat().st_mode & 0o777) == 0o600


def test_packaged_tools_do_not_contain_client_credentials():
    contents = "\n".join(
        path.read_text(encoding="utf-8")
        for path in TOOLS.iterdir() if path.is_file()
    )

    assert "100.66.1.5" not in contents
    assert "registration_token=" not in contents


def test_task_report_cli_submits_structured_progress(
    monkeypatch, tmp_path, capsys,
):
    import coordination

    module = _load_task_report()
    monkeypatch.setattr(coordination, "DB_PATH", tmp_path / "coordination.sqlite3")
    coordination.request_task_report(
        "demo", "w1:p1", "codex", "codex-main",
        request_id="request-1", now=10,
    )
    monkeypatch.setattr(module.sys, "argv", [
        "task-report", "--session", "demo", "--pane", "w1:p1",
        "--request-id", "request-1", "--progress", "75",
        "--summary", "完成实现", "--next", "执行回归", "--blocker", "",
    ])

    module.main()

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["progress"] == 75
    saved = coordination.task_report("demo", "w1:p1")
    assert saved["summary"] == "完成实现"
    assert saved["next_step"] == "执行回归"


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


def _write_registry(tmp_path, module, identities):
    registry = tmp_path / module.slugify(PROJECT)
    registry.mkdir(parents=True, exist_ok=True)
    for identity in identities:
        name = identity["name"]
        path = registry / f"{identity['agent']}--{identity['instance']}.json"
        path.write_text(json.dumps({"project_key": PROJECT, **identity}))
    module.REGISTRY_DIR = tmp_path


def test_resolve_recipients_maps_agent_type_to_unique_flower_name(tmp_path):
    module = _load_mail_send()
    _write_registry(tmp_path, module, [
        {"name": "qodercn-main", "agent": "qodercn", "instance": "main"},
    ])
    assert module._resolve_registry_recipients(["qodercn"], PROJECT) == ["qodercn-main"]


def test_resolve_recipients_maps_type_instance_alias(tmp_path):
    module = _load_mail_send()
    _write_registry(tmp_path, module, [
        {"name": "qodercn-main", "agent": "qodercn", "instance": "main"},
    ])
    assert module._resolve_registry_recipients(["qodercn-main"], PROJECT) == ["qodercn-main"]


def test_resolve_recipients_passes_known_flower_name_through(tmp_path):
    module = _load_mail_send()
    _write_registry(tmp_path, module, [
        {"name": "WindyBarn", "agent": "claude", "instance": "main"},
    ])
    assert module._resolve_registry_recipients(["WindyBarn"], PROJECT) == ["WindyBarn"]


def test_resolve_recipients_passes_unknown_name_through(tmp_path):
    module = _load_mail_send()
    _write_registry(tmp_path, module, [
        {"name": "qodercn-main", "agent": "qodercn", "instance": "main"},
    ])
    assert module._resolve_registry_recipients(["codex"], PROJECT) == ["codex"]


def test_resolve_recipients_ambiguous_type_raises(tmp_path):
    module = _load_mail_send()
    _write_registry(tmp_path, module, [
        {"name": "qodercn-main", "agent": "qodercn", "instance": "main"},
        {"name": "qodercn-rev", "agent": "qodercn", "instance": "rev"},
    ])
    with pytest.raises(SystemExit):
        module._resolve_registry_recipients(["qodercn"], PROJECT)


def test_resolve_recipients_no_registry_dir_passthrough(tmp_path):
    module = _load_mail_send()
    module.REGISTRY_DIR = tmp_path / "absent"
    assert module._resolve_registry_recipients(["qodercn"], PROJECT) == ["qodercn"]


def test_resolve_recipients_mixed_list(tmp_path):
    module = _load_mail_send()
    _write_registry(tmp_path, module, [
        {"name": "qodercn-main", "agent": "qodercn", "instance": "main"},
        {"name": "DarkBrook", "agent": "grok", "instance": "main"},
    ])
    assert module._resolve_registry_recipients(
        ["qodercn", "DarkBrook"], PROJECT
    ) == ["qodercn-main", "DarkBrook"]


def test_resolve_recipients_skips_entries_missing_agent_or_instance(tmp_path):
    module = _load_mail_send()
    _write_registry(tmp_path, module, [
        {"name": "BrokenEntry", "agent": None, "instance": None},
        {"name": "qodercn-main", "agent": "qodercn", "instance": "main"},
    ])
    assert module._resolve_registry_recipients(["None-None"], PROJECT) == ["None-None"]
    assert module._resolve_registry_recipients(["qodercn"], PROJECT) == ["qodercn-main"]
