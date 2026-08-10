import tomllib
import threading

import herdr_client
import server
import pytest
from fastapi.testclient import TestClient


def _write_config(tmp_path, monkeypatch, content=""):
    path = tmp_path / "config.toml"
    if content:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(path))
    return path


def test_set_theme_replaces_existing_name(tmp_path, monkeypatch):
    path = _write_config(tmp_path, monkeypatch, '''[theme]
name = "terminal"
auto_switch = false

[keys]
x = 1
''')
    herdr_client.set_theme_name("catppuccin")
    text = path.read_text(encoding="utf-8")
    config = tomllib.loads(text)
    assert config["theme"]["name"] == "catppuccin"
    assert config["theme"]["auto_switch"] is False  # 其余键不动
    assert config["keys"]["x"] == 1


def test_set_theme_inserts_name_when_missing(tmp_path, monkeypatch):
    path = _write_config(tmp_path, monkeypatch, '''[theme]
auto_switch = false
''')
    herdr_client.set_theme_name("dracula")
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    assert config["theme"]["name"] == "dracula"
    assert config["theme"]["auto_switch"] is False


def test_set_theme_appends_section_when_absent(tmp_path, monkeypatch):
    path = _write_config(tmp_path, monkeypatch, "[keys]\nx = 1\n")
    herdr_client.set_theme_name("nord")
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    assert config["theme"]["name"] == "nord"
    assert config["keys"]["x"] == 1


def test_set_theme_creates_missing_file(tmp_path, monkeypatch):
    path = _write_config(tmp_path, monkeypatch)
    herdr_client.set_theme_name("vesper")
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    assert config["theme"]["name"] == "vesper"


def test_set_theme_rejects_invalid_name(tmp_path, monkeypatch):
    path = _write_config(tmp_path, monkeypatch, "[theme]\n")
    for bad in ("", "a b", "../x", 'x"; evil', "a" * 65):
        with pytest.raises(ValueError):
            herdr_client.set_theme_name(bad)
    assert path.read_text(encoding="utf-8") == "[theme]\n"  # 未改动


def test_reload_config_hits_every_running_session(monkeypatch):
    calls = []
    monkeypatch.setattr(
        herdr_client, "list_sessions",
        lambda: [{"name": "s1", "status": "running"}, {"name": "s2", "status": "running"}],
    )
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: calls.append(args) or "",
    )
    result = herdr_client.reload_config()
    assert result["ok"] is True
    assert result["reloaded"] == ["s1", "s2"]
    assert calls == [
        ["--session", "s1", "server", "reload-config"],
        ["--session", "s2", "server", "reload-config"],
    ]


def test_reload_config_partial_failure_reports_session(monkeypatch):
    monkeypatch.setattr(
        herdr_client, "list_sessions",
        lambda: [{"name": "good", "status": "running"}, {"name": "bad", "status": "running"}],
    )

    def flaky(args, timeout=10):
        if args[1] == "bad":
            raise RuntimeError("boom")
        return ""

    monkeypatch.setattr(herdr_client, "_run", flaky)
    result = herdr_client.reload_config()
    assert result["ok"] is False
    assert result["reloaded"] == ["good"]
    assert any("bad" in e for e in result["errors"])


def test_reload_config_skips_not_running_sessions(monkeypatch):
    calls = []
    monkeypatch.setattr(
        herdr_client, "list_sessions",
        lambda: [
            {"name": "default", "status": "stopped"},
            {"name": "live", "status": "running"},
        ],
    )
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: calls.append(args) or "",
    )
    result = herdr_client.reload_config()
    assert result["ok"] is True and result["reloaded"] == ["live"]
    assert calls == [["--session", "live", "server", "reload-config"]]


def test_reload_config_falls_back_without_sessions(monkeypatch):
    calls = []
    monkeypatch.setattr(herdr_client, "list_sessions", lambda: [])
    monkeypatch.setattr(
        herdr_client, "_run",
        lambda args, timeout=10: calls.append(args) or "",
    )
    result = herdr_client.reload_config()
    assert result["ok"] is True and calls == [["server", "reload-config"]]


def test_theme_herdr_endpoint(monkeypatch, tmp_path):
    _write_config(tmp_path, monkeypatch, "[theme]\nname = \"terminal\"\n")
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    reloads = []
    effects = []
    effect_done = threading.Event()
    monkeypatch.setattr(
        server.herdr_client, "reload_config",
        lambda timeout=10: reloads.append(True) or {"ok": True},
    )
    monkeypatch.setattr(server.terminal, "list_terms", lambda: [])
    monkeypatch.setattr(
        server.herdr_client,
        "apply_agent_web_themes",
        lambda mode: effects.append(mode) or effect_done.set() or {
            "ok": True, "applied": [], "errors": [],
        },
    )
    client = TestClient(server.app, headers={"Authorization": "Bearer secret"})

    r = client.post("/api/theme/herdr", json={"mode": "dark"})
    assert r.status_code == 200
    body = r.json()
    assert body["theme"] == "catppuccin"
    assert body["config_changed"] is True
    assert body["reload"]["ok"] is True
    assert body["reload"].get("scheduled") is True or body["reload"].get("skipped") is False
    assert "notified_terms" in body
    assert effect_done.wait(2)
    assert reloads == [True]
    assert effects == ["dark"]

    # 第二次同 mode：config 未变，应跳过 reload-config
    reloads.clear()
    effect_done.clear()
    r = client.post("/api/theme/herdr", json={"mode": "dark"})
    assert r.status_code == 200
    body = r.json()
    assert body["config_changed"] is False
    assert body["reload"].get("skipped") is True
    assert effect_done.wait(2)
    assert reloads == []
    assert effects == ["dark", "dark"]

    effect_done.clear()
    r = client.post("/api/theme/herdr", json={"mode": "light"})
    assert r.json()["theme"] == "solarized-light"
    assert effect_done.wait(2)

    r = client.post("/api/theme/herdr", json={"mode": "purple"})
    assert r.status_code == 400


def test_theme_side_effects_are_serialized_and_latest_request_wins(monkeypatch):
    first_reload_started = threading.Event()
    release_first_reload = threading.Event()
    dark_applied = threading.Event()
    reload_count = 0
    applied = []

    monkeypatch.setattr(server.herdr_client, "set_web_theme_mode", lambda _mode: None)
    monkeypatch.setattr(
        server.herdr_client,
        "set_theme_for_web_mode",
        lambda _mode, name_override=None: {"changed": True},
    )
    monkeypatch.setattr(server.terminal, "list_terms", lambda: [])

    def reload_config(timeout=10):
        nonlocal reload_count
        reload_count += 1
        if reload_count == 1:
            first_reload_started.set()
            assert release_first_reload.wait(2)
        return {"ok": True}

    def apply_agents(mode):
        applied.append(mode)
        if mode == "dark":
            dark_applied.set()
        return {"ok": True, "applied": [], "errors": []}

    monkeypatch.setattr(server.herdr_client, "reload_config", reload_config)
    monkeypatch.setattr(server.herdr_client, "apply_agent_web_themes", apply_agents)

    server.api_theme_herdr(server.ThemeHerdrReq(mode="light"))
    assert first_reload_started.wait(2)
    server.api_theme_herdr(server.ThemeHerdrReq(mode="dark"))
    release_first_reload.set()

    assert dark_applied.wait(2)
    assert applied == ["dark"]


def test_latest_same_theme_request_keeps_pending_config_reload(monkeypatch):
    changes = iter((True, False))
    reloaded = []
    applied = []
    applied_done = threading.Event()
    monkeypatch.setattr(server, "_THEME_CONFIG_DIRTY", False)
    monkeypatch.setattr(server.herdr_client, "set_web_theme_mode", lambda _mode: None)
    monkeypatch.setattr(
        server.herdr_client,
        "set_theme_for_web_mode",
        lambda _mode, name_override=None: {"changed": next(changes)},
    )
    monkeypatch.setattr(server.terminal, "list_terms", lambda: [])
    monkeypatch.setattr(
        server.herdr_client, "reload_config",
        lambda timeout=10: reloaded.append(True) or {"ok": True},
    )

    def apply_agents(mode):
        applied.append(mode)
        applied_done.set()
        return {"ok": True, "applied": [], "errors": []}

    monkeypatch.setattr(server.herdr_client, "apply_agent_web_themes", apply_agents)

    server._THEME_EFFECT_LOCK.acquire()
    try:
        server.api_theme_herdr(server.ThemeHerdrReq(mode="light"))
        second = server.api_theme_herdr(server.ThemeHerdrReq(mode="light"))
    finally:
        server._THEME_EFFECT_LOCK.release()

    assert second["reload"]["scheduled"] is True
    assert applied_done.wait(2)
    assert reloaded == [True]
    assert applied == ["light"]


def test_failed_theme_reload_stays_dirty_for_same_mode_retry(monkeypatch):
    changes = iter((True, False))
    reload_results = iter(({"ok": False, "errors": ["boom"]}, {"ok": True}))
    reload_count = 0
    applied_done = threading.Event()
    monkeypatch.setattr(server, "_THEME_CONFIG_DIRTY", False)
    monkeypatch.setattr(server.herdr_client, "set_web_theme_mode", lambda _mode: None)
    monkeypatch.setattr(
        server.herdr_client, "set_theme_for_web_mode",
        lambda _mode, name_override=None: {"changed": next(changes)},
    )
    monkeypatch.setattr(server.terminal, "list_terms", lambda: [])

    def reload_config(timeout=10):
        nonlocal reload_count
        reload_count += 1
        return next(reload_results)

    monkeypatch.setattr(server.herdr_client, "reload_config", reload_config)
    monkeypatch.setattr(
        server.herdr_client, "apply_agent_web_themes",
        lambda _mode: applied_done.set() or {"ok": True, "applied": [], "errors": []},
    )

    server.api_theme_herdr(server.ThemeHerdrReq(mode="dark"))
    assert applied_done.wait(2)
    with server._THEME_REQUEST_LOCK:
        assert server._THEME_CONFIG_DIRTY is True

    applied_done.clear()
    second = server.api_theme_herdr(server.ThemeHerdrReq(mode="dark"))
    assert second["reload"]["scheduled"] is True
    assert applied_done.wait(2)
    assert reload_count == 2
    with server._THEME_REQUEST_LOCK:
        assert server._THEME_CONFIG_DIRTY is False


def test_frontend_set_theme_calls_herdr_sync():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "/api/theme/herdr" in html
    # 壳层可调 sendAll(notify=false)；Mode 2031 只走 API 一次
    assert "sendAllTermColorSchemes(false)" in html
    assert "armTermInputGate" in html
