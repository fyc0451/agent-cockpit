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


ROOT = Path(__file__).resolve().parents[1]


def test_next_server_installs_agent_routes_and_g3_scope() -> None:
    environment = dict(os.environ)
    environment.pop("COCKPIT_NEXT_PROFILE", None)
    environment["PYTHONPATH"] = str(ROOT)
    source = """
import json
from fastapi.routing import APIRoute
from agent_cockpit import instance_lock, next_profile
next_profile.enabled = lambda *args, **kwargs: True
instance_lock.require_registered_owner = lambda: object()
from agent_cockpit import server
paths = sorted(
    (route.path, sorted(route.methods or ()))
    for route in server.app.routes
    if isinstance(route, APIRoute) and '/agents' in route.path
)
sample = '/api/projects/prj_' + 'a' * 32 + '/workspaces/ws_' + 'b' * 32 + '/agents'
print(json.dumps({
    'paths': paths,
    'scoped': server._scoped_g3_path(sample),
    'controller': server.workspace_agent_controller,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    value = json.loads(result.stdout)
    project = "{project_id}"
    workspace = "{workspace_id}"
    agent = "{agent_id}"
    assert value == {
        "paths": [
            [f"/api/projects/{project}/workspaces/{workspace}/agents", ["POST"]],
            [f"/api/projects/{project}/workspaces/{workspace}/agents/{agent}", ["GET"]],
            [
                f"/api/projects/{project}/workspaces/{workspace}/agents/{agent}/prompts",
                ["POST"],
            ],
        ],
        "scoped": True,
        "controller": None,
    }


def _lifespan_dependencies(monkeypatch, events, agent_controller):
    class TerminalError(RuntimeError):
        pass

    class AgentError(RuntimeError):
        pass

    class TerminalController:
        def __init__(self, **_kwargs):
            events.append("terminal:create")

        def close(self):
            events.append("terminal:close")

        def ready(self):
            return True

    monkeypatch.setattr(server.next_profile, "enabled", lambda: True)
    monkeypatch.setattr(server.next_profile, "is_ephemeral", lambda: True)
    monkeypatch.setattr(server, "_require_next_instance_lock", lambda: None)
    monkeypatch.setattr(
        server, "project_registry_api_service",
        lambda: SimpleNamespace(prepare=lambda: events.append("registry:prepare")),
    )
    monkeypatch.setattr(
        server, "_initialize_foundation_stores",
        lambda: events.append("foundation:create"),
    )
    monkeypatch.setattr(
        server, "_close_foundation_stores",
        lambda: events.append("foundation:close"),
    )
    monkeypatch.setattr(
        server.next_profile, "finalize_ephemeral_runtime_root",
        lambda: events.append("ephemeral:finalize"),
    )
    monkeypatch.setattr(
        server, "workspace_terminal",
        SimpleNamespace(
            WorkspaceTerminalController=TerminalController,
            WorkspaceTerminalError=TerminalError,
        ),
    )
    monkeypatch.setattr(
        server, "workspace_agent",
        SimpleNamespace(
            WorkspaceAgentController=agent_controller,
            WorkspaceAgentError=AgentError,
        ),
    )
    server.workspace_terminal_controller = None
    server.workspace_agent_controller = None


def test_agent_lifespan_orders_publication_close_and_repeated_cycles(
    monkeypatch,
) -> None:
    events: list[str] = []
    created: list[object] = []

    class AgentController:
        def __init__(self, **_kwargs):
            events.append("agent:create")
            created.append(self)
            self.closed = False

        def ready(self):
            return not self.closed

        def close(self):
            self.closed = True
            events.append("agent:close")

    _lifespan_dependencies(monkeypatch, events, AgentController)

    async def run():
        for cycle in range(2):
            async with server.lifespan(server.app):
                assert server.workspace_agent_controller is created[cycle]
                assert server._workspace_agent_provider() is created[cycle]
                assert server.workspace_terminal_controller is not None
            assert server.workspace_agent_controller is None
            assert server.workspace_terminal_controller is None

    asyncio.run(run())
    assert len(created) == 2 and created[0] is not created[1]
    assert events == [
        "registry:prepare", "foundation:create", "terminal:create",
        "agent:create", "agent:close", "terminal:close", "foundation:close",
        "ephemeral:finalize",
        "registry:prepare", "foundation:create", "terminal:create",
        "agent:create", "agent:close", "terminal:close", "foundation:close",
        "ephemeral:finalize",
    ]


def test_agent_lifespan_initializer_failure_has_no_partial_publication(
    monkeypatch,
) -> None:
    events: list[str] = []

    class FailingAgentController:
        def __init__(self, **_kwargs):
            events.append("agent:create:failed")
            raise RuntimeError("private agent init detail")

    _lifespan_dependencies(monkeypatch, events, FailingAgentController)

    async def run():
        with pytest.raises(RuntimeError, match="private agent init detail"):
            async with server.lifespan(server.app):
                raise AssertionError("lifespan must not publish")

    asyncio.run(run())
    assert server.workspace_agent_controller is None
    assert server.workspace_terminal_controller is None
    assert events == [
        "registry:prepare", "foundation:create", "terminal:create",
        "agent:create:failed", "terminal:close", "foundation:close",
        "ephemeral:finalize",
    ]
