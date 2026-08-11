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


def test_sse_tracking_cleans_up_on_close_and_exception():
    async def exercise():
        async def waiting_events():
            yield {"event": "ready"}
            await asyncio.Event().wait()

        tracked = server._track_sse_events(waiting_events())
        assert await anext(tracked) == {"event": "ready"}
        assert runtime_stats.connection_stats()["sse"] == 1
        await tracked.aclose()
        assert runtime_stats.connection_stats()["sse"] == 0

        async def failing_events():
            yield {"event": "ready"}
            raise RuntimeError("stream failed")

        tracked = server._track_sse_events(failing_events())
        await anext(tracked)
        assert runtime_stats.connection_stats()["sse"] == 1
        with pytest.raises(RuntimeError, match="stream failed"):
            await anext(tracked)
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
