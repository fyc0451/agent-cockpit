"""4.0 团队账本与本机群彻底隔离。"""
from __future__ import annotations

import json

import pytest

from agent_cockpit import chat_ledger
from agent_cockpit import runtime_paths
from agent_cockpit import team_ledger


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
    yield {"data": data}
    runtime_paths.reset_cache()


def test_append_stays_off_chat_messages(isolated_ledger):
    chat = runtime_paths.store("chat_messages")
    team = runtime_paths.store("team_messages")
    assert chat != team
    assert team.name == "team-messages.json"
    row = team_ledger.append_message(
        "agent-cockpit.general",
        hub="https://hub.example",
        kind="me",
        sender="human",
        text="hello team",
        to=["BrownDesert"],
    )
    assert row["id"].startswith("tmsg_")
    assert not chat.exists()
    saved = json.loads(team.read_text(encoding="utf-8"))
    assert saved["version"] == 1
    assert saved["messages"][0]["text"] == "hello team"
    assert chat_ledger.list_messages("cockpit") == []


def test_hand_to_leader_does_not_copy_local(isolated_ledger):
    row = team_ledger.append_message(
        "agent-cockpit.general",
        hub="https://hub.example",
        kind="agent",
        sender="peer-lead",
        text="请看这条",
        to=["BrownDesert"],
    )
    marked = team_ledger.mark_handed_to_leader(row["id"])
    assert marked is not None
    assert marked["handed_to_leader"] is True
    assert not runtime_paths.store("chat_messages").exists()
    assert chat_ledger.list_messages("cockpit") == []


def test_team_ledger_does_not_import_chat_ledger():
    assert "agent_cockpit.chat_ledger" not in getattr(team_ledger, "__dict__", {})
    assert "chat_ledger" not in team_ledger.__dict__
    import ast
    from pathlib import Path

    tree = ast.parse(Path(team_ledger.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(alias.name for alias in node.names)
    assert "chat_ledger" not in imported
    assert "agent_cockpit.chat_ledger" not in imported
