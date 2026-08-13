"""Read-only G3 boundary for the dormant Operation Journal v1."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import operation_store


@dataclass(frozen=True)
class ApiService:
    store_provider: Callable[[], operation_store.OperationStore]


_STATUS = {
    "invalid_argument": 400,
    "operation_not_found": 404,
    "schema_missing": 503,
    "migration_required": 503,
    "future_schema": 503,
    "schema_fingerprint_mismatch": 503,
    "store_corrupt": 503,
    "store_unsafe": 503,
    "store_read_failed": 503,
}
_RETRYABLE = frozenset({"schema_missing", "migration_required", "store_read_failed"})


def install(app: FastAPI, service: ApiService) -> None:
    @app.get("/api/operations/{operation_id}")
    def get_operation(operation_id: str, request: Request):
        def read():
            value = service.store_provider().get_operation(operation_id)
            if value is None:
                raise operation_store.OperationError("operation_not_found")
            return JSONResponse({"data": value, "meta": _meta(request)})

        return _run(read, request)


def _request_id(request: Request) -> str:
    current = getattr(request.state, "operation_request_id", None)
    if isinstance(current, str) and current.startswith("req_"):
        return current
    value = "req_" + secrets.token_hex(16)
    request.state.operation_request_id = value
    return value


def _meta(request: Request) -> dict[str, object]:
    unavailable = {
        "available": False,
        "reason": "operation_executor_not_wired",
    }
    return {
        "request_id": _request_id(request),
        "generated_at": datetime.now(UTC).isoformat(),
        "partial": False,
        "sources": [{
            "name": "operation_journal",
            "status": "available",
            "observed_at": None,
            "reason": None,
        }],
        "warnings": [],
        "capabilities": {
            "operations.read": {"available": True, "reason": None},
            "operations.execute": unavailable,
            "operations.retry": unavailable,
            "operations.reconcile": unavailable,
        },
    }


def _error(request: Request, code: str) -> JSONResponse:
    public = code if code in _STATUS else "internal_error"
    status = _STATUS.get(public, 500)
    return JSONResponse(status_code=status, content={"error": {
        "code": public,
        "message": public.replace("_", " "),
        "retryable": public in _RETRYABLE,
        "request_id": _request_id(request),
        "details": {},
    }})


def _run(operation, request: Request) -> JSONResponse:
    try:
        return operation()
    except operation_store.OperationError as exc:
        return _error(request, exc.code)
    except Exception:
        return _error(request, "internal_error")
