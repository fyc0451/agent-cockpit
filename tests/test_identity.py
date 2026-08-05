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


@pytest.fixture
def mail_db(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            human_key TEXT,
            created_at REAL,
            archived_at REAL
        );
        CREATE TABLE agents (
            id INTEGER PRIMARY KEY,
            project_id INTEGER,
            name TEXT,
            program TEXT,
            model TEXT,
            task_description TEXT,
            inception_ts REAL,
            last_active_ts REAL,
            contact_policy TEXT,
            retired_at REAL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            project_id INTEGER,
            thread_id TEXT,
            topic TEXT,
            subject TEXT,
            body_md TEXT,
            importance TEXT,
            ack_required INTEGER,
            created_ts REAL,
            reply_to INTEGER,
            sender_id INTEGER
        );
        CREATE TABLE message_recipients (
            message_id INTEGER,
            agent_id INTEGER,
            kind TEXT,
            read_ts REAL,
            ack_ts REAL
        );
        INSERT INTO projects VALUES (1, 'active', '/active', 1, NULL);
        INSERT INTO projects VALUES (2, 'archived', '/archived', 2, 3);
        INSERT INTO agents VALUES
            (1, 1, 'active-agent', 'codex', '', '', 1, 1, 'open', NULL),
            (2, 2, 'archived-agent', 'codex', '', '', 1, 1, 'open', NULL);
        INSERT INTO messages VALUES
            (1, 1, 't1', '', 'one', '', 'normal', 0, 1, NULL, 1),
            (2, 1, 't2', '', 'two', '', 'normal', 0, 2, NULL, 1),
            (3, 2, 't3', '', 'archived', '', 'normal', 0, 3, NULL, 2);
        INSERT INTO message_recipients VALUES
            (1, 1, 'to', NULL, NULL),
            (2, 1, 'to', NULL, NULL),
            (3, 2, 'to', NULL, NULL);
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


def test_identity_accepts_claude_code_program(identity_db):
    identity_db.execute(
        "INSERT INTO agents VALUES (1, 1, 'claude-main', 'claude-code', '', 1, NULL)"
    )

    assert db.identity_by_cwd("/project", "claude")["name"] == "claude-main"


def test_server_identity_name_uses_registered_identity(monkeypatch):
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda cwd, program: {"name": "GentleCompass"},
    )

    assert server._identity_name("/project", "claude") == "GentleCompass"


def test_server_identity_does_not_guess_a_main_worktree(monkeypatch):
    seen = []
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda cwd, program: seen.append(cwd) or (
            {"name": "codex-main", "program": program, "human_key": cwd}
            if cwd == "/project" else None
        ),
    )
    assert server._identity_name("/tmp/review", "codex") is None
    assert seen == ["/tmp/review"]


def test_server_identity_uses_only_the_given_canonical_key(monkeypatch):
    seen = []
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda cwd, program: seen.append(cwd) or (
            {"name": "codex-main"} if cwd == "/project/apps/api" else None
        ),
    )
    assert server._identity_name("/tmp/review/apps/api", "codex") is None
    assert seen == ["/tmp/review/apps/api"]


def test_board_snapshot_adds_identity_from_session_binding(monkeypatch):
    snapshot = {
        "sessions": [{"session": "demo", "directory": "/sessions/demo"}],
        "panes": [
            {"session": "demo", "pane_id": "w1:p1", "agent": "codex"},
            {"session": "demo", "pane_id": "w1:p2", "agent": None},
        ],
    }
    monkeypatch.setattr(server.herdr_client, "snapshot", lambda: snapshot)
    monkeypatch.setattr(
        server.mail_projects,
        "get",
        lambda session, session_dir: "/project"
        if (session, session_dir) == ("demo", "/sessions/demo") else None,
    )
    seen = []
    monkeypatch.setattr(
        server,
        "_identity_name",
        lambda project, agent: seen.append((project, agent)) or "codex-main",
    )

    result = server._board_snapshot()

    assert result["panes"][0]["mail_name"] == "codex-main"
    assert "mail_name" not in result["panes"][1]
    assert seen == [("/project", "codex")]


def test_board_snapshot_omits_identity_without_binding(monkeypatch):
    snapshot = {
        "sessions": [{"session": "demo", "directory": "/sessions/demo"}],
        "panes": [{"session": "demo", "pane_id": "w1:p1", "agent": "codex"}],
    }
    monkeypatch.setattr(server.herdr_client, "snapshot", lambda: snapshot)
    monkeypatch.setattr(server.mail_projects, "get", lambda *_: None)
    monkeypatch.setattr(
        server,
        "_identity_name",
        lambda *_: (_ for _ in ()).throw(AssertionError("无绑定时不能猜花名")),
    )

    assert "mail_name" not in server._board_snapshot()["panes"][0]


def test_agent_mail_db_prefers_new_xdg_install_path(monkeypatch, tmp_path):
    data_home = tmp_path / "share"
    new_db = data_home / "mcp_agent_mail" / "storage.sqlite3"
    legacy_db = tmp_path / "mcp_agent_mail" / "storage.sqlite3"
    new_db.parent.mkdir(parents=True)
    legacy_db.parent.mkdir(parents=True)
    new_db.touch()
    legacy_db.touch()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.delenv("AGENT_MAIL_DB_PATH", raising=False)

    assert db._resolve_db_path() == new_db


def test_agent_mail_db_keeps_legacy_path_compatible(monkeypatch, tmp_path):
    legacy_db = tmp_path / "mcp_agent_mail" / "storage.sqlite3"
    legacy_db.parent.mkdir(parents=True)
    legacy_db.touch()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("AGENT_MAIL_DB_PATH", raising=False)

    assert db._resolve_db_path() == legacy_db


def test_agent_mail_db_explicit_path_has_priority(monkeypatch, tmp_path):
    configured = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("AGENT_MAIL_DB_PATH", str(configured))

    assert db._resolve_db_path() == configured


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


def test_global_unread_excludes_archived_projects(mail_db):
    assert db.global_unread_count() == 2
    assert [row["project_slug"] for row in db.unread_by_agent()] == ["active"]
    assert db.overview()["total_unread"] == 2


def test_overview_does_not_run_redundant_agent_unread_query(monkeypatch):
    monkeypatch.setattr(db, "project_stats", lambda: [])
    monkeypatch.setattr(db, "unread_by_project", lambda: {})
    monkeypatch.setattr(db, "global_unread_count", lambda: 0)
    monkeypatch.setattr(
        db,
        "unread_by_agent",
        lambda: (_ for _ in ()).throw(AssertionError("不应执行冗余查询")),
    )

    assert db.overview() == {
        "projects": [],
        "total_unread": 0,
        "total_projects": 0,
        "total_agents": 0,
    }


def test_recent_messages_batches_recipient_query(mail_db, monkeypatch):
    calls = []
    original_rows = db._rows

    def tracked_rows(sql, params=()):
        calls.append((sql, params))
        return original_rows(sql, params)

    monkeypatch.setattr(db, "_rows", tracked_rows)

    messages = db.recent_messages(1)

    assert [message["id"] for message in messages] == [2, 1]
    assert [message["recipients"][0]["name"] for message in messages] == [
        "active-agent",
        "active-agent",
    ]
    assert len(calls) == 2
