"""群聊持续工作 harness：交接单还有下一步时，把空闲 Leader 叫醒，不靠 Boss 再 @。"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

DEFAULT_HANDOFF = Path(
    os.environ.get(
        "AGENT_MEMORY_HANDOFF",
        "/mnt/d/Obsidian/agent-memory/handoff/agent-cockpit-next.md",
    )
)
DEFAULT_PROJECT = os.environ.get("AGENT_MEMORY_PROJECT", "agent-cockpit-next")
# 只挡 pane_send 后状态还没翻成 working 的连发；做完变空闲应尽快再叫。
DEFAULT_MIN_GAP = 90.0
_BUSY = frozenset({"working", "blocked"})


def project_key_of(path: str) -> str | None:
    marker = Path(path) / ".agent-memory-project"
    try:
        for line in marker.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text:
                return text
    except OSError:
        return None
    return None


def bound_sessions_for_project(
    *,
    workspaces: list[dict[str, Any]],
    threads: list[dict[str, Any]],
    project: str,
    extra_paths: set[Path] | None = None,
) -> set[str]:
    """只叫醒本项目工作区绑定的群，不把交接单灌进别的 session。"""
    extra = set()
    for item in extra_paths or set():
        try:
            extra.add(item.resolve())
        except OSError:
            continue
    allowed: set[str] = set()
    for workspace in workspaces:
        ws_id = str(workspace.get("id") or "")
        path = str(workspace.get("path") or "")
        if not ws_id or not path:
            continue
        if project_key_of(path) == project:
            allowed.add(ws_id)
            continue
        try:
            if Path(path).resolve() in extra:
                allowed.add(ws_id)
        except OSError:
            continue
    return {
        str(thread["herdr_session"])
        for thread in threads
        if str(thread.get("workspace_id") or "") in allowed and thread.get("herdr_session")
    }


@dataclass(frozen=True)
class Wake:
    session: str
    pane_id: str
    mail_name: str
    next_item: str


def parse_next_steps(text: str) -> list[str]:
    items: list[str] = []
    in_section = False
    for line in (text or "").splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## 下一步"
            continue
        if not in_section:
            continue
        match = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if match:
            items.append(match.group(1).strip())
    return items


def wake_prompt(
    session: str,
    next_item: str,
    *,
    wake_mail_name: str = "",
    leader_mail_name: str = "",
) -> str:
    wake_name = wake_mail_name.strip()
    leader_name = leader_mail_name.strip()
    if leader_name and wake_name.casefold() == leader_name.casefold():
        report = (
            f"你就是本群 Leader（{leader_name}），不要给自己发 Agent Mail；"
            "完成后直接在终端写结论并更新交接单。"
        )
    elif leader_name:
        report = (
            f"完成后给 Leader 回信：mail-send --to leader --thread {session}。"
        )
    else:
        report = "本群尚未登记 Leader，不要猜收件人；完成后在终端写结论。"
    return (
        "【持续工作】不要等 Boss 再 @。交接单下一步还没做完。\n"
        f"请立刻做：{next_item}\n"
        f"{report}"
        "有持久事实写 session。"
    )


def _pane_idle(pane: dict[str, Any]) -> bool:
    status = str(pane.get("agent_status") or "unknown")
    return status not in _BUSY


def is_watch_item(item: str) -> bool:
    """Leader 盯梢项：别人还在做编号活时不要连叫。"""
    text = item or ""
    return (
        "盯交付" in text
        or "空闲就分下一刀" in text
        or "等 Boss" in text
        or "等待 Boss" in text
        or "待 Boss" in text
        or "拍板" in text
        or re.search(r"Boss.{0,24}(?:确认|授权|回复)", text) is not None
    )


def match_assignee(
    item: str,
    panes: list[dict[str, Any]],
    *,
    session: str,
    leader: str,
) -> dict[str, Any] | None:
    """交接单写了花名或 agent 类型就点那个人；否则点 Leader。"""
    text = (item or "").strip()
    if not text:
        return None
    lowered = text.lower()
    session_panes = [
        pane for pane in panes
        if isinstance(pane, dict)
        and str(pane.get("session") or "") == session
        and pane.get("agent")
        and pane.get("pane_id")
    ]
    named: list[dict[str, Any]] = []
    for pane in session_panes:
        mail = str(pane.get("mail_name") or "").strip()
        if mail and mail.lower() in lowered:
            named.append(pane)
    if len(named) == 1:
        return named[0]
    kinds: dict[str, list[dict[str, Any]]] = {}
    for pane in session_panes:
        kind = str(pane.get("agent") or "").strip().lower()
        if kind and re.search(rf"(^|[\s@/]){re.escape(kind)}(\s|-|$)", lowered):
            kinds.setdefault(kind, []).append(pane)
    unique = [rows[0] for rows in kinds.values() if len(rows) == 1]
    if len(unique) == 1:
        return unique[0]
    for pane in session_panes:
        mail = str(pane.get("mail_name") or "").strip()
        if leader and mail == leader:
            return pane
    return None


def plan_wakes(
    *,
    panes: list[dict[str, Any]],
    bound_sessions: set[str],
    leaders: dict[str, str],
    next_item: str = "",
    next_items: list[str] | None = None,
    now: float,
    last_wake: dict[str, float],
    min_gap: float = DEFAULT_MIN_GAP,
) -> list[Wake]:
    items = [item for item in ([next_item] if next_item else []) + list(next_items or []) if item]
    if not items or not bound_sessions:
        return []
    # 交接单只剩「盯交付」时不要每 90 秒叫醒 Leader 空转。
    if all(is_watch_item(item) for item in items):
        return []
    out: list[Wake] = []
    claimed: set[str] = set()
    for session in bound_sessions:
        leader = (leaders.get(session) or "").strip()
        others_busy = False
        for item in items:
            if is_watch_item(item):
                continue
            assignee = match_assignee(item, panes, session=session, leader=leader)
            if assignee and not _pane_idle(assignee):
                others_busy = True
                break
        for item in items:
            pane = match_assignee(item, panes, session=session, leader=leader)
            if not pane or not _pane_idle(pane):
                continue
            mail = str(pane.get("mail_name") or "").strip()
            if others_busy and is_watch_item(item) and leader and mail == leader:
                continue
            pane_id = str(pane.get("pane_id") or "")
            key = f"{session}|{pane_id}"
            if key in claimed or now - float(last_wake.get(key) or 0) < min_gap:
                continue
            mail = mail or str(pane.get("agent") or "")
            out.append(Wake(session, pane_id, mail, item))
            claimed.add(key)
    return out


def load_state(path: Path) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}
    rows = raw.get("wakes") if isinstance(raw, dict) else None
    if not isinstance(rows, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in rows.items():
        if isinstance(key, str) and isinstance(value, (int, float)):
            out[key] = float(value)
    return out


def save_state(path: Path, last_wake: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"wakes": last_wake}, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def tick(
    *,
    now: float,
    panes: list[dict[str, Any]],
    bound_sessions: set[str],
    leaders: dict[str, str],
    handoff_text: str,
    send: Callable[..., dict[str, Any]],
    state_path: Path,
    min_gap: float = DEFAULT_MIN_GAP,
) -> list[Wake]:
    items = parse_next_steps(handoff_text)
    if not items:
        return []
    last = load_state(state_path)
    planned = plan_wakes(
        panes=panes,
        bound_sessions=bound_sessions,
        leaders=leaders,
        next_items=items,
        now=now,
        last_wake=last,
        min_gap=min_gap,
    )
    sent: list[Wake] = []
    for wake in planned:
        leader_name = str(leaders.get(wake.session) or "").strip()
        result = send(
            wake.session,
            wake.pane_id,
            wake_prompt(
                wake.session,
                wake.next_item,
                wake_mail_name=wake.mail_name,
                leader_mail_name=leader_name,
            ),
            "prompt",
        )
        if not isinstance(result, dict) or result.get("error") or result.get("skipped"):
            continue
        last[f"{wake.session}|{wake.pane_id}"] = now
        sent.append(wake)
    if sent:
        save_state(state_path, last)
    return sent
