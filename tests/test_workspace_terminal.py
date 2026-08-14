from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_cockpit import operation_store
from agent_cockpit import terminal_ticket_store
from agent_cockpit import workspace_terminal


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
TICKET = "ttk_" + "c" * 32


def _ticket(*, desired="running", observed="running", generation=1, revision=1):
    return terminal_ticket_store.TerminalTicket(
        TICKET, PROJECT, WORKSPACE, desired, observed, generation, 0, (), revision,
        "2026-08-14T00:00:00.000000Z", "2026-08-14T00:00:00.000000Z",
    )


class Registry:
    def __init__(self, *, isolation="shared", available="available"):
        self.workspace = SimpleNamespace(
            workspace_id=WORKSPACE, project_id=PROJECT, repo_location_id="loc_" + "d" * 32,
            lifecycle="active", isolation_kind=isolation, version=1,
        )
        self.location = SimpleNamespace(
            repo_location_id=self.workspace.repo_location_id, lifecycle="active",
            node_id="local", availability=available, canonical_path="/unused",
        )
    def get_project_by_id(self, project_id):
        return SimpleNamespace(project=SimpleNamespace(lifecycle="active")) if project_id == PROJECT else None
    def get_workspace(self, project_id, workspace_id):
        return self.workspace if (project_id, workspace_id) == (PROJECT, WORKSPACE) else None
    def list_repo_locations(self, project_id):
        return (self.location,) if project_id == PROJECT else None


class Tickets:
    def __init__(self, value): self.value, self.created = value, 0
    def get(self, *, project_id, workspace_id, ticket_id):
        return self.value if (project_id, workspace_id, ticket_id) == (PROJECT, WORKSPACE, TICKET) else None
    def list(self, **_kwargs): return (self.value,), None
    def create(self, *_args, **_kwargs): self.created += 1; return self.value


class Engine:
    def __init__(self): self.dimension_calls = 0
    def validate_dimensions(self, cols, rows):
        self.dimension_calls += 1
        if type(cols) is not int or type(rows) is not int or not 1 <= cols <= 500 or not 1 <= rows <= 300:
            raise ValueError("invalid")


def _controller(*, registry=None, ticket=None, engine=None):
    registry = registry or Registry(); ticket = ticket or _ticket(); engine = engine or Engine()
    return workspace_terminal.WorkspaceTerminalController(
        registry_provider=lambda: registry, ticket_provider=lambda: Tickets(ticket),
        operation_provider=lambda: None, engine=engine, root_opener=lambda _path: (_ for _ in ()).throw(AssertionError("root must not open")),
    ), engine


def test_authority_requires_shared_active_local_available():
    controller, _engine = _controller(registry=Registry(isolation="isolated_worktree"))
    with pytest.raises(workspace_terminal.WorkspaceTerminalError) as rejected:
        controller.list_tickets(PROJECT, WORKSPACE)
    assert rejected.value.code == "workspace_terminal_unavailable"
    assert controller.capability(Registry().workspace, Registry().location) == (True, None)


def test_invalid_create_dimensions_fail_before_ticket_or_root_side_effect():
    tickets = Tickets(_ticket()); engine = Engine(); registry = Registry()
    controller = workspace_terminal.WorkspaceTerminalController(
        registry_provider=lambda: registry, ticket_provider=lambda: tickets,
        operation_provider=lambda: None, engine=engine,
        root_opener=lambda _path: (_ for _ in ()).throw(AssertionError("root must not open")),
    )
    with pytest.raises(workspace_terminal.WorkspaceTerminalError) as rejected:
        controller.create(PROJECT, WORKSPACE, workspace_revision=1, cols=True, rows=24, idempotency_key="create")
    assert rejected.value.code == "invalid_argument"
    assert tickets.created == 0 and engine.dimension_calls == 1


def test_boot_loss_is_process_unknown_and_does_not_fork():
    controller, _engine = _controller(ticket=_ticket())
    view = controller.list_tickets(PROJECT, WORKSPACE)["items"][0]
    assert view["runtime"] == {"state": "process_unknown", "replay_available": False, "replay_truncated": False}
    with pytest.raises(workspace_terminal.WorkspaceTerminalError) as rejected:
        controller.stream_binding(PROJECT, WORKSPACE, TICKET, revision=1, generation=1, cursor=0)
    assert rejected.value.code == "terminal_process_unknown"


class ControlTickets:
    def __init__(self, value):
        self.value = value
        self.fail_update = False

    def get(self, **_kwargs):
        return self.value

    def list(self, **_kwargs):
        return (self.value,), None

    def update(self, *, expected_revision, value, **_kwargs):
        if self.fail_update:
            raise terminal_ticket_store.TerminalTicketError("revision_conflict")
        assert expected_revision == self.value.revision
        self.value = replace(
            self.value,
            desired_state=value["desired_state"],
            observed_state=value["observed_state"],
            engine_generation=value["engine_generation"],
            reconnect_cursor=value["reconnect_cursor"],
            receipt_refs=tuple(value["receipt_refs"]),
            revision=self.value.revision + 1,
        )
        return self.value


class ControlEngine:
    def __init__(self, *, alive=True, kill_results=(True,)):
        self.alive = alive
        self.kill_results = iter(kill_results)
        self.interrupts = 0
        self.resizes = 0
        self.creates = 0

    def validate_dimensions(self, cols, rows):
        if type(cols) is not int or type(rows) is not int:
            raise ValueError("dimensions")

    def is_alive(self, _term_id):
        return self.alive

    def interrupt_term(self, _term_id):
        self.interrupts += 1
        return True

    def resize_term_exact(self, _term_id, _cols, _rows):
        self.resizes += 1
        return True

    def kill_term(self, _term_id):
        result = next(self.kill_results)
        if isinstance(result, BaseException):
            raise result
        return result

    def create_bound_term(self, _root_fd, _cols, _rows):
        self.creates += 1
        return {"id": f"term-{self.creates}"}

    def output_snapshot(self, _term_id):
        return b"", False


def _control_ticket(*, cursor=0):
    return terminal_ticket_store.TerminalTicket(
        TICKET, PROJECT, WORKSPACE, "running", "running", 1, cursor, (), 1,
        "2026-08-14T00:00:00.000000Z", "2026-08-14T00:00:00.000000Z",
    )


def _control_controller(tickets, engine, journal):
    controller = workspace_terminal.WorkspaceTerminalController(
        registry_provider=lambda: None,
        ticket_provider=lambda: tickets,
        operation_provider=lambda: journal,
        engine=engine,
        root_opener=lambda _path: (os.open(".", os.O_RDONLY), None),
    )
    controller._authority = lambda *_args, **_kwargs: workspace_terminal._Authority(None, ".")
    controller._bindings[TICKET] = workspace_terminal._Binding(
        PROJECT, WORKSPACE, TICKET, "term-0", 1,
    )
    return controller


@pytest.mark.parametrize("action", ("interrupt", "restart", "close"))
def test_stale_controls_reject_before_operation_write(tmp_path, action):
    journal = operation_store.initialize(tmp_path / f"{action}.sqlite3")
    tickets = ControlTickets(_control_ticket())
    engine = ControlEngine()
    controller = _control_controller(tickets, engine, journal)
    kwargs = {"revision": 2, "generation": 1, "idempotency_key": f"{action}-stale"}
    if action == "interrupt":
        call = controller.interrupt
    elif action == "restart":
        call = controller.restart
        kwargs.update(cols=80, rows=24)
    else:
        call = controller.close_ticket
    with pytest.raises(workspace_terminal.WorkspaceTerminalError) as rejected:
        call(PROJECT, WORKSPACE, TICKET, **kwargs)
    assert rejected.value.code == "revision_conflict"
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM operation_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("action", ("interrupt", "restart", "close"))
def test_control_replay_is_read_only_after_ticket_revision_changes(tmp_path, action):
    journal = operation_store.initialize(tmp_path / f"replay-{action}.sqlite3")
    tickets = ControlTickets(_control_ticket())
    engine = ControlEngine()
    controller = _control_controller(tickets, engine, journal)
    kwargs = {"revision": 1, "generation": 1, "idempotency_key": f"{action}-replay"}
    if action == "interrupt":
        call = controller.interrupt
    elif action == "restart":
        call = controller.restart
        kwargs.update(cols=80, rows=24)
    else:
        call = controller.close_ticket
    first = call(PROJECT, WORKSPACE, TICKET, **kwargs)
    calls = (engine.interrupts, engine.resizes, engine.creates)
    second = call(PROJECT, WORKSPACE, TICKET, **kwargs)
    assert second == first
    assert (engine.interrupts, engine.resizes, engine.creates) == calls
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1


@pytest.mark.parametrize("action", ("restart", "close"))
@pytest.mark.parametrize("failure", (False, OSError("provider disconnected")))
def test_unknown_kill_retains_binding_and_records_uncertain_outcome(
    tmp_path, action, failure,
):
    journal = operation_store.initialize(tmp_path / f"unknown-{action}.sqlite3")
    tickets = ControlTickets(_control_ticket())
    engine = ControlEngine(kill_results=(failure, True))
    controller = _control_controller(tickets, engine, journal)
    kwargs = {"revision": 1, "generation": 1, "idempotency_key": f"{action}-unknown"}
    if action == "restart":
        call = controller.restart
        kwargs.update(cols=80, rows=24)
    else:
        call = controller.close_ticket
    with pytest.raises(workspace_terminal.WorkspaceTerminalError) as rejected:
        call(PROJECT, WORKSPACE, TICKET, **kwargs)
    assert rejected.value.code == "terminal_process_unknown"
    assert controller._bindings[TICKET].term_id == "term-0"
    with sqlite3.connect(journal.path) as connection:
        assert connection.execute(
            "SELECT status FROM operations"
        ).fetchone()[0] == "needs_attention"
        assert connection.execute(
            "SELECT status FROM operation_steps WHERE step_id='stop'"
        ).fetchone()[0] == "outcome_unknown"
        assert connection.execute(
            "SELECT status FROM operation_attempts WHERE step_id='stop'"
        ).fetchone()[0] == "outcome_unknown"
        assert connection.execute(
            "SELECT receipt_type,outcome FROM operation_receipts"
        ).fetchone() == ("provider_response_lost", "outcome_unknown")
    controller.close()
    assert TICKET not in controller._bindings


def test_reconnect_rejects_cursor_or_cas_before_resize(tmp_path):
    journal = operation_store.initialize(tmp_path / "reconnect.sqlite3")
    tickets = ControlTickets(_control_ticket(cursor=terminal_ticket_store.MAX_CURSOR))
    engine = ControlEngine()
    controller = _control_controller(tickets, engine, journal)
    with pytest.raises(workspace_terminal.WorkspaceTerminalError) as rejected:
        controller.reconnect(
            PROJECT, WORKSPACE, TICKET, revision=1, generation=1,
            cursor=terminal_ticket_store.MAX_CURSOR, cols=80, rows=24,
        )
    assert rejected.value.code == "reconnect_cursor_conflict"
    assert engine.resizes == 0
    tickets = ControlTickets(_control_ticket())
    tickets.fail_update = True
    engine = ControlEngine()
    controller = _control_controller(tickets, engine, journal)
    with pytest.raises(workspace_terminal.WorkspaceTerminalError) as rejected:
        controller.reconnect(
            PROJECT, WORKSPACE, TICKET, revision=1, generation=1,
            cursor=0, cols=80, rows=24,
        )
    assert rejected.value.code == "revision_conflict"
    assert engine.resizes == 0


def test_natural_exit_persists_exited_projection_without_websocket(tmp_path):
    journal = operation_store.initialize(tmp_path / "exit.sqlite3")
    tickets = ControlTickets(_control_ticket())
    controller = _control_controller(tickets, ControlEngine(alive=False), journal)
    assert controller.list_tickets(PROJECT, WORKSPACE)["items"][0]["runtime"]["state"] == "exited"
    assert tickets.value.observed_state == "stopped"
    restarted = _control_controller(tickets, ControlEngine(), journal)
    restarted._bindings.clear()
    assert restarted.list_tickets(PROJECT, WORKSPACE)["items"][0]["runtime"]["state"] == "exited"
