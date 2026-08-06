"""本机 Herdr Session 到远程 TeamProject 的显式绑定。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


STATE_PATH = Path.home() / "dashboard-data" / "team-sessions.json"
_lock = threading.RLock()


def _load() -> dict[str, Any]:
    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 1, "bindings": []}
    except (OSError, UnicodeError) as exc:
        raise OSError("Team Session 绑定状态不可读") from exc
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise OSError("Team Session 绑定状态已损坏") from exc
    bindings = data.get("bindings") if isinstance(data, dict) else None
    if not isinstance(bindings, list):
        raise OSError("Team Session 绑定状态格式无效")
    return {"version": 1, "bindings": [row for row in bindings if isinstance(row, dict)]}


def _write(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".team-sessions.", suffix=".tmp", dir=str(STATE_PATH.parent)
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, STATE_PATH)
        os.chmod(STATE_PATH, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_bindings(hub: str, human_id: int) -> list[dict[str, Any]]:
    with _lock:
        rows = _load()["bindings"]
    return [
        dict(row) for row in rows
        if row.get("hub") == hub and row.get("human_id") == human_id
    ]


def conflicts_for(
    *,
    hub: str,
    human_id: int,
    project_slug: str,
    session: str,
    session_generation: str,
) -> list[dict[str, Any]]:
    """返回会被本次绑定替换的当前代际绑定。"""
    with _lock:
        rows = _load()["bindings"]
    return [
        dict(row) for row in rows
        if row.get("hub") == hub
        and row.get("human_id") == human_id
        and (
            row.get("project_slug") == project_slug
            or (
                row.get("session") == session
                and row.get("session_generation") == session_generation
            )
        )
        and not (
            row.get("project_slug") == project_slug
            and row.get("session") == session
            and row.get("session_generation") == session_generation
        )
    ]


def bind(
    *,
    hub: str,
    human_id: int,
    project_slug: str,
    session: str,
    session_generation: str,
    session_dir: str,
    mail_project: str,
    lead: dict[str, str],
    client_session_id: str,
    agent_id: int,
    reply_token: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """同一用户/Hub 下保持 Session↔TeamProject 一对一；改绑需显式确认。"""
    generation = str(session_generation).strip()
    if not generation:
        raise ValueError("Session generation 不能为空")
    client_id = str(client_session_id).strip()
    if not client_id:
        raise ValueError("Team Session 标识不能为空")
    if reply_token is not None and (
        not isinstance(reply_token, str)
        or not reply_token
        or len(reply_token) > 128
    ):
        raise ValueError("Team Session 回复凭据无效")
    entry = {
        "hub": hub,
        "human_id": int(human_id),
        "project_slug": project_slug,
        "session": session,
        "session_generation": generation,
        "session_dir": str(Path(session_dir).expanduser().resolve()),
        "mail_project": str(Path(mail_project).expanduser().resolve()),
        "lead": {
            key: str(lead.get(key) or "")
            for key in ("pane_id", "agent", "mail_name", "participant_id")
        },
        "client_session_id": client_id,
        "agent_id": int(agent_id),
        "updated_ts": time.time(),
    }
    with _lock:
        data = _load()
        scoped = [
            row for row in data["bindings"]
            if row.get("hub") == hub and row.get("human_id") == human_id
        ]
        conflicts = [
            row for row in scoped
            if (
                row.get("project_slug") == project_slug
                or (
                    row.get("session") == session
                    and row.get("session_generation") == generation
                )
            )
            and not (
                row.get("project_slug") == project_slug
                and row.get("session") == session
                and row.get("session_generation") == generation
            )
        ]
        if conflicts and not replace:
            raise ValueError("Session 或团队项目已有绑定，改绑需要显式确认")
        existing = next((
            row for row in scoped
            if row.get("project_slug") == project_slug
            and row.get("session") == session
            and row.get("session_generation") == generation
            and row.get("client_session_id") == client_id
        ), None)
        effective_reply_token = reply_token
        if effective_reply_token is None and existing is not None:
            saved_token = existing.get("reply_token")
            if isinstance(saved_token, str) and saved_token:
                effective_reply_token = saved_token
        if effective_reply_token is not None:
            entry["reply_token"] = effective_reply_token
        data["bindings"] = [
            row for row in data["bindings"]
            if not (
                row.get("hub") == hub
                and row.get("human_id") == human_id
                and (
                    row.get("project_slug") == project_slug
                    or row.get("session") == session
                )
            )
        ]
        data["bindings"].append(entry)
        _write(data)
    return dict(entry)


def update_reply_token(
    *,
    hub: str,
    human_id: int,
    project_slug: str,
    client_session_id: str,
    reply_token: str,
) -> dict[str, Any]:
    """更新远端重建/轮换后的单 binding 回复凭据。"""
    if not isinstance(reply_token, str) or not reply_token or len(reply_token) > 128:
        raise ValueError("Team Session 回复凭据无效")
    with _lock:
        data = _load()
        matches = [
            row for row in data["bindings"]
            if row.get("hub") == hub
            and row.get("human_id") == human_id
            and row.get("project_slug") == project_slug
            and row.get("client_session_id") == client_session_id
        ]
        if len(matches) != 1:
            raise ValueError("Team Session 绑定缺失或不唯一")
        matches[0]["reply_token"] = reply_token
        matches[0]["updated_ts"] = time.time()
        _write(data)
        return dict(matches[0])


def reply_bindings_for_lead(
    mail_project: str, lead_mail_name: str,
) -> list[dict[str, Any]]:
    """按本机物理 mail project + lead 花名精确解析回复路由。"""
    project = str(Path(mail_project).expanduser().resolve())
    with _lock:
        rows = _load()["bindings"]
    return [
        dict(row) for row in rows
        if row.get("mail_project") == project
        and isinstance(row.get("lead"), dict)
        and row["lead"].get("mail_name") == lead_mail_name
        and isinstance(row.get("reply_token"), str)
        and bool(row.get("reply_token"))
    ]


def unbind_project(hub: str, human_id: int, project_slug: str) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        removed = next((
            row for row in data["bindings"]
            if row.get("hub") == hub
            and row.get("human_id") == human_id
            and row.get("project_slug") == project_slug
        ), None)
        if removed is None:
            return None
        data["bindings"] = [row for row in data["bindings"] if row is not removed]
        _write(data)
    return dict(removed)
