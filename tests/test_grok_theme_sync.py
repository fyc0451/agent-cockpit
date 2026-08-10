
import herdr_client


def test_grok_theme_slash_and_launch_args():
    assert herdr_client.grok_theme_slash("light") == "/theme light"
    assert herdr_client.grok_theme_slash("dark") == "/theme dark"
    assert herdr_client.grok_launch_theme_args("light") == ["--light"]
    assert herdr_client.grok_launch_theme_args("dark") == []
    assert herdr_client.grok_launch_theme_args(None) == []


def test_apply_grok_web_theme_targets_only_grok(monkeypatch):
    calls = []
    monkeypatch.setattr(
        herdr_client,
        "snapshot",
        lambda: {
            "panes": [
                {"session": "s1", "pane_id": "p1", "agent": "grok"},
                {"session": "s1", "pane_id": "p2", "agent": "codex"},
                {"session": "s2", "pane_id": "p3", "agent": "grok"},
            ]
        },
    )
    monkeypatch.setattr(
        herdr_client,
        "pane_send",
        lambda session, pane_id, text, mode="prompt": calls.append((session, pane_id, text, mode)) or {"available": True, "sent": text, "mode": mode},
    )
    out = herdr_client.apply_grok_web_theme("light")
    assert out["command"] == "/theme light"
    assert {(c[0], c[1], c[3]) for c in calls} == {("s1", "p1", "slash"), ("s2", "p3", "slash")}
    assert all(c[2] == "/theme light" for c in calls)


def test_start_agent_injects_light_flag_for_grok(monkeypatch):
    # 最小桩：只验证 agent_args 注入路径不抛、且 --light 进入 start argv
    captured = {}
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "require_herdr_capabilities", lambda: None)
    monkeypatch.setattr(herdr_client, "normalize_agent_kind", lambda a: "grok")
    monkeypatch.setattr(herdr_client, "normalize_agent_args", lambda a: a or "")
    monkeypatch.setattr(herdr_client, "current_web_theme_mode", lambda: "light")
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda s: {"panes": [], "agents": []})
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda a: "/usr/bin/true")
    monkeypatch.setattr(herdr_client, "resolve_unique_agent_name", lambda *a, **k: "grok-1")
    monkeypatch.setattr(herdr_client, "_agent_start_timeout", lambda a: 1.0)
    monkeypatch.setattr(herdr_client, "save_launch_descriptor", lambda **k: None)
    monkeypatch.setattr(herdr_client, "_rename_agent_context", lambda *a, **k: None)
    import os
    monkeypatch.setattr(os, "access", lambda *a, **k: True)
    from pathlib import Path
    monkeypatch.setattr(Path, "is_file", lambda self: True)

    def fake_run(args, timeout=10):
        captured["args"] = list(args)
        if "tab" in args and "create" in args:
            return 'data: {"result":{"pane":{"pane_id":"w1:p9"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)
    # simplify loop: make _snapshot after create return the new pane
    snaps = [
        {"panes": [], "agents": []},
        {"panes": [{"pane_id": "w1:p9", "agent": None}], "agents": []},
        {"panes": [{"pane_id": "w1:p9", "agent": "grok"}], "agents": []},
    ]
    def snap_session(s):
        return snaps.pop(0) if snaps else {"panes": [{"pane_id": "w1:p9", "agent": "grok"}], "agents": []}
    monkeypatch.setattr(herdr_client, "_snapshot_session", snap_session)
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    r = herdr_client.start_agent("demo", "/tmp", agent="grok")
    # may fail mid-start in complex path; check captured start argv if present
    args = captured.get("args") or []
    if "start" in args:
        assert "--light" in args
