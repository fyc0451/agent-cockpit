"""看现场 WebSocket 泵：先推一帧，变化才再推。"""
from __future__ import annotations

import asyncio
import threading

from fastapi.testclient import TestClient

from agent_cockpit import pane_live
from agent_cockpit import server


def test_extract_pane_text_unwraps_json_output():
    assert pane_live.extract_pane_text({
        "output": '{"result":{"text":"hello from pane"}}',
    }) == "hello from pane"
    assert pane_live.extract_pane_text({"output": "", "error": "gone"}) == "gone"


def test_snapshot_from_envelope_uses_matched_read():
    snap = pane_live.snapshot_from_envelope({
        "event": "pane.output_matched",
        "data": {
            "pane_id": "w1:p2",
            "matched_line": "four",
            "read": {"text": "two\nthree\nfour"},
        },
    })
    assert snap == {"type": "snapshot", "output": "two\nthree\nfour", "error": None}
    assert pane_live.snapshot_from_envelope({"event": "pane.scroll_changed", "data": {}}) is None


def test_pump_sends_first_frame_then_only_changes():
    frames = [
        {"type": "snapshot", "output": "one\ntwo\nthree", "error": None},
        {"type": "snapshot", "output": "one\ntwo\nthree", "error": None},
        {"type": "snapshot", "output": "two\nthree\nfour", "error": None},
    ]
    sent: list[dict] = []
    waits = {"n": 0}

    def reader(_session: str, _pane: str) -> dict:
        return frames[min(waits["n"], len(frames) - 1)]

    def wait() -> dict | None:
        waits["n"] += 1
        return None

    async def send(payload: dict) -> None:
        sent.append(payload)

    asyncio.run(pane_live.pump_pane_live(
        send, "cockpit", "w1:p2",
        reader=reader, wait=wait, closed=lambda: waits["n"] >= 2,
    ))
    assert [row["output"] for row in sent] == ["one\ntwo\nthree", "two\nthree\nfour"]


def test_pump_pushes_event_snapshot_without_reread():
    sent: list[dict] = []
    reads = {"n": 0}
    waits = {"n": 0}

    def reader(_session: str, _pane: str) -> dict:
        reads["n"] += 1
        return {"type": "snapshot", "output": "one", "error": None}

    def wait() -> dict | None:
        waits["n"] += 1
        return {"type": "snapshot", "output": "one\ntwo", "error": None}

    async def send(payload: dict) -> None:
        sent.append(payload)

    asyncio.run(pane_live.pump_pane_live(
        send, "cockpit", "w1:p2",
        reader=reader, wait=wait, closed=lambda: waits["n"] >= 1,
    ))
    assert reads["n"] == 1
    assert [row["output"] for row in sent] == ["one", "one\ntwo"]


class _GateWaiter:
    def __init__(self, *_a, **_k) -> None:
        self.gate = threading.Event()

    def wait(self):
        self.gate.wait(0.5)
        return None

    def close(self) -> None:
        self.gate.set()


def test_pane_live_websocket_pushes_snapshot(monkeypatch):
    monkeypatch.setattr(server, "_websocket_trusted", lambda _ws: True)
    monkeypatch.setattr(
        server.pane_live, "read_snapshot",
        lambda _s, _p: {"type": "snapshot", "output": "hello from live", "error": None},
    )
    monkeypatch.setattr(server.pane_live, "HerdrLiveWaiter", _GateWaiter)
    client = TestClient(server.app)
    with client.websocket_connect("/api/chat/sessions/cockpit/panes/w1:p2/live") as ws:
        assert ws.receive_json() == {
            "type": "snapshot", "output": "hello from live", "error": None,
        }
