"""本机工作区与群聊登记账本。

账本只记录关系，不拥有工作区目录，也不负责 Herdr session 的生命周期。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import runtime_paths

_lock = threading.RLock()
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WORKSPACE_FIELDS = frozenset({"id", "path", "title", "created_at", "order"})
_THREAD_FIELDS = frozenset({"id", "workspace_id", "herdr_session", "title", "created_at"})
_MESSAGE_FIELDS = frozenset({"id", "session", "kind", "sender", "text", "to", "ts"})
_MESSAGE_KINDS = frozenset({"me", "agent", "event", "error"})
_MAX_MESSAGES = 500


def _path(name: str) -> Path:
    return runtime_paths.store(name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _empty(kind: str) -> dict[str, Any]:
    return {"version": 1, kind: []}


def _validate_row(row: Any, fields: frozenset[str], *, thread: bool = False) -> dict[str, Any]:
    if not isinstance(row, dict) or set(row) != fields:
        raise ValueError("账本 JSON 字段格式无效")
    id_pattern = r"th_[0-9a-f]{12}" if thread else r"ws_[0-9a-f]{12}"
    if not isinstance(row["id"], str) or not re.fullmatch(id_pattern, row["id"]):
        raise ValueError("账本 ID 无效")
    if not isinstance(row["title"], str) or not row["title"]:
        raise ValueError("账本标题无效")
    if not isinstance(row["created_at"], str) or not row["created_at"]:
        raise ValueError("账本时间无效")
    if thread:
        workspace_id = row["workspace_id"]
        if not isinstance(workspace_id, str) or not re.fullmatch(r"ws_[0-9a-f]{12}", workspace_id):
            raise ValueError("workspace_id 无效")
        if not isinstance(row["herdr_session"], str) or not _SESSION_RE.fullmatch(row["herdr_session"]):
            raise ValueError("herdr_session 无效")
    else:
        if not isinstance(row["path"], str) or not Path(row["path"]).is_absolute():
            raise ValueError("工作区路径无效")
        if type(row["order"]) is not int or row["order"] < 0:
            raise ValueError("工作区顺序无效")
    return dict(row)


def _load(name: str, kind: str, fields: frozenset[str], *, thread: bool = False) -> dict[str, Any]:
    path = _path(name)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty(kind)
    except (OSError, UnicodeError) as exc:
        raise ValueError("账本不可读") from exc
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("账本 JSON 已损坏") from exc
    if not isinstance(data, dict) or set(data) != {"version", kind}:
        raise ValueError("账本 JSON 顶层格式无效")
    if type(data["version"]) is not int or data["version"] != 1 or not isinstance(data[kind], list):
        raise ValueError("账本 JSON 版本或集合格式无效")
    rows = [_validate_row(row, fields, thread=thread) for row in data[kind]]
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("账本包含重复 ID")
    if thread:
        sessions = [row["herdr_session"] for row in rows]
        if len(sessions) != len(set(sessions)):
            raise ValueError("账本包含重复 herdr_session")
    else:
        paths = [row["path"] for row in rows]
        if len(paths) != len(set(paths)):
            raise ValueError("账本包含重复工作区路径")
    return {"version": 1, kind: rows}


def _write(name: str, data: dict[str, Any]) -> None:
    path = runtime_paths.validate_store(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _workspace_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("工作区路径必须是绝对路径")
    raw = Path(value.strip()).expanduser()
    if not raw.is_absolute():
        raise ValueError("工作区路径必须是绝对路径")
    try:
        path = raw.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise ValueError("工作区必须是真实目录") from exc
    if not path.is_dir():
        raise ValueError("工作区必须是真实目录")
    from . import files
    reason = files._custom_root_policy(path)  # noqa: SLF001 - 复用统一安全策略
    if reason is not None:
        raise ValueError(f"工作区路径被拒绝: {reason.value}")
    return path


def _title(value: str | None, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValueError("标题无效")
    return value


def create_workspace(path: str, title: str | None = None) -> dict[str, Any]:
    canonical = _workspace_path(path)
    with _lock:
        data = _load("chat_workspaces", "workspaces", _WORKSPACE_FIELDS)
        for row in data["workspaces"]:
            if row["path"] == str(canonical):
                return dict(row)
        row = {
            "id": _new_id("ws_"),
            "path": str(canonical),
            "title": _title(title, canonical.name),
            "created_at": _now(),
            "order": max((item["order"] for item in data["workspaces"]), default=-1) + 1,
        }
        data["workspaces"].append(row)
        _write("chat_workspaces", data)
        return dict(row)


def delete_workspace(workspace_id: str) -> bool:
    with _lock:
        data = _load("chat_workspaces", "workspaces", _WORKSPACE_FIELDS)
        kept = [row for row in data["workspaces"] if row["id"] != workspace_id]
        if len(kept) == len(data["workspaces"]):
            return False
        data["workspaces"] = kept
        _write("chat_workspaces", data)
        return True


def create_thread(workspace_id: str, herdr_session: str, title: str | None = None) -> dict[str, Any]:
    if not isinstance(workspace_id, str) or not re.fullmatch(r"ws_[0-9a-f]{12}", workspace_id):
        raise ValueError("workspace_id 无效")
    if not isinstance(herdr_session, str) or not _SESSION_RE.fullmatch(herdr_session):
        raise ValueError("herdr_session 无效")
    with _lock:
        data = _load("chat_threads", "threads", _THREAD_FIELDS, thread=True)
        for row in data["threads"]:
            if row["herdr_session"] == herdr_session:
                return dict(row)
        workspaces = _load("chat_workspaces", "workspaces", _WORKSPACE_FIELDS)
        if not any(row["id"] == workspace_id for row in workspaces["workspaces"]):
            raise ValueError("workspace_id 不存在")
        row = {
            "id": _new_id("th_"),
            "workspace_id": workspace_id,
            "herdr_session": herdr_session,
            "title": _title(title, herdr_session),
            "created_at": _now(),
        }
        data["threads"].append(row)
        _write("chat_threads", data)
        return dict(row)


def delete_thread(thread_id: str) -> bool:
    with _lock:
        data = _load("chat_threads", "threads", _THREAD_FIELDS, thread=True)
        kept = [row for row in data["threads"] if row["id"] != thread_id]
        if len(kept) == len(data["threads"]):
            return False
        data["threads"] = kept
        _write("chat_threads", data)
        return True


def list_workspaces() -> list[dict[str, Any]]:
    with _lock:
        rows = [dict(row) for row in _load("chat_workspaces", "workspaces", _WORKSPACE_FIELDS)["workspaces"]]
    rows.sort(key=lambda item: (item["order"], item["created_at"], item["id"]))
    return rows


def list_threads(workspace_id: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        rows = _load("chat_threads", "threads", _THREAD_FIELDS, thread=True)["threads"]
        if workspace_id is not None:
            rows = [row for row in rows if row["workspace_id"] == workspace_id]
        out = [dict(row) for row in rows]
    out.sort(key=lambda item: (item["created_at"], item["id"]))
    return out


def get_workspace(workspace_id: str) -> dict[str, Any] | None:
    with _lock:
        for row in _load("chat_workspaces", "workspaces", _WORKSPACE_FIELDS)["workspaces"]:
            if row["id"] == workspace_id:
                return dict(row)
    return None


def get_thread(thread_id: str) -> dict[str, Any] | None:
    with _lock:
        for row in _load("chat_threads", "threads", _THREAD_FIELDS, thread=True)["threads"]:
            if row["id"] == thread_id:
                return dict(row)
    return None


def get_thread_by_session(herdr_session: str) -> dict[str, Any] | None:
    with _lock:
        for row in _load("chat_threads", "threads", _THREAD_FIELDS, thread=True)["threads"]:
            if row["herdr_session"] == herdr_session:
                return dict(row)
    return None


def _validate_message(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or set(row) != _MESSAGE_FIELDS:
        raise ValueError("群聊消息字段无效")
    if not isinstance(row["id"], str) or not re.fullmatch(r"msg_[0-9a-f]{12}", row["id"]):
        raise ValueError("群聊消息 ID 无效")
    if not isinstance(row["session"], str) or not _SESSION_RE.fullmatch(row["session"]):
        raise ValueError("群聊消息 session 无效")
    if row["kind"] not in _MESSAGE_KINDS:
        raise ValueError("群聊消息类型无效")
    if not isinstance(row["sender"], str) or not row["sender"]:
        raise ValueError("群聊消息发送者无效")
    if not isinstance(row["text"], str) or not row["text"]:
        raise ValueError("群聊消息正文无效")
    if not isinstance(row["to"], list) or any(not isinstance(item, str) or not item for item in row["to"]):
        raise ValueError("群聊消息收件人无效")
    if type(row["ts"]) is not int or row["ts"] < 0:
        raise ValueError("群聊消息时间无效")
    return {
        "id": row["id"],
        "session": row["session"],
        "kind": row["kind"],
        "sender": row["sender"],
        "text": row["text"],
        "to": list(row["to"]),
        "ts": row["ts"],
    }


def _load_messages() -> dict[str, Any]:
    path = runtime_paths.store("chat_messages")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 1, "messages": []}
    except (OSError, UnicodeError) as exc:
        raise ValueError("群聊消息账本不可读") from exc
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("群聊消息账本已损坏") from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "messages"}
        or type(data["version"]) is not int
        or data["version"] != 1
        or not isinstance(data["messages"], list)
    ):
        raise ValueError("群聊消息账本格式无效")
    rows = [_validate_message(row) for row in data["messages"]]
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("群聊消息账本包含重复 ID")
    return {"version": 1, "messages": rows}


def append_message(
    session: str,
    *,
    kind: str,
    sender: str,
    text: str,
    to: list[str] | None = None,
    ts: int | None = None,
) -> dict[str, Any]:
    if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
        raise ValueError("herdr_session 无效")
    body = text.strip() if isinstance(text, str) else ""
    if not body:
        raise ValueError("群聊消息正文无效")
    if len(body) > 16_384:
        body = body[:16_384]
    recipients = [item.strip() for item in (to or []) if isinstance(item, str) and item.strip()]
    stamp = int(ts) if isinstance(ts, int) and ts >= 0 else int(datetime.now(timezone.utc).timestamp() * 1000)
    row = {
        "id": _new_id("msg_"),
        "session": session,
        "kind": kind,
        "sender": sender.strip() or "human",
        "text": body,
        "to": recipients,
        "ts": stamp,
    }
    row = _validate_message(row)
    with _lock:
        data = _load_messages()
        data["messages"].append(row)
        if len(data["messages"]) > _MAX_MESSAGES:
            data["messages"] = data["messages"][-_MAX_MESSAGES:]
        _write("chat_messages", data)
    return dict(row)


def list_messages(session: str, limit: int = 200) -> list[dict[str, Any]]:
    if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
        raise ValueError("herdr_session 无效")
    cap = max(1, min(int(limit), 200))
    with _lock:
        rows = [
            dict(row)
            for row in _load_messages()["messages"]
            if row["session"] == session
        ]
    rows.sort(key=lambda item: (item["ts"], item["id"]))
    return rows[-cap:]


def rewrite_messages(rewriter: Any) -> dict[str, int]:
    """原子改写既有消息正文；返回 None 仅删除被明确判定为垃圾的记录。"""
    if not callable(rewriter):
        raise ValueError("消息改写器无效")
    updated = 0
    deleted = 0
    with _lock:
        data = _load_messages()
        rows: list[dict[str, Any]] = []
        for row in data["messages"]:
            replacement = rewriter(dict(row))
            if replacement is None:
                deleted += 1
                continue
            checked = _validate_message(replacement)
            if checked != row:
                updated += 1
            rows.append(checked)
        if updated or deleted:
            data["messages"] = rows
            _write("chat_messages", data)
    return {"updated": updated, "deleted": deleted}
