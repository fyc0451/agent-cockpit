"""群聊 session 的 Leader 花名。给 mail-send 和叫醒提示用，不进 herdr 配置。"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

LEADERS_DIR = Path.home() / ".agent-mail" / "session-leaders"
PANES_DIR = Path.home() / ".agent-mail" / "session-panes"
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PANE_RE = re.compile(r"^[A-Za-z0-9_]+:[A-Za-z0-9_]+$")


def leader_path(session: str) -> Path:
    if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
        raise ValueError("session 名无效")
    return LEADERS_DIR / f"{session}.json"


def get_session_leader(session: str) -> dict[str, str]:
    if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
        return {}
    path = LEADERS_DIR / f"{session}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    name = str(data.get("leader_mail_name") or "").strip()
    agent = str(data.get("leader_agent") or "").strip()
    if not name:
        return {}
    return {"leader_mail_name": name, "leader_agent": agent, "session": session}


def set_session_leader(session: str, mail_name: str, agent: str = "") -> dict[str, str]:
    path = leader_path(session)
    name = mail_name.strip()
    kind = agent.strip()
    if not name:
        raise ValueError("Leader 花名无效")
    row = {
        "version": 1,
        "session": session,
        "leader_mail_name": name,
        "leader_agent": kind,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".leader.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(row, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"leader_mail_name": name, "leader_agent": kind, "session": session}


def resolve_session_alias(recipient: str, record: dict[str, Any] | None) -> str:
    if not record or not recipient:
        return ""
    name = str(record.get("leader_mail_name") or "").strip()
    agent = str(record.get("leader_agent") or "").strip().lower()
    if not name:
        return ""
    key = recipient.strip()
    if key.lower() == "leader":
        return name
    if not agent:
        return ""
    lowered = key.lower()
    if lowered == agent or lowered == f"{agent}-main":
        return name
    return ""


def _pane_names_path(session: str) -> Path | None:
    if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
        return None
    return PANES_DIR / f"{session}.json"


def get_pane_mail_name(session: str, pane_id: str) -> str:
    path = _pane_names_path(session)
    if path is None or not isinstance(pane_id, str) or not pane_id:
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    names = data.get("panes")
    if not isinstance(names, dict):
        return ""
    name = str(names.get(pane_id) or "").strip()
    return name


def set_pane_mail_name(session: str, pane_id: str, mail_name: str) -> None:
    path = _pane_names_path(session)
    if path is None or not isinstance(pane_id, str) or not pane_id:
        return
    name = mail_name.strip()
    if not name:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, UnicodeError, ValueError):
        data = {}
    panes = data.get("panes")
    if not isinstance(panes, dict):
        panes = {}
    if panes.get(pane_id) == name:
        return
    panes[pane_id] = name
    row = {"version": 1, "session": session, "panes": panes}
    fd, tmp = tempfile.mkstemp(prefix=".panes.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(row, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
