import asyncio
import httpx
import re
import pytest
import threading
from pydantic import ValidationError
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import uvicorn

import server
from agent_cockpit import log_config


@pytest.mark.parametrize("model", ["-danger", "gpt 5", "gpt/5", "a" * 65, ""])
def test_start_task_request_rejects_invalid_model(model):
    with pytest.raises(ValidationError):
        server.StartTaskReq(workdir="/tmp", prompt="test", model=model)


@pytest.mark.parametrize("model", [None, "gpt-5", "gpt_5.1-codex"])
def test_start_task_request_accepts_supported_model_names(model):
    req = server.StartTaskReq(workdir="/tmp", prompt="test", model=model)
    assert req.model == model


def test_new_terminal_websocket_cancels_and_supersedes_old_reader():
    class FakeWebSocket:
        def __init__(self):
            self.closed = None

        async def close(self, **kwargs):
            self.closed = kwargs

    async def exercise():
        old_ws = FakeWebSocket()
        new_ws = FakeWebSocket()
        old_pump = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)
        old = {"websocket": old_ws, "pump_task": old_pump}
        server._TERM_WS_CONNECTIONS["term1"] = old
        try:
            new = await server._claim_term_websocket("term1", new_ws)
            assert old_pump.cancelled()
            assert old_ws.closed["code"] == server.TERM_WS_TAKEN_OVER_CODE
            assert server._term_websocket_is_current("term1", new)
            server._release_term_websocket("term1", old)
            assert server._term_websocket_is_current("term1", new)
            server._release_term_websocket("term1", new)
            assert "term1" not in server._TERM_WS_CONNECTIONS
        finally:
            server._TERM_WS_CONNECTIONS.pop("term1", None)

    asyncio.run(exercise())


def test_terminal_input_notes_are_coalesced_with_one_trailing_run(monkeypatch):
    """连续按键最多占一个线程；执行期间的新输入合并为一次尾随记录。"""
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fake_note(term_id):
        calls.append(term_id)
        started.set()
        assert release.wait(2)

    async def exercise():
        monkeypatch.setattr(server, "_TERM_INPUT_NOTE_TASKS", {})
        monkeypatch.setattr(server, "_TERM_INPUT_NOTE_PENDING", set())
        monkeypatch.setattr(server.terminal, "note_user_input", fake_note)

        for _ in range(64):
            server._schedule_term_input_note("term1")
        assert await asyncio.to_thread(started.wait, 1)
        for _ in range(64):
            server._schedule_term_input_note("term1")
        await asyncio.sleep(0)
        assert calls == ["term1"]

        task = server._TERM_INPUT_NOTE_TASKS["term1"]
        release.set()
        await asyncio.wait_for(task, 2)
        assert calls == ["term1", "term1"]
        assert server._TERM_INPUT_NOTE_TASKS == {}
        assert server._TERM_INPUT_NOTE_PENDING == set()

    asyncio.run(exercise())


def test_api_requires_auth_when_token_configured(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    response = TestClient(server.app).get("/api/files/roots")
    assert response.status_code == 401


def test_no_token_allows_loopback_and_rejects_lan_or_remote_client(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)

    local = TestClient(
        server.app,
        client=("127.0.0.1", 50000),
        headers={"host": "127.0.0.1"},
    )
    assert local.get("/api/files/roots").status_code == 200
    for host in ("10.18.160.11", "172.16.0.8", "192.168.1.8", "192.0.2.10"):
        client = TestClient(
            server.app,
            client=(host, 50000),
            headers={"host": "127.0.0.1"},
        )
        response = client.get("/api/files/roots")
        assert response.status_code == 403
        assert "COCKPIT_TOKEN" in response.json()["detail"]
        login = client.post("/api/auth/login", json={"token": ""})
        assert login.status_code == 403
        assert "COCKPIT_TOKEN" in login.json()["detail"]


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("localhost", "http://localhost"),
        ("LOCALHOST:8790", "http://localhost:8790"),
        ("127.0.0.1", "http://127.0.0.1"),
        ("127.20.30.40:8790", "http://127.20.30.40:8790"),
        ("[::1]", "http://[::1]"),
        ("[0:0:0:0:0:0:0:1]:8790", "http://[::1]:8790"),
        ("localhost", "http://localhost:80"),
        ("localhost:80", "http://localhost"),
    ],
)
def test_no_token_http_accepts_canonical_loopback_authorities(
    monkeypatch, host, origin,
):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    client = TestClient(server.app, client=("127.0.0.1", 50000))

    response = client.get(
        "/api/auth/status", headers={"host": host, "origin": origin},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("evil.example", None),
        ("localhost.example", None),
        ("192.168.1.8:8790", None),
        ("localhost:08790", None),
        ("localhost:", None),
        ("localhost:0", None),
        ("localhost:65536", None),
        ("user@localhost", None),
        ("::1", None),
        ("[", None),
        ("[::1", None),
        ("[]", None),
        ("[::1]]", None),
        ("localhost:8790", "http://localhost:8791"),
        ("localhost:8790", "https://localhost:8790"),
        ("localhost:8790", "https://evil.example"),
        ("localhost:8790", "null"),
        ("localhost:8790", "http://localhost:8790/path"),
        ("localhost:8790", "http://localhost:8790?"),
        ("localhost:8790", "http://localhost:8790#"),
        ("localhost:8790", "http://user@localhost:8790"),
    ],
)
def test_no_token_http_rejects_malformed_or_untrusted_authorities(
    monkeypatch, host, origin,
):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin

    response = TestClient(
        server.app, client=("127.0.0.1", 50000),
    ).get("/api/auth/status", headers=headers)

    assert response.status_code == 403


def test_no_token_http_rejects_missing_or_repeated_host_and_origin(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    assert not server._no_token_scope_trusted(
        {"scheme": "http", "headers": []}, require_origin=False,
    )
    client = TestClient(server.app, client=("127.0.0.1", 50000))

    duplicate_host = client.get(
        "/api/auth/status",
        headers=[("host", "127.0.0.1"), ("host", "evil.example")],
    )
    duplicate_origin = client.get(
        "/api/auth/status",
        headers=[
            ("host", "127.0.0.1"),
            ("origin", "http://127.0.0.1"),
            ("origin", "https://evil.example"),
        ],
    )

    assert duplicate_host.status_code == 403
    assert duplicate_origin.status_code == 403


def test_no_token_http_rejects_proxy_headers_instead_of_trusting_them(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    client = TestClient(server.app, client=("127.0.0.1", 50000))

    for header in (
        "forwarded", "x-forwarded-for", "x-forwarded-host",
        "x-forwarded-port", "x-forwarded-proto", "x-forwarded-client-cert",
        "x-forwarded-prefix", "x-real-ip",
    ):
        response = client.get(
            "/api/auth/status",
            headers={"host": "127.0.0.1", header: "127.0.0.1"},
        )
        assert response.status_code == 403, header


def test_no_token_host_gate_covers_public_health_and_static_entries(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    client = TestClient(server.app, client=("127.0.0.1", 50000))

    for path in ("/", "/static/missing", "/health/live"):
        rejected = client.get(path, headers={"host": "evil.example"})
        assert rejected.status_code == 403, path

        allowed = client.get(path, headers={"host": "127.0.0.1:8790"})
        assert allowed.status_code != 403, path


def test_no_token_closes_lead_http_reproductions(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    client = TestClient(server.app, client=("127.0.0.1", 50000))

    status = client.get("/api/auth/status", headers={"host": "evil.example"})
    logout = client.post(
        "/api/auth/logout",
        headers={"host": "evil.example", "origin": "http://evil.example"},
    )

    assert status.status_code == 403
    assert logout.status_code == 403


def test_token_mode_keeps_non_loopback_host_and_proxy_behavior(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    response = TestClient(server.app, client=("192.0.2.10", 50000)).get(
        "/api/auth/status",
        headers={
            "authorization": "Bearer secret",
            "host": "cockpit.example",
            "x-forwarded-host": "proxy.example",
        },
    )

    assert response.status_code == 200
    assert response.json()["authenticated"] is True


def test_token_mode_keeps_public_health_and_static_entries(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(
        server.app,
        client=("192.0.2.10", 50000),
        headers={"host": "cockpit.example"},
    )

    for path in ("/", "/static/missing", "/health/live"):
        assert client.get(path).status_code != 403, path


@pytest.mark.parametrize(("token", "proxy_headers"), [("", False), ("secret", True)])
def test_server_main_configures_proxy_headers_by_auth_mode(
    monkeypatch, token, proxy_headers,
):
    run_calls = []
    monkeypatch.setattr(server, "COCKPIT_TOKEN", token, raising=False)
    monkeypatch.setattr(
        server.next_profile, "validate_server_environment", lambda _root: None,
    )
    monkeypatch.setattr(
        log_config, "configure_logging", lambda **_kwargs: "INFO",
    )
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: run_calls.append(kwargs))
    monkeypatch.setenv("COCKPIT_HOST", "127.0.0.1")
    monkeypatch.setenv("COCKPIT_PORT", "18790")

    assert server.main() == 0
    assert run_calls == [{
        "host": "127.0.0.1",
        "port": 18790,
        "proxy_headers": proxy_headers,
        "log_level": "info",
        "log_config": None,
        "access_log": False,
    }]


def test_no_token_loopback_write_rejects_cross_origin(monkeypatch):
    """无 Token 模式:loopback 放行但非安全方法必须拒绝跨源 Origin。

    无 Token 时认证只看 client IP,恶意站点可诱导浏览器向 localhost
    发跨源 POST;必须对存在的 Origin 校验同源,同时保留无 Origin 的 CLI。
    """
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    client = TestClient(
        server.app,
        client=("127.0.0.1", 50000),
        headers={"host": "127.0.0.1"},
    )

    # 无 Origin(CLI/curl)放行
    assert client.post("/api/auth/logout").status_code == 200
    # 同源 Origin 放行
    assert client.post(
        "/api/auth/logout", headers={"origin": "http://127.0.0.1"}
    ).status_code == 200
    # 跨源 Origin 拒绝(CSRF 防护)
    assert client.post(
        "/api/auth/logout", headers={"origin": "https://evil.example"}
    ).status_code == 403


def test_login_sets_session_cookie(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)

    assert client.post("/api/auth/login", json={"token": "wrong"}).status_code == 401
    response = client.post("/api/auth/login", json={"token": "secret"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "secret" not in cookie
    assert client.get("/api/files/roots").status_code == 200


def test_cookie_authenticated_write_requires_same_origin(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    client.post("/api/auth/login", json={"token": "secret"})

    assert client.post("/api/auth/logout").status_code == 403
    assert client.post(
        "/api/auth/logout", headers={"origin": "http://testserver"}
    ).status_code == 200


def test_bearer_authenticated_write_does_not_require_origin(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    response = TestClient(server.app).post(
        "/api/tasks/missing/apply",
        headers={"authorization": "Bearer secret"},
    )

    assert response.status_code == 400


def test_websocket_rejects_cross_origin(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    client.post("/api/auth/login", json={"token": "secret"})

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/term/missing", headers={"origin": "https://evil.example"}
        ):
            pass


def test_websocket_rejects_missing_auth(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)

    with pytest.raises(WebSocketDisconnect):
        with TestClient(server.app).websocket_connect(
            "/api/term/missing", headers={"origin": "http://testserver"}
        ):
            pass


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("localhost:8790", "http://localhost:8790"),
        ("127.0.0.1", "http://127.0.0.1"),
        ("[::1]:8790", "http://[0:0:0:0:0:0:0:1]:8790"),
    ],
)
def test_no_token_websocket_accepts_loopback_host_and_origin(
    monkeypatch, host, origin,
):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    monkeypatch.setattr(server.terminal, "list_terms", lambda: [])
    client = TestClient(server.app, client=("127.0.0.1", 50000))

    with client.websocket_connect(
        "/api/term/missing", headers={"host": host, "origin": origin},
    ) as websocket:
        assert "终端会话不存在" in websocket.receive_text()


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "evil.example", "origin": "http://evil.example"},
        {"host": "localhost:8790"},
        {"host": "localhost:8790", "origin": "https://localhost:8790"},
        {"host": "localhost:8790", "origin": "http://localhost:8791"},
        {
            "host": "localhost:8790",
            "origin": "http://localhost:8790",
            "x-forwarded-host": "localhost:8790",
        },
    ],
)
def test_no_token_websocket_rejects_untrusted_handshakes(monkeypatch, headers):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    client = TestClient(server.app, client=("127.0.0.1", 50000))

    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/api/term/missing", headers=headers):
            pass

    assert closed.value.code == 1008


def test_no_token_websocket_rejects_repeated_origin(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    headers = httpx.Headers(
        [
            ("host", "localhost:8790"),
            ("origin", "http://localhost:8790"),
            ("origin", "http://localhost:8790"),
        ]
    )

    with pytest.raises(WebSocketDisconnect) as closed:
        with TestClient(
            server.app, client=("127.0.0.1", 50000),
        ).websocket_connect("/api/term/missing", headers=headers):
            pass

    assert closed.value.code == 1008


def test_websocket_marks_missing_terminal_as_non_reconnectable(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.terminal, "list_terms", lambda: [])
    client = TestClient(server.app)
    client.post("/api/auth/login", json={"token": "secret"})

    with client.websocket_connect(
        "/api/term/missing", headers={"origin": "http://testserver"}
    ) as websocket:
        assert "终端会话不存在" in websocket.receive_text()
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()

    assert closed.value.code == server.TERM_WS_INVALID_CODE


def test_websocket_marks_replaced_terminal_as_taken_over(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.terminal, "list_terms", lambda: [])
    monkeypatch.setattr(server.terminal, "was_superseded", lambda term_id: True)
    client = TestClient(server.app)
    client.post("/api/auth/login", json={"token": "secret"})

    with client.websocket_connect(
        "/api/term/replaced", headers={"origin": "http://testserver"}
    ) as websocket:
        assert "更新的页面接管" in websocket.receive_text()
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()

    assert closed.value.code == server.TERM_WS_TAKEN_OVER_CODE


def test_non_loopback_bind_requires_token(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    for host in ("0.0.0.0", "10.18.160.11", "172.16.0.8", "192.168.1.8"):
        with pytest.raises(RuntimeError, match="COCKPIT_TOKEN"):
            server._validate_bind(host)

    server._validate_bind("127.0.0.1")


def test_non_loopback_bind_warns_about_plain_http(monkeypatch, caplog):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)

    server._validate_bind("0.0.0.0")

    assert "HTTPS" in caplog.text


def test_setup_workspace_rejects_shell_metacharacters(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    response = TestClient(server.app).post(
        "/api/herdr/setup-workspace",
        headers={"authorization": "Bearer secret"},
        json={
            "session": "bad;touch-pwned",
            "workdir": "/tmp",
            "agents": ["codex"],
        },
    )

    assert response.status_code == 400


def test_herdr_routes_reject_option_like_identifiers(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}

    assert client.post(
        "/api/herdr/session/--all/stop", headers=headers
    ).status_code == 400
    assert client.post(
        "/api/herdr/pane/good/--help/send",
        headers=headers,
        json={"text": "x", "mode": "send"},
    ).status_code == 400


def test_herdr_pane_send_rejects_unknown_mode(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    response = TestClient(server.app).post(
        "/api/herdr/pane/good/w1:p1/send",
        headers={"authorization": "Bearer secret"},
        json={"text": "x", "mode": "unexpected"},
    )

    assert response.status_code == 400


def test_start_agent_accepts_display_name_and_generates_opaque_instance(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server, "_agent_mail_requirement", lambda: None)
    calls = []
    monkeypatch.setattr(
        server.herdr_client,
        "start_agent",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "available": True, "pane_id": "w1:p3", "label": kwargs.get("label"),
        },
    )
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}

    response = client.post(
        "/api/herdr/start",
        headers=headers,
        json={
            "session": "demo", "workdir": str(tmp_path), "agent": "codex",
            "name": "codex-2", "layout": "right",
        },
    )

    assert response.status_code == 200
    body = response.json()
    kwargs = dict(calls[0][1])
    assert body["label"] == "codex-2"
    assert body["instance_id"] == kwargs.pop("instance_id")
    assert re.fullmatch(r"i-[a-z2-7]{26}", body["instance_id"])
    assert kwargs == {"layout": "right", "label": "codex-2", "args": ""}
    for display_name in ("bad name", "Codex-2", "夜班 负责人"):
        assert client.post(
            "/api/herdr/start",
            headers=headers,
            json={
                "session": "demo", "workdir": str(tmp_path), "agent": "codex",
                "name": display_name,
            },
        ).status_code == 200
    for invalid_name in ("bad\nname", "x" * 65):
        assert client.post(
            "/api/herdr/start",
            headers=headers,
            json={
                "session": "demo", "workdir": str(tmp_path), "agent": "codex",
                "name": invalid_name,
            },
        ).status_code == 400
    assert client.post(
        "/api/herdr/start",
        headers=headers,
        json={
            "session": "demo", "workdir": str(tmp_path), "agent": "codex",
            "name": "codex-2", "workspace": "unexpected",
        },
    ).status_code == 400
    assert client.post(
        "/api/herdr/start",
        headers=headers,
        json={
            "session": "demo", "workdir": str(tmp_path), "agent": "codex",
            "workspace": "isolated",
        },
    ).status_code == 400


def test_herdr_read_routes_reject_unbounded_line_counts(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}

    assert client.get(
        "/api/herdr/pane/demo/w1:p1?lines=0", headers=headers
    ).status_code == 422
    assert client.get(
        "/api/herdr/pane/demo/w1:p1?lines=1001", headers=headers
    ).status_code == 422
    assert client.get(
        "/api/herdr/pane/demo/w1:p1/summary?max_lines=0", headers=headers
    ).status_code == 422
    assert client.get(
        "/api/herdr/pane/demo/w1:p1/summary?max_lines=201", headers=headers
    ).status_code == 422


def test_terminal_create_maps_validation_and_limit_errors(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}

    monkeypatch.setattr(
        server.terminal, "create_term", lambda *args: (_ for _ in ()).throw(ValueError("bad dims"))
    )
    assert client.post("/api/term", headers=headers).status_code == 400

    monkeypatch.setattr(
        server.terminal, "create_term", lambda *args: (_ for _ in ()).throw(RuntimeError("limit"))
    )
    assert client.post("/api/term", headers=headers).status_code == 429


def test_terminal_create_can_atomically_replace_same_label(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}
    called = []

    def replace(cwd, cols, rows, label):
        called.append((cwd, cols, rows, label))
        return {"id": "replacement", "pid": 123, "label": label}

    monkeypatch.setattr(server.terminal, "replace_labeled_term", replace)
    response = client.post(
        "/api/term?label=demo&replace_existing=true", headers=headers
    )

    assert response.status_code == 200
    assert response.json()["id"] == "replacement"
    assert called == [(None, 80, 24, "demo")]

    called.clear()
    response = client.post("/api/term?label=legacy-page", headers=headers)
    assert response.status_code == 200
    assert called == [(None, 80, 24, "legacy-page")]


def test_websocket_forwards_non_control_json_input(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    written = []
    noted = []
    received = threading.Event()
    monkeypatch.setattr(server.terminal, "list_terms", lambda: [{"id": "term1"}])
    monkeypatch.setattr(server.terminal, "read_output", lambda *args: b"")
    monkeypatch.setattr(server.terminal, "is_alive", lambda *args: True)

    def record_write(term_id, text):
        written.append((term_id, text))
        received.set()

    monkeypatch.setattr(server.terminal, "write_term", record_write)
    monkeypatch.setattr(
        server.terminal, "note_user_input", lambda term_id: noted.append(term_id),
    )
    client = TestClient(server.app)
    client.post("/api/auth/login", json={"token": "secret"})

    with client.websocket_connect(
        "/api/term/term1", headers={"origin": "http://testserver"}
    ) as websocket:
        websocket.send_text('{"not":"a resize"}')
        assert received.wait(2)

    assert written == [("term1", '{"not":"a resize"}')]
    assert noted == ["term1"]


def test_websocket_drains_tail_before_exit_message(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.terminal, "list_terms", lambda: [{"id": "term1"}])
    monkeypatch.setattr(server.terminal, "read_output", lambda *args: b"")
    monkeypatch.setattr(server.terminal, "is_alive", lambda *args: False)
    monkeypatch.setattr(server.terminal, "drain_output", lambda *args: b"last output")
    client = TestClient(server.app)
    client.post("/api/auth/login", json={"token": "secret"})

    with client.websocket_connect(
        "/api/term/term1", headers={"origin": "http://testserver"}
    ) as websocket:
        assert websocket.receive_bytes() == b"last output"
        assert "进程已退出" in websocket.receive_text()


def test_websocket_replays_history_only_for_fresh_xterm(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.terminal, "list_terms", lambda: [{"id": "term1"}])
    monkeypatch.setattr(server.terminal, "output_history", lambda term_id: b"screen")
    monkeypatch.setattr(server.terminal, "read_available", lambda *args: b"")
    monkeypatch.setattr(server.terminal, "is_alive", lambda *args: False)
    monkeypatch.setattr(server.terminal, "drain_output", lambda *args: b"tail")
    client = TestClient(server.app)
    client.post("/api/auth/login", json={"token": "secret"})

    with client.websocket_connect(
        "/api/term/term1?replay=1", headers={"origin": "http://testserver"}
    ) as websocket:
        assert websocket.receive_bytes() == b"screen"
        assert websocket.receive_json() == {"type": "replay_complete"}
        assert websocket.receive_bytes() == b"tail"

    with client.websocket_connect(
        "/api/term/term1", headers={"origin": "http://testserver"}
    ) as websocket:
        assert websocket.receive_bytes() == b"tail"


def test_websocket_completes_empty_replay(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server.terminal, "list_terms", lambda: [{"id": "term1"}])
    monkeypatch.setattr(server.terminal, "output_history", lambda term_id: b"")
    monkeypatch.setattr(server.terminal, "read_available", lambda *args: b"")
    monkeypatch.setattr(server.terminal, "is_alive", lambda *args: False)
    monkeypatch.setattr(server.terminal, "drain_output", lambda *args: b"")
    client = TestClient(server.app)
    client.post("/api/auth/login", json={"token": "secret"})

    with client.websocket_connect(
        "/api/term/term1?replay=1", headers={"origin": "http://testserver"}
    ) as websocket:
        assert websocket.receive_json() == {"type": "replay_complete"}


# ── P1：服务端会话（logout 吊销 / 多会话 / 时效 / 非 ASCII 稳定 4xx）──────

def _fresh_registry():
    """每个用例独立注册表，避免跨用例会话泄漏。"""
    registry = server.SessionRegistry()
    monkey_target = server._auth_sessions
    return registry, monkey_target


def test_logout_revokes_replayed_cookie(monkeypatch):
    """P1 核心：logout 后重放旧 cookie 必须 401，不能只靠 delete_cookie。"""
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    registry, _ = _fresh_registry()
    monkeypatch.setattr(server, "_auth_sessions", registry)
    login = TestClient(server.app)
    resp = login.post("/api/auth/login", json={"token": "secret"})
    assert resp.status_code == 200
    cookie_value = resp.cookies[server.AUTH_COOKIE]

    # 显式携带该 cookie logout（同源 Origin 满足 CSRF）
    logout = TestClient(server.app, cookies={server.AUTH_COOKIE: cookie_value})
    assert logout.post(
        "/api/auth/logout", headers={"origin": "http://testserver"}
    ).status_code == 200

    # 重放旧 cookie：必须 401
    replay = TestClient(server.app, cookies={server.AUTH_COOKIE: cookie_value})
    assert replay.get("/api/files/roots").status_code == 401


def test_multiple_browser_sessions_logout_revokes_only_current(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    registry, _ = _fresh_registry()
    monkeypatch.setattr(server, "_auth_sessions", registry)
    a = TestClient(server.app).post("/api/auth/login", json={"token": "secret"}).cookies[server.AUTH_COOKIE]
    b = TestClient(server.app).post("/api/auth/login", json={"token": "secret"}).cookies[server.AUTH_COOKIE]
    assert a != b
    # 会话 A 登出
    out = TestClient(server.app, cookies={server.AUTH_COOKIE: a})
    assert out.post("/api/auth/logout", headers={"origin": "http://testserver"}).status_code == 200
    # A 失效，B 仍有效
    assert TestClient(server.app, cookies={server.AUTH_COOKIE: a}).get("/api/files/roots").status_code == 401
    assert TestClient(server.app, cookies={server.AUTH_COOKIE: b}).get("/api/files/roots").status_code == 200


def test_login_cookie_carries_finite_max_age(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    registry, _ = _fresh_registry()
    monkeypatch.setattr(server, "_auth_sessions", registry)
    resp = TestClient(server.app).post("/api/auth/login", json={"token": "secret"})
    cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert f"Max-Age={server.DEFAULT_SESSION_TTL_SECONDS}" in cookie
    assert "secret" not in cookie


def test_non_ascii_bearer_stable_401(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    # 以原始 UTF-8 字节发送（真实线路形态；httpx str 头拒绝非 ASCII）
    for bad in (
        "Bearer ünïcødé".encode("utf-8"),
        "Bearer 令牌".encode("utf-8"),
        b"Bearer \xc3\x28invalid",
    ):
        resp = client.get("/api/files/roots", headers={"authorization": bad})
        assert resp.status_code == 401, bad


def test_non_ascii_login_token_stable_401(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    # 非 ASCII 经原始 JSON 字节；lone surrogate 经 \ud800 转义（服务端解码后
    # str 无法 utf-8 编码，必须被捕获为 401 而非 500）
    bodies = [
        '{"token": "ünïcødé"}'.encode("utf-8"),
        '{"token": "令牌错误的值"}'.encode("utf-8"),
        b'{"token": "\ud800"}',
    ]
    for bad in bodies:
        resp = client.post(
            "/api/auth/login", content=bad,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 401, bad
        assert "secret" not in resp.text


def test_non_ascii_cookie_stable_401(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    raw = f"{server.AUTH_COOKIE}=ünïcødé-令牌".encode("utf-8")
    client = TestClient(server.app, headers={"cookie": raw})
    assert client.get("/api/files/roots").status_code == 401


def test_bearer_priority_over_cookie(monkeypatch):
    """Bearer 优先策略保留：有效 Bearer + 无效 cookie 仍认证成功。"""
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app, cookies={server.AUTH_COOKIE: "garbage"})
    resp = client.get(
        "/api/files/roots", headers={"authorization": "Bearer secret"}
    )
    assert resp.status_code == 200


def test_cookie_write_csrf_and_websocket_same_origin_preserved(monkeypatch):
    """回归：cookie 写需同源；WS 同源放行（走服务端会话注册表路径）。"""
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    registry, _ = _fresh_registry()
    monkeypatch.setattr(server, "_auth_sessions", registry)
    client = TestClient(server.app)
    client.post("/api/auth/login", json={"token": "secret"})
    assert client.post("/api/auth/logout").status_code == 403
    assert client.post(
        "/api/auth/logout", headers={"origin": "http://evil.example"}
    ).status_code == 403
