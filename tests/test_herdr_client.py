import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import call

from agent_cockpit import herdr_client
import pytest


REQUIRED_H0_METHODS = {
    "session.snapshot",
    "agent.list",
    "agent.get",
    "agent.start",
    "agent.read",
    "agent.prompt",
    "agent.wait",
    "agent.send_keys",
    "pane.process_info",
    "events.subscribe",
}


@pytest.fixture(autouse=True)
def _assume_supported_herdr(monkeypatch):
    """现有单元测试聚焦命令语义；能力门由本文件的专门用例覆盖。"""
    monkeypatch.setattr(
        herdr_client,
        "require_herdr_capabilities",
        lambda: {
            "version": "0.8.0",
            "protocol": 19,
            "schema_version": 1,
            "methods": sorted(REQUIRED_H0_METHODS),
        },
        raising=False,
    )


@pytest.fixture(autouse=True)
def _isolated_launch_descriptors(monkeypatch, tmp_path):
    """launch descriptor 落盘到临时路径，避免污染真实 ~/dashboard-data。"""
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "launch-descriptors.json"),
    )


def test_dev_profile_herdr_cli_uses_user_herdr_not_isolated_tree(monkeypatch):
    monkeypatch.setenv("COCKPIT_NEXT_PROFILE", "dev")
    monkeypatch.setenv("HERDR_CONFIG_PATH", "/tmp/isolated/herdr/config.toml")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/isolated")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/isolated-state")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/isolated-data")
    monkeypatch.setenv("HERDR_SESSION", "github-agent-cockpit-next")
    env = herdr_client._herdr_subprocess_env()
    assert "HERDR_CONFIG_PATH" not in env
    assert "HERDR_SESSION" not in env
    assert env["XDG_CONFIG_HOME"] == str(Path.home() / ".config")


def test_herdr_tui_env_drops_inherited_pane_context(monkeypatch):
    monkeypatch.setattr(
        herdr_client,
        "_herdr_subprocess_env",
        lambda: {
            "PATH": "/usr/bin",
            "HERDR_CONFIG_PATH": "/profile/herdr.toml",
            "HERDR_ENV": "1",
            "HERDR_SESSION": "caller",
            "HERDR_WORKSPACE_ID": "w1",
            "HERDR_TAB_ID": "w1:t1",
            "HERDR_PANE_ID": "w1:p1",
            "HERDR_TERMINAL_ID": "term-1",
        },
    )

    env = herdr_client._herdr_tui_env()

    assert env == {
        "PATH": "/usr/bin",
        "HERDR_CONFIG_PATH": "/profile/herdr.toml",
    }


def _herdr_schema(*, protocol=19, schema_version=1, methods=None):
    method_names = REQUIRED_H0_METHODS if methods is None else set(methods)
    return json.dumps({
        "protocol": protocol,
        "schema_version": schema_version,
        "schemas": {
            "request": {
                "oneOf": [
                    {"properties": {"method": {"const": method}}}
                    for method in sorted(method_names)
                ]
            }
        },
    })


def test_probe_herdr_capabilities_accepts_supported_installed_schema(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if args == ["--version"]:
            return "herdr 0.8.0\n"
        if args == ["api", "schema", "--json"]:
            return _herdr_schema()
        raise AssertionError(args)

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.probe_herdr_capabilities()

    assert result == {
        "version": "0.8.0",
        "protocol": 19,
        "schema_version": 1,
        "methods": sorted(REQUIRED_H0_METHODS),
    }
    assert calls == [
        call(["--version"], timeout=5),
        call(["api", "schema", "--json"], timeout=5),
    ]


@pytest.mark.parametrize(
    ("version", "schema", "match"),
    [
        ("herdr 0.7.4", _herdr_schema(), "0.8.0"),
        ("herdr 0.8.0", _herdr_schema(protocol=18), "protocol 19"),
        (
            "herdr 0.8.0",
            _herdr_schema(methods=REQUIRED_H0_METHODS - {"events.subscribe"}),
            "events.subscribe",
        ),
        (
            "herdr 0.8.0",
            _herdr_schema(methods=REQUIRED_H0_METHODS - {"agent.wait"}),
            "agent.wait",
        ),
        (
            "herdr 0.8.0",
            _herdr_schema(methods=REQUIRED_H0_METHODS - {"pane.process_info"}),
            "pane.process_info",
        ),
        ("herdr 0.8.0", "not-json", "API schema"),
    ],
)
def test_probe_herdr_capabilities_requires_upgrade_instead_of_fallback(
    monkeypatch, version, schema, match,
):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda args, timeout=10: version if args == ["--version"] else schema,
    )

    with pytest.raises(herdr_client.HerdrCapabilityError, match=match):
        herdr_client.probe_herdr_capabilities()


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("codex", "codex"),
        ("claude", "claude"),
        ("kimi", "kimi"),
        ("opencode", "opencode"),
        ("grok", "grok"),
        ("qoder", "qodercli"),
        ("qodercli", "qodercli"),
        ("qodercn", "qodercli"),
        ("qoderclicn", "qodercli"),
    ],
)
def test_normalize_agent_kind_uses_herdr_kind_aliases(name, kind):
    assert herdr_client.normalize_agent_kind(name) == kind


def test_normalize_agent_kind_rejects_unknown_kind():
    with pytest.raises(ValueError, match="不支持的 agent"):
        herdr_client.normalize_agent_kind("unknown")


@pytest.mark.parametrize("name", ["codex-1", "lead", "reviewer_2", "a" * 32])
def test_validate_agent_name_accepts_herdr_native_names(name):
    assert herdr_client.validate_agent_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "1-codex", "Codex-1", "bad name", "a" * 33, "agent/one"],
)
def test_validate_agent_name_rejects_names_herdr_cannot_own(name):
    with pytest.raises(ValueError, match="实例名称"):
        herdr_client.validate_agent_name(name)


def test_display_name_is_user_text_and_may_repeat():
    assert herdr_client.validate_display_name(" codex terra ") == "codex terra"
    assert herdr_client.validate_display_name("夜班负责人") == "夜班负责人"
    with pytest.raises(ValueError, match="不能为空"):
        herdr_client.validate_display_name("   ")
    with pytest.raises(ValueError, match="控制字符"):
        herdr_client.validate_display_name("bad\nname")
    with pytest.raises(ValueError, match="最长 64"):
        herdr_client.validate_display_name("x" * 65)


def test_new_agent_instance_id_is_opaque_unique_and_herdr_safe():
    first = herdr_client.new_agent_instance_id()
    second = herdr_client.new_agent_instance_id()

    assert first != second
    assert len(first) == 28 and first.startswith("i-")
    assert set(first[2:]) <= set("abcdefghijklmnopqrstuvwxyz234567")
    assert herdr_client.validate_agent_name(first) == first


def test_resolve_unique_agent_name_checks_live_names_without_reusing_labels():
    agents = [{"name": "codex-1"}, {"name": "reviewer"}]

    assert herdr_client.resolve_unique_agent_name("codex", None, agents) == "codex-2"
    assert herdr_client.resolve_unique_agent_name("qoder", "qoder-main", agents) == "qoder-main"
    with pytest.raises(ValueError, match="已被占用"):
        herdr_client.resolve_unique_agent_name("codex", "reviewer", agents)


def test_require_live_pane_id_uses_exact_snapshot_id_as_opaque_handle():
    panes = [{"pane_id": "w1:p1"}, {"pane_id": "p_7_9"}]

    assert herdr_client.require_live_pane_id("w1:p1", panes) == "w1:p1"
    assert herdr_client.require_live_pane_id("p_7_9", panes) == "p_7_9"
    for invalid in ("", "--help", "term_abc", "reviewer", "w1:p2"):
        with pytest.raises(ValueError, match="pane"):
            herdr_client.require_live_pane_id(invalid, panes)


def test_start_agent_capability_failure_does_not_mutate_layout(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client,
        "require_herdr_capabilities",
        lambda: (_ for _ in ()).throw(
            herdr_client.HerdrCapabilityError("Herdr protocol 19 required；请升级 Herdr")
        ),
    )
    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应修改 pane")),
    )

    result = herdr_client.start_agent("demo", "/tmp/project", "codex")

    assert result == {
        "available": True,
        "error_code": "herdr_upgrade_required",
        "error": "Herdr protocol 19 required；请升级 Herdr",
    }


def test_normalize_agent_args_preserves_argv_without_shell_execution():
    raw = '--model "gpt 5" ; touch /tmp/pwn $(id)'

    normalized = herdr_client.normalize_agent_args(raw)

    assert shlex.split(normalized) == [
        "--model", "gpt 5", ";", "touch", "/tmp/pwn", "$(id)",
    ]
    assert "';'" in normalized
    assert "'$(id)'" in normalized


@pytest.mark.parametrize(
    "args",
    [
        '--model "unterminated',
        "--model gpt\n--dangerous",
        "x" * (herdr_client.MAX_AGENT_ARGS_LENGTH + 1),
    ],
)
def test_normalize_agent_args_rejects_invalid_input(args):
    with pytest.raises(ValueError, match="启动参数"):
        herdr_client.normalize_agent_args(args)


def test_onboarding_required_when_config_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(tmp_path / "missing.toml"))

    assert herdr_client.onboarding_required() is True


def test_onboarding_completed_only_by_explicit_false(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(config))
    config.write_text('onboarding = false\n[theme]\nname = "terminal"\n')

    assert herdr_client.onboarding_required() is False

    config.write_text('[theme]\nname = "terminal"\n')
    assert herdr_client.onboarding_required() is True


def test_invalid_config_is_not_misreported_as_onboarding(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("invalid = [\n")
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(config))

    assert herdr_client.onboarding_required() is False


def test_list_sessions_prefers_stable_json_output(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return (
            '{"sessions":[{"name":"demo","running":true,'
            '"session_dir":"/tmp/a project","socket_path":"/tmp/demo.sock"}]}'
        )

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    assert herdr_client.list_sessions() == [{
        "name": "demo",
        "status": "running",
        "directory": "/tmp/a project",
        "socket": "/tmp/demo.sock",
    }]
    assert calls == [call(["session", "list", "--json"], timeout=8)]


def test_list_sessions_falls_back_for_old_herdr(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)

    def fake_run(args, timeout=10):
        if args[-1] == "--json":
            raise RuntimeError("unknown option --json")
        return "name status directory socket\ndemo running /tmp/demo /tmp/demo.sock\n"

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    assert herdr_client.list_sessions() == [{
        "name": "demo",
        "status": "running",
        "directory": "/tmp/demo",
        "socket": "/tmp/demo.sock",
    }]


def test_scoped_sessions_resolve_bounded_config_alias(
    monkeypatch, tmp_path,
) -> None:
    alias = tmp_path / "bounded-alias"
    target = tmp_path / "runtime-config"
    alias.mkdir(mode=0o700)
    (target / "herdr" / "sessions" / "demo").mkdir(
        parents=True, mode=0o700,
    )
    (alias / "herdr").symlink_to(target / "herdr", target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(alias))

    rows = herdr_client._scoped_session_rows([{
        "name": "demo", "status": "running",
        "directory": str(target / "herdr" / "sessions" / "demo"),
        "socket": str(target / "herdr" / "sessions" / "demo" / "herdr.sock"),
    }], "demo")

    assert len(rows) == 1


def test_pane_read_forwards_line_limit_to_agent_and_plain_panes(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []
    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda args, timeout=10: calls.append(call(args, timeout=timeout)) or "output",
    )

    agent = herdr_client.pane_read("demo", "w1:p2", 300, is_agent=True)
    plain = herdr_client.pane_read("demo", "w1:p3", 300, is_agent=False)

    assert agent["output"] == "output"
    assert plain["output"] == "output"
    assert calls == [
        call(
            [
                "--session", "demo", "agent", "read", "w1:p2",
                "--source", "recent-unwrapped", "--lines", "300",
            ],
            timeout=8,
        ),
        call(
            ["--session", "demo", "pane", "read", "w1:p3", "--lines", "300"],
            timeout=8,
        ),
    ]


def test_pane_read_falls_back_to_visible_when_agent_is_working(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "--source" not in args or "recent-unwrapped" in args:
            raise RuntimeError(
                'herdr failed: {"error":{"code":"agent_not_idle"}}'
            )
        return "current visible screen"

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.pane_read("demo", "w1:p4", 300, is_agent=True)

    assert result == {
        "available": True,
        "session": "demo",
        "pane_id": "w1:p4",
        "output": "current visible screen",
        "source": "visible",
        "degraded": True,
        "notice": "Agent 正在运行，仅显示当前画面；空闲后自动恢复完整历史。",
    }
    assert calls == [
        call(
            [
                "--session", "demo", "agent", "read", "w1:p4",
                "--source", "recent-unwrapped", "--lines", "300",
            ],
            timeout=8,
        ),
        call(
            [
                "--session", "demo", "agent", "read", "w1:p4",
                "--source", "visible", "--lines", "300",
            ],
            timeout=8,
        ),
    ]


def test_pane_read_does_not_hide_unrelated_agent_errors(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        raise RuntimeError("herdr failed: pane_not_found")

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.pane_read("demo", "w1:p404", 80, is_agent=True)

    assert result == {
        "available": True,
        "error": "herdr failed: pane_not_found",
        "output": "",
    }
    assert len(calls) == 1


def test_pane_summary_uses_visible_when_unwrapped_is_prompt_chrome(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)

    def fake_run(args, timeout=10):
        source = args[args.index("--source") + 1]
        if source == "recent-unwrapped":
            return "❯\n\n  ➜  agent-cockpit git:(main)\n"
        return (
            "● 检查结果\n\n"
            "Grok 对话未回复的原因分析：终端已经写完，瀑布流还钉着旧气泡。\n"
        )

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    result = herdr_client.pane_summary("cockpit", "w1:p6", 80)
    assert result["source"] == "visible"
    assert "检查结果" in result["summary"]
    assert "瀑布流还钉着旧气泡" in result["summary"]


def test_pane_summary_appends_visible_when_unwrapped_is_old_scrollback(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)

    def fake_run(args, timeout=10):
        source = args[args.index("--source") + 1]
        if source == "recent-unwrapped":
            return "实现完成总结 ✅\n已成功为 Agent Cockpit 4.0 实现团队协作功能。\n"
        return "● 检查结果\n\nGrok 对话未回复的原因分析：漏刮，不是没回。\n"

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    result = herdr_client.pane_summary("cockpit", "w1:p6", 80)
    assert result["source"] == "visible"
    assert "实现完成总结" in result["summary"]
    assert "检查结果" in result["summary"]
    assert "漏刮，不是没回" in result["summary"]


def test_latest_codex_final_reply_uses_current_turn_structured_message(tmp_path):
    home = tmp_path / "private-codex"
    home.mkdir(mode=0o700)
    session_id = "01a02877-ca29-73a1-87c2-3bd629ba288f"
    rollout = home / "sessions" / "2026" / "08" / "22" / f"rollout-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)

    def row(phase, text, created):
        return json.dumps({
            "timestamp": f"1970-01-01T00:00:{created:02d}Z",
            "type": "response_item",
            "payload": {
                "role": "assistant",
                "phase": phase,
                "content": [{"type": "output_text", "text": text}],
                "internal_chat_message_metadata_passthrough": {"create_time": created},
            },
        })

    rollout.write_text("\n".join([
        row("final_answer", "上一轮旧回复", 10),
        row("commentary", "本轮过程更新", 20),
        row("final_answer", "本轮准确最终回复", 21),
    ]) + "\n", encoding="utf-8")
    herdr_client._CODEX_ROLLOUT_CACHE.clear()

    result = herdr_client.latest_codex_final_reply(
        {"agent": "codex", "kind": "id", "value": session_id},
        since_ms=15_000,
        codex_home=str(home),
    )

    assert result == {
        "available": True,
        "text": "本轮准确最终回复",
        "created_ms": 21_000,
    }


@pytest.mark.parametrize(
    ("agent", "kind"),
    [
        ("codex", "codex"), ("claude", "claude"), ("kimi", "kimi"),
        ("opencode", "opencode"), ("grok", "grok"),
        ("qoder", "qodercli"), ("qodercli", "qodercli"), ("qodercn", "qodercli"),
    ],
)
def test_start_agent_unifies_every_supported_kind_on_native_start(monkeypatch, agent, kind):
    """H0.2：全部受支持 agent 统一原生 agent start，删除按类型回退 pane run。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": []},
        {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1"}]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p1"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    herdr_client.start_agent("demo", "/tmp/project", agent, layout="tab")

    start_calls = [c for c in calls if "agent" in c.args[0] and "start" in c.args[0]]
    assert len(start_calls) == 1
    argv = start_calls[0].args[0]
    # label 缺省：resolve_unique_agent_name 分配 agent-1，避免裸名与同 kind live agent 冲突
    assert argv[2:6] == ["agent", "start", f"{agent}-1", "--kind"]
    assert argv[6] == kind
    assert "--pane" in argv and "w1:p1" in argv
    assert "--timeout" in argv
    # 全程不回退 pane run / send-text / send-keys 键盘模拟
    flat = [c.args[0] for c in calls]
    assert not any(a[2:4] == ["pane", "run"] for a in flat)
    assert not any("send-text" in a or "send-keys" in a for a in flat)


def test_kimi_workspace_id_matches_known_paths():
    assert herdr_client._kimi_workspace_id("/home/fyc/github/agent-cockpit") == (
        "wd_agent-cockpit_ba85402d38b8"
    )
    assert herdr_client._kimi_workspace_id(
        "/home/fyc/pitapat/tts/146个训练文件_音色配置全覆盖_20260729"
    ) == "wd_146-_-_20260729_d3cffa88f1bc"


def test_ensure_kimi_workspace_trusted_writes_once(tmp_path, monkeypatch):
    monkeypatch.setattr(herdr_client, "_kimi_config_home", lambda: tmp_path / ".kimi-code")
    workdir = "/tmp/kimi-trust-demo"
    first = herdr_client._ensure_kimi_workspace_trusted(workdir)
    path = tmp_path / ".kimi-code" / "workspace-trust" / first
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["root"] == workdir
    assert isinstance(payload["trustedAt"], int)
    before = path.read_text(encoding="utf-8")
    again = herdr_client._ensure_kimi_workspace_trusted(workdir)
    assert again == first
    assert path.read_text(encoding="utf-8") == before


def test_start_agent_kimi_prewrites_workspace_trust(monkeypatch, tmp_path):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(herdr_client, "_kimi_config_home", lambda: tmp_path / ".kimi-code")
    snapshots = iter([
        {"panes": []},
        {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1"}]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    monkeypatch.setattr(herdr_client, "_run", lambda args, timeout=10: (
        'data: {"result":{"tab":{"focused_pane_id":"w1:p1"}}}'
        if "create" in args else ""
    ))

    result = herdr_client.start_agent(
        "demo", "/tmp/project", "kimi", model="kimi-code/k3", layout="tab",
    )
    assert result.get("error") is None
    workspace_id = herdr_client._kimi_workspace_id("/tmp/project")
    trust = tmp_path / ".kimi-code" / "workspace-trust" / workspace_id
    assert trust.is_file()
    assert json.loads(trust.read_text(encoding="utf-8"))["root"] == "/tmp/project"


def test_pane_send_send_mode_uses_atomic_pane_run(monkeypatch):
    """普通命令用原子 pane run，不再拆成 send-text + send-keys 两次。"""
    from chat_harness import restore_real_pane_send
    restore_real_pane_send(monkeypatch)
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: calls.append(call(args, timeout=timeout)) or "",
    )

    result = herdr_client.pane_send("demo", "w1:p2", "ls -la", "send")

    assert result == {"available": True, "sent": "ls -la", "mode": "send"}
    assert calls == [
        call(["--session", "demo", "pane", "run", "w1:p2", "ls -la"], timeout=8),
    ]
    assert not any(
        "send-text" in c.args[0] or "send-keys" in c.args[0] for c in calls
    )


def test_pane_send_prompt_mode_uses_agent_prompt(monkeypatch):
    """prompt 模式必须用 agent prompt，而非键盘 send-text/send-keys。"""
    from chat_harness import restore_real_pane_send
    restore_real_pane_send(monkeypatch)
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: calls.append(call(args, timeout=timeout)) or "",
    )

    result = herdr_client.pane_send("demo", "w1:p2", "hello world", "prompt")

    assert result == {"available": True, "sent": "hello world", "mode": "prompt"}
    assert calls == [
        call(["--session", "demo", "agent", "prompt", "w1:p2", "hello world"], timeout=10),
    ]


def test_pane_send_prompt_failure_does_not_fall_back_to_keyboard(monkeypatch):
    """agent prompt 失败时返回结构化错误，绝不回退键盘模拟。"""
    from chat_harness import restore_real_pane_send
    restore_real_pane_send(monkeypatch)
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        raise RuntimeError("agent prompt 失败: agent_not_found")

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.pane_send("demo", "w1:p2", "hello", "prompt")

    assert result["error"] == "agent prompt 失败: agent_not_found"
    assert len(calls) == 1
    assert calls[0] == call(
        ["--session", "demo", "agent", "prompt", "w1:p2", "hello"], timeout=10
    )
    assert not any(
        "send-text" in c.args[0] or "send-keys" in c.args[0] for c in calls
    )


def test_agent_wait_uses_native_agent_wait_primitive(monkeypatch):
    """等待 agent 状态使用原生 agent wait --until --timeout，不轮询不键盘模拟。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: calls.append(call(args, timeout=timeout)) or "",
    )

    result = herdr_client.agent_wait(
        "demo", "codex-2", until=["idle", "done"], timeout_ms=5000,
    )

    assert result == {
        "available": True, "session": "demo", "target": "codex-2",
        "matched": True,
    }
    assert calls == [
        call([
            "--session", "demo", "agent", "wait", "codex-2",
            "--until", "idle", "--until", "done", "--timeout", "5000",
        ], timeout=10),
    ]


def _idle_get() -> str:
    return json.dumps({
        "id": "cli:agent:get",
        "result": {
            "agent": "codex", "agent_status": "idle",
            "state_change_seq": 1, "pane_id": "w1:p2",
        },
    })


def _working_get() -> str:
    return json.dumps({
        "id": "cli:agent:get",
        "result": {
            "agent": "codex", "agent_status": "working",
            "state_change_seq": 2, "pane_id": "w1:p2",
        },
    })


def _idle_explain() -> str:
    return json.dumps({
        "visible_idle": True, "visible_blocker": False, "state": "idle",
        "matched_rule": {"id": "osc_title_idle"},
    })


def test_submit_agent_prompt_waits_for_working_and_is_the_receipt(
    monkeypatch,
) -> None:
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []
    prompted = {"n": 0}

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if args[2:4] == ["agent", "explain"]:
            return _idle_explain()
        if args[2:4] == ["agent", "get"]:
            return _working_get() if prompted["n"] else _idle_get()
        if args[2:4] == ["agent", "prompt"]:
            prompted["n"] += 1
            return ""
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.submit_agent_prompt_until_working(
        "demo", "w1:p2", "COCKPIT_WAKEUP_V1\nStart",
    )

    assert result["available"] is True
    assert result["submitted"] is True
    assert result["executing"] is True
    assert result.get("error") is None
    assert result["state_change_seq"] == 2
    assert result["status"] == "working"
    verbs = [c.args[0][3] for c in calls if c.args[0][2] == "agent"]
    assert verbs[0] == "focus"
    assert "explain" in verbs
    assert "focus" in verbs
    assert "prompt" in verbs
    prompt = next(c.args[0] for c in calls if c.args[0][2:4] == ["agent", "prompt"])
    assert prompt[4:6] == ["w1:p2", "COCKPIT_WAKEUP_V1\nStart"]
    assert "--wait" in prompt
    assert prompt[prompt.index("--timeout") + 1] == "15000"
    assert not any("send-text" in c.args[0] or "read" in c.args[0] for c in calls)


def test_execution_proven_rejects_blocked_even_when_seq_advances() -> None:
    assert herdr_client._execution_proven(
        {"state_change_seq": 1},
        {"agent_status": "blocked", "state_change_seq": 2},
    ) is False
    assert herdr_client._execution_proven(
        {"state_change_seq": 1},
        {"agent_status": "idle", "state_change_seq": 2},
    ) is False
    assert herdr_client._execution_proven(
        {"state_change_seq": 1},
        {"agent_status": "unknown", "state_change_seq": 2},
    ) is False
    assert herdr_client._execution_proven(
        {"state_change_seq": 1},
        {"agent_status": "working", "state_change_seq": 2},
    ) is True


def test_submit_agent_prompt_blocked_after_seq_is_typed_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []
    prompted = {"n": 0}

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if args[2:4] == ["agent", "explain"]:
            return _idle_explain()
        if args[2:4] == ["agent", "get"]:
            if prompted["n"]:
                return json.dumps({
                    "id": "cli:agent:get",
                    "result": {
                        "agent": "codex", "agent_status": "blocked",
                        "state_change_seq": 2, "pane_id": "w1:p2",
                    },
                })
            return _idle_get()
        if args[2:4] == ["agent", "prompt"]:
            prompted["n"] += 1
            return ""
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    result = herdr_client.submit_agent_prompt_until_working(
        "demo", "w1:p2", "COCKPIT_WAKEUP_V1\nStart",
    )
    assert result["executing"] is False
    assert result["submitted"] is False
    assert result["error"] == "blocked"
    assert result.get("status") == "blocked"
    assert not any(c.args[0][2:4] == ["agent", "send-keys"] for c in calls)


def test_submit_agent_prompt_focus_failure_does_not_prompt(monkeypatch) -> None:
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if args[2:4] == ["agent", "explain"]:
            return _idle_explain()
        if args[2:4] == ["agent", "get"]:
            return _idle_get()
        if args[2:4] == ["agent", "focus"]:
            raise RuntimeError("agent focus 失败: pane_not_found")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    result = herdr_client.submit_agent_prompt_until_working(
        "demo", "w1:p2", "COCKPIT_WAKEUP_V1\nStart",
    )
    assert result["executing"] is False
    assert result["submitted"] is False
    assert "focus" in result["error"]
    assert not any(c.args[0][2:4] == ["agent", "prompt"] for c in calls)
    assert not any(c.args[0][2:4] == ["agent", "send-keys"] for c in calls)


def test_submit_agent_prompt_auth_wall_is_typed_failure(monkeypatch) -> None:
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if args[2:4] == ["agent", "explain"]:
            return json.dumps({
                "visible_idle": False, "visible_blocker": True,
                "state": "blocked", "matched_rule": {"id": "login_wall"},
            })
        return _idle_get()

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    result = herdr_client.submit_agent_prompt_until_working(
        "demo", "w1:p2", "COCKPIT_WAKEUP_V1\nStart",
    )
    assert result["executing"] is False
    assert result["submitted"] is False
    assert result["error"] == "auth_wall"
    assert not any(c.args[0][2:4] == ["agent", "prompt"] for c in calls)


def test_submit_agent_prompt_stall_fails_closed_without_keyboard_retry(monkeypatch) -> None:
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if args[2:4] == ["agent", "explain"]:
            return _idle_explain()
        if args[2:4] == ["agent", "get"]:
            return _idle_get()
        if args[2:4] == ["agent", "prompt"]:
            raise RuntimeError("agent prompt 失败: timeout")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.submit_agent_prompt_until_working(
        "demo", "w1:p2", "COCKPIT_WAKEUP_V1\nStart",
    )

    assert result["available"] is True
    assert result["submitted"] is False
    assert result["executing"] is False
    assert result["error"]
    verbs = [c.args[0][3] for c in calls if c.args[0][2] == "agent"]
    assert verbs[0] == "focus"
    assert "focus" in verbs
    assert "prompt" in verbs
    assert "send-keys" not in verbs
    assert "wait" not in verbs
    assert not any("send-text" in c.args[0] or "read" in c.args[0] for c in calls)


def test_submit_agent_prompt_never_uses_enter_to_manufacture_working(monkeypatch) -> None:
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []
    working = {"n": False}

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if args[2:4] == ["agent", "explain"]:
            return _idle_explain()
        if args[2:4] == ["agent", "get"]:
            return _working_get() if working["n"] else _idle_get()
        if args[2:4] == ["agent", "prompt"]:
            raise RuntimeError("agent prompt 失败: timeout")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.submit_agent_prompt_until_working(
        "demo", "w1:p2", "COCKPIT_WAKEUP_V1\nStart",
    )

    assert result["submitted"] is False
    assert result["executing"] is False
    assert result["error"]
    assert not any(c.args[0][2:4] == ["agent", "send-keys"] for c in calls)


def test_agent_wait_reports_timeout_without_keyboard_fallback(monkeypatch):
    """agent wait 超时返回 matched=False 的结构化错误，不回退键盘。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        raise RuntimeError("agent wait 失败: timeout")

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.agent_wait("demo", "codex-2", timeout_ms=1000)

    assert result["matched"] is False
    assert "timeout" in result["error"]
    assert len(calls) == 1
    assert not any(
        "send-text" in c.args[0] or "send-keys" in c.args[0] for c in calls
    )


def test_start_agent_reuses_existing_pane_with_cwd(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client,
        "_snapshot_session",
        lambda session: {
            "panes": [{
                "pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1",
                "agent": "codex", "cwd": "/tmp/project",
            }],
        },
    )
    calls = []
    monkeypatch.setattr(herdr_client, "_run", lambda args, timeout=10: calls.append(args) or "")

    assert herdr_client.start_agent("demo", "/tmp/project", "codex") == {
        "available": True,
        "pane_id": "w1:p2",
        "agent": "codex",
        "cwd": "/tmp/project",
        "reused": True,
        "msg": "codex pane 已存在(w1:p2),跳过",
    }
    assert ["--session", "demo", "pane", "rename", "w1:p2", "codex"] in calls
    assert ["--session", "demo", "tab", "rename", "w1:t2", "codex"] in calls
    assert ["--session", "demo", "workspace", "rename", "w1", "demo"] in calls


def test_snapshot_handles_unexpected_json_shapes(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_run", lambda *args, **kwargs: "[]")

    assert herdr_client._snapshot_session("demo") == {
        "session": "demo", "error": "snapshot parse failed", "panes": []
    }

    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda *args, **kwargs: '{"result":{"snapshot":{"panes":"bad"}}}',
    )
    assert herdr_client._snapshot_session("demo") == {
        "session": "demo", "error": "snapshot parse failed", "panes": []
    }


def test_notify_opencode_color_scheme_removed():
    """H0.5 清债:OpenCode pane 字节注入已删除,主题只走外层终端协议/Herdr 传播。"""
    assert not hasattr(herdr_client, "notify_opencode_color_scheme")


def test_start_agent_uses_snapshot_delta_before_native_start(monkeypatch):
    """无创建响应时只能选前后 snapshot 唯一新增 pane，再用原生 agent start 启动。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": [{"pane_id": "w1:p9", "agent": None}]},
        {"panes": [
            {"pane_id": "w1:p9", "agent": None},
            {"pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1", "agent": None},
        ]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project", "codex", layout="tab")

    assert result["pane_id"] == "w1:p2"
    assert result["layout"] == "tab"
    assert result["agent"] == "codex"
    # label 缺省：resolve_unique_agent_name 分配 codex-1
    assert result["name"] == "codex-1"
    assert result["kind"] == "codex"
    # 全 agent 统一原生 agent start，不再 pane run
    assert call(
        [
            "--session", "demo", "agent", "start", "codex-1",
            "--kind", "codex", "--pane", "w1:p2", "--timeout", "60000",
        ],
        timeout=65,
    ) in calls
    assert not any(
        c.args[0][:4] == ["--session", "demo", "pane", "run"] for c in calls
    )
    assert call(
        ["--session", "demo", "pane", "rename", "w1:p2", "codex-1"],
        timeout=5,
    ) in calls
    assert call(
        ["--session", "demo", "tab", "rename", "w1:t2", "codex-1"],
        timeout=5,
    ) in calls
    assert call(
        ["--session", "demo", "workspace", "rename", "w1", "demo"],
        timeout=5,
    ) in calls


def test_start_agent_renames_workspace_tab_and_pane(monkeypatch):
    """tab 布局必须改用户实际看到的三层名称，而不只是 pane label。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": []},
        {"panes": [{
            "pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1",
        }]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p2"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project", layout="tab")

    assert result["pane_id"] == "w1:p2"
    assert result["name"] == "codex-1"
    assert call(
        ["--session", "demo", "agent", "start", "codex-1",
         "--kind", "codex", "--pane", "w1:p2", "--timeout", "60000"],
        timeout=65,
    ) in calls
    assert call(["--session", "demo", "pane", "rename", "w1:p2", "codex-1"], timeout=5) in calls
    assert call(["--session", "demo", "tab", "rename", "w1:t2", "codex-1"], timeout=5) in calls
    assert call(["--session", "demo", "workspace", "rename", "w1", "demo"], timeout=5) in calls


def test_start_agent_forces_opencode_to_tab_and_rolls_back_on_start_failure(monkeypatch):
    """opencode 强制独立 tab；原生 agent start 失败时回滚本次 pane，不回退键盘模拟。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": []},
        {"panes": [{"pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1"}]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p2"}}}'
        if "agent" in args and "start" in args:
            raise RuntimeError("agent start 失败: readiness timeout")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent(
        "demo", "/tmp/project", agent="opencode", layout="right"
    )

    assert result["error"] == "agent start 失败: readiness timeout"
    assert result["rolled_back"] is True
    # opencode 永远强制独立 tab，不会 split
    assert not any("split" in c.args[0] for c in calls)
    # 启动失败只回滚 pane，不回退 send-text/send-keys 键盘模拟
    assert not any("send-text" in c.args[0] or "send-keys" in c.args[0] for c in calls)
    assert call(
        ["--session", "demo", "pane", "close", "w1:p2"], timeout=5
    ) in calls


def test_agent_start_timeout_gives_codex_a_minute():
    assert herdr_client._agent_start_timeout("codex") == 60.0
    assert herdr_client._agent_start_timeout("claude") == 60.0


def test_start_agent_qodercli_passes_slow_timeout_to_native_start(monkeypatch):
    """QoderCLI 冷启动由 agent start --timeout 兜底，Cockpit 不再自造 readiness 轮询。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": []},
        {"panes": [{"pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1"}]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p2"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent(
        "demo", "/tmp/project", agent="qodercli", layout="tab", label="qoder-2",
        args='--model "qwen 2.5" ; $(id)',
    )

    assert result["pane_id"] == "w1:p2"
    assert result["agent"] == "qodercli"
    assert result["label"] == "qoder-2"
    assert call(
        ["--session", "demo", "pane", "close", "w1:p2"], timeout=5,
    ) not in calls
    assert call(
        [
            "--session", "demo", "agent", "start", "qoder-2",
            "--kind", "qodercli", "--pane", "w1:p2", "--timeout", "60000",
            "--", "--model", "qwen 2.5", ";", "$(id)",
        ],
        timeout=65,
    ) in calls
    assert call(
        ["--session", "demo", "pane", "run", "w1:p2", "qodercli"],
        timeout=8,
    ) not in calls


def test_start_agent_grok_uses_native_start(monkeypatch):
    """grok 与所有受支持 agent 一致，统一走原生 agent start。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": []},
        {"panes": [{"pane_id": "w1:p3", "tab_id": "w1:t3", "workspace_id": "w1"}]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p3"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project", agent="grok")

    assert result["pane_id"] == "w1:p3"
    assert result["name"] == "grok-1"
    assert call(
        [
            "--session", "demo", "agent", "start", "grok-1",
            "--kind", "grok", "--pane", "w1:p3", "--timeout", "60000",
        ],
        timeout=65,
    ) in calls
    assert call(
        ["--session", "demo", "pane", "run", "w1:p3", "grok"],
        timeout=8,
    ) not in calls


def _native_start_harness(monkeypatch, pane="w1:p3"):
    """原生启动路径公共桩:可控时钟(供 busy 重试计时)+ 固定 snapshot 序列。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    clock = {"now": 0.0}
    monkeypatch.setattr(herdr_client.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        herdr_client.time, "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    snapshots = iter([
        {"panes": []},
        {"panes": [{"pane_id": pane, "tab_id": "w1:t3", "workspace_id": "w1"}]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    return clock, pane


def test_start_agent_grok_retries_pane_busy_then_succeeds(monkeypatch):
    """新建 pane shell 未就绪(busy)时按 0.5s 重试,就绪后正常启动不回滚。"""
    clock, pane = _native_start_harness(monkeypatch)
    calls = []
    busy_left = {"n": 2}

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"%s"}}}' % pane
        if "agent" in args and "start" in args and busy_left["n"]:
            busy_left["n"] -= 1
            raise RuntimeError(
                'agent start 失败: {"error":{"code":"agent_pane_busy"}}'
            )
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project", agent="grok")

    assert result.get("error") is None
    assert result["pane_id"] == pane
    starts = [c for c in calls if "start" in c.args[0] and "agent" in c.args[0]]
    assert len(starts) == 3  # 2 次 busy + 1 次成功
    assert clock["now"] >= 1.0  # 两次 0.5s 重试等待
    assert call(
        ["--session", "demo", "pane", "close", pane], timeout=5,
    ) not in calls


def test_start_agent_grok_pane_busy_gives_up_and_rolls_back(monkeypatch):
    """busy 持续到就绪窗口(10s)耗尽:不无限重试,保留原错误并关闭本次 pane。"""
    clock, pane = _native_start_harness(monkeypatch)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"%s"}}}' % pane
        if "agent" in args and "start" in args:
            raise RuntimeError(
                'agent start 失败: {"error":{"code":"agent_pane_busy"}}'
            )
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project", agent="grok")

    assert "agent_pane_busy" in result["error"]
    assert result["rolled_back"] is True
    starts = [c for c in calls if "start" in c.args[0] and "agent" in c.args[0]]
    assert 15 <= len(starts) <= 25  # 10s/0.5s 有限重试,非死循环
    assert clock["now"] >= 10.0
    assert call(
        ["--session", "demo", "pane", "close", pane], timeout=5,
    ) in calls


def test_start_agent_grok_non_busy_error_not_retried(monkeypatch):
    """非 busy 的启动错误不重试,直接回滚。"""
    _clock, pane = _native_start_harness(monkeypatch)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"%s"}}}' % pane
        if "agent" in args and "start" in args:
            raise RuntimeError("agent start 失败: unknown kind")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project", agent="grok")

    assert "unknown kind" in result["error"]
    assert result["rolled_back"] is True
    starts = [c for c in calls if "start" in c.args[0] and "agent" in c.args[0]]
    assert len(starts) == 1


def test_start_agent_qodercli_rolls_back_when_native_start_fails(monkeypatch):
    """原生 agent start 未在 --timeout 内达到 readiness 时，关闭本次新建 pane。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": []},
        {"panes": [{"pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1"}]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p2"}}}'
        if "agent" in args and "start" in args:
            raise RuntimeError("agent start 失败: readiness timeout")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent(
        "demo", "/tmp/project", agent="qodercli", layout="tab",
    )

    assert result["rolled_back"] is True
    assert "readiness timeout" in result["error"]
    assert call(
        ["--session", "demo", "pane", "close", "w1:p2"], timeout=5,
    ) in calls


def test_qoder_aliases_get_longer_start_timeout_only():
    assert herdr_client._agent_start_timeout("qoder") == 60.0
    assert herdr_client._agent_start_timeout("qodercli") == 60.0
    assert herdr_client._agent_start_timeout("qodercn") == 60.0
    assert herdr_client._agent_start_timeout("grok") == 60.0
    assert herdr_client._agent_start_timeout("codex") == 60.0
    assert herdr_client._agent_start_timeout("opencode") == 60.0


def test_start_agent_reuses_only_matching_workdir(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client,
        "_snapshot_session",
        lambda session: {
            "panes": [{"pane_id": "w1:p5", "agent": "codex", "cwd": "/tmp/project"}]
        },
    )
    calls = []
    monkeypatch.setattr(herdr_client, "_run", lambda args, timeout=10: calls.append(args) or "")

    result = herdr_client.start_agent("demo", "/tmp/project/./", "codex")

    assert result["reused"] is True
    assert result["pane_id"] == "w1:p5"


def test_start_agent_uses_label_to_create_second_same_type_instance(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": [{
            "pane_id": "w1:p2", "agent": "codex", "cwd": "/tmp/project",
            "label": "codex-1",
        }]},
        {"panes": [
            {"pane_id": "w1:p2", "agent": "codex", "cwd": "/tmp/project", "label": "codex-1"},
            {"pane_id": "w1:p3", "tab_id": "w1:t3", "workspace_id": "w1"},
        ]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p3"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent(
        "demo", "/tmp/project", "codex", layout="tab", label="codex-2",
    )

    assert result["pane_id"] == "w1:p3"
    assert result["label"] == "codex-2"
    assert result.get("reused") is not True
    # 同类型第二实例也用原生 agent start，name 用唯一 label
    assert call(
        [
            "--session", "demo", "agent", "start", "codex-2",
            "--kind", "codex", "--pane", "w1:p3", "--timeout", "60000",
        ],
        timeout=65,
    ) in calls
    assert call(
        ["--session", "demo", "pane", "rename", "w1:p3", "codex-2"],
        timeout=5,
    ) in calls


def test_start_agent_assigns_unique_runtime_name_for_same_kind_second_instance(monkeypatch):
    """同 kind + 不同 cwd + 无 label：resolve_unique_agent_name 分配唯一名，不在创建后冲突。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    # 已有一个 codex（live name codex-1）在别的 cwd；本次不同 cwd、无 label
    snapshots = iter([
        {"panes": [{"pane_id": "w1:p1", "agent": "codex", "cwd": "/tmp/other"}],
         "agents": [{"name": "codex-1"}]},
        {"panes": [
            {"pane_id": "w1:p1", "agent": "codex", "cwd": "/tmp/other"},
            {"pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1"},
        ]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p2"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project", "codex", layout="tab")

    # cwd 不同 → 不复用 → 新建；codex-1 已 live → 分配 codex-2，绝不回退裸名 codex
    assert result["pane_id"] == "w1:p2"
    assert result["name"] == "codex-2"
    assert result.get("reused") is not True
    assert call(
        ["--session", "demo", "agent", "start", "codex-2",
         "--kind", "codex", "--pane", "w1:p2", "--timeout", "60000"],
        timeout=65,
    ) in calls
    assert not any(
        c.args[0][:6] == ["--session", "demo", "agent", "start", "codex", "--kind"]
        for c in calls
    )


def test_start_agent_persists_launch_descriptor_retrievable_by_pane_and_name(monkeypatch):
    """启动成功持久化权威契约 {name, kind, args}，可按 session+pane / session+name 精确取回。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": []},
        {"panes": [{"pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1"}]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: 'data: {"result":{"tab":{"focused_pane_id":"w1:p2"}}}'
        if "create" in args else "",
    )

    herdr_client.start_agent(
        "demo", "/tmp/project", "codex", layout="tab", label="lead",
        args='--model "gpt 5" ; echo hi',
    )

    by_pane = herdr_client.get_launch_descriptor("demo", "w1:p2")
    by_name = herdr_client.get_launch_descriptor_by_name("demo", "lead")
    assert by_pane == by_name
    # args 为原生 argv 列表，保留空格/分号原样，未被 shell 重组
    assert by_pane == {
        "session": "demo", "name": "lead", "kind": "codex",
        "args": ["--model", "gpt 5", ";", "echo", "hi"],
        "agent": "codex", "pane_id": "w1:p2", "workdir": "/tmp/project",
    }


def test_rebind_live_launch_descriptor_reactivates_retired_instance():
    instance = "i-yzh33bkopbhev3ae654tc7tila"
    herdr_client.save_launch_descriptor(
        session="old-session", pane_id="w1:p3", name=instance, kind="grok",
        args=[], agent="grok", workdir="/old/worktree", instance_id=instance,
        display_name="grok-cockpit",
    )
    herdr_client.update_launch_descriptor_by_instance(instance, pane_id="w1:p3")
    data = herdr_client._load_launch_descriptors()
    data["descriptors"][f"instance|{instance}"]["state"] = "retired"
    herdr_client._save_launch_descriptors(data)
    assert herdr_client.get_launch_descriptor("cockpit", "w1:p1") is None

    rebound = herdr_client.rebind_live_launch_descriptor(
        instance_id=instance,
        session="cockpit",
        pane_id="w1:p1",
        workdir="/home/fyc/github/agent-cockpit",
        mail_name="BrownDesert",
    )
    assert rebound["state"] == "active"
    assert rebound["session"] == "cockpit"
    assert rebound["pane_id"] == "w1:p1"
    found = herdr_client.get_launch_descriptor("cockpit", "w1:p1")
    assert found is not None
    assert found["instance_id"] == instance
    assert found["mail_name"] == "BrownDesert"


def test_start_agent_descriptor_uses_canonical_kind_for_aliases(monkeypatch):
    """qoder 别名启动时 descriptor 的 kind 必须是 canonical qodercli。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": []},
        {"panes": [{"pane_id": "w1:p5", "tab_id": "w1:t5", "workspace_id": "w1"}]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p5"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    herdr_client.start_agent("demo", "/tmp/project", "qoder", layout="tab", label="q-1")

    assert herdr_client.get_launch_descriptor("demo", "w1:p5")["kind"] == "qodercli"
    assert call(
        ["--session", "demo", "agent", "start", "q-1",
         "--kind", "qodercli", "--pane", "w1:p5", "--timeout", "60000"],
        timeout=65,
    ) in calls


def test_start_agent_keeps_distinct_descriptors_for_same_kind_instances(monkeypatch):
    """同类型多实例各自有独立 name 与独立 descriptor，按 pane 各自取回不串。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    state = {"panes": [], "agents": []}
    counter = {"n": 1}

    def snapshot(session):
        return {"panes": list(state["panes"]), "agents": list(state["agents"])}

    def fake_run(args, timeout=10):
        if "create" in args:
            pid = "w1:p%d" % (counter["n"] + 1)
            counter["n"] += 1
            state["panes"].append({
                "pane_id": pid, "tab_id": "w1:t%d" % counter["n"], "workspace_id": "w1",
            })
            return 'data: {"result":{"tab":{"focused_pane_id":"%s"}}}' % pid
        if "agent" in args and "start" in args:
            state["agents"].append({"name": args[args.index("start") + 1]})
        return ""

    monkeypatch.setattr(herdr_client, "_snapshot_session", snapshot)
    monkeypatch.setattr(herdr_client, "_run", fake_run)

    herdr_client.start_agent("demo", "/tmp/project", "codex", layout="tab")
    herdr_client.start_agent("demo", "/tmp/project", "codex", layout="tab")

    d1 = herdr_client.get_launch_descriptor("demo", "w1:p2")
    d2 = herdr_client.get_launch_descriptor("demo", "w1:p3")
    assert d1["name"] == "codex-1"
    assert d2["name"] == "codex-2"
    assert d1 != d2


def test_managed_start_allows_duplicate_display_names_but_uses_opaque_runtime_id(
    monkeypatch,
):
    instance_id = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {
            "panes": [{
                "pane_id": "w1:p1", "agent": "codex", "cwd": "/tmp/project",
                "label": "同名",
            }],
            "agents": [{"name": "i-bbbbbbbbbbbbbbbbbbbbbbbbbb"}],
        },
        {"panes": [
            {"pane_id": "w1:p1", "agent": "codex", "cwd": "/tmp/project", "label": "同名"},
            {"pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1"},
        ]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p2"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent(
        "demo", "/tmp/project", "codex", layout="tab", label="同名",
        instance_id=instance_id,
    )

    assert result.get("reused") is not True
    assert result["instance_id"] == instance_id
    assert result["name"] == instance_id
    assert result["display_name"] == "同名"
    trust_args = herdr_client._workspace_codex_trust_args("/tmp/project")
    assert call(
        ["--session", "demo", "agent", "start", instance_id,
         "--kind", "codex", "--pane", "w1:p2", "--timeout", "60000",
         "--", *trust_args],
        timeout=65,
    ) in calls
    descriptor = herdr_client.get_launch_descriptor("demo", "w1:p2")
    assert descriptor["instance_id"] == instance_id
    assert descriptor["display_name"] == "同名"
    assert descriptor["args"] == []


def test_managed_descriptors_keep_duplicate_display_names_separate_and_tombstoned():
    first = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    second = "i-bbbbbbbbbbbbbbbbbbbbbbbbbb"
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p1", name=first, kind="codex", args=[],
        agent="codex", instance_id=first, display_name="同名",
    )
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p2", name=second, kind="codex", args=[],
        agent="codex", instance_id=second, display_name="同名",
    )

    assert herdr_client.get_launch_descriptor_by_instance(first)["pane_id"] == "w1:p1"
    assert herdr_client.get_launch_descriptor_by_instance(second)["pane_id"] == "w1:p2"

    pending = herdr_client.mark_launch_descriptor_retirement_pending("demo", "w1:p1")
    assert pending["instance_ids"] == [first]
    herdr_client.finalize_launch_descriptor_retirement(first)

    assert herdr_client.get_launch_descriptor("demo", "w1:p1") is None
    tombstone = herdr_client.get_launch_descriptor_by_instance(first, include_retired=True)
    assert tombstone["state"] == "retired"
    assert herdr_client.get_launch_descriptor("demo", "w1:p2")["instance_id"] == second


def test_get_launch_descriptor_returns_none_without_guessing(monkeypatch, tmp_path):
    """无契约时返回 None；调用方（restart）不得据此猜测 name/kind/args。"""
    monkeypatch.setenv("COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "none.json"))
    assert herdr_client.get_launch_descriptor("demo", "w1:p9") is None
    assert herdr_client.get_launch_descriptor_by_name("demo", "ghost") is None


def test_start_agent_reuse_exposes_descriptor_name_without_fabricating(monkeypatch):
    """复用由本路径启动过的 pane 时暴露其权威 name/kind；legacy pane 无契约则不臆造。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snap_with_codex = {
        "panes": [{"pane_id": "w1:p2", "agent": "codex", "cwd": "/tmp/project",
                   "tab_id": "w1:t2", "workspace_id": "w1"}],
    }
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: snap_with_codex)
    monkeypatch.setattr(herdr_client, "_run", lambda args, timeout=10: "")
    # legacy：尚无 descriptor
    legacy = herdr_client.start_agent("demo", "/tmp/project", "codex")
    assert legacy["reused"] is True
    assert "name" not in legacy and "kind" not in legacy
    # 写入契约后再复用：应暴露权威 name/kind
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p2", name="codex-1", kind="codex", args=[], agent="codex",
    )
    with_desc = herdr_client.start_agent("demo", "/tmp/project", "codex")
    assert with_desc["name"] == "codex-1"
    assert with_desc["kind"] == "codex"


def test_clear_launch_descriptors_removes_only_target_session():
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p1", name="a-1", kind="codex", args=[], agent="codex",
    )
    herdr_client.save_launch_descriptor(
        session="other", pane_id="w1:p1", name="a-1", kind="codex", args=[], agent="codex",
    )
    assert herdr_client.clear_launch_descriptors("demo") == {"cleared": 1}
    assert herdr_client.clear_launch_descriptors("demo") == {"cleared": 0}  # 幂等
    assert herdr_client.get_launch_descriptor_by_name("demo", "a-1") is None
    assert herdr_client.get_launch_descriptor_by_name("other", "a-1") is not None


def test_stop_session_treats_dead_socket_as_already_stopped(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    payload = (
        'herdr session stop demo 失败: {"error":{"code":"session_stop_failed",'
        '"message":"session demo is not running or cannot be reached at '
        '/home/fyc/.config/herdr/sessions/demo/herdr.sock"}}'
    )
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: (_ for _ in ()).throw(RuntimeError(payload)),
    )
    result = herdr_client.stop_session("demo")
    assert result["stopped"] == "demo"
    assert result.get("already_stopped") is True
    assert "error" not in result


def test_stop_session_still_surfaces_real_stop_failure(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: (_ for _ in ()).throw(RuntimeError("herdr session stop demo 失败: busy")),
    )
    result = herdr_client.stop_session("demo")
    assert result.get("error") == "herdr session stop demo 失败: busy"
    assert "stopped" not in result


def test_delete_session_treats_already_absent_as_success(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: (_ for _ in ()).throw(
            RuntimeError("herdr session delete demo 失败: session not found")
        ),
    )
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p1", name="codex-1", kind="codex",
        args=["--old"], agent="codex",
    )
    result = herdr_client.delete_session("demo")
    assert result["deleted"] == "demo"
    assert "error" not in result
    assert herdr_client.get_launch_descriptor("demo", "w1:p1") is None


def test_delete_session_clears_descriptors_only_on_success(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p1", name="codex-1", kind="codex",
        args=["--old"], agent="codex",
    )
    # delete 失败 → 不清理，结果只含 error
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: (_ for _ in ()).throw(RuntimeError("herdr failed: not_stopped")),
    )
    failed = herdr_client.delete_session("demo")
    assert failed["error"] == "herdr failed: not_stopped"
    assert "descriptors_cleared" not in failed
    assert herdr_client.get_launch_descriptor("demo", "w1:p1") is not None
    # delete 成功 → 清理
    monkeypatch.setattr(herdr_client, "_run", lambda args, timeout=10: "")
    ok = herdr_client.delete_session("demo")
    assert ok["deleted"] == "demo"
    assert ok["descriptors_cleared"] == 1
    assert herdr_client.get_launch_descriptor("demo", "w1:p1") is None


def test_delete_session_preserves_other_sessions_descriptors(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_run", lambda args, timeout=10: "")
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p1", name="codex-1", kind="codex",
        args=["--a"], agent="codex",
    )
    herdr_client.save_launch_descriptor(
        session="other", pane_id="w1:p1", name="codex-1", kind="codex",
        args=["--b"], agent="codex",
    )
    result = herdr_client.delete_session("demo")
    assert result["descriptors_cleared"] == 1
    assert herdr_client.get_launch_descriptor("demo", "w1:p1") is None
    kept = herdr_client.get_launch_descriptor("other", "w1:p1")
    assert kept is not None and kept["args"] == ["--b"]


def test_same_name_session_recreate_cannot_read_stale_descriptor(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_run", lambda args, timeout=10: "")
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p1", name="agent-1", kind="codex",
        args=["--old"], agent="codex",
    )
    herdr_client.delete_session("demo")
    # 同名 session 重建后，即便 Herdr 复用 w1:p1 / agent-1，也读不到上一代契约
    assert herdr_client.get_launch_descriptor("demo", "w1:p1") is None
    assert herdr_client.get_launch_descriptor_by_name("demo", "agent-1") is None


def test_close_pane_clears_its_descriptor_only_on_success(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p2", name="codex-1", kind="codex",
        args=[], agent="codex",
    )
    flag = {"fail": True}

    def fake_run(args, timeout=10):
        if flag["fail"]:
            raise RuntimeError("herdr failed: busy")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    failed = herdr_client.close_pane("demo", "w1:p2")
    assert failed["error"] == "herdr failed: busy"
    assert herdr_client.get_launch_descriptor("demo", "w1:p2") is not None  # close 失败不清
    flag["fail"] = False
    ok = herdr_client.close_pane("demo", "w1:p2")
    assert ok["closed"] == "w1:p2"
    assert ok["descriptors_cleared"] == 1
    assert herdr_client.get_launch_descriptor("demo", "w1:p2") is None


def test_close_managed_pane_preserves_pending_retirement_tombstone(monkeypatch):
    instance_id = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p2", name=instance_id, kind="codex",
        args=[], agent="codex", instance_id=instance_id, display_name="同名",
    )
    flag = {"fail": True}

    def fake_run(args, timeout=10):
        if flag["fail"]:
            raise RuntimeError("herdr failed: busy")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    assert herdr_client.close_pane("demo", "w1:p2")["error"] == "herdr failed: busy"
    assert herdr_client.get_launch_descriptor_by_instance(instance_id) is not None

    flag["fail"] = False
    result = herdr_client.close_pane("demo", "w1:p2")
    assert result["retirement_pending"] == [instance_id]
    assert herdr_client.get_launch_descriptor_by_instance(instance_id) is None
    tombstone = herdr_client.get_launch_descriptor_by_instance(
        instance_id, include_retired=True,
    )
    assert tombstone["state"] == "retirement_pending"


def test_delete_session_marks_each_managed_instance_pending(monkeypatch):
    first = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    second = "i-bbbbbbbbbbbbbbbbbbbbbbbbbb"
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_run", lambda args, timeout=10: "")
    for pane_id, instance_id in (("w1:p1", first), ("w1:p2", second)):
        herdr_client.save_launch_descriptor(
            session="demo", pane_id=pane_id, name=instance_id, kind="codex",
            args=[], agent="codex", instance_id=instance_id, display_name="同名",
        )

    result = herdr_client.delete_session("demo")

    assert result["retirement_pending"] == [first, second]
    assert {item["instance_id"] for item in herdr_client.pending_launch_descriptor_retirements()} == {
        first, second,
    }


def test_descriptor_cleanup_failure_is_surfaced_not_silent(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p1", name="codex-1", kind="codex",
        args=["--old"], agent="codex",
    )
    monkeypatch.setattr(herdr_client, "_run", lambda args, timeout=10: "")  # delete 成功
    monkeypatch.setattr(
        herdr_client, "_save_launch_descriptors",
        lambda data: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = herdr_client.delete_session("demo")
    # herdr 侧已删除，但 descriptor 清理失败必须结构化暴露，不静默宣告安全
    assert result["deleted"] == "demo"
    assert result["descriptor_cleanup_error"] == "disk full"
    assert "descriptors_cleared" not in result
    # 清理失败 → 盘上旧记录仍在（读取走 _load，不受 _save mock 影响），由运营处理
    assert herdr_client.get_launch_descriptor("demo", "w1:p1") is not None


def test_start_agent_rejects_label_used_by_another_pane(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client,
        "_snapshot_session",
        lambda session: {"panes": [{
            "pane_id": "w1:p2", "agent": "opencode", "cwd": "/tmp/other",
            "label": "codex-2",
        }]},
    )
    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应创建 pane")),
    )

    result = herdr_client.start_agent(
        "demo", "/tmp/project", "codex", label="codex-2",
    )

    assert result == {
        "available": True,
        "error": "实例名称已被 pane w1:p2 使用: codex-2",
    }


def test_start_agent_reports_missing_executable_before_split(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: {"panes": []})
    monkeypatch.setattr(
        herdr_client,
        "_find_agent_bin",
        lambda agent: "/definitely/missing/qoder",
    )
    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应创建 pane")),
    )

    result = herdr_client.start_agent("demo", "/tmp/project", "qoder")

    assert result == {"available": True, "error": "qoder 未安装或不在 PATH"}


def _managed_restart_snapshot(*, running=True, name="opencode-1", kind="opencode"):
    pane = {
        "pane_id": "w1:p5", "agent": kind if running else None,
        "cwd": "/tmp/project",
    }
    agents = ([{
        "pane_id": "w1:p5", "agent": kind, "name": name,
        "interactive_ready": True,
    }] if running else [])
    return {"panes": [pane], "agents": agents}


def _shell_process_info(pane_id="w1:p5", shell_pid=123):
    return "data: " + json.dumps({
        "result": {"type": "pane_process_info", "process_info": {
            "pane_id": pane_id,
            "shell_pid": shell_pid,
            "foreground_process_group_id": shell_pid,
            "foreground_processes": [{
                "pid": shell_pid, "name": "zsh", "argv": ["zsh"],
            }],
        }},
    })


def test_restart_pane_rebuilds_original_managed_identity_on_same_pane(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name="opencode-1", kind="opencode",
        args=["--model", "gpt 5", ";"], agent="opencode", workdir="/tmp/project",
    )
    snapshots = iter([
        _managed_restart_snapshot(),
        _managed_restart_snapshot(running=False),
    ])
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: next(snapshots),
    )
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "process-info" in args:
            return _shell_process_info()
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["restarted"] is True
    assert result["preserved"] is True
    assert result["name"] == "opencode-1"
    assert result["kind"] == "opencode"
    assert result["agent"] == "opencode"
    assert call(
        ["--session", "demo", "agent", "send-keys", "opencode-1", "esc"],
        timeout=3,
    ) in calls
    assert call(
        ["--session", "demo", "agent", "send-keys", "opencode-1", "ctrl+c"],
        timeout=3,
    ) in calls
    assert call(
        ["--session", "demo", "agent", "start", "opencode-1", "--kind", "opencode",
         "--pane", "w1:p5", "--timeout", "60000", "--", "--model", "gpt 5", ";"],
        timeout=65,
    ) in calls
    assert not any(c.args[0][2:4] == ["pane", "run"] for c in calls)
    assert not any("/quit" in c.args[0] for c in calls)
    assert not any("ctrl+u" in c.args[0] for c in calls)
    assert not any("close" in c.args[0] for c in calls)


def test_restart_pane_grok_empty_composer_clears_then_quits_and_preserves_identity(
    monkeypatch,
):
    instance_id = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name=instance_id, kind="grok",
        args=[], agent="grok", instance_id=instance_id, display_name="退出测试",
    )
    snapshots = iter([
        _managed_restart_snapshot(name=instance_id, kind="grok"),
        _managed_restart_snapshot(running=False, name=instance_id, kind="grok"),
    ])
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: next(snapshots),
    )
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return _shell_process_info() if "process-info" in args else ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["restarted"] is True
    assert result["instance_id"] == instance_id
    assert result["display_name"] == "退出测试"
    clear_call = call(
        ["--session", "demo", "pane", "send-keys", "w1:p5", "ctrl+u"],
        timeout=3,
    )
    quit_call = call(
        ["--session", "demo", "pane", "send-text", "w1:p5", "/quit"],
        timeout=3,
    )
    enter_call = call(
        ["--session", "demo", "pane", "send-keys", "w1:p5", "Enter"],
        timeout=3,
    )
    assert clear_call in calls
    assert quit_call in calls
    assert enter_call in calls
    assert calls.index(clear_call) < calls.index(quit_call) < calls.index(enter_call)
    assert not any(c.args[0][2:4] == ["agent", "send-keys"] for c in calls)
    assert not any("ctrl+c" in c.args[0] for c in calls)
    assert call(
        ["--session", "demo", "agent", "start", instance_id,
         "--kind", "grok", "--pane", "w1:p5", "--timeout", "60000"],
        timeout=65,
    ) in calls


def test_restart_pane_grok_dirty_composer_is_cleared_before_raw_quit(monkeypatch):
    instance_id = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name=instance_id, kind="grok",
        args=[], agent="grok", instance_id=instance_id, display_name="草稿测试",
    )
    snapshots = iter([
        _managed_restart_snapshot(name=instance_id, kind="grok"),
        _managed_restart_snapshot(running=False, name=instance_id, kind="grok"),
    ])
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: next(snapshots),
    )
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return _shell_process_info() if "process-info" in args else ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["restarted"] is True
    exit_calls = [
        call(["--session", "demo", "pane", "send-keys", "w1:p5", "ctrl+u"], timeout=3),
        call(["--session", "demo", "pane", "send-text", "w1:p5", "/quit"], timeout=3),
        call(["--session", "demo", "pane", "send-keys", "w1:p5", "Enter"], timeout=3),
    ]
    assert [item for item in calls if item in exit_calls] == exit_calls
    assert not any(c.args[0][2:4] == ["agent", "send-keys"] for c in calls)
    assert not any("ctrl+c" in c.args[0] for c in calls)


def test_restart_pane_grok_quit_timeout_never_falls_back_to_interrupt(monkeypatch):
    instance_id = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name=instance_id, kind="grok",
        args=[], agent="grok", instance_id=instance_id, display_name="退出测试",
    )
    monkeypatch.setattr(
        herdr_client, "_snapshot_session",
        lambda session: _managed_restart_snapshot(name=instance_id, kind="grok"),
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(herdr_client, "RESTART_SHELL_TIMEOUT_S", 0.5)
    monkeypatch.setattr(herdr_client.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        herdr_client.time, "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    calls = []
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: calls.append(call(args, timeout=timeout)) or "",
    )

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["error_code"] == "restart_shell_not_ready"
    assert result["preserved"] is True
    assert call(
        ["--session", "demo", "pane", "send-keys", "w1:p5", "ctrl+u"],
        timeout=3,
    ) in calls
    assert call(
        ["--session", "demo", "pane", "send-text", "w1:p5", "/quit"],
        timeout=3,
    ) in calls
    assert call(
        ["--session", "demo", "pane", "send-keys", "w1:p5", "Enter"],
        timeout=3,
    ) in calls
    assert not any(c.args[0][2:4] == ["agent", "send-keys"] for c in calls)
    assert not any("start" in c.args[0] for c in calls)


def test_restart_pane_preserves_opaque_instance_id(monkeypatch):
    instance_id = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name=instance_id, kind="opencode",
        args=[], agent="opencode", instance_id=instance_id, display_name="夜班",
    )
    snapshots = iter([
        _managed_restart_snapshot(name=instance_id),
        _managed_restart_snapshot(running=False, name=instance_id),
    ])
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: next(snapshots),
    )
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return _shell_process_info() if "process-info" in args else ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["restarted"] is True
    assert result["instance_id"] == instance_id
    assert result["display_name"] == "夜班"
    assert call(
        ["--session", "demo", "agent", "start", instance_id,
         "--kind", "opencode", "--pane", "w1:p5", "--timeout", "60000"],
        timeout=65,
    ) in calls


def test_restart_pane_rejects_unknown_pane_before_sending_keys(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: {"panes": []})
    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pane 未确认前不应发送按键")
        ),
    )

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["error_code"] == "restart_pane_not_found"
    assert result["preserved"] is True


def test_restart_pane_rejects_missing_descriptor_before_sending_keys(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: _managed_restart_snapshot(),
    )
    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("agent 未确认前不应发送按键")
        ),
    )

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["error_code"] == "restart_identity_missing"
    assert result["preserved"] is True


def test_restart_pane_rejects_descriptor_live_identity_mismatch(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name="opencode-1", kind="opencode", args=[],
    )
    monkeypatch.setattr(
        herdr_client, "_snapshot_session",
        lambda session: _managed_restart_snapshot(name="other-agent"),
    )
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应发送按键")),
    )

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["error_code"] == "restart_identity_mismatch"
    assert result["preserved"] is True


def test_restart_pane_accepts_unnamed_live_claude_on_same_pane(monkeypatch):
    instance_id = "i-kww5gslmiqhdzbgoh7m5ca5bu4"
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p6", name=instance_id, kind="claude",
        args=[], agent="claude", instance_id=instance_id, display_name="GrayFalcon",
    )
    snapshots = iter([
        {
            "panes": [{"pane_id": "w1:p6", "agent": "claude", "cwd": "/repo"}],
            "agents": [{"pane_id": "w1:p6", "agent": "claude"}],
        },
        {
            "panes": [{"pane_id": "w1:p6", "agent": None, "cwd": "/repo"}],
            "agents": [],
        },
    ])
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: next(snapshots),
    )
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return _shell_process_info("w1:p6") if "process-info" in args else ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.restart_pane("demo", "w1:p6")

    assert result["restarted"] is True
    assert result["name"] == instance_id
    assert result["kind"] == "claude"
    assert call(
        ["--session", "demo", "pane", "send-keys", "w1:p6", "esc"],
        timeout=3,
    ) in calls
    assert call(
        ["--session", "demo", "pane", "send-keys", "w1:p6", "ctrl+c"],
        timeout=3,
    ) in calls
    assert not any(c.args[0][2:4] == ["agent", "send-keys"] for c in calls)
    assert call(
        ["--session", "demo", "agent", "start", instance_id,
         "--kind", "claude", "--pane", "w1:p6", "--timeout", "60000"],
        timeout=65,
    ) in calls


def test_restart_pane_rejects_unknown_live_kind_without_raising(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name="opencode-1", kind="opencode", args=[],
    )
    snap = _managed_restart_snapshot()
    snap["agents"][0]["agent"] = "future-agent"
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: snap)

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["error_code"] == "restart_identity_mismatch"
    assert result["preserved"] is True


def test_restart_pane_rejects_unknown_requested_agent_without_raising(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name="opencode-1", kind="opencode", args=[],
    )
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: _managed_restart_snapshot(),
    )

    result = herdr_client.restart_pane("demo", "w1:p5", agent="future-agent")

    assert result["error_code"] == "restart_identity_invalid"
    assert result["preserved"] is True


def test_restart_pane_capability_failure_does_not_mutate(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "require_herdr_capabilities",
        lambda: (_ for _ in ()).throw(herdr_client.HerdrCapabilityError("upgrade")),
    )
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应修改 pane")),
    )

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["error_code"] == "herdr_upgrade_required"
    assert result["preserved"] is True


def test_restart_pane_times_out_if_agent_never_returns_to_shell(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name="opencode-1", kind="opencode", args=[],
    )
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: _managed_restart_snapshot(),
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(herdr_client.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        herdr_client.time, "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    calls = []
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: calls.append(call(args, timeout=timeout)) or "",
    )

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["error_code"] == "restart_shell_not_ready"
    assert result["preserved"] is True
    assert not any("start" in c.args[0] for c in calls)
    assert not any("close" in c.args[0] for c in calls)


def test_restart_pane_surfaces_shell_probe_failure(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name="opencode-1", kind="opencode", args=[],
    )
    snapshots = iter([
        _managed_restart_snapshot(),
        _managed_restart_snapshot(running=False),
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))

    def fake_run(args, timeout=10):
        if "process-info" in args:
            raise RuntimeError("process probe failed")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["error_code"] == "restart_shell_probe_failed"
    assert result["preserved"] is True


def test_restart_pane_rejects_shell_with_foreground_command(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name="opencode-1", kind="opencode", args=[],
    )
    snapshots = iter([
        _managed_restart_snapshot(),
        *[_managed_restart_snapshot(running=False) for _ in range(4)],
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    clock = {"now": 0.0}
    monkeypatch.setattr(herdr_client, "RESTART_SHELL_TIMEOUT_S", 0.5)
    monkeypatch.setattr(herdr_client.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        herdr_client.time, "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def fake_run(args, timeout=10):
        if "process-info" in args:
            data = json.loads(_shell_process_info()[len("data: "):])
            info = data["result"]["process_info"]
            info["foreground_process_group_id"] = 456
            info["foreground_processes"] = [{"pid": 456, "name": "pytest"}]
            return "data: " + json.dumps(data)
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["error_code"] == "restart_shell_not_ready"
    assert result["preserved"] is True


def test_restart_pane_preserves_pane_when_native_start_fails(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name="opencode-1", kind="opencode", args=[],
    )
    snapshots = iter([
        _managed_restart_snapshot(),
        _managed_restart_snapshot(running=False),
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "process-info" in args:
            return _shell_process_info()
        if "start" in args:
            raise RuntimeError("agent start failed: unknown kind")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["error_code"] == "restart_start_failed"
    assert result["preserved"] is True
    assert not any("close" in c.args[0] for c in calls)


def test_restart_pane_retries_native_start_when_shell_temporarily_busy(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name="opencode-1", kind="opencode", args=[],
    )
    snapshots = iter([
        _managed_restart_snapshot(),
        _managed_restart_snapshot(running=False),
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []
    busy = {"left": 2}

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "process-info" in args:
            return _shell_process_info()
        if "start" in args and busy["left"]:
            busy["left"] -= 1
            raise RuntimeError('{"error":{"code":"agent_pane_busy"}}')
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["restarted"] is True
    assert len([c for c in calls if "start" in c.args[0]]) == 3


def test_restart_pane_rejects_resume_for_non_codex_before_mutation(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name="opencode-1", kind="opencode", args=[],
    )
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: _managed_restart_snapshot(),
    )
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应修改 pane")),
    )

    result = herdr_client.restart_pane("demo", "w1:p5", resume=True)

    assert result["error_code"] == "restart_resume_unsupported"
    assert result["preserved"] is True


def test_restart_pane_codex_resume_keeps_original_args(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name="codex-1", kind="codex",
        args=["--model", "gpt-5"], agent="codex",
    )
    snapshots = iter([
        _managed_restart_snapshot(name="codex-1", kind="codex"),
        _managed_restart_snapshot(running=False, name="codex-1", kind="codex"),
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return _shell_process_info() if "process-info" in args else ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.restart_pane("demo", "w1:p5", resume=True)

    assert result["restarted"] is True
    assert result["args"] == ["--model", "gpt-5", "resume", "--last"]
    assert call(
        ["--session", "demo", "agent", "start", "codex-1", "--kind", "codex",
         "--pane", "w1:p5", "--timeout", "60000", "--",
         "--model", "gpt-5", "resume", "--last"],
        timeout=65,
    ) in calls


def test_restart_workspace_codex_rederives_trust_without_persisting_or_exposing_it(
    monkeypatch,
):
    instance_id = "i-" + "a" * 26
    workdir = "/tmp/project=trusted"
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name=instance_id, kind="codex",
        args=[], agent="codex", workdir=workdir, instance_id=instance_id,
        display_name="codex", project_id="prj_" + "a" * 32,
        workspace_id="ws_" + "b" * 32,
    )
    snapshots = iter([
        _managed_restart_snapshot(name=instance_id, kind="codex"),
        _managed_restart_snapshot(running=False, name=instance_id, kind="codex"),
    ])
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: next(snapshots),
    )
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return _shell_process_info() if "process-info" in args else ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.restart_pane("demo", "w1:p5")

    trust_args = herdr_client._workspace_codex_trust_args(workdir)
    assert result["restarted"] is True
    assert result["args"] == []
    assert workdir not in repr(result)
    assert "trust_level" not in repr(result)
    assert call(
        ["--session", "demo", "agent", "start", instance_id,
         "--kind", "codex", "--pane", "w1:p5", "--timeout", "60000",
         "--", *trust_args],
        timeout=65,
    ) in calls
    descriptor = herdr_client.get_launch_descriptor_by_instance(instance_id)
    assert descriptor is not None
    assert descriptor["args"] == []


def test_restart_managed_codex_rederives_trust_without_workspace_authority(
    monkeypatch,
):
    instance_id = "i-" + "a" * 26
    workdir = "/tmp/new-project"
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p5", name=instance_id, kind="codex",
        args=[], agent="codex", workdir=workdir, instance_id=instance_id,
        display_name="codex",
    )
    snapshots = iter([
        _managed_restart_snapshot(name=instance_id, kind="codex"),
        _managed_restart_snapshot(running=False, name=instance_id, kind="codex"),
    ])
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: next(snapshots),
    )
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return _shell_process_info() if "process-info" in args else ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.restart_pane("demo", "w1:p5")

    trust_args = herdr_client._workspace_codex_trust_args(workdir)
    assert result["restarted"] is True
    assert result["args"] == []
    assert workdir not in repr(result)
    assert "trust_level" not in repr(result)
    assert call(
        ["--session", "demo", "agent", "start", instance_id,
         "--kind", "codex", "--pane", "w1:p5", "--timeout", "60000",
         "--", *trust_args],
        timeout=65,
    ) in calls
    descriptor = herdr_client.get_launch_descriptor_by_instance(instance_id)
    assert descriptor is not None
    assert descriptor["args"] == []


def test_restart_pane_rejects_concurrent_request(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    key = ("demo", "w1:p5")
    with herdr_client._RESTART_GUARD:
        herdr_client._RESTARTING_PANES.add(key)
    try:
        result = herdr_client.restart_pane("demo", "w1:p5")
    finally:
        with herdr_client._RESTART_GUARD:
            herdr_client._RESTARTING_PANES.discard(key)

    assert result["error_code"] == "restart_in_progress"
    assert result["preserved"] is True


def _layout_json(panes, zoomed=False, focused="w1:p2"):
    import json as _json
    return "data: " + _json.dumps({
        "result": {
            "type": "pane_layout",
            "layout": {
                "workspace_id": "w1", "tab_id": "w1:t1",
                "zoomed": zoomed, "focused_pane_id": focused,
                "area": {"x": 0, "y": 0, "width": 240, "height": 50},
                "panes": panes,
                "splits": [],
            },
        },
    })


def test_pane_layout_detects_horizontal_split(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    calls = []
    panes = [
        {"pane_id": f"w1:p{i}", "focused": i == 2,
         "rect": {"x": x, "y": 0, "width": 80, "height": 50}}
        for i, x in ((1, 0), (2, 80), (3, 160))
    ]
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: calls.append(args) or _layout_json(panes),
    )

    result = herdr_client.pane_layout("demo", "w1:p2")

    assert result["available"] is True
    assert result["horizontal_split"] is True
    assert result["zoomed"] is False
    assert result["focused_pane_id"] == "w1:p2"
    assert [p["pane_id"] for p in result["panes"]] == ["w1:p1", "w1:p2", "w1:p3"]
    assert calls == [["--session", "demo", "pane", "layout", "--pane", "w1:p2"]]


def test_pane_layout_vertical_split_is_not_horizontal(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    panes = [
        {"pane_id": "w1:p1", "focused": True,
         "rect": {"x": 0, "y": 0, "width": 240, "height": 25}},
        {"pane_id": "w1:p2", "focused": False,
         "rect": {"x": 0, "y": 25, "width": 240, "height": 25}},
    ]
    monkeypatch.setattr(
        herdr_client, "_run", lambda args, timeout=10: _layout_json(panes)
    )

    result = herdr_client.pane_layout("demo")

    assert result["horizontal_split"] is False
    # pane_id 省略时不带 --pane(查 UI 焦点 pane 所在 tab)
    assert result["tab_id"] == "w1:t1"


def test_pane_layout_parses_plain_json_without_sse_prefix(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: _layout_json([], zoomed=True)[len("data: "):],
    )

    result = herdr_client.pane_layout("demo")

    assert result["available"] is True
    assert result["zoomed"] is True
    assert result["panes"] == []
    assert result["horizontal_split"] is False


def test_pane_layout_degrades_on_error_and_unavailable(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert herdr_client.pane_layout("demo")["error"] == "boom"

    monkeypatch.setattr(herdr_client, "is_available", lambda: False)
    assert herdr_client.pane_layout("demo") == {"available": False}


def test_pane_zoom_on_is_idempotent_and_maps_result(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    import json as _json
    calls = []

    def fake_run(args, timeout=10):
        calls.append(args)
        return "data: " + _json.dumps({
            "result": {
                "type": "pane_zoom",
                "zoom": {
                    "changed": False, "zoom_changed": False, "focus_changed": False,
                    "pane_id": "w1:p2", "focused_pane_id": "w1:p2",
                    "zoomed": True, "reason": "already_zoomed", "layout": {},
                },
            },
        })

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.pane_zoom("demo", "w1:p2", mode="on")

    assert result == {
        "available": True, "session": "demo", "pane_id": "w1:p2",
        "zoomed": True, "changed": False,
        "reason": "already_zoomed", "focused_pane_id": "w1:p2",
        "tab_id": None, "horizontal_split": False,
    }
    assert calls == [["--session", "demo", "pane", "zoom", "w1:p2", "--on"]]


def test_pane_zoom_defaults_to_on_without_pane_targeting_ui_focus(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    import json as _json
    calls = []

    def fake_run(args, timeout=10):
        calls.append(args)
        return _json.dumps({"result": {"zoomed": True, "zoom_changed": True,
                                       "pane_id": "w1:p1",
                                       "focused_pane_id": "w1:p1",
                                       "layout": {
                                           "tab_id": "w1:t1", "workspace_id": "w1",
                                           "zoomed": True, "focused_pane_id": "w1:p1",
                                           "area": {"x": 0, "y": 0, "width": 240, "height": 50},
                                           "panes": [
                                               {"pane_id": "w1:p1", "focused": True,
                                                "rect": {"x": 0, "y": 0, "width": 120, "height": 50}},
                                               {"pane_id": "w1:p2", "focused": False,
                                                "rect": {"x": 120, "y": 0, "width": 120, "height": 50}},
                                           ],
                                           "splits": [],
                                       }}})

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.pane_zoom("demo")

    assert result["zoomed"] is True
    assert result["changed"] is True
    assert result["reason"] is None
    assert result["tab_id"] == "w1:t1"
    assert result["horizontal_split"] is True
    assert calls == [["--session", "demo", "pane", "zoom", "--on"]]


def test_pane_zoom_rejects_toggle_and_invalid_mode_before_running(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应执行")),
    )

    # 共享状态下 toggle 语义会漂移,显式拒绝
    assert herdr_client.pane_zoom("demo", mode="toggle") == {
        "available": True, "error": "非法 zoom mode(仅支持 on/off): toggle",
    }
    assert herdr_client.pane_zoom("demo", mode="yes") == {
        "available": True, "error": "非法 zoom mode(仅支持 on/off): yes",
    }


def test_snapshot_session_exposes_slim_layouts(monkeypatch):
    """snapshot 必须暴露 layouts.zoomed/几何,供 server sidecar 判断共享 zoom 状态。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    import json as _json
    payload = {
        "result": {
            "snapshot": {
                "panes": [{"pane_id": "w1:p2", "cwd": "/tmp/p"}],
                "agents": [],
                "focused_pane_id": "w1:p2",
                "layouts": [{
                    "workspace_id": "w1", "tab_id": "w1:t1",
                    "zoomed": True, "focused_pane_id": "w1:p2",
                    "area": {"x": 0, "y": 0, "width": 240, "height": 50},
                    "panes": [
                        {"pane_id": "w1:p1", "focused": False,
                         "rect": {"x": 0, "y": 0, "width": 120, "height": 50}},
                        {"pane_id": "w1:p2", "focused": True,
                         "rect": {"x": 120, "y": 0, "width": 120, "height": 50}},
                    ],
                    "splits": [],
                }],
            },
        },
    }
    monkeypatch.setattr(
        herdr_client, "_run", lambda args, timeout=10: "data: " + _json.dumps(payload)
    )

    result = herdr_client._snapshot_session("demo")

    assert len(result["layouts"]) == 1
    layout = result["layouts"][0]
    assert layout["zoomed"] is True
    assert layout["tab_id"] == "w1:t1"
    assert layout["focused_pane_id"] == "w1:p2"
    assert layout["horizontal_split"] is True
    assert layout["panes"][1]["x"] == 120


def test_pane_zoom_degrades_on_error_and_unavailable(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert herdr_client.pane_zoom("demo", "w1:p2", mode="off")["error"] == "boom"

    monkeypatch.setattr(herdr_client, "is_available", lambda: False)
    assert herdr_client.pane_zoom("demo") == {"available": False}


def _fake_herdr(monkeypatch, panes):
    monkeypatch.setattr(
        herdr_client, "_snapshot_session",
        lambda session: {"panes": list(panes)},
    )


def _move_result(*, changed=True, reason=None, pane=None):
    move = {"changed": changed}
    if reason:
        move["reason"] = reason
    if pane:
        move["pane"] = pane
    return 'data: {"result":{"move_result":%s}}' % __import__("json").dumps(move)


def test_split_pane_once_prefers_reported_new_pane(monkeypatch):
    calls = []

    def fake_run(args, timeout=10):
        calls.append(list(args))
        return 'data: {"result":{"pane":{"pane_id":"w1:p2"}}}'

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    _fake_herdr(monkeypatch, [{"pane_id": "w1:p1"}])

    assert herdr_client._split_pane_once("demo", "w1:p1", "right") == "w1:p2"
    assert calls[0][2:6] == ["pane", "split", "w1:p1", "--direction"]
    assert calls[0][6] == "right"


def test_split_pane_layout_modes(monkeypatch):
    calls = []
    reported = iter(["w1:p2", "w1:p3", "w1:p4"])

    def fake_run(args, timeout=10):
        calls.append(list(args))
        return 'data: {"result":{"pane":{"pane_id":"%s"}}}' % next(reported)

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    _fake_herdr(monkeypatch, [{"pane_id": "w1:p1"}])

    assert herdr_client.split_pane_layout("demo", "w1:p1", "horizontal") == ["w1:p2"]

    calls.clear()
    reported = iter(["w1:p2", "w1:p3", "w1:p4"])
    assert herdr_client.split_pane_layout("demo", "w1:p1", "vertical") == ["w1:p2"]
    assert calls[0][6] == "down"

    calls.clear()
    reported = iter(["w1:p2", "w1:p3", "w1:p4"])
    assert herdr_client.split_pane_layout("demo", "w1:p1", "grid4") == [
        "w1:p2", "w1:p3", "w1:p4",
    ]
    splits = [c[6] for c in calls]
    assert splits == ["right", "down", "down"]
    # 第三刀必须切在第一刀产生的右栏上,才能形成 2×2。
    assert calls[2][4] == "w1:p2"

    import pytest as _pt
    with _pt.raises(ValueError):
        herdr_client.split_pane_layout("demo", "w1:p1", "diagonal")


def test_detach_pane_moves_to_new_tab(monkeypatch):
    calls = []
    _fake_herdr(monkeypatch, [
        {"pane_id": "w1:p1", "tab_id": "w1:t2"},
        {"pane_id": "w1:p3", "tab_id": "w1:t2"},
    ])
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: calls.append(list(args)) or _move_result(),
    )

    herdr_client.detach_pane("demo", "w1:p3")

    assert calls[0][2:] == ["pane", "move", "w1:p3", "--new-tab"]


def test_detach_pane_restores_display_name_and_tracks_changed_pane_id(monkeypatch):
    instance_id = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p3", name=instance_id, kind="opencode",
        args=[], agent="opencode", instance_id=instance_id, display_name="夜班负责人",
    )
    _fake_herdr(monkeypatch, [
        {"pane_id": "w1:p1", "tab_id": "w1:t2"},
        {
            "pane_id": "w1:p3", "tab_id": "w1:t2", "workspace_id": "w1",
            "agent": "opencode", "label": instance_id,
        },
    ])
    calls = []

    def fake_run(args, timeout=10):
        calls.append(list(args))
        if args[2:4] == ["pane", "move"]:
            return _move_result(pane={
                "pane_id": "w1:p8", "tab_id": "w1:t8", "workspace_id": "w1",
            })
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    moved = herdr_client.detach_pane("demo", "w1:p3")

    assert moved == "w1:p8"
    assert ["--session", "demo", "pane", "rename", "w1:p8", "夜班负责人"] in calls
    assert ["--session", "demo", "tab", "rename", "w1:t8", "夜班负责人"] in calls
    descriptor = herdr_client.get_launch_descriptor("demo", "w1:p8")
    assert descriptor["instance_id"] == instance_id
    assert herdr_client.get_launch_descriptor("demo", "w1:p3") is None


def test_detach_pane_uses_snapshot_label_before_agent_kind(monkeypatch):
    _fake_herdr(monkeypatch, [
        {"pane_id": "w1:p1", "tab_id": "w1:t2"},
        {
            "pane_id": "w1:p3", "tab_id": "w1:t2", "workspace_id": "w1",
            "agent": "opencode", "label": "zcode-cockpit",
        },
    ])
    calls = []

    def fake_run(args, timeout=10):
        calls.append(list(args))
        if args[2:4] == ["pane", "move"]:
            return _move_result(pane={
                "pane_id": "w1:p3", "tab_id": "w1:t8", "workspace_id": "w1",
            })
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    herdr_client.detach_pane("demo", "w1:p3")

    assert ["--session", "demo", "pane", "rename", "w1:p3", "zcode-cockpit"] in calls


def test_detach_pane_reports_new_id_when_descriptor_migration_fails(monkeypatch):
    instance_id = "i-dddddddddddddddddddddddddd"
    herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p3", name=instance_id, kind="codex",
        args=[], agent="codex", instance_id=instance_id, display_name="负责人",
    )
    _fake_herdr(monkeypatch, [
        {"pane_id": "w1:p1", "tab_id": "w1:t2"},
        {
            "pane_id": "w1:p3", "tab_id": "w1:t2", "workspace_id": "w1",
            "agent": "codex",
        },
    ])
    calls = []

    def fake_run(args, timeout=10):
        calls.append(list(args))
        if args[2:4] == ["pane", "move"]:
            return _move_result(pane={
                "pane_id": "w1:p8", "tab_id": "w1:t8", "workspace_id": "w1",
            })
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    monkeypatch.setattr(
        herdr_client, "update_launch_descriptor_by_instance",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(RuntimeError, match="w1:p8.*descriptor.*disk full"):
        herdr_client.detach_pane("demo", "w1:p3")

    assert ["--session", "demo", "pane", "rename", "w1:p8", "负责人"] in calls


def test_untile_tab_moves_all_but_first(monkeypatch):
    panes = [
        {"pane_id": "w1:p1", "tab_id": "w1:t2"},
        {"pane_id": "w1:p2", "tab_id": "w1:t2"},
        {"pane_id": "w1:p3", "tab_id": "w1:t2"},
        {"pane_id": "w1:p9", "tab_id": "w1:t5"},
    ]
    _fake_herdr(monkeypatch, panes)
    calls = []
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: calls.append(list(args)) or _move_result(),
    )

    moved = herdr_client.untile_tab("demo", "w1:t2")

    assert moved == ["w1:p2", "w1:p3"]
    moves = [call for call in calls if call[2:4] == ["pane", "move"]]
    assert moves[0][2:] == ["pane", "move", "w1:p2", "--new-tab"]
    assert moves[1][2:] == ["pane", "move", "w1:p3", "--new-tab"]


def test_compose_pane_placement_order(monkeypatch):
    panes = [
        {"pane_id": pid, "tab_id": "w1:t1"}
        for pid in ("w1:p1", "w1:p2", "w1:p3", "w1:p4")
    ]
    _fake_herdr(monkeypatch, panes)

    def compose(orientation):
        calls = []
        monkeypatch.setattr(
            herdr_client, "_run",
            lambda args, timeout=10: calls.append(list(args)) or _move_result(),
        )
        herdr_client.compose_panes(
            "demo", ["w1:p1", "w1:p2", "w1:p3", "w1:p4"], orientation)
        return [tuple(c[2:]) for c in calls if c[2:4] == ["pane", "move"]]

    assert compose("horizontal") == [
        ("pane", "move", "w1:p2", "--tab", "w1:t1", "--target-pane", "w1:p1", "--split", "right", "--ratio", "0.25"),
        ("pane", "move", "w1:p3", "--tab", "w1:t1", "--target-pane", "w1:p2", "--split", "right", "--ratio", "0.333333"),
        ("pane", "move", "w1:p4", "--tab", "w1:t1", "--target-pane", "w1:p3", "--split", "right", "--ratio", "0.5"),
    ]
    assert compose("vertical") == [
        ("pane", "move", "w1:p2", "--tab", "w1:t1", "--target-pane", "w1:p1", "--split", "down", "--ratio", "0.25"),
        ("pane", "move", "w1:p3", "--tab", "w1:t1", "--target-pane", "w1:p2", "--split", "down", "--ratio", "0.333333"),
        ("pane", "move", "w1:p4", "--tab", "w1:t1", "--target-pane", "w1:p3", "--split", "down", "--ratio", "0.5"),
    ]


def test_compose_panes_tracks_moved_ids_and_restores_each_label(monkeypatch):
    second = "i-bbbbbbbbbbbbbbbbbbbbbbbbbb"
    third = "i-cccccccccccccccccccccccccc"
    panes = [
        {
            "pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1",
            "agent": "codex", "label": "负责人",
        },
        {
            "pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1",
            "agent": "codex", "label": second,
        },
        {
            "pane_id": "w1:p3", "tab_id": "w1:t3", "workspace_id": "w1",
            "agent": "opencode", "label": third,
        },
    ]
    for pane_id, instance_id, display_name, agent in (
        ("w1:p2", second, "同名", "codex"),
        ("w1:p3", third, "同名", "opencode"),
    ):
        herdr_client.save_launch_descriptor(
            session="demo", pane_id=pane_id, name=instance_id, kind=agent,
            args=[], agent=agent, instance_id=instance_id, display_name=display_name,
        )
    _fake_herdr(monkeypatch, panes)
    move_results = iter([
        _move_result(pane={
            "pane_id": "w1:p8", "tab_id": "w1:t1", "workspace_id": "w1",
        }),
        _move_result(pane={
            "pane_id": "w1:p9", "tab_id": "w1:t1", "workspace_id": "w1",
        }),
    ])
    calls = []

    def fake_run(args, timeout=10):
        calls.append(list(args))
        if args[2:4] == ["pane", "move"]:
            return next(move_results)
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    assert herdr_client.compose_panes(
        "demo", ["w1:p1", "w1:p2", "w1:p3"], "horizontal",
    ) == "w1:p1"

    moves = [call for call in calls if call[2:4] == ["pane", "move"]]
    assert "w1:p8" in moves[1]
    assert ["--session", "demo", "pane", "rename", "w1:p8", "同名"] in calls
    assert ["--session", "demo", "pane", "rename", "w1:p9", "同名"] in calls
    assert herdr_client.get_launch_descriptor("demo", "w1:p8")["instance_id"] == second
    assert herdr_client.get_launch_descriptor("demo", "w1:p9")["instance_id"] == third


def test_compose_panes_rejects_bad_input(monkeypatch):
    panes = [
        {"pane_id": pid, "tab_id": "w1:t1"} for pid in ("w1:p1", "w1:p2")
    ]
    _fake_herdr(monkeypatch, panes)
    monkeypatch.setattr(herdr_client, "_run", lambda *a, **k: "")

    import pytest as _pt
    with _pt.raises(ValueError):
        herdr_client.compose_panes("demo", ["w1:p1"], "horizontal")
    with _pt.raises(ValueError):
        herdr_client.compose_panes("demo", ["w1:p%d" % i for i in range(6)], "horizontal")
    with _pt.raises(ValueError):
        herdr_client.compose_panes("demo", ["w1:p1", "w1:p1"], "horizontal")
    with _pt.raises(ValueError):
        herdr_client.compose_panes("demo", ["w1:p1", "nope"], "horizontal")
    with _pt.raises(ValueError):
        herdr_client.compose_panes("demo", ["w1:p1", "w1:p2"], "diagonal")


def test_layout_changes_reject_zoomed_tab_before_mutation(monkeypatch):
    panes = [
        {"pane_id": "w1:p1", "tab_id": "w1:t1"},
        {"pane_id": "w1:p2", "tab_id": "w1:t1"},
    ]
    monkeypatch.setattr(
        herdr_client, "_snapshot_session",
        lambda session: {
            "panes": panes,
            "layouts": [{"tab_id": "w1:t1", "zoomed": True}],
        },
    )
    calls = []
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: calls.append(list(args)) or _move_result(),
    )

    import pytest as _pt
    with _pt.raises(ValueError, match="正在放大"):
        herdr_client.detach_pane("demo", "w1:p2")
    with _pt.raises(ValueError, match="正在放大"):
        herdr_client.compose_panes("demo", ["w1:p1", "w1:p2"], "horizontal")
    assert calls == []


def test_move_pane_rejects_herdr_noop(monkeypatch):
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: _move_result(changed=False, reason="zoomed_tab"),
    )

    import pytest as _pt
    with _pt.raises(RuntimeError, match="正在放大"):
        herdr_client._move_pane("demo", ["w1:p2", "--new-tab"])


# ── B1: snapshot 有界并行 + poll 指标/退避 ─────────────────────

def _mock_sessions(names):
    """构造 list_sessions 返回值(全 running)。"""
    return [{"name": n, "status": "running", "directory": f"/tmp/{n}", "socket": ""} for n in names]


def _wait_snapshot_pool_idle(timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with herdr_client._SNAPSHOT_EXECUTOR_LOCK:
            if not herdr_client._SNAPSHOT_FUTURES:
                return
        time.sleep(0.01)
    raise AssertionError("snapshot worker pool did not become idle")


def test_snapshot_parallelizes_sessions_and_preserves_order(monkeypatch):
    """多 session 并行执行 _snapshot_session,结果按 list_sessions 顺序回填。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "list_sessions", lambda: _mock_sessions(["s1", "s2", "s3"]))
    barrier = threading.Barrier(3)

    def fake_snapshot(name):
        # 3 个线程都到达 barrier 才放行 → 证明并行(串行会永远阻塞)
        barrier.wait(timeout=2)
        time.sleep(0.05)  # 模拟 fork 耗时
        return {"session": name, "panes": [{"pane_id": name + ":p1", "agent": "codex"}]}

    monkeypatch.setattr(herdr_client, "_snapshot_session", fake_snapshot)
    result = herdr_client.snapshot()
    # 顺序保持(list_sessions 的 s1/s2/s3)
    assert [s["session"] for s in result["sessions"]] == ["s1", "s2", "s3"]
    assert result["total_panes"] == 3
    assert result["agent_panes"] == 3
    # directory 正确回填
    assert result["sessions"][0]["directory"] == "/tmp/s1"


def test_snapshot_isolates_single_session_failure(monkeypatch):
    """单个 session 失败返回 error dict,不阻断其他 session。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "list_sessions", lambda: _mock_sessions(["ok", "bad", "ok2"]))

    def fake_snapshot(name):
        if name == "bad":
            return {"session": "bad", "error": "boom", "panes": []}
        return {"session": name, "panes": [{"pane_id": name + ":p1"}]}

    monkeypatch.setattr(herdr_client, "_snapshot_session", fake_snapshot)
    result = herdr_client.snapshot()
    sessions = {s["session"]: s for s in result["sessions"]}
    assert sessions["bad"]["error"] == "boom"
    assert sessions["ok"]["panes"]  # 其他正常
    assert sessions["ok2"]["panes"]
    assert result["total_panes"] == 2  # bad 的空 panes 不计入


def test_snapshot_single_session_skips_thread_pool(monkeypatch):
    """N=1 时不走线程池(barrier 不会卡住)。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "list_sessions", lambda: _mock_sessions(["only"]))
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda name: {"session": name, "panes": []})
    result = herdr_client.snapshot()
    assert len(result["sessions"]) == 1


def test_snapshot_no_running_sessions_returns_empty(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "list_sessions", lambda: _mock_sessions([]))
    result = herdr_client.snapshot()
    assert result["sessions"] == []
    assert result["total_panes"] == 0


def test_snapshot_excludes_canary_sessions_before_worker_submission(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "list_sessions", lambda: _mock_sessions(["target", "other"])
    )
    called = []
    monkeypatch.setattr(
        herdr_client,
        "_snapshot_session",
        lambda name: called.append(name) or {"session": name, "panes": []},
    )

    result = herdr_client.snapshot(exclude_sessions={"target"})

    assert called == ["other"]
    assert [item["session"] for item in result["sessions"]] == ["other"]


def test_snapshot_worker_cap_is_min_4_n(monkeypatch):
    """并发 worker 峰值不超过 min(4, N)。6 个 session 时峰值=4。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "list_sessions", lambda: _mock_sessions([f"s{i}" for i in range(6)]))
    peak = {"current": 0, "max": 0}
    lock = threading.Lock()

    def fake_snapshot(name):
        with lock:
            peak["current"] += 1
            peak["max"] = max(peak["max"], peak["current"])
        time.sleep(0.08)
        with lock:
            peak["current"] -= 1
        return {"session": name, "panes": []}

    monkeypatch.setattr(herdr_client, "_snapshot_session", fake_snapshot)
    herdr_client.snapshot()
    assert peak["max"] <= 4, f"worker 峰值 {peak['max']} 超过 min(4,N)=4"


def test_snapshot_session_safe_catches_crash_and_preserves_order(monkeypatch):
    """_snapshot_session_safe 兜底:即使 _snapshot_session 抛非 RuntimeError(如 KeyError),
    也返回保持 session/panes 空的 error dict,且并行结果顺序不变。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "list_sessions", lambda: _mock_sessions(["s1", "crash", "s3"]))

    def crashing_snapshot(name):
        if name == "crash":
            raise KeyError("simulated parse bug")
        return {"session": name, "panes": [{"pane_id": name + ":p1"}]}

    monkeypatch.setattr(herdr_client, "_snapshot_session", crashing_snapshot)
    result = herdr_client.snapshot()
    sessions = result["sessions"]
    # 顺序保持: s1, crash, s3
    assert [s["session"] for s in sessions] == ["s1", "crash", "s3"]
    # crash 的结构正确(error + 空 panes)
    crash_session = sessions[1]
    assert "error" in crash_session
    assert crash_session["panes"] == []
    # 前后 session 正常
    assert sessions[0]["panes"]
    assert sessions[2]["panes"]
    assert result["total_panes"] == 2


def test_snapshot_total_deadline_limits_later_worker_waves(monkeypatch):
    _wait_snapshot_pool_idle()
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client,
        "list_sessions",
        lambda: _mock_sessions([f"s{i}" for i in range(8)]),
    )
    monkeypatch.setattr(herdr_client, "SNAPSHOT_TOTAL_TIMEOUT_S", 0.5)
    budgets: list[float] = []
    lock = threading.Lock()

    def budgeted_snapshot(name):
        budget = herdr_client._snapshot_timeout()
        with lock:
            budgets.append(budget)
        if budget < 0.2:
            time.sleep(budget)
            return {"session": name, "error": "snapshot total timeout", "panes": []}
        time.sleep(0.3)
        return {"session": name, "panes": [{"pane_id": name + ":p1"}]}

    monkeypatch.setattr(herdr_client, "_snapshot_session", budgeted_snapshot)
    started = time.monotonic()
    result = herdr_client.snapshot()
    elapsed = time.monotonic() - started

    assert elapsed < 0.8
    assert [s["session"] for s in result["sessions"]] == [f"s{i}" for i in range(8)]
    assert min(budgets) < max(budgets) - 0.15
    assert any("timeout" in s.get("error", "") for s in result["sessions"][4:])


def test_snapshot_total_deadline_includes_session_listing(monkeypatch):
    _wait_snapshot_pool_idle()
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "SNAPSHOT_TOTAL_TIMEOUT_S", 0.5)

    def slow_list():
        time.sleep(0.15)
        return _mock_sessions(["one"])

    budgets = []
    monkeypatch.setattr(herdr_client, "list_sessions", slow_list)
    monkeypatch.setattr(
        herdr_client, "_snapshot_session",
        lambda name: budgets.append(herdr_client._snapshot_timeout()) or {
            "session": name, "panes": [],
        },
    )

    herdr_client.snapshot()
    assert budgets and budgets[0] < 0.4


def test_snapshot_returns_without_waiting_for_uncooperative_worker(monkeypatch):
    _wait_snapshot_pool_idle()
    release = threading.Event()
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "list_sessions", lambda: _mock_sessions(["one", "two"]),
    )
    monkeypatch.setattr(herdr_client, "SNAPSHOT_TOTAL_TIMEOUT_S", 0.2)
    monkeypatch.setattr(
        herdr_client, "_snapshot_session",
        lambda name: release.wait(2) or {"session": name, "panes": []},
    )

    started = time.monotonic()
    try:
        result = herdr_client.snapshot()
        assert time.monotonic() - started < 0.6
        assert all("timeout" in row.get("error", "") for row in result["sessions"])
    finally:
        release.set()
        _wait_snapshot_pool_idle()


def test_single_snapshot_returns_without_waiting_for_uncooperative_worker(monkeypatch):
    _wait_snapshot_pool_idle()
    release = threading.Event()
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "list_sessions", lambda: _mock_sessions(["one"]),
    )
    monkeypatch.setattr(herdr_client, "SNAPSHOT_TOTAL_TIMEOUT_S", 0.2)
    monkeypatch.setattr(
        herdr_client, "_snapshot_session",
        lambda name: release.wait(2) or {"session": name, "panes": []},
    )

    started = time.monotonic()
    try:
        result = herdr_client.snapshot()
        assert time.monotonic() - started < 0.6
        assert "timeout" in result["sessions"][0]["error"]
    finally:
        release.set()
        _wait_snapshot_pool_idle()


def test_repeated_snapshot_timeouts_keep_worker_threads_bounded(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "list_sessions",
        lambda: _mock_sessions(["one", "two", "three", "four"]),
    )
    monkeypatch.setattr(herdr_client, "SNAPSHOT_TOTAL_TIMEOUT_S", 0.02)
    monkeypatch.setattr(
        herdr_client, "_snapshot_session",
        lambda name: release.wait(1) or {"session": name, "panes": []},
    )

    try:
        for _ in range(3):
            herdr_client.snapshot()
        workers = [
            thread for thread in threading.enumerate()
            if thread.name.startswith("cockpit-snapshot")
        ]
        assert len(workers) <= 4
    finally:
        release.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and any(
            thread.name.startswith("cockpit-snapshot")
            for thread in threading.enumerate()
        ):
            time.sleep(0.01)


def test_snapshot_reports_session_list_failure(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_run", lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("socket down")
        ),
    )

    result = herdr_client.snapshot()

    assert result["available"] is False
    assert result["error"] == "session list failed"


# ================= managed Codex 私有 CODEX_HOME 接缝 =================

_CODEXHOME_PROJECT = "prj_" + "a" * 32
_CODEXHOME_WORKSPACE = "ws_" + "b" * 32
_CODEXHOME_INSTANCE = "i-" + "a" * 26
_CODEXHOME_SESSION = "codexhome-unit"
_CODEXHOME_WORKDIR = "/repo/shared"


def _codexhome_dirs(tmp_path):
    """私有 0700 CODEX_HOME + 隔离 launch descriptor 存根。"""
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    return home


def _codexhome_harness(
    monkeypatch, tmp_path, *, snapshots: list[dict] | None = None,
    run_error_at_pane_run: bool = False,
):
    """复刻 managed start 所需的最小 herdr mock（不触发真实 CLI）。"""
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH",
        str(tmp_path / "launch.json"),
    )
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "require_herdr_capabilities", lambda: {},
    )
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: "/bin/sh")
    monkeypatch.setattr(herdr_client, "time", herdr_client.time)

    pending: list[dict] = list(snapshots or [])
    polls: list[dict] = []

    def snapshot(session):
        if pending:
            return pending.pop(0)
        return polls[-1] if polls else {
            "session": session, "panes": [], "agents": [], "tabs": [],
            "workspaces": [{"workspace_id": "w1", "focused": True}],
            "focused_workspace_id": "w1",
        }

    calls: list[list[str]] = []

    def run(args, timeout=10):
        calls.append(list(args))
        if run_error_at_pane_run and args[2:4] == ["pane", "run"]:
            raise RuntimeError("pane run failed")
        if args[2:4] == ["workspace", "create"]:
            return json.dumps({
                "result": {
                    "type": "workspace_created",
                    "workspace": {
                        "workspace_id": "w1", "active_tab_id": "w1:t1",
                    },
                    "tab": {"tab_id": "w1:t1", "workspace_id": "w1"},
                    "root_pane": {
                        "pane_id": "w1:p1", "tab_id": "w1:t1",
                        "workspace_id": "w1", "cwd": _CODEXHOME_WORKDIR,
                    },
                },
            })
        if args[2:4] == ["tab", "create"]:
            return json.dumps({
                "result": {
                    "tab": {"tab_id": "w1:t2", "workspace_id": "w1"},
                    "root_pane": {
                        "pane_id": "w1:p2", "tab_id": "w1:t2",
                        "workspace_id": "w1", "cwd": _CODEXHOME_WORKDIR,
                    },
                },
            })
        return ""

    monkeypatch.setattr(herdr_client, "_snapshot_session", snapshot)
    monkeypatch.setattr(herdr_client, "_run", run)

    def detect_later(panes_spec):
        polls.extend(panes_spec)

    return calls, detect_later


def _codexhome_start_kwargs(home: str, **extra):
    payload = dict(
        session=_CODEXHOME_SESSION, workdir=_CODEXHOME_WORKDIR,
        instance_id=_CODEXHOME_INSTANCE, project_id=_CODEXHOME_PROJECT,
        workspace_id=_CODEXHOME_WORKSPACE, codex_home=home,
        label="codex",
    )
    payload.update(extra)
    return payload


@pytest.mark.parametrize(
    "bad",
    [
        "relative/codex-home",
        "/tmp/x/../codex",
        "/tmp/codex-home/",
        "/tmp/has space",
        "/tmp/quote'home",
        "/tmp/semi;home",
        "/tmp/dollar$home",
        "/tmp/emoji-中文",
    ],
)
def test_codex_home_rejects_unsafe_paths(monkeypatch, tmp_path, bad) -> None:
    home = _codexhome_dirs(tmp_path)
    del home
    with pytest.raises(ValueError):
        herdr_client._validate_workspace_codex_home(bad)


def test_codex_home_rejects_mode_symlink_and_default_home(
    monkeypatch, tmp_path,
) -> None:
    too_open = tmp_path / "open-home"
    too_open.mkdir()
    too_open.chmod(0o755)
    with pytest.raises(ValueError):
        herdr_client._validate_workspace_codex_home(str(too_open))

    leaf_link = tmp_path / "link-home"
    real_dir = tmp_path / "real-home"
    real_dir.mkdir(mode=0o700)
    leaf_link.symlink_to(real_dir)
    with pytest.raises(ValueError):
        herdr_client._validate_workspace_codex_home(str(leaf_link))

    ancestor_link = tmp_path / "linked"
    ancestor_link.symlink_to(real_dir)
    nested = ancestor_link / "inner"
    nested.mkdir(mode=0o700)
    with pytest.raises(ValueError):
        herdr_client._validate_workspace_codex_home(str(nested))

    monkeypatch.delenv("CODEX_HOME", raising=False)
    default_home = Path.home() / ".codex"
    with pytest.raises(ValueError):
        herdr_client._validate_workspace_codex_home(str(default_home))
    monkeypatch.setenv("CODEX_HOME", str(real_dir))
    with pytest.raises(ValueError):
        herdr_client._validate_workspace_codex_home(str(real_dir))


def test_codex_home_valid_private_dir_passes(tmp_path) -> None:
    home = _codexhome_dirs(tmp_path)
    assert herdr_client._validate_workspace_codex_home(str(home)) == str(home)


def test_codex_home_entry_is_managed_codex_only(tmp_path) -> None:
    home = _codexhome_dirs(tmp_path)
    # start_agent 的 seam 签名被 pin：不得出现 codex_home 参数
    with pytest.raises(TypeError):
        herdr_client.start_agent(
            _CODEXHOME_SESSION, _CODEXHOME_WORKDIR, "codex",
            codex_home=str(home),
        )
    # 专用入口没有 args 参数：合同固定 exact --sandbox read-only
    with pytest.raises(TypeError):
        herdr_client.start_workspace_codex_home(
            **_codexhome_start_kwargs(str(home), args="")
        )


def test_codex_home_invalid_fails_before_any_herdr_call(tmp_path) -> None:
    too_open = tmp_path / "open"
    too_open.mkdir()
    too_open.chmod(0o755)
    result = herdr_client.start_workspace_codex_home(
        **_codexhome_start_kwargs(str(too_open)),
    )
    assert result["error_code"] == "workspace_codex_home_invalid"


def test_codex_home_env_command_rejects_injection() -> None:
    good = herdr_client._codex_home_env_command(
        codex_home="/srv/private/attach-1",
        agent_bin="/usr/local/bin/codex",
        public_args=["--sandbox", "read-only"],
    )
    assert good == (
        "/usr/bin/env CODEX_HOME=/srv/private/attach-1"
        " /usr/local/bin/codex --sandbox read-only"
    )
    for bad_bin in ["codex", "/tmp/x y/codex", "/tmp/codex;sh"]:
        with pytest.raises(ValueError):
            herdr_client._codex_home_env_command(
                codex_home="/srv/private/attach-1",
                agent_bin=bad_bin,
                public_args=["--sandbox", "read-only"],
            )
    for bad_args in (
        [], ["-c", "prompt=rm"], ["--sandbox", "read-only", "BODY"],
        ["--sandbox", "read-only", "tok=secret"], ["-"], ["read-only"],
    ):
        with pytest.raises(ValueError):
            herdr_client._codex_home_env_command(
                codex_home="/srv/private/attach-1",
                agent_bin="/usr/local/bin/codex",
                public_args=bad_args,
            )


def test_codex_home_managed_start_uses_atomic_pane_run(
    monkeypatch, tmp_path,
) -> None:
    home = _codexhome_dirs(tmp_path)
    detected = {
        "session": _CODEXHOME_SESSION,
        "panes": [{
            "pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1",
            "cwd": _CODEXHOME_WORKDIR, "agent": "codex",
        }],
        "agents": [{"name": None, "agent": "codex", "pane_id": "w1:p2"}],
        "tabs": [], "workspaces": [{"workspace_id": "w1", "focused": False}],
        "focused_workspace_id": None,
    }
    empty = {
        "session": _CODEXHOME_SESSION, "panes": [], "agents": [], "tabs": [],
        "workspaces": [{"workspace_id": "w1", "focused": True}],
        "focused_workspace_id": "w1",
    }
    calls, add_polls = _codexhome_harness(
        monkeypatch, tmp_path,
        snapshots=[empty, empty, empty, detected],
    )
    add_polls([detected, detected])
    result = herdr_client.start_workspace_codex_home(
        **_codexhome_start_kwargs(str(home)),
    )
    assert result["available"] is True, result
    assert result["pane_id"] == "w1:p2"
    run_calls = [c for c in calls if c[2:4] == ["pane", "run"]]
    assert len(run_calls) == 1
    assert run_calls[0] == [
        "--session", _CODEXHOME_SESSION, "pane", "run", "w1:p2",
        "/usr/bin/env CODEX_HOME=" + str(home)
        + " /bin/sh --sandbox read-only",
    ]
    assert not [c for c in calls if c[2:4] == ["agent", "start"]]
    descriptor = herdr_client.get_launch_descriptor_by_instance(
        _CODEXHOME_INSTANCE,
    )
    assert descriptor is not None
    assert descriptor["args"] == ["--sandbox", "read-only"]
    assert descriptor["codex_home"] == str(home)
    assert descriptor["state"] == "active"


def test_codex_home_detection_failure_retires_and_closes(
    monkeypatch, tmp_path,
) -> None:
    home = _codexhome_dirs(tmp_path)
    empty = {
        "session": _CODEXHOME_SESSION, "panes": [], "agents": [], "tabs": [],
        "workspaces": [{"workspace_id": "w1", "focused": True}],
        "focused_workspace_id": "w1",
    }
    with_pane = {
        "session": _CODEXHOME_SESSION,
        "panes": [{
            "pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1",
            "cwd": _CODEXHOME_WORKDIR,
        }],
        "agents": [], "tabs": [],
        "workspaces": [{"workspace_id": "w1", "focused": False}],
        "focused_workspace_id": None,
    }
    calls, _ = _codexhome_harness(
        monkeypatch, tmp_path,
        snapshots=[empty, empty, empty, with_pane],
    )
    # 检测窗口内 pane 始终无 agent → 超时（把窗口缩短避免慢测）
    monkeypatch.setattr(herdr_client, "_await_agent_detection",
                        lambda *a, **k: False)
    result = herdr_client.start_workspace_codex_home(
        **_codexhome_start_kwargs(str(home)),
    )
    assert result["available"] is True
    assert result["error_code"] == "workspace_agent_readiness_failed"
    assert result["rolled_back"] is True
    assert [c for c in calls if c[2:4] == ["pane", "close"]]
    assert herdr_client.get_launch_descriptor_by_instance(
        _CODEXHOME_INSTANCE,
    ) is None

    # 回收无法确认 → 必须 descriptor_cleanup_incomplete，绝不静默宣称回滚
    monkeypatch.undo()
    calls2, _ = _codexhome_harness(
        monkeypatch, tmp_path,
        snapshots=[empty, empty, empty, with_pane],
    )
    monkeypatch.setattr(herdr_client, "_await_agent_detection",
                        lambda *a, **k: False)
    monkeypatch.setattr(
        herdr_client, "_close_created_pane_verified",
        lambda *a, **k: False,
    )
    second = herdr_client.start_workspace_codex_home(
        **_codexhome_start_kwargs(str(home)),
    )
    assert second["error_code"] == "descriptor_cleanup_incomplete"
    assert second["rolled_back"] is False
    assert second["pane_id"]
    monkeypatch.undo()


def test_restart_with_codex_home_reuses_env_and_fails_closed(
    monkeypatch, tmp_path,
) -> None:
    home = _codexhome_dirs(tmp_path)
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "launch.json"),
    )
    herdr_client.save_launch_descriptor(
        session=_CODEXHOME_SESSION, pane_id="w1:p2",
        name=_CODEXHOME_INSTANCE, kind="codex",
        args=["--sandbox", "read-only"], agent="codex",
        workdir=_CODEXHOME_WORKDIR, instance_id=_CODEXHOME_INSTANCE,
        display_name="codex", project_id=_CODEXHOME_PROJECT,
        workspace_id=_CODEXHOME_WORKSPACE, codex_home=str(home),
    )
    # pane.run 私有启动的 Codex 由 detection 管理：live agent 没有 name。
    live_unnamed = {
        "session": _CODEXHOME_SESSION,
        "panes": [{
            "pane_id": "w1:p2", "agent": "codex", "cwd": _CODEXHOME_WORKDIR,
        }],
        "agents": [{"name": None, "agent": "codex", "pane_id": "w1:p2"}],
    }
    codex_back = live_unnamed
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "require_herdr_capabilities", lambda: {},
    )
    monkeypatch.setattr(
        herdr_client, "_find_agent_bin", lambda name: "/bin/sh",
    )
    state = {"interrupted": False}

    def snapshot(session):
        if state["interrupted"]:
            return {
                "session": session,
                "panes": [{
                    "pane_id": "w1:p2", "agent": None, "cwd": _CODEXHOME_WORKDIR,
                }],
                "agents": [],
            }
        return live_unnamed

    calls: list[list[str]] = []

    def run(args, timeout=10):
        calls.append(list(args))
        if args[2:4] == ["pane", "send-keys"]:
            state["interrupted"] = True
        return ""

    monkeypatch.setattr(herdr_client, "_snapshot_session", snapshot)
    monkeypatch.setattr(herdr_client, "_run", run)
    monkeypatch.setattr(
        herdr_client, "_pane_at_available_shell", lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        herdr_client, "_await_agent_detection",
        lambda session, pane, kind: True,
    )
    result = herdr_client.restart_pane(_CODEXHOME_SESSION, "w1:p2")
    assert result.get("restarted") is True, result
    run_calls = [c for c in calls if c[2:4] == ["pane", "run"]]
    assert run_calls == [[
        "--session", _CODEXHOME_SESSION, "pane", "run", "w1:p2",
        "/usr/bin/env CODEX_HOME=" + str(home)
        + " /bin/sh --sandbox read-only",
    ]]
    assert not [c for c in calls if c[2:4] == ["agent", "start"]]

    # home 失效（被删）→ 在任何中断键之前 fail closed，零副作用
    shutil.rmtree(home)
    calls.clear()
    state["interrupted"] = False
    monkeypatch.setattr(herdr_client, "_await_agent_detection",
                        lambda *a, **k: True)
    failed = herdr_client.restart_pane(_CODEXHOME_SESSION, "w1:p2")
    assert failed.get("error_code") == "restart_identity_invalid"
    assert not [c for c in calls if c[2:4] == ["agent", "start"]]
    assert not [c for c in calls if c[2:4] == ["pane", "send-keys"]]
    assert not [c for c in calls if c[2:4] == ["agent", "send-keys"]]

    # descriptor codex_home + 空 args → 同样 mutation 前拒绝
    home2 = tmp_path / "home2"
    home2.mkdir(mode=0o700)
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "launch2.json"),
    )
    herdr_client.save_launch_descriptor(
        session=_CODEXHOME_SESSION, pane_id="w1:p2",
        name=_CODEXHOME_INSTANCE, kind="codex",
        args=[], agent="codex",
        workdir=_CODEXHOME_WORKDIR, instance_id=_CODEXHOME_INSTANCE,
        display_name="codex", project_id=_CODEXHOME_PROJECT,
        workspace_id=_CODEXHOME_WORKSPACE, codex_home=str(home2),
    )
    calls.clear()
    empty_args = herdr_client.restart_pane(_CODEXHOME_SESSION, "w1:p2")
    assert empty_args.get("error_code") == "restart_identity_invalid"
    assert not [c for c in calls if c[2:4] == ["pane", "send-keys"]]


@pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("herdr") is None,
    reason="需要真实 codex 与 herdr 二进制",
)
def test_codex_home_real_live_managed_start(
    monkeypatch, tmp_path,
) -> None:
    """真实 herdr（私有 XDG）+ 真实 codex：原子 pane.run、检测、关闭、清理。"""
    import tempfile
    import uuid

    # 既有 test_snapshot_reports_session_list_failure 会把线程局部
    # _LIST_SESSIONS_FAILED 留为 True（历史已知顺序污染）；live 前复位。
    herdr_client._LIST_SESSIONS_FAILED.value = False

    # herdr 的 unix socket 受 sun_path 108 字符上限约束；pytest 的深层
    # basetemp 会把 session socket 路径推过上限，故用 /tmp 下短根。
    isolated = Path(tempfile.mkdtemp(prefix="chx-", dir="/tmp"))
    isolated.chmod(0o700)
    session = "ch-live-" + uuid.uuid4().hex[:6]
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(isolated / "h" / "config.toml"))
    (isolated / "h").mkdir(mode=0o700)
    (isolated / "h" / "config.toml").write_text(
        "onboarding = false\n", encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated / "x"))
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated / "s"))
    monkeypatch.setenv("XDG_DATA_HOME", str(isolated / "d"))
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(isolated / "launch.json"),
    )
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    for extra in (
        "COCKPIT_HERDR_STATE_MODE", "COCKPIT_HERDR_STATE_CANARY_SESSIONS",
        "COCKPIT_B0_MODE", "COCKPIT_EDITION", "COCKPIT_UPGRADE_V2_ENABLED",
        "COCKPIT_SCHEMA_EVIDENCE_PATH", "COCKPIT_SCHEMA_EVIDENCE_SHA256",
    ):
        monkeypatch.delenv(extra, raising=False)
    monkeypatch.setattr(
        herdr_client.next_profile, "require_session", lambda value: value,
    )
    workdir = isolated / "repo"
    workdir.mkdir(mode=0o700)
    home = isolated / "codex-home"
    home.mkdir(mode=0o700)

    try:
        server_log = isolated / "server.log"
        log_handle = open(server_log, "wb")
        owned = subprocess.Popen(
            [herdr_client.HERDR_BIN, "--session", session, "server"],
            stdin=subprocess.DEVNULL, stdout=log_handle,
            stderr=subprocess.STDOUT, close_fds=True, start_new_session=True,
        )
        herdr_client._SESSION_BOOTSTRAP_PROCESSES[session] = owned
        ensured = herdr_client.ensure_session(session=session)
        assert ensured.get("available") is True, (
            f"{ensured} rc={owned.poll()} sessions={herdr_client.list_sessions()!r}"
            f" log={server_log.read_text(errors='replace')[-1500:]!r}"
        )
        result = herdr_client.start_workspace_codex_home(
            session=session, workdir=str(workdir),
            instance_id=_CODEXHOME_INSTANCE,
            project_id=_CODEXHOME_PROJECT, workspace_id=_CODEXHOME_WORKSPACE,
            codex_home=str(home), label="codex",
        )
        assert result.get("available") is True, result
        assert result.get("instance_id") == _CODEXHOME_INSTANCE
        pane_id = result["pane_id"]

        nul = chr(0)
        argv_seen = None
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = [
                    part for part in
                    (entry / "cmdline").read_bytes().decode().split(nul)
                    if part
                ]
                environ = (entry / "environ").read_bytes().decode(
                    errors="replace",
                ).split(nul)
            except OSError:
                continue
            if not raw:
                continue
            head = Path(raw[0]).name
            if head == "node" and len(raw) > 1 and "codex" in raw[1]:
                tail = raw[2:]
            elif "codex" in head and head != "node":
                tail = raw[1:]
            else:
                continue
            if tail == ["--sandbox", "read-only"] and (
                f"CODEX_HOME={home}" in environ
            ):
                argv_seen = raw
                break
        assert argv_seen is not None, "未找到私有 CODEX_HOME 的 codex 进程"
        assert "CODEX_HOME" not in argv_seen
        assert str(home) not in argv_seen

        descriptor = herdr_client.get_launch_descriptor_by_instance(
            _CODEXHOME_INSTANCE,
        )
        assert descriptor is not None
        assert descriptor["codex_home"] == str(home)
        assert descriptor["state"] == "active"

        # ---- 真实 restart：同一私有 home 原位重建（detection agent 无 name）----
        restarted = herdr_client.restart_pane(session, pane_id)
        assert restarted.get("restarted") is True, restarted
        assert restarted.get("pane_id") == pane_id
        argv_after = None
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = [
                    part for part in
                    (entry / "cmdline").read_bytes().decode().split(nul)
                    if part
                ]
                environ = (entry / "environ").read_bytes().decode(
                    errors="replace",
                ).split(nul)
            except OSError:
                continue
            if not raw:
                continue
            head = Path(raw[0]).name
            if head == "node" and len(raw) > 1 and "codex" in raw[1]:
                tail = raw[2:]
            elif "codex" in head and head != "node":
                tail = raw[1:]
            else:
                continue
            if tail == ["--sandbox", "read-only"] and (
                f"CODEX_HOME={home}" in environ
            ):
                argv_after = raw
                break
        assert argv_after is not None, "restart 后未找到私有 CODEX_HOME 的 codex"
        assert "CODEX_HOME" not in argv_after

        closed = herdr_client.close_pane(session, pane_id)
        assert closed.get("available") is True
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snap_value = herdr_client.session_snapshot(session)
            if not any(
                p.get("pane_id") == pane_id for p in snap_value.get("panes", [])
            ):
                break
            time.sleep(0.2)
        assert not any(
            p.get("pane_id") == pane_id
            for p in herdr_client.session_snapshot(session).get("panes", [])
        )
        subprocess.run(
            ["herdr", "--session", session, "session", "close"],
            capture_output=True, text=True, timeout=15,
        )
    finally:
        # 终止本测试自起的 herdr server，避免遗留进程干扰同进程内其他
        # 真实 herdr 用例（session close CLI 不保证回收 server 进程）。
        try:
            owned.terminate()
            owned.wait(timeout=5)
        except Exception:
            pass
        herdr_client._SESSION_BOOTSTRAP_PROCESSES.pop(session, None)
        shutil.rmtree(isolated, ignore_errors=True)


_CODEX_PROVIDER_CONFIG = Path("/home/fyc/.codex/relay.config.toml")


@pytest.mark.skipif(
    shutil.which("codex") is None
    or shutil.which("herdr") is None
    or not _CODEX_PROVIDER_CONFIG.is_file(),
    reason="需要真实 herdr/codex 与显式 host provider config",
)
def test_real_wakeup_prompt_receipt_requires_working(
    monkeypatch,
) -> None:
    """已登录 Codex：fixed wakeup 必须进入 working 且 seq 前进，失败不算绿。"""
    import tempfile
    import uuid

    from agent_cockpit import local_codex_harness as harness_mod

    herdr_client._LIST_SESSIONS_FAILED.value = False
    isolated = Path(tempfile.mkdtemp(prefix="e3w2-", dir="/tmp"))
    isolated.chmod(0o700)
    session = "e3w2-" + uuid.uuid4().hex[:6]
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(isolated / "h" / "config.toml"))
    (isolated / "h").mkdir(mode=0o700)
    (isolated / "h" / "config.toml").write_text(
        "onboarding = false\n", encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated / "x"))
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated / "s"))
    monkeypatch.setenv("XDG_DATA_HOME", str(isolated / "d"))
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(isolated / "launch.json"),
    )
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.setattr(
        herdr_client.next_profile, "require_session", lambda value: value,
    )
    workdir = isolated / "repo"
    workdir.mkdir(mode=0o700)
    home = isolated / "codex-home"
    home.mkdir(mode=0o700)
    capability_root = isolated / "caps"
    capability_root.mkdir(mode=0o700)
    capability = capability_root / "probe.cap"
    capability.write_text("{}", encoding="ascii")
    os.chmod(capability, 0o600)
    provenance = harness_mod._provider_auth_provenance_from_path(
        _CODEX_PROVIDER_CONFIG,
    )
    harness_mod._write_mcp_home_config(
        home, capability, capability_root,
        provenance=provenance, checkout=workdir,
    )
    owned = None
    try:
        server_log = isolated / "server.log"
        log_handle = open(server_log, "wb")
        owned = subprocess.Popen(
            [herdr_client.HERDR_BIN, "--session", session, "server"],
            stdin=subprocess.DEVNULL, stdout=log_handle,
            stderr=subprocess.STDOUT, close_fds=True, start_new_session=True,
        )
        herdr_client._SESSION_BOOTSTRAP_PROCESSES[session] = owned
        ensured = herdr_client.ensure_session(session=session)
        assert ensured.get("available") is True, ensured
        started = herdr_client.start_workspace_codex_home(
            session=session, workdir=str(workdir),
            instance_id="i-" + "b" * 26,
            project_id=_CODEXHOME_PROJECT, workspace_id=_CODEXHOME_WORKSPACE,
            codex_home=str(home), label="codex",
        )
        assert started.get("available") is True, started
        pane_id = started["pane_id"]
        before = herdr_client.inspect_agent(session, pane_id)
        receipt = herdr_client.submit_agent_prompt_until_working(
            session, pane_id, harness_mod.WAKEUP_TEXT,
        )
        after = herdr_client.inspect_agent(session, pane_id)
        (isolated / "wakeup-receipt.json").write_text(
            json.dumps({
                "available": receipt.get("available"),
                "submitted": receipt.get("submitted"),
                "executing": receipt.get("executing"),
                "status": receipt.get("status"),
                "state_change_seq": receipt.get("state_change_seq"),
            }, sort_keys=True),
            encoding="utf-8",
        )
        assert receipt.get("available") is True, receipt
        assert receipt.get("submitted") is True, receipt
        assert receipt.get("executing") is True, receipt
        assert receipt.get("status") == "working"
        assert type(receipt.get("state_change_seq")) is int
        if type(before.get("state_change_seq")) is int:
            assert receipt["state_change_seq"] > before["state_change_seq"]
        assert after.get("agent_status") == "working"
        assert "BOSS" not in json.dumps(receipt)
        assert "root_message" not in json.dumps(receipt)
        assert "token" not in json.dumps(receipt)
        assert "OPENAI_API_KEY" not in json.dumps(receipt)
        herdr_client.close_pane(session, pane_id)
        subprocess.run(
            ["herdr", "--session", session, "session", "close"],
            capture_output=True, text=True, timeout=15,
        )
    finally:
        if owned is not None:
            try:
                owned.terminate()
                owned.wait(timeout=5)
            except Exception:
                pass
        herdr_client._SESSION_BOOTSTRAP_PROCESSES.pop(session, None)
        shutil.rmtree(isolated, ignore_errors=True)


def test_recycle_private_session_skips_shared_name() -> None:
    assert herdr_client.is_private_ephemeral_session("s") is False
    assert herdr_client.recycle_private_session("s") == {
        "available": True, "skipped": True,
    }


def test_recycle_private_session_fail_closed_on_leftovers(tmp_path, monkeypatch) -> None:
    session = "ephemeral-recycleleftover0123456789ab"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("COCKPIT_NEXT_PROFILE", "ephemeral")
    monkeypatch.setenv("COCKPIT_EPHEMERAL_ROOT", str(tmp_path))
    monkeypatch.setattr(
        herdr_client.next_profile, "ephemeral_session_for_root", lambda _root: session,
    )
    monkeypatch.setenv("HERDR_SESSION", session)
    monkeypatch.setattr(herdr_client.next_profile, "require_session", lambda value: value)
    root = tmp_path / "herdr" / "sessions" / session
    root.mkdir(parents=True)
    (root / "session.json").write_text("{}", encoding="utf-8")
    (root / "herdr.sock").write_bytes(b"s")
    (root / "herdr-client.sock").write_bytes(b"c")
    monkeypatch.setattr(
        herdr_client, "stop_session",
        lambda name: {"available": True, "stopped": name},
    )
    monkeypatch.setattr(
        herdr_client, "delete_session",
        lambda name: {"available": True, "deleted": name},
    )
    result = herdr_client.recycle_private_session(session)
    assert result["available"] is True
    assert "session leftovers remain" in result["error"]
    assert "session.json" in result["error"]


def test_recycle_private_session_succeeds_when_stop_delete_clear_files(
    tmp_path, monkeypatch,
) -> None:
    session = "ephemeral-recyclecleared0123456789abc"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("COCKPIT_NEXT_PROFILE", "ephemeral")
    monkeypatch.setenv("COCKPIT_EPHEMERAL_ROOT", str(tmp_path))
    monkeypatch.setattr(
        herdr_client.next_profile, "ephemeral_session_for_root", lambda _root: session,
    )
    monkeypatch.setenv("HERDR_SESSION", session)
    monkeypatch.setattr(herdr_client.next_profile, "require_session", lambda value: value)
    root = tmp_path / "herdr" / "sessions" / session
    root.mkdir(parents=True)
    (root / "session.json").write_text("{}", encoding="utf-8")

    def delete(name: str) -> dict[str, object]:
        for child in root.iterdir():
            child.unlink()
        root.rmdir()
        return {"available": True, "deleted": name}

    monkeypatch.setattr(
        herdr_client, "stop_session",
        lambda name: {"available": True, "stopped": name},
    )
    monkeypatch.setattr(herdr_client, "delete_session", delete)
    result = herdr_client.recycle_private_session(session)
    assert result == {
        "available": True, "stopped": session, "deleted": session,
    }
    assert not root.exists()


def test_fixed_scoped_session_is_not_private_ephemeral(monkeypatch) -> None:
    monkeypatch.setenv("HERDR_SESSION", "agent-cockpit-next")
    monkeypatch.delenv("COCKPIT_NEXT_PROFILE", raising=False)
    assert herdr_client.is_private_ephemeral_session("agent-cockpit-next") is False


def test_stop_known_error_is_surface_and_delete_is_not_called(monkeypatch) -> None:
    deleted: list[str] = []
    session = "ephemeral-" + "a" * 32
    monkeypatch.setenv("COCKPIT_NEXT_PROFILE", "ephemeral")
    monkeypatch.setenv("COCKPIT_EPHEMERAL_ROOT", "/tmp/ephemeral-test-root")
    monkeypatch.setattr(
        herdr_client.next_profile, "ephemeral_session_for_root", lambda _root: session,
    )
    monkeypatch.setattr(herdr_client.next_profile, "require_session", lambda value: value)
    monkeypatch.setattr(
        herdr_client,
        "stop_session",
        lambda name: {"available": True, "error": "stop failed"},
    )

    def delete(name: str) -> dict[str, object]:
        deleted.append(name)
        return {"available": True, "deleted": name}

    monkeypatch.setattr(herdr_client, "delete_session", delete)
    result = herdr_client.recycle_private_session(session)
    assert result.get("error") == "stop failed"
    assert deleted == []


def test_delete_known_error_is_not_reported_as_success(monkeypatch) -> None:
    session = "ephemeral-" + "a" * 32
    monkeypatch.setenv("COCKPIT_NEXT_PROFILE", "ephemeral")
    monkeypatch.setenv("COCKPIT_EPHEMERAL_ROOT", "/tmp/ephemeral-test-root")
    monkeypatch.setattr(
        herdr_client.next_profile, "ephemeral_session_for_root", lambda _root: session,
    )
    monkeypatch.setattr(herdr_client.next_profile, "require_session", lambda value: value)
    monkeypatch.setattr(
        herdr_client,
        "stop_session",
        lambda name: {"available": True, "stopped": name},
    )
    monkeypatch.setattr(
        herdr_client,
        "delete_session",
        lambda _name: {"available": True, "error": "delete failed"},
    )
    result = herdr_client.recycle_private_session(session)
    assert result.get("error") == "delete failed"
    assert "deleted" not in result
