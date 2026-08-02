import sqlite3

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
