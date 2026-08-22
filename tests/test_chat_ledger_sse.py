"""测试账本 SSE 初始快照与路由校验。"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from starlette.testclient import TestClient

from agent_cockpit import chat_ledger
from agent_cockpit import runtime_paths
import agent_cockpit.server as server


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("COCKPIT_DATA_DIR", str(data))
    monkeypatch.delenv("COCKPIT_COORDINATION_DB", raising=False)
    runtime_paths.reset_cache()
    yield
    runtime_paths.reset_cache()


@pytest.fixture
def client(monkeypatch):
    """测试客户端。"""
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "test-token")
    return TestClient(server.app)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-token"}


@pytest.fixture
def session_name():
    return "test-sse-stream"


def test_sse_snapshot_initial(session_name):
    """SSE 初始 snapshot 事件包含全部消息。"""
    # 准备账本：添加 2 条消息
    ts = int(time.time() * 1000)
    chat_ledger.append_message(
        session_name, kind="me", sender="alice", text="hello", ts=ts,
        source="composer", direct=True,
    )
    chat_ledger.append_message(
        session_name, kind="agent", sender="bob", text="hi there", ts=ts + 1000,
    )

    class Request:
        async def is_disconnected(self):
            return True

    async def first_event():
        response = await server.api_chat_session_mail_stream(session_name, Request())
        return await anext(response.body_iterator)

    event = asyncio.run(first_event())
    assert event["event"] == "snapshot"
    event_data = json.loads(event["data"])
    messages = event_data["messages"]
    assert len(messages) == 2
    alice = next(m for m in messages if m["sender"] == "alice")
    assert alice["text"] == "hello"
    assert alice["source"] == "composer"
    assert alice["direct"] is True
    assert any(m["sender"] == "bob" and m["text"] == "hi there" for m in messages)


def test_sse_invalid_session(client, auth_headers):
    """无效会话名由路由校验返回 400。"""
    response = client.get("/api/chat/sessions/invalid@name/mail/stream", headers=auth_headers)
    assert response.status_code == 400
    assert response.json()["detail"] == "session 名仅允许字母、数字、下划线和连字符"
