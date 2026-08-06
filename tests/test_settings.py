"""settings.py 配置存储与各模块接入测试。"""
import json
import os
import time

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
    assert cfg["team_hub_url"] == ""
    assert cfg["human_auth_url"] == ""
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


def test_update_validates_team_service_urls():
    out = settings.update({
        "team_hub_url": " http://127.0.0.1:9765/ ",
        "human_auth_url": "https://team.example/human-auth/",
    })
    assert out["team_hub_url"] == "http://127.0.0.1:9765"
    assert out["human_auth_url"] == "https://team.example/human-auth"

    with pytest.raises(ValueError, match="本机 HTTP 或 HTTPS"):
        settings.update({"team_hub_url": "http://team.example:8765"})
    with pytest.raises(ValueError, match="不能为空"):
        settings.normalize_service_url("", "Team Hub")


def test_update_persists_validated_values(tmp_settings):
    """落盘必须是 _validate 规范化后的值,不能写未验证的原始输入。"""
    settings.update({"upload_max_mb": 99999, "term": {"max_terms": 9999}})
    on_disk = json.loads((tmp_settings / "settings.json").read_text())
    assert on_disk["upload_max_mb"] == 2048
    assert on_disk["term"]["max_terms"] == 64
    # 去重后落盘;重读时无需再经历 clamp 也是合法值
    settings.update({"enabled_agents": ["codex", "codex", "kimi"]})
    on_disk = json.loads((tmp_settings / "settings.json").read_text())
    assert on_disk["enabled_agents"] == ["codex", "kimi"]


def test_update_keeps_nested_settings_sparse(tmp_settings):
    settings.update({"term": {"max_terms": 9999}})

    on_disk = json.loads((tmp_settings / "settings.json").read_text())
    assert on_disk == {"term": {"max_terms": 64}}


def test_update_refreshes_global_cache(monkeypatch):
    settings.update({"language": "en"})
    monkeypatch.setattr(
        settings,
        "_read_merged",
        lambda: (_ for _ in ()).throw(AssertionError("写后应命中缓存")),
    )

    assert settings.get()["language"] == "en"


def test_update_serializes_concurrent_read_modify_write(tmp_settings, monkeypatch):
    """并发 update 不得丢失更新:整个 RMW 持锁,后写基于最新落盘值。"""
    import threading

    real_replace = os.replace
    first = True
    entered = threading.Event()
    gate = threading.Event()

    def slow_replace(src, dst):
        nonlocal first
        if first:
            first = False
            entered.set()
            gate.wait()
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", slow_replace)
    results = {}

    def do_lang():
        results["lang"] = settings.update({"language": "en"})

    def do_mb():
        results["mb"] = settings.update({"upload_max_mb": 50})

    ta = threading.Thread(target=do_lang)
    tb = threading.Thread(target=do_mb)
    ta.start()
    assert entered.wait(5)
    tb.start()
    time.sleep(0.3)  # 让 tb 进入 update:若未持锁会读到旧文件,持锁则阻塞
    gate.set()
    tb.join(10)
    ta.join(10)

    on_disk = json.loads((tmp_settings / "settings.json").read_text())
    assert on_disk["language"] == "en"
    assert on_disk["upload_max_mb"] == 50
    assert results["lang"]["language"] == "en"
    assert results["mb"]["upload_max_mb"] == 50


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
    monkeypatch.setattr(server.hub_client, "TEAM_HUB_URL", "http://127.0.0.1:9765")
    monkeypatch.setattr(server.hub_client, "HUMAN_AUTH_URL", "http://127.0.0.1:9766")
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}

    response = client.get("/api/agent-mail/config", headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "hub": "http://old-hub:8765",
        "team_hub": "http://127.0.0.1:9765",
        "human_auth": "http://127.0.0.1:9766",
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
        json={
            "hub": "https://team.example:9765",
            "team_hub": "http://127.0.0.1:9775/",
            "human_auth": "http://127.0.0.1:9776/",
        },
    )
    assert response.status_code == 200
    assert response.json()["team_hub"] == "http://127.0.0.1:9775"
    assert response.json()["human_auth"] == "http://127.0.0.1:9776"
    assert settings.get()["team_hub_url"] == "http://127.0.0.1:9775"
    assert settings.get()["human_auth_url"] == "http://127.0.0.1:9776"

    response = client.put(
        "/api/agent-mail/config",
        headers=headers,
        json={"hub": "https://team.example:9765", "team_hub": "https://new.example"},
    )
    assert response.status_code == 400

    response = client.put(
        "/api/agent-mail/config",
        headers=headers,
        json={
            "hub": "https://team.example:9765",
            "team_hub": "http://remote.example:8765",
            "human_auth": "https://remote.example/human-auth",
        },
    )
    assert response.status_code == 400
    assert settings.get()["team_hub_url"] == "http://127.0.0.1:9775"

    response = client.put(
        "/api/agent-mail/config",
        headers=headers,
        json={"hub": "file:///etc/passwd"},
    )
    assert response.status_code == 400
    assert "top-secret" not in response.text
    assert client.get("/api/agent-mail/config").status_code == 401


def test_team_proxy_only_forwards_allowlisted_human_api(monkeypatch):
    from fastapi.testclient import TestClient
    import server

    calls = []
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.hub_client,
        "human_api",
        lambda method, path, authorization, payload=None: calls.append(
            (method, path, authorization, payload)
        ) or {"ok": True},
    )
    monkeypatch.setattr(
        server.hub_client,
        "human_profile",
        lambda authorization: {"profile": {"username": "fyc"}},
    )
    client = TestClient(server.app)
    client.cookies.set(server.TEAM_AUTH_COOKIE, "human.jwt", path="/api")
    headers = {"authorization": "Bearer secret"}

    response = client.get("/api/team/projects", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    response = client.patch(
        "/api/team/projects/demo/members/7",
        headers=headers,
        json={"status": "active"},
    )
    assert response.status_code == 200
    assert client.get("/api/team/inbox", headers=headers).status_code == 200
    assert client.post(
        "/api/team/inbox/mark-read",
        headers=headers,
        json={"ids": [11]},
    ).status_code == 200
    support_payload = {
        "subject": "终端求助",
        "body_md": "请协助排查",
        "mention_handles": ["bob"],
    }
    assert client.post(
        "/api/team/projects/demo/support-requests",
        headers=headers,
        json=support_payload,
    ).status_code == 200
    assert calls == [
        ("GET", "/hub/api/projects", "Bearer human.jwt", None),
        (
            "PATCH",
            "/hub/api/projects/demo/members/7",
            "Bearer human.jwt",
            {"status": "active"},
        ),
        ("GET", "/hub/api/inbox", "Bearer human.jwt", None),
        (
            "POST",
            "/hub/api/inbox/mark-read",
            "Bearer human.jwt",
            {"ids": [11]},
        ),
        (
            "POST",
            "/hub/api/projects/demo/support-requests",
            "Bearer human.jwt",
            support_payload,
        ),
    ]

    assert client.post(
        "/api/team/projects/demo/agents",
        headers=headers,
        json={},
    ).status_code == 404
    assert client.get(
        "/api/team/projects/demo/../../env-check",
        headers=headers,
    ).status_code == 404
    assert client.put(
        "/api/team/inbox",
        headers=headers,
        json={},
    ).status_code == 404
    client.cookies.clear()
    assert client.get("/api/team/projects", headers=headers).status_code == 401


def test_team_auth_login_only_proxies_credentials_to_independent_issuer(monkeypatch):
    from fastapi.testclient import TestClient
    import server

    calls = []
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.hub_client,
        "human_login",
        lambda username, password: calls.append((username, password)) or {
            "access_token": "human.jwt",
            "token_type": "Bearer",
            "expires_in": 3600,
            "profile": {
                "username": username,
                "display_name": "付彦超",
                "roles": ["writer", "admin"],
                "status": "active",
            },
        },
    )
    client = TestClient(server.app)
    response = client.post(
        "/api/team-auth/login",
        headers={"authorization": "Bearer secret"},
        json={"username": "fyc", "password": "local-secret"},
    )
    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert "access_token" not in response.json()
    assert "cockpit_team_human_session=human.jwt" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert calls == [("fyc", "local-secret")]


def test_team_proxy_rechecks_disabled_human_session(monkeypatch):
    from fastapi.testclient import TestClient
    import server

    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.hub_client,
        "human_profile",
        lambda _authorization: (_ for _ in ()).throw(
            server.hub_client.HumanAuthError(401, "Authentication required")
        ),
    )
    monkeypatch.setattr(
        server.hub_client,
        "human_api",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled account must not reach Hub")
        ),
    )
    client = TestClient(server.app)
    client.cookies.set(server.TEAM_AUTH_COOKIE, "disabled.jwt", path="/api")
    response = client.get(
        "/api/team/projects",
        headers={"authorization": "Bearer secret"},
    )
    assert response.status_code == 401


def test_team_auth_registration_and_admin_routes_use_http_only_session(monkeypatch):
    from fastapi.testclient import TestClient
    import server

    calls = []
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.hub_client,
        "human_register",
        lambda username, display_name, password, invite_code: calls.append(
            ("register", username, display_name, password, invite_code)
        ) or {"account": {"username": username, "status": "pending"}},
    )
    monkeypatch.setattr(
        server.hub_client,
        "human_profile",
        lambda authorization: calls.append(("profile", authorization)) or {
            "profile": {"username": "fyc", "roles": ["writer", "admin"]}
        },
    )
    monkeypatch.setattr(
        server.hub_client,
        "human_create_invitation",
        lambda authorization, expires_in: calls.append(
            ("invite", authorization, expires_in)
        ) or {"invite_code": "one-time-code", "expires_at": 123},
    )
    monkeypatch.setattr(
        server.hub_client,
        "human_list_users",
        lambda authorization: calls.append(("users", authorization)) or {"users": []},
    )
    monkeypatch.setattr(
        server.hub_client,
        "human_set_user_status",
        lambda authorization, username, status: calls.append(
            ("status", authorization, username, status)
        ) or {"user": {"username": username, "status": status}},
    )
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}

    registered = client.post(
        "/api/team-auth/register",
        headers=headers,
        json={
            "username": "alice",
            "display_name": "Alice",
            "password": "alice-password-123",
            "invite_code": "one-time-code",
        },
    )
    assert registered.status_code == 201
    assert client.get("/api/team-auth/status", headers=headers).status_code == 401

    client.cookies.set(server.TEAM_AUTH_COOKIE, "human.jwt", path="/api")
    assert client.get("/api/team-auth/status", headers=headers).status_code == 200
    assert client.post(
        "/api/team-auth/invitations",
        headers=headers,
        json={"expires_in": 3600},
    ).status_code == 201
    assert client.get("/api/team-auth/users", headers=headers).status_code == 200
    assert client.patch(
        "/api/team-auth/users/alice",
        headers=headers,
        json={"status": "active"},
    ).status_code == 200
    logged_out = client.post("/api/team-auth/logout", headers=headers)
    assert logged_out.status_code == 200
    assert "cockpit_team_human_session=" in logged_out.headers["set-cookie"]
    assert calls == [
        ("register", "alice", "Alice", "alice-password-123", "one-time-code"),
        ("profile", "Bearer human.jwt"),
        ("invite", "Bearer human.jwt", 3600),
        ("users", "Bearer human.jwt"),
        ("status", "Bearer human.jwt", "alice", "active"),
    ]
