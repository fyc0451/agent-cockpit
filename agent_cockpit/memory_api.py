"""Injectable G3 read boundary for the dormant Project Memory store."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import memory_store


_STATUS = {
    "invalid_argument": 400,
    "schema_missing": 503,
    "schema_fingerprint_mismatch": 503,
    "future_schema": 503,
    "store_corrupt": 503,
    "store_read_failed": 503,
    "store_unsafe": 503,
}
_RETRYABLE = frozenset({"store_read_failed"})
_REQUEST_ID_STATE = "memory_request_id"


@dataclass(frozen=True)
class MemoryApiService:
    store_provider: Callable[[], memory_store.MemoryStore]


def install(app: FastAPI, service: MemoryApiService) -> None:
    @app.get("/api/projects/{project_id}/memory/summary")
    def summary(project_id: str, request: Request) -> JSONResponse:
        def operation() -> JSONResponse:
            _only_query(request, set())
            value = service.store_provider().summary(project_id)
            return _success(value.public_dict(), request)

        return _run(operation, request)

    @app.get("/api/projects/{project_id}/memory/facts")
    def facts(project_id: str, request: Request) -> JSONResponse:
        def operation() -> JSONResponse:
            _only_query(request, {"status", "after_key", "limit"})
            items, cursor = service.store_provider().list_facts(
                project_id=project_id,
                statuses=tuple(request.query_params.getlist("status")),
                after_key=_single(request, "after_key"),
                limit=_integer(_single(request, "limit"), default=50, minimum=1),
            )
            return _success({
                "items": [item.public_dict() for item in items],
                "next_cursor": cursor,
            }, request)

        return _run(operation, request)

    @app.get("/api/projects/{project_id}/memory/candidates")
    def candidates(project_id: str, request: Request) -> JSONResponse:
        def operation() -> JSONResponse:
            _only_query(
                request, {"status", "after_candidate_id", "limit"},
            )
            items, cursor = service.store_provider().list_candidates(
                project_id=project_id,
                statuses=tuple(request.query_params.getlist("status")),
                after_candidate_id=_single(request, "after_candidate_id"),
                limit=_integer(_single(request, "limit"), default=50, minimum=1),
            )
            return _success({
                "items": [item.public_dict() for item in items],
                "next_cursor": cursor,
            }, request)

        return _run(operation, request)

    @app.get("/api/projects/{project_id}/memory/timeline")
    def timeline(project_id: str, request: Request) -> JSONResponse:
        def operation() -> JSONResponse:
            _only_query(request, {"after_seq", "limit"})
            items, cursor = service.store_provider().timeline(
                project_id=project_id,
                after_seq=_integer(
                    _single(request, "after_seq"), default=0, minimum=0,
                ),
                limit=_integer(_single(request, "limit"), default=50, minimum=1),
            )
            return _success({
                "items": [item.public_dict() for item in items],
                "next_cursor": cursor,
            }, request)

        return _run(operation, request)


def _only_query(request: Request, allowed: set[str]) -> None:
    if any(key not in allowed for key in request.query_params.keys()):
        raise memory_store.MemoryStoreError("invalid_argument")


def _single(request: Request, key: str) -> str | None:
    values = request.query_params.getlist(key)
    if len(values) > 1:
        raise memory_store.MemoryStoreError("invalid_argument")
    return values[0] if values else None


def _integer(
    value: str | None, *, default: int, minimum: int,
) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise memory_store.MemoryStoreError("invalid_argument") from None
    if parsed < minimum or parsed > memory_store.MAX_SQLITE_INTEGER:
        raise memory_store.MemoryStoreError("invalid_argument")
    if str(parsed) != value:
        raise memory_store.MemoryStoreError("invalid_argument")
    return parsed


def _meta(request: Request) -> dict[str, object]:
    return {
        "request_id": _request_id(request),
        "generated_at": datetime.now(UTC).isoformat(),
        "partial": False,
        "sources": [{
            "name": "project_memory",
            "status": "available",
            "observed_at": None,
            "reason": None,
        }],
        "warnings": [],
        "capabilities": {
            "memory.read": {"available": True, "reason": None},
            "memory.write": {
                "available": False,
                "reason": "authenticated_human_boundary_deferred",
            },
            "memory.checkpoint": {
                "available": False,
                "reason": "deferred_after_memory_core",
            },
            "memory.contextPack": {
                "available": False,
                "reason": "deferred_after_checkpoint",
            },
        },
    }


def _request_id(request: Request) -> str:
    current = getattr(request.state, _REQUEST_ID_STATE, None)
    if isinstance(current, str) and current.startswith("req_"):
        return current
    value = "req_" + secrets.token_hex(16)
    setattr(request.state, _REQUEST_ID_STATE, value)
    return value


def _success(data: object, request: Request) -> JSONResponse:
    return JSONResponse({"data": data, "meta": _meta(request)})


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
    except memory_store.MemoryStoreError as exc:
        return _error(request, exc.code)
    except Exception:
        return _error(request, "internal_error")
