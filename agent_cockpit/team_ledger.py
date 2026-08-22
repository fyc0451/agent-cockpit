"""4.0 团队 topic 账本。与本机群 chat_ledger 彻底隔离。

团队消息只写入 store ``team_messages``（``team-messages.json``）。
本模块不得 import ``chat_ledger``，也不得改 ``chat-ledger.sqlite3``。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from . import runtime_paths

_lock = threading.RLock()
_STORE = "team_messages"
_TOPIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MESSAGE_FIELDS = frozenset({"id", "topic", "hub", "kind", "sender", "text", "to", "ts"})
_OPTIONAL_FIELDS = frozenset({"handed_to_leader"})
_KINDS = frozenset({"me", "agent", "event", "error"})
_MAX_MESSAGES = 500


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _new_id() -> str:
    return f"tmsg_{uuid.uuid4().hex[:12]}"


def _empty() -> dict[str, Any]:
    return {"version": 1, "messages": []}


def _validate(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("团队消息字段无效")
    keys = set(row)
    if not _MESSAGE_FIELDS <= keys or not keys <= (_MESSAGE_FIELDS | _OPTIONAL_FIELDS):
        raise ValueError("团队消息字段无效")
    if not isinstance(row["id"], str) or not re.fullmatch(r"tmsg_[0-9a-f]{12}", row["id"]):
        raise ValueError("团队消息 ID 无效")
    if not isinstance(row["topic"], str) or not _TOPIC_RE.fullmatch(row["topic"]):
        raise ValueError("团队 topic 无效")
    if not isinstance(row["hub"], str) or not row["hub"].strip():
        raise ValueError("团队 Hub 无效")
    if row["kind"] not in _KINDS:
        raise ValueError("团队消息类型无效")
    if not isinstance(row["sender"], str) or not row["sender"]:
        raise ValueError("团队消息发送者无效")
    if not isinstance(row["text"], str) or not row["text"]:
        raise ValueError("团队消息正文无效")
    if not isinstance(row["to"], list) or any(
        not isinstance(item, str) or not item for item in row["to"]
    ):
        raise ValueError("团队消息收件人无效")
    if type(row["ts"]) is not int or row["ts"] < 0:
        raise ValueError("团队消息时间无效")
    out: dict[str, Any] = {
        "id": row["id"],
        "topic": row["topic"],
        "hub": row["hub"].strip(),
        "kind": row["kind"],
        "sender": row["sender"],
        "text": row["text"],
        "to": list(row["to"]),
        "ts": row["ts"],
    }
    if "handed_to_leader" in row:
        if type(row["handed_to_leader"]) is not bool:
            raise ValueError("交给 leader 标记无效")
        out["handed_to_leader"] = row["handed_to_leader"]
    return out


def _load() -> dict[str, Any]:
    path = runtime_paths.store(_STORE)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    except (OSError, UnicodeError) as exc:
        raise ValueError("团队消息账本不可读") from exc
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("团队消息账本已损坏") from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "messages"}
        or type(data["version"]) is not int
        or data["version"] != 1
        or not isinstance(data["messages"], list)
    ):
        raise ValueError("团队消息账本格式无效")
    rows = [_validate(row) for row in data["messages"]]
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("团队消息账本包含重复 ID")
    return {"version": 1, "messages": rows}


def _write(data: dict[str, Any]) -> None:
    path = runtime_paths.validate_store(_STORE)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def list_messages(topic: str, *, hub: str) -> list[dict[str, Any]]:
    if not isinstance(topic, str) or not _TOPIC_RE.fullmatch(topic):
        raise ValueError("团队 topic 无效")
    hub_url = hub.strip() if isinstance(hub, str) else ""
    if not hub_url:
        raise ValueError("团队 Hub 无效")
    with _lock:
        rows = _load()["messages"]
    return [
        dict(row) for row in rows
        if row["topic"] == topic and row["hub"] == hub_url
    ]


def append_message(
    topic: str,
    *,
    hub: str,
    kind: str,
    sender: str,
    text: str,
    to: list[str] | None = None,
    ts: int | None = None,
    handed_to_leader: bool | None = None,
) -> dict[str, Any]:
    if not isinstance(topic, str) or not _TOPIC_RE.fullmatch(topic):
        raise ValueError("团队 topic 无效")
    hub_url = hub.strip() if isinstance(hub, str) else ""
    if not hub_url:
        raise ValueError("团队 Hub 无效")
    body = text.strip() if isinstance(text, str) else ""
    if not body:
        raise ValueError("团队消息正文无效")
    if len(body) > 16_384:
        body = body[:16_384]
    recipients = [
        item.strip() for item in (to or [])
        if isinstance(item, str) and item.strip()
    ]
    stamp = ts if isinstance(ts, int) and ts >= 0 else _now_ms()
    row: dict[str, Any] = {
        "id": _new_id(),
        "topic": topic,
        "hub": hub_url,
        "kind": kind,
        "sender": sender.strip() or "human",
        "text": body,
        "to": recipients,
        "ts": stamp,
    }
    if handed_to_leader is not None:
        row["handed_to_leader"] = handed_to_leader
    row = _validate(row)
    with _lock:
        data = _load()
        data["messages"].append(row)
        if len(data["messages"]) > _MAX_MESSAGES:
            data["messages"] = data["messages"][-_MAX_MESSAGES:]
        _write(data)
    return dict(row)


def mark_handed_to_leader(message_id: str) -> dict[str, Any] | None:
    """只在团队账本打标。不写本机群，不 pane_send。"""
    if not isinstance(message_id, str) or not re.fullmatch(r"tmsg_[0-9a-f]{12}", message_id):
        raise ValueError("团队消息 ID 无效")
    with _lock:
        data = _load()
        for index, row in enumerate(data["messages"]):
            if row["id"] != message_id:
                continue
            updated = dict(row)
            updated["handed_to_leader"] = True
            data["messages"][index] = _validate(updated)
            _write(data)
            return dict(data["messages"][index])
    return None


def store_path():
    return runtime_paths.store(_STORE)
