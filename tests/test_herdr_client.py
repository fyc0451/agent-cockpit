import sys
import time
from unittest.mock import call

import herdr_client


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


def test_start_agent_renames_pane_to_agent_name(monkeypatch):
    """启动成功后把 pane 命名为 agent 名(默认序号难辨认);rename 失败不影响启动。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_agent_cmd", lambda *args: "codex")
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    snapshots = iter([
        {"panes": []},
        {"panes": [{"pane_id": "w1:p2", "agent": None}]},
        {"panes": [{"pane_id": "w1:p2", "agent": "codex"}]},
        {"panes": [{"pane_id": "w1:p2", "agent": "codex"}]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []
    monkeypatch.setattr(
        herdr_client, "_run", lambda args, timeout=10: calls.append(args) or ""
    )

    result = herdr_client.start_agent("demo", "/tmp/project")

    assert result["pane_id"] == "w1:p2"
    assert ["--session", "demo", "pane", "rename", "w1:p2", "codex"] in calls


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


def test_start_agent_reuses_only_matching_workdir(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client,
        "_snapshot_session",
        lambda session: {
            "panes": [{"pane_id": "w1:p5", "agent": "codex", "cwd": "/tmp/project"}]
        },
    )
    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应创建 pane")),
    )

    result = herdr_client.start_agent("demo", "/tmp/project/./", "codex")

    assert result["reused"] is True
    assert result["pane_id"] == "w1:p5"


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
