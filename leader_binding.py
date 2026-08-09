"""leader_binding.py — Leader Mail 绑定持久层（B0-PREP Q1）。

按 scope（user / team / channel）维护唯一 active Leader Mail 绑定；
原子 compare-and-swap 改绑（stale version 拒绝）；previous 绑定只软退役
（state=previous），不删除；新绑定激活失败时旧 active 保持不变。

复用 coordination.py 的 SQLite 模式：WAL、busy_timeout、幂等 schema +
ALTER 前向迁移、BEGIN IMMEDIATE 事务。敏感凭据（token/密码）绝不入库。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(
    os.environ.get(
        "LEADER_BINDING_DB",
        str(Path.home() / "dashboard-data" / "leader-binding.sqlite3"),
    )
).expanduser()

SCOPE_KINDS = ("user", "team", "channel")
# binding state 不含 degraded：active 退化用 previous_state='degraded' 表达
# （previous 邮箱排空失败），active 行本身不可变 degraded——避免 partial
# unique index（WHERE state='active'）出现"active 改 degraded 后可再插第二
# active"的 footgun。
BINDING_STATES = ("active", "previous", "retired")
PREVIOUS_STATES = ("pending", "draining", "drained", "degraded")
CONNECT_RETRIES = 6
CONNECT_RETRY_BASE = 0.02
_CONNECT_INIT_LOCK = threading.Lock()
# 明文凭据绝不入库（B0 红线：拉取凭证方案单独验证，不进 binding 表）
_FORBIDDEN_FIELDS = ("token", "password", "secret", "credential")


class BindingError(ValueError):
    """绑定参数/状态非法。"""


class StaleVersionError(BindingError):
    """CAS 改绑失败：expected version 与当前 active 版本不符。"""


class BindingExistsError(BindingError):
    """该 mail_name 已在本 scope 存在其他 state 绑定，无法激活。"""


def _initialize_connection(con: sqlite3.Connection) -> None:
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS leader_bindings (
          scope_kind TEXT NOT NULL,
          scope_id TEXT NOT NULL,
          mail_name TEXT NOT NULL,
          previous_mail_name TEXT,
          previous_state TEXT,
          agent_name TEXT,
          agent_kind TEXT,
          session TEXT,
          pane_id TEXT,
          binding_version INTEGER NOT NULL,
          state TEXT NOT NULL,
          degraded_reason TEXT,
          updated_ts REAL NOT NULL,
          PRIMARY KEY(scope_kind, scope_id, mail_name),
          CHECK (state IN ('active', 'previous', 'retired')),
          CHECK (binding_version > 0)
        );
        CREATE INDEX IF NOT EXISTS leader_bindings_scope
          ON leader_bindings(scope_kind, scope_id, state);
        """
    )
    # 前向迁移：历史库缺列时补齐（幂等）
    columns = {
        row["name"]
        for row in con.execute("PRAGMA table_info(leader_bindings)").fetchall()
    }
    for name, ddl in (
        ("previous_mail_name", "TEXT"),
        ("previous_state", "TEXT"),
        ("agent_name", "TEXT"),
        ("agent_kind", "TEXT"),
        ("session", "TEXT"),
        ("pane_id", "TEXT"),
        ("degraded_reason", "TEXT"),
    ):
        if name not in columns:
            try:
                con.execute(
                    f"ALTER TABLE leader_bindings ADD COLUMN {name} {ddl}"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS leader_bindings_active_once "
        "ON leader_bindings(scope_kind, scope_id) WHERE state='active'"
    )


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    delay = CONNECT_RETRY_BASE
    for attempt in range(CONNECT_RETRIES):
        con = None
        try:
            con = sqlite3.connect(DB_PATH, timeout=5)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=5000")
            with _CONNECT_INIT_LOCK:
                _initialize_connection(con)
            return con
        except sqlite3.OperationalError as exc:
            if con is not None:
                con.close()
            if "locked" not in str(exc).lower() or attempt == CONNECT_RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("leader binding DB 连接重试耗尽")


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _validate_scope(scope_kind: str, scope_id: str) -> None:
    if scope_kind not in SCOPE_KINDS:
        raise BindingError(f"非法 scope_kind: {scope_kind!r}（允许 {SCOPE_KINDS}）")
    if not isinstance(scope_id, str) or not scope_id or len(scope_id) > 256:
        raise BindingError("scope_id 必须是非空字符串（≤256）")


def _validate_mail_name(mail_name: str) -> None:
    if not isinstance(mail_name, str) or not mail_name or len(mail_name) > 128:
        raise BindingError("mail_name 必须是非空字符串（≤128）")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in mail_name):
        raise BindingError("mail_name 含非法控制字符")


def _validate_fields(fields: dict[str, Any]) -> None:
    for key in fields:
        if any(token in key.lower() for token in _FORBIDDEN_FIELDS):
            raise BindingError(f"禁止入库敏感字段: {key}")


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def get_active_binding(
    scope_kind: str, scope_id: str,
) -> dict[str, Any] | None:
    """当前 scope 的 active 绑定（每 scope 至多一个）。"""
    _validate_scope(scope_kind, scope_id)
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE scope_kind=? AND scope_id=? AND state='active'",
            (scope_kind, scope_id),
        ).fetchone()
    finally:
        con.close()
    return _dict(row)


def get_binding(
    scope_kind: str, scope_id: str, mail_name: str,
) -> dict[str, Any] | None:
    """指定 scope+mail_name 的绑定（任意 state）。"""
    _validate_scope(scope_kind, scope_id)
    _validate_mail_name(mail_name)
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE scope_kind=? AND scope_id=? AND mail_name=?",
            (scope_kind, scope_id, mail_name),
        ).fetchone()
    finally:
        con.close()
    return _dict(row)


def list_bindings(
    scope_kind: str | None = None, scope_id: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """按条件列出绑定；state 过滤，默认含全部（含软退役 previous）。"""
    if scope_kind is not None and scope_kind not in SCOPE_KINDS:
        raise BindingError(f"非法 scope_kind: {scope_kind!r}")
    if state is not None and state not in BINDING_STATES:
        raise BindingError(f"非法 state: {state!r}（允许 {BINDING_STATES}）")
    clauses: list[str] = []
    params: list[Any] = []
    if scope_kind is not None:
        clauses.append("scope_kind=?")
        params.append(scope_kind)
    if scope_id is not None:
        clauses.append("scope_id=?")
        params.append(scope_id)
    if state is not None:
        clauses.append("state=?")
        params.append(state)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    con = _connect()
    try:
        rows = con.execute(
            f"SELECT * FROM leader_bindings {where} "
            "ORDER BY scope_kind, scope_id, updated_ts",
            params,
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 改绑（原子 CAS）
# ---------------------------------------------------------------------------

def bind_leader(
    scope_kind: str, scope_id: str, *,
    mail_name: str,
    agent_name: str | None = None,
    agent_kind: str | None = None,
    session: str | None = None,
    pane_id: str | None = None,
    expected_version: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """原子改绑：新 mail_name 成为 scope 唯一 active。

    mandatory CAS：所有 mutation 必须携带 expected_version——无 active
    首绑传 0，active 存在时必须等于当前 binding_version，否则抛
    StaleVersionError 且零变更（含同 mail_name 幂等刷新路径）。None 一律
    拒绝。同一事务内先软退役旧 active（state=previous，记录排空起点），
    再插入/复活新 active；任何约束失败都会 rollback，旧 binding 保持有效。

    返回新 active 行。previous 行只软退役，永不删除。
    """
    _validate_scope(scope_kind, scope_id)
    _validate_mail_name(mail_name)
    _validate_fields({
        "agent_name": agent_name, "agent_kind": agent_kind,
        "session": session, "pane_id": pane_id,
    })
    if expected_version is None:
        raise BindingError(
            "必须提供 expected_version（无 active 首绑传 0）"
        )
    current = time.time() if now is None else now
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        active = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE scope_kind=? AND scope_id=? AND state='active'",
            (scope_kind, scope_id),
        ).fetchone()
        # CAS 先于一切 mutation：首绑期望 0，有 active 时必须等于当前版本
        actual = int(active["binding_version"]) if active is not None else 0
        if actual != expected_version:
            raise StaleVersionError(
                f"CAS 失败：expected version {expected_version}，"
                f"当前 active version {actual}"
            )
        if active is not None and str(active["mail_name"]) == mail_name:
            # 幂等（CAS 通过后）：同一 mail_name 已是 active，仅刷新运行信息
            con.execute(
                "UPDATE leader_bindings SET agent_name=COALESCE(?,agent_name), "
                "agent_kind=COALESCE(?,agent_kind), session=COALESCE(?,session), "
                "pane_id=COALESCE(?,pane_id), updated_ts=? "
                "WHERE scope_kind=? AND scope_id=? AND mail_name=?",
                (agent_name, agent_kind, session, pane_id, current,
                 scope_kind, scope_id, mail_name),
            )
            con.commit()
            return dict(con.execute(
                "SELECT * FROM leader_bindings "
                "WHERE scope_kind=? AND scope_id=? AND mail_name=?",
                (scope_kind, scope_id, mail_name),
            ).fetchone())
        version = (int(active["binding_version"]) + 1) if active is not None else 1
        if active is not None:
            # 软退役旧 active：保留行，标记 previous 与排空起点
            con.execute(
                "UPDATE leader_bindings SET state='previous', "
                "previous_state=COALESCE(previous_state,'draining'), "
                "degraded_reason=NULL, updated_ts=? "
                "WHERE scope_kind=? AND scope_id=? AND mail_name=?",
                (current, scope_kind, scope_id, str(active["mail_name"])),
            )
        # 目标 mail_name 已有 previous/retired 行（软退役不删除，主键仍占用）：
        # 原地复活为 active，而不是 INSERT 撞主键。
        existing = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE scope_kind=? AND scope_id=? AND mail_name=?",
            (scope_kind, scope_id, mail_name),
        ).fetchone()
        previous_name = str(active["mail_name"]) if active is not None else None
        if existing is not None:
            con.execute(
                "UPDATE leader_bindings SET state='active', "
                "previous_mail_name=?, previous_state=NULL, "
                "agent_name=COALESCE(?,agent_name), "
                "agent_kind=COALESCE(?,agent_kind), "
                "session=COALESCE(?,session), pane_id=COALESCE(?,pane_id), "
                "binding_version=?, degraded_reason=NULL, updated_ts=? "
                "WHERE scope_kind=? AND scope_id=? AND mail_name=?",
                (previous_name, agent_name, agent_kind, session, pane_id,
                 version, current, scope_kind, scope_id, mail_name),
            )
        else:
            con.execute(
                "INSERT INTO leader_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scope_kind, scope_id, mail_name, previous_name,
                    None,  # 新 active 自身没有 previous_state
                    agent_name, agent_kind, session, pane_id,
                    version, "active", None, current,
                ),
            )
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    return get_active_binding(scope_kind, scope_id) or {}


def mark_previous_state(
    scope_kind: str, scope_id: str, previous_mail_name: str, *,
    state: str, reason: str | None = None, now: float | None = None,
) -> bool:
    """更新软退役 previous 绑定的排空状态（draining→drained/degraded）。"""
    _validate_scope(scope_kind, scope_id)
    _validate_mail_name(previous_mail_name)
    if state not in PREVIOUS_STATES:
        raise BindingError(
            f"非法 previous state: {state!r}（允许 {PREVIOUS_STATES}）"
        )
    current = time.time() if now is None else now
    con = _connect()
    try:
        cur = con.execute(
            "UPDATE leader_bindings SET previous_state=?, "
            "degraded_reason=?, updated_ts=? "
            "WHERE scope_kind=? AND scope_id=? AND mail_name=? "
            "AND state='previous'",
            (state, reason, current, scope_kind, scope_id, previous_mail_name),
        )
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


def retire_binding(
    scope_kind: str, scope_id: str, mail_name: str, *,
    reason: str | None = None, now: float | None = None,
) -> bool:
    """把 previous 绑定标记为 retired（排空完成后归档；不删除行）。"""
    _validate_scope(scope_kind, scope_id)
    _validate_mail_name(mail_name)
    current = time.time() if now is None else now
    con = _connect()
    try:
        cur = con.execute(
            "UPDATE leader_bindings SET state='retired', "
            "previous_state='drained', "
            "degraded_reason=COALESCE(?,degraded_reason), updated_ts=? "
            "WHERE scope_kind=? AND scope_id=? AND mail_name=? "
            "AND state='previous'",
            (reason, current, scope_kind, scope_id, mail_name),
        )
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()
