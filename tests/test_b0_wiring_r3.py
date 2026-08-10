"""test_b0_wiring_r3.py — R3 Lead probes（#2078 REVIEW_BLOCK 红转绿）。

P0-1 鉴权：bogus cookie 不得视为 user；空 token 非回环拒绝；active Leader
走一次性/有时效 grant，禁止长期 digest。
P0-2 fanout：未 durable delivered（delivered_count=0）不得 mark，1→1→1。
P0-3 api_send 发送端必须调用 cross_run_fail_fast（真接线）。
P0-4 不得存在第二 outbox（ledger 废除）；重启语义以 receipts 权威。
P1-5 祖先目录 0775（即使属主为当前 uid）拒绝；0700 放行。
P1-6 drain 只认真实排空证明（Hub 空 + receipt 计数全零）。
P1-7 sync_bindings cleared handoff 不丢；G6 面板重绑。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

import b0_wiring
import hub_client
import leader_binding

from tests.test_b0_wiring import (
    AUTH, ISSUER, _RecordingAdapter, _make_coordinator, _msg, _stub_fetch,
    _write_identity, binding_db, client, registry,  # noqa: F401  fixtures
)


# ---------------------------------------------------------------------------
# P0-1：鉴权
# ---------------------------------------------------------------------------

def test_rebind_bogus_cookie_not_user(client) -> None:
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={"mail_name": "x", "expected_version": 0},
        headers={"cookie": "cockpit_session=bogus"},
    )
    assert resp.status_code in (401, 403), resp.text


def test_rebind_nonloopback_without_token_rejected(
    binding_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "B0_ISSUER", ISSUER)
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    c = TestClient(server.app)  # client.host=testclient（非回环）
    resp = c.post(
        "/api/binding/user/default/rebind",
        json={"mail_name": "x", "expected_version": 0},
    )
    assert resp.status_code in (401, 403), resp.text


def test_rebind_leader_one_time_grant(client, registry: Path) -> None:
    _write_identity(registry, "proj-x/n--main.json", name="leader-new")
    # 用户建立首绑
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={
            "mail_name": "leader-new", "expected_version": 0,
            "registry_selector": "proj-x/n--main.json",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    active = leader_binding.get_active_binding(ISSUER, "user", "default")
    version = int(active["binding_version"])
    # 用户签发一次性 grant
    resp = client.post(
        "/api/binding/user/default/rebind-grant",
        json={"mail_name": "leader-new"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    grant = resp.json()["grant_token"]
    # Leader 用 grant 改绑（无用户凭据）
    body = {
        "mail_name": "leader-new", "expected_version": version,
        "pane_id": "pane-g1",
        "registry_selector": "proj-x/n--main.json",
        "caller_mail_name": "leader-new", "grant_token": grant,
    }
    resp = client.post(
        "/api/binding/user/default/rebind", json=body,
        headers={"x-b0-grant-token": grant},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["actor"] == "active_leader"
    # grant 单次使用：第二次拒绝
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={**body, "expected_version": version + 1},
        headers={"x-b0-grant-token": grant},
    )
    assert resp.status_code in (401, 403), resp.text
    # 无 grant 的 leader 自称拒绝
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={
            "mail_name": "leader-new", "expected_version": version + 1,
            "caller_mail_name": "leader-new",
        },
    )
    assert resp.status_code in (401, 403), resp.text


def test_rebind_grant_wrong_mail_name_rejected(client, registry: Path) -> None:
    _write_identity(registry, "proj-x/n--main.json", name="leader-new")
    client.post(
        "/api/binding/user/default/rebind",
        json={
            "mail_name": "leader-new", "expected_version": 0,
            "registry_selector": "proj-x/n--main.json",
        },
        headers=AUTH,
    )
    resp = client.post(
        "/api/binding/user/default/rebind-grant",
        json={"mail_name": "leader-new"},
        headers=AUTH,
    )
    grant = resp.json()["grant_token"]
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={
            "mail_name": "leader-new", "expected_version": 1,
            "caller_mail_name": "impostor", "grant_token": grant,
        },
        headers={"x-b0-grant-token": grant},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# P0-2：fanout 仅在 delivered 证明后 mark（1→1→1→0）
# ---------------------------------------------------------------------------

def test_fanout_no_mark_without_delivery_proof(
    binding_db: Path, registry: Path,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry)
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "working")
    assert len(leader_binding.undelivered_control_events(ISSUER)) == 1
    # 第 1 轮：pending，不 mark
    assert coord.fanout_control_events() == 0
    assert len(leader_binding.undelivered_control_events(ISSUER)) == 1
    # 第 2 轮：core duplicate（delivered_count=0），仍不 mark（R2 的洞）
    assert coord.fanout_control_events() == 0
    assert len(leader_binding.undelivered_control_events(ISSUER)) == 1
    assert coord.core.state(scope)["delivered_count"] == 0
    # 变为 eligible：投递成功产生 durable 证明后才 mark
    coord.set_target_status(scope, "ready")
    assert coord.core.state(scope)["delivered_count"] >= 1
    coord.fanout_control_events()
    assert leader_binding.undelivered_control_events(ISSUER) == []


# ---------------------------------------------------------------------------
# P0-3：api_send 发送端 fail-fast（真接线）
# ---------------------------------------------------------------------------

@pytest.fixture()
def send_client(binding_db: Path, monkeypatch: pytest.MonkeyPatch):
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(server, "_agent_mail_status", lambda: {
        "write_available": True, "write_reason": None,
    })

    class _Db:
        @staticmethod
        def project_by_id(pid):
            return {"human_key": "/tmp/proj-s"} if pid == 1 else None

        @staticmethod
        def agent_by_name(pid, name):
            return (
                {"name": name, "registration_token": "rt-stub"}
                if name == "sender-a" else None
            )

        def __getattr__(self, name):
            import db as real_db
            return getattr(real_db, name)

    monkeypatch.setattr(server, "db", _Db())
    monkeypatch.setattr(
        server.coordination, "prepare_metadata",
        lambda **kw: ({"run_id": "run-1"}, []),
    )
    monkeypatch.setattr(
        server.coordination, "add_metadata", lambda body, meta: body,
    )
    sent = []
    monkeypatch.setattr(
        server.hub_client, "send_message",
        lambda **kw: sent.append(kw) or {"message_id": 1},
    )
    contexts = {
        ("sender-a",): {"run_id": "run-1"},
        ("same-run",): {"run_id": "run-1"},
        ("other-run",): {"run_id": "run-2"},
    }

    def active_context(project_key, recipient):
        return contexts.get((recipient,))

    monkeypatch.setattr(server.coordination, "active_context", active_context)
    return TestClient(server.app), sent


def test_api_send_cross_run_fail_fast_wired(send_client) -> None:
    c, sent = send_client
    resp = c.post(
        "/api/send",
        json={
            "project_id": 1, "sender_name": "sender-a",
            "to": ["same-run", "other-run"], "subject": "s", "body": "b",
        },
        headers=AUTH,
    )
    assert resp.status_code == 409, resp.text
    assert "cross_run_fail_fast" in resp.text
    assert sent == []  # 发送前失败，零半成功


def test_api_send_run_independent_escape(send_client) -> None:
    c, sent = send_client
    resp = c.post(
        "/api/send",
        json={
            "project_id": 1, "sender_name": "sender-a",
            "to": ["other-run"], "subject": "s", "body": "b",
            "run_independent": True,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert len(sent) == 1


def test_api_send_same_run_passes(send_client) -> None:
    c, sent = send_client
    resp = c.post(
        "/api/send",
        json={
            "project_id": 1, "sender_name": "sender-a",
            "to": ["same-run"], "subject": "s", "body": "b",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# P0-4：无第二 outbox；重启以 receipts 为权威
# ---------------------------------------------------------------------------

def test_no_second_outbox_module() -> None:
    assert not hasattr(b0_wiring, "B0DeliveryLedger")
    assert not hasattr(b0_wiring, "LEDGER_PATH")


def test_restart_skip_uses_receipt_authority(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import coordination

    coord, adapter = _make_coordinator(binding_db, registry)
    _stub_fetch(monkeypatch, [_msg(41)])
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")
    coord.poll_once()
    assert len(adapter.calls) == 1
    # receipts（权威）中该消息已带投递标记
    receipt = coordination.receipt("/tmp/proj-x", "agent-a", 41)
    assert receipt is not None
    assert "b0_prompted" in str(receipt.get("checkpoint_json") or "")

    # 重启：新协调器，同一 unread（未 claim）→ 不得再次 prompt
    adapter2 = _RecordingAdapter(True)
    coord2 = b0_wiring.B0Coordinator(adapter2, ISSUER)
    coord2.sync_bindings()
    coord2.set_target_status(scope, "ready")
    coord2.rebuild()
    assert len(adapter2.calls) == 0


# ---------------------------------------------------------------------------
# P1-5：祖先目录 0700/属主门
# ---------------------------------------------------------------------------

def test_ancestor_dir_0775_rejected_even_own_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registry3"
    (root / "proj-z").mkdir(parents=True)
    os.chmod(root, 0o700)
    os.chmod(root / "proj-z", 0o775)  # 属主是当前 uid 也拒绝
    monkeypatch.setattr(b0_wiring, "REGISTRY_DIR", root)
    monkeypatch.setattr(hub_client, "HUB", "http://127.0.0.1:8765")
    monkeypatch.setattr(hub_client, "TOKEN", "client-token")
    p = root / "proj-z" / "a--main.json"
    p.write_text(json.dumps({
        "project_key": "/tmp/proj-z", "name": "z",
        "registration_token": "rt-z", "hub": "http://127.0.0.1:8765",
    }))
    os.chmod(p, 0o600)
    with pytest.raises(b0_wiring.CredentialUnavailable):
        b0_wiring.resolve_selector("proj-z/a--main.json")
    # 收紧 0700 后放行
    os.chmod(root / "proj-z", 0o700)
    ident = b0_wiring.resolve_selector("proj-z/a--main.json")
    assert ident["name"] == "z"


# ---------------------------------------------------------------------------
# P1-6：drain 只认真实排空证明
# ---------------------------------------------------------------------------

def test_drain_requires_real_proof(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import coordination

    coord, adapter = _make_coordinator(binding_db, registry)
    _write_identity(registry, "proj-x/b--main.json", name="agent-b", token="rt-b")
    leader_binding.bind_leader(
        ISSUER, "user", "default", mail_name="agent-b",
        session="sess-2", pane_id="pane-2",
        registry_selector="proj-x/b--main.json", expected_version=1,
    )
    coord.sync_bindings()
    # 场景 A：previous Hub 仍有在途 → 不得 drain
    monkeypatch.setattr(
        b0_wiring, "fetch_inbox_for",
        lambda identity, **kw: [_msg(51)] if identity["name"] == "agent-a" else [],
    )
    coord.poll_once()
    prev = leader_binding.get_binding(ISSUER, "user", "default", "agent-a")
    assert prev["previous_state"] in ("pending", "draining")
    # 场景 B：Hub 空但 receipt 有未 processed 认领 → 不得 drain
    con = coordination._connect()
    now = time.time()
    con.execute(
        "INSERT INTO receipts(project_key,recipient,message_id,intent,"
        "importance,state,ack_pending,created_ts,updated_ts) "
        "VALUES('/tmp/proj-x','agent-a',51,'info','normal','claimed',1,?,?)",
        (now, now),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(
        b0_wiring, "fetch_inbox_for", lambda identity, **kw: [],
    )
    coord.poll_once()
    prev = leader_binding.get_binding(ISSUER, "user", "default", "agent-a")
    assert prev["previous_state"] in ("pending", "draining")
    # 场景 C：receipt 全 processed + Hub 空 → 真实计数全零才 drain
    con = coordination._connect()
    con.execute(
        "UPDATE receipts SET state='processed', ack_pending=0 "
        "WHERE recipient='agent-a'",
    )
    con.commit()
    con.close()
    coord.poll_once()
    prev = leader_binding.get_binding(ISSUER, "user", "default", "agent-a")
    assert prev["previous_state"] == "drained"


# ---------------------------------------------------------------------------
# P1-7：cleared handoff 与 G6
# ---------------------------------------------------------------------------

def test_sync_bindings_cleared_handoff_not_lost(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry, accept=True)
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "working")
    _stub_fetch(monkeypatch, [_msg(61)])
    coord.poll_once()
    assert coord.core.state(scope)["pending_count"] == 1
    # 改绑（新 mail_name）→ 版本切换，cleared 事件必须 handoff 不丢
    leader_binding.bind_leader(
        ISSUER, "user", "default", mail_name="agent-b",
        session="sess-2", pane_id="pane-2",
        registry_selector="proj-x/a--main.json", expected_version=1,
    )
    coord.sync_bindings()
    coord.set_target_status(scope, "ready")
    delivered = [ev for call in adapter.calls for ev in call[2]]
    assert any(
        ev.summary.get("message_id") == 61 and ev.binding_version == 2
        for ev in delivered
    ), "cleared 事件未 handoff 到新 binding 版本"


def test_g6_pane_rebind_by_mail_name(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    _make_coordinator(binding_db, registry)
    monkeypatch.setattr(server, "B0_ISSUER", ISSUER)
    monkeypatch.setattr(server, "_b0_coordinator", None)
    active = leader_binding.get_active_binding(ISSUER, "user", "default")
    assert active["pane_id"] == "pane-1"
    route_epoch = int(active["route_epoch"])
    # 同名 agent 以新 pane 重启：snap 只有新 pane_id
    snap = {
        "available": True,
        "panes": [{
            "session": "sess-9", "pane_id": "pane-NEW",
            "mail_name": "agent-a", "agent_status": "ready",
        }],
    }
    server._b0_apply_live_status(snap)
    row = leader_binding.get_active_binding(ISSUER, "user", "default")
    assert row["pane_id"] == "pane-NEW"
    assert row["session"] == "sess-9"
    assert int(row["route_epoch"]) == route_epoch + 1  # binding_updated
    assert int(row["binding_version"]) == int(active["binding_version"]) + 1
