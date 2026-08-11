"""Historical task statistics and fixed-route regressions."""
from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

import runtime_paths
import server
import tasks


@pytest.fixture()
def task_db(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_DATA_DIR", str(tmp_path))
    runtime_paths.reset_cache()
    monkeypatch.setattr(tasks, "DATA_DIR", tmp_path)
    monkeypatch.setattr(tasks, "TASKS_DB", tmp_path / "tasks.sqlite3")
    monkeypatch.setattr(tasks, "WORKTREE_ROOT", tmp_path / "worktrees")
    tasks._db_swept = False
    tasks._init_db()
    yield
    runtime_paths.reset_cache()


def _insert_task(
    task_id: str,
    status: str,
    *,
    started: object = None,
    finished: object = None,
) -> None:
    with tasks._db() as con:
        con.execute(
            "INSERT INTO tasks "
            "(id, workdir, prompt, status, created_ts, started_ts, finished_ts) "
            "VALUES (?, '/tmp/project', 'test', ?, 1, ?, ?)",
            (task_id, status, started, finished),
        )
        con.commit()


def test_task_stats_empty_database(task_db):
    assert tasks.task_stats() == {
        "total": 0,
        "status_counts": {
            "pending": 0,
            "running": 0,
            "done": 0,
            "failed": 0,
            "cancelled": 0,
        },
        "unknown_count": 0,
        "terminal_count": 0,
        "completion_rate": None,
        "duration_count": 0,
        "median_duration_seconds": None,
        "p95_duration_seconds": None,
    }


def test_task_stats_known_unknown_and_nearest_rank_durations(task_db):
    rows = [
        ("pending", "pending", None, None),
        ("running", "running", 20, None),
        ("done-1", "done", 10, 11),
        ("done-2", "done", 10, 12),
        ("failed", "failed", 10, 13),
        ("cancelled", "cancelled", 10, 14),
        ("unknown", "paused", 10, 9),
        ("nonfinite", "mystery", 10, math.inf),
    ]
    for task_id, status, started, finished in rows:
        _insert_task(task_id, status, started=started, finished=finished)

    stats = tasks.task_stats()

    assert stats["total"] == 8
    assert stats["status_counts"] == {
        "pending": 1,
        "running": 1,
        "done": 2,
        "failed": 1,
        "cancelled": 1,
    }
    assert stats["unknown_count"] == 2
    assert stats["terminal_count"] == 4
    assert stats["completion_rate"] == 0.5
    assert stats["duration_count"] == 4
    # Durations [1, 2, 3, 4]: nearest-rank P50 is rank 2, P95 is rank 4.
    assert stats["median_duration_seconds"] == 2
    assert stats["p95_duration_seconds"] == 4


def test_task_stats_reads_every_persisted_task_without_list_limit(task_db):
    for index in range(75):
        _insert_task(f"done-{index}", "done", started=0, finished=index + 1)

    assert len(tasks.list_tasks()) == 50
    stats = tasks.task_stats()
    assert stats["total"] == 75
    assert stats["status_counts"]["done"] == 75
    assert stats["duration_count"] == 75


def test_task_stats_route_precedes_dynamic_task_route(monkeypatch):
    expected = {
        "total": 7,
        "status_counts": {},
        "unknown_count": 0,
        "terminal_count": 0,
        "completion_rate": None,
        "duration_count": 0,
        "median_duration_seconds": None,
        "p95_duration_seconds": None,
    }
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "")
    monkeypatch.setattr(server.tasks, "task_stats", lambda: expected)

    def dynamic_route_must_not_run(_task_id):
        raise AssertionError("/api/tasks/stats was swallowed by {task_id}")

    monkeypatch.setattr(server.tasks, "get_task", dynamic_route_must_not_run)
    response = TestClient(server.app, client=("127.0.0.1", 50000)).get(
        "/api/tasks/stats"
    )
    assert response.status_code == 200
    assert response.json() == expected
