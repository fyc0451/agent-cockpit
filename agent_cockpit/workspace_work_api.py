"""G3 HTTP boundary for Local Workspace Boss work items."""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import workspace_work_store as work_store


_STATUS = {
    "invalid_argument": 400,
    "idempotency_key_required": 400,
    "project_not_found": 404,
    "workspace_not_found": 404,
    "workspace_not_active": 409,
    "work_item_not_found": 404,
    "idempotency_conflict": 409,
    "stale_revision": 409,
    "stale_generation": 409,
    "claim_conflict": 409,
    "claim_not_active": 409,
    "reply_conflict": 409,
    "execution_terminal": 409,
    "workspace_work_schema_missing": 503,
    "migration_required": 503,
    "future_schema": 503,
    "schema_fingerprint_mismatch": 503,
    "store_unsafe": 503,
    "store_corrupt": 503,
    "store_read_failed": 503,
    "store_write_failed": 503,
}
_RETRYABLE = frozenset({"store_read_failed", "store_write_failed"})
_BODY_FIELDS = frozenset({"body", "acceptance", "constraints"})
_ROUTE = "/api/projects/{project_id}/workspaces/{workspace_id}/work-items"
_ITEM_ROUTE = _ROUTE + "/{work_item_id}"


class RegistryLookup(Protocol):
    def get_project_by_id(self, project_id: str) -> object | None: ...

    def get_workspace(
        self, project_id: str, workspace_id: str,
    ) -> object | None: ...


class ApiError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ApiService:
    registry_provider: Callable[[], RegistryLookup]
    store_provider: Callable[[], work_store.WorkspaceWorkStore]


def install(app: FastAPI, service: ApiService) -> None:
    @app.post(_ROUTE)
    async def create_work_item(
        project_id: str, workspace_id: str, request: Request,
    ):
        async def operation():
            payload = await _body(request)
            key = _idempotency_key(request)
            _require_scope(service, project_id, workspace_id, write=True)
            created = service.store_provider().create_work_item(
                project_id=project_id,
                workspace_id=workspace_id,
                body=payload["body"],
                acceptance=payload["acceptance"],
                constraints=payload["constraints"],
                idempotency_key=key,
            )
            return _success(request, created.item.public_dict(), status=201)
        return await _run_async(operation, request)

    @app.get(_ROUTE)
    def list_work_items(project_id: str, workspace_id: str, request: Request):
        def operation():
            if request.query_params:
                raise ApiError("invalid_argument")
            _require_scope(service, project_id, workspace_id, write=False)
            items = service.store_provider().list_work_items(
                project_id=project_id, workspace_id=workspace_id,
            )
            return _success(request, {
                "items": [item.public_dict() for item in items],
                "next_cursor": None,
            })
        return _run(operation, request)

    @app.get(_ITEM_ROUTE)
    def get_work_item_detail(
        project_id: str, workspace_id: str, work_item_id: str, request: Request,
    ):
        def operation():
            if request.query_params:
                raise ApiError("invalid_argument")
            _require_scope(service, project_id, workspace_id, write=False)
            detail = service.store_provider().get_work_item_detail(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            if detail is None:
                raise ApiError("work_item_not_found")
            return _success(request, detail)
        return _run(operation, request)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


async def _body(request: Request) -> dict[str, Any]:
    try:
        value = json.loads(
            (await request.body()).decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise ApiError("invalid_argument") from None
    if not isinstance(value, dict) or set(value) != _BODY_FIELDS:
        raise ApiError("invalid_argument")
    try:
        return {
            "body": work_store.body_text(value["body"]),
            "acceptance": work_store.note_text(value["acceptance"]),
            "constraints": work_store.note_text(value["constraints"]),
        }
    except work_store.WorkspaceWorkError as exc:
        if exc.code == "invalid_argument":
            raise ApiError("invalid_argument") from None
        raise


def _idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= 128
        or any(ord(char) < 33 or ord(char) == 127 for char in key)
    ):
        raise ApiError("idempotency_key_required")
    return key


def _project_record(snapshot: object) -> object:
    record = getattr(snapshot, "project", None)
    if record is None:
        raise ApiError("project_not_found")
    return record


def _require_scope(
    service: ApiService, project_id: str, workspace_id: str, *, write: bool,
) -> None:
    registry = service.registry_provider()
    snapshot = registry.get_project_by_id(project_id)
    if snapshot is None:
        raise ApiError("project_not_found")
    project = _project_record(snapshot)
    workspace = registry.get_workspace(project_id, workspace_id)
    if (
        workspace is None
        or getattr(workspace, "project_id", None) != project_id
        or getattr(workspace, "workspace_id", None) != workspace_id
    ):
        raise ApiError("workspace_not_found")
    if write and (
        getattr(project, "lifecycle", None) != "active"
        or getattr(workspace, "lifecycle", None) != "active"
    ):
        raise ApiError("workspace_not_active")


def _request_id(request: Request) -> str:
    current = getattr(request.state, "workspace_work_request_id", None)
    if isinstance(current, str) and current.startswith("req_"):
        return current
    value = "req_" + secrets.token_hex(16)
    request.state.workspace_work_request_id = value
    return value


def _meta(request: Request) -> dict[str, object]:
    return {
        "request_id": _request_id(request),
        "generated_at": datetime.now(UTC).isoformat(),
        "partial": False,
        "sources": [{
            "name": "workspace_work",
            "status": "available",
            "observed_at": None,
            "reason": None,
        }],
        "warnings": [],
        "capabilities": {
            "workspaceWork.read": {"available": True, "reason": None},
            "workspaceWork.write": {"available": True, "reason": None},
        },
    }


def _success(request: Request, data: object, *, status: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"data": data, "meta": _meta(request)},
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
    except work_store.WorkspaceWorkError as exc:
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
    except work_store.WorkspaceWorkError as exc:
        return _error(request, exc.code)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str):
            return _error(request, code)
        return _error(request, "internal_error")
