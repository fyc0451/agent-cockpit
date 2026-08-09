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
CONTROL_EVENT_TYPES = ("binding_changed", "binding_retired", "drain_state_changed")
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


def _initialize_connection(con: sqlite3.Connection) -> None:
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
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
          drain_remaining INTEGER NOT NULL DEFAULT 0,
          drain_pending INTEGER NOT NULL DEFAULT 0,
          drain_claimed INTEGER NOT NULL DEFAULT 0,
          drain_ack_pending INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(issuer, scope_kind, scope_id, mail_name),
          CHECK (state IN ('active', 'previous', 'retired')),
          CHECK (binding_version > 0)
        );
        CREATE TABLE IF NOT EXISTS binding_migrations (
          migration_id TEXT PRIMARY KEY,
          issuer TEXT NOT NULL,
          scope_kind TEXT NOT NULL,
          scope_id TEXT NOT NULL,
          from_binding_id TEXT,
          to_binding_id TEXT,
          route_epoch INTEGER NOT NULL,
          created_ts REAL NOT NULL
        );
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
        );
        CREATE INDEX IF NOT EXISTS control_events_scope
          ON control_events(issuer, scope_kind, scope_id, seq);
        CREATE INDEX IF NOT EXISTS control_events_pending
          ON control_events(fanned_out, seq);
        """
    )
    # 前向迁移：历史库缺列时补齐（幂等）；失败给可定位诊断
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
        ("binding_id", "TEXT"),
        ("drain_remaining", "INTEGER NOT NULL DEFAULT 0"),
        ("drain_pending", "INTEGER NOT NULL DEFAULT 0"),
        ("drain_claimed", "INTEGER NOT NULL DEFAULT 0"),
        ("drain_ack_pending", "INTEGER NOT NULL DEFAULT 0"),
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
    con.execute(
        "CREATE INDEX IF NOT EXISTS leader_bindings_scope "
        "ON leader_bindings(issuer, scope_kind, scope_id, state)"
    )
    # fail-closed：旧表已有行但无法推断 issuer（NULL/空）→ 显式迁移诊断，
    # 绝不猜默认 issuer
    rows = con.execute(
        "SELECT COUNT(*) AS n FROM leader_bindings"
    ).fetchone()
    if rows and int(rows["n"]) > 0:
        null_issuer = con.execute(
            "SELECT COUNT(*) AS n FROM leader_bindings "
            "WHERE issuer IS NULL OR issuer=''"
        ).fetchone()
        if null_issuer and int(null_issuer["n"]) > 0:
            raise RuntimeError(
                "leader_bindings fail-closed: 旧 schema 存在无法推断 issuer 的"
                f"绑定行（{null_issuer['n']} 行）。需人工指定 trust-domain "
                "issuer 迁移，不得猜测默认值"
            )
        null_binding_id = con.execute(
            "SELECT COUNT(*) AS n FROM leader_bindings "
            "WHERE binding_id IS NULL OR binding_id=''"
        ).fetchone()
        if null_binding_id and int(null_binding_id["n"]) > 0:
            raise RuntimeError(
                "leader_bindings fail-closed: 旧 schema 存在缺少 binding_id 的"
                f"绑定行（{null_binding_id['n']} 行）。需人工迁移补全 binding_id"
            )
    # 重复 active 诊断：在唯一索引重建前拦截，给可定位错误
    dup = con.execute(
        "SELECT issuer, scope_kind, scope_id, COUNT(*) AS n FROM leader_bindings "
        "WHERE state='active' GROUP BY issuer, scope_kind, scope_id "
        "HAVING COUNT(*) > 1"
    ).fetchall()
    if dup:
        spots = ", ".join(
            f"{r['issuer']}/{r['scope_kind']}/{r['scope_id']}({r['n']})"
            for r in dup
        )
        raise RuntimeError(
            "leader_bindings fail-closed: 旧 schema 存在重复 active 绑定，"
            f"拒绝启动（需人工修复）: {spots}"
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
            # payload 变化 = 变更：version+1 + migration + outbox
            version = int(active["binding_version"]) + 1
            migration_id = uuid.uuid4().hex
            event_id = uuid.uuid4().hex
            con.execute(
                "UPDATE leader_bindings SET agent_name=?, agent_kind=?, "
                "session=?, pane_id=?, registry_selector=?, "
                "binding_version=?, migration_id=?, updated_ts=? "
                "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=?",
                (agent_name, agent_kind, session, pane_id, registry_selector,
                 version, migration_id, current,
                 issuer, scope_kind, scope_id, mail_name),
            )
            con.execute(
                "INSERT INTO binding_migrations VALUES(?,?,?,?,?,?,?,?)",
                (migration_id, issuer, scope_kind, scope_id,
                 str(active["binding_id"]), str(active["binding_id"]),
                 int(active["route_epoch"]), current),
            )
            con.execute(
                "INSERT INTO control_events "
                "(event_id, issuer, scope_kind, scope_id, event_type, "
                "binding_version, migration_id, payload_json, created_ts, "
                "fanned_out) VALUES(?,?,?,?,?,?,?,?,?,0)",
                (event_id, issuer, scope_kind, scope_id, "binding_changed",
                 version, migration_id,
                 _event_payload({
                     "mail_name": mail_name, "previous_mail_name": mail_name,
                     "issuer": issuer, "route_epoch": int(active["route_epoch"]),
                     "registry_selector": registry_selector,
                     "changed_fields": True,
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
                "INSERT INTO leader_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,0,0,0,0)",
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
            "INSERT INTO binding_migrations VALUES(?,?,?,?,?,?,?,?)",
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
        # 强制 CAS：version + migration id + state 全匹配才更新
        if (
            int(row["binding_version"]) != expected_binding_version
            or (row["migration_id"] or "") != expected_migration_id
        ):
            raise StaleVersionError(
                "drain CAS 失败：expected "
                f"version={expected_binding_version} "
                f"migration={expected_migration_id!r}，当前 "
                f"version={row['binding_version']} "
                f"migration={row['migration_id']!r}"
            )
        prev_state = row["previous_state"] or "pending"
        if prev_state != expected_state:
            raise StaleVersionError(
                f"drain CAS 失败：expected state {expected_state!r}，"
                f"当前 {prev_state!r}"
            )
        if state not in _DRAIN_FORWARD.get(prev_state, frozenset()):
            raise BindingError(
                f"非法 drain 迁移: {prev_state!r} → {state!r}"
                f"（合法: {sorted(_DRAIN_FORWARD.get(prev_state, frozenset()))}）"
            )
        set_clause = "previous_state=?, degraded_reason=?, updated_ts=?"
        params: list[Any] = [state, reason, current]
        for name in ("remaining", "pending", "claimed", "ack_pending"):
            if name in counters:
                set_clause += f", drain_{name}=?"
                params.append(counters[name])
        params += [issuer, scope_kind, scope_id, previous_mail_name]
        con.execute(
            f"UPDATE leader_bindings SET {set_clause} "
            "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=? "
            "AND state='previous'",
            params,
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
    reason: str | None = None, now: float | None = None,
) -> dict[str, Any]:
    """把 previous 绑定标记 retired。前置从 DB 读取证明：previous_state 必须
    为 drained 且 drain_remaining/pending/claimed/ack_pending 全零（无调用
    者直传旁路）。成功同一事务写 binding_retired control event。"""
    _validate_issuer(issuer)
    _validate_scope(scope_kind, scope_id)
    _validate_mail_name(mail_name)
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
        con.execute(
            "UPDATE leader_bindings SET state='retired', "
            "previous_state='drained', "
            "degraded_reason=COALESCE(?,degraded_reason), updated_ts=? "
            "WHERE issuer=? AND scope_kind=? AND scope_id=? AND mail_name=? "
            "AND state='previous'",
            (reason, current, issuer, scope_kind, scope_id, mail_name),
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


def undelivered_control_events(limit: int = 100) -> list[dict[str, Any]]:
    """未 fanout 的 control events（按 seq 升序；fanout 按 event_id 幂等）。"""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM control_events WHERE fanned_out=0 "
            "ORDER BY seq LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def mark_event_fanned_out(event_id: str) -> bool:
    """标记事件已 fanout；重复标记幂等（已 fanout 返回 False）。"""
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
