"""M3 HTTP boundary for exact Handoff review and explicit apply."""
from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import workspace_delivery_service as service_mod
from . import workspace_delivery_store as store_mod


_BASE = "/api/projects/{project_id}/workspaces/{workspace_id}/work-items/{work_item_id}"
_REVIEW = _BASE + "/reviews"
_APPLY = _BASE + "/apply"
_DELIVERY = _BASE + "/delivery"
_REVIEW_FIELDS = frozenset({
    "handoff_id", "reviewer_identity_id", "reviewer_generation",
    "expected_handoff_revision", "expected_delivery_revision", "head_sha",
    "diff_digest", "decision", "summary", "test_evidence",
})
_APPLY_FIELDS = frozenset({"expected_delivery_revision"})
_STATUS = {
    "invalid_argument": 400, "idempotency_key_required": 400,
    "work_item_not_found": 404, "handoff_not_found": 404,
    "review_not_found": 404, "identity_not_found": 404,
    "stale_revision": 409, "stale_generation": 409,
    "handoff_conflict": 409, "review_conflict": 409,
    "self_review_forbidden": 409, "stale_handoff": 409,
    "review_not_accepted": 409, "source_dirty": 409,
    "source_changed": 409, "path_outside_allowed_scope": 409,
    "apply_outcome_unknown": 503, "git_unavailable": 503,
    "git_command_failed": 503, "workspace_work_schema_missing": 503,
    "migration_required": 503, "future_schema": 503,
    "schema_fingerprint_mismatch": 503, "store_unsafe": 503,
    "store_corrupt": 503, "store_read_failed": 503,
    "store_write_failed": 503,
}


class ApiError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def install(
    app: FastAPI, *, service_provider: Callable[[], service_mod.WorkspaceDeliveryService],
    store_provider: Callable[[], store_mod.WorkspaceDeliveryStore],
    identity_provider: Callable[[str, str, str], object | None],
) -> None:
    @app.get(_DELIVERY)
    def get_delivery(
        project_id: str, workspace_id: str, work_item_id: str, request: Request,
    ):
        try:
            if request.query_params:
                raise ApiError("invalid_argument")
            packet = store_provider().get_packet(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
            )
            if packet is None:
                raise ApiError("work_item_not_found")
            return _success(request, packet)
        except Exception as exc:
            return _error(request, _code(exc))

    @app.post(_REVIEW)
    async def review(
        project_id: str, workspace_id: str, work_item_id: str, request: Request,
    ):
        try:
            payload = await _body(request, _REVIEW_FIELDS)
            key = _key(request)
            identity = identity_provider(
                project_id, workspace_id, payload["reviewer_identity_id"],
            )
            if identity is None:
                raise ApiError("identity_not_found")
            if getattr(identity, "revision", None) is None:
                raise ApiError("identity_not_found")
            result = store_provider().review_handoff(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id, idempotency_key=key, **payload,
            )
            return _success(request, result)
        except Exception as exc:
            return _error(request, _code(exc))

    @app.post(_APPLY)
    async def apply(
        project_id: str, workspace_id: str, work_item_id: str, request: Request,
    ):
        try:
            payload = await _body(request, _APPLY_FIELDS)
            key = _key(request)
            result = service_provider().apply(
                project_id=project_id, workspace_id=workspace_id,
                work_item_id=work_item_id,
                expected_delivery_revision=payload["expected_delivery_revision"],
                idempotency_key=key,
            )
            return _success(request, result)
        except Exception as exc:
            return _error(request, _code(exc))


async def _body(request: Request, fields: frozenset[str]) -> dict[str, object]:
    try:
        value = json.loads(
            (await request.body()).decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, ValueError):
        raise ApiError("invalid_argument") from None
    if not isinstance(value, dict) or set(value) != fields:
        raise ApiError("invalid_argument")
    for field in (
        "reviewer_generation", "expected_handoff_revision",
        "expected_delivery_revision",
    ):
        if field in value and (type(value[field]) is not int or value[field] < 1):
            raise ApiError("invalid_argument")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    if (
        not isinstance(key, str) or not 1 <= len(key) <= 128
        or any(ord(char) < 33 or ord(char) == 127 for char in key)
    ):
        raise ApiError("idempotency_key_required")
    return key


def _code(exc: BaseException) -> str:
    value = getattr(exc, "code", None)
    return value if isinstance(value, str) else "internal_error"


def _request_id(request: Request) -> str:
    value = getattr(request.state, "workspace_delivery_request_id", None)
    if isinstance(value, str):
        return value
    value = "req_" + secrets.token_hex(16)
    request.state.workspace_delivery_request_id = value
    return value


def _meta(request: Request) -> dict[str, object]:
    return {
        "request_id": _request_id(request),
        "generated_at": datetime.now(UTC).isoformat(),
        "partial": False,
        "sources": [{
            "name": "workspace_delivery", "status": "available",
            "observed_at": None, "reason": None,
        }],
        "warnings": [],
        "capabilities": {
            "workspaceDelivery.reviewApply": {"available": True, "reason": None},
        },
    }


def _success(request: Request, data: object) -> JSONResponse:
    return JSONResponse(content={"data": data, "meta": _meta(request)})


def _error(request: Request, code: str) -> JSONResponse:
    public = code if code in _STATUS else "internal_error"
    return JSONResponse(status_code=_STATUS.get(public, 500), content={"error": {
        "code": public, "message": public.replace("_", " "),
        "retryable": public.endswith("_unavailable") or public.startswith("store_"),
        "request_id": _request_id(request), "details": {},
    }})
