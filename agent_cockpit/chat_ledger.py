"""本机工作区与群聊登记账本。

账本只记录关系，不拥有工作区目录，也不负责 Herdr session 的生命周期。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import runtime_paths

_lock = threading.RLock()
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WORKSPACE_FIELDS = frozenset({"id", "path", "title", "created_at", "order"})
_THREAD_FIELDS = frozenset({"id", "workspace_id", "herdr_session", "title", "created_at"})
_MESSAGE_FIELDS = frozenset({"id", "session", "kind", "sender", "text", "to", "ts"})
_OPTIONAL_MESSAGE_FIELDS = frozenset({
    "delivery", "notified_to", "read_by", "duration_ms", "git", "source", "direct",
})
_MESSAGE_KINDS = frozenset({"me", "agent", "event", "error"})
_MESSAGE_DELIVERIES = frozenset({"interrupt", "queue"})
_LEGACY_FILES = {
    "workspaces": "chat-workspaces.json",
    "threads": "chat-threads.json",
    "messages": "chat-messages.json",
}
_SCHEMA_VERSION = 1
_SCHEMA_STATEMENTS = (
    """CREATE TABLE metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE workspaces (
        id TEXT PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        sort_order INTEGER NOT NULL CHECK (sort_order >= 0)
    )""",
    """CREATE TABLE threads (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        herdr_session TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        session TEXT NOT NULL,
        kind TEXT NOT NULL,
        sender TEXT NOT NULL,
        text TEXT NOT NULL,
        recipients TEXT NOT NULL,
        ts INTEGER NOT NULL CHECK (ts >= 0),
        delivery TEXT,
        notified_to TEXT,
        read_by TEXT,
        duration_ms INTEGER,
        git TEXT,
        source TEXT,
        direct INTEGER
    )""",
    "CREATE INDEX workspaces_order_idx ON workspaces(sort_order, created_at, id)",
    "CREATE INDEX threads_workspace_idx ON threads(workspace_id, created_at, id)",
    "CREATE INDEX messages_session_idx ON messages(session, ts, id)",
)
_EXPECTED_COLUMNS = {
    "metadata": ("key", "value"),
    "workspaces": ("id", "path", "title", "created_at", "sort_order"),
    "threads": ("id", "workspace_id", "herdr_session", "title", "created_at"),
    "messages": (
        "id", "session", "kind", "sender", "text", "recipients", "ts",
        "delivery", "notified_to", "read_by", "duration_ms", "git", "source",
        "direct",
    ),
}


class ChatLedgerError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _legacy_path(kind: str) -> Path:
    path = runtime_paths.data_root() / _LEGACY_FILES[kind]
    if Path(os.path.realpath(path)) != path:
        raise ValueError("旧群聊账本路径不安全")
    return path


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


def _load_legacy(
    kind: str, fields: frozenset[str], *, thread: bool = False,
) -> dict[str, Any]:
    path = _legacy_path(kind)
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


def _secure_database_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ChatLedgerError("store_create_failed") from exc
    try:
        info = path.stat()
    except OSError as exc:
        raise ChatLedgerError("store_unreadable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or (info.st_uid != os.getuid() and os.getuid() != 0)
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ChatLedgerError("store_unsafe")


def _open_connection(
    path: Path, *, readonly: bool = False, wal: bool = False,
) -> sqlite3.Connection:
    try:
        if readonly:
            uri = f"file:{quote(str(path), safe='/')}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        else:
            connection = sqlite3.connect(path, timeout=5.0)
            if wal:
                connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection
    except sqlite3.Error as exc:
        raise ChatLedgerError("store_unreadable") from exc


def _validate_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version < _SCHEMA_VERSION:
        raise ChatLedgerError("migration_required")
    if version > _SCHEMA_VERSION:
        raise ChatLedgerError("future_schema")
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables != set(_EXPECTED_COLUMNS):
        raise ChatLedgerError("schema_fingerprint_mismatch")
    for table, expected in _EXPECTED_COLUMNS.items():
        columns = tuple(
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        )
        if columns != expected:
            raise ChatLedgerError("schema_fingerprint_mismatch")


def _create_schema(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def _load_legacy_messages() -> dict[str, Any]:
    path = _legacy_path("messages")
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


def _legacy_snapshot() -> dict[str, list[dict[str, Any]]]:
    return {
        "workspaces": _load_legacy(
            "workspaces", _WORKSPACE_FIELDS,
        )["workspaces"],
        "threads": _load_legacy(
            "threads", _THREAD_FIELDS, thread=True,
        )["threads"],
        "messages": _load_legacy_messages()["messages"],
    }


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _insert_message(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    connection.execute(
        """INSERT INTO messages (
            id, session, kind, sender, text, recipients, ts, delivery,
            notified_to, read_by, duration_ms, git, source, direct
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["id"], row["session"], row["kind"], row["sender"], row["text"],
            _json_value(row["to"]), row["ts"], row.get("delivery"),
            _json_value(row["notified_to"]) if "notified_to" in row else None,
            _json_value(row["read_by"]) if "read_by" in row else None,
            row.get("duration_ms"),
            _json_value(row["git"]) if "git" in row else None,
            row.get("source"),
            (1 if row["direct"] else 0) if "direct" in row else None,
        ),
    )


def _migrate_snapshot(
    connection: sqlite3.Connection, snapshot: dict[str, list[dict[str, Any]]],
) -> None:
    for row in snapshot["workspaces"]:
        connection.execute(
            "INSERT INTO workspaces (id, path, title, created_at, sort_order) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["id"], row["path"], row["title"], row["created_at"], row["order"]),
        )
    for row in snapshot["threads"]:
        connection.execute(
            "INSERT INTO threads (id, workspace_id, herdr_session, title, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                row["id"], row["workspace_id"], row["herdr_session"],
                row["title"], row["created_at"],
            ),
        )
    for row in snapshot["messages"]:
        _insert_message(connection, row)
    connection.execute(
        "INSERT INTO metadata (key, value) VALUES ('legacy_migration', 'complete')"
    )
    connection.execute(
        "INSERT INTO metadata (key, value) VALUES ('legacy_migrated_at', ?)",
        (_now(),),
    )


def _ensure_database(path: Path | None = None) -> Path:
    database = path if path is not None else runtime_paths.validate_store("chat_ledger")
    _secure_database_path(database)
    connection = _open_connection(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            snapshot = _legacy_snapshot() if path is None else {
                "workspaces": [], "threads": [], "messages": [],
            }
            _create_schema(connection)
            _migrate_snapshot(connection, snapshot)
        _validate_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return database


def initialize(path: Path) -> sqlite3.Connection:
    database = _ensure_database(Path(path))
    return open_existing(database)


def open_existing(path: Path) -> sqlite3.Connection:
    connection = _open_connection(Path(path), readonly=True)
    try:
        _validate_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def _database():
    path = _ensure_database()
    connection = _open_connection(path, wal=True)
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


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
        with _database() as connection:
            existing = connection.execute(
                "SELECT id, path, title, created_at, sort_order FROM workspaces "
                "WHERE path = ?",
                (str(canonical),),
            ).fetchone()
            if existing is not None:
                return _workspace_from_record(existing)
            next_order = int(connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM workspaces"
            ).fetchone()[0])
            row = {
                "id": _new_id("ws_"),
                "path": str(canonical),
                "title": _title(title, canonical.name),
                "created_at": _now(),
                "order": next_order,
            }
            connection.execute(
                "INSERT INTO workspaces (id, path, title, created_at, sort_order) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    row["id"], row["path"], row["title"], row["created_at"],
                    row["order"],
                ),
            )
            return dict(row)


def delete_workspace(workspace_id: str) -> bool:
    with _lock:
        with _database() as connection:
            cursor = connection.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
            return cursor.rowcount > 0


def create_thread(workspace_id: str, herdr_session: str, title: str | None = None) -> dict[str, Any]:
    if not isinstance(workspace_id, str) or not re.fullmatch(r"ws_[0-9a-f]{12}", workspace_id):
        raise ValueError("workspace_id 无效")
    if not isinstance(herdr_session, str) or not _SESSION_RE.fullmatch(herdr_session):
        raise ValueError("herdr_session 无效")
    with _lock:
        with _database() as connection:
            existing = connection.execute(
                "SELECT id, workspace_id, herdr_session, title, created_at "
                "FROM threads WHERE herdr_session = ?",
                (herdr_session,),
            ).fetchone()
            if existing is not None:
                return _thread_from_record(existing)
            workspace = connection.execute(
                "SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise ValueError("workspace_id 不存在")
            row = {
                "id": _new_id("th_"),
                "workspace_id": workspace_id,
                "herdr_session": herdr_session,
                "title": _title(title, herdr_session),
                "created_at": _now(),
            }
            connection.execute(
                "INSERT INTO threads (id, workspace_id, herdr_session, title, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    row["id"], row["workspace_id"], row["herdr_session"],
                    row["title"], row["created_at"],
                ),
            )
            return dict(row)


def delete_thread(thread_id: str) -> bool:
    with _lock:
        with _database() as connection:
            cursor = connection.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            return cursor.rowcount > 0


def _workspace_from_record(record: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(record["id"]),
        "path": str(record["path"]),
        "title": str(record["title"]),
        "created_at": str(record["created_at"]),
        "order": int(record["sort_order"]),
    }


def _thread_from_record(record: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(record["id"]),
        "workspace_id": str(record["workspace_id"]),
        "herdr_session": str(record["herdr_session"]),
        "title": str(record["title"]),
        "created_at": str(record["created_at"]),
    }


def list_workspaces() -> list[dict[str, Any]]:
    with _lock:
        with _database() as connection:
            rows = connection.execute(
                "SELECT id, path, title, created_at, sort_order FROM workspaces "
                "ORDER BY sort_order, created_at, id"
            ).fetchall()
    return [_workspace_from_record(row) for row in rows]


def list_threads(workspace_id: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        with _database() as connection:
            if workspace_id is None:
                rows = connection.execute(
                    "SELECT id, workspace_id, herdr_session, title, created_at "
                    "FROM threads ORDER BY created_at, id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, workspace_id, herdr_session, title, created_at "
                    "FROM threads WHERE workspace_id = ? ORDER BY created_at, id",
                    (workspace_id,),
                ).fetchall()
    return [_thread_from_record(row) for row in rows]


def get_workspace(workspace_id: str) -> dict[str, Any] | None:
    with _lock:
        with _database() as connection:
            row = connection.execute(
                "SELECT id, path, title, created_at, sort_order FROM workspaces "
                "WHERE id = ?",
                (workspace_id,),
            ).fetchone()
    return _workspace_from_record(row) if row is not None else None


def get_thread(thread_id: str) -> dict[str, Any] | None:
    with _lock:
        with _database() as connection:
            row = connection.execute(
                "SELECT id, workspace_id, herdr_session, title, created_at "
                "FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
    return _thread_from_record(row) if row is not None else None


def get_thread_by_session(herdr_session: str) -> dict[str, Any] | None:
    with _lock:
        with _database() as connection:
            row = connection.execute(
                "SELECT id, workspace_id, herdr_session, title, created_at "
                "FROM threads WHERE herdr_session = ?",
                (herdr_session,),
            ).fetchone()
    return _thread_from_record(row) if row is not None else None


def normalize_delivery(value: Any) -> str:
    if value in _MESSAGE_DELIVERIES:
        return str(value)
    if value in (None, ""):
        return "interrupt"
    raise ValueError("群聊消息投递类型无效")


def _validate_message(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("群聊消息字段无效")
    keys = set(row)
    if not _MESSAGE_FIELDS <= keys or not keys <= (_MESSAGE_FIELDS | _OPTIONAL_MESSAGE_FIELDS):
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
    out: dict[str, Any] = {
        "id": row["id"],
        "session": row["session"],
        "kind": row["kind"],
        "sender": row["sender"],
        "text": row["text"],
        "to": list(row["to"]),
        "ts": row["ts"],
    }
    if "delivery" in row:
        out["delivery"] = normalize_delivery(row["delivery"])
    if "notified_to" in row:
        dests = row["notified_to"]
        if not isinstance(dests, list) or any(
            not isinstance(item, str) or not item.strip() for item in dests
        ):
            raise ValueError("群聊消息投递记录无效")
        out["notified_to"] = [item.strip() for item in dests]
    if "read_by" in row:
        dests = row["read_by"]
        if not isinstance(dests, list) or any(
            not isinstance(item, str) or not item.strip() for item in dests
        ):
            raise ValueError("群聊消息已读记录无效")
        out["read_by"] = [item.strip() for item in dests]
    if "duration_ms" in row:
        duration = row["duration_ms"]
        if type(duration) is not int or duration < 0:
            raise ValueError("群聊消息耗时无效")
        out["duration_ms"] = duration
    if "git" in row:
        out["git"] = normalize_git_card(row["git"])
    if "source" in row:
        source = row["source"].strip() if isinstance(row["source"], str) else ""
        if not source or len(source) > 32:
            raise ValueError("群聊消息来源标记无效")
        out["source"] = source
    if "direct" in row:
        if type(row["direct"]) is not bool:
            raise ValueError("群聊消息定向标记无效")
        out["direct"] = row["direct"]
    return out


def normalize_git_card(value: Any) -> dict[str, Any]:
    """git 变更卡片：files 为改动文件数，stat 为截断后的 diff --stat 概要。"""
    if not isinstance(value, dict):
        raise ValueError("群聊消息 git 卡片无效")
    files = value.get("files")
    stat = value.get("stat")
    if type(files) is not int or files < 0:
        raise ValueError("群聊消息 git 卡片无效")
    if not isinstance(stat, str) or len(stat) > 4096:
        raise ValueError("群聊消息 git 卡片无效")
    return {"files": files, "stat": stat}


def _decode_json(value: Any, expected: type) -> Any:
    if not isinstance(value, str):
        raise ChatLedgerError("row_corrupt")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ChatLedgerError("row_corrupt") from exc
    if not isinstance(decoded, expected):
        raise ChatLedgerError("row_corrupt")
    return decoded


def _message_from_record(record: sqlite3.Row) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": str(record["id"]),
        "session": str(record["session"]),
        "kind": str(record["kind"]),
        "sender": str(record["sender"]),
        "text": str(record["text"]),
        "to": _decode_json(record["recipients"], list),
        "ts": int(record["ts"]),
    }
    for name in ("delivery", "duration_ms", "source"):
        if record[name] is not None:
            row[name] = record[name]
    for name in ("notified_to", "read_by"):
        if record[name] is not None:
            row[name] = _decode_json(record[name], list)
    if record["git"] is not None:
        row["git"] = _decode_json(record["git"], dict)
    if record["direct"] is not None:
        row["direct"] = bool(record["direct"])
    return _validate_message(row)


_MESSAGE_SELECT = (
    "SELECT id, session, kind, sender, text, recipients, ts, delivery, "
    "notified_to, read_by, duration_ms, git, source, direct FROM messages"
)


def _find_message(
    connection: sqlite3.Connection, message_id: str,
) -> dict[str, Any] | None:
    record = connection.execute(
        f"{_MESSAGE_SELECT} WHERE id = ?", (message_id,),
    ).fetchone()
    return _message_from_record(record) if record is not None else None


def append_message(
    session: str,
    *,
    kind: str,
    sender: str,
    text: str,
    to: list[str] | None = None,
    ts: int | None = None,
    delivery: str | None = None,
    notified_to: list[str] | None = None,
    read_by: list[str] | None = None,
    duration_ms: int | None = None,
    git: dict[str, Any] | None = None,
    source: str | None = None,
    direct: bool = False,
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
    row: dict[str, Any] = {
        "id": _new_id("msg_"),
        "session": session,
        "kind": kind,
        "sender": sender.strip() or "human",
        "text": body,
        "to": recipients,
        "ts": stamp,
    }
    if delivery is not None:
        row["delivery"] = normalize_delivery(delivery)
    if notified_to:
        row["notified_to"] = [
            item.strip() for item in notified_to if isinstance(item, str) and item.strip()
        ]
    if read_by:
        row["read_by"] = [
            item.strip() for item in read_by if isinstance(item, str) and item.strip()
        ]
    if duration_ms is not None:
        row["duration_ms"] = duration_ms
    if git is not None:
        row["git"] = normalize_git_card(git)
    if source is not None:
        row["source"] = source
    if type(direct) is not bool:
        raise ValueError("群聊消息定向标记无效")
    if direct:
        row["direct"] = True
    row = _validate_message(row)
    with _lock:
        with _database() as connection:
            _insert_message(connection, row)
    return dict(row)


def replace_message_text(message_id: str, text: str) -> dict[str, Any] | None:
    """同一条 agent 结论变长时原地改正文，不另开气泡。"""
    if not isinstance(message_id, str) or not re.fullmatch(r"msg_[0-9a-f]{12}", message_id):
        raise ValueError("群聊消息 ID 无效")
    body = text.strip() if isinstance(text, str) else ""
    if not body:
        raise ValueError("群聊消息正文无效")
    if len(body) > 16_384:
        body = body[:16_384]
    with _lock:
        with _database() as connection:
            row = _find_message(connection, message_id)
            if row is not None:
                updated = dict(row)
                updated["text"] = body
                updated = _validate_message(updated)
                connection.execute(
                    "UPDATE messages SET text = ? WHERE id = ?", (body, message_id),
                )
                return updated
    return None


def mark_message_notified(message_id: str, recipients: list[str]) -> dict[str, Any] | None:
    """排队消息投递后记下已叫醒的收件人，避免空闲后再推一次。"""
    if not isinstance(message_id, str) or not re.fullmatch(r"msg_[0-9a-f]{12}", message_id):
        raise ValueError("群聊消息 ID 无效")
    extra = [item.strip() for item in recipients if isinstance(item, str) and item.strip()]
    if not extra:
        return None
    with _lock:
        with _database() as connection:
            row = _find_message(connection, message_id)
            if row is not None:
                updated = dict(row)
                seen = {
                    item for item in (updated.get("notified_to") or [])
                    if isinstance(item, str)
                }
                seen.update(extra)
                updated["notified_to"] = sorted(seen)
                updated = _validate_message(updated)
                connection.execute(
                    "UPDATE messages SET notified_to = ? WHERE id = ?",
                    (_json_value(updated["notified_to"]), message_id),
                )
                return updated
    return None


def mark_messages_read(session: str, recipient: str) -> list[dict[str, Any]]:
    """对方已开始处理后，把已投递给他的 Boss 消息标成已读。"""
    if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
        raise ValueError("herdr_session 无效")
    dest = recipient.strip() if isinstance(recipient, str) else ""
    if not dest:
        return []
    changed: list[dict[str, Any]] = []
    with _lock:
        with _database() as connection:
            records = connection.execute(
                f"{_MESSAGE_SELECT} WHERE session = ? AND kind = 'me' ORDER BY ts, id",
                (session,),
            ).fetchall()
            for record in records:
                row = _message_from_record(record)
                targets = [item for item in (row.get("to") or []) if isinstance(item, str)]
                if dest not in targets:
                    continue
                notified = {
                    item for item in (row.get("notified_to") or []) if isinstance(item, str)
                }
                if dest not in notified:
                    continue
                seen = {
                    item for item in (row.get("read_by") or []) if isinstance(item, str)
                }
                if dest in seen:
                    continue
                updated = dict(row)
                updated["read_by"] = sorted(seen | {dest})
                updated = _validate_message(updated)
                connection.execute(
                    "UPDATE messages SET read_by = ? WHERE id = ?",
                    (_json_value(updated["read_by"]), updated["id"]),
                )
                changed.append(updated)
    return changed


def set_message_duration(message_id: str, duration_ms: int) -> dict[str, Any] | None:
    """结论收进账本时记下这一轮用了多久。"""
    if not isinstance(message_id, str) or not re.fullmatch(r"msg_[0-9a-f]{12}", message_id):
        raise ValueError("群聊消息 ID 无效")
    if type(duration_ms) is not int or duration_ms < 0:
        raise ValueError("群聊消息耗时无效")
    with _lock:
        with _database() as connection:
            row = _find_message(connection, message_id)
            if row is not None:
                updated = dict(row)
                updated["duration_ms"] = duration_ms
                updated = _validate_message(updated)
                connection.execute(
                    "UPDATE messages SET duration_ms = ? WHERE id = ?",
                    (duration_ms, message_id),
                )
                return updated
    return None


def set_message_git(message_id: str, git: dict[str, Any] | None) -> dict[str, Any] | None:
    """结论原地更新时刷新 git 变更卡片；None 表示清掉旧卡片。"""
    if not isinstance(message_id, str) or not re.fullmatch(r"msg_[0-9a-f]{12}", message_id):
        raise ValueError("群聊消息 ID 无效")
    card = normalize_git_card(git) if git is not None else None
    with _lock:
        with _database() as connection:
            row = _find_message(connection, message_id)
            if row is not None:
                updated = dict(row)
                if card is None:
                    updated.pop("git", None)
                else:
                    updated["git"] = card
                updated = _validate_message(updated)
                connection.execute(
                    "UPDATE messages SET git = ? WHERE id = ?",
                    (_json_value(card) if card is not None else None, message_id),
                )
                return updated
    return None


def list_messages(session: str, limit: int = 200) -> list[dict[str, Any]]:
    if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
        raise ValueError("herdr_session 无效")
    cap = max(1, min(int(limit), 200))
    with _lock:
        with _database() as connection:
            records = connection.execute(
                f"{_MESSAGE_SELECT} WHERE session = ? "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (session, cap),
            ).fetchall()
    return [_message_from_record(record) for record in reversed(records)]
