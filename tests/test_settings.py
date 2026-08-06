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
        "team_hub_url": " http://10.18.160.11:8765/ ",
        "human_auth_url": "http://10.18.160.11:8766/",
    })
    assert out["team_hub_url"] == "http://10.18.160.11:8765"
    assert out["human_auth_url"] == "http://10.18.160.11:8766"

    with pytest.raises(ValueError, match="本机/私网 HTTP 或 HTTPS"):
        settings.update({"team_hub_url": "http://team.example:8765"})
    with pytest.raises(ValueError, match="本机/私网 HTTP 或 HTTPS"):
        settings.update({"team_hub_url": "http://8.8.8.8:8765"})
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
    # M3d: agents catalog + agent-bindings 白名单
    response = client.get("/api/team/agents", headers=headers)
    assert response.status_code == 200
    response = client.get("/api/team/projects/demo/agent-bindings", headers=headers)
    assert response.status_code == 200
    response = client.post(
        "/api/team/projects/demo/agent-bindings",
        headers=headers,
        json={"agent_id": 7},
    )
    assert response.status_code == 200
    response = client.request(
        "DELETE",
        "/api/team/projects/demo/agent-bindings/7",
        headers={**headers, "content-type": "application/json"},
        data=b"{}",
    )
    assert response.status_code == 200
    # bindings 只允许 DELETE 单条；不支持 GET 单条 / 批量 DELETE
    assert client.get(
        "/api/team/projects/demo/agent-bindings/7",
        headers=headers,
    ).status_code == 404
    assert client.delete(
        "/api/team/projects/demo/agent-bindings",
        headers=headers,
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


# ── M3e: 本地注册身份选择与安全认领 ────────────────────────────

IDENTITY_ID = "home-fyc-github-agent-cockpit/qodercn--main.json"


def _make_registry(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import server

    registry = tmp_path / "registry"
    project_dir = registry / "home-fyc-github-agent-cockpit"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr(server, "_REGISTRY_ROOT", registry)
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.hub_client,
        "human_profile",
        lambda _authorization: {"profile": {"username": "fyc"}},
    )
    monkeypatch.setattr(
        server.hub_client,
        "public_team_config",
        lambda: {"team_hub": "http://127.0.0.1:8765", "human_auth": "http://127.0.0.1:8766"},
    )
    client = TestClient(server.app)
    client.cookies.set(server.TEAM_AUTH_COOKIE, "human.jwt", path="/api")
    headers = {"authorization": "Bearer secret"}
    return server, client, headers, project_dir


def _write_identity(path, **overrides):
    identity = {
        "project_key": "/home/fyc/github/agent-cockpit",
        "project_slug": "home-fyc-github-agent-cockpit",
        "agent": "qodercn",
        "instance": "main",
        "name": "qodercn-main",
        "registration_token": "secret-token-123",
        "program": "qodercn",
        "model": "unknown",
        "hub": "http://127.0.0.1:8765",
    }
    identity.update(overrides)
    path.write_text(json.dumps(identity), encoding="utf-8")
    os.chmod(path, 0o600)


def _claim(client, headers, identity_id=IDENTITY_ID, project_slug="demo"):
    return client.post(
        "/api/team-auth/local-identities/claim",
        headers=headers,
        json={"identity_id": identity_id, "project_slug": project_slug},
    )


def test_local_identities_lists_only_matching_safe_summaries(tmp_path, monkeypatch):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    _write_identity(project_dir / "qodercn--main.json")
    # 其他 Hub 的身份返回 eligible=false + hub_mismatch，而不是被静默过滤
    _write_identity(
        project_dir / "codex--main.json",
        name="codex-main",
        agent="codex",
        registration_token="other-token",
        hub="http://10.18.160.11:8765",
    )

    response = client.get("/api/team-auth/local-identities", headers=headers)
    assert response.status_code == 200
    identities = response.json()["identities"]
    assert [i["name"] for i in identities] == ["codex-main", "qodercn-main"]
    qodercn = next(i for i in identities if i["name"] == "qodercn-main")
    codex = next(i for i in identities if i["name"] == "codex-main")
    assert qodercn["eligible"] is True
    assert qodercn["identity_id"] == IDENTITY_ID
    assert codex["eligible"] is False
    assert codex["reason"] == "hub_mismatch"
    body = response.text
    assert "secret-token-123" not in body
    assert "other-token" not in body
    assert "registration_token" not in body


def test_local_identities_rejects_bad_json_and_broad_permissions(tmp_path, monkeypatch):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    (project_dir / "broken--main.json").write_text("{not json", encoding="utf-8")
    os.chmod(project_dir / "broken--main.json", 0o600)
    _write_identity(project_dir / "wide--main.json", name="wide-main")
    os.chmod(project_dir / "wide--main.json", 0o644)
    # 0700 也不允许（严格 0600）
    _write_identity(project_dir / "group--main.json", name="group-main")
    os.chmod(project_dir / "group--main.json", 0o700)

    response = client.get("/api/team-auth/local-identities", headers=headers)
    assert response.status_code == 200
    assert response.json()["identities"] == []


def test_local_identities_rejects_symlink_escape_and_non_matching_files(
    tmp_path, monkeypatch
):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    outside = tmp_path / "outside.json"
    _write_identity(outside, name="escape-main")
    (project_dir / "escape--main.json").symlink_to(outside)
    (project_dir / "not-a-registry.json").write_text("{}", encoding="utf-8")
    (project_dir / "README.md").write_text("hello", encoding="utf-8")
    # project_dir 本身是 symlink 也必须拒绝
    symlink_project = tmp_path / "registry" / "home-fyc-github-agent-cockpit-2"
    real_project = tmp_path / "real-project"
    real_project.mkdir()
    _write_identity(real_project / "kimi--main.json", name="kimi-main")
    symlink_project.symlink_to(real_project, target_is_directory=True)

    response = client.get("/api/team-auth/local-identities", headers=headers)
    assert response.status_code == 200
    assert response.json()["identities"] == []


def test_local_identities_requires_team_login(tmp_path, monkeypatch):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    _write_identity(project_dir / "qodercn--main.json")
    client.cookies.clear()

    response = client.get("/api/team-auth/local-identities", headers=headers)
    assert response.status_code == 401


def test_claim_identity_forwards_token_to_hub_and_never_leaks(tmp_path, monkeypatch):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    _write_identity(project_dir / "qodercn--main.json")

    calls = []
    monkeypatch.setattr(
        server.hub_client,
        "claim_agent",
        lambda **kwargs: calls.append(kwargs) or {
            "agent": {"name": kwargs["agent_name"], "id": 7}
        },
    )

    response = _claim(client, headers)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "agent": {"name": "qodercn-main", "id": 7}}
    assert calls[0]["registration_token"] == "secret-token-123"
    assert calls[0]["agent_name"] == "qodercn-main"
    assert calls[0]["project_slug"] == "demo"
    assert calls[0]["source_project_slug"] == "home-fyc-github-agent-cockpit"
    assert calls[0]["authorization"] == "Bearer human.jwt"
    assert "program" not in calls[0]
    assert "project_key" not in calls[0]
    assert "secret-token-123" not in response.text


def test_claim_identity_rejects_identity_with_invalid_project_slug(
    tmp_path, monkeypatch
):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    _write_identity(project_dir / "qodercn--main.json", project_slug="bad.slug")
    called = []
    monkeypatch.setattr(
        server.hub_client,
        "claim_agent",
        lambda **kwargs: called.append(kwargs) or {"agent": {"name": "x", "id": 1}},
    )

    response = _claim(client, headers)
    assert response.status_code == 404
    assert called == []
    assert "bad slug" not in response.text


def test_claim_identity_strips_token_from_hub_result(tmp_path, monkeypatch):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    _write_identity(project_dir / "qodercn--main.json")
    monkeypatch.setattr(
        server.hub_client,
        "claim_agent",
        lambda **kwargs: {"agent": {"name": "qodercn-main", "registration_token": "leak", "id": 1}},
    )

    response = _claim(client, headers)
    assert response.status_code == 200
    assert "leak" not in response.text
    assert "registration_token" not in response.text
    assert response.json()["agent"] == {"name": "qodercn-main", "id": 1}


def test_claim_identity_rejects_unknown_or_forged_id(tmp_path, monkeypatch):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    _write_identity(project_dir / "qodercn--main.json")
    monkeypatch.setattr(
        server.hub_client,
        "claim_agent",
        lambda **kwargs: {"agent": {"name": kwargs["agent_name"], "id": 7}},
    )

    for identity_id in (
        "../qodercn-main",
        "home-fyc-github-agent-cockpit/../x.json",
        "..",
        "no-such.json",
        "home-fyc-github-agent-cockpit/no-such--main.json",
    ):
        response = _claim(client, headers, identity_id=identity_id)
        assert response.status_code in (400, 404), identity_id
    assert _claim(client, headers).status_code == 200


def test_claim_identity_rejects_hub_mismatch_without_migration(tmp_path, monkeypatch):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    _write_identity(
        project_dir / "codex--main.json",
        name="codex-main",
        agent="codex",
        registration_token="other-token",
        hub="http://10.18.160.11:8765",
    )
    called = []
    monkeypatch.setattr(
        server.hub_client,
        "claim_agent",
        lambda **kwargs: called.append(kwargs) or {"agent": {}},
    )

    response = _claim(client, headers, identity_id="home-fyc-github-agent-cockpit/codex--main.json")
    assert response.status_code == 409
    assert "另一个 Hub" in response.text
    assert called == []
    assert "other-token" not in response.text


def test_claim_identity_requires_valid_project_slug(tmp_path, monkeypatch):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    _write_identity(project_dir / "qodercn--main.json")
    called = []
    monkeypatch.setattr(
        server.hub_client,
        "claim_agent",
        lambda **kwargs: called.append(kwargs) or {"agent": {"name": "x", "id": 1}},
    )

    for project_slug in ("../escape", "demo.project"):
        response = _claim(client, headers, project_slug=project_slug)
        assert response.status_code == 400
    assert called == []


def test_claim_identity_hub_error_uses_fixed_mapping_without_token(tmp_path, monkeypatch):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    _write_identity(project_dir / "qodercn--main.json")
    monkeypatch.setattr(
        server.hub_client,
        "claim_agent",
        lambda **kwargs: (_ for _ in ()).throw(
            server.hub_client.HumanAPIError(403, "Hub 拒绝认领")
        ),
    )

    response = _claim(client, headers)
    assert response.status_code == 403
    assert "Hub 拒绝认领" not in response.text
    assert "Hub 认领失败" in response.text
    assert "secret-token-123" not in response.text


def test_claim_identity_hub_detail_with_token_never_leaks(tmp_path, monkeypatch):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    _write_identity(project_dir / "qodercn--main.json")
    # 故意让上游 detail 包含 registration_token，断言 Cockpit 响应绝不回显
    monkeypatch.setattr(
        server.hub_client,
        "claim_agent",
        lambda **kwargs: (_ for _ in ()).throw(
            server.hub_client.HumanAPIError(
                500, f"上游异常 presented token secret-token-123"
            )
        ),
    )

    response = _claim(client, headers)
    assert response.status_code == 500
    assert "secret-token-123" not in response.text
    assert "上游异常" not in response.text
    assert "Hub 认领失败" in response.text


def test_claim_identity_requires_team_login(tmp_path, monkeypatch):
    server, client, headers, project_dir = _make_registry(tmp_path, monkeypatch)
    _write_identity(project_dir / "qodercn--main.json")
    client.cookies.clear()

    response = _claim(client, headers)
    assert response.status_code == 401
