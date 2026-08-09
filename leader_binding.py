"""leader_binding.py — Leader Mail 绑定持久层（B0-PREP Q1 + R2 扩大门禁）。

按 scope（user / team / channel）维护唯一 active Leader Mail 绑定；
原子 compare-and-swap 改绑（mandatory expected_version）；previous 绑定
只软退役（state=previous），不删除；新绑定激活失败时旧 active 保持不变。

扩大门禁（#1732）：
- issuer（改绑发起者）与 registry_selector（安全 registry 引用，绝不存
  token）；route_epoch 路由纪元随切换递增
- 每次切换/退役在同库同事务写 control_events outbox（event_id 唯一，
  migration_id 标识本次迁移），fanout 按 event_id 可重放
- drain 状态机单调（pending→draining→drained；drained 不可回退；
  degraded 仅可重试 draining），以 expected_state CAS
- previous 未排空（非 drained/retired）禁止再次改绑（a→b→c 链）
- retire 前置：previous_state=drained 且 remaining/pending/claimed/
  ack_pending 全零
- 旧 schema 重复 active 与迁移失败给出可定位 fail-closed 诊断

复用 coordination.py 的 SQLite 模式：WAL、busy_timeout、幂等 schema +
ALTER 前向迁移、BEGIN IMMEDIATE 事务。敏感凭据（token/密码）绝不入库。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
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
# drain 状态机合法迁移：pending→draining→drained；degraded→draining（重试）。
# drained 为终态不可回退；draining 表示排空进行中。
PREVIOUS_STATES = ("pending", "draining", "drained", "degraded")
_DRAIN_FORWARD: dict[str, frozenset[str]] = {
    "pending": frozenset({"draining", "drained"}),
    "draining": frozenset({"drained", "degraded"}),
    "degraded": frozenset({"draining"}),
    "drained": frozenset(),  # 终态：不可回退
}
CONTROL_EVENT_TYPES = ("binding_changed", "binding_retired", "drain_state_changed")
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
          issuer TEXT,
          registry_selector TEXT,
          route_epoch INTEGER NOT NULL DEFAULT 0,
          migration_id TEXT,
          PRIMARY KEY(scope_kind, scope_id, mail_name),
          CHECK (state IN ('active', 'previous', 'retired')),
          CHECK (binding_version > 0)
        );
        CREATE INDEX IF NOT EXISTS leader_bindings_scope
          ON leader_bindings(scope_kind, scope_id, state);
        CREATE TABLE IF NOT EXISTS control_events (
          event_id TEXT PRIMARY KEY,
          scope_kind TEXT NOT NULL,
          scope_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          binding_version INTEGER NOT NULL,
          migration_id TEXT,
          payload_json TEXT NOT NULL,
          created_ts REAL NOT NULL,
          fanned_out INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS control_events_scope
          ON control_events(scope_kind, scope_id, event_id);
        CREATE INDEX IF NOT EXISTS control_events_pending
          ON control_events(fanned_out, created_ts);
        """
    )
    # 前向迁移：历史库缺列时补齐（幂等）；ALTER 失败给可定位诊断
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
        ("issuer", "TEXT"),
        ("registry_selector", "TEXT"),
        ("route_epoch", "INTEGER NOT NULL DEFAULT 0"),
        ("migration_id", "TEXT"),
    ):
        if name not in columns:
            try:
                con.execute(
                    f"ALTER TABLE leader_bindings ADD COLUMN {name} {ddl}"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise RuntimeError(
                        f"leader_bindings 前向迁移失败（列 {name}）: {exc}"
                    ) from exc
    # fail-closed 诊断：旧 schema 已存在重复 active 必须显式报错（在唯一
    # 索引重建之前拦截——否则 CREATE UNIQUE INDEX 会抛难以定位的
    # IntegrityError），绝不静默继续
    dup = con.execute(
        "SELECT scope_kind, scope_id, COUNT(*) AS n FROM leader_bindings "
        "WHERE state='active' GROUP BY scope_kind, scope_id HAVING COUNT(*) > 1"
    ).fetchall()
    if dup:
        spots = ", ".join(
            f"{r['scope_kind']}/{r['scope_id']}({r['n']})" for r in dup
        )
        raise RuntimeError(
            "leader_bindings fail-closed: 旧 schema 存在重复 active 绑定，"
            f"拒绝启动（需人工修复）: {spots}"
        )
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
    issuer: str,
    agent_name: str | None = None,
    agent_kind: str | None = None,
    session: str | None = None,
    pane_id: str | None = None,
    registry_selector: str | None = None,
    expected_version: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """原子改绑：新 mail_name 成为 scope 唯一 active。

    mandatory CAS：所有 mutation 必须携带 expected_version——无 active
    首绑传 0，active 存在时必须等于当前 binding_version，否则抛
    StaleVersionError 且零变更（含同 mail_name 幂等刷新路径）。None 一律
    拒绝。issuer 必填（改绑发起者 principal）。

    未排空禁止 a→b→c：scope 存在 previous 且 previous_state 非
    drained/retired 时拒绝再次改绑（必须先排空或显式处理 degraded）。

    同一事务内：软退役旧 active（state=previous，记录排空起点）→
    激活新行 → 写 control_events outbox（binding_changed，event_id/
    migration_id 唯一，route_epoch+1），fanout 按 event_id 可重放。
    任何约束失败都会 rollback，旧 binding 保持有效。

    返回新 active 行。previous 行只软退役，永不删除。
    """
    _validate_scope(scope_kind, scope_id)
    _validate_mail_name(mail_name)
    if not isinstance(issuer, str) or not issuer or len(issuer) > 128:
        raise BindingError("issuer 必须是非空字符串（≤128）")
    if registry_selector is not None and (
        not isinstance(registry_selector, str) or len(registry_selector) > 256
    ):
        raise BindingError("registry_selector 必须是字符串（≤256）")
    _validate_fields({
        "agent_name": agent_name, "agent_kind": agent_kind,
        "session": session, "pane_id": pane_id,
        "registry_selector": registry_selector,
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
                "pane_id=COALESCE(?,pane_id), "
                "registry_selector=COALESCE(?,registry_selector), "
                "issuer=?, updated_ts=? "
                "WHERE scope_kind=? AND scope_id=? AND mail_name=?",
                (agent_name, agent_kind, session, pane_id, registry_selector,
                 issuer, current, scope_kind, scope_id, mail_name),
            )
            con.commit()
            return dict(con.execute(
                "SELECT * FROM leader_bindings "
                "WHERE scope_kind=? AND scope_id=? AND mail_name=?",
                (scope_kind, scope_id, mail_name),
            ).fetchone())
        # 未排空禁止 a→b→c：previous 存在且未 drained/retired → 拒绝改绑
        previous = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE scope_kind=? AND scope_id=? AND state='previous'",
            (scope_kind, scope_id),
        ).fetchone()
        if previous is not None and previous["previous_state"] not in (
            "drained", None,
        ):
            raise BindingError(
                f"previous 邮箱未排空（{previous['previous_state']}），"
                "禁止再次改绑；请先完成排空或显式处理 degraded"
            )
        version = (int(active["binding_version"]) + 1) if active is not None else 1
        route_epoch = (int(active["route_epoch"]) + 1) if active is not None else 1
        migration_id = uuid.uuid4().hex
        event_id = uuid.uuid4().hex
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
                "registry_selector=COALESCE(?,registry_selector), "
                "issuer=?, binding_version=?, route_epoch=?, migration_id=?, "
                "degraded_reason=NULL, updated_ts=? "
                "WHERE scope_kind=? AND scope_id=? AND mail_name=?",
                (previous_name, agent_name, agent_kind, session, pane_id,
                 registry_selector, issuer, version, route_epoch, migration_id,
                 current, scope_kind, scope_id, mail_name),
            )
        else:
            con.execute(
                "INSERT INTO leader_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?)",
                (
                    scope_kind, scope_id, mail_name, previous_name,
                    None,  # 新 active 自身没有 previous_state
                    agent_name, agent_kind, session, pane_id,
                    version, "active", None, current,
                    issuer, registry_selector, route_epoch, migration_id,
                ),
            )
        # control-event outbox：同库同事务，fanout 按 event_id 可重放
        con.execute(
            "INSERT INTO control_events VALUES(?,?,?,?,?,?,?,?,?)",
            (
                event_id, scope_kind, scope_id, "binding_changed",
                version, migration_id,
                _event_payload({
                    "mail_name": mail_name,
                    "previous_mail_name": previous_name,
                    "issuer": issuer,
                    "route_epoch": route_epoch,
                    "registry_selector": registry_selector,
                }),
                current, 0,
            ),
        )
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    return get_active_binding(scope_kind, scope_id) or {}


def _event_payload(data: dict[str, Any]) -> str:
    import json
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _write_control_event(
    con: sqlite3.Connection, *, scope_kind: str, scope_id: str,
    event_type: str, binding_version: int, migration_id: str | None,
    payload: dict[str, Any], created_ts: float,
) -> str:
    event_id = uuid.uuid4().hex
    con.execute(
        "INSERT INTO control_events VALUES(?,?,?,?,?,?,?,?,?)",
        (
            event_id, scope_kind, scope_id, event_type, binding_version,
            migration_id, _event_payload(payload), created_ts, 0,
        ),
    )
    return event_id


def mark_previous_state(
    scope_kind: str, scope_id: str, previous_mail_name: str, *,
    state: str, reason: str | None = None,
    expected_state: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """单调更新 previous 绑定的排空状态，以 expected_state CAS。

    状态机：pending→draining→drained（终态不可回退）；degraded 仅可重试
    draining。expected_state 提供时必须是当前 previous_state，否则抛
    StaleVersionError 且零变更。变更在同一事务写 drain_state_changed
    control event（outbox，可重放）。
    """
    _validate_scope(scope_kind, scope_id)
    _validate_mail_name(previous_mail_name)
    if state not in PREVIOUS_STATES:
        raise BindingError(
            f"非法 previous state: {state!r}（允许 {PREVIOUS_STATES}）"
        )
    current = time.time() if now is None else now
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE scope_kind=? AND scope_id=? AND mail_name=? AND state='previous'",
            (scope_kind, scope_id, previous_mail_name),
        ).fetchone()
        if row is None:
            con.rollback()
            return {"updated": False, "event_id": None}
        prev_state = row["previous_state"] or "pending"
        if expected_state is not None and prev_state != expected_state:
            raise StaleVersionError(
                f"drain CAS 失败：expected state {expected_state!r}，"
                f"当前 {prev_state!r}"
            )
        if state not in _DRAIN_FORWARD.get(prev_state, frozenset()):
            raise BindingError(
                f"非法 drain 迁移: {prev_state!r} → {state!r}"
                f"（合法: {sorted(_DRAIN_FORWARD.get(prev_state, frozenset()))}）"
            )
        con.execute(
            "UPDATE leader_bindings SET previous_state=?, "
            "degraded_reason=?, updated_ts=? "
            "WHERE scope_kind=? AND scope_id=? AND mail_name=? "
            "AND state='previous'",
            (state, reason, current, scope_kind, scope_id, previous_mail_name),
        )
        event_id = _write_control_event(
            con, scope_kind=scope_kind, scope_id=scope_id,
            event_type="drain_state_changed",
            binding_version=int(row["binding_version"]),
            migration_id=row["migration_id"],
            payload={"mail_name": previous_mail_name, "previous_state": state,
                     "reason": reason},
            created_ts=current,
        )
        con.commit()
        return {"updated": True, "event_id": event_id}
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def retire_binding(
    scope_kind: str, scope_id: str, mail_name: str, *,
    remaining: int = 0, pending: int = 0, claimed: int = 0,
    ack_pending: int = 0,
    reason: str | None = None, now: float | None = None,
) -> dict[str, Any]:
    """把 previous 绑定标记为 retired（排空完成归档；不删除行）。

    前置：previous_state 必须为 drained，且 remaining/pending/claimed/
    ack_pending 全部为零（调用方提供排空证明计数），否则拒绝。成功时在
    同一事务写 binding_retired control event（outbox，可重放）。
    """
    _validate_scope(scope_kind, scope_id)
    _validate_mail_name(mail_name)
    for name, value in (
        ("remaining", remaining), ("pending", pending),
        ("claimed", claimed), ("ack_pending", ack_pending),
    ):
        if not isinstance(value, int) or value < 0:
            raise BindingError(f"{name} 必须是非负整数")
    if not (remaining == 0 and pending == 0 and claimed == 0 and ack_pending == 0):
        raise BindingError(
            f"retire 前置不满足：remaining={remaining} pending={pending} "
            f"claimed={claimed} ack_pending={ack_pending}，必须全零"
        )
    current = time.time() if now is None else now
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE scope_kind=? AND scope_id=? AND mail_name=? AND state='previous'",
            (scope_kind, scope_id, mail_name),
        ).fetchone()
        if row is None:
            con.rollback()
            return {"retired": False, "event_id": None}
        if (row["previous_state"] or "pending") != "drained":
            raise BindingError(
                f"retire 前置不满足：previous_state={row['previous_state']!r}，"
                "必须为 drained"
            )
        con.execute(
            "UPDATE leader_bindings SET state='retired', "
            "previous_state='drained', "
            "degraded_reason=COALESCE(?,degraded_reason), updated_ts=? "
            "WHERE scope_kind=? AND scope_id=? AND mail_name=? "
            "AND state='previous'",
            (reason, current, scope_kind, scope_id, mail_name),
        )
        event_id = _write_control_event(
            con, scope_kind=scope_kind, scope_id=scope_id,
            event_type="binding_retired",
            binding_version=int(row["binding_version"]),
            migration_id=row["migration_id"],
            payload={"mail_name": mail_name, "reason": reason},
            created_ts=current,
        )
        con.commit()
        return {"retired": True, "event_id": event_id}
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# control-event outbox（fanout 可重放）
# ---------------------------------------------------------------------------

def list_control_events(
    scope_kind: str | None = None, scope_id: str | None = None,
    *, limit: int = 100, after_event_id: str | None = None,
) -> list[dict[str, Any]]:
    """列出 control events（按 event_id 升序）；after_event_id 做游标续读。"""
    clauses: list[str] = []
    params: list[Any] = []
    if scope_kind is not None:
        clauses.append("scope_kind=?")
        params.append(scope_kind)
    if scope_id is not None:
        clauses.append("scope_id=?")
        params.append(scope_id)
    if after_event_id is not None:
        clauses.append("event_id > ?")
        params.append(after_event_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 1000)))
    con = _connect()
    try:
        rows = con.execute(
            f"SELECT * FROM control_events {where} "
            "ORDER BY created_ts, event_id LIMIT ?",
            params,
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def undelivered_control_events(limit: int = 100) -> list[dict[str, Any]]:
    """未 fanout 的 control events（fanout 可重放：按 event_id 幂等消费）。"""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM control_events WHERE fanned_out=0 "
            "ORDER BY created_ts, event_id LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def mark_event_fanned_out(event_id: str) -> bool:
    """标记事件已 fanout；重复标记幂等（已 fanout 返回 False 不报错）。"""
    con = _connect()
    try:
        cur = con.execute(
            "UPDATE control_events SET fanned_out=1 "
            "WHERE event_id=? AND fanned_out=0",
            (event_id,),
        )
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()
