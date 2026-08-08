import tomllib

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
        lambda: [{"name": "s1"}, {"name": "s2"}],
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
    monkeypatch.setattr(herdr_client, "list_sessions", lambda: [{"name": "good"}, {"name": "bad"}])

    def flaky(args, timeout=10):
        if args[1] == "bad":
            raise RuntimeError("boom")
        return ""

    monkeypatch.setattr(herdr_client, "_run", flaky)
    result = herdr_client.reload_config()
    assert result["ok"] is False
    assert result["reloaded"] == ["good"]
    assert any("bad" in e for e in result["errors"])


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
    monkeypatch.setattr(server.herdr_client, "reload_config", lambda timeout=10: {"ok": True})
    client = TestClient(server.app, headers={"Authorization": "Bearer secret"})

    r = client.post("/api/theme/herdr", json={"mode": "dark"})
    assert r.status_code == 200
    body = r.json()
    assert body["theme"] == "catppuccin" and body["reload"] == {"ok": True}

    r = client.post("/api/theme/herdr", json={"mode": "light"})
    assert r.json()["theme"] == "solarized"

    r = client.post("/api/theme/herdr", json={"mode": "purple"})
    assert r.status_code == 400


def test_frontend_set_theme_calls_herdr_sync():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "/api/theme/herdr" in html
    assert "sendAllTermColorSchemes();" in html
