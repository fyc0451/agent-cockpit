"""leader_binding.py — Leader Mail 绑定持久层（B0-PREP Q1 + R2/R3 门禁）。

按 (issuer, scope) 维护唯一 active Leader Mail 绑定；原子 CAS 改绑
（mandatory expected_version）；previous 只软退役不删除；新绑定激活失败
旧 active 保持不变。

R3 门禁（#1743/#1740）：
- issuer（稳定 trust-domain principal）进入全部 PK/unique/query/CAS/
  outbox scope；同名调用不得改写 issuer；旧表有行且无法推断 issuer 时
  fail-closed 并给显式迁移诊断，不猜默认
- 独立 binding_migrations 不可变记录（from/to binding id、migration_id、
  route_epoch）；previous 行不保留误导的旧激活 migration id（软退役时
  改为关联本次迁移）
- drain mutation 强制 expected binding_version+migration_id+state CAS，
  任何缺省拒绝，stale 零变更；drain remaining/pending/claimed/ack_pending
  持久化并以同 CAS 更新；retire 从 DB 读证明，无调用者直传旁路
- 同 mail_name 幂等要求规范化 payload 完全一致；session/pane/selector/
  actor 任一变化必须 version+1 并同事务写 outbox 与 migration
- control_events 单调 seq（AUTOINCREMENT）排序/分页，event_id 仅幂等标识
- 凭证只存 registry selector，绝不存 token

复用 coordination.py 的 SQLite 模式：WAL、busy_timeout、幂等 schema +
ALTER 前向迁移、BEGIN IMMEDIATE 事务。
"""
from __future__ import annotations

import json
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
BINDING_STATES = ("active", "previous", "retired")
# drain 状态机合法迁移：pending→draining→drained；degraded→draining（重试）。
# drained 为终态不可回退。
PREVIOUS_STATES = ("pending", "draining", "drained", "degraded")
# self-loop 合法（同状态保持 + 计数/原因更新）；drained 仍不可回退到其他状态
_DRAIN_FORWARD: dict[str, frozenset[str]] = {
    "pending": frozenset({"pending", "draining", "drained"}),
    "draining": frozenset({"draining", "drained", "degraded"}),
    "degraded": frozenset({"degraded", "draining"}),
    "drained": frozenset({"drained"}),  # 终态：仅幂等确认，不可回退
}
CONTROL_EVENT_TYPES = ("binding_changed", "binding_updated", "binding_retired", "drain_state_changed")
CONNECT_RETRIES = 6
CONNECT_RETRY_BASE = 0.02
_CONNECT_INIT_LOCK = threading.Lock()
# 明文凭据绝不入库（B0 红线：拉取凭证方案单独验证，不进 binding 表）
_FORBIDDEN_FIELDS = ("token", "password", "secret", "credential")

# 幂等比较的规范化 payload 键（任一变化即视为变更，必须 version+1）
_RUNTIME_FIELDS = (
    "agent_name", "agent_kind", "session", "pane_id", "registry_selector",
)


class BindingError(ValueError):
    """绑定参数/状态非法。"""


class StaleVersionError(BindingError):
    """CAS 失败：expected version/state/migration 与当前不符。"""


_LEADER_BINDINGS_COLUMNS = (
    "issuer", "scope_kind", "scope_id", "mail_name", "binding_id",
    "previous_mail_name", "previous_state", "agent_name", "agent_kind",
    "session", "pane_id", "registry_selector", "binding_version", "state",
    "degraded_reason", "updated_ts", "route_epoch", "migration_id",
    "drain_revision",
    "drain_remaining", "drain_pending", "drain_claimed", "drain_ack_pending",
)
_LEADER_BINDINGS_DDL = """
CREATE TABLE IF NOT EXISTS leader_bindings (
  issuer TEXT NOT NULL,
  scope_kind TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  mail_name TEXT NOT NULL,
  binding_id TEXT NOT NULL,
  previous_mail_name TEXT,
  previous_state TEXT,
  agent_name TEXT,
  agent_kind TEXT,
  session TEXT,
  pane_id TEXT,
  registry_selector TEXT,
  binding_version INTEGER NOT NULL,
  state TEXT NOT NULL,
  degraded_reason TEXT,
  updated_ts REAL NOT NULL,
  route_epoch INTEGER NOT NULL DEFAULT 0,
  migration_id TEXT,
  drain_revision INTEGER NOT NULL DEFAULT 0,
  drain_remaining INTEGER NOT NULL DEFAULT 0,
  drain_pending INTEGER NOT NULL DEFAULT 0,
  drain_claimed INTEGER NOT NULL DEFAULT 0,
  drain_ack_pending INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(issuer, scope_kind, scope_id, mail_name),
  CHECK (state IN ('active', 'previous', 'retired')),
  CHECK (binding_version > 0)
)
"""
_BINDING_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS binding_migrations (
  migration_id TEXT PRIMARY KEY,
  issuer TEXT NOT NULL,
  scope_kind TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  from_binding_id TEXT,
  to_binding_id TEXT,
  route_epoch INTEGER NOT NULL,
  created_ts REAL NOT NULL
)
"""
_CONTROL_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS control_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  issuer TEXT NOT NULL,
  scope_kind TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  binding_version INTEGER NOT NULL,
  migration_id TEXT,
  payload_json TEXT NOT NULL,
  created_ts REAL NOT NULL,
  fanned_out INTEGER NOT NULL DEFAULT 0
)
"""


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}


def _pk_columns(con: sqlite3.Connection, table: str) -> list[str]:
    rows = [
        (int(r["pk"]), r["name"])
        for r in con.execute(f"PRAGMA table_info({table})")
        if int(r["pk"]) > 0
    ]
    rows.sort()
    return [name for _, name in rows]


def _rebuild_leader_bindings(con: sqlite3.Connection) -> None:
    """旧 schema（PK 不含 issuer）事务重建为新 schema（真单事务，#1860-1）。

    rename 前预检（NULL issuer/binding_id、重复 active）；BEGIN 后用 con.execute
    逐条（executescript 会隐式 COMMIT 破坏事务）；任一失败 rollback 后原表名/
    sqlite_master/index/行不变。空旧库→可 bind；有行旧库 issuer/binding_id 不可
    无歧义映射→fail-closed 原库不变。
    """
    old_cols = _table_columns(con, "leader_bindings")
    n_rows = int(con.execute("SELECT COUNT(*) FROM leader_bindings").fetchone()[0])
    if n_rows > 0:
        if "issuer" not in old_cols:
            raise RuntimeError(
                "leader_bindings fail-closed: 旧 schema 有行但缺 issuer 列，无法无歧义映射；原库不变，需人工迁移"
            )
        if int(con.execute(
            "SELECT COUNT(*) FROM leader_bindings WHERE issuer IS NULL OR issuer=''"
        ).fetchone()[0]):
            raise RuntimeError(
                "leader_bindings fail-closed: 存在 issuer 为空的行，无法迁移；原库不变"
            )
        if "binding_id" not in old_cols:
            raise RuntimeError(
                "leader_bindings fail-closed: 旧 schema 有行但缺 binding_id，无法无歧义映射；原库不变，需人工迁移"
            )
        if int(con.execute(
            "SELECT COUNT(*) FROM leader_bindings WHERE binding_id IS NULL OR binding_id=''"
        ).fetchone()[0]):
            raise RuntimeError(
                "leader_bindings fail-closed: 存在 binding_id 为空的行，无法迁移；原库不变"
            )
    # 重复 active 预检（rename 前，避免 rename 后唯一索引撞冲突）
    if "state" in old_cols and "issuer" in old_cols:
        dup = con.execute(
            "SELECT issuer, scope_kind, scope_id, COUNT(*) AS n FROM leader_bindings "
            "WHERE state='active' GROUP BY issuer, scope_kind, scope_id HAVING COUNT(*) > 1"
        ).fetchall()
        if dup:
            spots = ", ".join(
                f"{r['issuer']}/{r['scope_kind']}/{r['scope_id']}({r['n']})" for r in dup
            )
            raise RuntimeError(
                f"leader_bindings fail-closed: 重复 active，原库不变: {spots}"
            )
    # 构建 SELECT 列映射：旧表有则取，无则用字面量（仅空表会到此处拷贝 0 行）
    select_cols: list[str] = []
    for col in _LEADER_BINDINGS_COLUMNS:
        if col in old_cols:
            select_cols.append(col)
        elif col in (
            "drain_revision", "drain_remaining", "drain_pending",
            "drain_claimed", "drain_ack_pending", "route_epoch",
        ):
            select_cols.append("0")
        elif col == "binding_id":
            select_cols.append("lower(hex(randomblob(16)))")
        else:
            select_cols.append("NULL")
    con.execute("BEGIN")
    try:
        con.execute("ALTER TABLE leader_bindings RENAME TO _leader_bindings_old")
        con.execute(_LEADER_BINDINGS_DDL)  # con.execute（非 executescript）保持事务
        con.execute(
            f"INSERT INTO leader_bindings ({', '.join(_LEADER_BINDINGS_COLUMNS)}) "
            f"SELECT {', '.join(select_cols)} FROM _leader_bindings_old"
        )
        con.execute("DROP TABLE _leader_bindings_old")
        con.execute(
            "CREATE UNIQUE INDEX leader_bindings_active_once "
            "ON leader_bindings(issuer, scope_kind, scope_id) WHERE state='active'"
        )
        con.execute(
            "CREATE INDEX leader_bindings_scope "
            "ON leader_bindings(issuer, scope_kind, scope_id, state)"
        )
        con.commit()
    except BaseException:
        con.rollback()
        raise


def _rebuild_control_events(con: sqlite3.Connection) -> None:
    """旧 control_events 事务重建为 seq-AUTOINCREMENT schema（真单事务，#1860）。

    seq 按旧稳定 ORDER BY created_ts, event_id 确定性分配（覆盖乱序/同 timestamp）；
    rename 前预检 event_id 唯一 + NULL issuer fail-closed；BEGIN 后 con.execute 逐条。
    """
    old_cols = _table_columns(con, "control_events")
    n_rows = int(con.execute("SELECT COUNT(*) FROM control_events").fetchone()[0])
    if n_rows > 0:
        if "issuer" not in old_cols:
            raise RuntimeError(
                "control_events fail-closed: 旧 schema 有行但缺 issuer，无法迁移；原库不变，需人工迁移"
            )
        if "event_id" in old_cols:
            dup_eid = int(con.execute(
                "SELECT COUNT(*) FROM (SELECT event_id FROM control_events "
                "GROUP BY event_id HAVING COUNT(*) > 1)"
            ).fetchone()[0])
            if dup_eid:
                raise RuntimeError(
                    f"control_events fail-closed: {dup_eid} 个重复 event_id，无法迁移；原库不变"
                )
    new_cols = (
        "event_id", "issuer", "scope_kind", "scope_id", "event_type",
        "binding_version", "migration_id", "payload_json", "created_ts", "fanned_out",
    )
    select_cols = [
        c if c in old_cols
        else ("''" if c == "issuer" else ("0" if c == "fanned_out" else "NULL"))
        for c in new_cols
    ]
    # seq 按旧稳定 created_ts, event_id 顺序确定性分配
    order_by = ""
    if "created_ts" in old_cols and "event_id" in old_cols:
        order_by = " ORDER BY created_ts, event_id"
    elif "event_id" in old_cols:
        order_by = " ORDER BY event_id"
    con.execute("BEGIN")
    try:
        con.execute("ALTER TABLE control_events RENAME TO _control_events_old")
        con.execute(_CONTROL_EVENTS_DDL)  # con.execute 保持事务
        con.execute(
            f"INSERT INTO control_events ({', '.join(new_cols)}) "
            f"SELECT {', '.join(select_cols)} FROM _control_events_old{order_by}"
        )
        con.execute("DROP TABLE _control_events_old")
        con.execute(
            "CREATE INDEX control_events_scope "
            "ON control_events(issuer, scope_kind, scope_id, seq)"
        )
        con.execute(
            "CREATE INDEX control_events_pending ON control_events(fanned_out, seq)"
        )
        con.commit()
    except BaseException:
        con.rollback()
        raise


def _initialize_connection(con: sqlite3.Connection) -> None:
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        con.execute("PRAGMA journal_mode=WAL")
    lb_exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='leader_bindings'"
    ).fetchone()
    if not lb_exists:
        # 全新 leader_bindings + binding_migrations；control_events 由下方块统一处理
        con.executescript(_LEADER_BINDINGS_DDL + ";\n" + _BINDING_MIGRATIONS_DDL)
    # leader_bindings 已存在：判断是否需要重建（旧 PK 不含 issuer）
    lb_pk = _pk_columns(con, "leader_bindings")
    if lb_pk != ["issuer", "scope_kind", "scope_id", "mail_name"]:
        _rebuild_leader_bindings(con)
    else:
        # R3→R4：仅缺 drain_revision 时 ALTER 补列（PK 已正确）
        lb_cols = _table_columns(con, "leader_bindings")
        if "drain_revision" not in lb_cols:
            con.execute(
                "ALTER TABLE leader_bindings ADD COLUMN drain_revision "
                "INTEGER NOT NULL DEFAULT 0"
            )
    # control_events：旧 schema（无 seq/issuer，event_id 为 PK）需重建
    ce_exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='control_events'"
    ).fetchone()
    if ce_exists:
        ce_cols = _table_columns(con, "control_events")
        if "seq" not in ce_cols or "issuer" not in ce_cols:
            _rebuild_control_events(con)
    else:
        con.executescript(_CONTROL_EVENTS_DDL)
        con.execute(
            "CREATE INDEX control_events_scope "
            "ON control_events(issuer, scope_kind, scope_id, seq)"
        )
        con.execute(
            "CREATE INDEX control_events_pending ON control_events(fanned_out, seq)"
        )
    # binding_migrations（幂等创建）
    con.executescript(_BINDING_MIGRATIONS_DDL)
    # 索引幂等保证（重建路径已建，此处理论 IF NOT EXISTS 跳过）
    con.execute(
        "CREATE INDEX IF NOT EXISTS leader_bindings_scope "
        "ON leader_bindings(issuer, scope_kind, scope_id, state)"
    )
    # fail-closed：有行但 issuer/binding_id 空 → 干净诊断（重建后新库不应触发）
    rows = con.execute("SELECT COUNT(*) AS n FROM leader_bindings").fetchone()
    if rows and int(rows["n"]) > 0:
        null_issuer = con.execute(
            "SELECT COUNT(*) AS n FROM leader_bindings WHERE issuer IS NULL OR issuer=''"
        ).fetchone()
        if null_issuer and int(null_issuer["n"]) > 0:
            raise RuntimeError(
                f"leader_bindings fail-closed: {null_issuer['n']} 行 issuer 为空，无法迁移"
            )
        null_bid = con.execute(
            "SELECT COUNT(*) AS n FROM leader_bindings WHERE binding_id IS NULL OR binding_id=''"
        ).fetchone()
        if null_bid and int(null_bid["n"]) > 0:
            raise RuntimeError(
                f"leader_bindings fail-closed: {null_bid['n']} 行 binding_id 为空，需人工补全"
            )
    # 重复 active 诊断
    dup = con.execute(
        "SELECT issuer, scope_kind, scope_id, COUNT(*) AS n FROM leader_bindings "
        "WHERE state='active' GROUP BY issuer, scope_kind, scope_id HAVING COUNT(*) > 1"
    ).fetchall()
    if dup:
        spots = ", ".join(
            f"{r['issuer']}/{r['scope_kind']}/{r['scope_id']}({r['n']})" for r in dup
        )
        raise RuntimeError(
            f"leader_bindings fail-closed: 重复 active 绑定，拒绝启动: {spots}"
        )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS leader_bindings_active_once "
        "ON leader_bindings(issuer, scope_kind, scope_id) WHERE state='active'"
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
        except BaseException:
            # #1860: 非 OperationalError 初始化失败也 close，不泄漏连接
            if con is not None:
                con.close()
            raise
    raise RuntimeError("leader binding DB 连接重试耗尽")


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _validate_issuer(issuer: str) -> None:
    if not isinstance(issuer, str) or not issuer or len(issuer) > 128:
        raise BindingError("issuer 必须是非空字符串（≤128）")


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


def _normalized_payload(row: sqlite3.Row | dict[str, Any]) -> tuple[Any, ...]:
    """规范化运行时 payload（幂等比较用）。"""
    data = dict(row) if isinstance(row, sqlite3.Row) else row
    return tuple(data.get(f) for f in _RUNTIME_FIELDS)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def get_active_binding(
    issuer: str, scope_kind: str, scope_id: str,
) -> dict[str, Any] | None:
    """当前 (issuer, scope) 的 active 绑定（每 issuer+scope 至多一个）。"""
    _validate_issuer(issuer)
    _validate_scope(scope_kind, scope_id)
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE issuer=? AND scope_kind=? AND scope_id=? AND state='active'",
            (issuer, scope_kind, scope_id),
        ).fetchone()
    finally:
        con.close()
    return _dict(row)


def get_binding(
    issuer: str, scope_kind: str, scope_id: str, mail_name: str,
) -> dict[str, Any] | None:
    """指定 (issuer, scope, mail_name) 的绑定（任意 state）。"""
    _validate_issuer(issuer)
    _validate_scope(scope_kind, scope_id)
    _validate_mail_name(mail_name)
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=?",
            (issuer, scope_kind, scope_id, mail_name),
        ).fetchone()
    finally:
        con.close()
    return _dict(row)


def list_bindings(
    issuer: str | None = None, scope_kind: str | None = None,
    scope_id: str | None = None, state: str | None = None,
) -> list[dict[str, Any]]:
    """按条件列出绑定；issuer/state 过滤，默认含全部。"""
    if issuer is not None:
        _validate_issuer(issuer)
    if scope_kind is not None and scope_kind not in SCOPE_KINDS:
        raise BindingError(f"非法 scope_kind: {scope_kind!r}")
    if state is not None and state not in BINDING_STATES:
        raise BindingError(f"非法 state: {state!r}（允许 {BINDING_STATES}）")
    clauses: list[str] = []
    params: list[Any] = []
    if issuer is not None:
        clauses.append("issuer=?")
        params.append(issuer)
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
            "ORDER BY issuer, scope_kind, scope_id, updated_ts",
            params,
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def get_migration(migration_id: str) -> dict[str, Any] | None:
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM binding_migrations WHERE migration_id=?",
            (migration_id,),
        ).fetchone()
    finally:
        con.close()
    return _dict(row)


# ---------------------------------------------------------------------------
# 改绑（原子 CAS）
# ---------------------------------------------------------------------------

def bind_leader(
    issuer: str, scope_kind: str, scope_id: str, *,
    mail_name: str,
    agent_name: str | None = None,
    agent_kind: str | None = None,
    session: str | None = None,
    pane_id: str | None = None,
    registry_selector: str | None = None,
    expected_version: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """原子改绑：(issuer, scope) 内新 mail_name 成为唯一 active。

    mandatory CAS：expected_version 必填（无 active 首绑传 0）。issuer 是
    稳定 trust-domain principal：同名调用不得改写 issuer；所有 PK/unique/
    query/CAS/outbox 都以 issuer 参与 scope。

    幂等规则：同 mail_name 且规范化运行时 payload（agent_name/agent_kind/
    session/pane_id/registry_selector）完全一致 → no-op（版本不变、无
    outbox/migration）；任一变化 → version+1 并同事务写 migration 与
    binding_changed outbox。

    未排空禁止 a→b→c：scope 存在 previous 且非 drained 时拒绝再次改绑。
    切换/变更写 binding_migrations 记录；previous 行 migration_id 更新为
    本次迁移（不保留误导的旧激活 migration id）。任何约束失败 rollback，
    旧 binding 保持有效。
    """
    _validate_issuer(issuer)
    _validate_scope(scope_kind, scope_id)
    _validate_mail_name(mail_name)
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
            "WHERE issuer=? AND scope_kind=? AND scope_id=? AND state='active'",
            (issuer, scope_kind, scope_id),
        ).fetchone()
        actual = int(active["binding_version"]) if active is not None else 0
        if actual != expected_version:
            raise StaleVersionError(
                f"CAS 失败：expected version {expected_version}，"
                f"当前 active version {actual}"
            )
        incoming = (agent_name, agent_kind, session, pane_id, registry_selector)
        if active is not None and str(active["mail_name"]) == mail_name:
            # 同 mail_name：规范化 payload 完全一致才是真幂等（no-op）
            if _normalized_payload(active) == incoming:
                con.rollback()
                return dict(active)
            # 路由载荷变化（同 mail_name）：version+1、route_epoch+1，发
            # binding_updated；不造 from=to migration、不产生 previous（ADR §3）
            version = int(active["binding_version"]) + 1
            route_epoch = int(active["route_epoch"]) + 1
            event_id = uuid.uuid4().hex
            con.execute(
                "UPDATE leader_bindings SET agent_name=?, agent_kind=?, "
                "session=?, pane_id=?, registry_selector=?, "
                "binding_version=?, route_epoch=?, updated_ts=? "
                "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=?",
                (agent_name, agent_kind, session, pane_id, registry_selector,
                 version, route_epoch, current,
                 issuer, scope_kind, scope_id, mail_name),
            )
            con.execute(
                "INSERT INTO control_events "
                "(event_id, issuer, scope_kind, scope_id, event_type, "
                "binding_version, migration_id, payload_json, created_ts, "
                "fanned_out) VALUES(?,?,?,?,?,?,NULL,?,?,0)",
                (event_id, issuer, scope_kind, scope_id, "binding_updated",
                 version,
                 _event_payload({
                     "mail_name": mail_name, "issuer": issuer,
                     "route_epoch": route_epoch,
                     "registry_selector": registry_selector,
                 }),
                 current),
            )
            con.commit()
            return dict(con.execute(
                "SELECT * FROM leader_bindings "
                "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=?",
                (issuer, scope_kind, scope_id, mail_name),
            ).fetchone())
        # 未排空禁止 a→b→c
        previous = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE issuer=? AND scope_kind=? AND scope_id=? AND state='previous'",
            (issuer, scope_kind, scope_id),
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
        old_binding_id = str(active["binding_id"]) if active is not None else None
        if active is not None:
            # 软退役旧 active：previous 行 migration_id 关联本次迁移
            # （不保留误导的旧激活 migration id），排空计数归零起点
            con.execute(
                "UPDATE leader_bindings SET state='previous', "
                "previous_state=COALESCE(previous_state,'draining'), "
                "degraded_reason=NULL, migration_id=?, updated_ts=? "
                "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=?",
                (migration_id, current, issuer, scope_kind, scope_id,
                 str(active["mail_name"])),
            )
        existing = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=?",
            (issuer, scope_kind, scope_id, mail_name),
        ).fetchone()
        previous_name = str(active["mail_name"]) if active is not None else None
        if existing is not None:
            # 原地复活：binding_id 保持
            new_binding_id = str(existing["binding_id"])
            con.execute(
                "UPDATE leader_bindings SET state='active', "
                "previous_mail_name=?, previous_state=NULL, "
                "agent_name=?, agent_kind=?, session=?, pane_id=?, "
                "registry_selector=?, binding_version=?, route_epoch=?, "
                "migration_id=?, degraded_reason=NULL, updated_ts=?, "
                "drain_revision=0, "
                "drain_remaining=0, drain_pending=0, drain_claimed=0, "
                "drain_ack_pending=0 "
                "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=?",
                (previous_name, agent_name, agent_kind, session, pane_id,
                 registry_selector, version, route_epoch, migration_id,
                 current, issuer, scope_kind, scope_id, mail_name),
            )
        else:
            new_binding_id = uuid.uuid4().hex
            con.execute(
                f"INSERT INTO leader_bindings ({', '.join(_LEADER_BINDINGS_COLUMNS)}) "
                f"VALUES({','.join(['?'] * 18 + ['0'] * 5)})",
                (
                    issuer, scope_kind, scope_id, mail_name, new_binding_id,
                    previous_name,
                    None,  # 新 active 自身没有 previous_state
                    agent_name, agent_kind, session, pane_id,
                    registry_selector,
                    version, "active", None, current,
                    route_epoch, migration_id,
                ),
            )
        con.execute(
            "INSERT INTO binding_migrations (migration_id, issuer, scope_kind, "
            "scope_id, from_binding_id, to_binding_id, route_epoch, created_ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (migration_id, issuer, scope_kind, scope_id,
             old_binding_id, new_binding_id, route_epoch, current),
        )
        con.execute(
            "INSERT INTO control_events "
            "(event_id, issuer, scope_kind, scope_id, event_type, "
            "binding_version, migration_id, payload_json, created_ts, "
            "fanned_out) VALUES(?,?,?,?,?,?,?,?,?,0)",
            (event_id, issuer, scope_kind, scope_id, "binding_changed",
             version, migration_id,
             _event_payload({
                 "mail_name": mail_name,
                 "previous_mail_name": previous_name,
                 "issuer": issuer,
                 "route_epoch": route_epoch,
                 "registry_selector": registry_selector,
             }),
             current),
        )
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    return get_active_binding(issuer, scope_kind, scope_id) or {}


def _event_payload(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _write_control_event(
    con: sqlite3.Connection, *, issuer: str, scope_kind: str, scope_id: str,
    event_type: str, binding_version: int, migration_id: str | None,
    payload: dict[str, Any], created_ts: float,
) -> str:
    event_id = uuid.uuid4().hex
    con.execute(
        "INSERT INTO control_events "
        "(event_id, issuer, scope_kind, scope_id, event_type, "
        "binding_version, migration_id, payload_json, created_ts, "
        "fanned_out) VALUES(?,?,?,?,?,?,?,?,?,0)",
        (
            event_id, issuer, scope_kind, scope_id, event_type,
            binding_version, migration_id, _event_payload(payload),
            created_ts,
        ),
    )
    return event_id


def mark_previous_state(
    issuer: str, scope_kind: str, scope_id: str, previous_mail_name: str, *,
    state: str,
    expected_binding_version: int | None = None,
    expected_migration_id: str | None = None,
    expected_state: str | None = None,
    expected_drain_revision: int | None = None,
    remaining: int | None = None,
    pending: int | None = None,
    claimed: int | None = None,
    ack_pending: int | None = None,
    reason: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """单调更新 previous 排空状态；强制 binding version+migration id+state
    CAS（任何缺省拒绝，stale 零变更）。drain 计数在同类 CAS 下持久化更新。

    状态机：pending→draining→drained（终态）；degraded 仅可重试 draining。
    变更同一事务写 drain_state_changed control event。
    """
    _validate_issuer(issuer)
    _validate_scope(scope_kind, scope_id)
    _validate_mail_name(previous_mail_name)
    if state not in PREVIOUS_STATES:
        raise BindingError(
            f"非法 previous state: {state!r}（允许 {PREVIOUS_STATES}）"
        )
    # 强制 CAS 参数：任何缺省拒绝
    for name, value in (
        ("expected_binding_version", expected_binding_version),
        ("expected_migration_id", expected_migration_id),
        ("expected_state", expected_state),
        ("expected_drain_revision", expected_drain_revision),
    ):
        if value is None:
            raise BindingError(f"必须提供 {name}（drain CAS 强制）")
    counters: dict[str, int] = {}
    for name, value in (
        ("remaining", remaining), ("pending", pending),
        ("claimed", claimed), ("ack_pending", ack_pending),
    ):
        if value is not None and (not isinstance(value, int) or value < 0):
            raise BindingError(f"{name} 必须是非负整数")
        if value is not None:
            counters[name] = value
    current = time.time() if now is None else now
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=? "
            "AND state='previous'",
            (issuer, scope_kind, scope_id, previous_mail_name),
        ).fetchone()
        if row is None:
            con.rollback()
            return {"updated": False, "event_id": None}
        # 强制 CAS：version+migration+state+drain_revision 全匹配
        prev_state = row["previous_state"] or "pending"
        cur_drain_rev = int(row["drain_revision"])
        if (
            int(row["binding_version"]) != expected_binding_version
            or (row["migration_id"] or "") != expected_migration_id
            or prev_state != expected_state
            or cur_drain_rev != expected_drain_revision
        ):
            raise StaleVersionError(
                "drain CAS 失败：expected "
                f"version={expected_binding_version} "
                f"migration={expected_migration_id!r} "
                f"state={expected_state!r} "
                f"drain_revision={expected_drain_revision}，当前 "
                f"version={row['binding_version']} "
                f"migration={row['migration_id']!r} "
                f"state={prev_state!r} drain_revision={cur_drain_rev}"
            )
        if state not in _DRAIN_FORWARD.get(prev_state, frozenset()):
            raise BindingError(
                f"非法 drain 迁移: {prev_state!r} → {state!r}"
                f"（合法: {sorted(_DRAIN_FORWARD.get(prev_state, frozenset()))}）"
            )
        set_clause = (
            "previous_state=?, degraded_reason=?, updated_ts=?, "
            "drain_revision=drain_revision+1"
        )
        params: list[Any] = [state, reason, current]
        for name in ("remaining", "pending", "claimed", "ack_pending"):
            if name in counters:
                set_clause += f", drain_{name}=?"
                params.append(counters[name])
        # 原子 CAS：UPDATE WHERE 含 version+migration+state+drain_revision，
        # rowcount 检查使并发 self-loop 只一方成功（另一方 drain_revision 已变）
        params += [
            issuer, scope_kind, scope_id, previous_mail_name,
            expected_binding_version, expected_migration_id, expected_state,
            expected_drain_revision,
        ]
        cur = con.execute(
            f"UPDATE leader_bindings SET {set_clause} "
            "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=? "
            "AND state='previous' AND binding_version=? "
            "AND COALESCE(migration_id,'')=? "
            "AND COALESCE(previous_state,'pending')=? "
            "AND drain_revision=?",
            params,
        )
        if cur.rowcount != 1:
            raise StaleVersionError(
                "drain CAS 失败：并发竞态，行已被另一 worker 改动（零变更）"
            )
        event_id = _write_control_event(
            con, issuer=issuer, scope_kind=scope_kind, scope_id=scope_id,
            event_type="drain_state_changed",
            binding_version=int(row["binding_version"]),
            migration_id=row["migration_id"],
            payload={"mail_name": previous_mail_name, "previous_state": state,
                     "reason": reason, "counters": counters},
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
    issuer: str, scope_kind: str, scope_id: str, mail_name: str, *,
    expected_binding_version: int | None = None,
    expected_migration_id: str | None = None,
    expected_state: str | None = None,
    expected_drain_revision: int | None = None,
    reason: str | None = None, now: float | None = None,
) -> dict[str, Any]:
    """把 previous 绑定标记 retired。强制 binding version+migration id+state+
    drain_revision CAS（任何缺省拒绝），UPDATE WHERE 原子 CAS + rowcount，
    跨 a→b→a→b 旧轮 worker（持旧 migration context）零变更。前置从 DB 读证明：
    previous_state=drained 且 drain_* 全零。成功同一事务写 binding_retired event。"""
    _validate_issuer(issuer)
    _validate_scope(scope_kind, scope_id)
    _validate_mail_name(mail_name)
    for name, value in (
        ("expected_binding_version", expected_binding_version),
        ("expected_migration_id", expected_migration_id),
        ("expected_state", expected_state),
        ("expected_drain_revision", expected_drain_revision),
    ):
        if value is None:
            raise BindingError(f"必须提供 {name}（retire CAS 强制）")
    current = time.time() if now is None else now
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM leader_bindings "
            "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=? "
            "AND state='previous'",
            (issuer, scope_kind, scope_id, mail_name),
        ).fetchone()
        if row is None:
            con.rollback()
            return {"retired": False, "event_id": None}
        if (row["previous_state"] or "pending") != "drained":
            raise BindingError(
                f"retire 前置不满足：previous_state={row['previous_state']!r}，"
                "必须为 drained"
            )
        counts = {
            "remaining": int(row["drain_remaining"]),
            "pending": int(row["drain_pending"]),
            "claimed": int(row["drain_claimed"]),
            "ack_pending": int(row["drain_ack_pending"]),
        }
        if any(counts.values()):
            raise BindingError(
                "retire 前置不满足（DB 证明）："
                + " ".join(f"{k}={v}" for k, v in counts.items())
                + "，必须全零"
            )
        cur = con.execute(
            "UPDATE leader_bindings SET state='retired', "
            "previous_state='drained', "
            "degraded_reason=COALESCE(?,degraded_reason), updated_ts=? "
            "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=? "
            "AND state='previous' AND binding_version=? "
            "AND COALESCE(migration_id,'')=? "
            "AND COALESCE(previous_state,'pending')=? "
            "AND drain_revision=?",
            (reason, current, issuer, scope_kind, scope_id, mail_name,
             expected_binding_version, expected_migration_id,
             expected_state, expected_drain_revision),
        )
        if cur.rowcount != 1:
            raise StaleVersionError(
                "retire CAS 失败：行已被另一迁移/worker 改动（零变更）"
            )
        event_id = _write_control_event(
            con, issuer=issuer, scope_kind=scope_kind, scope_id=scope_id,
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
# control-event outbox（单调 seq；fanout 可重放）
# ---------------------------------------------------------------------------

def list_control_events(
    issuer: str | None = None, scope_kind: str | None = None,
    scope_id: str | None = None,
    *, limit: int = 100, after_seq: int | None = None,
) -> list[dict[str, Any]]:
    """列出 control events（按单调 seq 升序）；after_seq 做游标续读。"""
    clauses: list[str] = []
    params: list[Any] = []
    if issuer is not None:
        _validate_issuer(issuer)
        clauses.append("issuer=?")
        params.append(issuer)
    if scope_kind is not None:
        clauses.append("scope_kind=?")
        params.append(scope_kind)
    if scope_id is not None:
        clauses.append("scope_id=?")
        params.append(scope_id)
    if after_seq is not None:
        clauses.append("seq > ?")
        params.append(int(after_seq))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 1000)))
    con = _connect()
    try:
        rows = con.execute(
            f"SELECT * FROM control_events {where} "
            "ORDER BY seq LIMIT ?",
            params,
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def undelivered_control_events(
    issuer: str, *, limit: int = 100,
) -> list[dict[str, Any]]:
    """该 issuer 未 fanout 的 control events（按 seq 升序；issuer 隔离）。"""
    _validate_issuer(issuer)
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM control_events WHERE issuer=? AND fanned_out=0 "
            "ORDER BY seq LIMIT ?",
            (issuer, max(1, min(int(limit), 1000))),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def mark_event_fanned_out(issuer: str, event_id: str) -> bool:
    """标记本 issuer 的某事件已 fanout；跨 issuer 零变更（WHERE issuer 隔离）。"""
    _validate_issuer(issuer)
    con = _connect()
    try:
        cur = con.execute(
            "UPDATE control_events SET fanned_out=1 "
            "WHERE issuer=? AND event_id=? AND fanned_out=0",
            (issuer, event_id),
        )
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()
