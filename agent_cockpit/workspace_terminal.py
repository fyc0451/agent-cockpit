"""Workspace-scoped live PTY controller for Cockpit Next."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Sequence

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import files
from . import operation_store
from . import project_registry_api
from . import project_registry_store
from . import runtime_stats
from . import terminal
from . import terminal_ticket_store


MAX_INPUT_BYTES = 64 * 1024
STREAM_INVALID = 4400
STREAM_NOT_FOUND = 4404
STREAM_CONFLICT = 4409
STREAM_TAKEN_OVER = 4409
STREAM_UNAVAILABLE = 4503

_STATUS = {
    "invalid_argument": 400,
    "idempotency_key_required": 400,
    "project_or_workspace_not_found": 404,
    "terminal_ticket_not_found": 404,
    "revision_conflict": 409,
    "generation_conflict": 409,
    "reconnect_cursor_conflict": 409,
    "idempotency_conflict": 409,
    "terminal_not_running": 409,
    "terminal_process_unknown": 409,
    "workspace_terminal_unavailable": 412,
    "terminal_limit_reached": 429,
    "terminal_io_unavailable": 503,
    "store_read_failed": 503,
    "store_write_failed": 503,
    "schema_missing": 503,
    "migration_required": 503,
    "future_schema": 503,
    "schema_fingerprint_mismatch": 503,
    "store_corrupt": 503,
    "store_unsafe": 503,
    "internal_error": 500,
}
_RETRYABLE = frozenset({
    "terminal_io_unavailable", "store_read_failed", "store_write_failed",
    "schema_missing", "migration_required",
})


class WorkspaceTerminalError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _Binding:
    project_id: str
    workspace_id: str
    ticket_id: str
    term_id: str
    generation: int


@dataclass(frozen=True)
class _Authority:
    workspace: Any
    canonical_path: str


@dataclass(frozen=True)
class ApiService:
    controller_provider: Callable[[], "WorkspaceTerminalController"]
    websocket_authorizer: Callable[[WebSocket], bool]


def is_workspace_terminal_path(path: str) -> bool:
    return path.startswith("/api/projects/") and "/terminal-tickets" in path


class WorkspaceTerminalController:
    def __init__(
        self,
        *,
        registry_provider: Callable[[], project_registry_store.ProjectRegistryStore],
        ticket_provider: Callable[[], terminal_ticket_store.TerminalTicketStore],
        operation_provider: Callable[[], operation_store.OperationStore],
        engine: Any = terminal.workspace_engine,
        root_opener: Callable[[str], tuple[int, Any]] = files._open_trusted_root,
    ):
        self._registry_provider = registry_provider
        self._ticket_provider = ticket_provider
        self._operation_provider = operation_provider
        self._engine = engine
        self._root_opener = root_opener
        self._lock = threading.RLock()
        self._bindings: dict[str, _Binding] = {}
        self._known_exited: set[tuple[str, int]] = set()
        self._exit_watchers: set[str] = set()
        self._control_replays: dict[tuple[str, str, str, str], tuple[str, dict[str, object] | str]] = {}
        self._closed = False

    def ready(self) -> bool:
        with self._lock:
            return not self._closed

    def capability(self, workspace: Any, location: Any) -> tuple[bool, str | None]:
        if not self.ready():
            return False, "workspace_terminal_unavailable"
        if workspace.lifecycle != "active":
            return False, "workspace_not_active"
        if workspace.isolation_kind != "shared":
            return False, "workspace_isolation_not_supported"
        if location.lifecycle != "active":
            return False, "repo_location_not_active"
        if location.node_id != "local":
            return False, "repo_location_not_local"
        if location.availability != "available":
            return False, "repo_location_unavailable"
        return True, None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            bindings = tuple(self._bindings.values())
        for binding in bindings:
            try:
                confirmed = self._engine.kill_term(binding.term_id)
            except Exception:
                confirmed = False
            if confirmed:
                with self._lock:
                    if self._bindings.get(binding.ticket_id) == binding:
                        self._bindings.pop(binding.ticket_id, None)
            self._mark_stopped(binding, recovery_required=True)

    def list_tickets(
        self, project_id: str, workspace_id: str, cursor: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            self._require_ready()
            self._authority(project_id, workspace_id)
            try:
                values, next_cursor = self._ticket_provider().list(
                    project_id=project_id,
                    workspace_id=workspace_id,
                    after_ticket_id=cursor,
                    limit=100,
                )
            except Exception as exc:
                self._translate_store_error(exc)
            return {
                "items": [self._view(value) for value in values],
                "next_cursor": next_cursor,
            }

    def get_ticket(
        self, project_id: str, workspace_id: str, ticket_id: str,
    ) -> dict[str, object]:
        with self._lock:
            self._require_ready()
            self._authority(project_id, workspace_id)
            return self._view(self._ticket(project_id, workspace_id, ticket_id))

    def create(
        self,
        project_id: str,
        workspace_id: str,
        *,
        workspace_revision: int,
        cols: int,
        rows: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        with self._lock:
            self._require_ready()
            self._dimensions(cols, rows)
            authority = self._authority(
                project_id, workspace_id, expected_revision=workspace_revision,
            )
            request_data = {
                "workspace_revision": workspace_revision,
                "cols": cols,
                "rows": rows,
            }
            replay = self._replay(
                "create", project_id, workspace_id, idempotency_key, request_data,
            )
            if replay is not None:
                return replay
            tickets = self._ticket_provider()
            try:
                original = tickets.create({
                    "project_id": project_id,
                    "workspace_id": workspace_id,
                    "desired_state": "running",
                    "observed_state": "unknown",
                    "engine_generation": 1,
                    "reconnect_cursor": 0,
                    "receipt_refs": [],
                }, idempotency_key=idempotency_key)
                current = tickets.get(
                    project_id=project_id,
                    workspace_id=workspace_id,
                    ticket_id=original.ticket_id,
                )
            except Exception as exc:
                self._translate_store_error(exc)
            assert current is not None
            binding_holder: dict[str, _Binding] = {}

            def start(_execution_id: str) -> Mapping[str, object]:
                binding = self._bindings.get(current.ticket_id)
                if binding is not None:
                    if (
                        binding.generation == current.engine_generation
                        and self._engine.is_alive(binding.term_id)
                    ):
                        binding_holder["value"] = binding
                        return {"generation": binding.generation, "started": True}
                    self._bindings.pop(current.ticket_id, None)
                root_fd = self._open_root(authority.canonical_path)
                try:
                    created = self._engine.create_bound_term(root_fd, cols, rows)
                except RuntimeError as exc:
                    raise _ProviderFailure("terminal_limit_reached") from exc
                except (OSError, ValueError) as exc:
                    raise _ProviderFailure("terminal_io_unavailable") from exc
                finally:
                    os.close(root_fd)
                binding = _Binding(
                    project_id, workspace_id, current.ticket_id,
                    str(created["id"]), current.engine_generation,
                )
                self._bindings[current.ticket_id] = binding
                binding_holder["value"] = binding
                self._watch_binding(binding)
                return {"generation": binding.generation, "started": True}

            try:
                operation_id = self._execute_plan(
                    action="create",
                    ticket=current,
                    idempotency_key=idempotency_key,
                    request=request_data,
                    preconditions=(
                        operation_store.Precondition(
                            "workspace.revision", "workspace", workspace_id,
                            expected_revision=workspace_revision,
                        ),
                        operation_store.Precondition(
                            "ticket.revision", "terminal_ticket", current.ticket_id,
                            expected_revision=current.revision,
                            expected_generation=str(current.engine_generation),
                        ),
                    ),
                    steps=(("start", "terminal.start", start),),
                )
            except WorkspaceTerminalError as exc:
                self._remember_replay(
                    "create", project_id, workspace_id, idempotency_key, request_data, exc,
                )
                raise
            binding = self._bindings.get(current.ticket_id)
            if binding is None or not self._engine.is_alive(binding.term_id):
                raise WorkspaceTerminalError("terminal_process_unknown")
            current = self._current_ticket(current)
            if not _has_operation_ref(current, operation_id):
                current = self._update_ticket(
                    current,
                    desired_state="running",
                    observed_state="running",
                    operation_id=operation_id,
                )
            result = self._view(current)
            self._remember_replay(
                "create", project_id, workspace_id, idempotency_key, request_data, result,
            )
            return result

    def interrupt(
        self,
        project_id: str,
        workspace_id: str,
        ticket_id: str,
        *,
        revision: int,
        generation: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        with self._lock:
            self._require_ready()
            self._authority(project_id, workspace_id)
            ticket = self._ticket(project_id, workspace_id, ticket_id)
            request_data = {"revision": revision, "generation": generation}
            replay = self._replay(
                "interrupt", project_id, workspace_id, idempotency_key, request_data,
            )
            if replay is not None:
                return replay
            self._assert_ticket(ticket, revision, generation)

            def deliver(_execution_id: str) -> Mapping[str, object]:
                self._assert_ticket(ticket, revision, generation)
                binding = self._live_binding(ticket)
                try:
                    delivered = self._engine.interrupt_term(binding.term_id)
                except (TimeoutError, ConnectionError) as exc:
                    raise _ProviderFailure(
                        "terminal_process_unknown", uncertain=True,
                    ) from exc
                if not delivered:
                    raise _ProviderFailure("terminal_not_running")
                return {"generation": generation, "delivered": True}

            try:
                operation_id = self._execute_plan(
                    action="interrupt",
                    ticket=ticket,
                    idempotency_key=idempotency_key,
                    request=request_data,
                    preconditions=_ticket_preconditions(ticket_id, revision, generation),
                    steps=(("interrupt", "terminal.interrupt", deliver),),
                )
            except WorkspaceTerminalError as exc:
                self._remember_replay(
                    "interrupt", project_id, workspace_id, idempotency_key, request_data, exc,
                )
                raise
            current = self._current_ticket(ticket)
            if not _has_operation_ref(current, operation_id):
                current = self._update_ticket(current, operation_id=operation_id)
            result = self._view(current)
            self._remember_replay(
                "interrupt", project_id, workspace_id, idempotency_key, request_data, result,
            )
            return result

    def reconnect(
        self,
        project_id: str,
        workspace_id: str,
        ticket_id: str,
        *,
        revision: int,
        generation: int,
        cursor: int,
        cols: int,
        rows: int,
    ) -> dict[str, object]:
        with self._lock:
            self._require_ready()
            self._dimensions(cols, rows)
            self._authority(project_id, workspace_id)
            ticket = self._ticket(project_id, workspace_id, ticket_id)
            self._assert_ticket(ticket, revision, generation, cursor)
            if cursor == terminal_ticket_store.MAX_CURSOR:
                raise WorkspaceTerminalError("reconnect_cursor_conflict")
            binding = self._live_binding(ticket)
            try:
                updated = self._ticket_provider().update(
                    project_id=project_id,
                    workspace_id=workspace_id,
                    ticket_id=ticket_id,
                    expected_revision=revision,
                    value=_ticket_value(ticket, reconnect_cursor=cursor + 1),
                )
            except Exception as exc:
                self._translate_store_error(exc)
            if not self._engine.resize_term_exact(binding.term_id, cols, rows):
                raise WorkspaceTerminalError("terminal_not_running")
            return self._view(updated)

    def restart(
        self,
        project_id: str,
        workspace_id: str,
        ticket_id: str,
        *,
        revision: int,
        generation: int,
        cols: int,
        rows: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        with self._lock:
            self._require_ready()
            self._dimensions(cols, rows)
            if generation >= 2**63 - 1:
                raise WorkspaceTerminalError("generation_conflict")
            authority = self._authority(project_id, workspace_id)
            ticket = self._ticket(project_id, workspace_id, ticket_id)
            request_data = {
                "revision": revision,
                "generation": generation,
                "cols": cols,
                "rows": rows,
            }
            replay = self._replay(
                "restart", project_id, workspace_id, idempotency_key, request_data,
            )
            if replay is not None:
                return replay
            self._assert_ticket(ticket, revision, generation)

            def stop(_execution_id: str) -> Mapping[str, object]:
                self._assert_ticket(ticket, revision, generation)
                binding = self._binding_for_stop(ticket)
                if binding is None:
                    return {"generation": generation, "stopped": True}
                try:
                    confirmed = self._engine.kill_term(binding.term_id)
                except Exception as exc:
                    raise _ProviderFailure(
                        "terminal_process_unknown", uncertain=True,
                    ) from exc
                if not confirmed:
                    raise _ProviderFailure("terminal_process_unknown", uncertain=True)
                self._bindings.pop(ticket_id, None)
                self._known_exited.add((ticket_id, generation))
                return {"generation": generation, "stopped": True}

            def start(_execution_id: str) -> Mapping[str, object]:
                next_generation = generation + 1
                root_fd = self._open_root(authority.canonical_path)
                try:
                    created = self._engine.create_bound_term(root_fd, cols, rows)
                except RuntimeError as exc:
                    raise _ProviderFailure("terminal_limit_reached") from exc
                except (OSError, ValueError) as exc:
                    raise _ProviderFailure("terminal_io_unavailable") from exc
                finally:
                    os.close(root_fd)
                self._bindings[ticket_id] = _Binding(
                    project_id, workspace_id, ticket_id,
                    str(created["id"]), next_generation,
                )
                self._watch_binding(self._bindings[ticket_id])
                self._known_exited.discard((ticket_id, generation))
                return {"generation": next_generation, "started": True}

            try:
                operation_id = self._execute_plan(
                    action="restart",
                    ticket=ticket,
                    idempotency_key=idempotency_key,
                    request=request_data,
                    preconditions=_ticket_preconditions(ticket_id, revision, generation),
                    steps=(
                        ("stop", "terminal.stop", stop),
                        ("start", "terminal.start", start),
                    ),
                )
            except WorkspaceTerminalError as exc:
                self._remember_replay(
                    "restart", project_id, workspace_id, idempotency_key, request_data, exc,
                )
                raise
            binding = self._bindings.get(ticket_id)
            if binding is None or binding.generation != generation + 1:
                raise WorkspaceTerminalError("terminal_process_unknown")
            current = self._current_ticket(ticket)
            if not _has_operation_ref(current, operation_id):
                current = self._update_ticket(
                    current,
                    desired_state="running",
                    observed_state="running",
                    generation=generation + 1,
                    cursor=0,
                    operation_id=operation_id,
                )
            result = self._view(current)
            self._remember_replay(
                "restart", project_id, workspace_id, idempotency_key, request_data, result,
            )
            return result

    def close_ticket(
        self,
        project_id: str,
        workspace_id: str,
        ticket_id: str,
        *,
        revision: int,
        generation: int,
        idempotency_key: str,
    ) -> dict[str, object]:
        with self._lock:
            self._require_ready()
            self._authority(project_id, workspace_id)
            ticket = self._ticket(project_id, workspace_id, ticket_id)
            request_data = {"revision": revision, "generation": generation}
            replay = self._replay(
                "close", project_id, workspace_id, idempotency_key, request_data,
            )
            if replay is not None:
                return replay
            self._assert_ticket(ticket, revision, generation)

            def stop(_execution_id: str) -> Mapping[str, object]:
                self._assert_ticket(ticket, revision, generation)
                binding = self._binding_for_stop(ticket)
                if binding is None:
                    return {"generation": generation, "stopped": True}
                try:
                    confirmed = self._engine.kill_term(binding.term_id)
                except Exception as exc:
                    raise _ProviderFailure(
                        "terminal_process_unknown", uncertain=True,
                    ) from exc
                if not confirmed:
                    raise _ProviderFailure("terminal_process_unknown", uncertain=True)
                self._bindings.pop(ticket_id, None)
                self._known_exited.add((ticket_id, generation))
                return {"generation": generation, "stopped": True}

            try:
                operation_id = self._execute_plan(
                    action="close",
                    ticket=ticket,
                    idempotency_key=idempotency_key,
                    request=request_data,
                    preconditions=_ticket_preconditions(ticket_id, revision, generation),
                    steps=(("stop", "terminal.stop", stop),),
                )
            except WorkspaceTerminalError as exc:
                self._remember_replay(
                    "close", project_id, workspace_id, idempotency_key, request_data, exc,
                )
                raise
            current = self._current_ticket(ticket)
            if not _has_operation_ref(current, operation_id):
                current = self._update_ticket(
                    current,
                    desired_state="stopped",
                    observed_state="stopped",
                    operation_id=operation_id,
                )
            result = self._view(current)
            self._remember_replay(
                "close", project_id, workspace_id, idempotency_key, request_data, result,
            )
            return result

    def stream_binding(
        self,
        project_id: str,
        workspace_id: str,
        ticket_id: str,
        *, revision: int, generation: int, cursor: int,
    ) -> _Binding:
        with self._lock:
            self._require_ready()
            self._authority(project_id, workspace_id)
            ticket = self._ticket(project_id, workspace_id, ticket_id)
            self._assert_ticket(ticket, revision, generation, cursor)
            return self._live_binding(ticket)

    def write_input(
        self, binding: _Binding, *, revision: int, generation: int,
        cursor: int, value: str,
    ) -> None:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_INPUT_BYTES:
            raise WorkspaceTerminalError("invalid_argument")
        with self._lock:
            ticket = self._ticket(
                binding.project_id, binding.workspace_id, binding.ticket_id,
            )
            self._assert_ticket(ticket, revision, generation, cursor)
            self._assert_binding(binding, ticket)
            if not self._engine.write_term(binding.term_id, value):
                raise WorkspaceTerminalError("terminal_not_running")

    def resize(
        self, binding: _Binding, *, revision: int, generation: int,
        cursor: int, cols: int, rows: int,
    ) -> None:
        with self._lock:
            ticket = self._ticket(
                binding.project_id, binding.workspace_id, binding.ticket_id,
            )
            self._assert_ticket(ticket, revision, generation, cursor)
            self._assert_binding(binding, ticket)
            try:
                changed = self._engine.resize_term_exact(binding.term_id, cols, rows)
            except ValueError as exc:
                raise WorkspaceTerminalError("invalid_argument") from exc
            if not changed:
                raise WorkspaceTerminalError("terminal_not_running")

    def replay(self, binding: _Binding) -> tuple[bytes, bool]:
        with self._lock:
            current = self._bindings.get(binding.ticket_id)
            if current != binding:
                raise WorkspaceTerminalError("terminal_not_running")
        history, truncated = self._engine.output_snapshot(binding.term_id)
        if history is None:
            raise WorkspaceTerminalError("terminal_process_unknown")
        return history, truncated

    def read(self, binding: _Binding, timeout: float, maximum: int) -> bytes:
        with self._lock:
            current = self._bindings.get(binding.ticket_id)
            if current != binding:
                return b""
        return self._engine.read_available(binding.term_id, timeout, maximum)

    def alive(self, binding: _Binding) -> bool:
        with self._lock:
            return (
                self._bindings.get(binding.ticket_id) == binding
                and self._engine.is_alive(binding.term_id)
            )

    def drain(self, binding: _Binding) -> bytes:
        return self._engine.drain_output(binding.term_id, 0.05)

    def observe_exit(self, binding: _Binding) -> None:
        with self._lock:
            if self._bindings.get(binding.ticket_id) != binding:
                return
            self._known_exited.add((binding.ticket_id, binding.generation))
            self._mark_stopped(binding, natural_exit=True)

    def _watch_binding(self, binding: _Binding) -> None:
        if binding.term_id in self._exit_watchers:
            return
        self._exit_watchers.add(binding.term_id)
        threading.Thread(
            target=self._watch_exit, args=(binding,), daemon=True,
            name="workspace-terminal-exit",
        ).start()

    def _watch_exit(self, binding: _Binding) -> None:
        try:
            while not self._closed:
                with self._lock:
                    if self._bindings.get(binding.ticket_id) != binding:
                        return
                if not self._engine.is_alive(binding.term_id):
                    self.observe_exit(binding)
                    return
                threading.Event().wait(0.1)
        finally:
            with self._lock:
                self._exit_watchers.discard(binding.term_id)

    def _mark_stopped(
        self, binding: _Binding, *, recovery_required: bool = False,
        natural_exit: bool = False,
    ) -> None:
        try:
            ticket = self._ticket_provider().get(
                project_id=binding.project_id,
                workspace_id=binding.workspace_id,
                ticket_id=binding.ticket_id,
            )
            if (
                ticket is not None
                and ticket.engine_generation == binding.generation
                and (
                    ticket.observed_state != "stopped"
                    or natural_exit and not _has_exit_receipt(ticket, binding.generation)
                )
            ):
                refs = list(ticket.receipt_refs)
                if natural_exit and not _has_exit_receipt(ticket, binding.generation):
                    refs = refs[-31:] + [{
                        "type": "terminal_exit",
                        "id": _exit_receipt_id(binding.ticket_id, binding.generation),
                    }]
                self._ticket_provider().update(
                    project_id=binding.project_id,
                    workspace_id=binding.workspace_id,
                    ticket_id=binding.ticket_id,
                    expected_revision=ticket.revision,
                    value=_ticket_value(
                        ticket,
                        desired_state="recovery_required" if recovery_required else None,
                        observed_state="stopped",
                        receipt_refs=refs,
                    ),
                )
        except Exception:
            return

    def _execute_plan(
        self,
        *,
        action: str,
        ticket: terminal_ticket_store.TerminalTicket,
        idempotency_key: str,
        request: Mapping[str, object],
        preconditions: Sequence[operation_store.Precondition],
        steps: Sequence[tuple[str, str, Callable[[str], Mapping[str, object]]]],
    ) -> str:
        journal = self._operation_provider()
        plan_digest = _digest({
            "action": action,
            "steps": [{"id": item[0], "kind": item[1]} for item in steps],
        })
        try:
            created = journal.create_operation(
                scope=f"workspace_terminal.{action}:{ticket.project_id}:{ticket.workspace_id}",
                idempotency_key=idempotency_key,
                request={"action": action, "ticket_id": ticket.ticket_id, **request},
                kind=f"terminal.{action}",
                subject_type="terminal_ticket",
                subject_id=ticket.ticket_id,
                plan_digest=plan_digest,
                approval_required=False,
                project_id=ticket.project_id,
                workspace_id=ticket.workspace_id,
                preconditions=preconditions,
                steps=tuple(operation_store.Step(item[0], item[1]) for item in steps),
            )
            projection = created.projection
            status = str(projection["operation"]["status"])
            if status == "succeeded":
                return created.operation_id
            if status == "needs_attention":
                raise WorkspaceTerminalError("terminal_process_unknown")
            if status == "failed":
                raise WorkspaceTerminalError("terminal_io_unavailable")
            if status == "planned":
                projection = journal.transition(
                    created.operation_id,
                    expected_operation_revision=int(projection["operation"]["revision"]),
                    status="running",
                )
            callbacks = {step_id: callback for step_id, _kind, callback in steps}
            for step_id, _kind, _callback in steps:
                projection = journal.get_operation(created.operation_id)
                assert projection is not None
                step = next(item for item in projection["steps"] if item["step_id"] == step_id)
                if step["status"] == "succeeded":
                    continue
                attempts = [
                    item for item in projection["attempts"] if item["step_id"] == step_id
                ]
                if step["status"] == "pending":
                    prepared = journal.prepare_attempt(
                        created.operation_id,
                        step_id,
                        expected_operation_revision=int(projection["operation"]["revision"]),
                        expected_step_revision=int(step["revision"]),
                        mode="execute",
                        provider_kind="local_pty",
                    )
                    projection = prepared.projection
                    execution_id = prepared.step_execution_id
                    attempt = next(
                        item for item in projection["attempts"]
                        if item["step_execution_id"] == execution_id
                    )
                elif attempts:
                    attempt = attempts[-1]
                    execution_id = str(attempt["step_execution_id"])
                else:
                    raise WorkspaceTerminalError("terminal_process_unknown")
                if attempt["status"] == "prepared":
                    projection = journal.dispatch_attempt(
                        created.operation_id,
                        execution_id,
                        expected_operation_revision=int(projection["operation"]["revision"]),
                        provider_operation_ref=execution_id,
                    )
                    step = next(
                        item for item in projection["steps"] if item["step_id"] == step_id
                    )
                elif attempt["status"] in {"dispatched", "outcome_unknown"}:
                    raise WorkspaceTerminalError("terminal_process_unknown")
                elif attempt["status"] == "succeeded":
                    continue
                else:
                    raise WorkspaceTerminalError("terminal_io_unavailable")
                try:
                    evidence = callbacks[step_id](execution_id)
                except _ProviderFailure as exc:
                    outcome = "outcome_unknown" if exc.uncertain else "failed"
                    receipt_type = (
                        "provider_response_lost" if exc.uncertain else "provider_outcome"
                    )
                    journal.record_attempt_outcome(
                        created.operation_id,
                        execution_id,
                        expected_operation_revision=int(projection["operation"]["revision"]),
                        expected_step_revision=int(step["revision"]),
                        receipt_id=_receipt_id(execution_id),
                        receipt_type=receipt_type,
                        outcome=outcome,
                        evidence_kind="provider_execution",
                        evidence_ref=execution_id,
                        evidence_digest=_digest({
                            "action": action, "step": step_id,
                            "execution": execution_id, "outcome": outcome,
                        }),
                        failure_code=None if exc.uncertain else exc.code,
                    )
                    raise WorkspaceTerminalError(exc.code) from None
                projection = journal.record_attempt_outcome(
                    created.operation_id,
                    execution_id,
                    expected_operation_revision=int(projection["operation"]["revision"]),
                    expected_step_revision=int(step["revision"]),
                    receipt_id=_receipt_id(execution_id),
                    receipt_type="provider_outcome",
                    outcome="succeeded",
                    evidence_kind="opaque_digest",
                    evidence_ref=execution_id,
                    evidence_digest=_digest({
                        "action": action, "step": step_id,
                        "execution": execution_id, "evidence": evidence,
                    }),
                )
            projection = journal.get_operation(created.operation_id)
            assert projection is not None
            if projection["operation"]["status"] == "running":
                journal.transition(
                    created.operation_id,
                    expected_operation_revision=int(projection["operation"]["revision"]),
                    status="succeeded",
                )
            return created.operation_id
        except WorkspaceTerminalError:
            raise
        except Exception as exc:
            self._translate_store_error(exc)

    def _authority(
        self, project_id: str, workspace_id: str,
        *, expected_revision: int | None = None,
    ) -> _Authority:
        try:
            registry = self._registry_provider()
            project = registry.get_project_by_id(project_id)
            workspace = registry.get_workspace(project_id, workspace_id)
            locations = registry.list_repo_locations(project_id)
        except Exception as exc:
            self._translate_store_error(exc)
        if project is None or workspace is None or locations is None:
            raise WorkspaceTerminalError("project_or_workspace_not_found")
        location = next(
            (item for item in locations if item.repo_location_id == workspace.repo_location_id),
            None,
        )
        if location is None:
            raise WorkspaceTerminalError("project_or_workspace_not_found")
        if expected_revision is not None and workspace.version != expected_revision:
            raise WorkspaceTerminalError("revision_conflict")
        if (
            project.project.lifecycle != "active"
            or workspace.lifecycle != "active"
            or workspace.isolation_kind != "shared"
            or location.lifecycle != "active"
            or location.node_id != "local"
            or location.availability != "available"
        ):
            raise WorkspaceTerminalError("workspace_terminal_unavailable")
        return _Authority(workspace, location.canonical_path)

    def _open_root(self, path: str) -> int:
        try:
            root_fd, _lexical = self._root_opener(path)
            return root_fd
        except Exception as exc:
            raise _ProviderFailure("workspace_terminal_unavailable") from exc

    def _ticket(
        self, project_id: str, workspace_id: str, ticket_id: str,
    ) -> terminal_ticket_store.TerminalTicket:
        try:
            value = self._ticket_provider().get(
                project_id=project_id,
                workspace_id=workspace_id,
                ticket_id=ticket_id,
            )
        except Exception as exc:
            self._translate_store_error(exc)
        if value is None:
            raise WorkspaceTerminalError("terminal_ticket_not_found")
        return value

    def _current_ticket(
        self, ticket: terminal_ticket_store.TerminalTicket,
    ) -> terminal_ticket_store.TerminalTicket:
        return self._ticket(ticket.project_id, ticket.workspace_id, ticket.ticket_id)

    def _assert_ticket(
        self,
        ticket: terminal_ticket_store.TerminalTicket,
        revision: int,
        generation: int,
        cursor: int | None = None,
    ) -> None:
        if type(revision) is not int or revision < 1 or revision != ticket.revision:
            raise WorkspaceTerminalError("revision_conflict")
        if type(generation) is not int or generation < 1 or generation != ticket.engine_generation:
            raise WorkspaceTerminalError("generation_conflict")
        if cursor is not None and (
            type(cursor) is not int or cursor < 0 or cursor != ticket.reconnect_cursor
        ):
            raise WorkspaceTerminalError("reconnect_cursor_conflict")

    def _live_binding(
        self, ticket: terminal_ticket_store.TerminalTicket,
    ) -> _Binding:
        binding = self._bindings.get(ticket.ticket_id)
        if binding is None:
            if ticket.observed_state == "running":
                raise WorkspaceTerminalError("terminal_process_unknown")
            raise WorkspaceTerminalError("terminal_not_running")
        self._assert_binding(binding, ticket)
        if not self._engine.is_alive(binding.term_id):
            self._known_exited.add((ticket.ticket_id, ticket.engine_generation))
            raise WorkspaceTerminalError("terminal_not_running")
        return binding

    def _binding_for_stop(
        self, ticket: terminal_ticket_store.TerminalTicket,
    ) -> _Binding | None:
        binding = self._bindings.get(ticket.ticket_id)
        if binding is None:
            if (ticket.ticket_id, ticket.engine_generation) in self._known_exited:
                return None
            if ticket.observed_state == "stopped":
                return None
            raise WorkspaceTerminalError("terminal_process_unknown")
        self._assert_binding(binding, ticket)
        if not self._engine.is_alive(binding.term_id):
            self._known_exited.add((ticket.ticket_id, ticket.engine_generation))
            self._mark_stopped(binding, natural_exit=True)
            return None
        return binding

    def _assert_binding(
        self, binding: _Binding, ticket: terminal_ticket_store.TerminalTicket,
    ) -> None:
        if (
            binding.project_id != ticket.project_id
            or binding.workspace_id != ticket.workspace_id
            or binding.ticket_id != ticket.ticket_id
            or binding.generation != ticket.engine_generation
            or self._bindings.get(ticket.ticket_id) != binding
        ):
            raise WorkspaceTerminalError("terminal_process_unknown")

    def _update_ticket(
        self,
        ticket: terminal_ticket_store.TerminalTicket,
        *,
        desired_state: str | None = None,
        observed_state: str | None = None,
        generation: int | None = None,
        cursor: int | None = None,
        operation_id: str | None = None,
    ) -> terminal_ticket_store.TerminalTicket:
        refs = list(ticket.receipt_refs)
        if operation_id is not None and not _has_operation_ref(ticket, operation_id):
            refs = refs[-31:] + [{"type": "operation", "id": operation_id}]
        try:
            return self._ticket_provider().update(
                project_id=ticket.project_id,
                workspace_id=ticket.workspace_id,
                ticket_id=ticket.ticket_id,
                expected_revision=ticket.revision,
                value=_ticket_value(
                    ticket,
                    desired_state=desired_state,
                    observed_state=observed_state,
                    generation=generation,
                    reconnect_cursor=cursor,
                    receipt_refs=refs,
                ),
            )
        except Exception as exc:
            self._translate_store_error(exc)

    def _view(
        self, ticket: terminal_ticket_store.TerminalTicket,
    ) -> dict[str, object]:
        binding = self._bindings.get(ticket.ticket_id)
        if binding is not None and binding.generation == ticket.engine_generation:
            if self._engine.is_alive(binding.term_id):
                state = "running"
                replay_available = True
                _history, replay_truncated = self._engine.output_snapshot(binding.term_id)
                if _history is None:
                    raise WorkspaceTerminalError("terminal_process_unknown")
            else:
                self._known_exited.add((ticket.ticket_id, ticket.engine_generation))
                self._mark_stopped(binding, natural_exit=True)
                state = "exited"
                replay_available = False
                replay_truncated = False
        elif (ticket.ticket_id, ticket.engine_generation) in self._known_exited:
            state = "exited"
            replay_available = False
            replay_truncated = False
        elif _has_exit_receipt(ticket, ticket.engine_generation):
            state = "exited"
            replay_available = False
            replay_truncated = False
        elif ticket.observed_state == "stopped" or ticket.desired_state == "stopped":
            state = "stopped"
            replay_available = False
            replay_truncated = False
        elif ticket.observed_state == "running":
            state = "process_unknown"
            replay_available = False
            replay_truncated = False
        else:
            state = "pending"
            replay_available = False
            replay_truncated = False
        return {
            "ticket": ticket.public_dict(),
            "runtime": {
                "state": state,
                "replay_available": replay_available,
                "replay_truncated": replay_truncated,
            },
        }

    def _require_ready(self) -> None:
        if self._closed:
            raise WorkspaceTerminalError("terminal_io_unavailable")

    def _replay(
        self, action: str, project_id: str, workspace_id: str,
        idempotency_key: str, request: Mapping[str, object],
    ) -> dict[str, object] | None:
        key = (action, project_id, workspace_id, idempotency_key)
        existing = self._control_replays.get(key)
        if existing is None:
            return None
        digest, result = existing
        if digest != _digest(request):
            raise WorkspaceTerminalError("idempotency_conflict")
        if isinstance(result, str):
            raise WorkspaceTerminalError(result)
        return json.loads(json.dumps(result, sort_keys=True))

    def _remember_replay(
        self, action: str, project_id: str, workspace_id: str,
        idempotency_key: str, request: Mapping[str, object],
        result: dict[str, object] | WorkspaceTerminalError,
    ) -> None:
        stored: dict[str, object] | str
        if isinstance(result, WorkspaceTerminalError):
            stored = result.code
        else:
            stored = json.loads(json.dumps(result, sort_keys=True))
        self._control_replays[(action, project_id, workspace_id, idempotency_key)] = (
            _digest(request), stored,
        )

    def _dimensions(self, cols: int, rows: int) -> None:
        try:
            self._engine.validate_dimensions(cols, rows)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WorkspaceTerminalError("invalid_argument") from exc

    @staticmethod
    def _translate_store_error(exc: Exception) -> None:
        code = getattr(exc, "code", None)
        if isinstance(code, str):
            if code in _STATUS:
                raise WorkspaceTerminalError(code) from None
            if code == "operation_not_found":
                raise WorkspaceTerminalError("terminal_process_unknown") from None
        raise WorkspaceTerminalError("terminal_io_unavailable") from None


class _ProviderFailure(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False):
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


def install(app: FastAPI, service: ApiService) -> None:
    streams: dict[str, dict[str, Any]] = {}
    streams_lock = asyncio.Lock()

    @app.get("/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets")
    def list_tickets(project_id: str, workspace_id: str, request: Request):
        def operation():
            cursor = _single_query(request, "cursor", None)
            return service.controller_provider().list_tickets(
                project_id, workspace_id, cursor,
            )
        return _run_http(operation, request)

    @app.get("/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets/{ticket_id}")
    def ticket_detail(project_id: str, workspace_id: str, ticket_id: str, request: Request):
        return _run_http(
            lambda: service.controller_provider().get_ticket(
                project_id, workspace_id, ticket_id,
            ),
            request,
        )

    @app.post("/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets")
    async def create_ticket(project_id: str, workspace_id: str, request: Request):
        try:
            body = await _body(request, {"revision", "cols", "rows"})
        except WorkspaceTerminalError as exc:
            return _error(request, exc.code)
        return _run_http(lambda: service.controller_provider().create(
            project_id,
            workspace_id,
            workspace_revision=_positive(body["revision"]),
            cols=_positive(body["cols"]),
            rows=_positive(body["rows"]),
            idempotency_key=_idempotency_key(request),
        ), request, status=201)

    @app.post("/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets/{ticket_id}/interrupt")
    async def interrupt(project_id: str, workspace_id: str, ticket_id: str, request: Request):
        try:
            body = await _body(request, {"revision", "generation"})
        except WorkspaceTerminalError as exc:
            return _error(request, exc.code)
        return _run_http(lambda: service.controller_provider().interrupt(
            project_id, workspace_id, ticket_id,
            revision=_positive(body["revision"]),
            generation=_positive(body["generation"]),
            idempotency_key=_idempotency_key(request),
        ), request)

    @app.post("/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets/{ticket_id}/reconnect")
    async def reconnect(project_id: str, workspace_id: str, ticket_id: str, request: Request):
        try:
            body = await _body(
                request, {"revision", "generation", "cursor", "cols", "rows"},
            )
        except WorkspaceTerminalError as exc:
            return _error(request, exc.code)
        return _run_http(lambda: service.controller_provider().reconnect(
            project_id, workspace_id, ticket_id,
            revision=_positive(body["revision"]),
            generation=_positive(body["generation"]),
            cursor=_nonnegative(body["cursor"]),
            cols=_positive(body["cols"]),
            rows=_positive(body["rows"]),
        ), request)

    @app.post("/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets/{ticket_id}/restart")
    async def restart(project_id: str, workspace_id: str, ticket_id: str, request: Request):
        try:
            body = await _body(request, {"revision", "generation", "cols", "rows"})
        except WorkspaceTerminalError as exc:
            return _error(request, exc.code)
        return _run_http(lambda: service.controller_provider().restart(
            project_id, workspace_id, ticket_id,
            revision=_positive(body["revision"]),
            generation=_positive(body["generation"]),
            cols=_positive(body["cols"]),
            rows=_positive(body["rows"]),
            idempotency_key=_idempotency_key(request),
        ), request)

    @app.post("/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets/{ticket_id}/close")
    async def close_ticket(project_id: str, workspace_id: str, ticket_id: str, request: Request):
        try:
            body = await _body(request, {"revision", "generation"})
        except WorkspaceTerminalError as exc:
            return _error(request, exc.code)
        return _run_http(lambda: service.controller_provider().close_ticket(
            project_id, workspace_id, ticket_id,
            revision=_positive(body["revision"]),
            generation=_positive(body["generation"]),
            idempotency_key=_idempotency_key(request),
        ), request)

    @app.websocket("/api/projects/{project_id}/workspaces/{workspace_id}/terminal-tickets/{ticket_id}/stream")
    async def stream(websocket: WebSocket, project_id: str, workspace_id: str, ticket_id: str):
        if websocket.query_params or not service.websocket_authorizer(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        connection: dict[str, Any] | None = None
        lease = None
        pump_task: asyncio.Task | None = None
        try:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            if message.get("bytes") is not None or not isinstance(message.get("text"), str):
                raise WorkspaceTerminalError("invalid_argument")
            try:
                first = json.loads(message["text"])
            except json.JSONDecodeError:
                raise WorkspaceTerminalError("invalid_argument") from None
            _exact(first, {"type", "revision", "generation", "cursor"})
            if first["type"] != "attach":
                raise WorkspaceTerminalError("invalid_argument")
            revision = _positive(first["revision"])
            generation = _positive(first["generation"])
            cursor = _nonnegative(first["cursor"])
            controller = service.controller_provider()
            binding = controller.stream_binding(
                project_id, workspace_id, ticket_id,
                revision=revision, generation=generation, cursor=cursor,
            )
            connection = {"websocket": websocket, "pump": None, "binding": binding}
            async with streams_lock:
                previous = streams.get(ticket_id)
                streams[ticket_id] = connection
            if previous is not None:
                previous_pump = previous.get("pump")
                if previous_pump is not None:
                    previous_pump.cancel()
                    with _suppress_cancelled():
                        await previous_pump
                with _suppress_errors():
                    await previous["websocket"].close(
                        code=STREAM_TAKEN_OVER,
                        reason="terminal opened by a newer page",
                    )
            lease = runtime_stats.open_connection("terminal_websocket")
            history, truncated = await asyncio.to_thread(controller.replay, binding)
            await websocket.send_json({
                "type": "replay_start", "revision": revision,
                "generation": generation, "cursor": cursor,
            })
            if history:
                await websocket.send_bytes(history)
            await websocket.send_json({
                "type": "replay_complete",
                "revision": revision,
                "generation": generation,
                "cursor": cursor,
                "truncated": truncated,
            })

            async def pump() -> None:
                while streams.get(ticket_id) is connection:
                    read_task = asyncio.create_task(asyncio.to_thread(
                        controller.read, binding, 0.1, 256 * 1024,
                    ))
                    try:
                        data = await asyncio.shield(read_task)
                    except asyncio.CancelledError:
                        with _suppress_cancelled():
                            await read_task
                        raise
                    if streams.get(ticket_id) is not connection:
                        return
                    if data:
                        await websocket.send_bytes(data)
                    elif not controller.alive(binding):
                        tail = await asyncio.to_thread(controller.drain, binding)
                        if tail:
                            await websocket.send_bytes(tail)
                        await asyncio.to_thread(controller.observe_exit, binding)
                        await websocket.send_json({
                            "type": "exit", "generation": generation,
                        })
                        await websocket.close(
                            code=STREAM_CONFLICT, reason="terminal process exited",
                        )
                        return

            pump_task = asyncio.create_task(pump())
            connection["pump"] = pump_task
            while streams.get(ticket_id) is connection:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes") is not None or not isinstance(message.get("text"), str):
                    raise WorkspaceTerminalError("invalid_argument")
                try:
                    frame = json.loads(message["text"])
                except json.JSONDecodeError:
                    raise WorkspaceTerminalError("invalid_argument") from None
                if not isinstance(frame, dict):
                    raise WorkspaceTerminalError("invalid_argument")
                if frame.get("type") == "input":
                    _exact(frame, {"type", "revision", "generation", "cursor", "input"})
                    await asyncio.to_thread(
                        controller.write_input,
                        binding,
                        revision=_positive(frame["revision"]),
                        generation=_positive(frame["generation"]),
                        cursor=_nonnegative(frame["cursor"]),
                        value=frame["input"],
                    )
                elif frame.get("type") == "resize":
                    _exact(frame, {
                        "type", "revision", "generation", "cursor", "cols", "rows",
                    })
                    await asyncio.to_thread(
                        controller.resize,
                        binding,
                        revision=_positive(frame["revision"]),
                        generation=_positive(frame["generation"]),
                        cursor=_nonnegative(frame["cursor"]),
                        cols=_positive(frame["cols"]),
                        rows=_positive(frame["rows"]),
                    )
                else:
                    raise WorkspaceTerminalError("invalid_argument")
        except WebSocketDisconnect:
            pass
        except WorkspaceTerminalError as exc:
            with _suppress_errors():
                await websocket.send_json({"type": "error", "code": exc.code})
            with _suppress_errors():
                await websocket.close(
                    code=_stream_code(exc.code), reason="workspace terminal unavailable",
                )
        except Exception:
            with _suppress_errors():
                await websocket.send_json({
                    "type": "error", "code": "terminal_io_unavailable",
                })
            with _suppress_errors():
                await websocket.close(
                    code=STREAM_UNAVAILABLE, reason="workspace terminal unavailable",
                )
        finally:
            if pump_task is not None:
                pump_task.cancel()
                with _suppress_cancelled():
                    await pump_task
            if connection is not None:
                async with streams_lock:
                    if streams.get(ticket_id) is connection:
                        streams.pop(ticket_id, None)
            if lease is not None:
                lease.close()


class _suppress_cancelled:
    def __enter__(self):
        return self

    def __exit__(self, kind, _value, _traceback):
        return kind is not None and issubclass(kind, (asyncio.CancelledError, WebSocketDisconnect))


class _suppress_errors:
    def __enter__(self):
        return self

    def __exit__(self, kind, _value, _traceback):
        return kind is not None and issubclass(kind, Exception)


def _run_http(operation: Callable[[], object], request: Request, *, status: int = 200) -> JSONResponse:
    try:
        return JSONResponse(
            status_code=status,
            content={"data": operation(), "meta": _meta(request)},
        )
    except WorkspaceTerminalError as exc:
        return _error(request, exc.code)
    except Exception:
        return _error(request, "internal_error")


async def _body(request: Request, keys: set[str]) -> dict[str, object]:
    try:
        value = await request.json()
    except Exception:
        raise WorkspaceTerminalError("invalid_argument") from None
    if not isinstance(value, dict) or set(value) != keys:
        raise WorkspaceTerminalError("invalid_argument")
    return value


def _single_query(request: Request, key: str, default: str | None) -> str | None:
    if set(request.query_params) - {key} or len(request.query_params.getlist(key)) > 1:
        raise WorkspaceTerminalError("invalid_argument")
    return request.query_params.get(key, default)


def _idempotency_key(request: Request) -> str:
    value = request.headers.get("Idempotency-Key")
    if value is None:
        raise WorkspaceTerminalError("idempotency_key_required")
    if not 1 <= len(value) <= 128 or any(ord(char) <= 0x20 or ord(char) >= 0x7F for char in value):
        raise WorkspaceTerminalError("invalid_argument")
    return value


def _positive(value: object) -> int:
    if type(value) is not int or value < 1 or value > 2**63 - 1:
        raise WorkspaceTerminalError("invalid_argument")
    return value


def _nonnegative(value: object) -> int:
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise WorkspaceTerminalError("invalid_argument")
    return value


def _exact(value: object, keys: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise WorkspaceTerminalError("invalid_argument")


def _meta(request: Request) -> dict[str, object]:
    return {
        "request_id": project_registry_api.request_id_for(request),
        "generated_at": datetime.now(UTC).isoformat(),
        "partial": False,
        "sources": [{
            "name": "workspace_terminal", "status": "available",
            "observed_at": None, "reason": None,
        }],
        "warnings": [],
        "capabilities": {
            "terminal.pty": {"available": True, "reason": None},
        },
    }


def _error(request: Request, code: str) -> JSONResponse:
    public = code if code in _STATUS else "internal_error"
    return project_registry_api.g3_error(
        request,
        status=_STATUS[public],
        code=public,
        message=public.replace("_", " "),
        retryable=public in _RETRYABLE,
    )


def _ticket_preconditions(
    ticket_id: str, revision: int, generation: int,
) -> tuple[operation_store.Precondition, ...]:
    return (operation_store.Precondition(
        "ticket.revision", "terminal_ticket", ticket_id,
        expected_revision=revision,
        expected_generation=str(generation),
    ),)


def _ticket_value(
    ticket: terminal_ticket_store.TerminalTicket,
    *,
    desired_state: str | None = None,
    observed_state: str | None = None,
    generation: int | None = None,
    reconnect_cursor: int | None = None,
    receipt_refs: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "project_id": ticket.project_id,
        "workspace_id": ticket.workspace_id,
        "desired_state": desired_state or ticket.desired_state,
        "observed_state": observed_state or ticket.observed_state,
        "engine_generation": generation if generation is not None else ticket.engine_generation,
        "reconnect_cursor": (
            reconnect_cursor if reconnect_cursor is not None else ticket.reconnect_cursor
        ),
        "receipt_refs": [dict(item) for item in (
            receipt_refs if receipt_refs is not None else ticket.receipt_refs
        )],
    }


def _has_operation_ref(
    ticket: terminal_ticket_store.TerminalTicket, operation_id: str,
) -> bool:
    return any(
        item.get("type") == "operation" and item.get("id") == operation_id
        for item in ticket.receipt_refs
    )


def _exit_receipt_id(ticket_id: str, generation: int) -> str:
    material = f"{ticket_id}:{generation}".encode("ascii")
    return "evt_" + hashlib.sha256(material).hexdigest()[:32]


def _has_exit_receipt(
    ticket: terminal_ticket_store.TerminalTicket, generation: int,
) -> bool:
    return any(
        item.get("type") == "terminal_exit"
        and item.get("id") == _exit_receipt_id(ticket.ticket_id, generation)
        for item in ticket.receipt_refs
    )


def _digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _receipt_id(execution_id: str) -> str:
    return "receipt_" + hashlib.sha256(execution_id.encode()).hexdigest()[:32]


def _stream_code(code: str) -> int:
    if code == "invalid_argument":
        return STREAM_INVALID
    if code in {"project_or_workspace_not_found", "terminal_ticket_not_found"}:
        return STREAM_NOT_FOUND
    if code in {
        "revision_conflict", "generation_conflict", "reconnect_cursor_conflict",
        "terminal_not_running",
    }:
        return STREAM_CONFLICT
    return STREAM_UNAVAILABLE
