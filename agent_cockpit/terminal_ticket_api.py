"""Injectable API helper for terminal ticket readers; no shared-server wiring."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import terminal_ticket_store as store


@dataclass(frozen=True)
class TerminalTicketApiService:
    store_provider: Callable[[], store.TerminalTicketStore]


def install(app: FastAPI, service: TerminalTicketApiService) -> None:
    @app.get("/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets/{ticket_id}")
    def get_ticket(project_id: str, workspace_id: str, ticket_id: str, request: Request) -> JSONResponse:
        request_id = "req_" + secrets.token_hex(16)
        try:
            ticket = service.store_provider().get(project_id=project_id, workspace_id=workspace_id, ticket_id=ticket_id)
            return _error(request_id, "terminal_ticket_not_found", 404) if ticket is None else JSONResponse({"data": ticket.public_dict(), "meta": _meta(request_id)})
        except store.TerminalTicketError as exc:
            return _error(request_id, exc.code, 400 if exc.code == "invalid_argument" else 503)
        except Exception:
            return _error(request_id, "internal_error", 500)


def _meta(request_id: str) -> dict[str, object]:
    return {"request_id": request_id, "generated_at": datetime.now(UTC).isoformat(), "partial": False, "sources": [{"name": "terminal_ticket", "status": "available", "observed_at": None, "reason": None}], "capabilities": {"terminalTicket.read": {"available": True, "reason": None}, "terminalTicket.write": {"available": False, "reason": "no_runtime_controller"}}}


def _error(request_id: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": code.replace("_", " "), "retryable": code in {"store_read_failed", "store_write_failed"}, "request_id": request_id, "details": {}}})
