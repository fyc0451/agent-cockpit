"""test_b0_wiring_r2.py — R2 反向 barrier（#2062 REVIEW_BLOCK 反例回归）。

四个确定性反例必须先失败后修复：
1. active/previous 返回同一 Hub message id → 只允许一次 prompt（去重键是
   Hub 全局 message_id，不得带 mail_name）。
2. active credential 失败、previous 成功 → scope 仍 degraded，不得被 pop。
3. prompt 已 accept 但未 claim 时重启 → 同一 unread 不得再次 prompt
   （durable delivery ledger）。
4. control event working 时 fanout 后 crash（未投递即 mark）→ 重建后必须
   仍可投递，不得永久丢。

三个合同补齐的 barrier：祖先目录门、cross_run_fail_fast 发送端、
B1 capability_digest、4s 显式 flush、previous drain CAS 闭环。
"""
from __future__ import annotations

import hashlib
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
# 反例 1：同 Hub message id 跨身份只投一次
# ---------------------------------------------------------------------------

def test_same_hub_id_across_identities_single_prompt(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry)
    _write_identity(registry, "proj-x/b--main.json", name="agent-b", token="rt-b")
    leader_binding.bind_leader(
        ISSUER, "user", "default", mail_name="agent-b",
        session="sess-2", pane_id="pane-2",
        registry_selector="proj-x/b--main.json", expected_version=1,
    )
    coord.sync_bindings()

    def fake_fetch(identity: dict[str, Any], **kw: Any) -> list[dict[str, Any]]:
        # 同一 Hub 消息（id=7）在两个身份都可见
        return [_msg(7)]

    monkeypatch.setattr(b0_wiring, "fetch_inbox_for", fake_fetch)
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")
    stats = coord.poll_once()
    assert stats["ingested"] == 1, stats
    delivered = [
        ev for call in adapter.calls for ev in call[2]
    ]
    assert len(delivered) == 1
    assert "mail:" in delivered[0].event_id and ":7" in delivered[0].event_id
    # 不得出现按 mail_name 区分的两个事件
    assert len({ev.event_id for ev in delivered}) == 1


# ---------------------------------------------------------------------------
# 反例 2：active 失败 + previous 成功 → degraded 保留
# ---------------------------------------------------------------------------

def test_partial_degraded_not_cleared(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry)
    _write_identity(registry, "proj-x/b--main.json", name="agent-b", token="rt-b")
    leader_binding.bind_leader(
        ISSUER, "user", "default", mail_name="agent-b",
        session="sess-2", pane_id="pane-2",
        registry_selector="proj-x/b--main.json", expected_version=1,
    )
    coord.sync_bindings()
    # active(agent-b) selector 删除 → 失败；previous(agent-a) 正常
    (registry / "proj-x" / "b--main.json").unlink()

    def fake_fetch(identity: dict[str, Any], **kw: Any) -> list[dict[str, Any]]:
        return [_msg(11)] if identity["name"] == "agent-a" else []

    monkeypatch.setattr(b0_wiring, "fetch_inbox_for", fake_fetch)
    stats = coord.poll_once()
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    assert stats["degraded"] >= 1
    assert coord.degraded.get(scope) == b0_wiring.REASON_CREDENTIAL_UNAVAILABLE


# ---------------------------------------------------------------------------
# 反例 3：durable delivery ledger（重启不重 prompt）
# ---------------------------------------------------------------------------

def test_restart_does_not_reprompt_delivered(
    binding_db: Path, registry: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / "b0-ledger.sqlite3"
    coord, adapter = _make_coordinator(binding_db, registry)
    _stub_fetch(monkeypatch, [_msg(21)])
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")
    coord.poll_once()
    assert len(adapter.calls) == 1  # 首次投递 accept
    delivered = coord.core.state(scope)["delivered_count"]
    assert delivered == 1

    # 模拟 crash/重启：全新协调器 + 同一 Hub unread（未 claim）
    adapter2 = _RecordingAdapter(True)
    coord2 = b0_wiring.B0Coordinator(adapter2, ISSUER)
    coord2.sync_bindings()
    coord2.set_target_status(scope, "ready")
    coord2.rebuild()
    assert len(adapter2.calls) == 0, "重启后不得对已投递事件再次 prompt"


# ---------------------------------------------------------------------------
# 反例 4：fanout crash 重建不得永久丢
# ---------------------------------------------------------------------------

def test_fanout_crash_rebuild_redelivers(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry, accept=False)
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "working")
    events = leader_binding.undelivered_control_events(ISSUER)
    assert events
    coord.fanout_control_events()
    # adapter 拒绝 + 进程 crash（内存 pending 全失）；但 outbox 不应提前 mark
    # 重建后（新协调器）必须仍能投递这些事件
    adapter2 = _RecordingAdapter(True)
    coord2 = b0_wiring.B0Coordinator(adapter2, ISSUER)
    coord2.sync_bindings()
    coord2.set_target_status(scope, "ready")
    coord2.fanout_control_events()
    delivered_types = {
        ev.summary.get("kind") for call in adapter2.calls for ev in call[2]
    }
    assert "control_event" in delivered_types, "crash 重建后 control event 永久丢失"


# ---------------------------------------------------------------------------
# 合同：祖先目录门
# ---------------------------------------------------------------------------

def test_selector_ancestor_dir_symlink_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = tmp_path / "real-proj"
    real.mkdir()
    root = tmp_path / "registry2"
    root.mkdir()
    (root / "proj-y").symlink_to(real)  # 祖先目录是 symlink
    monkeypatch.setattr(b0_wiring, "REGISTRY_DIR", root)
    monkeypatch.setattr(hub_client, "HUB", "http://127.0.0.1:8765")
    monkeypatch.setattr(hub_client, "TOKEN", "client-token")
    p = real / "a--main.json"
    p.write_text(json.dumps({
        "project_key": "/tmp/proj-y", "name": "agent-y",
        "registration_token": "rt-y", "hub": "http://127.0.0.1:8765",
    }))
    os.chmod(p, 0o600)
    with pytest.raises(b0_wiring.CredentialUnavailable):
        b0_wiring.resolve_selector("proj-y/a--main.json")


# ---------------------------------------------------------------------------
# 合同：cross_run_fail_fast（发送端）
# ---------------------------------------------------------------------------

def test_cross_run_fail_fast_sender_side() -> None:
    def active_context(project_key: str, recipient: str):
        if recipient == "same-run":
            return {"run_id": "run-1"}
        return {"run_id": "run-2"}

    with pytest.raises(b0_wiring.CrossRunFailFast):
        b0_wiring.cross_run_fail_fast(
            "p", "sender", ["same-run", "other-run"],
            sender_run_id="run-1", active_context=active_context,
        )
    # 显式 run-independent 放行
    b0_wiring.cross_run_fail_fast(
        "p", "sender", ["other-run"],
        sender_run_id="run-1", active_context=active_context,
        run_independent=True,
    )
    # 同 run 放行
    b0_wiring.cross_run_fail_fast(
        "p", "sender", ["same-run"],
        sender_run_id="run-1", active_context=active_context,
    )


# ---------------------------------------------------------------------------
# 合同：B1 capability_digest（非用户路径需能力证明）
# ---------------------------------------------------------------------------

def _digest_of_selector(registry: Path, rel: str) -> str:
    data = json.loads((registry / rel).read_text())
    return hashlib.sha256(
        str(data["registration_token"]).encode("utf-8"),
    ).hexdigest()


def test_rebind_leader_requires_capability_digest(
    client, registry: Path,
) -> None:
    # 首绑（用户路径，Bearer）建立 active
    _write_identity(registry, "proj-x/n--main.json", name="leader-new")
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={
            "mail_name": "leader-new", "expected_version": 0,
            "registry_selector": "proj-x/n--main.json",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    digest = _digest_of_selector(registry, "proj-x/n--main.json")
    body = {
        "mail_name": "leader-new", "expected_version": 1,
        "pane_id": "pane-77",
        "registry_selector": "proj-x/n--main.json",
        "caller_mail_name": "leader-new",
    }
    # 非用户路径 + 无 capability → 403
    resp = client.post("/api/binding/user/default/rebind", json=body)
    assert resp.status_code == 403, resp.text
    # 错误 digest → 403
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={**body, "capability_digest": "0" * 64},
    )
    assert resp.status_code == 403, resp.text
    # 正确 digest → 200，actor=active_leader
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={**body, "capability_digest": digest},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["actor"] == "active_leader"


# ---------------------------------------------------------------------------
# 合同：4s 显式 flush
# ---------------------------------------------------------------------------

def test_full_flush_explicitly_flushes_pending(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry, accept=False)
    _stub_fetch(monkeypatch, [_msg(31)])
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")
    coord.poll_once()
    assert coord.core.state(scope)["pending_count"] == 1
    assert coord.core.state(scope)["delivered_count"] == 0
    adapter.accept = True
    # 越过 4s 窗口后的 poll 必须显式 flush（无需状态翻转）
    monkeypatch.setattr(
        coord, "_last_full_flush", time.monotonic() - coord._flush_interval - 1,
    )
    coord.poll_once()
    assert coord.core.state(scope)["pending_count"] == 0
    assert coord.core.state(scope)["delivered_count"] == 1


# ---------------------------------------------------------------------------
# 合同：previous drain CAS 闭环
# ---------------------------------------------------------------------------

def test_drain_closes_when_previous_empty(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry)
    _write_identity(registry, "proj-x/b--main.json", name="agent-b", token="rt-b")
    leader_binding.bind_leader(
        ISSUER, "user", "default", mail_name="agent-b",
        session="sess-2", pane_id="pane-2",
        registry_selector="proj-x/b--main.json", expected_version=1,
    )
    coord.sync_bindings()

    def fake_fetch(identity: dict[str, Any], **kw: Any) -> list[dict[str, Any]]:
        return []  # previous 邮箱已无在途

    monkeypatch.setattr(b0_wiring, "fetch_inbox_for", fake_fetch)
    monkeypatch.setattr(
        b0_wiring.coordination, "receipt", lambda *a, **kw: {"state": "processed"},
    )
    coord.poll_once()
    prev = leader_binding.get_binding(ISSUER, "user", "default", "agent-a")
    assert prev["previous_state"] == "drained", prev["previous_state"]
    # drain_state_changed 审计事件已写
    events = leader_binding.list_control_events(issuer=ISSUER)
    assert any(e["event_type"] == "drain_state_changed" for e in events)
