from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import server
from agent_cockpit import workspace_terminal


ROOT = Path(__file__).resolve().parent.parent


class Controller:
    def list_tickets(self, _project, _workspace, _cursor):
        return {"items": [], "next_cursor": None}

    def get_ticket(self, project, workspace, ticket):
        return {"ticket": {"project_id": project, "workspace_id": workspace, "ticket_id": ticket}}


def _client():
    app = FastAPI()
    workspace_terminal.install(
        app, workspace_terminal.ApiService(lambda: Controller(), lambda _websocket: False),
    )
    return TestClient(app)


def test_collection_is_g3_and_create_body_is_exact_before_controller_call():
    http = _client()
    response = http.get("/api/projects/prj_" + "a" * 32 + "/workspaces/ws_" + "b" * 32 + "/terminal-tickets")
    assert set(response.json()) == {"data", "meta"}
    response = http.post(
        "/api/projects/prj_" + "a" * 32 + "/workspaces/ws_" + "b" * 32 + "/terminal-tickets",
        json={"revision": 1, "cols": 80, "rows": 24, "cwd": "/forbidden"},
        headers={"Idempotency-Key": "create"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_argument"


def test_controller_owns_exactly_one_workspace_ticket_detail_route():
    http = _client()
    routes = [
        route for route in http.app.routes
        if getattr(route, "path", "").endswith("/terminal-tickets/{ticket_id}")
        and "GET" in getattr(route, "methods", set())
    ]
    assert len(routes) == 1
    response = http.get(
        "/api/projects/prj_" + "a" * 32
        + "/workspaces/ws_" + "b" * 32
        + "/terminal-tickets/ttk_" + "c" * 32,
    )
    assert response.status_code == 200
    assert response.json()["data"]["ticket"]["ticket_id"] == "ttk_" + "c" * 32


def test_stream_close_codes_use_the_contract_closed_set():
    assert workspace_terminal._stream_code("invalid_argument") == 4400
    assert workspace_terminal._stream_code("terminal_ticket_not_found") == 4404
    assert workspace_terminal._stream_code("revision_conflict") == 4409
    assert workspace_terminal._stream_code("terminal_process_unknown") == 4503
    assert workspace_terminal._stream_code("workspace_terminal_unavailable") == 4503


def _websocket(*, origin, host, peer="127.0.0.1", cookies=None):
    headers = {"host": host}
    raw_headers = [(b"host", host.encode("ascii"))]
    if origin is not None:
        headers["origin"] = origin
        raw_headers.append((b"origin", origin.encode("ascii")))
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers=headers,
        cookies=cookies or {},
        scope={"type": "websocket", "scheme": "ws", "headers": raw_headers},
    )


def test_workspace_stream_no_token_requires_legacy_ws_trust(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "")
    assert server._websocket_trusted(_websocket(
        origin="https://evil.example", host="127.0.0.1:18790",
    )) is False
    assert server._websocket_trusted(_websocket(
        origin=None, host="127.0.0.1:18790",
    )) is False
    assert server._websocket_trusted(_websocket(
        origin="http://evil.example", host="evil.example:18790",
    )) is False


def test_workspace_stream_no_token_accepts_matching_loopback_origin(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "")
    assert server._websocket_trusted(_websocket(
        origin="http://127.0.0.1:18790", host="127.0.0.1:18790",
    )) is True


def test_workspace_stream_token_mode_rejects_cross_origin(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "test-token")
    assert server._websocket_trusted(_websocket(
        origin="https://evil.example", host="127.0.0.1:18790",
        cookies={server.AUTH_COOKIE: server._session_value()},
    )) is False


def _next_server_subprocess(source: str) -> dict[str, object]:
    environment = dict(os.environ)
    environment.pop("COCKPIT_NEXT_PROFILE", None)
    environment.pop("COCKPIT_TOKEN", None)
    environment["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_workspace_stream_actual_server_wiring_uses_trusted_authorizer():
    result = _next_server_subprocess("""
import json
from fastapi.routing import APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from agent_cockpit import instance_lock, next_profile

next_profile.enabled = lambda: True
instance_lock.require_registered_owner = lambda: object()
from agent_cockpit import server

stream_path = (
    '/api/projects/prj_' + 'a' * 32
    + '/workspaces/ws_' + 'b' * 32
    + '/terminal-tickets/ttk_' + 'c' * 32 + '/stream'
)
ws_url = 'ws://127.0.0.1:14321' + stream_path
routes = [
    route.path for route in server.app.routes
    if isinstance(route, APIWebSocketRoute) and route.path == (
        '/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets/'
        '{ticket_id}/stream'
    )
]
client = TestClient(
    server.app,
    base_url='http://127.0.0.1:14321',
    client=('127.0.0.1', 50000),
)

def rejected(origin):
    headers = {} if origin is None else {'origin': origin}
    try:
        with client.websocket_connect(ws_url, headers=headers):
            raise AssertionError('unexpected websocket accept')
    except WebSocketDisconnect as exc:
        return exc.code

with client.websocket_connect(
    ws_url,
    headers={'origin': 'http://127.0.0.1:14321'},
) as websocket:
    websocket.send_text('not-json-garbage')
    error = websocket.receive_json()
    close_message = websocket.receive()
    assert close_message['type'] == 'websocket.close'
    assert close_message['code'] == 4400

print(json.dumps({
    'routes': routes,
    'evil_close': rejected('https://evil.example'),
    'missing_close': rejected(None),
    'matching_error': error,
    'matching_close': close_message,
}))
""")
    assert result["routes"] == [
        "/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets/"
        "{ticket_id}/stream",
    ]
    assert result["evil_close"] == 1008
    assert result["missing_close"] == 1008
    assert result["matching_error"] == {"type": "error", "code": "invalid_argument"}
    assert result["matching_close"]["type"] == "websocket.close"
    assert result["matching_close"]["code"] == 4400
