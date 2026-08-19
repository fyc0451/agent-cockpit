"""Cockpit 3.0 工作区 / 群聊账本：只改登记，不删盘。"""
from __future__ import annotations

import json
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


def test_store_files_are_versioned_json(isolated_ledger):
    proj = _project(isolated_ledger)
    ws = chat_ledger.create_workspace(str(proj))
    chat_ledger.create_thread(ws["id"], "sess-1")
    workspaces = json.loads(
        (isolated_ledger["data"] / "chat-workspaces.json").read_text(encoding="utf-8")
    )
    threads = json.loads(
        (isolated_ledger["data"] / "chat-threads.json").read_text(encoding="utf-8")
    )
    assert workspaces["version"] == 1
    assert threads["version"] == 1
    assert workspaces["workspaces"][0]["id"] == ws["id"]
    assert threads["threads"][0]["herdr_session"] == "sess-1"
