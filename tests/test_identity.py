import sqlite3
import threading

import pytest

import db
import server


@pytest.fixture
def identity_db(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE projects (id INTEGER PRIMARY KEY, human_key TEXT);
        CREATE TABLE agents (
            id INTEGER PRIMARY KEY,
            project_id INTEGER,
            name TEXT,
            program TEXT,
            model TEXT,
            inception_ts REAL,
            retired_at REAL
        );
        INSERT INTO projects VALUES (1, '/project');
        """
    )
    monkeypatch.setattr(db, "_conn", con)
    yield con
    con.close()


def test_identity_prefers_exact_program(identity_db):
    identity_db.executescript(
        """
        INSERT INTO agents VALUES (1, 1, 'exact', 'qodercli', '', 1, NULL);
        INSERT INTO agents VALUES (2, 1, 'newer-alias', 'qodercn', '', 2, NULL);
        """
    )

    assert db.identity_by_cwd("/project", "qodercli")["name"] == "exact"


def test_identity_does_not_fuzzy_match_unrelated_program(identity_db):
    identity_db.execute(
        "INSERT INTO agents VALUES (1, 1, 'wrong', 'my-qoder-fork', '', 1, NULL)"
    )

    assert db.identity_by_cwd("/project", "qodercli") is None


def test_identity_accepts_known_legacy_alias(identity_db):
    identity_db.execute(
        "INSERT INTO agents VALUES (1, 1, 'legacy', 'kimi-work', '', 1, NULL)"
    )

    assert db.identity_by_cwd("/project", "kimi")["name"] == "legacy"


def test_server_identity_name_uses_registered_identity(monkeypatch):
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda cwd, program: {"name": "GentleCompass"},
    )

    assert server._identity_name("/project", "claude") == "GentleCompass"


def test_server_identity_falls_back_to_main_worktree(monkeypatch):
    seen = []
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda cwd, program: seen.append(cwd) or (
            {"name": "codex-main", "program": program, "human_key": cwd}
            if cwd == "/project" else None
        ),
    )
    monkeypatch.setattr(
        server,
        "_git",
        lambda cwd, *args: "worktree /project\nHEAD abc\n\nworktree /tmp/review\nHEAD def",
    )

    assert server._identity_name("/tmp/review", "codex") == "codex-main"
    assert seen == ["/tmp/review", "/project"]


def test_server_identity_preserves_nested_path_across_worktrees(monkeypatch):
    seen = []
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda cwd, program: seen.append(cwd) or (
            {"name": "codex-main"} if cwd == "/project/apps/api" else None
        ),
    )
    monkeypatch.setattr(
        server,
        "_git",
        lambda cwd, *args: "worktree /project\nHEAD abc\n\nworktree /tmp/review\nHEAD def",
    )

    assert server._identity_name("/tmp/review/apps/api", "codex") == "codex-main"
    assert seen == ["/tmp/review/apps/api", "/project/apps/api"]


def test_db_queries_serialize_shared_connection(monkeypatch):
    first_entered = threading.Event()
    release_first = threading.Event()
    overlap = threading.Event()
    state_lock = threading.Lock()
    errors = []

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConnection:
        active = 0

        def execute(self, *_args):
            with state_lock:
                self.active += 1
                first = self.active == 1
                if self.active > 1:
                    overlap.set()
            if first:
                first_entered.set()
                release_first.wait(1)
            with state_lock:
                self.active -= 1
            return FakeCursor()

    monkeypatch.setattr(db, "_conn", FakeConnection())

    def query():
        try:
            db._rows("SELECT 1")
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=query)
    second = threading.Thread(target=query)
    first.start()
    assert first_entered.wait(1)
    second.start()
    overlap.wait(0.2)
    release_first.set()
    first.join(1)
    second.join(1)

    assert not errors
    assert not overlap.is_set()
