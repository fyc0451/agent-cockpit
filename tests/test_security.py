import pytest
import threading
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server


def test_api_requires_auth_when_token_configured(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    response = TestClient(server.app).get("/api/files/roots")
    assert response.status_code == 401


def test_no_token_allows_loopback_and_rejects_remote_client(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)

    local = TestClient(server.app, client=("127.0.0.1", 50000))
    remote = TestClient(server.app, client=("192.0.2.10", 50000))
    assert local.get("/api/files/roots").status_code == 200
    assert remote.get("/api/files/roots").status_code == 403


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


def test_non_loopback_bind_requires_token(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    with pytest.raises(RuntimeError):
        server._validate_bind("0.0.0.0")

    server._validate_bind("127.0.0.1")


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
