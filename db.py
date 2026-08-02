"""db.py — 只读访问 hub 的 storage.sqlite3。

WAL 模式下只读连接可安全并发,不阻塞 hub 写入。绝不写这个库。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / "mcp_agent_mail" / "storage.sqlite3"
# 复用同一连接(线程内);只读 URI,每次查询用独立游标。
_conn: sqlite3.Connection | None = None
_conn_lock = threading.RLock()


def _con() -> sqlite3.Connection:
    """打开(并缓存)一个只读 SQLite 连接。"""
    global _conn
    with _conn_lock:
        if _conn is None:
            if not DB_PATH.is_file():
                raise RuntimeError(f"找不到 hub 数据库: {DB_PATH}")
            # 同一只读连接跨 FastAPI worker 线程复用；查询由 _conn_lock 串行化。
            _conn = sqlite3.connect(
                f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False
            )
            _conn.row_factory = sqlite3.Row
        return _conn


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """执行查询,返回 dict 列表。"""
    with _conn_lock:
        return [dict(r) for r in _con().execute(sql, params).fetchall()]


def _one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = _rows(sql, params)
    return rows[0] if rows else None


def status() -> dict[str, Any]:
    """Agent Mail 是否可用；调用方据此只降级消息能力。"""
    if not DB_PATH.is_file():
        return {"available": False, "reason": f"Agent Mail 数据库不存在: {DB_PATH}"}
    try:
        _one("SELECT 1 AS ok")
    except Exception as exc:
        return {"available": False, "reason": f"Agent Mail 数据库不可读: {exc}"}
    return {"available": True, "reason": None}


# ── 全局聚合 ────────────────────────────────────────────────────

def list_projects() -> list[dict[str, Any]]:
    """所有未归档项目。"""
    return _rows(
        "SELECT id, slug, human_key, created_at "
        "FROM projects WHERE archived_at IS NULL ORDER BY created_at"
    )


def project_stats() -> list[dict[str, Any]]:
    """每个项目的 agent 数、活跃 agent 数、消息数、最近活动时间。"""
    return _rows(
        "SELECT p.id, p.slug, p.human_key, "
        "  COUNT(DISTINCT a.id) AS agent_count, "
        "  COUNT(DISTINCT CASE WHEN a.retired_at IS NULL THEN a.id END) AS active_agent_count, "
        "  (SELECT COUNT(*) FROM messages m WHERE m.project_id = p.id) AS message_count, "
        "  (SELECT MAX(m.created_ts) FROM messages m WHERE m.project_id = p.id) AS last_activity "
        "FROM projects p "
        "LEFT JOIN agents a ON a.project_id = p.id "
        "WHERE p.archived_at IS NULL "
        "GROUP BY p.id ORDER BY p.created_at"
    )


def unread_by_agent() -> list[dict[str, Any]]:
    """全局未读统计:每个 agent 的未读数(只返回有未读的)。"""
    return _rows(
        "SELECT a.id AS agent_id, a.name AS agent_name, p.slug AS project_slug, "
        "  COUNT(mr.message_id) AS unread "
        "FROM agents a "
        "JOIN projects p ON p.id = a.project_id "
        "JOIN message_recipients mr ON mr.agent_id = a.id AND mr.read_ts IS NULL "
        "WHERE a.retired_at IS NULL "
        "GROUP BY a.id HAVING unread > 0"
    )


def global_unread_count() -> int:
    """全局未读总数。"""
    r = _one(
        "SELECT COUNT(*) AS n FROM message_recipients mr "
        "JOIN agents a ON a.id = mr.agent_id "
        "WHERE mr.read_ts IS NULL AND a.retired_at IS NULL"
    )
    return r["n"] if r else 0


def unread_messages(limit: int = 50) -> list[dict[str, Any]]:
    """跨项目聚合未读消息，供 Attention Inbox 使用。"""
    return _rows(
        "SELECT m.id, p.slug AS project_slug, m.subject, m.importance, "
        "m.created_ts, sa.name AS sender_name, "
        "GROUP_CONCAT(DISTINCT ra.name) AS recipients "
        "FROM messages m "
        "JOIN projects p ON p.id = m.project_id "
        "LEFT JOIN agents sa ON sa.id = m.sender_id "
        "JOIN message_recipients mr ON mr.message_id = m.id AND mr.read_ts IS NULL "
        "JOIN agents ra ON ra.id = mr.agent_id AND ra.retired_at IS NULL "
        "WHERE p.archived_at IS NULL "
        "GROUP BY m.id, p.id ORDER BY m.created_ts DESC LIMIT ?",
        (limit,),
    )


def overview() -> dict[str, Any]:
    """全局总览:项目列表 + 统计 + 未读。"""
    stats = project_stats()
    unread = unread_by_agent()
    # 把未读数挂到对应项目上
    unread_map: dict[int, int] = {}
    for u in unread:
        # 通过 agent 反查 project 需要额外信息;用 unread_by_project 补
        pass
    proj_unread = unread_by_project()
    for s in stats:
        s["unread"] = proj_unread.get(s["id"], 0)
    return {
        "projects": stats,
        "total_unread": global_unread_count(),
        "total_projects": len(stats),
        "total_agents": sum(s["active_agent_count"] for s in stats),
    }


def unread_by_project() -> dict[int, int]:
    """每个项目的未读数,返回 {project_id: count}。"""
    rows = _rows(
        "SELECT p.id AS pid, COUNT(mr.message_id) AS n "
        "FROM projects p "
        "JOIN agents a ON a.project_id = p.id "
        "JOIN message_recipients mr ON mr.agent_id = a.id AND mr.read_ts IS NULL "
        "WHERE a.retired_at IS NULL AND p.archived_at IS NULL "
        "GROUP BY p.id"
    )
    return {r["pid"]: r["n"] for r in rows}


# ── 项目详情 ────────────────────────────────────────────────────

def project_by_slug(slug: str) -> dict[str, Any] | None:
    return _one("SELECT * FROM projects WHERE slug = ? AND archived_at IS NULL", (slug,))


def project_by_id(project_id: int) -> dict[str, Any] | None:
    return _one("SELECT * FROM projects WHERE id = ?", (project_id,))


def list_agents(project_id: int) -> list[dict[str, Any]]:
    """项目内所有活跃 agent,含未读数。"""
    return _rows(
        "SELECT a.id, a.name, a.program, a.model, a.task_description, "
        "  a.inception_ts, a.last_active_ts, a.contact_policy, "
        "  (SELECT COUNT(*) FROM message_recipients mr WHERE mr.agent_id = a.id AND mr.read_ts IS NULL) AS unread "
        "FROM agents a WHERE a.project_id = ? AND a.retired_at IS NULL "
        "ORDER BY a.name",
        (project_id,),
    )


def recent_messages(project_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """项目内最近消息,含发件人/收件人(聚合收件人名)。"""
    msgs = _rows(
        "SELECT m.id, m.thread_id, m.topic, m.subject, m.body_md, "
        "  m.importance, m.ack_required, m.created_ts, m.reply_to, "
        "  sa.name AS sender_name, sa.program AS sender_program "
        "FROM messages m "
        "LEFT JOIN agents sa ON sa.id = m.sender_id "
        "WHERE m.project_id = ? "
        "ORDER BY m.created_ts DESC LIMIT ?",
        (project_id, limit),
    )
    # 批量取收件人
    for m in msgs:
        m["recipients"] = _rows(
            "SELECT a.name, mr.kind, mr.read_ts, mr.ack_ts "
            "FROM message_recipients mr JOIN agents a ON a.id = mr.agent_id "
            "WHERE mr.message_id = ?",
            (m["id"],),
        )
    return msgs


def agent_by_name(project_id: int, name: str) -> dict[str, Any] | None:
    """按花名查 agent(含 registration_token,供写操作用)。"""
    return _one(
        "SELECT * FROM agents WHERE project_id = ? AND name = ? AND retired_at IS NULL",
        (project_id, name),
    )


_PROGRAM_ALIASES = {
    "codex": ("codex", "codex-cli"),
    "codex-cli": ("codex-cli", "codex"),
    "kimi": ("kimi", "kimi-work"),
    "kimi-work": ("kimi-work", "kimi"),
    "qoder": ("qoder", "qodercli", "qodercn", "qoder-cli", "qoder-cn"),
    "qodercli": ("qodercli", "qodercn", "qoder-cn", "qoder", "qoder-cli"),
    "qodercn": ("qodercn", "qoder-cn", "qodercli", "qoder", "qoder-cli"),
    "qoder-cli": ("qoder-cli", "qodercli", "qodercn", "qoder-cn", "qoder"),
    "qoder-cn": ("qoder-cn", "qodercn", "qodercli", "qoder", "qoder-cli"),
}


def identity_by_cwd(cwd: str, program: str) -> dict[str, Any] | None:
    """按工作目录 + program 类型查 agent-mail 身份(@ 注入协作者信息用)。

    herdr agent 的 cwd → project_key(human_key),program(codex/kimi/...)→ agent。
    program 值不统一(新注册 codex/kimi,老项目 codex-cli/kimi-work),
    只在已知别名集合中精确匹配,避免 LIKE 误命中不相关 agent。
    """
    normalized = program.strip().lower()
    candidates = _PROGRAM_ALIASES.get(normalized, (normalized,))
    placeholders = ", ".join("?" for _ in candidates)
    return _one(
        "SELECT a.name, a.program, a.model, p.human_key "
        "FROM agents a JOIN projects p ON p.id = a.project_id "
        f"WHERE p.human_key = ? AND a.program IN ({placeholders}) "
        "AND a.retired_at IS NULL "
        "ORDER BY CASE WHEN a.program = ? THEN 0 ELSE 1 END, "
        "a.inception_ts DESC LIMIT 1",
        (cwd, *candidates, normalized),
    )
