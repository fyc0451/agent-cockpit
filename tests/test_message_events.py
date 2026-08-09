import asyncio
import json

import server


def _reset_message_state(monkeypatch, **overrides):
    state = {
        "revision": 0, "source_version": None, "signatures": None, "changes": [],
    }
    state.update(overrides)
    monkeypatch.setattr(server, "_message_state", state)


def test_message_revision_baselines_then_reports_only_changed_projects(monkeypatch):
    _reset_message_state(monkeypatch)
    versions = iter([1, 1, 2, 3])
    signatures = iter([
        {"one": (1,), "two": (2,)},
        {"one": (1,), "two": (3,)},
        {"one": (1,), "three": (1,)},
    ])
    monkeypatch.setattr(server.db, "data_version", lambda: next(versions))
    monkeypatch.setattr(server.db, "message_project_signatures", lambda: next(signatures))
    monkeypatch.setattr(server.db, "project_slugs_by_human_key", lambda: {})
    monkeypatch.setattr(server.coordination, "message_state_revision", lambda: ((0, 0), (0, 0)))
    monkeypatch.setattr(server.coordination, "message_project_signatures", lambda: {})

    server._refresh_message_state()
    assert server._message_state["revision"] == 0
    server._refresh_message_state()
    assert server._message_state["revision"] == 0
    server._refresh_message_state()
    assert server._message_state["revision"] == 1
    assert server._message_state["changes"][-1]["projects"] == ["two"]
    server._refresh_message_state()
    assert server._message_state["revision"] == 2
    assert server._message_state["changes"][-1]["projects"] == ["three", "two"]


def test_message_revision_ignores_irrelevant_database_writes(monkeypatch):
    _reset_message_state(
        monkeypatch, source_version=(1, ((0, 0), (0, 0))), signatures={"one": (1,)}, revision=4,
    )
    monkeypatch.setattr(server.db, "data_version", lambda: 2)
    monkeypatch.setattr(server.db, "message_project_signatures", lambda: {"one": (1,)})
    monkeypatch.setattr(server.db, "project_slugs_by_human_key", lambda: {})
    monkeypatch.setattr(server.coordination, "message_state_revision", lambda: ((0, 0), (0, 0)))
    monkeypatch.setattr(server.coordination, "message_project_signatures", lambda: {})
    server._refresh_message_state()
    assert server._message_state["revision"] == 4
    assert server._message_state["changes"] == []


def test_events_emit_explicit_message_payload_without_content(monkeypatch):
    _reset_message_state(
        monkeypatch, revision=7, source_version=(2, ((0, 0), (0, 0))),
        signatures={"demo": (1,)}, changes=[{"revision": 7, "projects": ["demo"]}],
    )
    monkeypatch.setattr(
        server, "_live_state",
        {"revision": 0, "unread": None, "snapshot": None, "attention": None},
    )

    class Request:
        async def is_disconnected(self):
            return False

    async def read_event():
        response = await server.api_events(Request())
        event = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return event

    event = asyncio.run(read_event())
    assert event["event"] == "messages"
    assert json.loads(event["data"]) == {"revision": 7, "projects": ["demo"]}
    assert "body" not in event["data"]


def test_events_reconnect_lists_all_projects_after_existing_revision(monkeypatch):
    _reset_message_state(
        monkeypatch, revision=8, source_version=(3, ((0, 0), (0, 0))),
        signatures={"demo": (1,), "other": (2,)},
        changes=[{"revision": 8, "projects": ["other"]}],
    )
    monkeypatch.setattr(
        server, "_live_state",
        {"revision": 0, "unread": None, "snapshot": None, "attention": None},
    )

    class Request:
        async def is_disconnected(self):
            return False

    async def read_event():
        response = await server.api_events(Request())
        event = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return event

    event = asyncio.run(read_event())
    assert json.loads(event["data"])["projects"] == ["demo", "other"]


def test_events_first_baseline_lists_all_projects_at_revision_zero(monkeypatch):
    _reset_message_state(
        monkeypatch, revision=0, source_version=(1, ((0, 0), (0, 0))),
        signatures={"demo": (1,), "other": (2,)}, changes=[],
    )
    monkeypatch.setattr(
        server, "_live_state",
        {"revision": 0, "unread": None, "snapshot": None, "attention": None},
    )

    class Request:
        async def is_disconnected(self):
            return False

    async def read_event():
        response = await server.api_events(Request())
        event = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return event

    event = asyncio.run(read_event())
    assert json.loads(event["data"]) == {
        "revision": 0, "projects": ["demo", "other"],
    }


def test_events_wait_for_baseline_before_emitting_revision_zero(monkeypatch):
    _reset_message_state(monkeypatch)
    monkeypatch.setattr(
        server, "_live_state",
        {"revision": 0, "unread": None, "snapshot": None, "attention": None},
    )

    class Request:
        calls = 0

        async def is_disconnected(self):
            self.calls += 1
            if self.calls == 2:
                server._message_state["source_version"] = (1, ((0, 0), (0, 0)))
                server._message_state["signatures"] = {"demo": (1,)}
            return False

    async def read_event():
        response = await server.api_events(Request())
        event = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return event

    event = asyncio.run(read_event())
    assert json.loads(event["data"]) == {"revision": 0, "projects": ["demo"]}


def test_coordination_receipt_change_updates_affected_project(monkeypatch):
    key = "/project"
    _reset_message_state(
        monkeypatch,
        source_version=(1, ((1, 1), (0, 0))),
        signatures={"demo": ((1,), (("agent", 1, "pending"),))},
        revision=2,
    )
    monkeypatch.setattr(server.db, "data_version", lambda: 1)
    monkeypatch.setattr(server.db, "message_project_signatures", lambda: {"demo": (1,)})
    monkeypatch.setattr(server.db, "project_slugs_by_human_key", lambda: {key: "demo"})
    monkeypatch.setattr(server.coordination, "message_state_revision", lambda: ((2, 1), (0, 0)))
    monkeypatch.setattr(
        server.coordination, "message_project_signatures",
        lambda: {key: (("agent", 1, "processed"),)},
    )
    server._refresh_message_state()
    assert server._message_state["revision"] == 3
    assert server._message_state["changes"][-1]["projects"] == ["demo"]


def test_events_union_projects_across_unseen_revisions(monkeypatch):
    _reset_message_state(
        monkeypatch, revision=3, source_version=(3, ((0, 0), (0, 0))),
        signatures={"one": (1,), "two": (2,)},
        changes=[
            {"revision": 2, "projects": ["one"]},
            {"revision": 3, "projects": ["two"]},
        ],
    )
    monkeypatch.setattr(
        server, "_live_state",
        {"revision": 0, "unread": None, "snapshot": None, "attention": None},
    )

    class Request:
        calls = 0
        async def is_disconnected(self):
            self.calls += 1
            if self.calls == 1:
                server._message_state["revision"] = 1
                return False
            server._message_state["revision"] = 3
            return False

    async def read_events():
        response = await server.api_events(Request())
        first = await anext(response.body_iterator)
        second = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return first, second

    _, event = asyncio.run(read_events())
    assert json.loads(event["data"])["projects"] == ["one", "two"]
