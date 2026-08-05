"""settings.py 配置存储与各模块接入测试。"""
import json

import pytest

import settings
import terminal
import uploads


@pytest.fixture(autouse=True)
def tmp_settings(tmp_path, monkeypatch):
    """设置文件隔离到 tmp_path,并清掉 mtime 缓存。"""
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(settings, "_cache_mtime", -1.0)
    return tmp_path


# ── 存储与校验 ──────────────────────────────────────────────────

def test_defaults_when_file_missing():
    cfg = settings.get()
    assert cfg["language"] == "zh"
    assert cfg["enabled_agents"] == settings.KNOWN_AGENTS
    assert cfg["upload_max_mb"] == 100
    assert cfg["term"]["max_terms"] == 16


def test_update_merges_and_persists(tmp_settings):
    out = settings.update({"language": "en", "upload_max_mb": 50})
    assert out["language"] == "en"
    assert out["upload_max_mb"] == 50
    # 落盘后可重读
    on_disk = json.loads((tmp_settings / "settings.json").read_text())
    assert on_disk["language"] == "en"
    assert settings.get()["language"] == "en"


def test_update_rejects_bad_language():
    with pytest.raises(ValueError, match="language"):
        settings.update({"language": "fr"})


def test_update_rejects_unknown_key():
    with pytest.raises(ValueError, match="未知配置项"):
        settings.update({"extra_roots": ["/tmp"]})


def test_update_rejects_unknown_agent():
    with pytest.raises(ValueError, match="未知 agent"):
        settings.update({"enabled_agents": ["codex", "gpt9"]})


def test_update_dir_agents_validates(tmp_path):
    with pytest.raises(ValueError, match="绝对路径"):
        settings.update({"dir_agents": {"relative/dir": "codex"}})
    with pytest.raises(ValueError, match="agent 未知"):
        settings.update({"dir_agents": {str(tmp_path): "gpt9"}})
    out = settings.update({"dir_agents": {str(tmp_path): "kimi"}})
    assert out["dir_agents"] == {str(tmp_path): "kimi"}


def test_update_clamps_numbers():
    out = settings.update({"upload_max_mb": 99999, "term": {"max_terms": 9999}})
    assert out["upload_max_mb"] == 2048
    assert out["term"]["max_terms"] == 64


def test_corrupt_file_falls_back_to_defaults(tmp_settings):
    (tmp_settings / "settings.json").write_text("{not json")
    assert settings.get()["language"] == "zh"


# ── live 读取语义:只有显式配置才覆盖调用方默认 ────────────────

def test_live_readers_use_default_without_explicit_config():
    assert settings.upload_max_bytes(12345) == 12345
    assert settings.term_setting("write_timeout", 7.7) == 7.7


def test_live_readers_respect_explicit_config():
    settings.update({"upload_max_mb": 3, "term": {"write_timeout": 5.0}})
    assert settings.upload_max_bytes(12345) == 3 * 1024 * 1024
    assert settings.term_setting("write_timeout", 7.7) == 5.0


# ── 接入点:uploads / terminal 走设置 ─────────────────────────

def test_uploads_max_size_uses_settings():
    assert uploads._max_size() == uploads.MAX_SIZE  # 未配置时用模块常量
    settings.update({"upload_max_mb": 2})
    assert uploads._max_size() == 2 * 1024 * 1024


def test_terminal_cfg_uses_settings():
    assert terminal._term_cfg("max_terms", terminal.MAX_TERMS) == terminal.MAX_TERMS
    settings.update({"term": {"max_terms": 3}})
    assert terminal._term_cfg("max_terms", terminal.MAX_TERMS) == 3


# ── server 路由 ─────────────────────────────────────────────────

def test_settings_routes(monkeypatch):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}

    r = client.get("/api/settings", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["language"] == "zh"
    assert "known_agents" in body and "languages" in body

    r = client.put("/api/settings", headers=headers, json={"language": "ja"})
    assert r.status_code == 200
    assert r.json()["language"] == "ja"

    r = client.put("/api/settings", headers=headers, json={"language": "xx"})
    assert r.status_code == 400

    # 未认证被拒
    assert client.get("/api/settings").status_code == 401


def test_agent_mail_config_routes_never_expose_token(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import am_common
    import server

    env_file = tmp_path / "agent-mail" / "client.env"
    env_file.parent.mkdir()
    env_file.write_text(
        "hub=http://old-hub:8765\ntoken=top-secret\nfuture=value\n"
    )
    monkeypatch.setattr(am_common, "CLIENT_ENV", env_file)
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.hub_client,
        "status",
        lambda: {"available": True, "reason": None},
    )
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}

    response = client.get("/api/agent-mail/config", headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "hub": "http://old-hub:8765",
        "status": {"available": True, "reason": None},
    }
    assert "top-secret" not in response.text
    assert "token" not in response.text.lower()

    response = client.put(
        "/api/agent-mail/config",
        headers=headers,
        json={"hub": "https://team.example:9765/"},
    )
    assert response.status_code == 200
    assert response.json()["hub"] == "https://team.example:9765"
    assert "top-secret" not in response.text
    assert env_file.read_text() == (
        "hub=https://team.example:9765\n"
        "token=top-secret\nfuture=value\n"
    )

    response = client.put(
        "/api/agent-mail/config",
        headers=headers,
        json={"hub": "file:///etc/passwd"},
    )
    assert response.status_code == 400
    assert "top-secret" not in response.text
    assert client.get("/api/agent-mail/config").status_code == 401
