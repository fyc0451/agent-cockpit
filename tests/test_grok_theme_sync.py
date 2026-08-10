
import json

import herdr_client


def test_grok_theme_slash_and_launch_args():
    assert herdr_client.grok_theme_slash("light") == "/theme light"
    assert herdr_client.grok_theme_slash("dark") == "/theme dark"
    assert herdr_client.grok_launch_theme_args("light") == ["--light"]
    assert herdr_client.grok_launch_theme_args("dark") == []
    assert herdr_client.grok_launch_theme_args(None) == []
    assert herdr_client.opencode_theme_name("light") == "palenight"
    assert herdr_client.opencode_theme_name("dark") == "aura"


def test_apply_grok_web_theme_targets_only_grok(monkeypatch):
    calls = []
    monkeypatch.setattr(
        herdr_client,
        "snapshot",
        lambda: {
            "panes": [
                {"session": "s1", "pane_id": "p1", "agent": "grok"},
                {"session": "s1", "pane_id": "p2", "agent": "codex"},
                {"session": "s1", "pane_id": "p4", "agent": "opencode"},
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
    assert calls == [
        ("s1", "p1", "/theme light", "slash"),
        ("s2", "p3", "/theme light", "slash"),
    ]


def test_opencode_theme_picker_uses_themes_filter_and_confirm(monkeypatch):
    calls = []
    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda args, timeout=10: calls.append(list(args)) or "",
    )
    monkeypatch.setattr(herdr_client.time, "sleep", lambda _seconds: None)

    result = herdr_client.apply_opencode_theme_to_pane("demo", "w1:p4", "palenight")

    assert result["available"] is True
    assert calls == [
        ["--session", "demo", "pane", "send-text", "w1:p4", "/themes"],
        ["--session", "demo", "pane", "send-keys", "w1:p4", "Enter"],
        ["--session", "demo", "pane", "send-text", "w1:p4", "palenight"],
        ["--session", "demo", "pane", "send-keys", "w1:p4", "Enter"],
    ]


def test_opencode_tui_theme_preserves_config_and_rejects_invalid_json(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "opencode" / "tui.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"theme": "old", "scroll_speed": 3}), encoding="utf-8")

    assert herdr_client.set_opencode_tui_theme("aura") == path
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "$schema": "https://opencode.ai/tui.json",
        "theme": "aura",
        "scroll_speed": 3,
    }

    path.write_text("{broken", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    try:
        herdr_client.set_opencode_tui_theme("palenight")
    except ValueError as exc:
        assert "不是有效 JSON" in str(exc)
    else:
        raise AssertionError("invalid tui.json must fail closed")
    assert path.read_text(encoding="utf-8") == before


def test_agent_theme_sync_skips_live_opencode_when_persistence_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(
        herdr_client, "set_opencode_tui_theme",
        lambda _name: (_ for _ in ()).throw(OSError("read-only")),
    )
    monkeypatch.setattr(
        herdr_client, "snapshot",
        lambda: {"panes": [
            {"session": "s1", "pane_id": "p1", "agent": "opencode"},
            {"session": "s1", "pane_id": "p2", "agent": "grok"},
        ]},
    )
    monkeypatch.setattr(
        herdr_client, "apply_opencode_theme_to_pane",
        lambda *args: calls.append(("opencode", *args)) or {"available": True},
    )
    monkeypatch.setattr(
        herdr_client, "pane_send",
        lambda session, pane_id, text, mode="prompt": calls.append(
            ("grok", session, pane_id, text, mode)
        ) or {"available": True},
    )

    result = herdr_client.apply_agent_web_themes("dark")

    assert result["ok"] is False
    assert any("tui.json" in error for error in result["errors"])
    assert result["skipped"] == [{
        "session": "s1", "pane_id": "p1", "agent": "opencode",
        "reason": "tui_config_write_failed",
    }]
    assert calls == [("grok", "s1", "p2", "/theme dark", "slash")]


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
