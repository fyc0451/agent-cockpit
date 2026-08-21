"""群聊「看现场」实时流：浏览器走 WebSocket，由 Herdr 事件唤醒后推快照。"""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
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
_LIST_OR_HEADING_RE = re.compile(r"^(?:[-*•●□]|\d+[、)]|\d+\.(?:\s|$)|#{1,6}\s|\|)")
_CURRENCY_ONLY_RE = re.compile(r"[¥$€]\d[\d.,]*")
_NARROW_WRAP_MAX = 56
_TUI_FOOTER_RE = re.compile(
    r"always-approve|Shift\+Tab:mode|Enter:open|Ctrl\+e:|Build anything"
    r"|command still running|Waiting fo"
)
_TUI_BOX_RE = re.compile(r"[╭╮╰╯┌┐└┘]")


def looks_like_tui_screen(text: str) -> bool:
    """Grok / Claude 整屏 TUI：有框和底栏，不能当日志拆行。"""
    if not text or not _TUI_BOX_RE.search(text):
        return False
    if _TUI_FOOTER_RE.search(text):
        return True
    return "▾ Tasks" in text or "Tasks 1" in text


def live_layout(text: str) -> str:
    return "tui" if looks_like_tui_screen(text) else "log"


def display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.east_asian_width(char) in {"W", "F"}:
            width += 2
        elif char == "\t":
            width += 8
        else:
            width += 1
    return width


def _unwrap_glue(left: str, right: str) -> str:
    if not left or not right:
        return f"{left}{right}"
    prev, nxt = left[-1], right[0]
    if prev in "-/_" and nxt.isalnum():
        return f"{left}{right}"
    if (prev.isascii() and prev.isalnum()) or (nxt.isascii() and nxt.isalnum()):
        return f"{left} {right}"
    return f"{left}{right}"


def _typical_wrap_col(widths: list[int]) -> int:
    clustered = [width for width in widths if 16 <= width <= _NARROW_WRAP_MAX]
    if len(clustered) < 3:
        return 0
    peak = max(clustered)
    near = [width for width in clustered if peak - 4 <= width <= peak]
    return peak if len(near) >= 3 else 0


def unwrap_terminal_wrap(text: str, *, short_limit: int = 20) -> str:
    """把分屏硬折行拼回句子，不碰列表、标题和空行。"""
    if not text:
        return text
    raw_lines = text.splitlines()
    wrap_col = _typical_wrap_col([
        display_width(line.strip())
        for line in raw_lines
        if line.strip() and not _LIST_OR_HEADING_RE.match(line.strip())
        and not herdr_client._is_box_table_row(line.strip())
        and not herdr_client._is_box_table_rule(line.strip())
    ])

    def is_short(stripped: str) -> bool:
        if short_limit and len(stripped) <= short_limit:
            return True
        return bool(wrap_col) and display_width(stripped) <= wrap_col

    out: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            out.append(buf)
            buf = ""

    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            flush()
            if out and out[-1] != "":
                out.append("")
            continue
        if re.match(r"^(?:error:|\(failed\)|gpt-)", stripped, re.I):
            flush()
            continue
        if _LIST_OR_HEADING_RE.match(stripped):
            flush()
            out.append(raw.rstrip())
            continue
        if herdr_client._is_box_table_row(stripped) or herdr_client._is_box_table_rule(stripped):
            flush()
            out.append(raw.rstrip())
            continue
        short = is_short(stripped)
        if short and out and re.match(r"^\s*(?:[-*•●□]|\d+[、)]|\d+\.(?:\s|$))", out[-1]):
            out[-1] = _unwrap_glue(out[-1].rstrip(), stripped)
            continue
        if buf and short:
            if _CURRENCY_ONLY_RE.fullmatch(buf):
                flush()
            else:
                buf = _unwrap_glue(buf, stripped)
                if stripped.endswith(("。", "！", "？", ".", "!", "?")):
                    flush()
                continue
        flush()
        if short:
            buf = stripped
        else:
            out.append(raw.rstrip())
    flush()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


_CODEX_TOOL_PREFIXES = (
    "Waited for",
    "Waiting for",
    "Ran ",
    "Explored",
    "Planning",
    "Edited",
    "Updated Plan",
    "Working",
    "Fixing",
    "<thinking>",
)


def extract_live_progress(text: str, agent_kind: str = "") -> str:
    """按 agent 抽一条给人看的进度。Codex 只留非工具 • 句，不进账本。"""
    if (agent_kind or "").strip().lower() != "codex":
        return ""
    last = ""
    for raw in unwrap_terminal_wrap(text).splitlines():
        stripped = raw.strip()
        if not stripped.startswith("•"):
            continue
        body = stripped[1:].lstrip()
        if not body or any(body.startswith(prefix) for prefix in _CODEX_TOOL_PREFIXES):
            continue
        last = body
    last = re.sub(r"\s+", " ", last).strip()
    if len(last) < 12:
        return ""
    if len(last) > 160:
        return last[:159] + "…"
    return last


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
        if looks_like_tui_screen(text):
            return text.rstrip("\n")
        return unwrap_terminal_wrap(text, short_limit=0)
    error = raw.get("error")
    return str(error) if isinstance(error, str) else ""


def _live_snapshot(output: str, error: str | None) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "output": output,
        "error": error,
        "layout": live_layout(output),
    }


def snapshot_from_read(raw: dict[str, Any] | None) -> dict[str, Any]:
    error = None
    if isinstance(raw, dict) and isinstance(raw.get("error"), str) and raw["error"]:
        error = raw["error"]
    return _live_snapshot(extract_pane_text(raw), error)


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
    output = text.rstrip("\n") if looks_like_tui_screen(text) else unwrap_terminal_wrap(
        text, short_limit=0,
    )
    return _live_snapshot(output, None)


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


def check_agent_mail_connectivity(session: str, pane_id: str) -> dict[str, Any]:
    """检查这个 pane 有没有花名、是不是还活着。

    不能读 Cockpit 进程的 AGENT_MAIL_NAME / HERDR_CONFIG_PATH：8790
    自己不是 agent，用它的环境会把整群标成未连接。
    """
    try:
        from . import chat_roster

        snapshot = herdr_client.snapshot()
        herdr_up = snapshot.get("available") is not False
        pane = next(
            (
                row for row in (snapshot.get("panes") or [])
                if isinstance(row, dict)
                and str(row.get("pane_id") or "") == pane_id
                and (not session or str(row.get("session") or "") == session)
            ),
            None,
        )
        has_live_agent = bool(pane and pane.get("agent"))
        roster_name = chat_roster.get_pane_mail_name(session, pane_id) if session else ""
        pane_name = str((pane or {}).get("mail_name") or "").strip()
        has_mail_name = bool(roster_name or pane_name)

        can_send_mail = False
        try:
            from agent_mail_commands import mail_send  # noqa: F401
            can_send_mail = True
        except Exception:
            pass

        connected = has_mail_name and herdr_up and has_live_agent and can_send_mail
        return {
            "connected": connected,
            "details": {
                "has_mail_name": has_mail_name,
                "has_config_path": herdr_up,
                "has_agent_session": has_live_agent,
                "can_send_mail": can_send_mail,
            },
            "pane_id": pane_id,
        }
    except Exception as e:
        return {"connected": False, "error": str(e), "pane_id": pane_id}

