"""G3 HTTP boundary for Checkpoint B execution preparation."""
from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import git_checkout_provider as checkout_mod
from . import local_codex_harness as harness_mod
from . import workspace_execution_service as exec_service
from . import workspace_execution_store as exec_store
from . import workspace_work_store as work_store


_STATUS = {
    "invalid_argument": 400,
    "idempotency_key_required": 400,
    "project_not_found": 404,
    "workspace_not_found": 404,
    "work_item_not_found": 404,
    "identity_not_found": 404,
    "preparation_not_found": 404,
    "workspace_not_active": 409,
    "idempotency_conflict": 409,
    "source_not_git": 409,
    "source_dirty": 409,
    "checkout_conflict": 409,
    "lease_conflict": 409,
    "stale_revision": 409,
    "runtime_identity_unverified": 409,
    "process_exited": 409,
    "runtime_unavailable": 503,
    "workspace_execution_schema_missing": 503,
    "workspace_work_schema_missing": 503,
    "migration_required": 503,
    "future_schema": 503,
    "schema_fingerprint_mismatch": 503,
    "store_unsafe": 503,
    "store_corrupt": 503,
    "store_read_failed": 503,
    "store_write_failed": 503,
}
_RETRYABLE = frozenset({
    "store_read_failed", "store_write_failed", "runtime_unavailable",
})
_MEMBERS = "/api/projects/{project_id}/workspaces/{workspace_id}/members"
_PREP = (
    "/api/projects/{project_id}/workspaces/{workspace_id}"
    "/work-items/{work_item_id}/preparation"
)
_HIDDEN = frozenset({
    "internal_path", "fence_digest", "pane_id", "instance_id", "session_name",
    "native_receipt", "argv", "token", "cwd", "path",
})


class ApiError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def install(app: FastAPI, service: exec_service.ExecutionService) -> None:
    @app.get(_MEMBERS)
    def list_members(project_id: str, workspace_id: str, request: Request):
        def operation():
            if request.query_params:
                raise ApiError("invalid_argument")
            items = service.list_members(project_id, workspace_id)
            return _success(request, {
                "items": [item.public_dict() for item in items],
                "next_cursor": None,
            })
        return _run(operation, request)

    @app.post(_MEMBERS)
    async def create_member(project_id: str, workspace_id: str, request: Request):
        async def operation():
            payload = await _body(request, {"display_name"})
            key = _idempotency_key(request)
            created = service.create_member(
                project_id, workspace_id,
                display_name=payload["display_name"], idempotency_key=key,
            )
            return _success(request, created.item.public_dict(), status=201)
        return await _run_async(operation, request)

    @app.get(_PREP)
    def get_preparation(
        project_id: str, workspace_id: str, work_item_id: str, request: Request,
    ):
        def operation():
            if request.query_params:
                raise ApiError("invalid_argument")
            item = service.get_preparation(project_id, workspace_id, work_item_id)
            return _success(request, item.public_dict())
        return _run(operation, request)

    @app.post(_PREP)
    async def create_preparation(
        project_id: str, workspace_id: str, work_item_id: str, request: Request,
    ):
        async def operation():
            payload = await _body(request, {"identity_id"})
            key = _idempotency_key(request)
            created = service.prepare(
                project_id, workspace_id, work_item_id,
                identity_id=payload["identity_id"], idempotency_key=key,
            )
            return _success(request, created, status=201)
        return await _run_async(operation, request)

    @app.post(_PREP + "/attach")
    async def attach_runtime(
        project_id: str, workspace_id: str, work_item_id: str, request: Request,
    ):
        async def operation():
            payload = await _revision_body(request)
            key = _idempotency_key(request)
            item = service.attach(
                project_id, workspace_id, work_item_id,
                expected_revision=payload["expected_revision"],
                idempotency_key=key,
            )
            return _success(request, item)
        return await _run_async(operation, request)

    @app.post(_PREP + "/detach")
    async def detach_runtime(
        project_id: str, workspace_id: str, work_item_id: str, request: Request,
    ):
        async def operation():
            payload = await _revision_body(request)
            key = _idempotency_key(request)
            item = service.detach(
                project_id, workspace_id, work_item_id,
                expected_revision=payload["expected_revision"],
                idempotency_key=key,
            )
            return _success(request, item)
        return await _run_async(operation, request)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


async def _body(request: Request, fields: set[str]) -> dict[str, Any]:
    try:
        value = json.loads(
            (await request.body()).decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise ApiError("invalid_argument") from None
    if not isinstance(value, dict) or set(value) != fields:
        raise ApiError("invalid_argument")
    return value


async def _revision_body(request: Request) -> dict[str, Any]:
    value = await _body(request, {"expected_revision"})
    revision = value["expected_revision"]
    if type(revision) is not int or revision < 1:
        raise ApiError("invalid_argument")
    return value


def _idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= 128
        or any(ord(char) < 33 or ord(char) == 127 for char in key)
    ):
        raise ApiError("idempotency_key_required")
    return key


def _request_id(request: Request) -> str:
    current = getattr(request.state, "workspace_execution_request_id", None)
    if isinstance(current, str) and current.startswith("req_"):
        return current
    value = "req_" + secrets.token_hex(16)
    request.state.workspace_execution_request_id = value
    return value


def _meta(request: Request) -> dict[str, object]:
    return {
        "request_id": _request_id(request),
        "generated_at": datetime.now(UTC).isoformat(),
        "partial": False,
        "sources": [{
            "name": "workspace_execution",
            "status": "available",
            "observed_at": None,
            "reason": None,
        }],
        "warnings": [],
        "capabilities": {
            "workspaceExecution.read": {"available": True, "reason": None},
            "workspaceExecution.write": {"available": True, "reason": None},
        },
    }


def _scrub(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _scrub(item)
            for key, item in value.items()
            if key not in _HIDDEN
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _success(request: Request, data: object, *, status: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"data": _scrub(data), "meta": _meta(request)},
    )


def _error(request: Request, code: str) -> JSONResponse:
    public = code if code in _STATUS else "internal_error"
    return JSONResponse(status_code=_STATUS.get(public, 500), content={"error": {
        "code": public,
        "message": public.replace("_", " "),
        "retryable": public in _RETRYABLE,
        "request_id": _request_id(request),
        "details": {},
    }})


def _run(operation: Callable[[], JSONResponse], request: Request) -> JSONResponse:
    try:
        return operation()
    except ApiError as exc:
        return _error(request, exc.code)
    except (
        exec_service.ExecutionServiceError, exec_store.WorkspaceExecutionError,
        work_store.WorkspaceWorkError, checkout_mod.CheckoutError,
        harness_mod.HarnessError,
    ) as exc:
        return _error(request, exc.code)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str):
            return _error(request, code)
        return _error(request, "internal_error")


async def _run_async(operation, request: Request) -> JSONResponse:
    try:
        return await operation()
    except ApiError as exc:
        return _error(request, exc.code)
    except (
        exec_service.ExecutionServiceError, exec_store.WorkspaceExecutionError,
        work_store.WorkspaceWorkError, checkout_mod.CheckoutError,
        harness_mod.HarnessError,
    ) as exc:
        return _error(request, exc.code)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str):
            return _error(request, code)
        return _error(request, "internal_error")
