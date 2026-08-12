from __future__ import annotations

import gc
import os
import sqlite3
from pathlib import Path

import pytest

from agent_cockpit import coordination, tasks, web_push


def _fd_root() -> Path:
    fd_root = Path("/proc/self/fd")
    if not fd_root.is_dir():
        fd_root = Path("/dev/fd")
    if not fd_root.is_dir():
        pytest.skip("fd directory is unavailable")
    return fd_root


def _total_fd_count() -> int:
    return sum(1 for _ in _fd_root().iterdir())


def _db_fd_count(path: Path) -> int:
    targets = {str(path), f"{path}-wal", f"{path}-shm"}
    count = 0
    for entry in _fd_root().iterdir():
        try:
            target = os.readlink(entry).removesuffix(" (deleted)")
        except OSError:
            continue
        count += target in targets
    return count


@pytest.mark.parametrize(
    ("module", "factory_name", "managed_name"),
    [
        (coordination, "_connect", "_managed_connection"),
        (tasks, "_db", "_managed_db"),
        (web_push, "_db", "_managed_db"),
    ],
)
def test_managed_connection_commits_and_closes(
    tmp_path, monkeypatch, module, factory_name, managed_name,
):
    path = tmp_path / f"{module.__name__.rsplit('.', 1)[-1]}.sqlite3"
    setup = sqlite3.connect(path)
    setup.execute("CREATE TABLE proof(value INTEGER)")
    setup.close()
    opened = []

    def connect():
        con = sqlite3.connect(path)
        opened.append(con)
        return con

    monkeypatch.setattr(module, factory_name, connect)
    with getattr(module, managed_name)() as con:
        con.execute("INSERT INTO proof VALUES (1)")

    with sqlite3.connect(path) as check:
        assert check.execute("SELECT value FROM proof").fetchall() == [(1,)]
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


@pytest.mark.parametrize(
    ("module", "factory_name", "managed_name"),
    [
        (coordination, "_connect", "_managed_connection"),
        (tasks, "_db", "_managed_db"),
        (web_push, "_db", "_managed_db"),
    ],
)
def test_managed_connection_rolls_back_exception_and_closes(
    tmp_path, monkeypatch, module, factory_name, managed_name,
):
    path = tmp_path / f"{module.__name__.rsplit('.', 1)[-1]}.sqlite3"
    setup = sqlite3.connect(path)
    setup.execute("CREATE TABLE proof(value INTEGER)")
    setup.close()
    opened = []

    def connect():
        con = sqlite3.connect(path)
        opened.append(con)
        return con

    monkeypatch.setattr(module, factory_name, connect)
    with pytest.raises(RuntimeError, match="rollback proof"):
        with getattr(module, managed_name)() as con:
            con.execute("INSERT INTO proof VALUES (1)")
            raise RuntimeError("rollback proof")

    with sqlite3.connect(path) as check:
        assert check.execute("SELECT value FROM proof").fetchall() == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


def test_read_hot_paths_keep_database_fd_count_stable_with_gc_disabled(
    tmp_path, monkeypatch,
):
    paths = {
        "coordination": tmp_path / "coordination.sqlite3",
        "tasks": tmp_path / "tasks.sqlite3",
        "web_push": tmp_path / "push.sqlite3",
    }
    monkeypatch.setattr(coordination, "DB_PATH", paths["coordination"])
    monkeypatch.setattr(tasks, "TASKS_DB", paths["tasks"])
    monkeypatch.setattr(tasks, "_db_swept", False)
    monkeypatch.setattr(web_push, "DB_PATH", paths["web_push"])

    coordination.list_assignments()
    tasks.list_tasks()
    web_push.list_subscriptions()
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        calls = (
            (paths["coordination"], coordination.message_project_signatures),
            (paths["tasks"], tasks.list_tasks),
            (paths["web_push"], web_push.list_subscriptions),
        )
        for path, call in calls:
            db_before = _db_fd_count(path)
            total_before = _total_fd_count()
            for _ in range(100):
                call()
            assert _db_fd_count(path) == db_before
            assert _total_fd_count() <= total_before
    finally:
        if was_enabled:
            gc.enable()


class _FailingConnection:
    def __init__(self) -> None:
        self.closed = False
        self.row_factory = None

    def execute(self, *_args, **_kwargs):
        raise RuntimeError("initialization failed")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("module", [coordination, tasks, web_push])
def test_connection_initialization_failure_closes_connection(
    tmp_path, monkeypatch, module,
):
    path = tmp_path / f"{module.__name__.rsplit('.', 1)[-1]}.sqlite3"
    fake = _FailingConnection()
    monkeypatch.setattr(module.sqlite3, "connect", lambda *_args, **_kwargs: fake)
    if module is coordination:
        monkeypatch.setattr(module, "DB_PATH", path)
    elif module is tasks:
        monkeypatch.setattr(module, "TASKS_DB", path)
        monkeypatch.setattr(module, "_db_swept", False)
    else:
        monkeypatch.setattr(module, "DB_PATH", path)

    factory = module._connect if module is coordination else module._db
    with pytest.raises(RuntimeError, match="initialization failed"):
        factory()

    assert fake.closed is True
