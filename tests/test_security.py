import asyncio
import pytest
import threading
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server


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


def test_api_requires_auth_when_token_configured(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    response = TestClient(server.app).get("/api/files/roots")
    assert response.status_code == 401


def test_no_token_allows_loopback_and_rejects_lan_or_remote_client(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)

    local = TestClient(server.app, client=("127.0.0.1", 50000))
    assert local.get("/api/files/roots").status_code == 200
    for host in ("10.18.160.11", "172.16.0.8", "192.168.1.8", "192.0.2.10"):
        client = TestClient(server.app, client=(host, 50000))
        response = client.get("/api/files/roots")
        assert response.status_code == 403
        assert "COCKPIT_TOKEN" in response.json()["detail"]
        login = client.post("/api/auth/login", json={"token": ""})
        assert login.status_code == 403
        assert "COCKPIT_TOKEN" in login.json()["detail"]


def test_no_token_loopback_write_rejects_cross_origin(monkeypatch):
    """无 Token 模式:loopback 放行但非安全方法必须拒绝跨源 Origin。

    无 Token 时认证只看 client IP,恶意站点可诱导浏览器向 localhost
    发跨源 POST;必须对存在的 Origin 校验同源,同时保留无 Origin 的 CLI。
    """
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    client = TestClient(server.app, client=("127.0.0.1", 50000))

    # 无 Origin(CLI/curl)放行
    assert client.post("/api/auth/logout").status_code == 200
    # 同源 Origin 放行
    assert client.post(
        "/api/auth/logout", headers={"origin": "http://testserver"}
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


def test_start_agent_accepts_unique_local_name_for_same_runtime(monkeypatch, tmp_path):
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
    assert response.json()["label"] == "codex-2"
    assert calls[0][1] == {"layout": "right", "label": "codex-2", "args": ""}
    assert client.post(
        "/api/herdr/start",
        headers=headers,
        json={
            "session": "demo", "workdir": str(tmp_path), "agent": "codex",
            "name": "bad name",
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
    received = threading.Event()
    monkeypatch.setattr(server.terminal, "list_terms", lambda: [{"id": "term1"}])
    monkeypatch.setattr(server.terminal, "read_output", lambda *args: b"")
    monkeypatch.setattr(server.terminal, "is_alive", lambda *args: True)

    def record_write(term_id, text):
        written.append((term_id, text))
        received.set()

    monkeypatch.setattr(server.terminal, "write_term", record_write)
    client = TestClient(server.app)
    client.post("/api/auth/login", json={"token": "secret"})

    with client.websocket_connect(
        "/api/term/term1", headers={"origin": "http://testserver"}
    ) as websocket:
        websocket.send_text('{"not":"a resize"}')
        assert received.wait(2)

    assert written == [("term1", '{"not":"a resize"}')]


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
        assert websocket.receive_bytes() == b"tail"

    with client.websocket_connect(
        "/api/term/term1", headers={"origin": "http://testserver"}
    ) as websocket:
        assert websocket.receive_bytes() == b"tail"
