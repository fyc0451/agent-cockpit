"""Strict G3 HTTP boundary for Workspace-scoped managed agents."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import project_registry_api, workspace_agent


_AGENT_PATH_RE = re.compile(
    r"^/api/projects/[^/]+/workspaces/[^/]+/agents"
    r"(?:/[^/]+(?:/prompts)?)?$"
)
_STATUS = {
    "invalid_argument": 400,
    "idempotency_key_required": 400,
    "idempotency_conflict": 409,
    "project_or_workspace_not_found": 404,
    "agent_not_found": 404,
    "workspace_agent_unavailable": 503,
    "agent_start_failed": 503,
    "agent_start_cleanup_incomplete": 503,
    "agent_send_outcome_unknown": 409,
    "workspace_agent_cleanup_incomplete": 503,
}
_RETRYABLE = frozenset({
    "workspace_agent_unavailable", "workspace_agent_cleanup_incomplete",
    "agent_start_failed", "agent_start_cleanup_incomplete",
})


@dataclass(frozen=True)
class ApiService:
    controller_provider: Callable[[], workspace_agent.WorkspaceAgentController]

    def prepare(self) -> None:
        self.controller_provider()


def install(app: FastAPI, service: ApiService) -> None:
    @app.post(
        "/api/projects/{project_id}/workspaces/{workspace_id}/agents",
        status_code=201,
    )
    async def start(project_id: str, workspace_id: str, request: Request):
        async def operation():
            body = await _body(request, {"kind"})
            result = await run_in_threadpool(
                service.controller_provider().start,
                project_id, workspace_id, kind=body["kind"],
                idempotency_key=_idempotency_key(request),
            )
            return _success(request, result, status=201)
        return await _run(operation, request)

    @app.get(
        "/api/projects/{project_id}/workspaces/{workspace_id}/agents/{agent_id}",
    )
    def get(project_id: str, workspace_id: str, agent_id: str, request: Request):
        return _run_sync(
            lambda: _success(
                request,
                service.controller_provider().get(project_id, workspace_id, agent_id),
            ),
            request,
        )

    @app.post(
        "/api/projects/{project_id}/workspaces/{workspace_id}/agents/"
        "{agent_id}/prompts",
    )
    async def prompt(
        project_id: str, workspace_id: str, agent_id: str, request: Request,
    ):
        async def operation():
            body = await _body(request, {"prompt"})
            result = await run_in_threadpool(
                service.controller_provider().prompt,
                project_id, workspace_id, agent_id, prompt=body["prompt"],
                idempotency_key=_idempotency_key(request),
            )
            return _success(request, result)
        return await _run(operation, request)


def is_scoped_agent_path(path: str) -> bool:
    return isinstance(path, str) and _AGENT_PATH_RE.fullmatch(path) is not None


async def _body(request: Request, fields: set[str]) -> dict[str, Any]:
    try:
        value = json.loads(
            (await request.body()).decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise workspace_agent.WorkspaceAgentError("invalid_argument") from None
    if not isinstance(value, dict) or set(value) != fields:
        raise workspace_agent.WorkspaceAgentError("invalid_argument")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _idempotency_key(request: Request) -> str:
    value = request.headers.get("Idempotency-Key")
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise workspace_agent.WorkspaceAgentError("idempotency_key_required")
    return value


def _meta(request: Request) -> dict[str, object]:
    return {
        "request_id": project_registry_api.request_id_for(request),
        "generated_at": datetime.now(UTC).isoformat(),
        "partial": False,
        "sources": [{
            "name": "local_herdr", "status": "available",
            "observed_at": None, "reason": None,
        }],
        "warnings": [],
        "capabilities": {
            "workspaceAgent.read": {"available": True, "reason": None},
            "workspaceAgent.write": {"available": True, "reason": None},
            "remote_herdr": {
                "available": False, "reason": "deferred_after_local_core",
            },
        },
    }


def _success(
    request: Request, data: dict[str, object], *, status: int = 200,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"data": _public_agent(data), "meta": _meta(request)},
    )


def _public_agent(value: object) -> dict[str, object]:
    fields = (
        "agent_id", "project_id", "workspace_id", "kind", "status", "transcript",
    )
    if not isinstance(value, dict) or not all(
        isinstance(value.get(field), str) for field in fields
    ):
        raise workspace_agent.WorkspaceAgentError("workspace_agent_unavailable")
    if (
        re.fullmatch(r"i-[a-z2-7]{26}", value["agent_id"]) is None
        or re.fullmatch(r"prj_[0-9a-f]{32}", value["project_id"]) is None
        or re.fullmatch(r"ws_[0-9a-f]{32}", value["workspace_id"]) is None
        or value["kind"] not in workspace_agent.ALLOWED_KINDS
        or value["status"] not in {"idle", "working", "blocked", "done", "unknown"}
    ):
        raise workspace_agent.WorkspaceAgentError("workspace_agent_unavailable")
    return {field: value[field] for field in fields}


def _error(request: Request, code: str) -> JSONResponse:
    status = _STATUS.get(code, 500)
    public_code = code if code in _STATUS else "internal_error"
    return project_registry_api.g3_error(
        request, status=status, code=public_code,
        message=public_code.replace("_", " "),
        retryable=public_code in _RETRYABLE,
    )


async def _run(operation: Callable[[], Any], request: Request) -> JSONResponse:
    try:
        return await operation()
    except workspace_agent.WorkspaceAgentError as exc:
        return _error(request, exc.code)
    except Exception:
        return _error(request, "internal_error")


def _run_sync(operation: Callable[[], JSONResponse], request: Request) -> JSONResponse:
    try:
        return operation()
    except workspace_agent.WorkspaceAgentError as exc:
        return _error(request, exc.code)
    except Exception:
        return _error(request, "internal_error")
