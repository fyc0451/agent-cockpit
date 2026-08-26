"""本机 Herdr Session 到远程 TeamProject 的显式绑定。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import runtime_paths


STATE_PATH = runtime_paths.store("team_sessions")
_lock = threading.RLock()
_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _load() -> dict[str, Any]:
    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 1, "bindings": [], "managed_sessions": []}
    except (OSError, UnicodeError) as exc:
        raise OSError("Team Session 绑定状态不可读") from exc
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise OSError("Team Session 绑定状态已损坏") from exc
    bindings = data.get("bindings") if isinstance(data, dict) else None
    if not isinstance(bindings, list):
        raise OSError("Team Session 绑定状态格式无效")
    rows = [row for row in bindings if isinstance(row, dict)]
    raw_managed = data.get("managed_sessions", [])
    if (
        not isinstance(raw_managed, list)
        or any(
            not isinstance(name, str) or _SESSION_NAME_RE.fullmatch(name) is None
            for name in raw_managed
        )
        or len(set(raw_managed)) != len(raw_managed)
    ):
        raise OSError("Team Session 绑定状态格式无效")
    managed = set(raw_managed)
    managed.update(
        str(row.get("session"))
        for row in rows
        if row.get("managed_runtime") is True and row.get("session")
    )
    return {
        "version": 1,
        "bindings": rows,
        "managed_sessions": sorted(managed),
    }


def _write(data: dict[str, Any]) -> None:
    runtime_paths.validate_store("team_sessions")  # R3-B:symlink 逃逸 fail-closed
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


def is_managed_session(session: str, session_generation: str) -> bool:
    """当前精确代际是否已被 Human 明确指定为 Team 专用 Session。"""
    name = str(session).strip()
    generation = str(session_generation).strip()
    if not name or not generation:
        return False
    with _lock:
        rows = _load()["bindings"]
    return any(
        row.get("session") == name
        and row.get("session_generation") == generation
        and row.get("managed_runtime") is True
        for row in rows
    )


def managed_binding_for_session(session: str) -> dict[str, Any] | None:
    """返回当前仍绑定的 Team 专用 Session；普通会话不匹配。"""
    name = str(session).strip()
    if not name:
        return None
    with _lock:
        rows = _load()["bindings"]
    row = next((
        item for item in rows
        if item.get("session") == name
        and item.get("managed_runtime") is True
    ), None)
    return dict(row) if row is not None else None


def managed_session_names() -> set[str]:
    """返回全部 Team 专用 Session 名，包括等待删除的已替换 runtime。"""
    with _lock:
        names = _load()["managed_sessions"]
    return set(names)


def forget_managed_session(session: str) -> bool:
    """仅在真实 Session 已删除后移除 read-model 隔离 tombstone。"""
    name = str(session).strip()
    if _SESSION_NAME_RE.fullmatch(name) is None:
        raise ValueError("Session 名称无效")
    with _lock:
        data = _load()
        current = set(data["managed_sessions"])
        if name not in current:
            return False
        current.remove(name)
        data["managed_sessions"] = sorted(current)
        _write(data)
    return True


def managed_binding_for_sender(
    instance_id: str, mail_name: str, mail_project: str,
) -> dict[str, Any] | None:
    """精确识别受管 Team sender；不得仅凭可伪造的花名或项目判断。"""
    instance = str(instance_id).strip()
    name = str(mail_name).strip()
    try:
        project = str(Path(mail_project).expanduser().resolve())
    except (OSError, ValueError):
        return None
    if not instance or not name:
        return None
    with _lock:
        rows = _load()["bindings"]
    matches = []
    for row in rows:
        lead = row.get("lead") if isinstance(row.get("lead"), dict) else {}
        try:
            row_project = str(
                Path(str(row.get("mail_project") or "")).expanduser().resolve()
            )
        except (OSError, ValueError):
            continue
        if (
            row.get("managed_runtime") is True
            and row.get("session_generation") == instance
            and lead.get("mail_name") == name
            and row_project == project
        ):
            matches.append(row)
    return dict(matches[0]) if len(matches) == 1 else None


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
    reply_mode: str | None = None,
    auth_expires_at: float | None = None,
    managed_runtime: bool = False,
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
    if reply_mode is not None and reply_mode not in {"confirm", "auto"}:
        raise ValueError("Team Session 回复模式无效")
    if auth_expires_at is not None and (
        isinstance(auth_expires_at, bool)
        or not isinstance(auth_expires_at, (int, float))
        or auth_expires_at <= 0
    ):
        raise ValueError("Team Human 登录租约无效")
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
        "managed_runtime": bool(managed_runtime),
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
        effective_reply_mode = reply_mode
        if effective_reply_mode is None and existing is not None:
            saved_mode = existing.get("reply_mode")
            if saved_mode in {"confirm", "auto"}:
                effective_reply_mode = saved_mode
        entry["reply_mode"] = effective_reply_mode or "confirm"
        effective_auth_expiry = auth_expires_at
        if effective_auth_expiry is None and existing is not None:
            saved_expiry = existing.get("auth_expires_at")
            if isinstance(saved_expiry, (int, float)) and not isinstance(
                saved_expiry, bool
            ):
                effective_auth_expiry = float(saved_expiry)
        if effective_auth_expiry is not None:
            entry["auth_expires_at"] = float(effective_auth_expiry)
        existing_lead = (
            existing.get("lead")
            if existing is not None and isinstance(existing.get("lead"), dict)
            else {}
        )
        same_lead_identity = all(
            existing_lead.get(key) == entry["lead"].get(key)
            for key in ("agent", "mail_name", "participant_id")
        )
        if (
            same_lead_identity
            and existing is not None
            and isinstance(existing.get("consult_target"), dict)
        ):
            entry["consult_target"] = dict(existing["consult_target"])
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
        if managed_runtime:
            data["managed_sessions"] = sorted({
                *data["managed_sessions"], session,
            })
        _write(data)
    return dict(entry)


def set_consult_target(
    *,
    hub: str,
    human_id: int,
    project_slug: str,
    target: dict[str, Any] | None,
) -> dict[str, Any]:
    """为一个 Topic 显式选择同项目普通开发 Lead；None 表示关闭咨询。"""
    normalized: dict[str, Any] | None = None
    if target is not None:
        lead = target.get("lead") if isinstance(target.get("lead"), dict) else {}
        required = (
            str(target.get("session") or "").strip(),
            str(target.get("session_generation") or "").strip(),
            str(target.get("mail_project") or "").strip(),
            str(lead.get("mail_name") or "").strip(),
        )
        if not all(required):
            raise ValueError("咨询目标无效")
        normalized = {
            "session": required[0],
            "session_generation": required[1],
            "mail_project": str(Path(required[2]).expanduser().resolve()),
            "lead": {
                key: str(lead.get(key) or "")
                for key in ("agent", "mail_name", "participant_id")
            },
            "updated_ts": time.time(),
        }
    with _lock:
        data = _load()
        row = next((
            item for item in data["bindings"]
            if item.get("hub") == hub
            and item.get("human_id") == human_id
            and item.get("project_slug") == project_slug
        ), None)
        if row is None:
            raise KeyError("团队项目尚未绑定本机 Session")
        if normalized is not None and row.get("managed_runtime") is not True:
            raise ValueError("只有 Topic 专用 Team Agent 可以选择咨询目标")
        if (
            normalized is not None
            and normalized.get("mail_project") != row.get("mail_project")
        ):
            raise ValueError("咨询目标必须属于同一项目")
        if normalized is None:
            row.pop("consult_target", None)
        else:
            row["consult_target"] = normalized
        row["updated_ts"] = time.time()
        _write(data)
        return dict(row)


def authorize_human(
    *, hub: str, human_id: int, auth_expires_at: float,
) -> int:
    """刷新已由 Hub 验证的 Human 对本机 binding 的使用租约。"""
    if (
        isinstance(auth_expires_at, bool)
        or not isinstance(auth_expires_at, (int, float))
        or auth_expires_at <= 0
    ):
        raise ValueError("Team Human 登录租约无效")
    updated = 0
    with _lock:
        data = _load()
        for row in data["bindings"]:
            if row.get("hub") != hub or row.get("human_id") != human_id:
                continue
            if row.get("auth_expires_at") != float(auth_expires_at):
                row["auth_expires_at"] = float(auth_expires_at)
                updated += 1
        if updated:
            _write(data)
    return updated


def binding_auth_active(row: dict[str, Any], now: float | None = None) -> bool:
    """旧 binding 无登录租约时 fail closed；过期后 worker 立即停领。"""
    expires_at = row.get("auth_expires_at")
    return bool(
        isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and float(expires_at) > (time.time() if now is None else now)
    )


def suspend_human(
    *, hub: str, human_id: int, revoke_capability: bool,
) -> list[dict[str, Any]]:
    """暂停 Human 的全部本机 binding；可同时销毁本地 reply capability。"""
    suspended: list[dict[str, Any]] = []
    with _lock:
        data = _load()
        for row in data["bindings"]:
            if row.get("hub") != hub or row.get("human_id") != human_id:
                continue
            suspended.append(dict(row))
            row["auth_expires_at"] = 0.0
            if revoke_capability:
                row.pop("reply_token", None)
        if suspended:
            _write(data)
    return suspended


def suspend_all(*, revoke_capability: bool) -> list[dict[str, Any]]:
    """无有效 Human Cookie 的显式注销仍须 fail closed 暂停本机自动化。"""
    with _lock:
        data = _load()
        suspended = [dict(row) for row in data["bindings"]]
        for row in data["bindings"]:
            row["auth_expires_at"] = 0.0
            if revoke_capability:
                row.pop("reply_token", None)
        if suspended:
            _write(data)
    return suspended


def update_reply_token(
    *,
    hub: str,
    human_id: int,
    project_slug: str,
    client_session_id: str,
    reply_token: str,
) -> dict[str, Any]:
    """更新远端重建/轮换后的单 binding 回复凭据。"""
    return update_reply_capability(
        hub=hub,
        human_id=human_id,
        project_slug=project_slug,
        client_session_id=client_session_id,
        reply_token=reply_token,
    )


def update_reply_capability(
    *,
    hub: str,
    human_id: int,
    project_slug: str,
    client_session_id: str,
    reply_token: str,
    reply_mode: str | None = None,
) -> dict[str, Any]:
    """原子更新单 binding 的轮换凭据及可选回复模式。"""
    if not isinstance(reply_token, str) or not reply_token or len(reply_token) > 128:
        raise ValueError("Team Session 回复凭据无效")
    if reply_mode is not None and reply_mode not in {"confirm", "auto"}:
        raise ValueError("Team Session 回复模式无效")
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
        if reply_mode is not None:
            matches[0]["reply_mode"] = reply_mode
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
        and binding_auth_active(row)
        and row.get("managed_runtime") is True
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
