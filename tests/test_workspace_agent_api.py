from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import workspace_agent, workspace_agent_api


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
AGENT = "i-" + "a" * 26
BASE = f"/api/projects/{PROJECT}/workspaces/{WORKSPACE}/agents"
PUBLIC = {
    "agent_id": AGENT,
    "project_id": PROJECT,
    "workspace_id": WORKSPACE,
    "kind": "codex",
    "status": "idle",
    "transcript": "hello",
}


class Controller:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.error: str | None = None
        self.include_internal = False
        self.raw_error: Exception | None = None

    def _result(self):
        if self.raw_error is not None:
            raise self.raw_error
        if self.error is not None:
            raise workspace_agent.WorkspaceAgentError(self.error)
        value = dict(PUBLIC)
        if self.include_internal:
            value.update({
                "session": "secret-session", "pane_id": "secret-pane",
                "workdir": "/private/repo", "error": "raw herdr error",
            })
        return value

    def start(self, project_id, workspace_id, *, kind, idempotency_key):
        self.calls.append(("start", project_id, workspace_id, kind, idempotency_key))
        return self._result()

    def get(self, project_id, workspace_id, agent_id):
        self.calls.append(("get", project_id, workspace_id, agent_id))
        return self._result()

    def prompt(self, project_id, workspace_id, agent_id, *, prompt, idempotency_key):
        self.calls.append((
            "prompt", project_id, workspace_id, agent_id, prompt, idempotency_key,
        ))
        return self._result()


def client():
    controller = Controller()
    app = FastAPI()
    workspace_agent_api.install(
        app, workspace_agent_api.ApiService(lambda: controller),
    )
    return TestClient(app), controller


def assert_g3(response, status: int = 200):
    assert response.status_code == status
    assert set(response.json()) == {"data", "meta"}
    assert response.json()["data"] == PUBLIC
    assert set(response.json()["data"]) == {
        "agent_id", "project_id", "workspace_id", "kind", "status", "transcript",
    }


def test_exact_routes_and_g3_success() -> None:
    web, controller = client()
    started = web.post(
        BASE,
        headers={"Idempotency-Key": "start"},
        json={"kind": "codex"},
    )
    assert_g3(started, 201)
    assert_g3(web.get(f"{BASE}/{AGENT}"))
    prompted = web.post(
        f"{BASE}/{AGENT}/prompts",
        headers={"Idempotency-Key": "prompt"},
        json={"prompt": "hello"},
    )
    assert_g3(prompted)
    assert controller.calls == [
        ("start", PROJECT, WORKSPACE, "codex", "start"),
        ("get", PROJECT, WORKSPACE, AGENT),
        ("prompt", PROJECT, WORKSPACE, AGENT, "hello", "prompt"),
    ]


def test_bodies_and_idempotency_headers_are_strict() -> None:
    web, controller = client()
    bad_requests = (
        web.post(BASE, json={"kind": "codex"}),
        web.post(BASE, headers={"Idempotency-Key": "k"}, json={}),
        web.post(
            BASE,
            headers={"Idempotency-Key": "k"},
            json={"kind": "codex", "workdir": "/private"},
        ),
        web.post(
            f"{BASE}/{AGENT}/prompts",
            headers={"Idempotency-Key": "k"},
            json={"prompt": "hello", "pane_id": "pane-secret"},
        ),
        *(
            web.post(
                BASE,
                headers={"Idempotency-Key": "k"},
                json={"kind": "codex", field: value},
            )
            for field, value in (
                ("session", "forbidden"),
                ("cwd", "/private"),
                ("pane_id", "pane-secret"),
                ("argv", ["--dangerous"]),
            )
        ),
        web.post(
            BASE,
            headers={
                "Idempotency-Key": "k", "Content-Type": "application/json",
            },
            content=b'{"kind":"codex","kind":"claude"}',
        ),
    )
    assert [item.status_code for item in bad_requests] == [400] * len(bad_requests)
    assert [item.json()["error"]["code"] for item in bad_requests] == [
        "idempotency_key_required", "invalid_argument",
        "invalid_argument", "invalid_argument",
        "invalid_argument", "invalid_argument", "invalid_argument",
        "invalid_argument", "invalid_argument",
    ]
    assert controller.calls == []


def test_errors_are_g3_and_provider_details_never_escape() -> None:
    web, controller = client()
    statuses = {
        "invalid_argument": (400, False),
        "idempotency_key_required": (400, False),
        "idempotency_conflict": (409, False),
        "project_or_workspace_not_found": (404, False),
        "agent_not_found": (404, False),
        "workspace_agent_unavailable": (503, True),
        "workspace_agent_cleanup_incomplete": (503, True),
        "agent_start_failed": (503, True),
        "agent_start_cleanup_incomplete": (503, True),
        "agent_send_outcome_unknown": (409, False),
    }
    assert set(workspace_agent_api._STATUS) == set(statuses)
    for code, (status, retryable) in statuses.items():
        controller.error = code
        response = web.get(f"{BASE}/{AGENT}")
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert response.json()["error"]["retryable"] is retryable
        assert set(response.json()["error"]) == {
            "code", "message", "retryable", "request_id", "details",
        }
        assert "/private" not in response.text

    controller.error = None
    controller.include_internal = True
    projected = web.get(f"{BASE}/{AGENT}")
    assert_g3(projected)
    assert "secret" not in projected.text and "/private" not in projected.text

    controller.include_internal = False
    controller.raw_error = RuntimeError("/private/raw herdr exception")
    failed = web.get(f"{BASE}/{AGENT}")
    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "internal_error"
    assert "/private" not in failed.text and "herdr" not in failed.text


def test_agent_path_classifier_is_exact() -> None:
    assert workspace_agent_api.is_scoped_agent_path(BASE)
    assert workspace_agent_api.is_scoped_agent_path(f"{BASE}/{AGENT}")
    assert workspace_agent_api.is_scoped_agent_path(f"{BASE}/{AGENT}/prompts")
    assert not workspace_agent_api.is_scoped_agent_path("/api/agents")
    assert not workspace_agent_api.is_scoped_agent_path(
        f"/api/projects/{PROJECT}/agents",
    )


def test_slow_start_runs_off_event_loop_and_other_endpoint_responds() -> None:
    class SlowController(Controller):
        def start(self, project_id, workspace_id, *, kind, idempotency_key):
            time.sleep(0.2)
            return super().start(
                project_id, workspace_id, kind=kind,
                idempotency_key=idempotency_key,
            )

    async def run() -> None:
        controller = SlowController()
        app = FastAPI()
        workspace_agent_api.install(
            app, workspace_agent_api.ApiService(lambda: controller),
        )

        @app.get("/ping")
        async def ping():
            return {"ok": True}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://ordinary.test",
        ) as web:
            slow = asyncio.create_task(web.post(
                BASE,
                headers={"Idempotency-Key": "slow"},
                json={"kind": "codex"},
            ))
            await asyncio.sleep(0.03)
            started = time.monotonic()
            response = await web.get("/ping")
            elapsed = time.monotonic() - started
            assert response.json() == {"ok": True}
            assert elapsed < 0.1
            assert (await slow).status_code == 201

    asyncio.run(run())
