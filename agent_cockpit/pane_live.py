"""群聊「看现场」实时流：浏览器走 WebSocket，由 Herdr 事件唤醒后推快照。"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

from . import herdr_client
from .herdr_state import (
    HerdrSocket,
    HerdrSocketError,
    HerdrSocketIdleTimeout,
)

FALLBACK_WAIT_S = 0.15
EVENT_WAIT_S = 2.0
LIVE_LINES = 200
JsonSender = Callable[[dict[str, Any]], Awaitable[None]]
ClosedFn = Callable[[], bool]


def extract_pane_text(raw: dict[str, Any] | None) -> str:
    if not isinstance(raw, dict):
        return ""
    output = raw.get("output") or ""
    if isinstance(output, str) and output.strip().startswith("{"):
        try:
            parsed = json.loads(output)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            result = parsed.get("result") if isinstance(parsed.get("result"), dict) else parsed
            for key in ("text", "output", "content"):
                value = result.get(key) if isinstance(result, dict) else None
                if isinstance(value, str) and value.strip():
                    output = value
                    break
    text = str(output or "")
    if text.strip():
        return text
    error = raw.get("error")
    return str(error) if isinstance(error, str) else ""


def snapshot_from_read(raw: dict[str, Any] | None) -> dict[str, Any]:
    error = None
    if isinstance(raw, dict) and isinstance(raw.get("error"), str) and raw["error"]:
        error = raw["error"]
    return {
        "type": "snapshot",
        "output": extract_pane_text(raw),
        "error": error,
    }


def snapshot_from_envelope(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(envelope, dict):
        return None
    data = envelope.get("data")
    if not isinstance(data, dict):
        data = envelope.get("result") if isinstance(envelope.get("result"), dict) else envelope
    read = data.get("read") if isinstance(data, dict) else None
    if not isinstance(read, dict):
        return None
    text = read.get("text")
    if not isinstance(text, str) or not text:
        return None
    return {"type": "snapshot", "output": text, "error": None}


def read_snapshot(session: str, pane_id: str, lines: int = LIVE_LINES) -> dict[str, Any]:
    return snapshot_from_read(herdr_client.pane_read(session, pane_id, lines, True))


def live_subscriptions(pane_id: str) -> list[dict[str, Any]]:
    return [
        {"type": "pane.scroll_changed", "pane_id": pane_id},
        {"type": "pane.agent_status_changed", "pane_id": pane_id},
        {
            "type": "pane.output_matched",
            "pane_id": pane_id,
            "source": "recent",
            "lines": LIVE_LINES,
            "match": {"type": "regex", "value": "."},
        },
    ]


def session_socket_path(session: str) -> str | None:
    for row in herdr_client.list_sessions():
        if row.get("name") == session:
            path = str(row.get("socket") or "").strip()
            return path or None
    return None


class HerdrLiveWaiter:
    """订 Herdr 事件；订不到就短睡，调用方再读一帧。"""

    def __init__(self, session: str, pane_id: str) -> None:
        self.session = session
        self.pane_id = pane_id
        self._sock: HerdrSocket | None = None
        try:
            path = session_socket_path(session)
            if not path:
                return
            sock = HerdrSocket(path)
            sock.connect()
            sock.request(
                "events.subscribe",
                {"subscriptions": live_subscriptions(pane_id)},
                expect_type="subscription_started",
            )
            self._sock = sock
        except (HerdrSocketError, OSError, ValueError):
            self.close()

    @property
    def subscribed(self) -> bool:
        return self._sock is not None

    def wait(self, timeout: float = EVENT_WAIT_S) -> dict[str, Any] | None:
        if self._sock is None:
            time.sleep(FALLBACK_WAIT_S)
            return None
        try:
            envelope = self._sock.read_line(timeout=timeout)
        except HerdrSocketIdleTimeout:
            return None
        except HerdrSocketError:
            self.close()
            return None
        return snapshot_from_envelope(envelope)

    def close(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            sock.close()


async def pump_pane_live(
    send_json: JsonSender,
    session: str,
    pane_id: str,
    *,
    reader: Callable[[str, str], dict[str, Any]] | None = None,
    wait: Callable[[], dict[str, Any] | None] | None = None,
    closed: ClosedFn | None = None,
) -> None:
    """先推一帧，再等变化。wait 返回带正文的快照则直接推，否则再读 pane。"""
    read = reader or (lambda sess, pane: read_snapshot(sess, pane))
    last: tuple[str, str | None] | None = None

    def key_of(payload: dict[str, Any]) -> tuple[str, str | None]:
        return (str(payload.get("output") or ""), payload.get("error"))

    async def push(payload: dict[str, Any]) -> None:
        nonlocal last
        current = key_of(payload)
        if current == last:
            return
        await send_json(payload)
        last = current

    await push(read(session, pane_id))
    while closed is None or not closed():
        if wait is None:
            await asyncio.sleep(FALLBACK_WAIT_S)
            event_snap = None
        else:
            event_snap = await asyncio.to_thread(wait)
        if event_snap is not None:
            await push(event_snap)
            continue
        await push(read(session, pane_id))
