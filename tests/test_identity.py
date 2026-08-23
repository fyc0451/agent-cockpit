import json
import re
import sqlite3
import threading

import pytest

from agent_cockpit import db
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
        "identity_for_chat_pane",
        lambda cwd, program: {"name": "GentleCompass"},
    )

    assert server._identity_name("/project", "claude") == "GentleCompass"


def test_managed_identity_lookup_is_exact_by_opaque_instance(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    instance_id = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    registry_root = tmp_path / "registry"
    registry_dir = registry_root / re.sub(
        r"[^A-Za-z0-9]+", "-", str(project.resolve()),
    ).strip("-").lower()
    registry_dir.mkdir(parents=True)
    identity_file = registry_dir / f"codex--{instance_id}.json"
    identity_file.write_text(json.dumps({
        "project_key": str(project),
        "project_slug": "project",
        "agent": "codex",
        "instance": instance_id,
        "name": "FreshMailbox",
        "registration_token": "secret",
        "program": "codex-cli",
        "model": "gpt",
        "hub": "http://127.0.0.1:8765",
    }))
    identity_file.chmod(0o600)
    monkeypatch.setattr(server, "_REGISTRY_ROOT", registry_root)
    seen = []
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda cwd, program, name=None: seen.append((cwd, program, name)) or (
            {"name": name, "program": "codex-cli", "human_key": cwd}
            if name == "FreshMailbox" else None
        ),
    )

    assert server._identity_name(str(project), "codex", instance_id) == "FreshMailbox"
    assert seen == [(str(project), "codex", "FreshMailbox")]


def test_dev_registry_scope_includes_all_workspaces(monkeypatch):
    monkeypatch.setattr(server.next_profile, "is_dev", lambda: True)
    monkeypatch.setattr(
        server.next_profile,
        "project",
        lambda: (_ for _ in ()).throw(AssertionError("dev registry must be global")),
    )

    assert server._registry_project_scope() is None


def test_isolated_registry_scope_stays_single_project(monkeypatch):
    monkeypatch.setattr(server.next_profile, "is_dev", lambda: False)
    monkeypatch.setattr(server.next_profile, "project", lambda: "/isolated/project")

    assert server._registry_project_scope() == "/isolated/project"


def test_managed_identity_lookup_separates_two_instances_and_rejects_mismatch(
    monkeypatch, tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    registry_root = tmp_path / "registry"
    registry_dir = registry_root / re.sub(
        r"[^A-Za-z0-9]+", "-", str(project.resolve()),
    ).strip("-").lower()
    registry_dir.mkdir(parents=True)
    first = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    second = "i-bbbbbbbbbbbbbbbbbbbbbbbbbb"

    def write(instance_id, name, **overrides):
        data = {
            "project_key": str(project), "project_slug": "project",
            "agent": "codex", "instance": instance_id, "name": name,
            "registration_token": "secret", "program": "codex-cli",
            "model": "gpt", "hub": "http://127.0.0.1:8765",
        }
        data.update(overrides)
        path = registry_dir / f"codex--{instance_id}.json"
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    write(first, "MailboxA")
    second_path = write(second, "MailboxB")
    monkeypatch.setattr(server, "_REGISTRY_ROOT", registry_root)
    seen = []
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda cwd, program, name=None: seen.append(name) or (
            {"name": name, "program": program, "human_key": cwd}
            if name in {"MailboxA", "MailboxB"} else None
        ),
    )

    assert server._identity_name(str(project), "codex", first) == "MailboxA"
    assert server._identity_name(str(project), "codex", second) == "MailboxB"
    assert server._identity_name(
        str(project), "codex", "i-cccccccccccccccccccccccccc",
    ) is None
    assert seen == ["MailboxA", "MailboxB"]

    bad = json.loads(second_path.read_text())
    bad["instance"] = first
    second_path.write_text(json.dumps(bad))
    second_path.chmod(0o600)
    assert server._identity_name(str(project), "codex", second) is None


def test_identity_hint_uses_opaque_instance_not_display_name():
    instance_id = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    hint = server._identity_hint(
        "FreshMailbox", "/tmp/project", "codex", instance_id=instance_id,
    )

    assert f"--instance {instance_id}" in hint
    assert "--instance main" not in hint


def test_server_identity_does_not_guess_a_main_worktree(monkeypatch):
    seen = []
    monkeypatch.setattr(
        server.db,
        "identity_for_chat_pane",
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
        "identity_for_chat_pane",
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
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: snapshot)
    monkeypatch.setattr(server.herdr_client, "list_active_launch_descriptors", lambda: [])
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
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: snapshot)
    monkeypatch.setattr(server.herdr_client, "list_active_launch_descriptors", lambda: [])
    monkeypatch.setattr(server.mail_projects, "get", lambda *_: None)
    monkeypatch.setattr(server, "_chat_workspace_root", lambda *_: None)
    monkeypatch.setattr(
        server,
        "_identity_name",
        lambda *_: (_ for _ in ()).throw(AssertionError("无绑定时不能猜花名")),
    )

    assert "mail_name" not in server._board_snapshot()["panes"][0]


def test_board_snapshot_uses_chat_workspace_when_unbound(monkeypatch, tmp_path):
    workspace = tmp_path / "scc"
    workspace.mkdir()
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "descriptors.json"),
    )
    snapshot = {
        "sessions": [{"session": "scc-1", "directory": "/sessions/scc-1"}],
        "panes": [{"session": "scc-1", "pane_id": "w1:p2", "agent": "grok"}],
    }
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: snapshot)
    monkeypatch.setattr(server.herdr_client, "list_active_launch_descriptors", lambda: [])
    monkeypatch.setattr(server.herdr_client, "get_launch_descriptor", lambda *_: None)
    monkeypatch.setattr(server.mail_projects, "get", lambda *_: None)
    monkeypatch.setattr(
        server, "_chat_workspace_root",
        lambda name: workspace if name == "scc-1" else None,
    )
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        server, "_identity_name",
        lambda project, agent, instance=None: (
            seen.append((str(project), agent)) or "DarkBrook"
        ),
    )

    result = server._board_snapshot()

    assert result["panes"][0]["mail_name"] == "DarkBrook"
    assert seen == [(str(workspace), "grok")]


def test_board_snapshot_fills_flower_for_stub_descriptor(monkeypatch, tmp_path):
    workspace = tmp_path / "scc"
    workspace.mkdir()
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "descriptors.json"),
    )
    snapshot = {
        "sessions": [{"session": "scc-1", "directory": "/sessions/scc-1"}],
        "panes": [{"session": "scc-1", "pane_id": "w1:p2", "agent": "grok"}],
    }
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: snapshot)
    monkeypatch.setattr(server.herdr_client, "list_active_launch_descriptors", lambda: [])
    monkeypatch.setattr(
        server.herdr_client, "get_launch_descriptor",
        lambda *_: {"session": "scc-1", "pane_id": "w1:p2", "agent": "grok", "name": "grok-1"},
    )
    monkeypatch.setattr(server.mail_projects, "get", lambda *_: None)
    monkeypatch.setattr(
        server, "_chat_workspace_root",
        lambda name: workspace if name == "scc-1" else None,
    )
    monkeypatch.setattr(server, "_identity_name", lambda *_a, **_k: "DarkBrook")

    result = server._board_snapshot()

    assert result["panes"][0]["mail_name"] == "DarkBrook"


def test_board_snapshot_ignores_leftover_session_leader(monkeypatch, tmp_path):
    from agent_cockpit import chat_roster

    workspace = tmp_path / "scc"
    workspace.mkdir()
    instance_id = "i-2amw527jf3zreyzsuceags3mc4"
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "descriptors.json"),
    )
    monkeypatch.setattr(chat_roster, "LEADERS_DIR", tmp_path / "leaders")
    chat_roster.set_session_leader("scc-1", "codex", "codex")
    server.herdr_client.save_launch_descriptor(
        session="scc-1", pane_id="w1:p3", name=instance_id, kind="codex",
        args=[], agent="codex", instance_id=instance_id, display_name="codex",
    )
    snapshot = {
        "sessions": [{"session": "scc-1", "directory": "/sessions/scc-1"}],
        "panes": [
            {"session": "scc-1", "pane_id": "w1:p2", "agent": "grok"},
            {"session": "scc-1", "pane_id": "w1:p3", "agent": "codex"},
        ],
    }
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: snapshot)
    monkeypatch.setattr(server.herdr_client, "list_active_launch_descriptors", lambda: [])
    monkeypatch.setattr(server.mail_projects, "get", lambda *_: None)
    monkeypatch.setattr(
        server, "_chat_workspace_root",
        lambda name: workspace if name == "scc-1" else None,
    )
    monkeypatch.setattr(
        server, "_identity_name",
        lambda project, agent, instance=None: (
            None if instance else ("DarkBrook" if agent == "grok" else "codex-main")
        ),
    )

    result = server._board_snapshot()
    names = {pane["pane_id"]: pane.get("mail_name") for pane in result["panes"]}
    assert names["w1:p2"] == "DarkBrook"
    assert names["w1:p3"] == "codex-main"


def test_board_snapshot_unique_instance_uses_project_mailbox(monkeypatch, tmp_path):
    workspace = tmp_path / "scc"
    workspace.mkdir()
    instance_id = "i-2amw527jf3zreyzsuceags3mc4"
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "descriptors.json"),
    )
    server.herdr_client.save_launch_descriptor(
        session="scc-1", pane_id="w1:p3", name=instance_id, kind="codex",
        args=[], agent="codex", instance_id=instance_id, display_name="codex",
    )
    snapshot = {
        "sessions": [{"session": "scc-1", "directory": "/sessions/scc-1"}],
        "panes": [{"session": "scc-1", "pane_id": "w1:p3", "agent": "codex"}],
    }
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: snapshot)
    monkeypatch.setattr(server.herdr_client, "list_active_launch_descriptors", lambda: [])
    monkeypatch.setattr(server.mail_projects, "get", lambda *_: None)
    monkeypatch.setattr(
        server, "_chat_workspace_root",
        lambda name: workspace if name == "scc-1" else None,
    )
    monkeypatch.setattr(
        server, "_identity_name",
        lambda project, agent, instance=None: (
            None if instance else "codex-main"
        ),
    )

    result = server._board_snapshot()

    assert result["panes"][0]["mail_name"] == "codex-main"


def test_board_snapshot_keeps_same_kind_managed_instances_separate(monkeypatch, tmp_path):
    first = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    second = "i-bbbbbbbbbbbbbbbbbbbbbbbbbb"
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "descriptors.json"),
    )
    for pane_id, instance_id, display_name in (
        ("w1:p1", first, "同名"), ("w1:p2", second, "同名"),
    ):
        server.herdr_client.save_launch_descriptor(
            session="demo", pane_id=pane_id, name=instance_id, kind="codex",
            args=[], agent="codex", instance_id=instance_id,
            display_name=display_name,
        )
    monkeypatch.setattr(
        server,
        "_herdr_runtime_snapshot",
        lambda: {
            "sessions": [{"session": "demo", "directory": "/sessions/demo"}],
            "panes": [
                {"session": "demo", "pane_id": "w1:p1", "agent": "codex"},
                {"session": "demo", "pane_id": "w1:p2", "agent": "codex"},
            ],
        },
    )
    monkeypatch.setattr(server.mail_projects, "get", lambda *_: "/project")
    seen = []
    monkeypatch.setattr(
        server,
        "_identity_name",
        lambda project, agent, instance_id: (
            seen.append((project, agent, instance_id)) or f"mail-{instance_id[-1]}"
        ),
    )

    result = server._board_snapshot()

    assert [pane["instance_id"] for pane in result["panes"]] == [first, second]
    assert [pane["display_name"] for pane in result["panes"]] == ["同名", "同名"]
    assert [pane["mail_name"] for pane in result["panes"]] == ["mail-a", "mail-b"]
    assert seen == [("/project", "codex", first), ("/project", "codex", second)]


def test_board_snapshot_uses_descriptor_mail_name_when_registry_misses(
    monkeypatch, tmp_path,
):
    instance_id = "i-7h657f4kbfpr3uujhfpe3wikha"
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "descriptors.json"),
    )
    server.herdr_client.save_launch_descriptor(
        session="pitapat-video-platform-1", pane_id="w1:p4",
        name=instance_id, kind="grok", args=[], agent="grok",
        instance_id=instance_id, display_name="grok",
    )
    server.herdr_client.update_launch_descriptor_by_instance(
        instance_id, mail_agent="grok", mail_instance=instance_id,
        mail_name="RusticEagle",
        mail_project="/home/fyc/pitapat/pitapat-video-platform",
    )
    snapshot = {
        "sessions": [{
            "session": "pitapat-video-platform-1",
            "directory": "/sessions/pitapat",
        }],
        "panes": [
            {
                "session": "pitapat-video-platform-1", "pane_id": "w1:p3",
                "agent": "grok", "mail_name": "TurquoiseBay",
            },
            {
                "session": "pitapat-video-platform-1", "pane_id": "w1:p4",
                "agent": "grok",
            },
        ],
    }
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: snapshot)
    monkeypatch.setattr(server.herdr_client, "list_active_launch_descriptors", lambda: [])
    monkeypatch.setattr(
        server.mail_projects, "get",
        lambda *_: "/home/fyc/pitapat/pitapat-video-platform",
    )
    monkeypatch.setattr(server, "_identity_name", lambda *_a, **_k: None)

    result = server._enrich_board_identities(snapshot)
    names = {pane["pane_id"]: pane.get("mail_name") for pane in result["panes"]}
    assert names["w1:p3"] == "TurquoiseBay"
    assert names["w1:p4"] == "RusticEagle"


def test_board_snapshot_uses_descriptor_product_for_zcode_registry(monkeypatch, tmp_path):
    instance_id = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "descriptors.json"),
    )
    server.herdr_client.save_launch_descriptor(
        session="demo", pane_id="w1:p1", name=instance_id, kind="opencode",
        args=[], agent="zcode", instance_id=instance_id, display_name="ZCode",
    )
    snapshot = {
        "sessions": [{"session": "demo", "directory": "/sessions/demo"}],
        "panes": [{
            "session": "demo", "pane_id": "w1:p1", "agent": "opencode",
        }],
    }
    monkeypatch.setattr(server.mail_projects, "get", lambda *_args: "/project")
    seen = []
    monkeypatch.setattr(
        server, "_identity_name",
        lambda project, agent, instance: seen.append((project, agent, instance))
        or "ZCodeMailbox",
    )

    result = server._enrich_board_identities(snapshot)

    assert result["panes"][0]["mail_name"] == "ZCodeMailbox"
    assert seen == [("/project", "zcode", instance_id)]


def test_board_snapshot_omits_ambiguous_legacy_main_for_same_type(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "descriptors.json"),
    )
    snapshot = {
        "sessions": [{"session": "demo", "directory": "/sessions/demo"}],
        "panes": [
            {"session": "demo", "pane_id": "w1:p1", "agent": "codex"},
            {"session": "demo", "pane_id": "w1:p2", "agent": "codex"},
        ],
    }
    monkeypatch.setattr(server.mail_projects, "get", lambda *_args: "/project")
    monkeypatch.setattr(
        server, "_identity_name",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("多个无 descriptor pane不得共享 legacy main")
        ),
    )

    result = server._enrich_board_identities(snapshot)

    assert all("mail_name" not in pane for pane in result["panes"])


def test_board_snapshot_prefers_session_leader_over_newest_identity(
    monkeypatch, tmp_path,
):
    from agent_cockpit import chat_roster

    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "descriptors.json"),
    )
    monkeypatch.setattr(chat_roster, "LEADERS_DIR", tmp_path / "leaders")
    chat_roster.set_session_leader("cockpit", "BrownDesert", "grok")
    snapshot = {
        "sessions": [{"session": "cockpit", "directory": "/sessions/cockpit"}],
        "panes": [
            {"session": "cockpit", "pane_id": "w1:p1", "agent": "grok"},
            {"session": "cockpit", "pane_id": "w1:p2", "agent": "codex"},
        ],
    }
    monkeypatch.setattr(server.mail_projects, "get", lambda *_args: "/project")
    monkeypatch.setattr(
        server, "_identity_name",
        lambda project, agent, instance=None: (
            "grok-agent-cockpit" if agent == "grok" else "codex-main"
        ),
    )

    result = server._enrich_board_identities(snapshot)

    assert result["panes"][0]["mail_name"] == "BrownDesert"
    assert result["panes"][1]["mail_name"] == "codex-main"


def test_identity_record_uses_registry_name_when_hub_retired(monkeypatch, tmp_path):
    from agent_cockpit import server

    instance = "i-yzh33bkopbhev3ae654tc7tila"
    cwd = "/home/fyc/github/agent-cockpit"
    monkeypatch.setattr(server.next_profile, "require_project", lambda path: path)
    monkeypatch.setattr(
        server,
        "_registry_identity_for_instance",
        lambda *_a, **_k: {
            "name": "BrownDesert",
            "program": "grok",
            "model": "unknown",
            "project_key": cwd,
            "agent": "grok",
            "instance": instance,
            "status": "retired",
        },
    )
    monkeypatch.setattr(server.db, "identity_by_cwd", lambda *_a, **_k: None)
    record = server._identity_record(cwd, "grok", instance)
    assert record is not None
    assert record["name"] == "BrownDesert"


def test_identity_record_uses_registry_when_agent_mail_db_is_unavailable(monkeypatch):
    instance = "i-yzh33bkopbhev3ae654tc7tila"
    cwd = "/home/fyc/github/agent-cockpit"
    monkeypatch.setattr(server.next_profile, "require_project", lambda path: path)
    monkeypatch.setattr(
        server,
        "_registry_identity_for_instance",
        lambda *_a, **_k: {
            "name": "BrownDesert",
            "program": "grok",
            "model": "unknown",
            "project_key": cwd,
            "agent": "grok",
            "instance": instance,
        },
    )
    monkeypatch.setattr(
        server.db,
        "identity_by_cwd",
        lambda *_a, **_k: (_ for _ in ()).throw(
            FileNotFoundError("Agent Mail database missing")
        ),
    )

    record = server._identity_record(cwd, "grok", instance)

    assert record is not None
    assert record["name"] == "BrownDesert"


def test_identity_for_chat_pane_skips_program_main(monkeypatch):
    from agent_cockpit import db

    monkeypatch.setattr(db.next_profile, "require_project", lambda path: path)
    monkeypatch.setattr(
        db, "_rows",
        lambda *_a, **_k: [
            {"name": "kimi-main", "program": "kimi", "model": "", "human_key": "/repo"},
            {"name": "FoggyBasin", "program": "kimi", "model": "", "human_key": "/repo"},
        ],
    )
    picked = db.identity_for_chat_pane("/repo", "kimi")
    assert picked is not None
    assert picked["name"] == "FoggyBasin"


def test_throwaway_identity_prompt_skips_pytest_tmp():
    from agent_cockpit import herdr_client

    assert herdr_client._is_throwaway_identity_prompt(
        "[agent-mail 身份告知] 花名=FuchsiaPond,"
        "项目=/tmp/pytest-of-fyc/pytest-2826/test_list_chat_mail_does_not_l0/same-proj"
    )
    assert not herdr_client._is_throwaway_identity_prompt(
        "[agent-mail 身份告知] 花名=BrownDesert,项目=/home/fyc/github/agent-cockpit"
    )
    assert not herdr_client._is_throwaway_identity_prompt("普通提示")


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


def test_message_project_signatures_cover_message_and_read_state(mail_db):
    initial = db.message_project_signatures()
    assert set(initial) == {"active"}

    mail_db.execute("UPDATE message_recipients SET read_ts = 5 WHERE message_id = 1")
    mail_db.commit()
    after_read = db.message_project_signatures()
    assert after_read["active"] != initial["active"]

    mail_db.execute(
        "INSERT INTO messages VALUES (4, 1, 't4', '', 'four', '', 'normal', 0, 4, NULL, 1)"
    )
    mail_db.execute("INSERT INTO message_recipients VALUES (4, 1, 'to', NULL, NULL)")
    mail_db.commit()
    after_message = db.message_project_signatures()
    assert after_message["active"] != after_read["active"]
