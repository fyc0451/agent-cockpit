import importlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "agent-mail-tools"
PROJECT = "/tmp/project"


def _load_mail_send():
    return importlib.reload(importlib.import_module("agent_mail_commands.mail_send"))


def _load_mail_recv():
    return importlib.reload(importlib.import_module("agent_mail_commands.mail_recv"))


def _load_task_report():
    return importlib.reload(importlib.import_module("agent_mail_commands.task_report"))


def _load_team_work():
    return importlib.reload(importlib.import_module("agent_mail_commands.team_work"))


def _load_tool(name, module_name):
    del module_name
    package_name = name.replace("-", "_").removesuffix(".py")
    if package_name == "am_common":
        return importlib.import_module("agent_mail_commands.common")
    return importlib.reload(importlib.import_module(f"agent_mail_commands.{package_name}"))


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


def test_team_work_cli_reads_with_local_identity_only(monkeypatch, capsys):
    module = _load_team_work()
    monkeypatch.setattr(module, "load_identity", lambda *_args: ({
        "project_key": PROJECT,
        "name": "codex-main",
        "registration_token": "registration-secret",
    }, "http://hub", "hub-secret"))
    calls = []
    monkeypatch.setattr(
        module, "_post",
        lambda path, payload: calls.append((path, payload)) or {"status": "empty"},
    )

    module.main(["--agent", "codex", "--instance", "main", "--project", PROJECT])

    assert calls == [("/api/agent/team-work/next", {
        "mail_project": PROJECT,
        "sender_name": "codex-main",
        "registration_token": "registration-secret",
    })]
    output = capsys.readouterr().out
    assert "empty" in output
    assert "registration-secret" not in output


def test_team_work_cli_submits_explicit_reply(monkeypatch, capsys):
    module = _load_team_work()
    monkeypatch.setattr(module, "load_identity", lambda *_args: ({
        "project_key": PROJECT,
        "name": "codex-main",
        "registration_token": "registration-secret",
    }, "http://hub", "hub-secret"))
    calls = []
    monkeypatch.setattr(
        module, "_post",
        lambda path, payload: calls.append((path, payload)) or {
            "status": "draft_pending", "draft_id": 8,
        },
    )

    module.main([
        "--agent", "codex", "--instance", "main", "--project", PROJECT,
        "--work-id", "a" * 32, "--to", "alice", "--subject", "Re",
        "--body", "done",
    ])

    assert calls[0][0] == f"/api/agent/team-work/{'a' * 32}/respond"
    assert calls[0][1]["mention_handles"] == ["alice"]
    output = capsys.readouterr().out
    assert "draft_pending" in output
    assert "registration-secret" not in output


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


def test_notification_identity_rejects_retired_registry(tmp_path):
    module = _load_mail_send()
    module.REGISTRY_DIR = tmp_path
    registry = tmp_path / module.slugify(PROJECT)
    registry.mkdir()
    (registry / "zcode--i-aaaaaaaaaaaaaaaaaaaaaaaaaa.json").write_text(json.dumps({
        "project_key": PROJECT, "name": "Luna", "agent": "zcode",
        "instance": "i-aaaaaaaaaaaaaaaaaaaaaaaaaa", "status": "retired",
        "retired_at": "2026-08-12T00:00:00Z",
    }))

    assert module._notification_identity("Luna", PROJECT) is None


@pytest.mark.parametrize(
    "descriptor_change",
    [
        {"state": "retired"},
        {"mail_name": "StaleLuna"},
        {"mail_project": "/other/project"},
        {"mail_instance": "i-bbbbbbbbbbbbbbbbbbbbbbbbbb"},
        {"mail_agent": "opencode"},
    ],
)
def test_managed_notification_never_falls_back_from_invalid_descriptor(
    monkeypatch, tmp_path, descriptor_change,
):
    module = _load_mail_send()
    descriptors = tmp_path / "descriptors.json"
    instance = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    record = {
        "session": "demo", "pane_id": "w1:p1", "agent": "zcode",
        "kind": "opencode", "instance_id": instance, "name": instance,
        "state": "active", "workdir": "/worktree", "mail_agent": "zcode",
        "mail_instance": instance, "mail_name": "Luna", "mail_project": PROJECT,
    }
    record.update(descriptor_change)
    descriptors.write_text(json.dumps({
        "schema": 2, "descriptors": {f"instance|{instance}": record},
    }))
    monkeypatch.setattr(module, "LAUNCH_DESCRIPTORS_PATH", str(descriptors), raising=False)
    monkeypatch.setattr(
        module, "_select_notify_targets",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("managed identity不得降级到legacy fallback")
        ),
    )

    candidates = [("demo", "w1:p1", "/worktree", True, True, instance, "opencode")]
    assert module._managed_notify_target(
        candidates, PROJECT, "Luna", "zcode", instance,
    ) == []


def test_managed_notification_requires_unique_exact_live_runtime(monkeypatch, tmp_path):
    module = _load_mail_send()
    descriptors = tmp_path / "descriptors.json"
    instance = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    descriptor = {
        "session": "demo", "pane_id": "w1:p1", "agent": "zcode",
        "kind": "opencode", "instance_id": instance, "name": instance,
        "state": "active", "workdir": "/worktree", "mail_agent": "zcode",
        "mail_instance": instance, "mail_name": "Luna", "mail_project": PROJECT,
    }
    descriptors.write_text(json.dumps({
        "schema": 2, "descriptors": {f"instance|{instance}": descriptor},
    }))
    monkeypatch.setattr(module, "LAUNCH_DESCRIPTORS_PATH", str(descriptors), raising=False)
    stale = [("demo", "w1:p1", "/worktree", True, True, "old-name", "opencode")]
    exact = [("demo", "w1:p1", "/worktree", True, True, instance, "opencode")]

    assert module._managed_notify_target(stale, PROJECT, "Luna", "zcode", instance) == []
    assert module._managed_notify_target(exact, PROJECT, "Luna", "zcode", instance) == [
        ("demo", "w1:p1", "/worktree", False)
    ]


def test_managed_notification_malformed_descriptor_root_fails_empty(
    monkeypatch, tmp_path,
):
    module = _load_mail_send()
    descriptors = tmp_path / "descriptors.json"
    descriptors.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(module, "LAUNCH_DESCRIPTORS_PATH", str(descriptors))

    assert module._managed_notify_target(
        [], PROJECT, "Luna", "zcode", "i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
    ) == []


def test_duplicate_managed_descriptors_are_ambiguous(monkeypatch, tmp_path):
    module = _load_mail_send()
    descriptors = tmp_path / "descriptors.json"
    instance = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    record = {
        "session": "demo", "pane_id": "w1:p1", "agent": "zcode",
        "kind": "opencode", "instance_id": instance, "name": instance,
        "state": "active", "workdir": "/worktree", "mail_agent": "zcode",
        "mail_instance": instance, "mail_name": "Luna", "mail_project": PROJECT,
    }
    duplicate = {**record, "session": "other", "pane_id": "w1:p2"}
    descriptors.write_text(json.dumps({
        "schema": 2,
        "descriptors": {f"instance|{instance}": record, "duplicate": duplicate},
    }))
    monkeypatch.setattr(module, "LAUNCH_DESCRIPTORS_PATH", str(descriptors), raising=False)
    candidates = [
        ("demo", "w1:p1", "/worktree", True, True, instance, "opencode"),
        ("other", "w1:p2", "/worktree", True, True, instance, "opencode"),
    ]

    assert module._managed_notify_target(
        candidates, PROJECT, "Luna", "zcode", instance,
    ) == []


def test_notify_pane_managed_identity_prompts_only_exact_live_runtime(
    monkeypatch, tmp_path,
):
    module = _load_mail_send()
    herdr = tmp_path / "herdr"
    herdr.touch()
    project = tmp_path / "project"
    worktree = tmp_path / "worktree"
    session_dir = tmp_path / "session"
    for path in (project, worktree, session_dir):
        path.mkdir()
    instance = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    descriptors = tmp_path / "descriptors.json"
    descriptors.write_text(json.dumps({"schema": 2, "descriptors": {
        f"instance|{instance}": {
            "session": "demo", "pane_id": "w1:p1", "agent": "zcode",
            "kind": "opencode", "instance_id": instance, "name": instance,
            "state": "active", "workdir": str(worktree),
            "mail_agent": "zcode", "mail_instance": instance,
            "mail_name": "Luna", "mail_project": str(project),
        },
    }}))
    monkeypatch.setattr(module, "HERDR_BIN", str(herdr))
    monkeypatch.setattr(module, "LAUNCH_DESCRIPTORS_PATH", str(descriptors))
    monkeypatch.setattr(module, "_load_bindings", lambda: {})
    monkeypatch.setattr(module, "_recipient_typing", lambda *_args: False)
    monkeypatch.setattr(
        module, "_session_rows",
        lambda _env: [{"name": "demo", "running": True, "directory": str(session_dir)}],
    )
    prompts = []
    runtime_name = {"value": "stale-luna"}

    def run(args, **_kwargs):
        if args[-2:] == ["api", "snapshot"]:
            return module.subprocess.CompletedProcess(args, 0, json.dumps({
                "result": {"snapshot": {
                    "panes": [{
                        "pane_id": "w1:p1", "agent": "opencode",
                        "cwd": str(worktree), "agent_status": "idle",
                    }],
                    "agents": [{
                        "pane_id": "w1:p1", "agent": "opencode",
                        "name": runtime_name["value"],
                    }],
                }},
            }), "")
        if "prompt" in args:
            prompts.append(args)
            return module.subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(module.subprocess, "run", run)

    module._notify_pane(
        "zcode", instance, 900, "stale", str(project),
        mail_name="Luna",
    )
    assert prompts == []

    runtime_name["value"] = instance
    pane = module._session_panes("demo", module._herdr_env())[0]
    assert pane["_runtime_name"] == instance
    assert pane["_runtime_kind"] == "opencode"
    assert module._managed_notify_target(
        [("demo", "w1:p1", str(worktree), False, False,
          pane["_runtime_name"], pane["_runtime_kind"])],
        str(project), "Luna", "zcode", instance,
    ) == [("demo", "w1:p1", str(worktree), False)]
    module._notify_pane(
        "zcode", instance, 901, "exact", str(project),
        mail_name="Luna",
    )
    assert len(prompts) == 1
    assert prompts[0][3:6] == ["agent", "prompt", "w1:p1"]


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
    source = (ROOT / "agent_mail_commands" / "mail_send.py").read_text(encoding="utf-8")

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
    source = (ROOT / "agent_mail_commands" / "am_init_project.py").read_text(encoding="utf-8")

    assert '("claude", "claude-code", "unknown")' in source
    assert '("qodercn", "qoder-cn", "unknown")' in source


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
    source = (ROOT / "agent_mail_commands" / "am_register.py").read_text(encoding="utf-8")

    assert 'f"{args.agent}-{args.instance}"' not in source
    assert 'if args.name:' in source
    assert 'mcp_tool(hub, token, "whois"' in source
    assert 'mcp_tool(hub, token, "unretire_agent"' not in source
    assert "--force" in source


def _load_am_register():
    return importlib.reload(importlib.import_module("agent_mail_commands.am_register"))


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


def test_register_rejects_flower_name_as_agent(tmp_path, monkeypatch):
    """花名(CamelCase)不能当 --agent：否则注册出 agent=花名 的幽灵身份。"""
    module = _load_am_register()
    module.REGISTRY_DIR = tmp_path
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(module.sys, "argv", [
        "am-register", "--agent", "LilacMountain", "--project", str(project),
    ])
    with pytest.raises(SystemExit, match="不是花名"):
        module.main()


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


@pytest.mark.parametrize("retired_marker", [
    {"retired_at": "2026-08-02T00:00:00Z"},
    {"status": "retired"},
])
def test_register_reuse_rejects_retired_identity(
    tmp_path, monkeypatch, retired_marker,
):
    """retired identity 不得恢复，否则同名新实例会收到旧收件箱。"""
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
    registry_file.chmod(0o600)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_args, **_kwargs: {})
    calls = []

    def tool(_hub, _token, name, args):
        calls.append((name, args))
        if name == "whois":
            return {"name": "codex-main", **retired_marker}
        raise AssertionError(name)

    monkeypatch.setattr(module, "mcp_tool", tool)
    monkeypatch.setattr(module.sys, "argv", [
        "am-register", "--agent", "codex", "--project", str(project),
    ])

    with pytest.raises(SystemExit, match="禁止恢复或复用"):
        module.main()

    assert [name for name, _args in calls] == ["whois"]
    assert calls[0][1]["include_recent_commits"] is False


def test_register_reuse_does_not_unretire_active_identity(tmp_path, monkeypatch):
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
    registry_file.chmod(0o600)
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
    registry_file = registry_dir / "codex--default.json"
    registry_file.write_text(json.dumps({
        "project_key": str(project),
        "name": "codex-main",
        "registration_token": "registration-token",
    }))
    registry_file.chmod(0o600)
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
    from agent_cockpit import coordination

    module = _load_task_report()
    monkeypatch.setattr(coordination, "DB_PATH", tmp_path / "coordination.sqlite3")
    coordination.request_task_report(
        "demo", "w1:p1", "codex", "codex-main",
        request_id="request-1", now=10,
    )
    argv = [
        "--session", "demo", "--pane", "w1:p1",
        "--request-id", "request-1", "--progress", "75",
        "--summary", "完成实现", "--next", "执行回归", "--blocker", "",
    ]

    module.main(argv)

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["progress"] == 75
    saved = coordination.task_report("demo", "w1:p1")
    assert saved["summary"] == "完成实现"
    assert saved["next_step"] == "执行回归"


def test_mail_recv_ack_failure_keeps_processed_receipt_and_retry_suppresses_body(
    monkeypatch, tmp_path, capsys,
):
    from agent_cockpit import coordination

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


def test_resolve_recipients_uses_session_leader_not_program_main(tmp_path, monkeypatch):
    from agent_cockpit import chat_roster

    module = _load_mail_send()
    monkeypatch.setattr(chat_roster, "LEADERS_DIR", tmp_path / "leaders")
    chat_roster.set_session_leader("cockpit", "BrownDesert", "grok")
    _write_registry(tmp_path, module, [
        {"name": "JadeBay", "agent": "grok", "instance": "main"},
        {"name": "BrownDesert", "agent": "grok", "instance": "i-yzh33bkopbhev3ae654tc7tila"},
        {"name": "codex-main", "agent": "codex", "instance": "main"},
    ])
    assert module._resolve_registry_recipients(
        ["leader"], PROJECT, session="cockpit",
    ) == ["BrownDesert"]
    assert module._resolve_registry_recipients(
        ["grok-main"], PROJECT, session="cockpit",
    ) == ["BrownDesert"]


def test_bound_mail_thread_rejects_other_workspace_session(monkeypatch):
    module = _load_mail_send()
    monkeypatch.setattr(
        module.chat_ledger, "get_thread_by_session",
        lambda name: {
            "cockpit": {"workspace_id": "ws_cockpit"},
            "scc-1": {"workspace_id": "ws_scc"},
        }.get(name),
    )
    assert module.bound_mail_thread("scc-1", "scc-1") == "scc-1"
    assert module.bound_mail_thread("", "scc-1") == "scc-1"
    assert module.bound_mail_thread("cockpit", "cockpit") == "cockpit"
    try:
        module.bound_mail_thread("cockpit", "scc-1")
    except SystemExit as exc:
        assert "scc-1" in str(exc)
        assert "cockpit" in str(exc)
    else:
        raise AssertionError("expected SystemExit")


def test_bound_mail_thread_fail_closed_when_own_session_unregistered(monkeypatch):
    module = _load_mail_send()
    # platform 的 session 不在账本里（mine=None），写已注册的 cockpit thread 必须拒绝。
    monkeypatch.setattr(
        module.chat_ledger, "get_thread_by_session",
        lambda name: {"cockpit": {"workspace_id": "ws_cockpit"}}.get(name),
    )
    try:
        module.bound_mail_thread("cockpit", "pitapat-video-platform-1")
    except SystemExit as exc:
        assert "pitapat-video-platform-1" in str(exc)
        assert "cockpit" in str(exc)
    else:
        raise AssertionError("expected SystemExit")
    # dest 不是已注册 thread 时保持放行（自定义 thread 名）。
    assert module.bound_mail_thread("scratch", "pitapat-video-platform-1") == "scratch"


def test_resolve_recipients_rewrites_program_main_to_unique_flower(tmp_path):
    module = _load_mail_send()
    _write_registry(tmp_path, module, [
        {"name": "kimi-main", "agent": "kimi", "instance": "main"},
        {"name": "FoggyBasin", "agent": "kimi", "instance": "i-5plnoam2zkgklftfrwrel7qc44", "status": "active"},
    ])
    assert module._resolve_registry_recipients(["kimi-main"], PROJECT) == ["FoggyBasin"]
    assert module._resolve_registry_recipients(["kimi"], PROJECT) == ["FoggyBasin"]


def test_resolve_recipients_passes_known_flower_name_through(tmp_path):
    module = _load_mail_send()
    _write_registry(tmp_path, module, [
        {"name": "WindyBarn", "agent": "claude", "instance": "main"},
    ])
    assert module._resolve_registry_recipients(["WindyBarn"], PROJECT) == ["WindyBarn"]


def _run_send_main(module, monkeypatch, tmp_path, recipients, calls):
    identity = {
        "project_key": str(tmp_path.resolve()),
        "name": "codex-main",
        "registration_token": "registration-secret",
    }
    monkeypatch.setattr(module, "load_identity", lambda *_args: (identity, "hub", "token"))
    monkeypatch.setattr(
        module.coordination, "prepare_metadata", lambda **_kwargs: ({"v": 1}, []),
    )
    monkeypatch.setattr(
        module.coordination,
        "add_metadata",
        lambda body, _meta: "[agent-cockpit-meta]internal[/agent-cockpit-meta]\n" + body,
    )
    monkeypatch.setattr(module.coordination, "register_message", lambda **_kwargs: None)
    monkeypatch.setattr(
        module, "mcp_call", lambda *_args, **_kwargs: calls.append(("initialize", None)),
    )

    def tool(_hub, _token, name, arguments):
        calls.append((name, arguments))
        return {"deliveries": [{
            "payload": {"id": 11, "to": list(arguments["to"]), "thread_id": None},
        }]}

    def team_reply(payload):
        calls.append(("team_reply", payload))
        return {"status": "delivered", "message_id": 12, "deliveries": [{
            "name": name, "status": "delivered_human_inbox", "reason": None,
        } for name in payload["mention_handles"]]}

    monkeypatch.setattr(module, "mcp_tool", tool)
    monkeypatch.setattr(module, "_team_reply", team_reply)
    monkeypatch.setattr(module.sys, "argv", [
        "mail-send", "--agent", "codex", "--project", str(tmp_path),
        "--to", recipients, "--subject", "测试", "--body", "正文", "--no-notify",
        "--idempotency-key", "stable-retry-key",
    ])
    module.main()


def test_mail_send_explicit_human_recipient_uses_only_cockpit_proxy(
    monkeypatch, tmp_path, capsys,
):
    module = _load_mail_send()
    calls = []

    _run_send_main(module, monkeypatch, tmp_path, "@fyc-mac", calls)

    assert [name for name, _args in calls] == ["team_reply"]
    payload = calls[0][1]
    assert payload["mail_project"] == str(tmp_path.resolve())
    assert payload["sender_name"] == "codex-main"
    assert payload["mention_handles"] == ["fyc-mac"]
    assert payload["idempotency_key"] == "stable-retry-key"
    assert payload["body_md"] == "正文"
    output = capsys.readouterr()
    assert "@fyc-mac -> Team" in output.out
    assert "registration-secret" not in output.out + output.err


def test_mail_send_mixed_recipients_split_local_agent_and_remote_human(
    monkeypatch, tmp_path,
):
    module = _load_mail_send()
    calls = []

    _run_send_main(module, monkeypatch, tmp_path, "kimi-main,@fyc-mac", calls)

    assert [name for name, _args in calls] == [
        "initialize", "send_message", "team_reply",
    ]
    assert calls[1][1]["to"] == ["kimi-main"]
    assert calls[1][1]["body_md"].startswith("[agent-cockpit-meta]")
    assert calls[2][1]["mention_handles"] == ["fyc-mac"]
    assert calls[2][1]["body_md"] == "正文"


def test_team_reply_retry_reuses_exact_request_body(monkeypatch):
    module = _load_mail_send()
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read(_limit):
            return b'{"status":"already_delivered","message_id":12,"deliveries":[]}'

    def urlopen(request, timeout):
        assert timeout == 35
        seen.append(request.data)
        if len(seen) == 1:
            raise urllib.error.URLError("temporary")
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    payload = {"idempotency_key": "same-key", "registration_token": "secret"}

    result = module._team_reply(payload)

    assert result["status"] == "already_delivered"
    assert seen == [seen[0], seen[0]]


def test_team_reply_url_reads_custom_port_from_environment(monkeypatch, tmp_path):
    module = _load_mail_send()
    monkeypatch.setattr(module, "INSTALL_ROOT", str(tmp_path))
    monkeypatch.setenv("COCKPIT_PORT", "18790")

    assert module._team_reply_url() == (
        "http://127.0.0.1:18790/api/agent/team-reply"
    )


def test_team_reply_url_reads_custom_port_from_dotenv(monkeypatch, tmp_path):
    module = _load_mail_send()
    monkeypatch.setattr(module, "INSTALL_ROOT", str(tmp_path))
    monkeypatch.delenv("COCKPIT_PORT", raising=False)
    (tmp_path / ".env").write_text(
        'export COCKPIT_PORT="18791" # custom\n', encoding="utf-8",
    )

    assert module._team_reply_url() == (
        "http://127.0.0.1:18791/api/agent/team-reply"
    )


@pytest.mark.parametrize("port", ["", "0", "65536", "not-a-port"])
def test_team_reply_url_rejects_invalid_port(monkeypatch, tmp_path, port):
    module = _load_mail_send()
    monkeypatch.setattr(module, "INSTALL_ROOT", str(tmp_path))
    monkeypatch.setenv("COCKPIT_PORT", port)

    with pytest.raises(SystemExit, match="COCKPIT_PORT"):
        module._team_reply_url()


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


def test_notify_skips_pane_while_recipient_typing(monkeypatch, tmp_path):
    """收件 session 用户正在 Web 终端输入时,跳过实时通知(消息保留未读)。"""
    module = _load_mail_send()
    herdr = tmp_path / "herdr"
    herdr.touch()
    session_dir = tmp_path / "session"
    project = tmp_path / "project"
    for item in (session_dir, project):
        item.mkdir()
    monkeypatch.setattr(module, "HERDR_BIN", str(herdr))
    monkeypatch.setattr(
        module, "_session_rows",
        lambda _: [{"name": "demo", "running": True, "directory": str(session_dir)}],
    )
    monkeypatch.setattr(module, "_load_bindings", lambda: {})
    typing_file = tmp_path / "typing.json"
    # 输入时记录的是落点 pane w1:pA;此后焦点切到 w1:p8 也不影响判定
    typing_file.write_text(
        json.dumps({"demo": {"panes": {"w1:pA": time.time()}}}), encoding="utf-8"
    )
    monkeypatch.setattr(module, "TYPING_STATE_PATH", str(typing_file))
    prompts = []

    def run(args, **kwargs):
        if args[-2:] == ["api", "snapshot"]:
            return module.subprocess.CompletedProcess(args, 0, json.dumps({
                "result": {"snapshot": {
                    "focused_pane_id": "w1:p8",
                    "panes": [{
                        "pane_id": "w1:pA", "agent": "kimi", "cwd": str(project),
                        "focused": False,
                    }],
                }}
            }), "")
        if "prompt" in args:
            prompts.append(args)
            return module.subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(module.subprocess, "run", run)

    # 目标 pane w1:pA 有未提交草稿(焦点已切走)→ 仍跳过
    module._notify_pane("kimi", "main", 900, "输入避让", str(project))
    assert prompts == []

    # 状态过期后恢复通知
    typing_file.write_text(
        json.dumps({"demo": {"panes": {"w1:pA": time.time() - 120}}}),
        encoding="utf-8",
    )
    module._notify_pane("kimi", "main", 901, "输入避让2", str(project))
    assert len(prompts) == 1


def test_notify_skips_working_agent_without_interrupting_current_turn(
    monkeypatch, tmp_path,
):
    """Herdr prompt 是合成用户输入；working Agent 必须只保留未读消息。"""
    module = _load_mail_send()
    herdr = tmp_path / "herdr"
    herdr.touch()
    session_dir = tmp_path / "session"
    project = tmp_path / "project"
    for item in (session_dir, project):
        item.mkdir()
    monkeypatch.setattr(module, "HERDR_BIN", str(herdr))
    monkeypatch.setattr(module, "TYPING_STATE_PATH", str(tmp_path / "none.json"))
    monkeypatch.setattr(
        module, "_session_rows",
        lambda _: [{"name": "demo", "running": True, "directory": str(session_dir)}],
    )
    monkeypatch.setattr(module, "_load_bindings", lambda: {})
    status = {"value": "working"}
    prompts = []

    def run(args, **kwargs):
        if args[-2:] == ["api", "snapshot"]:
            return module.subprocess.CompletedProcess(args, 0, json.dumps({
                "result": {"snapshot": {"panes": [{
                    "pane_id": "w1:pA", "agent": "kimi", "cwd": str(project),
                    "agent_status": status["value"],
                }]}},
            }), "")
        if "prompt" in args:
            prompts.append(args)
            return module.subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(module.subprocess, "run", run)

    module._notify_pane("kimi", "main", 905, "工作中避让", str(project))
    assert prompts == []

    status["value"] = "idle"
    module._notify_pane("kimi", "main", 906, "空闲可通知", str(project))
    assert len(prompts) == 1


def test_notify_not_deferred_when_typing_in_other_pane(monkeypatch, tmp_path):
    """pane 粒度:用户在同 session 另一个 pane 输入时,目标 pane 通知不避让。"""
    module = _load_mail_send()
    herdr = tmp_path / "herdr"
    herdr.touch()
    session_dir = tmp_path / "session"
    project = tmp_path / "project"
    for item in (session_dir, project):
        item.mkdir()
    monkeypatch.setattr(module, "HERDR_BIN", str(herdr))
    monkeypatch.setattr(
        module, "_session_rows",
        lambda _: [{"name": "demo", "running": True, "directory": str(session_dir)}],
    )
    monkeypatch.setattr(module, "_load_bindings", lambda: {})
    typing_file = tmp_path / "typing.json"
    # pA 记录已过期、p8 新鲜: 两 pane 窗口独立,投 pA 不避让
    typing_file.write_text(json.dumps({"demo": {"panes": {
        "w1:pA": time.time() - 120, "w1:p8": time.time(),
    }}}), encoding="utf-8")
    monkeypatch.setattr(module, "TYPING_STATE_PATH", str(typing_file))
    prompts = []

    def run(args, **kwargs):
        if args[-2:] == ["api", "snapshot"]:
            return module.subprocess.CompletedProcess(args, 0, json.dumps({
                "result": {"snapshot": {
                    "panes": [
                        {"pane_id": "w1:pA", "agent": "kimi", "cwd": str(project),
                         "focused": False},
                        {"pane_id": "w1:p8", "agent": "claude", "cwd": str(project),
                         "focused": True},
                    ],
                }}
            }), "")
        if "prompt" in args:
            prompts.append(args)
            return module.subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(module.subprocess, "run", run)

    module._notify_pane("kimi", "main", 902, "pane 粒度", str(project))
    assert len(prompts) == 1

    # 旧 float 格式(升级前 session 级记录)→ 保守避让
    prompts.clear()
    typing_file.write_text(json.dumps({"demo": time.time()}), encoding="utf-8")
    module._notify_pane("kimi", "main", 903, "旧格式", str(project))
    assert prompts == []

    # unknown 落点(输入时解析不到 pane)→ 保守避让
    typing_file.write_text(
        json.dumps({"demo": {"unknown": time.time()}}), encoding="utf-8"
    )
    module._notify_pane("kimi", "main", 904, "未知落点", str(project))
    assert prompts == []


# ============ 分级选路与显式通知目标(#1162 修订版 A/B) ============

def test_no_global_fallback_for_unrelated_unique_candidate():
    """project_key 存在时,禁止跨项目'全局唯一同类型'兜底。"""
    module = _load_mail_send()
    assert module._select_notify_targets([
        ("other", "w1:p9", "/somewhere/else"),
    ], PROJECT) == []


def test_cwd_subdir_unique_hit(tmp_path):
    """cwd 位于项目根子目录 → 三级命中(非 git 仅路径包含)。"""
    module = _load_mail_send()
    project = tmp_path / "proj"
    sub = project / "src"
    sub.mkdir(parents=True)
    monkey_git = lambda path: None  # noqa: E731
    module._git_common_dir = monkey_git
    result = module._select_notify_targets(
        [("demo", "w1:p1", str(sub))], str(project))
    assert result == [("demo", "w1:p1", str(sub), False)]


def test_same_tier_ambiguity_skipped(tmp_path):
    """同级多候选 → 跳过,不任选。"""
    module = _load_mail_send()
    project = tmp_path / "proj"
    sub = project / "src"
    sub.mkdir(parents=True)
    module._git_common_dir = lambda path: None
    assert module._select_notify_targets([
        ("demo", "w1:p1", str(sub)),
        ("demo", "w1:p2", str(project)),
    ], str(project)) == []


def test_worktree_common_dir_unique_hit(tmp_path):
    """cwd 与项目根同 git common-dir → 四级命中。"""
    module = _load_mail_send()
    project = tmp_path / "repo"
    worktree = tmp_path / "wt"
    project.mkdir()
    worktree.mkdir()
    common = str(project / ".git")
    module._git_common_dir = (
        lambda path: common if path in (str(project), str(worktree)) else None
    )
    result = module._select_notify_targets(
        [("demo", "w1:p1", str(worktree))], str(project))
    assert result == [("demo", "w1:p1", str(worktree), False)]


def test_git_common_dir_real_worktree(tmp_path):
    """真实 git common-dir 解析:worktree 归一到主仓库。"""
    module = _load_mail_send()
    repo = tmp_path / "repo"
    repo.mkdir()
    module.subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)], check=True)
    module.subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    wt = tmp_path / "wt"
    module.subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(wt)], check=True)
    assert module._git_common_dir(str(repo)) == module._git_common_dir(str(wt))
    result = module._select_notify_targets([("s", "w1:p1", str(wt))], str(repo))
    assert result and result[0][:2] == ("s", "w1:p1")


def test_coordination_identity_binding_takes_priority(tmp_path):
    """一级:coordination mail_name→pane 强绑定优先于 cwd 命中。"""
    module = _load_mail_send()
    project = tmp_path / "proj"
    sub = project / "sub"
    sub.mkdir(parents=True)
    module._git_common_dir = lambda path: None
    module._identity_bound_panes = (
        lambda project_key, name: {"bound-sess": {"w1:pB"}}
        if name == "KimiFoo" else {}
    )
    result = module._select_notify_targets([
        ("bound-sess", "w1:pB", "/unrelated/dir"),
        ("other", "w1:pC", str(sub)),
    ], str(project), mail_name="KimiFoo")
    assert result == [("bound-sess", "w1:pB", "/unrelated/dir", False)]


def test_panes_by_mail_name_reads_active_runs(tmp_path):
    from agent_cockpit import coordination
    coordination.start_run(
        project_key=str(tmp_path), session="demo",
        session_dir=str(tmp_path),
        participants=[{"id": "k1", "agent": "kimi", "role": "developer",
                       "task": "", "workdir": str(tmp_path)}],
    )
    run = coordination.run_by_session("demo")
    assert coordination.bind_identity(
        str(run["run_id"]), "k1", "KimiFoo", "w1:pB")
    assert coordination.panes_by_mail_name(str(tmp_path), "KimiFoo") == {
        "demo": {"w1:pB"}}
    assert coordination.panes_by_mail_name(str(tmp_path), "Nobody") == {}


def test_panes_by_mail_name_isolated_by_project(tmp_path):
    """花名是项目内身份：两个项目同花名时只返回目标项目的绑定 pane。"""
    from agent_cockpit import coordination
    project_a = tmp_path / "proj-a"
    project_b = tmp_path / "proj-b"
    project_a.mkdir()
    project_b.mkdir()
    coordination.start_run(
        project_key=str(project_a), session="sess-a",
        session_dir=str(project_a),
        participants=[{"id": "k1", "agent": "kimi", "role": "developer",
                       "task": "", "workdir": str(project_a)}],
    )
    coordination.start_run(
        project_key=str(project_b), session="sess-b",
        session_dir=str(project_b),
        participants=[{"id": "k2", "agent": "kimi", "role": "developer",
                       "task": "", "workdir": str(project_b)}],
    )
    coordination.bind_identity(
        str(coordination.run_by_session("sess-a")["run_id"]),
        "k1", "KimiFoo", "w1:pA")
    coordination.bind_identity(
        str(coordination.run_by_session("sess-b")["run_id"]),
        "k2", "KimiFoo", "w1:pB")
    assert coordination.panes_by_mail_name(str(project_a), "KimiFoo") == {
        "sess-a": {"w1:pA"}}
    assert coordination.panes_by_mail_name(str(project_b), "KimiFoo") == {
        "sess-b": {"w1:pB"}}


def test_first_nonempty_tier_ambiguity_does_not_downgrade(tmp_path):
    """tier1 两个强绑定候选为歧义：即使 tier2/3 存在唯一候选也不得降级投递。"""
    module = _load_mail_send()
    project = tmp_path / "proj"
    sub = project / "sub"
    sub.mkdir(parents=True)
    module._git_common_dir = lambda path: None
    module._identity_bound_panes = (
        lambda project_key, name: {"s1": {"w1:p1", "w1:p2"}}
        if name == "KimiFoo" else {}
    )
    result = module._select_notify_targets([
        ("s1", "w1:p1", "/elsewhere"),
        ("s1", "w1:p2", "/elsewhere"),
        ("s2", "w1:p3", str(sub)),
    ], str(project), mail_name="KimiFoo")
    assert result == []


def test_resolve_explicit_target_success(monkeypatch):
    module = _load_mail_send()
    monkeypatch.setattr(module, "_session_rows",
                        lambda env: [{"name": "demo", "running": True}])
    monkeypatch.setattr(module, "_session_panes", lambda s, env: [
        {"pane_id": "w1:p2", "agent": "kimi", "cwd": "/x",
         "agent_status": "working"}])
    assert module.resolve_explicit_target("demo", "w1:p2", "kimi") == (
        "demo", "w1:p2", "/x", "working")


def test_resolve_explicit_target_session_missing(monkeypatch):
    module = _load_mail_send()
    monkeypatch.setattr(module, "_session_rows", lambda env: [])
    with pytest.raises(ValueError, match="session"):
        module.resolve_explicit_target("ghost", "w1:p1", "kimi")


def test_resolve_explicit_target_pane_missing(monkeypatch):
    module = _load_mail_send()
    monkeypatch.setattr(module, "_session_rows",
                        lambda env: [{"name": "demo", "running": True}])
    monkeypatch.setattr(module, "_session_panes", lambda s, env: [])
    with pytest.raises(ValueError, match="pane"):
        module.resolve_explicit_target("demo", "w1:p9", "kimi")


def test_resolve_explicit_target_type_incompatible(monkeypatch):
    module = _load_mail_send()
    monkeypatch.setattr(module, "_session_rows",
                        lambda env: [{"name": "demo", "running": True}])
    monkeypatch.setattr(module, "_session_panes", lambda s, env: [
        {"pane_id": "w1:p2", "agent": "codex", "cwd": "/x"}])
    with pytest.raises(ValueError, match="不兼容"):
        module.resolve_explicit_target("demo", "w1:p2", "kimi")


def test_explicit_target_notifies_without_auto_routing(monkeypatch, tmp_path):
    """显式目标直达 pane,失败不回退自动选路(此处验证不触发自动选路)。"""
    module = _load_mail_send()
    herdr = tmp_path / "herdr"
    herdr.touch()
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(module, "HERDR_BIN", str(herdr))
    monkeypatch.setattr(module, "TYPING_STATE_PATH", str(tmp_path / "none.json"))

    def no_auto(*args, **kwargs):
        raise AssertionError("显式目标不应触发自动选路")

    monkeypatch.setattr(module, "_select_notify_targets", no_auto)
    prompts = []

    def run(args, **kwargs):
        if "prompt" in args:
            prompts.append(args)
            return module.subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(module.subprocess, "run", run)
    module._notify_pane(
        "kimi", "main", 1, "显式", str(project),
        explicit=("demo", "w1:pX", "/x"),
    )
    assert len(prompts) == 1
    assert prompts[0][5] == "w1:pX"


def test_explicit_target_uses_cached_working_status_without_prompt(
    monkeypatch, tmp_path,
):
    module = _load_mail_send()
    herdr = tmp_path / "herdr"
    herdr.touch()
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(module, "HERDR_BIN", str(herdr))
    monkeypatch.setattr(module, "TYPING_STATE_PATH", str(tmp_path / "none.json"))

    def no_prompt(*args, **kwargs):
        raise AssertionError("working Agent 不应收到 Herdr prompt")

    monkeypatch.setattr(module.subprocess, "run", no_prompt)
    module._notify_pane(
        "kimi", "main", 1, "工作中显式目标", str(project),
        explicit=("demo", "w1:p1", "/x", "working"),
    )


def test_explicit_target_never_re_resolves(monkeypatch, tmp_path):
    """显式目标只在发送前解析一次；通知阶段不得再次 resolve。"""
    module = _load_mail_send()
    herdr = tmp_path / "herdr"
    herdr.touch()
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(module, "HERDR_BIN", str(herdr))
    monkeypatch.setattr(module, "TYPING_STATE_PATH", str(tmp_path / "none.json"))

    def no_resolve(*args, **kwargs):
        raise AssertionError("显式目标不得在通知阶段二次 resolve")

    monkeypatch.setattr(module, "resolve_explicit_target", no_resolve)
    prompts = []

    def run(args, **kwargs):
        if "prompt" in args:
            prompts.append(args)
            return module.subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(module.subprocess, "run", run)
    module._notify_pane(
        "kimi", "main", 1, "只解析一次", str(project),
        explicit=("demo", "w1:p1", str(project)),
    )
    assert len(prompts) == 1


def test_explicit_target_pane_gone_does_not_fail_sent_message(monkeypatch, tmp_path):
    """pane 在实际 prompt 时消失：仅警告'消息已发送'，不得抛异常中断。"""
    module = _load_mail_send()
    herdr = tmp_path / "herdr"
    herdr.touch()
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(module, "HERDR_BIN", str(herdr))
    monkeypatch.setattr(module, "TYPING_STATE_PATH", str(tmp_path / "none.json"))

    def run(args, **kwargs):
        return module.subprocess.CompletedProcess(args, 1, "", "pane gone")

    monkeypatch.setattr(module.subprocess, "run", run)
    # 不得抛异常（持久发送已成功，通知失败只警告）
    module._notify_pane(
        "kimi", "main", 1, "pane消失", str(project),
        explicit=("demo", "w1:p1", "/x"),
    )


def test_explicit_target_exact_cwd_uses_exact_notification(monkeypatch, tmp_path):
    """显式目标 cwd 与项目相同时，不应误报“cwd 不同”。"""
    module = _load_mail_send()
    monkeypatch.setattr(module, "_recipient_typing", lambda *_args: False)
    exact_flags = []
    monkeypatch.setattr(
        module, "_notify_text",
        lambda _msg_id, _subject, _agent, _instance, _project, _cwd,
        is_exact, _intent: exact_flags.append(is_exact) or "note",
    )
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda *args, **kwargs: module.subprocess.CompletedProcess(
            args[0], 0, "", ""
        ),
    )

    module._deliver_notify_note(
        {}, "demo", "w1:p1", str(tmp_path), "explicit",
        1, "subject", "kimi", "main", str(tmp_path), "info",
    )

    assert exact_flags == [True]


def test_explicit_target_conflicts_with_no_notify(monkeypatch, tmp_path):
    module = _load_mail_send()
    monkeypatch.setattr(sys, "argv", [
        "mail-send", "--agent", "kimi", "--project", str(tmp_path),
        "--to", "KimiFoo", "--subject", "s", "--body", "b",
        "--no-notify", "--target", "demo/w1:p1",
    ])
    with pytest.raises(SystemExit, match="冲突"):
        module.main()


def test_explicit_target_rejects_human_only_recipient(monkeypatch, tmp_path):
    module = _load_mail_send()
    monkeypatch.setattr(sys, "argv", [
        "mail-send", "--agent", "kimi", "--project", str(tmp_path),
        "--to", "@fyc-mac", "--subject", "s", "--body", "b",
        "--target", "demo/w1:p1",
    ])
    monkeypatch.setattr(
        module, "load_identity",
        lambda *_args: (
            {"project_key": str(tmp_path), "name": "me",
             "registration_token": "t"},
            "http://hub", "tok",
        ),
    )
    with pytest.raises(SystemExit, match="至少需包含一个 Agent"):
        module.main()


def test_explicit_target_identity_unresolvable_fails_before_send(monkeypatch, tmp_path):
    """指定显式目标而收件人本地身份无法解析：发送前报错，不得静默忽略。"""
    module = _load_mail_send()
    monkeypatch.setattr(sys, "argv", [
        "mail-send", "--agent", "kimi", "--project", str(tmp_path),
        "--to", "KimiFoo", "--subject", "s", "--body", "b",
        "--session", "demo", "--pane", "w1:p1",
    ])
    monkeypatch.setattr(
        module, "load_identity",
        lambda agent, instance, project: (
            {"project_key": str(tmp_path), "name": "me",
             "registration_token": "t"},
            "http://hub", "tok"))
    monkeypatch.setattr(
        module, "_notification_identity", lambda name, project_key: None)

    def forbid(*args, **kwargs):
        raise AssertionError("身份不可解析时不得发送消息")

    monkeypatch.setattr(module, "mcp_call", forbid)
    monkeypatch.setattr(module, "mcp_tool", forbid)
    with pytest.raises(SystemExit, match="无法解析"):
        module.main()


# ============================================================================
# am-register --rotate / --recover
# ============================================================================


def _register_fixture(tmp_path, module, agent="demo", instance="main", old_token="o" * 43):
    module.REGISTRY_DIR = tmp_path
    project = tmp_path / "proj"
    project.mkdir()
    registry_dir = tmp_path / module.slugify(str(project))
    registry_dir.mkdir()
    registry_file = registry_dir / f"{agent}--{instance}.json"
    registry_file.write_text(json.dumps({
        "project_key": str(project),
        "project_slug": "proj",
        "agent": agent,
        "instance": instance,
        "name": "demo-main",
        "registration_token": old_token,
        "program": "qoderclicn",
        "model": "unknown",
        "hub": "http://127.0.0.1:8765",
    }))
    registry_file.chmod(0o600)
    registry_file.chmod(0o600)
    return project, registry_file


def _argv(module, *extra):
    return ["am-register", "--agent", "demo", "--instance", "main", "--project", str(module._project)] + list(extra)


def test_rotate_flag_mutually_exclusive_with_force_and_show(tmp_path, monkeypatch):
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_tool", lambda *_a, **_k: {})

    monkeypatch.setattr(module.sys, "argv", _argv(module, "--rotate", "--force"))
    with pytest.raises(SystemExit, match="不能与"):
        module.main()
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--rotate", "--show"))
    with pytest.raises(SystemExit, match="不能与"):
        module.main()
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--recover", "--force"))
    with pytest.raises(SystemExit, match="不能与"):
        module.main()


def test_rotate_requires_existing_registry(tmp_path, monkeypatch):
    module = _load_am_register()
    module.REGISTRY_DIR = tmp_path
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module.sys, "argv", [
        "am-register", "--agent", "demo", "--project", str(project), "--rotate",
    ])
    with pytest.raises(SystemExit, match="尚未注册"):
        module.main()


def test_rotate_writes_pending_0600_before_hub_call(tmp_path, monkeypatch, capsys):
    """阶段 1：先落 0600 pending，stdout/stderr 无任何值，Hub 收到新值。"""
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_a, **_k: {})
    captured = {}

    def tool(_hub, _token, name, args):
        if name == "rotate_agent_capability":
            captured["args"] = args
            pending = module._pending_path(registry_file)
            assert pending.is_file(), "Hub 调用时 pending 必须已落盘"
            assert (pending.stat().st_mode & 0o777) == 0o600
            return {"status": "rotated"}
        raise AssertionError(name)

    monkeypatch.setattr(module, "mcp_tool", tool)
    fsync_calls = []
    monkeypatch.setattr(module, "_fsync_dir", lambda p: fsync_calls.append(str(p)))
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--rotate"))

    module.main()

    old_token = "o" * 43
    new_token = captured["args"]["new_registration_token"]
    assert new_token != old_token
    assert captured["args"]["old_registration_token"] == old_token
    assert captured["args"]["agent_name"] == "demo-main"
    # promote 后 pending 消失，registry 为新值
    assert not module._pending_path(registry_file).exists()
    final = json.loads(registry_file.read_text())
    assert final["registration_token"] == new_token
    assert (registry_file.stat().st_mode & 0o777) == 0o600
    # 目录 fsync：pending 写 + registry 写 + pending unlink 各一次
    assert len(fsync_calls) >= 3
    _captured = capsys.readouterr()
    combined = _captured.out + _captured.err
    assert old_token not in combined and new_token not in combined


def test_rotate_hub_failure_rolls_back_when_old_still_valid(tmp_path, monkeypatch, capsys):
    """Hub 拒绝且明确旧值仍有效：回滚删 pending，registry 保持旧值。"""
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    old_token = "o" * 43
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_a, **_k: {})

    def tool(_hub, _token, name, args):
        if name == "rotate_agent_capability":
            raise SystemExit("Hub error with secret value")
        if name == "whois":
            if args["registration_token"] == old_token:
                return {"name": "demo-main"}
            raise SystemExit("invalid")
        raise AssertionError(name)

    monkeypatch.setattr(module, "mcp_tool", tool)
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--rotate"))

    module.main()

    assert json.loads(registry_file.read_text())["registration_token"] == old_token
    assert not module._pending_path(registry_file).exists()
    _captured = capsys.readouterr()
    combined = _captured.out + _captured.err
    assert old_token not in combined
    assert "Hub error" not in combined and "secret value" not in combined
    assert "已回滚" in combined


def test_rotate_hub_failure_indeterminate_keeps_pending(tmp_path, monkeypatch, capsys):
    """Hub 响应不确定（超时/解析失败且探测均无效）：保留 pending 提示 --recover。"""
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    old_token = "o" * 43
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_a, **_k: {})

    def tool(_hub, _token, name, _args):
        raise SystemExit("timeout")

    monkeypatch.setattr(module, "mcp_tool", tool)
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--rotate"))

    with pytest.raises(SystemExit, match="--recover"):
        module.main()

    assert json.loads(registry_file.read_text())["registration_token"] == old_token
    assert module._pending_path(registry_file).is_file()
    _captured = capsys.readouterr()
    combined = _captured.out + _captured.err
    assert old_token not in combined
    assert "timeout" not in combined


def test_rotate_hub_failure_promotes_when_hub_committed(tmp_path, monkeypatch, capsys):
    """Hub 已提交但响应丢失：探测新值有效 → promote，本地不失联。"""
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    old_token = "o" * 43
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_a, **_k: {})
    captured = {}

    def tool(_hub, _token, name, args):
        if name == "rotate_agent_capability":
            captured["new"] = args["new_registration_token"]
            raise SystemExit("response lost")
        if name == "whois":
            if args["registration_token"] == captured["new"]:
                return {"name": "demo-main"}
            raise SystemExit("invalid")
        raise AssertionError(name)

    monkeypatch.setattr(module, "mcp_tool", tool)
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--rotate"))

    module.main()

    new_token = captured["new"]
    assert json.loads(registry_file.read_text())["registration_token"] == new_token
    assert not module._pending_path(registry_file).exists()
    _captured = capsys.readouterr()
    combined = _captured.out + _captured.err
    assert old_token not in combined and new_token not in combined
    assert "已收敛为新值" in combined


def test_rotate_rejects_non_rotated_response_keeps_pending(tmp_path, monkeypatch, capsys):
    """rotate 返回 {} 或非 status=rotated：视为不确定，保 pending 提示 --recover。"""
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    old_token = "o" * 43
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_a, **_k: {})

    def tool(_hub, _token, name, _args):
        if name == "rotate_agent_capability":
            return {}
        raise SystemExit("unreachable")

    monkeypatch.setattr(module, "mcp_tool", tool)
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--rotate"))

    with pytest.raises(SystemExit, match="--recover"):
        module.main()

    assert json.loads(registry_file.read_text())["registration_token"] == old_token
    assert module._pending_path(registry_file).is_file()
    _captured = capsys.readouterr()
    combined = _captured.out + _captured.err
    assert old_token not in combined


def test_rotate_recover_argparse_mutually_exclusive(tmp_path, monkeypatch):
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_tool", lambda *_a, **_k: {})
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--rotate", "--recover"))
    with pytest.raises(SystemExit):
        module.main()


def test_rotate_lock_contention_rejected(tmp_path, monkeypatch):
    """同 registry 已有进程持锁：拒绝并发操作，防止互删/覆盖 pending。"""
    import fcntl

    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    lock_file = module._lock_path(registry_file)
    fh = open(lock_file, "w")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    try:
        monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
        monkeypatch.setattr(module, "mcp_tool", lambda *_a, **_k: {})
        monkeypatch.setattr(module.sys, "argv", _argv(module, "--rotate"))
        with pytest.raises(SystemExit, match="并发"):
            module.main()
    finally:
        fh.close()


def test_rotate_rename_failure_keeps_pending_then_recover_promotes(tmp_path, monkeypatch, capsys):
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    old_token = "o" * 43
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_a, **_k: {})
    captured = {}

    def tool(_hub, _token, name, args):
        if name == "rotate_agent_capability":
            captured["new"] = args["new_registration_token"]
            return {"status": "rotated"}
        raise AssertionError(name)

    monkeypatch.setattr(module, "mcp_tool", tool)
    real_atomic = module._atomic_write_identity

    def failing_atomic(path, identity):
        if path == registry_file:
            raise OSError("rename failed")
        return real_atomic(path, identity)

    monkeypatch.setattr(module, "_atomic_write_identity", failing_atomic)
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--rotate"))

    with pytest.raises(SystemExit, match="--recover"):
        module.main()

    new_token = captured["new"]
    # registry 仍旧值，pending 保留新值
    assert json.loads(registry_file.read_text())["registration_token"] == old_token
    pending = module._pending_path(registry_file)
    assert pending.is_file()
    assert json.loads(pending.read_text())["registration_token"] == new_token

    # --recover：old 无效、new 有效 → promote
    def probe_tool(_hub, _token, name, args):
        if name == "whois":
            if args["registration_token"] == new_token:
                return {"name": "demo-main"}
            raise SystemExit("invalid")
        raise AssertionError(name)

    monkeypatch.setattr(module, "_atomic_write_identity", real_atomic)
    monkeypatch.setattr(module, "mcp_tool", probe_tool)
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--recover"))

    module.main()

    assert json.loads(registry_file.read_text())["registration_token"] == new_token
    assert not pending.exists()
    _captured = capsys.readouterr()
    combined = _captured.out + _captured.err
    assert old_token not in combined and new_token not in combined


def test_recover_rollback_when_old_still_valid(tmp_path, monkeypatch, capsys):
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    old_token = "o" * 43
    new_token = "n" * 43
    pending_identity = json.loads(registry_file.read_text())
    pending_identity["registration_token"] = new_token
    module._atomic_write_identity(module._pending_path(registry_file), pending_identity)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))

    def tool(_hub, _token, name, args):
        if name == "whois":
            if args["registration_token"] == old_token:
                return {"name": "demo-main"}
            raise SystemExit("invalid")
        raise AssertionError(name)

    monkeypatch.setattr(module, "mcp_tool", tool)
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--recover"))

    module.main()

    assert json.loads(registry_file.read_text())["registration_token"] == old_token
    assert not module._pending_path(registry_file).exists()
    _captured = capsys.readouterr()
    combined = _captured.out + _captured.err
    assert old_token not in combined and new_token not in combined
    assert "rollback" in combined


def test_recover_indeterminate_keeps_both(tmp_path, monkeypatch, capsys):
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    old_token = "o" * 43
    new_token = "n" * 43
    pending_identity = json.loads(registry_file.read_text())
    pending_identity["registration_token"] = new_token
    module._atomic_write_identity(module._pending_path(registry_file), pending_identity)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_tool", lambda *_a, **_k: (_ for _ in ()).throw(SystemExit("unreachable")))
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--recover"))

    with pytest.raises(SystemExit, match="均无效"):
        module.main()

    assert module._pending_path(registry_file).is_file()
    assert json.loads(registry_file.read_text())["registration_token"] == old_token
    _captured = capsys.readouterr()
    combined = _captured.out + _captured.err
    assert old_token not in combined and new_token not in combined


def test_recover_rejects_mismatched_pending(tmp_path, monkeypatch, capsys):
    """pending 不可变字段与正式 registry 不一致：拒绝恢复外来/陈旧 pending。"""
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    old_token = "o" * 43
    new_token = "n" * 43
    pending_identity = json.loads(registry_file.read_text())
    pending_identity["registration_token"] = new_token
    pending_identity["name"] = "intruder-main"
    module._atomic_write_identity(module._pending_path(registry_file), pending_identity)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_tool", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不应调用 hub")))
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--recover"))

    with pytest.raises(SystemExit, match="身份不一致"):
        module.main()

    assert module._pending_path(registry_file).is_file()
    assert json.loads(registry_file.read_text())["registration_token"] == old_token
    _captured = capsys.readouterr()
    combined = _captured.out + _captured.err
    assert old_token not in combined and new_token not in combined


def test_recover_without_pending_is_noop(tmp_path, monkeypatch, capsys):
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_tool", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不应调用 hub")))
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--recover"))

    module.main()

    assert "无待恢复" in capsys.readouterr().out
    assert json.loads(registry_file.read_text())["registration_token"] == "o" * 43


def test_secure_read_rejects_symlink_registry(tmp_path, monkeypatch):
    """registry 为 symlink：拒绝读取。"""
    import os as _os

    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    real = registry_file.with_name("real.json")
    real.write_text(registry_file.read_text())
    real.chmod(0o600)
    registry_file.unlink()
    _os.symlink(real.name, registry_file)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_tool", lambda *_a, **_k: {})
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--recover"))
    with pytest.raises(SystemExit, match="symlink"):
        module.main()


def test_secure_read_rejects_symlink_pending(tmp_path, monkeypatch, capsys):
    """pending 为 symlink：拒绝恢复。"""
    import os as _os

    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    real = module._pending_path(registry_file).with_name("real.pending")
    real.write_text(registry_file.read_text())
    real.chmod(0o600)
    _os.symlink(real.name, module._pending_path(registry_file))
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_tool", lambda *_a, **_k: {})
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--recover"))
    with pytest.raises(SystemExit, match="symlink"):
        module.main()
    combined = capsys.readouterr().out + capsys.readouterr().err
    assert "o" * 43 not in combined


def test_secure_read_rejects_0644_registry(tmp_path, monkeypatch):
    """registry 权限过宽（0644）：拒绝。"""
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    registry_file.chmod(0o644)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_tool", lambda *_a, **_k: {})
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--recover"))
    with pytest.raises(SystemExit, match="权限过宽"):
        module.main()


def test_secure_read_rejects_0644_pending(tmp_path, monkeypatch, capsys):
    """pending 权限过宽（0644）：拒绝恢复。"""
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    pending_identity = json.loads(registry_file.read_text())
    pending_identity["registration_token"] = "n" * 43
    module._atomic_write_identity(module._pending_path(registry_file), pending_identity)
    module._pending_path(registry_file).chmod(0o644)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_tool", lambda *_a, **_k: {})
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--recover"))
    with pytest.raises(SystemExit, match="权限过宽"):
        module.main()
    combined = capsys.readouterr().out + capsys.readouterr().err
    assert ("o" * 43) not in combined and ("n" * 43) not in combined


def test_secure_read_rejects_foreign_owner_registry(tmp_path, monkeypatch):
    """registry 属主非当前用户：拒绝。"""
    import os as _os

    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    monkeypatch.setattr(_os, "getuid", lambda: 99999)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_tool", lambda *_a, **_k: {})
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--recover"))
    with pytest.raises(SystemExit, match="属主"):
        module.main()


def test_rotate_pending_write_fsync_failure_fails_closed(tmp_path, monkeypatch, capsys):
    """pending 写入目录 fsync 失败：fail-closed，不触碰 Hub、不宣告成功。"""
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    old_token = "o" * 43
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_a, **_k: {})
    monkeypatch.setattr(module, "mcp_tool", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不应调用 Hub")))

    def boom(_p):
        raise OSError("dir fsync failed")

    monkeypatch.setattr(module, "_fsync_dir", boom)
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--rotate"))

    with pytest.raises(SystemExit, match="pending 写入失败"):
        module.main()

    assert json.loads(registry_file.read_text())["registration_token"] == old_token
    # replace 先于目录 fsync：pending 可能已落但持久性未知；关键是未触碰 Hub 且未宣告成功
    combined = capsys.readouterr().out + capsys.readouterr().err
    assert old_token not in combined
    assert "轮换成功" not in combined


def test_rotate_unlink_fsync_failure_fails_closed(tmp_path, monkeypatch, capsys):
    """成功路径 unlink 目录 fsync 失败：不得宣告成功，提示 --recover。"""
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    old_token = "o" * 43
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))
    monkeypatch.setattr(module, "mcp_call", lambda *_a, **_k: {})
    captured = {}

    def tool(_hub, _token, name, args):
        if name == "rotate_agent_capability":
            captured["new"] = args["new_registration_token"]
            return {"status": "rotated"}
        raise AssertionError(name)

    monkeypatch.setattr(module, "mcp_tool", tool)
    real_fsync = module._fsync_dir

    def flaky(p):
        flaky.n += 1
        if flaky.n >= 3:
            raise OSError("dir fsync failed")
        return real_fsync(p)

    flaky.n = 0
    monkeypatch.setattr(module, "_fsync_dir", flaky)
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--rotate"))

    with pytest.raises(SystemExit, match="--recover"):
        module.main()

    new_token = captured["new"]
    assert json.loads(registry_file.read_text())["registration_token"] == new_token
    combined = capsys.readouterr().out + capsys.readouterr().err
    assert old_token not in combined and new_token not in combined
    assert "轮换成功" not in combined


def test_recover_promote_unlink_fsync_failure_fails_closed(tmp_path, monkeypatch, capsys):
    """recover promote 的 unlink 目录 fsync 失败：fail-closed，registry 已更新。"""
    module = _load_am_register()
    project, registry_file = _register_fixture(tmp_path, module)
    module._project = project
    old_token = "o" * 43
    new_token = "n" * 43
    pending_identity = json.loads(registry_file.read_text())
    pending_identity["registration_token"] = new_token
    module._atomic_write_identity(module._pending_path(registry_file), pending_identity)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "token"))

    def tool(_hub, _token, name, args):
        if name == "whois":
            if args["registration_token"] == new_token:
                return {"name": "demo-main"}
            raise SystemExit("invalid")
        raise AssertionError(name)

    monkeypatch.setattr(module, "mcp_tool", tool)
    real_fsync = module._fsync_dir

    def flaky(p):
        flaky.n += 1
        if flaky.n >= 2:
            raise OSError("dir fsync failed")
        return real_fsync(p)

    flaky.n = 0
    monkeypatch.setattr(module, "_fsync_dir", flaky)
    monkeypatch.setattr(module.sys, "argv", _argv(module, "--recover"))

    with pytest.raises(SystemExit, match="收敛失败"):
        module.main()

    assert json.loads(registry_file.read_text())["registration_token"] == new_token
    combined = capsys.readouterr().out + capsys.readouterr().err
    assert old_token not in combined and new_token not in combined
