import shlex
import sys
import time
from unittest.mock import call

import herdr_client
import pytest


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
            ["--session", "demo", "agent", "read", "w1:p2", "--lines", "300"],
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
        if "--source" not in args:
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
            ["--session", "demo", "agent", "read", "w1:p4", "--lines", "300"],
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


def test_start_agent_uses_snapshot_delta_and_single_command_argument(monkeypatch):
    """无创建响应时只能选前后 snapshot 唯一新增 pane，不能猜最大 id。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    command = "'/tmp/agent path/codex' --flag 'a;b'"
    monkeypatch.setattr(herdr_client, "_agent_cmd", lambda *args: command)
    # 可执行文件探测与宿主机解耦(CI 上没有安装 codex)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": [{"pane_id": "w1:p9", "agent": None}]},
        {"panes": [
            {"pane_id": "w1:p9", "agent": None},
            {"pane_id": "w1:p2", "agent": None},
        ]},
        {"panes": [
            {"pane_id": "w1:p9", "agent": None},
            {"pane_id": "w1:p2", "agent": "codex", "cwd": "/tmp/project"},
        ]},
        {"panes": [
            {"pane_id": "w1:p9", "agent": None},
            {"pane_id": "w1:p2", "agent": "codex", "cwd": "/tmp/project"},
        ]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project")

    assert result["pane_id"] == "w1:p2"
    assert result["layout"] == "tab"
    assert call(
        ["--session", "demo", "pane", "run", "w1:p2", command],
        timeout=8,
    ) in calls
    assert call(
        ["--session", "demo", "pane", "rename", "w1:p2", "codex"],
        timeout=5,
    ) in calls


def test_start_agent_renames_workspace_tab_and_pane(monkeypatch):
    """tab 布局必须改用户实际看到的三层名称，而不只是 pane label。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_agent_cmd", lambda *args: "codex")
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": []},
        {"panes": [{
            "pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1",
            "agent": None,
        }]},
        {"panes": [{
            "pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1",
            "agent": "codex",
        }]},
        {"panes": [{
            "pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1",
            "agent": "codex",
        }]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []
    monkeypatch.setattr(
        herdr_client, "_run", lambda args, timeout=10: calls.append(args) or ""
    )

    result = herdr_client.start_agent("demo", "/tmp/project", layout="tab")

    assert result["pane_id"] == "w1:p2"
    assert ["--session", "demo", "pane", "rename", "w1:p2", "codex"] in calls
    assert ["--session", "demo", "tab", "rename", "w1:t2", "codex"] in calls
    assert ["--session", "demo", "workspace", "rename", "w1", "demo"] in calls


def test_start_agent_forces_opencode_to_tab_and_rolls_back_delayed_crash(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_agent_cmd", lambda *args: "opencode")
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": []},
        {"panes": [{"pane_id": "w1:p2", "agent": None}]},
        {"panes": [{"pane_id": "w1:p2", "agent": "opencode"}]},
        {"panes": [{"pane_id": "w1:p2", "agent": None}]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent(
        "demo", "/tmp/project", agent="opencode", layout="right"
    )

    assert result["error"] == "opencode 启动后未能保持运行"
    assert result["rolled_back"] is True
    assert not any("split" in c.args[0] for c in calls)
    assert call(
        ["--session", "demo", "pane", "close", "w1:p2"], timeout=5
    ) in calls


def test_start_agent_allows_qodercli_slow_session_hook(monkeypatch):
    """QoderCLI 冷启动可超过 10 秒，不能在 SessionStart hook 前误杀。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_agent_cmd", lambda *args: "qodercli")
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)

    clock = {"now": 0.0, "snapshots": 0}

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        clock["now"] += seconds

    def snapshot(session):
        clock["snapshots"] += 1
        if clock["snapshots"] == 1:
            return {"panes": []}
        agent = "qodercli" if clock["now"] >= 40 else None
        return {"panes": [{
            "pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1",
            "agent": agent,
        }]}

    monkeypatch.setattr(herdr_client.time, "monotonic", monotonic)
    monkeypatch.setattr(herdr_client.time, "sleep", sleep)
    monkeypatch.setattr(herdr_client, "_snapshot_session", snapshot)
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
    assert clock["now"] >= 41.5
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
    """grok 独立终端秒起但 pane run 后台启动 herdr 不刷新检测 → 原生 agent start。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_agent_cmd", lambda *args: "grok")
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)

    clock = {"now": 0.0, "snapshots": 0}

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        clock["now"] += seconds

    def snapshot(session):
        clock["snapshots"] += 1
        if clock["snapshots"] == 1:
            return {"panes": []}
        return {"panes": [{
            "pane_id": "w1:p3", "tab_id": "w1:t3", "workspace_id": "w1",
            "agent": "grok",
        }]}

    monkeypatch.setattr(herdr_client.time, "monotonic", monotonic)
    monkeypatch.setattr(herdr_client.time, "sleep", sleep)
    monkeypatch.setattr(herdr_client, "_snapshot_session", snapshot)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p3"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project", agent="grok")

    assert result["pane_id"] == "w1:p3"
    assert call(
        [
            "--session", "demo", "agent", "start", "grok",
            "--kind", "grok", "--pane", "w1:p3", "--timeout", "60000",
        ],
        timeout=65,
    ) in calls
    assert call(
        ["--session", "demo", "pane", "run", "w1:p3", "grok"],
        timeout=8,
    ) not in calls


def _grok_native_start_harness(monkeypatch):
    """grok 原生启动路径的公共桩:可控时钟 + 可编程 _run。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_agent_cmd", lambda *args: "grok")
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    clock = {"now": 0.0, "snapshots": 0}

    def snapshot(session):
        clock["snapshots"] += 1
        if clock["snapshots"] == 1:
            return {"panes": []}
        return {"panes": [{
            "pane_id": "w1:p3", "tab_id": "w1:t3", "workspace_id": "w1",
            "agent": "grok",
        }]}

    monkeypatch.setattr(herdr_client.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        herdr_client.time, "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(herdr_client, "_snapshot_session", snapshot)
    return clock


def test_start_agent_grok_retries_pane_busy_then_succeeds(monkeypatch):
    """新建 pane shell 未就绪(busy)时按 0.5s 重试,就绪后正常启动不回滚。"""
    clock = _grok_native_start_harness(monkeypatch)
    calls = []
    busy_left = {"n": 2}

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p3"}}}'
        if "agent" in args and "start" in args and busy_left["n"]:
            busy_left["n"] -= 1
            raise RuntimeError(
                'agent start 失败: {"error":{"code":"agent_pane_busy"}}'
            )
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project", agent="grok")

    assert result.get("error") is None
    assert result["pane_id"] == "w1:p3"
    starts = [c for c in calls if "start" in c.args[0] and "agent" in c.args[0]]
    assert len(starts) == 3  # 2 次 busy + 1 次成功
    assert clock["now"] >= 1.0  # 两次 0.5s 重试等待
    assert call(
        ["--session", "demo", "pane", "close", "w1:p3"], timeout=5,
    ) not in calls


def test_start_agent_grok_pane_busy_gives_up_and_rolls_back(monkeypatch):
    """busy 持续到就绪窗口(10s)耗尽:不无限重试,保留原错误并关闭本次 pane。"""
    clock = _grok_native_start_harness(monkeypatch)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p3"}}}'
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
        ["--session", "demo", "pane", "close", "w1:p3"], timeout=5,
    ) in calls


def test_start_agent_grok_non_busy_error_not_retried(monkeypatch):
    """非 busy 的启动错误不重试,直接回滚。"""
    _grok_native_start_harness(monkeypatch)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p3"}}}'
        if "agent" in args and "start" in args:
            raise RuntimeError("agent start 失败: unknown kind")
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project", agent="grok")

    assert "unknown kind" in result["error"]
    assert result["rolled_back"] is True
    starts = [c for c in calls if "start" in c.args[0] and "agent" in c.args[0]]
    assert len(starts) == 1


def test_start_agent_rolls_back_qodercli_after_extended_timeout(monkeypatch):
    """QoderCLI 在专用窗口内仍未注册时，必须关闭本次新建 pane。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_agent_cmd", lambda *args: "qodercli")
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)

    clock = {"now": 0.0, "snapshots": 0}

    def snapshot(session):
        clock["snapshots"] += 1
        if clock["snapshots"] == 1:
            return {"panes": []}
        return {"panes": [{"pane_id": "w1:p2", "agent": None}]}

    monkeypatch.setattr(herdr_client.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        herdr_client.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(herdr_client, "_snapshot_session", snapshot)
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"w1:p2"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent(
        "demo", "/tmp/project", agent="qodercli", layout="tab",
    )

    assert result["error"] == "qodercli 启动超时，Herdr 未识别到运行中的 agent"
    assert result["rolled_back"] is True
    assert clock["now"] >= 60.0
    assert call(
        ["--session", "demo", "pane", "close", "w1:p2"], timeout=5,
    ) in calls


def test_qoder_aliases_get_longer_start_timeout_only():
    assert herdr_client._agent_start_timeout("qoder") == 60.0
    assert herdr_client._agent_start_timeout("qodercli") == 60.0
    assert herdr_client._agent_start_timeout("qodercn") == 60.0
    assert herdr_client._agent_start_timeout("grok") == 60.0
    assert herdr_client._agent_start_timeout("codex") == 10.0
    assert herdr_client._agent_start_timeout("opencode") == 10.0


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
    monkeypatch.setattr(herdr_client, "_agent_cmd", lambda *args: "codex")
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": [{
            "pane_id": "w1:p2", "agent": "codex", "cwd": "/tmp/project",
            "label": "codex-1",
        }]},
        {"panes": [
            {"pane_id": "w1:p2", "agent": "codex", "cwd": "/tmp/project", "label": "codex-1"},
            {"pane_id": "w1:p3", "agent": None, "cwd": "/tmp/project"},
        ]},
        {"panes": [
            {"pane_id": "w1:p2", "agent": "codex", "cwd": "/tmp/project", "label": "codex-1"},
            {"pane_id": "w1:p3", "agent": "codex", "cwd": "/tmp/project"},
        ]},
        {"panes": [
            {"pane_id": "w1:p2", "agent": "codex", "cwd": "/tmp/project", "label": "codex-1"},
            {"pane_id": "w1:p3", "agent": "codex", "cwd": "/tmp/project", "label": "codex-2"},
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
    assert call(
        ["--session", "demo", "pane", "rename", "w1:p3", "codex-2"],
        timeout=5,
    ) in calls


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
        "demo", "/tmp/project", "codex", label="CODEX-2",
    )

    assert result == {
        "available": True,
        "error": "实例名称已被 pane w1:p2 使用: CODEX-2",
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


def test_restart_pane_preserves_detected_agent(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client,
        "_snapshot_session",
        lambda session: {
            "panes": [{
                "pane_id": "w1:p5",
                "agent": "opencode",
                "cwd": "/tmp/project",
            }],
        },
    )
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(
        herdr_client,
        "_agent_cmd",
        lambda agent, workdir: f"{agent} {workdir}",
    )
    calls = []
    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda args, timeout=10: calls.append(call(args, timeout=timeout)) or "",
    )

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result["agent"] == "opencode"
    assert result["previous_agent"] == "opencode"
    assert call(
        ["--session", "demo", "pane", "run", "w1:p5", "cd /tmp/project && opencode /tmp/project"],
        timeout=8,
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

    assert result == {"available": True, "error": "找不到 pane: w1:p5"}


def test_restart_pane_rejects_unidentified_agent_before_sending_keys(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client,
        "_snapshot_session",
        lambda session: {"panes": [{"pane_id": "w1:p5", "agent": None}]},
    )
    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("agent 未确认前不应发送按键")
        ),
    )

    result = herdr_client.restart_pane("demo", "w1:p5")

    assert result == {
        "available": True,
        "error": "无法识别 pane w1:p5 的 agent，已取消重启",
    }


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


def _move_result(*, changed=True, reason=None):
    move = {"changed": changed}
    if reason:
        move["reason"] = reason
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
    assert calls[0][2:] == ["pane", "move", "w1:p2", "--new-tab"]
    assert calls[1][2:] == ["pane", "move", "w1:p3", "--new-tab"]


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
        return [tuple(c[2:]) for c in calls]

    assert compose("horizontal") == [
        ("pane", "move", "w1:p2", "--tab", "w1:t1", "--target-pane", "w1:p1", "--split", "right"),
        ("pane", "move", "w1:p3", "--tab", "w1:t1", "--target-pane", "w1:p1", "--split", "down"),
        ("pane", "move", "w1:p4", "--tab", "w1:t1", "--target-pane", "w1:p2", "--split", "down"),
    ]
    assert compose("vertical") == [
        ("pane", "move", "w1:p2", "--tab", "w1:t1", "--target-pane", "w1:p1", "--split", "down"),
        ("pane", "move", "w1:p3", "--tab", "w1:t1", "--target-pane", "w1:p1", "--split", "right"),
        ("pane", "move", "w1:p4", "--tab", "w1:t1", "--target-pane", "w1:p2", "--split", "right"),
    ]


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
