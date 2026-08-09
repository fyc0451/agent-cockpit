"""test_leader_binding.py — B0-PREP Q1 leader_binding 持久层测试。

覆盖：迁移幂等、首次绑定、CAS 改绑、stale version、并发改绑唯一 active、
无效状态/scope/字段拒绝、active/previous 查询、失败回滚旧绑定保持、
previous 排空状态演进、重启持久化、无凭据字段。
"""
from __future__ import annotations

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


# ---------------------------------------------------------------------------
# 迁移与 schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_migration_idempotent(self, db_path: Path) -> None:
        con = leader_binding._connect()
        con.close()
        con = leader_binding._connect()  # 二次初始化不报错
        con.close()
        assert db_path.is_file()

    def test_schema_has_no_credentials(self, db_path: Path) -> None:
        con = leader_binding._connect()
        columns = {
            row["name"] for row in con.execute("PRAGMA table_info(leader_bindings)").fetchall()
        }
        con.close()
        assert columns >= {
            "scope_kind", "scope_id", "mail_name", "previous_mail_name",
            "previous_state", "agent_name", "agent_kind", "session", "pane_id",
            "binding_version", "state", "degraded_reason", "updated_ts",
        }
        for col in columns:
            assert "token" not in col.lower()
            assert "password" not in col.lower()
            assert "secret" not in col.lower()

    def test_schema_forward_migration_adds_missing_column(self, db_path: Path) -> None:
        # 模拟历史库：先建旧版表（缺新列），再初始化应补齐
        con = _fresh_connect()
        con.executescript(
            """
            CREATE TABLE leader_bindings (
              scope_kind TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              mail_name TEXT NOT NULL,
              binding_version INTEGER NOT NULL,
              state TEXT NOT NULL,
              updated_ts REAL NOT NULL,
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
        assert "previous_mail_name" in columns
        assert "agent_kind" in columns

    def test_unique_active_index_enforced(self, db_path: Path) -> None:
        leader_binding.bind_leader("team", "t1", mail_name="a@t1", issuer="issuer-1", expected_version=0)
        with pytest.raises(sqlite3.IntegrityError):
            con = _fresh_connect()
            con.execute(
                "INSERT INTO leader_bindings VALUES('team','t1','x@t1',NULL,NULL,"
                "NULL,NULL,NULL,NULL,5,'active',NULL,1.0,"
                "'i',NULL,1,NULL)"
            )
            con.commit()
            con.close()


# ---------------------------------------------------------------------------
# 首次绑定与查询
# ---------------------------------------------------------------------------

class TestBind:
    def test_first_bind_creates_active(self, db_path: Path) -> None:
        binding = leader_binding.bind_leader(
            "team", "channel-1", mail_name="codex-agent-cockpit", issuer="issuer-1",
            agent_name="codex", agent_kind="codex", session="s1", pane_id="p1",
            expected_version=0,
        )
        assert binding["state"] == "active"
        assert binding["binding_version"] == 1
        assert binding["mail_name"] == "codex-agent-cockpit"
        assert binding["previous_mail_name"] is None
        active = leader_binding.get_active_binding("team", "channel-1")
        assert active is not None
        assert active["mail_name"] == "codex-agent-cockpit"
        assert active["agent_kind"] == "codex"
        assert active["session"] == "s1"
        assert active["pane_id"] == "p1"

    def test_same_mail_name_rebind_is_idempotent(self, db_path: Path) -> None:
        leader_binding.bind_leader("user", "u1", mail_name="a", issuer="issuer-1", pane_id="p1", expected_version=0)
        leader_binding.bind_leader("user", "u1", mail_name="a", issuer="issuer-1", pane_id="p2", expected_version=1)
        active = leader_binding.get_active_binding("user", "u1")
        assert active["mail_name"] == "a"
        assert active["pane_id"] == "p2"
        assert active["binding_version"] == 1  # 未产生新版本
        assert len(leader_binding.list_bindings("user", "u1")) == 1

    def test_invalid_scope_rejected(self, db_path: Path) -> None:
        with pytest.raises(leader_binding.BindingError, match="scope_kind"):
            leader_binding.bind_leader("org", "x", mail_name="a", issuer="issuer-1", expected_version=0)
        with pytest.raises(leader_binding.BindingError, match="scope_id"):
            leader_binding.bind_leader("team", "", mail_name="a", issuer="issuer-1", expected_version=0)

    def test_invalid_mail_name_rejected(self, db_path: Path) -> None:
        with pytest.raises(leader_binding.BindingError, match="mail_name"):
            leader_binding.bind_leader("team", "t1", mail_name="", expected_version=0, issuer="issuer-1")
        with pytest.raises(leader_binding.BindingError, match="mail_name"):
            leader_binding.bind_leader("team", "t1", mail_name="a\nb", issuer="issuer-1", expected_version=0)

    def test_credentials_field_rejected(self, db_path: Path) -> None:
        # 正常字段不抛
        leader_binding.bind_leader(
            "team", "t1", mail_name="a", issuer="issuer-1", agent_name="x", agent_kind="k",
            session="s", pane_id="p", expected_version=0,
        )
        # 敏感字段名被拒绝（白名单防护）
        with pytest.raises(leader_binding.BindingError, match="敏感"):
            leader_binding._validate_fields({"access_token": "secret"})
        with pytest.raises(leader_binding.BindingError, match="敏感"):
            leader_binding._validate_fields({"pane_password": "x"})

    def test_invalid_state_query_rejected(self, db_path: Path) -> None:
        with pytest.raises(leader_binding.BindingError, match="state"):
            leader_binding.list_bindings(state="bogus")


# ---------------------------------------------------------------------------
# CAS 改绑
# ---------------------------------------------------------------------------

class TestCasRebind:
    def test_rebind_with_correct_version(self, db_path: Path) -> None:
        first = leader_binding.bind_leader("team", "t1", mail_name="old@t1", issuer="issuer-1", expected_version=0)
        assert first["binding_version"] == 1
        second = leader_binding.bind_leader(
            "team", "t1", mail_name="new@t1", issuer="issuer-1", expected_version=1,
        )
        assert second["binding_version"] == 2
        assert second["state"] == "active"
        assert second["previous_mail_name"] == "old@t1"
        # 旧绑定软退役保留
        old = leader_binding.get_binding("team", "t1", "old@t1")
        assert old is not None
        assert old["state"] == "previous"
        assert old["previous_state"] == "draining"
        # 唯一 active
        assert leader_binding.get_active_binding("team", "t1")["mail_name"] == "new@t1"
        assert len(leader_binding.list_bindings("team", "t1", state="active")) == 1

    def test_stale_version_rejected_no_change(self, db_path: Path) -> None:
        leader_binding.bind_leader("team", "t1", mail_name="a", issuer="issuer-1", expected_version=0)
        with pytest.raises(leader_binding.StaleVersionError, match="CAS"):
            leader_binding.bind_leader(
                "team", "t1", mail_name="b", issuer="issuer-1", expected_version=99,
            )
        active = leader_binding.get_active_binding("team", "t1")
        assert active["mail_name"] == "a"
        assert active["binding_version"] == 1
        assert leader_binding.get_binding("team", "t1", "b") is None

    def test_rebind_without_version_rejected(self, db_path: Path) -> None:
        """mandatory CAS：expected_version=None 一律拒绝，零变更。"""
        leader_binding.bind_leader("team", "t1", mail_name="a", issuer="issuer-1", expected_version=0)
        with pytest.raises(leader_binding.BindingError, match="expected_version"):
            leader_binding.bind_leader("team", "t1", mail_name="b", issuer="issuer-1")
        with pytest.raises(leader_binding.BindingError, match="expected_version"):
            leader_binding.bind_leader("team", "t1", mail_name="a", issuer="issuer-1")  # 幂等路径同样拒绝
        active = leader_binding.get_active_binding("team", "t1")
        assert active["mail_name"] == "a"
        assert active["binding_version"] == 1
        assert leader_binding.get_binding("team", "t1", "b") is None

    def test_stale_same_mail_name_rebind_zero_mutation(self, db_path: Path) -> None:
        """同 mail_name 幂等路径也在 CAS 之后：wrong version 零变更。"""
        leader_binding.bind_leader(
            "team", "t1", mail_name="a", issuer="issuer-1", agent_kind="codex", pane_id="p1",
            expected_version=0,
        )
        with pytest.raises(leader_binding.StaleVersionError, match="CAS"):
            leader_binding.bind_leader(
                "team", "t1", mail_name="a", issuer="issuer-1", pane_id="p2", expected_version=99,
            )
        active = leader_binding.get_active_binding("team", "t1")
        assert active["pane_id"] == "p1"  # 未刷新
        assert active["binding_version"] == 1
        # 正确 version 的幂等刷新仍生效
        refreshed = leader_binding.bind_leader(
            "team", "t1", mail_name="a", issuer="issuer-1", pane_id="p3", expected_version=1,
        )
        assert refreshed["pane_id"] == "p3"
        assert refreshed["binding_version"] == 1

    def test_concurrent_rebind_single_active(self, db_path: Path) -> None:
        leader_binding.bind_leader("team", "t1", mail_name="base", issuer="issuer-1", expected_version=0)
        results: list[Any] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker(mail_name: str) -> None:
            barrier.wait()
            try:
                results.append(leader_binding.bind_leader(
                    "team", "t1", mail_name=mail_name, issuer="issuer-1", expected_version=1,
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
        assert len(results) == 1  # 恰一个 CAS 成功
        assert any(isinstance(e, leader_binding.StaleVersionError) for e in errors)
        active_rows = leader_binding.list_bindings("team", "t1", state="active")
        assert len(active_rows) == 1  # 任一 scope 不得出现两个 active

    def test_failed_activation_keeps_old_active(
        self, db_path: Path, monkeypatch: Any,
    ) -> None:
        """新 active 行插入失败 → 整个事务回滚，旧绑定保持 active（未被软退役）。"""
        leader_binding.bind_leader("team", "t1", mail_name="old@t1", issuer="issuer-1", expected_version=0)
        real_connect = leader_binding._connect

        class FlakyConnection:
            """代理连接：INSERT 到 leader_bindings 时注入失败。"""

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
                "team", "t1", mail_name="new@t1", issuer="issuer-1", expected_version=1,
            )
        # 注意：不能用 monkeypatch.undo()（会连 fixture 的 DB_PATH 一起撤销）
        monkeypatch.setattr(leader_binding, "_connect", real_connect)
        # 回滚验证：旧 active 行未被软退役、版本未变
        old = leader_binding.get_binding("team", "t1", "old@t1")
        assert old is not None
        assert old["state"] == "active"
        assert old["binding_version"] == 1
        # 新绑定未产生
        assert leader_binding.get_binding("team", "t1", "new@t1") is None
        assert leader_binding.get_active_binding("team", "t1")["mail_name"] == "old@t1"

    def test_reactivate_previous_mail_name(self, db_path: Path) -> None:
        """软退役行不删除但可原地复活为 active（主键占用不阻塞切回）。"""
        leader_binding.bind_leader("team", "t1", mail_name="a", issuer="issuer-1", expected_version=0)
        leader_binding.bind_leader("team", "t1", mail_name="b", issuer="issuer-1", expected_version=1)
        assert leader_binding.get_active_binding("team", "t1")["mail_name"] == "b"
        # 未排空禁止再改绑：a 仍是 previous(draining)，复活被拒
        with pytest.raises(leader_binding.BindingError, match="未排空"):
            leader_binding.bind_leader(
                "team", "t1", mail_name="a", issuer="issuer-1", expected_version=2,
            )
        # 排空 a 后可复活
        leader_binding.mark_previous_state("team", "t1", "a", state="drained")
        back = leader_binding.bind_leader(
            "team", "t1", mail_name="a", issuer="issuer-1", expected_version=2,
        )
        assert back["mail_name"] == "a"
        assert back["state"] == "active"
        assert back["binding_version"] == 3
        assert back["previous_mail_name"] == "b"
        # a 行原地复活（无重复行）
        rows = leader_binding.list_bindings("team", "t1")
        assert len(rows) == 2
        active_rows = [r for r in rows if r["state"] == "active"]
        assert len(active_rows) == 1
        assert active_rows[0]["mail_name"] == "a"


# ---------------------------------------------------------------------------
# previous 排空状态与退役
# ---------------------------------------------------------------------------

class TestPreviousDrain:
    def test_previous_state_progression(self, db_path: Path) -> None:
        leader_binding.bind_leader("team", "t1", mail_name="old@t1", issuer="issuer-1", expected_version=0)
        leader_binding.bind_leader("team", "t1", mail_name="new@t1", issuer="issuer-1", expected_version=1)
        old = leader_binding.get_binding("team", "t1", "old@t1")
        assert old["previous_state"] == "draining"
        # 排空失败 → degraded（显式可见，不静默宣告成功）
        assert leader_binding.mark_previous_state(
            "team", "t1", "old@t1", state="degraded",
            reason="拉取凭证缺失",
        )["updated"] is True
        old = leader_binding.get_binding("team", "t1", "old@t1")
        assert old["previous_state"] == "degraded"
        assert old["degraded_reason"] == "拉取凭证缺失"
        # degraded 仅可重试 draining
        assert leader_binding.mark_previous_state(
            "team", "t1", "old@t1", state="draining",
        )["updated"] is True
        # drained 终态
        assert leader_binding.mark_previous_state(
            "team", "t1", "old@t1", state="drained",
        )["updated"] is True
        old = leader_binding.get_binding("team", "t1", "old@t1")
        assert old["previous_state"] == "drained"
        # drained 不可回退
        with pytest.raises(leader_binding.BindingError, match="非法 drain 迁移"):
            leader_binding.mark_previous_state(
                "team", "t1", "old@t1", state="degraded",
            )

    def test_mark_previous_state_rejects_invalid(self, db_path: Path) -> None:
        with pytest.raises(leader_binding.BindingError, match="previous state"):
            leader_binding.mark_previous_state("team", "t1", "old", state="bogus")

    def test_retire_previous_keeps_row(self, db_path: Path) -> None:
        leader_binding.bind_leader("team", "t1", mail_name="old@t1", issuer="issuer-1", expected_version=0)
        leader_binding.bind_leader("team", "t1", mail_name="new@t1", issuer="issuer-1", expected_version=1)
        leader_binding.mark_previous_state("team", "t1", "old@t1", state="drained")
        r = leader_binding.retire_binding("team", "t1", "old@t1")
        assert r["retired"] is True
        old = leader_binding.get_binding("team", "t1", "old@t1")
        assert old is not None  # 软退役不删除
        assert old["state"] == "retired"
        assert old["previous_state"] == "drained"
        active = leader_binding.get_active_binding("team", "t1")
        assert active["mail_name"] == "new@t1"


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_restart_persists(self, db_path: Path, monkeypatch: Any) -> None:
        leader_binding.bind_leader(
            "team", "t1", mail_name="a", issuer="issuer-1", agent_kind="codex",
            session="s1", pane_id="p1", expected_version=0,
        )
        leader_binding.bind_leader("team", "t1", mail_name="b", issuer="issuer-1", expected_version=1)
        # 模拟重启：断开全部连接后重新绑定（新连接读取同一 DB 文件）
        con = _fresh_connect()
        rows = con.execute("SELECT * FROM leader_bindings ORDER BY updated_ts").fetchall()
        con.close()
        assert len(rows) == 2
        states = {r["mail_name"]: r["state"] for r in rows}
        assert states == {"a": "previous", "b": "active"}
        active = leader_binding.get_active_binding("team", "t1")
        assert active["mail_name"] == "b"
        assert active["binding_version"] == 2


# ── R2 复核：degraded 不作为 binding state（防第二 active footgun）──────

def test_binding_state_degraded_rejected_by_schema(db_path: Path):
    """binding state 无 degraded：active 退化只能经 previous_state=degraded
    表达，DB CHECK 拒绝 state='degraded' 行（避免 partial index 漏洞）。"""
    con = leader_binding._connect()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        con.execute(
            "INSERT INTO leader_bindings VALUES('team','t1','x@t1',NULL,NULL,"
            "NULL,NULL,NULL,NULL,1,'degraded',NULL,1.0,"
            "'i',NULL,1,NULL)"
        )
        con.commit()
    con.close()
    # 应用层同样拒绝
    with pytest.raises(leader_binding.BindingError, match="state"):
        leader_binding.list_bindings(state="degraded")


# ── 扩大门禁（#1732）：issuer/principal、route_epoch/migration_id、outbox ──

def test_issuer_and_registry_selector_persisted(db_path: Path) -> None:
    b = leader_binding.bind_leader(
        "team", "t1", mail_name="a", issuer="user-admin",
        registry_selector="registry://user/lark", expected_version=0,
    )
    assert b["issuer"] == "user-admin"
    assert b["registry_selector"] == "registry://user/lark"
    active = leader_binding.get_active_binding("team", "t1")
    assert active["issuer"] == "user-admin"
    assert active["registry_selector"] == "registry://user/lark"
    assert "token" not in str(active)  # 凭证只存 selector 不存 token


def test_route_epoch_and_migration_id_advance_on_switch(db_path: Path) -> None:
    first = leader_binding.bind_leader("team", "t1", mail_name="a", issuer="i", expected_version=0)
    assert first["route_epoch"] == 1
    assert first["migration_id"]
    second = leader_binding.bind_leader("team", "t1", mail_name="b", issuer="i", expected_version=1)
    assert second["route_epoch"] == 2
    assert second["migration_id"] != first["migration_id"]
    # 同版本幂等重绑不推进 route_epoch
    same = leader_binding.bind_leader("team", "t1", mail_name="b", issuer="i", expected_version=2)
    assert same["route_epoch"] == 2


def test_binding_changed_outbox_same_transaction(db_path: Path) -> None:
    leader_binding.bind_leader("team", "t1", mail_name="a", issuer="i", expected_version=0)
    leader_binding.bind_leader("team", "t1", mail_name="b", issuer="i", expected_version=1)
    events = leader_binding.list_control_events("team", "t1")
    assert len(events) == 2  # 两次切换各一个 binding_changed
    changed = events[-1]
    assert changed["event_type"] == "binding_changed"
    assert changed["binding_version"] == 2
    assert changed["migration_id"]
    assert changed["fanned_out"] == 0
    import json
    payload = json.loads(changed["payload_json"])
    assert payload["mail_name"] == "b"
    assert payload["previous_mail_name"] == "a"
    assert payload["issuer"] == "i"
    assert payload["route_epoch"] == 2


def test_outbox_fanout_replayable_and_idempotent(db_path: Path) -> None:
    leader_binding.bind_leader("team", "t1", mail_name="a", issuer="i", expected_version=0)
    pending = leader_binding.undelivered_control_events()
    assert len(pending) == 1
    event_id = pending[0]["event_id"]
    assert leader_binding.mark_event_fanned_out(event_id) is True
    assert leader_binding.undelivered_control_events() == []
    assert leader_binding.mark_event_fanned_out(event_id) is False  # 幂等
    # cursor 续读
    events = leader_binding.list_control_events(after_event_id=event_id)
    assert events == []


def test_binding_failure_rolls_back_outbox(db_path: Path, monkeypatch: Any) -> None:
    """激活失败回滚时 outbox 事件也不落库（同事务）。"""
    leader_binding.bind_leader("team", "t1", mail_name="old@t1", issuer="i", expected_version=0)
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
        leader_binding.bind_leader("team", "t1", mail_name="new@t1", issuer="i", expected_version=1)
    monkeypatch.setattr(leader_binding, "_connect", real_connect)
    events = leader_binding.list_control_events("team", "t1")
    assert len(events) == 1  # 只有首绑事件；失败切换未落 outbox
    assert leader_binding.get_active_binding("team", "t1")["mail_name"] == "old@t1"


def test_undrained_previous_blocks_chain(db_path: Path) -> None:
    """未排空禁止 a→b→c：previous 未 drained 时再次改绑被拒。"""
    leader_binding.bind_leader("team", "t1", mail_name="a", issuer="i", expected_version=0)
    leader_binding.bind_leader("team", "t1", mail_name="b", issuer="i", expected_version=1)
    with pytest.raises(leader_binding.BindingError, match="未排空"):
        leader_binding.bind_leader("team", "t1", mail_name="c", issuer="i", expected_version=2)
    # 排空后可继续
    leader_binding.mark_previous_state("team", "t1", "a", state="drained")
    c = leader_binding.bind_leader("team", "t1", mail_name="c", issuer="i", expected_version=2)
    assert c["mail_name"] == "c"


def test_drain_cas_expected_state(db_path: Path) -> None:
    leader_binding.bind_leader("team", "t1", mail_name="a", issuer="i", expected_version=0)
    leader_binding.bind_leader("team", "t1", mail_name="b", issuer="i", expected_version=1)
    with pytest.raises(leader_binding.StaleVersionError, match="drain CAS"):
        leader_binding.mark_previous_state(
            "team", "t1", "a", state="drained", expected_state="drained",
        )
    # 正确 expected_state 成功
    assert leader_binding.mark_previous_state(
        "team", "t1", "a", state="drained", expected_state="draining",
    )["updated"] is True


def test_drain_event_written(db_path: Path) -> None:
    leader_binding.bind_leader("team", "t1", mail_name="a", issuer="i", expected_version=0)
    leader_binding.bind_leader("team", "t1", mail_name="b", issuer="i", expected_version=1)
    r = leader_binding.mark_previous_state("team", "t1", "a", state="drained")
    assert r["event_id"]
    events = leader_binding.list_control_events("team", "t1")
    assert events[-1]["event_type"] == "drain_state_changed"
    assert events[-1]["event_id"] == r["event_id"]


def test_retire_requires_drained_and_zero_counts(db_path: Path) -> None:
    leader_binding.bind_leader("team", "t1", mail_name="a", issuer="i", expected_version=0)
    leader_binding.bind_leader("team", "t1", mail_name="b", issuer="i", expected_version=1)
    # 未 drained 拒绝
    with pytest.raises(leader_binding.BindingError, match="previous_state"):
        leader_binding.retire_binding("team", "t1", "a")
    leader_binding.mark_previous_state("team", "t1", "a", state="drained")
    # 计数非零拒绝
    with pytest.raises(leader_binding.BindingError, match="全零"):
        leader_binding.retire_binding("team", "t1", "a", remaining=1)
    with pytest.raises(leader_binding.BindingError, match="全零"):
        leader_binding.retire_binding("team", "t1", "a", ack_pending=2)
    # 全零成功 + outbox
    r = leader_binding.retire_binding("team", "t1", "a")
    assert r["retired"] is True
    assert r["event_id"]
    events = leader_binding.list_control_events("team", "t1")
    assert events[-1]["event_type"] == "binding_retired"
    old = leader_binding.get_binding("team", "t1", "a")
    assert old["state"] == "retired"


def test_duplicate_active_old_schema_fail_closed(db_path: Path) -> None:
    """旧 schema 存在重复 active：初始化必须抛可定位错误（fail-closed）。"""
    con = leader_binding._connect()
    con.execute(
        "INSERT INTO leader_bindings VALUES('team','t1','a@t1',NULL,NULL,"
        "NULL,NULL,NULL,NULL,1,'active',NULL,1.0,'i',NULL,1,NULL)"
    )
    con.commit()
    con.close()
    # 模拟历史库：先删除唯一索引，再插入第二个 active（旧 schema 才可能）
    con = sqlite3.connect(db_path)
    con.execute("DROP INDEX leader_bindings_active_once")
    con.commit()
    con.close()
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO leader_bindings VALUES('team','t1','b@t1',NULL,NULL,"
        "NULL,NULL,NULL,NULL,2,'active',NULL,1.0,'i',NULL,2,NULL)"
    )
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="重复 active"):
        leader_binding._connect()
