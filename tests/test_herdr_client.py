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
    assert ["--session", "demo", "tab", "rename", "w1:t2", "demo"] in calls
    assert ["--session", "demo", "workspace", "rename", "w1", "demo"] in calls


def test_start_agent_fallback_selects_highest_numeric_pane(monkeypatch):
    """fallback 应把 w1:p10 视为比 w1:p9 更新，而不是按字符串排序。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_agent_cmd", lambda *args: "codex")
    # 可执行文件探测与宿主机解耦(CI 上没有安装 codex)
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    snapshots = iter([
        {"panes": []},
        {"panes": [
            {"pane_id": "w1:p10", "agent": None},
            {"pane_id": "w1:p9", "agent": None},
        ]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project")

    assert result["pane_id"] == "w1:p10"
    assert call(
        ["--session", "demo", "pane", "run", "w1:p10", "codex"],
        timeout=8,
    ) in calls


def test_start_agent_renames_workspace_tab_and_pane(monkeypatch):
    """tab 布局必须改用户实际看到的三层名称，而不只是 pane label。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_agent_cmd", lambda *args: "codex")
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: sys.executable)
    monkeypatch.setattr(
        herdr_client,
        "_snapshot_session",
        lambda session: {"panes": [{
            "pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1",
            "agent": None,
        }]},
    )
    calls = []
    monkeypatch.setattr(
        herdr_client, "_run", lambda args, timeout=10: calls.append(args) or ""
    )

    result = herdr_client.start_agent("demo", "/tmp/project", layout="tab")

    assert result["pane_id"] == "w1:p2"
    assert ["--session", "demo", "pane", "rename", "w1:p2", "codex"] in calls
    assert ["--session", "demo", "tab", "rename", "w1:t2", "codex"] in calls
    assert ["--session", "demo", "workspace", "rename", "w1", "demo"] in calls


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
        [
            "--session", "demo", "pane", "run", "w1:p5",
            "cd", "/tmp/project", "&&", "opencode", "/tmp/project",
        ],
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
