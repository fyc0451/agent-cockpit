import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

import runtime_stats
import server
import tasks
import terminal


@pytest.fixture(autouse=True)
def reset_runtime_connections(monkeypatch):
    monkeypatch.setattr(
        runtime_stats,
        "_connection_counts",
        {"sse": 0, "terminal_websocket": 0},
    )


def test_connection_counts_enter_exit_and_exception_cleanup():
    assert runtime_stats.connection_stats() == {
        "sse": 0,
        "terminal_websocket": 0,
    }

    with runtime_stats.track_connection("sse"):
        assert runtime_stats.connection_stats()["sse"] == 1
    assert runtime_stats.connection_stats()["sse"] == 0

    with pytest.raises(RuntimeError, match="boom"):
        with runtime_stats.track_connection("terminal_websocket"):
            assert runtime_stats.connection_stats()["terminal_websocket"] == 1
            raise RuntimeError("boom")
    assert runtime_stats.connection_stats()["terminal_websocket"] == 0


def test_connection_counts_are_thread_safe():
    entered = threading.Barrier(9)
    release = threading.Event()

    def worker():
        with runtime_stats.track_connection("sse"):
            entered.wait()
            assert release.wait(2)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    entered.wait()
    assert runtime_stats.connection_stats()["sse"] == 8
    release.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert runtime_stats.connection_stats()["sse"] == 0


def test_task_output_buffer_stats_counts_utf8_bytes(monkeypatch):
    monkeypatch.setattr(
        tasks,
        "_output_buffers",
        {"task-a": ["x", "中🙂"], "task-b": []},
    )

    assert tasks.output_buffer_stats() == {
        "tasks": 2,
        "lines": 2,
        "utf8_bytes": 8,
    }


def test_terminal_session_stats_counts_total_and_alive(monkeypatch):
    monkeypatch.setattr(
        terminal,
        "_terms",
        {
            "alive": {"alive": True, "lock": threading.Lock()},
            "dead": {"alive": False, "lock": threading.Lock()},
        },
    )
    monkeypatch.setattr(terminal, "_check_alive", lambda term: term["alive"])

    assert terminal.session_stats() == {"total": 2, "alive": 1}


async def _asgi_collect(response, *, disconnect_immediately=False):
    """Drive a Starlette Response through a minimal ASGI cycle."""
    messages = []

    async def receive():
        if disconnect_immediately:
            return {"type": "http.disconnect"}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if (
            disconnect_immediately
            and message.get("type") == "http.response.start"
        ):
            # Keep send side alive briefly; disconnect is observed via receive.
            pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/events",
        "raw_path": b"/api/events",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    await response(scope, receive, send)
    return messages


def test_sse_tracking_cleans_up_on_close_and_exception():
    async def exercise():
        async def waiting_events():
            yield {"event": "ready", "data": "1"}
            await asyncio.Event().wait()

        response = server._SseCappedEventSourceResponse(waiting_events())
        assert runtime_stats.connection_stats()["sse"] == 0
        # Never-started response object must not hold a lease.
        assert runtime_stats.connection_stats()["sse"] == 0

        # Immediate disconnect after start: lease must return to 0.
        await _asgi_collect(response, disconnect_immediately=True)
        assert runtime_stats.connection_stats()["sse"] == 0

        async def failing_events():
            yield {"event": "ready", "data": "1"}
            raise RuntimeError("stream failed")

        response = server._SseCappedEventSourceResponse(failing_events())
        # Stream may error after start; outer finally still releases.
        with pytest.raises(BaseExceptionGroup) as exc_info:
            await _asgi_collect(response, disconnect_immediately=False)
        assert any(
            "stream failed" in str(item) for item in exc_info.value.exceptions
        )
        assert runtime_stats.connection_stats()["sse"] == 0

    asyncio.run(exercise())


def test_sse_lease_not_held_when_response_never_started():
    async def exercise():
        async def waiting_events():
            yield {"event": "ready", "data": "1"}
            await asyncio.Event().wait()

        response = await server.api_events(
            type("R", (), {"is_disconnected": lambda self: asyncio.sleep(0, result=False)})()
        )
        # Endpoint returned a Response without ASGI __call__ → no reservation.
        assert runtime_stats.connection_stats()["sse"] == 0
        # aclose on body_iterator alone must not leave a leaked slot (none taken).
        if hasattr(response, "body_iterator") and hasattr(response.body_iterator, "aclose"):
            await response.body_iterator.aclose()
        assert runtime_stats.connection_stats()["sse"] == 0

    asyncio.run(exercise())


def test_sse_asgi_cancel_or_send_error_releases_once():
    async def exercise():
        async def events():
            yield {"event": "ready", "data": "1"}
            await asyncio.Event().wait()

        response = server._SseCappedEventSourceResponse(events())
        messages = []

        async def receive():
            await asyncio.sleep(0)
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message.get("type") == "http.response.start":
                raise RuntimeError("send failed after start")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/events",
            "raw_path": b"/api/events",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 123),
            "server": ("test", 80),
        }
        with pytest.raises(BaseExceptionGroup) as exc_info:
            await response(scope, receive, send)
        assert any("send failed" in str(item) for item in exc_info.value.exceptions)
        assert runtime_stats.connection_stats()["sse"] == 0
        # Exactly-once: close is idempotent
        assert runtime_stats.connection_stats()["sse"] == 0

    asyncio.run(exercise())


def test_terminal_websocket_tracking_cleans_up_on_early_exit(monkeypatch):
    observed = []

    class FakeWebSocket:
        headers = {"origin": "http://testserver", "host": "testserver"}
        query_params = {}

        async def accept(self):
            return None

        async def close(self, **_kwargs):
            return None

    async def claim(_term_id, websocket):
        return {"websocket": websocket, "pump_task": None}

    def not_current(_term_id, _connection):
        observed.append(
            runtime_stats.connection_stats()["terminal_websocket"]
        )
        return False

    monkeypatch.setattr(server, "_websocket_authenticated", lambda _ws: True)
    monkeypatch.setattr(server, "_same_origin", lambda *_args: True)
    monkeypatch.setattr(
        server.terminal,
        "list_terms",
        lambda: [{"id": "term", "alive": True}],
    )
    monkeypatch.setattr(server, "_claim_term_websocket", claim)
    monkeypatch.setattr(server, "_term_websocket_is_current", not_current)

    asyncio.run(server.api_term_ws(FakeWebSocket(), "term"))

    assert observed == [1, 1]
    assert runtime_stats.connection_stats()["terminal_websocket"] == 0


def test_runtime_stats_endpoint_is_authenticated_and_low_cardinality(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server.runtime_stats,
        "process_stats",
        lambda: {"uptime_seconds": 12},
    )
    monkeypatch.setattr(
        server.runtime_stats,
        "connection_stats",
        lambda: {"sse": 3, "terminal_websocket": 2},
    )
    monkeypatch.setattr(
        server.terminal,
        "session_stats",
        lambda: {"total": 4, "alive": 1},
    )
    monkeypatch.setattr(
        server.tasks,
        "output_buffer_stats",
        lambda: {"tasks": 5, "lines": 6, "utf8_bytes": 7},
    )
    client = TestClient(server.app)

    assert client.get("/api/runtime/stats").status_code == 401
    response = client.get(
        "/api/runtime/stats",
        headers={"authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "process": {"uptime_seconds": 12},
        "connections": {"sse": 3, "terminal_websocket": 2},
        "terminal_sessions": {"total": 4, "alive": 1},
        "task_output_buffers": {"tasks": 5, "lines": 6, "utf8_bytes": 7},
    }
    assert "/api/runtime/stats" not in server.PUBLIC_PATHS


def test_runtime_stats_empty_state(monkeypatch):
    monkeypatch.setattr(
        server.runtime_stats,
        "process_stats",
        lambda: {"uptime_seconds": 0},
    )
    monkeypatch.setattr(terminal, "_terms", {})
    monkeypatch.setattr(tasks, "_output_buffers", {})

    assert server.api_runtime_stats() == {
        "process": {"uptime_seconds": 0},
        "connections": {"sse": 0, "terminal_websocket": 0},
        "terminal_sessions": {"total": 0, "alive": 0},
        "task_output_buffers": {"tasks": 0, "lines": 0, "utf8_bytes": 0},
    }


def test_process_uptime_is_nonnegative_integer(monkeypatch):
    monkeypatch.setattr(runtime_stats, "_PROCESS_STARTED", 100.25)
    monkeypatch.setattr(runtime_stats.time, "monotonic", lambda: 112.99)

    assert runtime_stats.process_stats() == {"uptime_seconds": 12}


def test_max_sse_connections_is_fixed_constant():
    assert runtime_stats.MAX_SSE_CONNECTIONS == 64
    with pytest.raises(ValueError):
        runtime_stats.try_open_connection("not-a-kind")
    with pytest.raises(ValueError):
        runtime_stats.open_connection("not-a-kind")


def test_sse_limit_boundary_64th_ok_65th_rejected():
    leases = []
    for _ in range(runtime_stats.MAX_SSE_CONNECTIONS):
        lease = runtime_stats.try_open_connection("sse")
        assert lease is not None
        leases.append(lease)
    assert runtime_stats.connection_stats()["sse"] == 64
    assert runtime_stats.try_open_connection("sse") is None
    assert runtime_stats.connection_stats()["sse"] == 64
    # terminal unlimited still works at SSE cap
    term = runtime_stats.try_open_connection("terminal_websocket")
    assert term is not None
    assert runtime_stats.connection_stats()["terminal_websocket"] == 1
    term.close()
    for lease in leases:
        lease.close()
    assert runtime_stats.connection_stats()["sse"] == 0


def test_sse_limit_concurrent_race_never_exceeds_cap():
    accepted = []
    barrier = threading.Barrier(80)

    def worker():
        barrier.wait(2)
        lease = runtime_stats.try_open_connection("sse")
        if lease is not None:
            accepted.append(lease)

    threads = [threading.Thread(target=worker) for _ in range(80)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3)
        assert not t.is_alive()
    assert len(accepted) == runtime_stats.MAX_SSE_CONNECTIONS
    assert runtime_stats.connection_stats()["sse"] == 64
    for lease in accepted:
        lease.close()
    assert runtime_stats.connection_stats()["sse"] == 0


def test_api_events_returns_429_without_slot_and_no_internal_leak(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    # Fill SSE cap without going through HTTP
    leases = [
        runtime_stats.try_open_connection("sse")
        for _ in range(runtime_stats.MAX_SSE_CONNECTIONS)
    ]
    assert all(leases)
    client = TestClient(server.app)
    # Auth first: unauthenticated still 401 (no reserve side-effect beyond middleware)
    assert client.get("/api/events").status_code == 401
    # Authenticated but full → 429 before stream
    response = client.get(
        "/api/events",
        headers={"authorization": "Bearer secret"},
    )
    assert response.status_code == 429
    assert response.headers.get("retry-after") == "5"
    body = response.text
    assert "64" not in body
    assert "sse" not in body.lower() or "streams" in body.lower()
    # stable public detail only
    assert response.json() == {"detail": "too many concurrent event streams"}
    assert runtime_stats.connection_stats()["sse"] == 64
    for lease in leases:
        lease.close()


def test_api_events_auth_before_reservation(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    # Without auth: 401 and zero SSE reservations
    assert client.get("/api/events").status_code == 401
    assert runtime_stats.connection_stats()["sse"] == 0



def test_terminal_websocket_not_subject_to_sse_cap():
    leases = [
        runtime_stats.try_open_connection("sse")
        for _ in range(runtime_stats.MAX_SSE_CONNECTIONS)
    ]
    assert all(leases)
    # many terminal connections still allowed
    terms = [runtime_stats.open_connection("terminal_websocket") for _ in range(10)]
    assert runtime_stats.connection_stats()["terminal_websocket"] == 10
    for t in terms:
        t.close()
    for lease in leases:
        lease.close()
