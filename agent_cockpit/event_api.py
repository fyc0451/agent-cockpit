"""Injectable G3 read boundary for the dormant Event Journal."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import event_store


@dataclass(frozen=True)
class EventApiService:
    store_provider: Callable[[], event_store.EventStore]


def install(app: FastAPI, service: EventApiService) -> None:
    @app.get("/api/events/{event_id}")
    def get_event(event_id: str, request: Request) -> JSONResponse:
        request_id = "req_" + secrets.token_hex(16)
        try:
            record = service.store_provider().get(event_id)
            if record is None:
                return _error(request_id, "event_not_found", 404)
            return JSONResponse({"data": record.public_dict(), "meta": _meta(request_id)})
        except event_store.EventStoreError as exc:
            return _error(request_id, exc.code, 400 if exc.code == "invalid_argument" else 503)
        except Exception:
            return _error(request_id, "internal_error", 500)


def _meta(request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id, "generated_at": datetime.now(UTC).isoformat(),
        "partial": False, "sources": [{"name": "event_journal", "status": "available", "observed_at": None, "reason": None}],
        "capabilities": {"eventJournal.read": {"available": True, "reason": None}, "eventJournal.write": {"available": False, "reason": "dormant_without_producer"}},
    }


def _error(request_id: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": code.replace("_", " "), "retryable": code == "store_read_failed", "request_id": request_id, "details": {}}})
