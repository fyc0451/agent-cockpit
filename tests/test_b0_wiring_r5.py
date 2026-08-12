"""test_b0_wiring_r5.py — R5 DB 边界故障注入红转绿（#2094）。

P0-1 mail：外部 accept 后 delivered-mark crash → 不得 catch 掩盖；重启不得
重 prompt（attempt-without-delivered = 不确定，按已投处理并可观测）。
P0-2 control：transport 成功 + mark crash → 重启不得重 prompt/重发；
Hub send 必须携稳定 event_id 幂等键；恢复后仅补 mark。
P0-3 claim 服务端 binding_version fail-closed 门（stable reason + 不得靠
prompt 指令）。
P0-4 drained→retired 闭环 + retired 行 selector 清除。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_cockpit import b0_wiring
from agent_cockpit import coordination
from agent_cockpit import leader_binding

from tests.test_b0_wiring import (
    AUTH, ISSUER, _RecordingAdapter, _make_coordinator, _msg, _stub_fetch,
    _write_identity, binding_db, client, registry,  # noqa: F401  fixtures
)


@pytest.fixture(autouse=True)
def restore_claim_gate():
    previous = coordination.CONTROL_CLAIM_GATE
    try:
        yield
    finally:
        coordination.CONTROL_CLAIM_GATE = previous


# ---------------------------------------------------------------------------
# P0-1：accept → delivered-mark crash
# ---------------------------------------------------------------------------

def test_mail_mark_crash_after_accept_no_restart_reprompt(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry, accept=True)
    _stub_fetch(monkeypatch, [_msg(91)])
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")

    real_set = b0_wiring.set_prompt_marker

    def crash_on_delivered(pk, mn, mid, phase):
        if phase == "delivered":
            raise RuntimeError("simulated DB crash")
        return real_set(pk, mn, mid, phase)

    monkeypatch.setattr(b0_wiring, "set_prompt_marker", crash_on_delivered)
    coord.poll_once()
    assert len(adapter.calls) == 1  # 外部已 accept
    # 不得把不确定投递当成 delivered（catch 掩盖 = 禁止）
    assert coord.core.state(scope)["delivered_count"] == 0

    monkeypatch.setattr(b0_wiring, "set_prompt_marker", real_set)
    # 重启：attempt-without-delivered = 不确定 → 不得重 prompt
    adapter2 = _RecordingAdapter(True)
    coord2 = b0_wiring.B0Coordinator(adapter2, ISSUER)
    coord2.sync_bindings()
    coord2.set_target_status(scope, "ready")
    coord2.rebuild()
    assert len(adapter2.calls) == 0, "accept→mark crash 后重启重复 prompt"


def test_mail_normal_accept_still_durable(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry, accept=True)
    _stub_fetch(monkeypatch, [_msg(92)])
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")
    coord.poll_once()
    assert coord.core.state(scope)["delivered_count"] == 1
    adapter2 = _RecordingAdapter(True)
    coord2 = b0_wiring.B0Coordinator(adapter2, ISSUER)
    coord2.sync_bindings()
    coord2.set_target_status(scope, "ready")
    coord2.rebuild()
    assert len(adapter2.calls) == 0


# ---------------------------------------------------------------------------
# P0-2：control transport 幂等 + mark crash 恢复
# ---------------------------------------------------------------------------

def test_control_mark_crash_recovery_idempotent(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry, accept=True)
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")
    events = leader_binding.undelivered_control_events(ISSUER)
    assert events

    transports: list[dict[str, Any]] = []

    def transport(event):
        transports.append(event)
        return True

    real_mark = leader_binding.mark_event_fanned_out

    def crash_mark(issuer, event_id):
        raise RuntimeError("simulated mark crash")

    monkeypatch.setattr(leader_binding, "mark_event_fanned_out", crash_mark)
    coord.fanout_control_events(transport=transport)
    assert len(transports) == 1
    assert len(adapter.calls) == 1  # 本地已投递
    assert leader_binding.undelivered_control_events(ISSUER), "mark 崩溃不得丢"

    monkeypatch.setattr(leader_binding, "mark_event_fanned_out", real_mark)
    # 重启（新协调器）：不得重 prompt、不得重发 transport，只补 mark
    adapter2 = _RecordingAdapter(True)
    coord2 = b0_wiring.B0Coordinator(adapter2, ISSUER)
    coord2.sync_bindings()
    coord2.set_target_status(scope, "ready")
    coord2.fanout_control_events(transport=transport)
    assert len(transports) == 1, "transport 必须按稳定 event_id 幂等"
    assert len(adapter2.calls) == 0, "control 事件重启不得重 prompt"
    assert leader_binding.undelivered_control_events(ISSUER) == []


def test_control_message_carries_stable_event_id(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_coordinator(binding_db, registry)
    event = leader_binding.undelivered_control_events(ISSUER)[0]
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        b0_wiring, "active_run_participants",
        lambda: [("/tmp/proj-x", "dev-a")],
    )
    from agent_cockpit import hub_client
    monkeypatch.setattr(
        hub_client, "send_message",
        lambda **kw: calls.append(kw) or {"deliveries": []},
    )
    ok = b0_wiring.send_control_message_to_participants(
        event, sender_identity={
            "name": "agent-a", "registration_token": "rt-a",
            "project_key": "/tmp/proj-x",
        },
    )
    assert ok and calls
    blob = calls[0]["subject"] + calls[0]["body_md"]
    assert str(event["event_id"]) in blob, "Hub control message 必须携稳定 event_id 幂等键"


# ---------------------------------------------------------------------------
# P0-3：claim 服务端 binding_version fail-closed 门
# ---------------------------------------------------------------------------

def test_claim_gate_rejects_stale_binding_version(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_coordinator(binding_db, registry)  # active v1
    monkeypatch.setattr(b0_wiring, "B0_ISSUER", ISSUER)
    b0_wiring.install_claim_gate()
    msg = {
        "id": 501, "from": "agent-a", "subject": "stop",
        "body_md": coordination.add_metadata("", {
            "intent": "stop", "binding_issuer": ISSUER,
            "binding_scope_kind": "user", "binding_scope_id": "default",
            "binding_version": 99,
        }),
        "kind": "message", "importance": "normal", "created_ts": 1.0,
    }
    result = coordination.claim_message(
        project_key="/tmp/proj-x", recipient="agent-x", message=msg,
        claimant="t", cwd="/tmp",
    )
    assert result.get("deliver") is False
    assert result.get("reason") == b0_wiring.REASON_STALE_BINDING_VERSION
    receipt = coordination.receipt("/tmp/proj-x", "agent-x", 501)
    assert receipt is not None and receipt["state"] == "stale"
    assert receipt["reason"] == b0_wiring.REASON_STALE_BINDING_VERSION


def test_claim_gate_missing_version_fail_closed(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_coordinator(binding_db, registry)
    monkeypatch.setattr(b0_wiring, "B0_ISSUER", ISSUER)
    b0_wiring.install_claim_gate()
    msg = {
        "id": 502, "from": "agent-a", "subject": "stop",
        "body_md": coordination.add_metadata("", {
            "intent": "stop", "binding_issuer": ISSUER,
            "binding_scope_kind": "user", "binding_scope_id": "default",
        }),
        "kind": "message", "importance": "normal", "created_ts": 1.0,
    }
    result = coordination.claim_message(
        project_key="/tmp/proj-x", recipient="agent-x", message=msg,
        claimant="t", cwd="/tmp",
    )
    assert result.get("deliver") is False, "存在 active binding 时控制消息缺版本必须拒绝"


def test_claim_gate_matching_version_passes(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_coordinator(binding_db, registry)
    active = leader_binding.get_active_binding(ISSUER, "user", "default")
    monkeypatch.setattr(b0_wiring, "B0_ISSUER", ISSUER)
    b0_wiring.install_claim_gate()
    msg = {
        "id": 503, "from": "agent-a", "subject": "stop",
        "body_md": coordination.add_metadata("", {
            "intent": "stop", "binding_issuer": ISSUER,
            "binding_scope_kind": "user", "binding_scope_id": "default",
            "binding_version": int(active["binding_version"]),
        }),
        "kind": "message", "importance": "normal", "created_ts": 1.0,
    }
    result = coordination.claim_message(
        project_key="/tmp/proj-x", recipient="agent-x", message=msg,
        claimant="t", cwd="/tmp",
    )
    assert result.get("deliver") is True


def test_claim_gate_no_binding_allows(
    binding_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(b0_wiring, "B0_ISSUER", ISSUER)
    b0_wiring.install_claim_gate()
    msg = {
        "id": 504, "from": "leader-a", "subject": "stop",
        "body_md": coordination.add_metadata("", {"intent": "stop"}),
        "kind": "message", "importance": "normal", "created_ts": 1.0,
    }
    result = coordination.claim_message(
        project_key="/tmp/proj-x", recipient="agent-x", message=msg,
        claimant="t", cwd="/tmp",
    )
    assert result.get("deliver") is True, "无 active binding 时保持旧行为"


# ---------------------------------------------------------------------------
# P0-4：drained → retired 闭环 + selector 清除
# ---------------------------------------------------------------------------

def test_drain_retire_closure_clears_selector(
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
    monkeypatch.setattr(b0_wiring, "fetch_inbox_for", lambda identity, **kw: [])
    monkeypatch.setattr(
        b0_wiring.coordination, "receipt", lambda *a, **kw: None,
    )
    coord.poll_once()
    prev = leader_binding.get_binding(ISSUER, "user", "default", "agent-a")
    assert prev["state"] == "retired", "drained 必须闭环到 retired"
    assert prev["previous_state"] == "drained"
    assert not prev["registry_selector"], "retire 后必须清除 previous selector"
