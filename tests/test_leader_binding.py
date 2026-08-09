"""test_leader_binding.py — B0-PREP Q1/R2/R3 leader_binding 持久层测试。

覆盖：迁移幂等与旧 schema fail-closed、issuer 跨域隔离、mandatory CAS、
真幂等 payload、migration 记录、drain 单调状态机 + 强制 CAS + 计数持久化、
retire DB 证明、control_events outbox 单调 seq 分页/fanout 重放、
并发改绑唯一 active、失败回滚、重启持久化。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

import leader_binding


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: Any) -> Path:
    path = tmp_path / "leader-binding.sqlite3"
    monkeypatch.setattr(leader_binding, "DB_PATH", path)
    return path


def _fresh_connect() -> sqlite3.Connection:
    con = sqlite3.connect(leader_binding.DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _bind(issuer="issuer-1", scope_kind="team", scope_id="t1", **kw):
    kw.setdefault("expected_version", 0)
    return leader_binding.bind_leader(
        issuer, scope_kind, scope_id, **kw,
    )


# ---------------------------------------------------------------------------
# 迁移与 schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_migration_idempotent(self, db_path: Path) -> None:
        con = leader_binding._connect()
        con.close()
        con = leader_binding._connect()
        con.close()
        assert db_path.is_file()

    def test_schema_has_no_credentials(self, db_path: Path) -> None:
        con = leader_binding._connect()
        columns = {
            row["name"] for row in con.execute("PRAGMA table_info(leader_bindings)").fetchall()
        }
        events = {
            row["name"] for row in con.execute("PRAGMA table_info(control_events)").fetchall()
        }
        con.close()
        assert columns >= {
            "issuer", "scope_kind", "scope_id", "mail_name", "binding_id",
            "previous_mail_name", "previous_state", "agent_name", "agent_kind",
            "session", "pane_id", "registry_selector", "binding_version",
            "state", "degraded_reason", "updated_ts", "route_epoch",
            "migration_id", "drain_remaining", "drain_pending",
            "drain_claimed", "drain_ack_pending",
        }
        assert "seq" in events and "event_id" in events
        for col in columns | events:
            for secret in ("token", "password", "secret"):
                assert secret not in col.lower()

    def test_old_schema_with_rows_fails_closed_no_issuer_guess(self, db_path: Path) -> None:
        """旧表有行且 issuer 无法推断：fail-closed 显式迁移诊断，不猜默认。"""
        con = _fresh_connect()
        con.executescript(
            """
            CREATE TABLE leader_bindings (
              scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL,
              mail_name TEXT NOT NULL, binding_version INTEGER NOT NULL,
              state TEXT NOT NULL, updated_ts REAL NOT NULL,
              PRIMARY KEY(scope_kind, scope_id, mail_name)
            );
            INSERT INTO leader_bindings VALUES('team','t1','a',1,'active',1.0);
            """
        )
        con.close()
        with pytest.raises(RuntimeError, match="issuer"):
            leader_binding._connect()

    def test_old_schema_empty_migrates(self, db_path: Path) -> None:
        con = _fresh_connect()
        con.executescript(
            """
            CREATE TABLE leader_bindings (
              scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL,
              mail_name TEXT NOT NULL, binding_version INTEGER NOT NULL,
              state TEXT NOT NULL, updated_ts REAL NOT NULL,
              PRIMARY KEY(scope_kind, scope_id, mail_name)
            );
            """
        )
        con.close()
        con = leader_binding._connect()
        columns = {
            row["name"] for row in con.execute("PRAGMA table_info(leader_bindings)").fetchall()
        }
        con.close()
        assert "issuer" in columns and "binding_id" in columns

    def test_duplicate_active_old_schema_fail_closed(self, db_path: Path) -> None:
        """旧 schema 重复 active：初始化必须抛可定位错误。"""
        con = leader_binding._connect()
        con.execute(
            "INSERT INTO leader_bindings VALUES('i','team','t1','a','b1',NULL,"
            "NULL,NULL,NULL,NULL,NULL,NULL,1,'active',NULL,1.0,1,NULL,0,0,0,0)"
        )
        con.commit()
        con.close()
        con = sqlite3.connect(db_path)
        con.execute("DROP INDEX leader_bindings_active_once")
        con.commit()
        con.close()
        con = sqlite3.connect(db_path)
        con.execute(
            "INSERT INTO leader_bindings VALUES('i','team','t1','b','b2',NULL,"
            "NULL,NULL,NULL,NULL,NULL,NULL,2,'active',NULL,1.0,2,NULL,0,0,0,0)"
        )
        con.commit()
        con.close()
        with pytest.raises(RuntimeError, match="重复 active"):
            leader_binding._connect()

    def test_binding_state_degraded_rejected_by_schema(self, db_path: Path) -> None:
        con = leader_binding._connect()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            con.execute(
                "INSERT INTO leader_bindings VALUES('i','team','t1','x','bx',"
                "NULL,NULL,NULL,NULL,NULL,NULL,NULL,1,'degraded',NULL,1.0,1,"
                "NULL,0,0,0,0)"
            )
            con.commit()
        con.close()
        with pytest.raises(leader_binding.BindingError, match="state"):
            leader_binding.list_bindings(state="degraded")


# ---------------------------------------------------------------------------
# 首次绑定与查询
# ---------------------------------------------------------------------------

class TestBind:
    def test_first_bind_creates_active(self, db_path: Path) -> None:
        binding = leader_binding.bind_leader(
            "issuer-1", "team", "channel-1", mail_name="codex-agent-cockpit",
            agent_name="codex", agent_kind="codex", session="s1", pane_id="p1",
            expected_version=0,
        )
        assert binding["state"] == "active"
        assert binding["binding_version"] == 1
        assert binding["binding_id"]
        assert binding["issuer"] == "issuer-1"
        active = leader_binding.get_active_binding("issuer-1", "team", "channel-1")
        assert active["mail_name"] == "codex-agent-cockpit"
        assert active["agent_kind"] == "codex"

    def test_same_issuer_same_payload_rebind_is_true_idempotent(self, db_path: Path) -> None:
        _bind(mail_name="a", pane_id="p1", agent_kind="codex")
        second = _bind(mail_name="a", pane_id="p1", agent_kind="codex",
                       expected_version=1)
        assert second["binding_version"] == 1  # 真幂等：不推进版本
        assert len(leader_binding.list_control_events("issuer-1", "team", "t1")) == 1
        assert len(leader_binding.list_bindings("issuer-1", "team", "t1")) == 1

    def test_same_mail_name_payload_change_bumps_version(self, db_path: Path) -> None:
        _bind(mail_name="a", pane_id="p1")
        changed = _bind(mail_name="a", pane_id="p2", expected_version=1)
        assert changed["binding_version"] == 2  # payload 变化 → version+1
        events = leader_binding.list_control_events("issuer-1", "team", "t1")
        assert len(events) == 2  # 变更也写 outbox
        assert events[-1]["binding_version"] == 2
        mig = leader_binding.get_migration(changed["migration_id"])
        assert mig is not None
        assert mig["from_binding_id"] == mig["to_binding_id"]  # 同 binding 变更

    def test_invalid_scope_rejected(self, db_path: Path) -> None:
        with pytest.raises(leader_binding.BindingError, match="scope_kind"):
            _bind(scope_kind="org", mail_name="a")
        with pytest.raises(leader_binding.BindingError, match="scope_id"):
            _bind(scope_id="", mail_name="a")

    def test_invalid_mail_name_rejected(self, db_path: Path) -> None:
        with pytest.raises(leader_binding.BindingError, match="mail_name"):
            _bind(mail_name="")
        with pytest.raises(leader_binding.BindingError, match="mail_name"):
            _bind(mail_name="a\nb")

    def test_credentials_field_rejected(self, db_path: Path) -> None:
        _bind(mail_name="a", pane_id="p")
        with pytest.raises(leader_binding.BindingError, match="敏感"):
            leader_binding._validate_fields({"access_token": "secret"})
        with pytest.raises(leader_binding.BindingError, match="敏感"):
            leader_binding._validate_fields({"pane_password": "x"})

    def test_issuer_isolation(self, db_path: Path) -> None:
        """跨 issuer 隔离：同 scope 不同 issuer 各自独立 active。"""
        a = _bind(issuer="issuer-a", mail_name="a1")
        b = _bind(issuer="issuer-b", mail_name="b1")
        assert a["binding_version"] == 1 and b["binding_version"] == 1
        assert leader_binding.get_active_binding("issuer-a", "team", "t1")["mail_name"] == "a1"
        assert leader_binding.get_active_binding("issuer-b", "team", "t1")["mail_name"] == "b1"
        # issuer-a 的改绑不影响 issuer-b
        _bind(issuer="issuer-a", mail_name="a2", expected_version=1)
        assert leader_binding.get_active_binding("issuer-b", "team", "t1")["mail_name"] == "b1"
        assert leader_binding.get_active_binding("issuer-b", "team", "t1")["binding_version"] == 1


# ---------------------------------------------------------------------------
# CAS 改绑
# ---------------------------------------------------------------------------

class TestCasRebind:
    def test_rebind_with_correct_version(self, db_path: Path) -> None:
        first = _bind(mail_name="old@t1")
        second = _bind(mail_name="new@t1", expected_version=1)
        assert second["binding_version"] == 2
        assert second["previous_mail_name"] == "old@t1"
        old = leader_binding.get_binding("issuer-1", "team", "t1", "old@t1")
        assert old["state"] == "previous"
        assert old["migration_id"] == second["migration_id"]  # 不保留旧激活 migration
        assert leader_binding.get_active_binding("issuer-1", "team", "t1")["mail_name"] == "new@t1"

    def test_migration_record_links_from_to_binding(self, db_path: Path) -> None:
        first = _bind(mail_name="a")
        second = _bind(mail_name="b", expected_version=1)
        mig = leader_binding.get_migration(second["migration_id"])
        assert mig is not None
        assert mig["issuer"] == "issuer-1"
        assert mig["from_binding_id"] == first["binding_id"]
        assert mig["to_binding_id"] == second["binding_id"]
        assert mig["route_epoch"] == 2

    def test_stale_version_rejected_no_change(self, db_path: Path) -> None:
        _bind(mail_name="a")
        with pytest.raises(leader_binding.StaleVersionError, match="CAS"):
            _bind(mail_name="b", expected_version=99)
        active = leader_binding.get_active_binding("issuer-1", "team", "t1")
        assert active["mail_name"] == "a"
        assert active["binding_version"] == 1
        assert leader_binding.get_binding("issuer-1", "team", "t1", "b") is None

    def test_rebind_without_version_rejected(self, db_path: Path) -> None:
        _bind(mail_name="a")
        with pytest.raises(leader_binding.BindingError, match="expected_version"):
            leader_binding.bind_leader(
                "issuer-1", "team", "t1", mail_name="b",
            )
        with pytest.raises(leader_binding.BindingError, match="expected_version"):
            leader_binding.bind_leader(
                "issuer-1", "team", "t1", mail_name="a", pane_id="p2",
            )

    def test_stale_same_mail_name_zero_mutation(self, db_path: Path) -> None:
        _bind(mail_name="a", pane_id="p1")
        with pytest.raises(leader_binding.StaleVersionError, match="CAS"):
            _bind(mail_name="a", pane_id="p2", expected_version=99)
        active = leader_binding.get_active_binding("issuer-1", "team", "t1")
        assert active["pane_id"] == "p1"
        assert active["binding_version"] == 1

    def test_concurrent_rebind_single_active(self, db_path: Path) -> None:
        _bind(mail_name="base")
        results: list[Any] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker(mail_name: str) -> None:
            barrier.wait()
            try:
                results.append(leader_binding.bind_leader(
                    "issuer-1", "team", "t1", mail_name=mail_name,
                    expected_version=1,
                ))
            except BaseException as exc:  # noqa: BLE001 - 收集 stale 等异常
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("a1",)),
            threading.Thread(target=worker, args=("a2",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(results) == 1
        assert any(isinstance(e, leader_binding.StaleVersionError) for e in errors)
        active_rows = leader_binding.list_bindings(
            "issuer-1", "team", "t1", state="active",
        )
        assert len(active_rows) == 1

    def test_failed_activation_keeps_old_active(
        self, db_path: Path, monkeypatch: Any,
    ) -> None:
        _bind(mail_name="old@t1")
        real_connect = leader_binding._connect

        class FlakyConnection:
            def __init__(self, con: sqlite3.Connection) -> None:
                self._con = con

            def __getattr__(self, name: str) -> Any:
                return getattr(self._con, name)

            def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
                if "INSERT INTO leader_bindings VALUES" in sql:
                    raise sqlite3.IntegrityError("injected insert failure")
                return self._con.execute(sql, *args, **kwargs)

        def flaky_connect() -> Any:
            return FlakyConnection(real_connect())

        monkeypatch.setattr(leader_binding, "_connect", flaky_connect)
        with pytest.raises(sqlite3.IntegrityError):
            leader_binding.bind_leader(
                "issuer-1", "team", "t1", mail_name="new@t1",
                expected_version=1,
            )
        monkeypatch.setattr(leader_binding, "_connect", real_connect)
        old = leader_binding.get_binding("issuer-1", "team", "t1", "old@t1")
        assert old["state"] == "active"
        assert old["binding_version"] == 1
        assert leader_binding.get_binding("issuer-1", "team", "t1", "new@t1") is None
        # outbox 无失败事件（同事务回滚）
        events = leader_binding.list_control_events("issuer-1", "team", "t1")
        assert len(events) == 1

    def test_reactivate_previous_mail_name(self, db_path: Path) -> None:
        _bind(mail_name="a")
        _bind(mail_name="b", expected_version=1)
        with pytest.raises(leader_binding.BindingError, match="未排空"):
            leader_binding.bind_leader(
                "issuer-1", "team", "t1", mail_name="a", expected_version=2,
            )
        _drain("a", expected_version=1)
        back = leader_binding.bind_leader(
            "issuer-1", "team", "t1", mail_name="a", expected_version=2,
        )
        assert back["mail_name"] == "a"
        assert back["binding_version"] == 3
        rows = leader_binding.list_bindings("issuer-1", "team", "t1")
        assert len(rows) == 2
        assert len([r for r in rows if r["state"] == "active"]) == 1


def _drain(mail_name, *, issuer="issuer-1", expected_version, migration_id=None,
           state="drained"):
    row = leader_binding.get_binding(issuer, "team", "t1", mail_name)
    mig = migration_id or row["migration_id"]
    return leader_binding.mark_previous_state(
        issuer, "team", "t1", mail_name, state=state,
        expected_binding_version=expected_version,
        expected_migration_id=mig,
        expected_state="draining",
    )


# ---------------------------------------------------------------------------
# drain 状态机（强制 CAS + 计数持久化）
# ---------------------------------------------------------------------------

class TestDrain:
    def test_forced_cas_required(self, db_path: Path) -> None:
        _bind(mail_name="a")
        _bind(mail_name="b", expected_version=1)
        with pytest.raises(leader_binding.BindingError, match="expected_binding_version"):
            leader_binding.mark_previous_state(
                "issuer-1", "team", "t1", "a", state="drained",
                expected_migration_id="m", expected_state="draining",
            )
        with pytest.raises(leader_binding.BindingError, match="expected_migration_id"):
            leader_binding.mark_previous_state(
                "issuer-1", "team", "t1", "a", state="drained",
                expected_binding_version=1, expected_state="draining",
            )
        with pytest.raises(leader_binding.BindingError, match="expected_state"):
            leader_binding.mark_previous_state(
                "issuer-1", "team", "t1", "a", state="drained",
                expected_binding_version=1, expected_migration_id="m",
            )

    def test_stale_worker_zero_mutation(self, db_path: Path) -> None:
        """stale worker（版本/迁移 id 不符）零变更。"""
        _bind(mail_name="a")
        _bind(mail_name="b", expected_version=1)
        with pytest.raises(leader_binding.StaleVersionError, match="drain CAS"):
            leader_binding.mark_previous_state(
                "issuer-1", "team", "t1", "a", state="drained",
                expected_binding_version=99,
                expected_migration_id="wrong-migration",
                expected_state="draining",
            )
        old = leader_binding.get_binding("issuer-1", "team", "t1", "a")
        assert old["previous_state"] == "draining"  # 零变更

    def test_drain_monotonic_and_terminal(self, db_path: Path) -> None:
        _bind(mail_name="a")
        _bind(mail_name="b", expected_version=1)
        row = leader_binding.get_binding("issuer-1", "team", "t1", "a")
        mig = row["migration_id"]
        # 合法：draining → degraded（失败可见）
        leader_binding.mark_previous_state(
            "issuer-1", "team", "t1", "a", state="degraded",
            expected_binding_version=1, expected_migration_id=mig,
            expected_state="draining", reason="拉取凭证缺失",
        )
        old = leader_binding.get_binding("issuer-1", "team", "t1", "a")
        assert old["previous_state"] == "degraded"
        assert old["degraded_reason"] == "拉取凭证缺失"
        # degraded 仅可重试 draining
        leader_binding.mark_previous_state(
            "issuer-1", "team", "t1", "a", state="draining",
            expected_binding_version=1, expected_migration_id=mig,
            expected_state="degraded",
        )
        # drained 终态
        leader_binding.mark_previous_state(
            "issuer-1", "team", "t1", "a", state="drained",
            expected_binding_version=1, expected_migration_id=mig,
            expected_state="draining",
        )
        old = leader_binding.get_binding("issuer-1", "team", "t1", "a")
        assert old["previous_state"] == "drained"
        # drained 不可回退
        with pytest.raises(leader_binding.BindingError, match="非法 drain 迁移"):
            leader_binding.mark_previous_state(
                "issuer-1", "team", "t1", "a", state="degraded",
                expected_binding_version=1, expected_migration_id=mig,
                expected_state="drained",
            )

    def test_drain_counters_persist_and_cas(self, db_path: Path) -> None:
        _bind(mail_name="a")
        _bind(mail_name="b", expected_version=1)
        row = leader_binding.get_binding("issuer-1", "team", "t1", "a")
        mig = row["migration_id"]
        leader_binding.mark_previous_state(
            "issuer-1", "team", "t1", "a", state="draining",
            expected_binding_version=1, expected_migration_id=mig,
            expected_state="draining", remaining=3, pending=2, claimed=1, ack_pending=0,
        )
        old = leader_binding.get_binding("issuer-1", "team", "t1", "a")
        assert old["drain_remaining"] == 3
        assert old["drain_pending"] == 2
        assert old["drain_claimed"] == 1
        assert old["drain_ack_pending"] == 0
        # 计数更新后置零
        leader_binding.mark_previous_state(
            "issuer-1", "team", "t1", "a", state="drained",
            expected_binding_version=1, expected_migration_id=mig,
            expected_state="draining", remaining=0, pending=0, claimed=0, ack_pending=0,
        )
        old = leader_binding.get_binding("issuer-1", "team", "t1", "a")
        assert old["drain_remaining"] == 0


# ---------------------------------------------------------------------------
# retire（DB 证明，无调用者旁路）
# ---------------------------------------------------------------------------

class TestRetire:
    def test_retire_requires_drained_and_db_zero_counts(self, db_path: Path) -> None:
        _bind(mail_name="a")
        _bind(mail_name="b", expected_version=1)
        with pytest.raises(leader_binding.BindingError, match="previous_state"):
            leader_binding.retire_binding("issuer-1", "team", "t1", "a")
        # 排空但计数非零（DB 证明）→ 拒绝
        row = leader_binding.get_binding("issuer-1", "team", "t1", "a")
        leader_binding.mark_previous_state(
            "issuer-1", "team", "t1", "a", state="drained",
            expected_binding_version=1, expected_migration_id=row["migration_id"],
            expected_state="draining", remaining=1,
        )
        with pytest.raises(leader_binding.BindingError, match="DB 证明"):
            leader_binding.retire_binding("issuer-1", "team", "t1", "a")
        # 计数清零后成功
        leader_binding.mark_previous_state(
            "issuer-1", "team", "t1", "a", state="drained",
            expected_binding_version=1, expected_migration_id=row["migration_id"],
            expected_state="drained", remaining=0,
        )
        r = leader_binding.retire_binding("issuer-1", "team", "t1", "a")
        assert r["retired"] is True
        old = leader_binding.get_binding("issuer-1", "team", "t1", "a")
        assert old["state"] == "retired"

    def test_retire_forgery_rejected(self, db_path: Path) -> None:
        """调用者无法伪造证明：无计数参数旁路。"""
        _bind(mail_name="a")
        _bind(mail_name="b", expected_version=1)
        with pytest.raises(TypeError):
            leader_binding.retire_binding(
                "issuer-1", "team", "t1", "a", remaining=0,  # 不再接受该参数
            )


# ---------------------------------------------------------------------------
# outbox（单调 seq 分页；fanout 重放）
# ---------------------------------------------------------------------------

class TestOutbox:
    def test_seq_monotonic_and_pagination(self, db_path: Path) -> None:
        _bind(mail_name="a")
        _bind(mail_name="b", expected_version=1)
        _drain("a", expected_version=1)
        _bind(mail_name="c", expected_version=2)
        _drain("b", expected_version=2)
        events = leader_binding.list_control_events("issuer-1", "team", "t1")
        assert len(events) == 5  # 3 binding_changed + 2 drain_state_changed
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == 5  # 单调唯一
        # 游标分页
        page2 = leader_binding.list_control_events(
            "issuer-1", "team", "t1", after_seq=seqs[0],
        )
        assert len(page2) == 4
        assert page2[0]["seq"] > seqs[0]

    def test_fanout_replayable_and_idempotent(self, db_path: Path) -> None:
        _bind(mail_name="a")
        pending = leader_binding.undelivered_control_events()
        assert len(pending) == 1
        event_id = pending[0]["event_id"]
        assert leader_binding.mark_event_fanned_out(event_id) is True
        assert leader_binding.undelivered_control_events() == []
        assert leader_binding.mark_event_fanned_out(event_id) is False

    def test_events_carry_issuer_scope_migration(self, db_path: Path) -> None:
        _bind(mail_name="a")
        _bind(mail_name="b", expected_version=1)
        events = leader_binding.list_control_events("issuer-1", "team", "t1")
        changed = events[-1]
        assert changed["event_type"] == "binding_changed"
        assert changed["issuer"] == "issuer-1"
        assert changed["binding_version"] == 2
        mig = leader_binding.get_migration(changed["migration_id"])
        assert mig is not None
        payload = json.loads(changed["payload_json"])
        assert payload["mail_name"] == "b"


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_restart_persists(self, db_path: Path) -> None:
        _bind(mail_name="a", agent_kind="codex", session="s1", pane_id="p1")
        _bind(mail_name="b", expected_version=1)
        con = _fresh_connect()
        rows = con.execute("SELECT * FROM leader_bindings ORDER BY updated_ts").fetchall()
        con.close()
        assert len(rows) == 2
        states = {r["mail_name"]: r["state"] for r in rows}
        assert states == {"a": "previous", "b": "active"}
        active = leader_binding.get_active_binding("issuer-1", "team", "t1")
        assert active["mail_name"] == "b"
        assert active["binding_version"] == 2
        con = _fresh_connect()
        migs = con.execute("SELECT COUNT(*) AS n FROM binding_migrations").fetchone()
        con.close()
        assert int(migs["n"]) == 2  # 首绑 + a→b 各一条 migration
