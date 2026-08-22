"""Cockpit 3.0 工作区 / 群聊账本：只改登记，不删盘。"""
from __future__ import annotations

import importlib
import json
import sqlite3
import stat
from pathlib import Path

import pytest

from agent_cockpit import chat_ledger
from agent_cockpit import runtime_paths


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    data = tmp_path / "data"
    config = tmp_path / "config"
    state = tmp_path / "state"
    uploads = tmp_path / "uploads"
    for path in (data, config, state, uploads):
        path.mkdir()
    monkeypatch.setenv("COCKPIT_DATA_DIR", str(data))
    monkeypatch.setenv("COCKPIT_CONFIG_DIR", str(config))
    monkeypatch.setenv("COCKPIT_STATE_DIR", str(state))
    monkeypatch.setenv("COCKPIT_UPLOADS_DIR", str(uploads))
    monkeypatch.delenv("COCKPIT_COORDINATION_DB", raising=False)
    runtime_paths.reset_cache()
    yield {"data": data, "proj": tmp_path / "proj"}
    runtime_paths.reset_cache()


def _project(isolated_ledger) -> Path:
    proj = isolated_ledger["proj"]
    proj.mkdir()
    (proj / "README.md").write_text("keep", encoding="utf-8")
    return proj


def test_list_empty_when_files_missing(isolated_ledger):
    assert chat_ledger.list_workspaces() == []
    assert chat_ledger.list_threads() == []
    assert chat_ledger.list_messages("demo-1") == []


def test_append_and_list_messages_by_session(isolated_ledger):
    first = chat_ledger.append_message(
        "demo-1", kind="me", sender="human", text="hello", to=["kimi"],
    )
    chat_ledger.append_message(
        "other-1", kind="me", sender="human", text="nope", to=["kimi"],
    )
    second = chat_ledger.append_message(
        "demo-1", kind="agent", sender="kimi", text="ok", to=["human"],
    )
    rows = chat_ledger.list_messages("demo-1")
    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    assert rows[0]["text"] == "hello"
    assert rows[0].get("delivery") is None
    assert rows[1]["kind"] == "agent"


def test_messages_survive_module_reload(isolated_ledger):
    saved = chat_ledger.append_message(
        "restart-1", kind="agent", sender="TopazOwl", text="持久结论",
        to=["human"], ts=1000,
    )
    reloaded = importlib.reload(chat_ledger)
    assert reloaded.list_messages("restart-1") == [saved]


def test_more_than_500_messages_are_retained(isolated_ledger):
    for index in range(550):
        chat_ledger.append_message(
            "long-1", kind="agent", sender="worker", text=f"message-{index}",
            to=["human"], ts=index,
        )
    database = isolated_ledger["data"] / "chat-ledger.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE session = 'long-1'"
        ).fetchone()[0] == 550
    visible = chat_ledger.list_messages("long-1", 200)
    assert len(visible) == 200
    assert visible[0]["text"] == "message-350"
    assert visible[-1]["text"] == "message-549"


def test_append_queue_message_and_mark_notified(isolated_ledger):
    row = chat_ledger.append_message(
        "demo-1", kind="me", sender="human", text="等你忙完再看",
        to=["BrownDesert"], delivery="queue",
    )
    assert row["delivery"] == "queue"
    assert "notified_to" not in row
    marked = chat_ledger.mark_message_notified(row["id"], ["BrownDesert"])
    assert marked is not None
    assert marked["notified_to"] == ["BrownDesert"]
    again = chat_ledger.mark_message_notified(row["id"], ["BrownDesert", "GrayFalcon"])
    assert again is not None
    assert again["notified_to"] == ["BrownDesert", "GrayFalcon"]
    listed = chat_ledger.list_messages("demo-1")
    assert listed[0]["delivery"] == "queue"
    assert listed[0]["notified_to"] == ["BrownDesert", "GrayFalcon"]
    with pytest.raises(ValueError, match="投递类型"):
        chat_ledger.append_message(
            "demo-1", kind="me", sender="human", text="坏类型",
            to=["BrownDesert"], delivery="urgent",
        )


def test_mark_messages_read_and_set_duration(isolated_ledger):
    row = chat_ledger.append_message(
        "demo-1", kind="me", sender="human", text="看这条",
        to=["BrownDesert"], delivery="interrupt",
    )
    chat_ledger.mark_message_notified(row["id"], ["BrownDesert"])
    changed = chat_ledger.mark_messages_read("demo-1", "BrownDesert")
    assert [item["id"] for item in changed] == [row["id"]]
    listed = chat_ledger.list_messages("demo-1")[0]
    assert listed["read_by"] == ["BrownDesert"]
    again = chat_ledger.mark_messages_read("demo-1", "BrownDesert")
    assert again == []
    reply = chat_ledger.append_message(
        "demo-1", kind="agent", sender="BrownDesert", text="收到", to=["human"],
    )
    updated = chat_ledger.set_message_duration(reply["id"], 12500)
    assert updated is not None
    assert updated["duration_ms"] == 12500
    assert chat_ledger.list_messages("demo-1")[1]["duration_ms"] == 12500
    with pytest.raises(ValueError, match="耗时"):
        chat_ledger.set_message_duration(reply["id"], -1)


def test_replace_message_text_updates_same_id(isolated_ledger):
    row = chat_ledger.append_message(
        "demo-1", kind="agent", sender="BrownDesert", text="草稿", to=["human"],
    )
    updated = chat_ledger.replace_message_text(row["id"], "完整结论")
    assert updated is not None
    assert updated["id"] == row["id"]
    assert updated["text"] == "完整结论"
    assert [item["text"] for item in chat_ledger.list_messages("demo-1")] == ["完整结论"]


def test_create_workspace_idempotent_by_path(isolated_ledger):
    proj = _project(isolated_ledger)
    first = chat_ledger.create_workspace(str(proj), "One")
    second = chat_ledger.create_workspace(str(proj), "Two")
    assert first["id"] == second["id"]
    assert first["path"] == str(proj.resolve())
    assert first["title"] == "One"
    assert len(chat_ledger.list_workspaces()) == 1


def test_reject_broad_and_sensitive_roots(isolated_ledger, tmp_path):
    with pytest.raises(ValueError, match="broad_root"):
        chat_ledger.create_workspace("/")
    with pytest.raises(ValueError, match="broad_root"):
        chat_ledger.create_workspace(str(Path.home()))
    ssh = Path.home() / ".ssh"
    if ssh.is_dir():
        with pytest.raises(ValueError, match="sensitive_root"):
            chat_ledger.create_workspace(str(ssh))
    missing = tmp_path / "no-such-dir"
    with pytest.raises(ValueError, match="真实目录"):
        chat_ledger.create_workspace(str(missing))


def test_delete_workspace_does_not_touch_disk(isolated_ledger):
    proj = _project(isolated_ledger)
    marker = proj / "README.md"
    before = marker.read_text(encoding="utf-8")
    names_before = sorted(p.name for p in proj.iterdir())
    row = chat_ledger.create_workspace(str(proj))
    assert chat_ledger.delete_workspace(row["id"]) is True
    assert chat_ledger.delete_workspace(row["id"]) is False
    assert proj.is_dir()
    assert marker.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in proj.iterdir()) == names_before
    assert chat_ledger.list_workspaces() == []


def test_delete_workspace_keeps_threads(isolated_ledger):
    proj = _project(isolated_ledger)
    ws = chat_ledger.create_workspace(str(proj))
    thread = chat_ledger.create_thread(ws["id"], "foo-1", "first")
    assert chat_ledger.delete_workspace(ws["id"]) is True
    assert chat_ledger.get_thread(thread["id"]) == thread
    assert chat_ledger.list_threads() == [thread]
    assert chat_ledger.list_threads(ws["id"]) == [thread]


def test_get_thread_by_session(isolated_ledger):
    proj = _project(isolated_ledger)
    ws = chat_ledger.create_workspace(str(proj))
    thread = chat_ledger.create_thread(ws["id"], "foo-1")
    assert chat_ledger.get_thread_by_session("foo-1") == thread
    assert chat_ledger.get_thread_by_session("missing") is None


def test_create_thread_idempotent_and_validates(isolated_ledger):
    proj = _project(isolated_ledger)
    ws = chat_ledger.create_workspace(str(proj))
    first = chat_ledger.create_thread(ws["id"], "foo-1")
    second = chat_ledger.create_thread(ws["id"], "foo-1", "other")
    assert first["id"] == second["id"]
    assert first["title"] == "foo-1"
    assert len(chat_ledger.list_threads()) == 1
    with pytest.raises(ValueError, match="herdr_session 无效"):
        chat_ledger.create_thread(ws["id"], "bad name")
    with pytest.raises(ValueError, match="workspace_id 无效"):
        chat_ledger.create_thread("ws_missing", "foo-2")
    with pytest.raises(ValueError, match="workspace_id 不存在"):
        chat_ledger.create_thread(f"ws_{'0' * 12}", "foo-2")


def test_expanduser_path(isolated_ledger, tmp_path, monkeypatch):
    home = tmp_path / "alt-home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    row = chat_ledger.create_workspace("~/proj")
    assert row["path"] == str(proj.resolve())


def test_corrupt_json_fail_closed(isolated_ledger):
    path = isolated_ledger["data"] / "chat-workspaces.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        chat_ledger.list_workspaces()


def test_sqlite_is_sole_authority_for_new_writes(isolated_ledger):
    proj = _project(isolated_ledger)
    ws = chat_ledger.create_workspace(str(proj))
    chat_ledger.create_thread(ws["id"], "sess-1")
    chat_ledger.append_message(
        "sess-1", kind="me", sender="human", text="hello", to=["agent"],
    )
    database = isolated_ledger["data"] / "chat-ledger.sqlite3"
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT id FROM workspaces").fetchone()[0] == ws["id"]
        assert connection.execute("SELECT herdr_session FROM threads").fetchone()[0] == "sess-1"
        assert connection.execute("SELECT text FROM messages").fetchone()[0] == "hello"
    assert not (isolated_ledger["data"] / "chat-workspaces.json").exists()
    assert not (isolated_ledger["data"] / "chat-threads.json").exists()
    assert not (isolated_ledger["data"] / "chat-messages.json").exists()


def _write_legacy_ledger(isolated_ledger) -> tuple[dict, dict, dict]:
    proj = _project(isolated_ledger)
    workspace = {
        "id": "ws_111111111111",
        "path": str(proj.resolve()),
        "title": "legacy",
        "created_at": "2026-08-22T00:00:00+00:00",
        "order": 0,
    }
    thread = {
        "id": "th_222222222222",
        "workspace_id": workspace["id"],
        "herdr_session": "legacy-1",
        "title": "legacy-1",
        "created_at": "2026-08-22T00:00:01+00:00",
    }
    message = {
        "id": "msg_333333333333",
        "session": "legacy-1",
        "kind": "agent",
        "sender": "TopazOwl",
        "text": "legacy message",
        "to": ["human"],
        "ts": 1000,
        "delivery": "queue",
        "notified_to": ["human"],
        "read_by": ["human"],
        "duration_ms": 25,
        "git": {"files": 2, "stat": "2 files changed"},
        "source": "agent-mail",
        "direct": False,
    }
    stores = (
        ("chat-workspaces.json", {"version": 1, "workspaces": [workspace]}),
        ("chat-threads.json", {"version": 1, "threads": [thread]}),
        ("chat-messages.json", {"version": 1, "messages": [message]}),
    )
    for name, body in stores:
        (isolated_ledger["data"] / name).write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8",
        )
    return workspace, thread, message


def test_legacy_json_migration_is_idempotent(isolated_ledger):
    workspace, thread, message = _write_legacy_ledger(isolated_ledger)
    assert chat_ledger.list_workspaces() == [workspace]
    assert chat_ledger.list_threads() == [thread]
    assert chat_ledger.list_messages("legacy-1") == [message]

    legacy_messages = isolated_ledger["data"] / "chat-messages.json"
    changed = json.loads(legacy_messages.read_text(encoding="utf-8"))
    changed["messages"].append({
        **message, "id": "msg_444444444444", "text": "must not reimport", "ts": 2000,
    })
    legacy_messages.write_text(json.dumps(changed), encoding="utf-8")

    reloaded = importlib.reload(chat_ledger)
    assert reloaded.list_messages("legacy-1") == [message]
    with sqlite3.connect(isolated_ledger["data"] / "chat-ledger.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'legacy_migration'"
        ).fetchone()[0] == "complete"


def test_migration_failure_rolls_back_and_can_retry(isolated_ledger, monkeypatch):
    workspace, thread, message = _write_legacy_ledger(isolated_ledger)
    legacy_paths = [
        isolated_ledger["data"] / "chat-workspaces.json",
        isolated_ledger["data"] / "chat-threads.json",
        isolated_ledger["data"] / "chat-messages.json",
    ]
    before = {path: path.read_bytes() for path in legacy_paths}
    real_migrate = chat_ledger._migrate_snapshot

    def fail_after_first_insert(connection, snapshot):
        connection.execute(
            "INSERT INTO workspaces (id, path, title, created_at, sort_order) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                workspace["id"], workspace["path"], workspace["title"],
                workspace["created_at"], workspace["order"],
            ),
        )
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(chat_ledger, "_migrate_snapshot", fail_after_first_insert)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        chat_ledger.list_workspaces()

    database = isolated_ledger["data"] / "chat-ledger.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall() == []
    assert {path: path.read_bytes() for path in legacy_paths} == before

    monkeypatch.setattr(chat_ledger, "_migrate_snapshot", real_migrate)
    assert chat_ledger.list_workspaces() == [workspace]
    assert chat_ledger.list_threads() == [thread]
    assert chat_ledger.list_messages("legacy-1") == [message]
