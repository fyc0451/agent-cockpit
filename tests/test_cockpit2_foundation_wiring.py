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
    server._close_workspace_work_store()
    server._close_workspace_execution_store()
    yield
    server._close_foundation_stores()
    server._close_workspace_work_store()
    server._close_workspace_execution_store()


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
names = (
    'runtime_provider_store','event_store','operation_store','memory_store',
    'terminal_ticket_store','workspace_work_store','workspace_work_api',
    'workspace_execution_store','workspace_execution_api',
    'workspace_execution_service',
)
paths = sorted(route.path for route in server.app.routes)
print(json.dumps({
    'loaded': [name for name in names if f'agent_cockpit.{name}' in sys.modules],
    'foundation': [path for path in paths if path.startswith('/api/events/') or '/memory/' in path or path.startswith('/api/operations/') or '/terminal-tickets/' in path],
    'work_items': [path for path in paths if path.endswith('/work-items')],
    'execution': [path for path in paths if '/workspaces/' in path and (path.endswith('/members') or path.endswith('/preparation') or path.endswith('/preparation/attach') or path.endswith('/preparation/detach'))],
    'legacy_pane_send': any(path == '/api/herdr/pane/{session}/{pane_id}/send' for path in paths),
    'public': sorted(server.PUBLIC_PATHS),
    'api_routes': sum(path.startswith('/api/') for path in paths),
}))
""")
    assert result["loaded"] == []
    assert result["foundation"] == []
    assert result["work_items"] == []
    assert result["execution"] == []
    assert result["legacy_pane_send"] is True
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
work_items = sorted(
    (route.path, sorted(route.methods or ()))
    for route in server.app.routes
    if isinstance(route, APIRoute) and route.path.endswith('/work-items')
)
print(json.dumps({
    'http_routes': http_routes, 'ws_routes': ws_routes, 'responses': responses,
    'paths': paths, 'public': sorted(server.PUBLIC_PATHS), 'work_items': work_items,
    'scoped': server._scoped_g3_path(
        '/api/projects/prj_' + 'a' * 32 + '/workspaces/ws_' + 'b' * 32 + '/work-items'
    ),
    'execution': sorted(
        (route.path, sorted(route.methods or ()))
        for route in server.app.routes
        if isinstance(route, APIRoute) and (
            route.path.endswith('/members')
            or route.path.endswith('/preparation')
            or route.path.endswith('/preparation/attach')
            or route.path.endswith('/preparation/detach')
        )
    ),
    'legacy_pane_send': any(
        getattr(route, 'path', '') == '/api/herdr/pane/{session}/{pane_id}/send'
        for route in server.app.routes
    ),
    'prompt_scoped': server._scoped_g3_path(
        '/api/projects/prj_' + 'a' * 32 + '/workspaces/ws_' + 'b' * 32
        + '/agents/agt_' + 'c' * 32 + '/prompts'
    ),
}))
""")
    assert len(result["http_routes"]) == 7
    assert all(methods == ["GET"] for _path, methods in result["http_routes"])
    assert result["ws_routes"] == [
        "/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets/"
        "{ticket_id}/stream"
    ]
    assert result["work_items"] == [
        [
            "/api/projects/{project_id}/workspaces/{workspace_id}/work-items",
            ["GET"],
        ],
        [
            "/api/projects/{project_id}/workspaces/{workspace_id}/work-items",
            ["POST"],
        ],
    ]
    assert result["scoped"] is True
    assert all(status == 503 for status, _body in result["responses"])
    for _status, body in result["responses"]:
        assert body["error"]["code"] == "schema_missing"
        assert "private" not in json.dumps(body)
    assert not any(path.startswith("/api/runtime-provider") for path in result["paths"])
    assert result["public"] == sorted(server.PUBLIC_PATHS)
    assert result["legacy_pane_send"] is True
    assert result["prompt_scoped"] is True
    project = "{project_id}"
    workspace = "{workspace_id}"
    work_item = "{work_item_id}"
    assert result["execution"] == [
        [f"/api/projects/{project}/workspaces/{workspace}/members", ["GET"]],
        [f"/api/projects/{project}/workspaces/{workspace}/members", ["POST"]],
        [
            f"/api/projects/{project}/workspaces/{workspace}/work-items/"
            f"{work_item}/preparation",
            ["GET"],
        ],
        [
            f"/api/projects/{project}/workspaces/{workspace}/work-items/"
            f"{work_item}/preparation",
            ["POST"],
        ],
        [
            f"/api/projects/{project}/workspaces/{workspace}/work-items/"
            f"{work_item}/preparation/attach",
            ["POST"],
        ],
        [
            f"/api/projects/{project}/workspaces/{workspace}/work-items/"
            f"{work_item}/preparation/detach",
            ["POST"],
        ],
    ]


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
        monkeypatch.setattr(server, "_initialize_workspace_work_store", lambda: None)
        monkeypatch.setattr(
            server, "_initialize_workspace_execution_store", lambda: None,
        )
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
        monkeypatch.setattr(server, "_initialize_workspace_work_store", lambda: None)
        monkeypatch.setattr(
            server, "_initialize_workspace_execution_store", lambda: None,
        )
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


def test_server_post_get_and_restart_restores_same_work_item_ids() -> None:
    result = _subprocess("""
import json, os, tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from agent_cockpit import instance_lock, next_profile, runtime_paths

root = Path(tempfile.mkdtemp())
for name in ('data', 'config', 'state', 'uploads'):
    (root / name).mkdir()
os.environ['COCKPIT_DATA_DIR'] = str(root / 'data')
os.environ['COCKPIT_CONFIG_DIR'] = str(root / 'config')
os.environ['COCKPIT_STATE_DIR'] = str(root / 'state')
os.environ['COCKPIT_UPLOADS_DIR'] = str(root / 'uploads')
os.environ['COCKPIT_TOKEN'] = ''
os.environ['COCKPIT_EDITION'] = 'source'
os.environ.pop('COCKPIT_COORDINATION_DB', None)
runtime_paths.reset_cache()
next_profile.enabled = lambda *args, **kwargs: True
instance_lock.require_registered_owner = lambda: object()
from agent_cockpit import server
server._require_next_instance_lock = lambda: None
client_kwargs = {'base_url': 'http://127.0.0.1', 'client': ('127.0.0.1', 50000)}

def seed(client):
    registry = server._project_registry()
    project = registry.create_project(slug='alpha', display_name='Alpha', goal=None)
    location = registry.add_repo_location(
        project_id=project.project_id, node_id='local',
        canonical_path='/repo/alpha', vcs_kind='none', availability='available',
    )
    workspace = registry.create_workspace(
        project_id=project.project_id,
        repo_location_id=location.repo_location_id,
        name='main', goal=None, isolation_kind='shared',
    )
    return project.project_id, workspace.workspace_id

with TestClient(server.app, **client_kwargs) as client:
    project_id, workspace_id = seed(client)
    url = f'/api/projects/{project_id}/workspaces/{workspace_id}/work-items'
    created = client.post(
        url,
        json={'body': 'Persist this Boss question', 'acceptance': None, 'constraints': None},
        headers={'Idempotency-Key': 'save-1'},
    )
    listed = client.get(url)
    store_path = str(runtime_paths.store('workspace_work'))
    bundle_len = len(server._foundation_bundle or ())
    work_id = id(server._workspace_work_store)

with TestClient(server.app, **client_kwargs) as client:
    restored = client.get(url)
    restored_work_id = id(server._workspace_work_store)
    same_path = str(runtime_paths.store('workspace_work')) == store_path

print(json.dumps({
    'created_status': created.status_code,
    'listed_status': listed.status_code,
    'restored_status': restored.status_code,
    'created': created.json(),
    'listed': listed.json(),
    'restored': restored.json(),
    'store_leaf': Path(store_path).name,
    'bundle_len': bundle_len,
    'same_path': same_path,
    'rebuilt_store': work_id != restored_work_id,
    'created_text': created.text,
    'restored_text': restored.text,
}))
""")
    created = result["created"]
    listed = result["listed"]
    restored = result["restored"]
    assert result["created_status"] == 201
    assert result["listed_status"] == 200
    assert result["restored_status"] == 200
    item = created["data"]
    assert set(item) == {"thread", "root_message", "work_item"}
    assert listed["data"] == {"items": [item], "next_cursor": None}
    assert restored["data"] == listed["data"]
    assert item["thread"]["thread_id"] == restored["data"]["items"][0]["thread"]["thread_id"]
    assert item["root_message"]["message_id"] == (
        restored["data"]["items"][0]["root_message"]["message_id"]
    )
    assert item["work_item"]["work_item_id"] == (
        restored["data"]["items"][0]["work_item"]["work_item_id"]
    )
    assert result["store_leaf"] == "workspace-work.sqlite3"
    assert result["bundle_len"] == 5
    assert result["same_path"] is True
    assert result["rebuilt_store"] is True
    assert "workspace-work.sqlite3" not in result["created_text"]
    assert "workspace-work.sqlite3" not in result["restored_text"]
    assert "/tmp/" not in result["created_text"]
    assert "/tmp/" not in result["restored_text"]


def test_server_members_post_get_and_restart_restores_same_identity_ids() -> None:
    result = _subprocess("""
import json, os, tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from agent_cockpit import instance_lock, next_profile, runtime_paths

root = Path(tempfile.mkdtemp())
for name in ('data', 'config', 'state', 'uploads'):
    (root / name).mkdir()
os.environ['COCKPIT_DATA_DIR'] = str(root / 'data')
os.environ['COCKPIT_CONFIG_DIR'] = str(root / 'config')
os.environ['COCKPIT_STATE_DIR'] = str(root / 'state')
os.environ['COCKPIT_UPLOADS_DIR'] = str(root / 'uploads')
os.environ['COCKPIT_TOKEN'] = ''
os.environ['COCKPIT_EDITION'] = 'source'
os.environ['COCKPIT_NEXT_PROFILE'] = next_profile.FIXED_PROFILE
os.environ['HERDR_SESSION'] = next_profile.SESSION
os.environ.pop('COCKPIT_COORDINATION_DB', None)
runtime_paths.reset_cache()
next_profile.enabled = lambda *args, **kwargs: True
instance_lock.require_registered_owner = lambda: object()
from agent_cockpit import server
server._require_next_instance_lock = lambda: None
client_kwargs = {'base_url': 'http://127.0.0.1', 'client': ('127.0.0.1', 50000)}

with TestClient(server.app, **client_kwargs) as client:
    registry = server._project_registry()
    project = registry.create_project(slug='alpha', display_name='Alpha', goal=None)
    location = registry.add_repo_location(
        project_id=project.project_id, node_id='local',
        canonical_path='/repo/alpha', vcs_kind='none', availability='available',
    )
    workspace = registry.create_workspace(
        project_id=project.project_id,
        repo_location_id=location.repo_location_id,
        name='main', goal=None, isolation_kind='shared',
    )
    url = f'/api/projects/{project.project_id}/workspaces/{workspace.workspace_id}/members'
    created = client.post(
        url, json={'display_name': 'Atlas'}, headers={'Idempotency-Key': 'member-1'},
    )
    listed = client.get(url)
    store_path = str(runtime_paths.store('workspace_execution'))
    bundle_len = len(server._foundation_bundle or ())

with TestClient(server.app, **client_kwargs) as client:
    restored = client.get(url)

print(json.dumps({
    'created_status': created.status_code,
    'listed_status': listed.status_code,
    'restored_status': restored.status_code,
    'created': created.json(),
    'listed': listed.json(),
    'restored': restored.json(),
    'store_leaf': Path(store_path).name,
    'bundle_len': bundle_len,
    'created_text': created.text,
    'restored_text': restored.text,
}))
""")
    assert result["created_status"] == 201
    assert result["listed_status"] == 200
    assert result["restored_status"] == 200
    item = result["created"]["data"]
    assert item["display_name"] == "Atlas"
    identity_id = item["identity_id"]
    assert result["listed"]["data"]["items"][0]["identity_id"] == identity_id
    assert result["restored"]["data"]["items"][0]["identity_id"] == identity_id
    assert result["store_leaf"] == "workspace-execution.sqlite3"
    assert result["bundle_len"] == 5
    assert "workspace-execution.sqlite3" not in result["created_text"]
    assert "workspace-execution.sqlite3" not in result["restored_text"]
    for hidden in ("fence_digest", "pane_id", "internal_path", "argv"):
        assert hidden not in result["created_text"]
        assert hidden not in result["restored_text"]


def test_next_on_disables_old_agent_prompt_and_keeps_legacy_pane_send() -> None:
    result = _subprocess("""
import json
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
from agent_cockpit import instance_lock, next_profile
next_profile.enabled = lambda *args, **kwargs: True
instance_lock.require_registered_owner = lambda: object()
from agent_cockpit import server
prompt = (
    '/api/projects/prj_' + 'a' * 32 + '/workspaces/ws_' + 'b' * 32
    + '/agents/agt_' + 'c' * 32 + '/prompts'
)
client = TestClient(server.app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000))
response = client.post(prompt, json={'prompt': 'do not send'})
paths = [
    route.path for route in server.app.routes
    if isinstance(route, APIRoute)
]
print(json.dumps({
    'status': response.status_code,
    'body': response.json(),
    'text': response.text,
    'legacy_pane_send': '/api/herdr/pane/{session}/{pane_id}/send' in paths,
    'legacy_tasks': any(path.startswith('/api/tasks') for path in paths),
}))
""")
    assert result["status"] in {403, 404}
    error = result["body"].get("error") or {}
    assert error.get("code") in {"capability_unavailable", "not_found"}
    assert "do not send" not in result["text"]
    assert "pane_send" not in result["text"]
    assert result["legacy_pane_send"] is True
    assert result["legacy_tasks"] is True


def _isolate_execution_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from agent_cockpit import runtime_paths

    for name in ("data", "config", "state", "uploads"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("COCKPIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COCKPIT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("COCKPIT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COCKPIT_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("COCKPIT_COORDINATION_DB", raising=False)
    runtime_paths.reset_cache()


def test_execution_service_injects_fixed_and_ephemeral_herdr_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from agent_cockpit import next_profile

    _isolate_execution_roots(monkeypatch, tmp_path)
    server._initialize_workspace_execution_store()
    monkeypatch.setattr(server, "_operation_journal_provider", lambda: object())

    monkeypatch.setenv(next_profile.PROFILE_ENV, next_profile.FIXED_PROFILE)
    monkeypatch.setenv("HERDR_SESSION", next_profile.SESSION)
    server._workspace_execution_service = None
    fixed = server._require_workspace_execution_service()
    assert fixed.session_name == next_profile.SESSION
    assert next_profile.require_session(fixed.session_name) == next_profile.SESSION
    with pytest.raises(next_profile.NextProfileError) as forbidden:
        next_profile.require_session("cockpit-b-readonly")
    assert forbidden.value.args[0] == "next_session_forbidden"

    token = "75b93b24959cf098c5c6a0f11ebbaff5"
    ephemeral = f"ephemeral-{token}"
    monkeypatch.setenv(next_profile.PROFILE_ENV, next_profile.EPHEMERAL_PROFILE)
    monkeypatch.setenv(next_profile.EPHEMERAL_READY_TOKEN_ENV, token)
    monkeypatch.setenv("HERDR_SESSION", ephemeral)
    server._workspace_execution_service = None
    live = server._require_workspace_execution_service()
    assert live.session_name == ephemeral
    assert next_profile.require_session(live.session_name) == ephemeral
    with pytest.raises(next_profile.NextProfileError) as wrong_fixed:
        next_profile.require_session(next_profile.SESSION)
    assert wrong_fixed.value.args[0] == "next_session_forbidden"


def test_execution_session_fail_closed_off_profile_and_wrong_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_cockpit import next_profile

    monkeypatch.delenv(next_profile.PROFILE_ENV, raising=False)
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    with pytest.raises(next_profile.NextProfileError) as off:
        server._workspace_execution_session_name()
    assert off.value.args[0] == "next_session_forbidden"

    monkeypatch.setenv(next_profile.PROFILE_ENV, next_profile.FIXED_PROFILE)
    monkeypatch.setenv("HERDR_SESSION", "cockpit-b-readonly")
    with pytest.raises(next_profile.NextProfileError) as invalid:
        server._workspace_execution_session_name()
    assert invalid.value.args[0] == "next_profile_invalid:HERDR_SESSION"
    monkeypatch.setenv("HERDR_SESSION", next_profile.SESSION)
    with pytest.raises(next_profile.NextProfileError) as rejected:
        next_profile.require_session("cockpit-b-readonly")
    assert rejected.value.args[0] == "next_session_forbidden"
