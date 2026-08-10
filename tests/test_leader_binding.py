"""test_leader_binding.py — B0 leader_binding R4 持久层测试。

覆盖（R4 #1766 关闭 + 正面回归）：
- 旧 schema 事务重建：空库可迁移、有行 issuer/binding_id 歧义 fail-closed、
  control_events 旧 schema 重建、重复 active fail-closed、degraded 不入 state。
- issuer 跨域隔离、mandatory CAS、真幂等 payload、migration 记录。
- 同 mail_name 路由载荷变化→binding_updated（version+route_epoch+1，不造
  migration/previous）；同 payload 真 no-op。
- drain：强制 version+migration+state+drain_revision CAS，drain_revision 单调；
  并发 self-loop 单赢家（rowcount）；计数持久化。
- retire：强制四元 CAS，跨 a→b→a→b 旧轮 worker 零变更；DB 证明计数。
- outbox：单调 seq 分页、issuer-scoped fanout、跨 issuer 读/ack 零影响。
- 并发改绑唯一 active、失败回滚保旧、重启持久化。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

import leader_binding

ISSUER = "issuer-1"


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: Any) -> Path:
    path = tmp_path / "leader-binding.sqlite3"
    monkeypatch.setattr(leader_binding, "DB_PATH", path)
    return path


def _fresh_connect() -> sqlite3.Connection:
    con = sqlite3.connect(leader_binding.DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _bind(issuer=ISSUER, scope_kind="team", scope_id="t1", **kw):
    kw.setdefault("expected_version", 0)
    return leader_binding.bind_leader(issuer, scope_kind, scope_id, **kw)


def _prev(mail_name, *, issuer=ISSUER, scope_kind="team", scope_id="t1"):
    return leader_binding.get_binding(issuer, scope_kind, scope_id, mail_name)


def _drain(mail_name, *, issuer=ISSUER, expected_version, state="drained",
           expected_state=None, **counts):
    """读当前 previous_state+drain_revision 做 CAS；可多步串联。"""
    row = _prev(mail_name, issuer=issuer)
    exp_state = expected_state or (row["previous_state"] or "draining")
    return leader_binding.mark_previous_state(
        issuer, "team", "t1", mail_name, state=state,
        expected_binding_version=expected_version,
        expected_migration_id=row["migration_id"],
        expected_state=exp_state,
        expected_drain_revision=int(row["drain_revision"]),
        **counts,
    )


def _retire(mail_name, *, issuer=ISSUER, expected_version):
    row = _prev(mail_name, issuer=issuer)
    return leader_binding.retire_binding(
        issuer, "team", "t1", mail_name,
        expected_binding_version=expected_version,
        expected_migration_id=row["migration_id"],
        expected_state=row["previous_state"] or "draining",
        expected_drain_revision=int(row["drain_revision"]),
    )


# ── 旧 schema 事务重建 ───────────────────────────────────────────────────

class TestSchema:
    def test_migration_idempotent(self, db_path: Path) -> None:
        leader_binding._connect().close()
        leader_binding._connect().close()
        assert db_path.is_file()

    def test_schema_has_no_credentials(self, db_path: Path) -> None:
        con = leader_binding._connect()
        cols = {r["name"] for r in con.execute("PRAGMA table_info(leader_bindings)")}
        ev = {r["name"] for r in con.execute("PRAGMA table_info(control_events)")}
        con.close()
        assert cols >= {"issuer", "drain_revision", "binding_id", "route_epoch"}
        assert "seq" in ev and "issuer" in ev
        for c in cols | ev:
            for s in ("token", "password", "secret"):
                assert s not in c.lower()

    def test_old_schema_empty_rebuilds_and_can_bind(self, db_path: Path) -> None:
        con = _fresh_connect()
        con.executescript(
            "CREATE TABLE leader_bindings (scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, "
            "mail_name TEXT NOT NULL, binding_version INTEGER NOT NULL, state TEXT NOT NULL, "
            "updated_ts REAL NOT NULL, PRIMARY KEY(scope_kind, scope_id, mail_name));"
        )
        con.close()
        b = _bind(mail_name="a")  # 空旧库重建后可真实 bind
        assert b["binding_version"] == 1
        assert b["issuer"] == ISSUER
        # PK 已重建为含 issuer
        con = _fresh_connect()
        pk = [r["name"] for r in con.execute("PRAGMA table_info(leader_bindings)") if int(r["pk"]) > 0]
        con.close()
        assert pk == ["issuer", "scope_kind", "scope_id", "mail_name"]

    def test_old_schema_with_rows_no_issuer_fails_closed(self, db_path: Path) -> None:
        con = _fresh_connect()
        con.executescript(
            "CREATE TABLE leader_bindings (scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, "
            "mail_name TEXT NOT NULL, binding_version INTEGER NOT NULL, state TEXT NOT NULL, "
            "updated_ts REAL NOT NULL, PRIMARY KEY(scope_kind, scope_id, mail_name));"
            "INSERT INTO leader_bindings VALUES('team','t1','a',1,'active',1.0);"
        )
        con.close()
        with pytest.raises(RuntimeError, match="issuer"):
            leader_binding._connect()

    def test_old_control_events_rebuilds(self, db_path: Path) -> None:
        con = _fresh_connect()
        # 需先有新 schema leader_bindings，_initialize_connection 才会走
        # control_events 重建分支（否则会走"全新库"分支直接建表而非重建）。
        con.executescript(
            "CREATE TABLE leader_bindings ("
            "issuer TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, "
            "mail_name TEXT NOT NULL, binding_id TEXT NOT NULL, previous_mail_name TEXT, "
            "previous_state TEXT, agent_name TEXT, agent_kind TEXT, session TEXT, "
            "pane_id TEXT, registry_selector TEXT, binding_version INTEGER NOT NULL, "
            "state TEXT NOT NULL, degraded_reason TEXT, updated_ts REAL NOT NULL, "
            "route_epoch INTEGER NOT NULL DEFAULT 0, migration_id TEXT, "
            "drain_revision INTEGER NOT NULL DEFAULT 0, "
            "drain_remaining INTEGER NOT NULL DEFAULT 0, "
            "drain_pending INTEGER NOT NULL DEFAULT 0, "
            "drain_claimed INTEGER NOT NULL DEFAULT 0, "
            "drain_ack_pending INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(issuer, scope_kind, scope_id, mail_name));"
            "CREATE TABLE control_events (event_id TEXT PRIMARY KEY, scope_kind TEXT, scope_id TEXT, "
            "event_type TEXT, binding_version INTEGER, migration_id TEXT, payload_json TEXT, "
            "created_ts REAL, fanned_out INTEGER);"
        )
        con.close()
        con = leader_binding._connect()
        cols = {r["name"] for r in con.execute("PRAGMA table_info(control_events)")}
        con.close()
        assert "seq" in cols and "issuer" in cols

    def test_duplicate_active_fail_closed(self, db_path: Path) -> None:
        con = leader_binding._connect()
        con.execute(
            "INSERT INTO leader_bindings VALUES('i','team','t1','a','b1',NULL,NULL,NULL,"
            "NULL,NULL,NULL,NULL,1,'active',NULL,1.0,1,NULL,0,0,0,0,0)"
        )
        con.execute("DROP INDEX leader_bindings_active_once")
        con.execute(
            "INSERT INTO leader_bindings VALUES('i','team','t1','b','b2',NULL,NULL,NULL,"
            "NULL,NULL,NULL,NULL,2,'active',NULL,1.0,2,NULL,0,0,0,0,0)"
        )
        con.commit(); con.close()
        with pytest.raises(RuntimeError, match="重复 active"):
            leader_binding._connect()

    def test_degraded_state_rejected_by_schema(self, db_path: Path) -> None:
        con = leader_binding._connect()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            con.execute(
                "INSERT INTO leader_bindings VALUES('i','team','t1','x','bx',NULL,NULL,NULL,"
                "NULL,NULL,NULL,NULL,1,'degraded',NULL,1.0,1,NULL,0,0,0,0,0)"
            )
        con.close()
        with pytest.raises(leader_binding.BindingError, match="state"):
            leader_binding.list_bindings(state="degraded")


# ── 绑定与查询 ───────────────────────────────────────────────────────────

class TestBind:
    def test_first_bind_creates_active(self, db_path: Path) -> None:
        b = _bind(scope_id="c1", mail_name="codex-agent-cockpit",
                  agent_name="codex", agent_kind="codex", session="s1", pane_id="p1")
        assert b["state"] == "active" and b["binding_version"] == 1 and b["issuer"] == ISSUER
        a = leader_binding.get_active_binding(ISSUER, "team", "c1")
        assert a["mail_name"] == "codex-agent-cockpit" and a["agent_kind"] == "codex"

    def test_same_payload_rebind_is_true_noop(self, db_path: Path) -> None:
        _bind(mail_name="a", pane_id="p1", agent_kind="codex")
        second = _bind(mail_name="a", pane_id="p1", agent_kind="codex", expected_version=1)
        assert second["binding_version"] == 1  # 真 no-op
        assert len(leader_binding.list_control_events(ISSUER, "team", "t1")) == 1
        assert len(leader_binding.list_bindings(ISSUER, "team", "t1")) == 1

    def test_same_mail_payload_change_is_binding_updated(self, db_path: Path) -> None:
        """ADR §3：同 mail_name 路由载荷变化→binding_updated（version+route_epoch+1，
        不造 from=to migration/previous）。"""
        _bind(mail_name="a", pane_id="p1")
        before = leader_binding.get_active_binding(ISSUER, "team", "t1")
        changed = _bind(mail_name="a", pane_id="p2", expected_version=1)
        assert changed["binding_version"] == 2
        assert changed["route_epoch"] == before["route_epoch"] + 1
        events = leader_binding.list_control_events(ISSUER, "team", "t1")
        assert events[-1]["event_type"] == "binding_updated"
        assert events[-1]["migration_id"] in (None, "")  # 不造 migration
        # 同名变更不新增 migration（仅初始 bind 的 1 条）
        con = _fresh_connect()
        n_mig = con.execute("SELECT COUNT(*) FROM binding_migrations").fetchone()[0]
        con.close()
        assert n_mig == 1
        # 不产生 previous
        assert leader_binding.get_binding(ISSUER, "team", "t1", "a")["state"] == "active"

    def test_invalid_scope_mail_name_rejected(self, db_path: Path) -> None:
        with pytest.raises(leader_binding.BindingError, match="scope_kind"):
            _bind(scope_kind="org", mail_name="a")
        with pytest.raises(leader_binding.BindingError, match="mail_name"):
            _bind(mail_name="a\nb")

    def test_credentials_field_rejected(self, db_path: Path) -> None:
        _bind(mail_name="a")
        with pytest.raises(leader_binding.BindingError, match="敏感"):
            leader_binding._validate_fields({"access_token": "x"})

    def test_issuer_isolation(self, db_path: Path) -> None:
        _bind(issuer="ia", mail_name="a1")
        _bind(issuer="ib", mail_name="b1")
        assert leader_binding.get_active_binding("ia", "team", "t1")["mail_name"] == "a1"
        assert leader_binding.get_active_binding("ib", "team", "t1")["mail_name"] == "b1"
        _bind(issuer="ia", mail_name="a2", expected_version=1)
        assert leader_binding.get_active_binding("ib", "team", "t1")["binding_version"] == 1


# ── CAS 改绑 ─────────────────────────────────────────────────────────────

class TestCasRebind:
    def test_rebind_with_correct_version(self, db_path: Path) -> None:
        _bind(mail_name="old@t1")
        second = _bind(mail_name="new@t1", expected_version=1)
        assert second["binding_version"] == 2 and second["previous_mail_name"] == "old@t1"
        old = _prev("old@t1")
        assert old["state"] == "previous" and old["migration_id"] == second["migration_id"]

    def test_migration_links_from_to(self, db_path: Path) -> None:
        first = _bind(mail_name="a")
        second = _bind(mail_name="b", expected_version=1)
        mig = leader_binding.get_migration(second["migration_id"])
        assert mig["from_binding_id"] == first["binding_id"]
        assert mig["to_binding_id"] == second["binding_id"]
        assert mig["route_epoch"] == 2

    def test_stale_version_rejected_no_change(self, db_path: Path) -> None:
        _bind(mail_name="a")
        with pytest.raises(leader_binding.StaleVersionError, match="CAS"):
            _bind(mail_name="b", expected_version=99)
        assert leader_binding.get_active_binding(ISSUER, "team", "t1")["mail_name"] == "a"
        assert _prev("b") is None

    def test_rebind_without_version_rejected(self, db_path: Path) -> None:
        _bind(mail_name="a")
        with pytest.raises(leader_binding.BindingError, match="expected_version"):
            leader_binding.bind_leader(ISSUER, "team", "t1", mail_name="b")

    def test_concurrent_rebind_single_active(self, db_path: Path) -> None:
        _bind(mail_name="base")
        results, errors, barrier = [], [], threading.Barrier(2)

        def worker(mail_name):
            barrier.wait()
            try:
                results.append(leader_binding.bind_leader(ISSUER, "team", "t1", mail_name=mail_name, expected_version=1))
            except BaseException as exc:
                errors.append(exc)

        ts = [threading.Thread(target=worker, args=(m,)) for m in ("a1", "a2")]
        for t in ts: t.start()
        for t in ts: t.join(timeout=10)
        assert len(results) == 1
        assert any(isinstance(e, leader_binding.StaleVersionError) for e in errors)
        assert len(leader_binding.list_bindings(ISSUER, "team", "t1", state="active")) == 1

    def test_failed_activation_keeps_old_active(self, db_path, monkeypatch) -> None:
        _bind(mail_name="old@t1")
        real = leader_binding._connect

        class Flaky:
            def __init__(self, c): self._c = c
            def __getattr__(self, n): return getattr(self._c, n)
            def execute(self, sql, *a, **k):
                if "INSERT INTO leader_bindings" in sql:
                    raise sqlite3.IntegrityError("injected")
                return self._c.execute(sql, *a, **k)

        monkeypatch.setattr(leader_binding, "_connect", lambda: Flaky(real()))
        with pytest.raises(sqlite3.IntegrityError):
            _bind(mail_name="new@t1", expected_version=1)
        monkeypatch.setattr(leader_binding, "_connect", real)
        assert _prev("old@t1")["state"] == "active"
        assert _prev("new@t1") is None

    def test_reactivate_previous_mail_name(self, db_path: Path) -> None:
        _bind(mail_name="a")
        _bind(mail_name="b", expected_version=1)
        with pytest.raises(leader_binding.BindingError, match="未排空"):
            _bind(mail_name="a", expected_version=2)
        _drain("a", expected_version=1)
        _retire("a", expected_version=1)
        back = _bind(mail_name="a", expected_version=2)
        assert back["binding_version"] == 3 and back["mail_name"] == "a"
        assert len([r for r in leader_binding.list_bindings(ISSUER, "team", "t1") if r["state"] == "active"]) == 1


# ── drain：强制四元 CAS + drain_revision 单调 ───────────────────────────

class TestDrain:
    def test_forced_cas_all_four_required(self, db_path: Path) -> None:
        _bind(mail_name="a"); _bind(mail_name="b", expected_version=1)
        for missing in ("expected_binding_version", "expected_migration_id", "expected_state", "expected_drain_revision"):
            kw = dict(state="drained", expected_binding_version=1, expected_migration_id="m",
                      expected_state="draining", expected_drain_revision=0)
            kw.pop(missing)
            with pytest.raises(leader_binding.BindingError, match=missing):
                leader_binding.mark_previous_state(ISSUER, "team", "t1", "a", **kw)

    def test_stale_drain_revision_zero_mutation(self, db_path: Path) -> None:
        _bind(mail_name="a"); _bind(mail_name="b", expected_version=1)
        row = _prev("a")
        with pytest.raises(leader_binding.StaleVersionError, match="drain CAS"):
            leader_binding.mark_previous_state(
                ISSUER, "team", "t1", "a", state="drained",
                expected_binding_version=1, expected_migration_id=row["migration_id"],
                expected_state="draining", expected_drain_revision=999,  # stale
            )
        assert _prev("a")["previous_state"] == "draining"  # 零变更

    def test_drain_revision_monotonic_multistep(self, db_path: Path) -> None:
        _bind(mail_name="a"); _bind(mail_name="b", expected_version=1)
        row = _prev("a"); mig = row["migration_id"]
        # draining→degraded
        leader_binding.mark_previous_state(ISSUER, "team", "t1", "a", state="degraded",
            expected_binding_version=1, expected_migration_id=mig, expected_state="draining", expected_drain_revision=0, reason="凭证缺失")
        assert _prev("a")["drain_revision"] == 1
        # degraded→draining（重试）
        leader_binding.mark_previous_state(ISSUER, "team", "t1", "a", state="draining",
            expected_binding_version=1, expected_migration_id=mig, expected_state="degraded", expected_drain_revision=1)
        assert _prev("a")["drain_revision"] == 2
        # draining→drained
        leader_binding.mark_previous_state(ISSUER, "team", "t1", "a", state="drained",
            expected_binding_version=1, expected_migration_id=mig, expected_state="draining", expected_drain_revision=2)
        assert _prev("a")["previous_state"] == "drained" and _prev("a")["drain_revision"] == 3
        # drained 不可回退
        with pytest.raises(leader_binding.BindingError, match="非法"):
            leader_binding.mark_previous_state(ISSUER, "team", "t1", "a", state="degraded",
                expected_binding_version=1, expected_migration_id=mig, expected_state="drained", expected_drain_revision=3)

    def test_concurrent_self_loop_single_winner(self, db_path: Path) -> None:
        """#1766: 两个 worker 同 expected 四元组（含 self-loop draining→draining），
        只一方成功（drain_revision CAS + rowcount），另一方零变更。"""
        _bind(mail_name="a"); _bind(mail_name="b", expected_version=1)
        row = _prev("a"); mig = row["migration_id"]
        results, errors, barrier = [], [], threading.Barrier(2)

        def worker(counts):
            barrier.wait()
            try:
                results.append(leader_binding.mark_previous_state(
                    ISSUER, "team", "t1", "a", state="draining",
                    expected_binding_version=1, expected_migration_id=mig,
                    expected_state="draining", expected_drain_revision=0,  # 同一 drain_revision
                    remaining=counts))
            except BaseException as exc:
                errors.append(exc)

        ts = [threading.Thread(target=worker, args=(c,)) for c in (1, 9)]
        for t in ts: t.start()
        for t in ts: t.join(timeout=10)
        assert len(results) == 1  # 恰一成功
        assert any(isinstance(e, leader_binding.StaleVersionError) for e in errors)
        # 胜者写入的 remaining 持久化；败者未改
        assert _prev("a")["drain_revision"] == 1

    def test_drain_counters_persist(self, db_path: Path) -> None:
        _bind(mail_name="a"); _bind(mail_name="b", expected_version=1)
        _drain("a", expected_version=1, state="draining", expected_state="draining",
               remaining=3, pending=2, claimed=1)
        assert _prev("a")["drain_remaining"] == 3 and _prev("a")["drain_claimed"] == 1
        _drain("a", expected_version=1, remaining=0, pending=0, claimed=0, ack_pending=0)
        assert _prev("a")["drain_remaining"] == 0


# ── retire：四元 CAS + 跨轮 stale 零变更 ──────────────────────────────────

class TestRetire:
    def test_retire_requires_drained_and_zero_counts(self, db_path: Path) -> None:
        _bind(mail_name="a"); _bind(mail_name="b", expected_version=1)
        row = _prev("a")
        # previous_state 非 drained → 拒绝
        with pytest.raises(leader_binding.BindingError, match="previous_state"):
            _retire("a", expected_version=1)
        # drained 但计数非零 → 拒绝（DB 证明）
        _drain("a", expected_version=1, remaining=1)
        with pytest.raises(leader_binding.BindingError, match="DB 证明"):
            _retire("a", expected_version=1)
        # 计数清零后成功
        _drain("a", expected_version=1, remaining=0, pending=0, claimed=0, ack_pending=0)
        assert _retire("a", expected_version=1)["retired"] is True
        assert _prev("a")["state"] == "retired"

    def test_retire_requires_cas_params(self, db_path: Path) -> None:
        _bind(mail_name="a"); _bind(mail_name="b", expected_version=1)
        _drain("a", expected_version=1, remaining=0, pending=0, claimed=0, ack_pending=0)
        with pytest.raises(leader_binding.BindingError, match="expected_binding_version"):
            leader_binding.retire_binding(ISSUER, "team", "t1", "a")

    def test_cross_round_stale_retire_zero_mutation(self, db_path: Path) -> None:
        """#1766: a→b→a→b 旧轮 worker 持旧 migration context 退役新 previous 零变更。"""
        _bind(mail_name="a")
        _bind(mail_name="b", expected_version=1)  # a→previous（migration M1）
        _drain("a", expected_version=1, remaining=0, pending=0, claimed=0, ack_pending=0)
        old_prev = _prev("a")
        # 旧 worker 持 a 的旧 drain_revision/migration 尝试 retire，但 a 已 drained；
        # 用 a 的 CAS retire 应成功（同 migration）。再用 WRONG migration → 零变更。
        with pytest.raises(leader_binding.StaleVersionError, match="CAS"):
            leader_binding.retire_binding(
                ISSUER, "team", "t1", "a",
                expected_binding_version=old_prev["binding_version"],
                expected_migration_id="wrong-migration",
                expected_state="drained", expected_drain_revision=old_prev["drain_revision"])
        assert _prev("a")["state"] == "previous"  # 零变更（未退役）
        # 正确 CAS retire 成功
        assert _retire("a", expected_version=1)["retired"] is True


# ── outbox：单调 seq + issuer-scoped fanout ──────────────────────────────

class TestOutbox:
    def test_seq_monotonic_and_pagination(self, db_path: Path) -> None:
        _bind(mail_name="a"); _bind(mail_name="b", expected_version=1)
        _drain("a", expected_version=1); _retire("a", expected_version=1)
        events = leader_binding.list_control_events(ISSUER, "team", "t1")
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        page2 = leader_binding.list_control_events(ISSUER, "team", "t1", after_seq=seqs[0])
        assert len(page2) == len(events) - 1 and page2[0]["seq"] > seqs[0]

    def test_fanout_issuer_scoped_isolation(self, db_path: Path) -> None:
        """#1766: issuer-scoped fanout；跨 issuer 读/ack 零影响。"""
        _bind(issuer="ia", mail_name="a")
        _bind(issuer="ib", mail_name="b")
        ia_pending = leader_binding.undelivered_control_events("ia")
        ib_pending = leader_binding.undelivered_control_events("ib")
        assert len(ia_pending) == 1 and len(ib_pending) == 1
        ia_id = ia_pending[0]["event_id"]
        ib_id = ib_pending[0]["event_id"]
        # issuer-B ack issuer-A 的事件 → 零变更
        assert leader_binding.mark_event_fanned_out("ib", ia_id) is False
        assert len(leader_binding.undelivered_control_events("ia")) == 1
        # 正确 issuer ack 成功
        assert leader_binding.mark_event_fanned_out("ia", ia_id) is True
        assert leader_binding.mark_event_fanned_out("ia", ia_id) is False  # 幂等
        assert len(leader_binding.undelivered_control_events("ia")) == 0
        assert len(leader_binding.undelivered_control_events("ib")) == 1  # ib 不受影响

    def test_events_carry_issuer_and_migration(self, db_path: Path) -> None:
        _bind(mail_name="a"); _bind(mail_name="b", expected_version=1)
        ev = leader_binding.list_control_events(ISSUER, "team", "t1")[-1]
        assert ev["event_type"] == "binding_changed" and ev["issuer"] == ISSUER
        assert ev["binding_version"] == 2
        assert leader_binding.get_migration(ev["migration_id"]) is not None


# ── 持久化 ───────────────────────────────────────────────────────────────

class TestPersistence:
    def test_restart_persists(self, db_path: Path) -> None:
        _bind(mail_name="a", agent_kind="codex", session="s1", pane_id="p1")
        _bind(mail_name="b", expected_version=1)
        con = _fresh_connect()
        rows = con.execute("SELECT mail_name, state FROM leader_bindings").fetchall()
        migs = con.execute("SELECT COUNT(*) FROM binding_migrations").fetchone()[0]
        con.close()
        assert {r["mail_name"]: r["state"] for r in rows} == {"a": "previous", "b": "active"}
        assert migs == 2  # 首绑 + a→b

# ── R5: 迁移安全（#1860 两项 HIGH）──────────────────────────────────────

_OLD_LB_R2 = (
    "CREATE TABLE leader_bindings ("
    "issuer TEXT NOT NULL, scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, "
    "mail_name TEXT NOT NULL, binding_id TEXT NOT NULL, binding_version INTEGER NOT NULL, "
    "state TEXT NOT NULL, updated_ts REAL NOT NULL, route_epoch INTEGER NOT NULL DEFAULT 0, "
    "migration_id TEXT, drain_revision INTEGER NOT NULL DEFAULT 0, "
    "drain_remaining INTEGER NOT NULL DEFAULT 0, drain_pending INTEGER NOT NULL DEFAULT 0, "
    "drain_claimed INTEGER NOT NULL DEFAULT 0, drain_ack_pending INTEGER NOT NULL DEFAULT 0, "
    "PRIMARY KEY(scope_kind, scope_id, mail_name));"
)
_OLD_CE_R2 = (
    "CREATE TABLE control_events ("
    "event_id TEXT PRIMARY KEY, issuer TEXT NOT NULL, scope_kind TEXT, scope_id TEXT, "
    "event_type TEXT, binding_version INTEGER, migration_id TEXT, payload_json TEXT, "
    "created_ts REAL, fanned_out INTEGER);"
)


class TestR5MigrationSafety:
    def test_rebuild_rollback_on_copy_failure(self, db_path: Path) -> None:
        """rebuild INSERT(copy)失败→rollback→原表名/schema/行不变。"""
        con = _fresh_connect()
        con.executescript(_OLD_LB_R2)
        con.execute(
            "INSERT INTO leader_bindings VALUES('i','team','t1','a','bid1',1,'active',"
            "1.0,1,'m',0,0,0,0,0)"
        )
        con.commit(); con.close()
        # 注入 copy 失败（用 wrapper，sqlite3.Connection.execute 不可直接赋值）
        class FailOnCopy:
            def __init__(self, c):
                self._c = c
            def __getattr__(self, n):
                return getattr(self._c, n)
            def execute(self, sql, *a, **k):
                if "INSERT INTO leader_bindings" in sql and "_leader_bindings_old" in sql:
                    raise sqlite3.IntegrityError("injected copy failure")
                return self._c.execute(sql, *a, **k)
        con = FailOnCopy(_fresh_connect())
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            leader_binding._rebuild_leader_bindings(con)
        con.close()
        # rollback 后原表名/schema/行不变
        con = _fresh_connect()
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "leader_bindings" in tables
        assert "_leader_bindings_old" not in tables
        rows = con.execute("SELECT * FROM leader_bindings").fetchall()
        assert len(rows) == 1 and rows[0]["mail_name"] == "a"
        pk = [r["name"] for r in con.execute("PRAGMA table_info(leader_bindings)") if int(r["pk"]) > 0]
        assert "issuer" not in pk  # 旧 PK 未变（rollback 成功）
        con.close()

    def test_connect_closes_on_runtime_error(self, db_path, monkeypatch) -> None:
        """_connect 初始化 RuntimeError 时关闭连接（#1860 非 OperationalError close）。"""
        held = []
        def fake_init(con):
            held.append(con)
            raise RuntimeError("init boom")
        monkeypatch.setattr(leader_binding, "_initialize_connection", fake_init)
        with pytest.raises(RuntimeError, match="init boom"):
            leader_binding._connect()
        if held:
            with pytest.raises(Exception):
                held[0].execute("SELECT 1")


class TestR5ControlEventsSeqOrder:
    def test_seq_in_created_ts_event_id_order(self, db_path: Path) -> None:
        """旧 control_events 乱序→rebuild 后 seq 按 created_ts, event_id 稳定分配。"""
        con = _fresh_connect()
        con.executescript(_OLD_LB_R2 + _OLD_CE_R2)
        con.executescript(
            "INSERT INTO control_events VALUES('eid_z','i','team','t1','x',1,NULL,'{}',3.0,0);"
            "INSERT INTO control_events VALUES('eid_a','i','team','t1','x',1,NULL,'{}',1.0,0);"
            "INSERT INTO control_events VALUES('eid_m','i','team','t1','x',1,NULL,'{}',2.0,0);"
        )
        con.commit(); con.close()
        leader_binding._connect().close()
        con = _fresh_connect()
        rows = con.execute("SELECT seq, event_id FROM control_events ORDER BY seq").fetchall()
        con.close()
        assert [r["event_id"] for r in rows] == ["eid_a", "eid_m", "eid_z"]
        assert [r["seq"] for r in rows] == [1, 2, 3]

    def test_same_timestamp_tiebreak_by_event_id(self, db_path: Path) -> None:
        con = _fresh_connect()
        con.executescript(_OLD_LB_R2 + _OLD_CE_R2)
        con.executescript(
            "INSERT INTO control_events VALUES('zebra','i','team','t1','x',1,NULL,'{}',5.0,0);"
            "INSERT INTO control_events VALUES('alpha','i','team','t1','x',1,NULL,'{}',5.0,0);"
            "INSERT INTO control_events VALUES('mid','i','team','t1','x',1,NULL,'{}',5.0,0);"
        )
        con.commit(); con.close()
        leader_binding._connect().close()
        con = _fresh_connect()
        rows = con.execute("SELECT seq, event_id FROM control_events ORDER BY seq").fetchall()
        con.close()
        assert [r["event_id"] for r in rows] == ["alpha", "mid", "zebra"]

    def test_after_seq_restart_paging(self, db_path: Path) -> None:
        con = _fresh_connect()
        con.executescript(_OLD_LB_R2 + _OLD_CE_R2)
        con.executescript(
            "INSERT INTO control_events VALUES('e1','i','team','t1','x',1,NULL,'{}',1.0,0);"
            "INSERT INTO control_events VALUES('e2','i','team','t1','x',1,NULL,'{}',2.0,0);"
            "INSERT INTO control_events VALUES('e3','i','team','t1','x',1,NULL,'{}',3.0,0);"
        )
        con.commit(); con.close()
        leader_binding._connect().close()
        page2 = leader_binding.list_control_events("i", "team", "t1", after_seq=1, limit=10)
        assert len(page2) == 2
        assert [e["seq"] for e in page2] == [2, 3]
