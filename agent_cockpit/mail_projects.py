"""Cockpit session 到 Agent Mail human key 的本地绑定。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import runtime_paths


STATE_PATH = runtime_paths.store("mail_projects")
_lock = threading.RLock()


def _absolute(value: str, label: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label}必须是绝对路径")
    return str(path.resolve())


def _load() -> dict[str, Any]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "sessions": {}}
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return {"version": 1, "sessions": {}}
    return {"version": 1, "sessions": dict(data["sessions"])}


def _write(data: dict[str, Any]) -> None:
    runtime_paths.validate_store("mail_projects")  # R3-B:symlink 逃逸 fail-closed
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".mail-projects.", suffix=".tmp", dir=str(STATE_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, STATE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get(session: str, session_dir: str) -> str | None:
    """仅在 session 名和 session_dir 都一致时返回绑定。"""
    generation = _absolute(session_dir, "session_dir")
    with _lock:
        entry = _load()["sessions"].get(session)
    if not isinstance(entry, dict) or entry.get("session_dir") != generation:
        return None
    project = entry.get("project")
    return project if isinstance(project, str) and project else None


def bind(
    session: str,
    session_dir: str,
    project: str,
    *,
    replace: bool = False,
) -> str:
    """写入绑定；同一代 session 改项目必须显式 replace。"""
    generation = _absolute(session_dir, "session_dir")
    project_key = _absolute(project, "Agent Mail 项目")
    with _lock:
        data = _load()
        current = data["sessions"].get(session)
        if (
            isinstance(current, dict)
            and current.get("session_dir") == generation
            and current.get("project") not in (None, project_key)
            and not replace
        ):
            raise ValueError(
                f"session {session} 已绑定 {current['project']}，重新绑定需显式确认"
            )
        data["sessions"][session] = {
            "session_dir": generation,
            "project": project_key,
        }
        _write(data)
    return project_key


def unbind(session: str, session_dir: str | None = None) -> bool:
    """删除绑定；提供 session_dir 时只删除同一代 session。"""
    generation = _absolute(session_dir, "session_dir") if session_dir else None
    with _lock:
        data = _load()
        current = data["sessions"].get(session)
        if not isinstance(current, dict):
            return False
        if generation is not None and current.get("session_dir") != generation:
            return False
        del data["sessions"][session]
        _write(data)
    return True
