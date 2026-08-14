from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cockpit import server


ROOT = Path(__file__).resolve().parent.parent
STORE_NAMES = (
    "runtime_provider", "event_journal", "operation_journal",
    "project_memory", "terminal_ticket",
)
MODULE_NAMES = (
    "runtime_provider_store", "event_store", "operation_store",
    "memory_store", "terminal_ticket_store",
)


class FakeStore:
    def __init__(self, name: str, closed: list[str], *, fail_close: bool = False):
        self.name = name
        self.closed = closed
        self.fail_close = fail_close

    def close(self) -> None:
        self.closed.append(self.name)
        if self.fail_close:
            raise RuntimeError("private close detail")


@pytest.fixture(autouse=True)
def clear_bundle():
    server._close_foundation_stores()
    yield
    server._close_foundation_stores()


def _subprocess(source: str) -> dict[str, object]:
    environment = dict(os.environ)
    environment.pop("COCKPIT_NEXT_PROFILE", None)
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


def test_next_off_does_not_import_or_install_foundation_modules() -> None:
    result = _subprocess("""
import json, sys
from agent_cockpit import server
names = ('runtime_provider_store','event_store','operation_store','memory_store','terminal_ticket_store')
paths = sorted(route.path for route in server.app.routes)
print(json.dumps({
    'loaded': [name for name in names if f'agent_cockpit.{name}' in sys.modules],
    'foundation': [path for path in paths if path.startswith('/api/events/') or '/memory/' in path or path.startswith('/api/operations/') or '/terminal-tickets/' in path],
    'public': sorted(server.PUBLIC_PATHS),
    'api_routes': sum(path.startswith('/api/') for path in paths),
}))
""")
    assert result["loaded"] == []
    assert result["foundation"] == []
    assert result["public"] == [
        "/", "/api/agent/team-reply", "/api/auth/login", "/api/auth/status",
        "/health", "/health/live", "/health/ready",
    ]
    assert result["api_routes"] == 110
    result = _subprocess("""
import json
from agent_cockpit import server
print(json.dumps({'routes': len(server.app.routes)}))
""")
    assert result["routes"] == 124


def test_next_on_installs_only_accepted_reads_and_prestart_is_sanitized_503() -> None:
    result = _subprocess("""
import json
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute, APIWebSocketRoute
from agent_cockpit import instance_lock, next_profile
next_profile.enabled = lambda *args, **kwargs: True
instance_lock.require_registered_owner = lambda: object()
from agent_cockpit import server
http_routes = sorted(
    (route.path, sorted(route.methods or ())) for route in server.app.routes
    if isinstance(route, APIRoute) and "GET" in (route.methods or ()) and (
        route.path.startswith('/api/events/')
        or route.path.startswith('/api/operations/')
        or '/memory/' in route.path
        or '/terminal-tickets/' in route.path
    )
)
ws_routes = sorted(
    route.path for route in server.app.routes
    if isinstance(route, APIWebSocketRoute)
    if route.path.startswith('/api/events/')
    or route.path.startswith('/api/operations/')
    or '/memory/' in route.path
    or '/terminal-tickets/' in route.path
)
client = TestClient(server.app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000))
requests = (
    '/api/events/evt_missing',
    '/api/operations/op_missing',
    '/api/projects/prj_1/memory/summary',
    '/api/projects/prj_1/workspaces/ws_1/terminal-tickets/ttk_' + '0' * 32,
)
responses = [(response.status_code, response.json()) for response in map(client.get, requests)]
paths = [route.path for route in server.app.routes]
print(json.dumps({'http_routes': http_routes, 'ws_routes': ws_routes, 'responses': responses, 'paths': paths, 'public': sorted(server.PUBLIC_PATHS)}))
""")
    assert len(result["http_routes"]) == 7
    assert all(methods == ["GET"] for _path, methods in result["http_routes"])
    assert result["ws_routes"] == [
        "/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets/"
        "{ticket_id}/stream"
    ]
    assert all(status == 503 for status, _body in result["responses"])
    for _status, body in result["responses"]:
        assert body["error"]["code"] == "schema_missing"
        assert "private" not in json.dumps(body)
    assert not any(path.startswith("/api/runtime-provider") for path in result["paths"])
    assert result["public"] == sorted(server.PUBLIC_PATHS)


def _install_fake_initializers(
    monkeypatch: pytest.MonkeyPatch, fail_at: int | None,
) -> tuple[list[str], list[str]]:
    calls: list[str] = []
    closed: list[str] = []
    monkeypatch.setattr(
        server.runtime_paths,
        "validate_store",
        lambda name: calls.append(f"path:{name}") or Path(f"/{name}.sqlite3"),
    )
    for index, (module_name, store_name) in enumerate(zip(MODULE_NAMES, STORE_NAMES)):
        def initialize(_path, *, installed_at=None, i=index, name=store_name):
            assert server._foundation_bundle is None
            calls.append(f"init:{name}")
            if fail_at == i:
                raise RuntimeError("injected initializer failure")
            return FakeStore(name, closed)

        monkeypatch.setattr(
            server, module_name, SimpleNamespace(initialize=initialize), raising=False,
        )
    return calls, closed


@pytest.mark.parametrize("fail_at", range(5))
def test_each_initializer_failure_has_no_partial_publication_and_reverse_close(
    monkeypatch: pytest.MonkeyPatch, fail_at: int,
) -> None:
    calls, closed = _install_fake_initializers(monkeypatch, fail_at)
    with pytest.raises(RuntimeError, match="injected initializer failure"):
        server._initialize_foundation_stores()
    assert calls[:5] == [f"path:{name}" for name in STORE_NAMES]
    assert calls[5:] == [f"init:{name}" for name in STORE_NAMES[:fail_at + 1]]
    assert closed == list(reversed(STORE_NAMES[:fail_at]))
    assert server._foundation_bundle is None


def test_success_publishes_one_immutable_bundle_and_cleanup_isolated(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    calls, closed = _install_fake_initializers(monkeypatch, None)
    server._initialize_foundation_stores()
    bundle = server._foundation_bundle
    assert isinstance(bundle, tuple) and len(bundle) == 5
    assert calls[:5] == [f"path:{name}" for name in STORE_NAMES]
    bundle[3].fail_close = True
    server._close_foundation_stores()
    assert server._foundation_bundle is None
    assert closed == list(reversed(STORE_NAMES))
    assert "foundation store close failed" in caplog.text
    assert "private close detail" not in caplog.text


def test_post_init_startup_failure_closes_bundle_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        closed: list[str] = []

        def initialize() -> None:
            server._foundation_bundle = tuple(
                FakeStore(name, closed) for name in STORE_NAMES
            )

        monkeypatch.setattr(server.next_profile, "enabled", lambda: True)
        monkeypatch.setattr(server, "_require_next_instance_lock", lambda: None)
        monkeypatch.setattr(
            server, "project_registry_api_service",
            lambda: SimpleNamespace(prepare=lambda: None),
        )
        monkeypatch.setattr(server, "_initialize_foundation_stores", initialize)
        monkeypatch.setattr(server, "_h0_state_enabled", lambda: True)
        monkeypatch.setattr(
            server, "_open_state_clients",
            lambda: (_ for _ in ()).throw(RuntimeError("post-init failure")),
        )
        with pytest.raises(RuntimeError, match="post-init failure"):
            async with server.lifespan(server.app):
                pass
        assert closed == list(reversed(STORE_NAMES))
        assert server._foundation_bundle is None

    asyncio.run(run())


def test_repeated_lifespan_never_reuses_stale_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        cycles: list[tuple[FakeStore, ...]] = []
        closed: list[str] = []

        async def waiting_loop() -> None:
            await asyncio.Event().wait()

        def initialize() -> None:
            bundle = tuple(FakeStore(f"{len(cycles)}:{name}", closed) for name in STORE_NAMES)
            cycles.append(bundle)
            server._foundation_bundle = bundle

        monkeypatch.setattr(server.next_profile, "enabled", lambda: True)
        monkeypatch.setattr(server, "_require_next_instance_lock", lambda: None)
        monkeypatch.setattr(
            server, "project_registry_api_service",
            lambda: SimpleNamespace(prepare=lambda: None),
        )
        monkeypatch.setattr(server, "_initialize_foundation_stores", initialize)
        monkeypatch.setattr(server, "_h0_state_enabled", lambda: False)
        monkeypatch.setattr(server, "_b0_runtime_active", lambda: False)
        monkeypatch.setattr(server.b0_wiring, "uninstall_claim_gate", lambda: None)
        monkeypatch.setattr(server, "_poll_live_state", waiting_loop)
        monkeypatch.setattr(server, "_poll_message_state", waiting_loop)
        monkeypatch.setattr(server, "_worktree_cleanup_loop", waiting_loop)
        monkeypatch.setattr(server, "_identity_retirement_loop", waiting_loop)
        monkeypatch.setattr(server.tasks, "recover_pending_tasks", lambda: {"skipped": True})
        monkeypatch.setattr(server, "_release_all_zoom_leases", lambda: None)

        for expected_cycle in range(2):
            async with server.lifespan(server.app):
                assert server._foundation_bundle is cycles[expected_cycle]
            assert server._foundation_bundle is None
        assert len(cycles) == 2 and cycles[0] is not cycles[1]
        assert closed == [
            *(f"0:{name}" for name in reversed(STORE_NAMES)),
            *(f"1:{name}" for name in reversed(STORE_NAMES)),
        ]

    asyncio.run(run())
