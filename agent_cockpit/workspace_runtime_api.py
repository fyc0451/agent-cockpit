"""G3 boundary for body-free WorkItem dispatch."""
from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import workspace_dispatch_service as dispatch_mod


_ROUTE = (
    "/api/projects/{project_id}/workspaces/{workspace_id}"
    "/work-items/{work_item_id}/dispatch"
)
_FIELDS = {"expected_work_revision", "expected_preparation_revision"}
_STATUS = {
    "invalid_argument": 400,
    "idempotency_key_required": 400,
    "project_not_found": 404,
    "workspace_not_found": 404,
    "work_item_not_found": 404,
    "preparation_not_found": 404,
    "workspace_not_active": 409,
    "idempotency_conflict": 409,
    "stale_revision": 409,
    "stale_generation": 409,
    "delivery_conflict": 409,
    "claim_conflict": 409,
    "claim_not_active": 409,
    "execution_terminal": 409,
    "runtime_capability_invalid": 409,
    "runtime_unavailable": 503,
    "operation_journal_unavailable": 503,
    "wakeup_outcome_unknown": 503,
    "schema_missing": 503,
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
    "runtime_unavailable", "operation_journal_unavailable",
    "wakeup_outcome_unknown", "store_read_failed", "store_write_failed",
})


class ApiError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def install(app: FastAPI, service: dispatch_mod.DispatchService) -> None:
    @app.post(_ROUTE)
    async def dispatch(
        project_id: str, workspace_id: str, work_item_id: str, request: Request,
    ):
        try:
            payload = await _body(request)
            key = _idempotency_key(request)
            result = service.dispatch(
                project_id, workspace_id, work_item_id,
                expected_work_revision=payload["expected_work_revision"],
                expected_preparation_revision=(
                    payload["expected_preparation_revision"]
                ),
                idempotency_key=key,
            )
            return _success(request, _public_dispatch(result))
        except (ApiError, dispatch_mod.DispatchError) as exc:
            return _error(request, exc.code)
        except Exception as exc:
            code = getattr(exc, "code", None)
            return _error(request, code if isinstance(code, str) else "internal_error")


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
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ApiError("invalid_argument")
    if any(type(value[field]) is not int or value[field] < 1 for field in _FIELDS):
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
    current = getattr(request.state, "workspace_runtime_request_id", None)
    if isinstance(current, str) and current.startswith("req_"):
        return current
    value = "req_" + secrets.token_hex(16)
    request.state.workspace_runtime_request_id = value
    return value


def _meta(request: Request) -> dict[str, object]:
    return {
        "request_id": _request_id(request),
        "generated_at": datetime.now(UTC).isoformat(),
        "partial": False,
        "sources": [{
            "name": "workspace_runtime", "status": "available",
            "observed_at": None, "reason": None,
        }],
        "warnings": [],
        "capabilities": {
            "workspaceRuntime.dispatch": {"available": True, "reason": None},
        },
    }


def _public_dispatch(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ApiError("internal_error")
    operation_id = data.get("operation_id")
    outcome = data.get("outcome")
    if not isinstance(operation_id, str) or operation_id == "":
        raise ApiError("internal_error")
    if not isinstance(outcome, str) or outcome == "":
        raise ApiError("internal_error")
    return {"operation_id": operation_id, "outcome": outcome}


def _success(request: Request, data: object) -> JSONResponse:
    return JSONResponse(content={"data": data, "meta": _meta(request)})


def _error(request: Request, code: str) -> JSONResponse:
    public = code if code in _STATUS else "internal_error"
    return JSONResponse(status_code=_STATUS.get(public, 500), content={"error": {
        "code": public,
        "message": public.replace("_", " "),
        "retryable": public in _RETRYABLE,
        "request_id": _request_id(request),
        "details": {},
    }})
