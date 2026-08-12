"""test_b0_wiring_r4.py — R4 Lead blockers 红转绿（#2086）。

B1 durable 标记与真实 accept 闭合（拒绝后重启必须补投，不得永久漏）。
B2 F6/F7：fanout 必须向 active run 参与者发送可 claim、携 binding version
   的 Hub control message（transport 真存在）。
B3 W6：canonical 授权门必须在 hard C-c 之前；非 canonical 不得 stop/redirect。
B4 rebind 预校验：selector 缺失/identity 名不符/非本地 Hub/pane 缺失 →
   CAS 前零变更。
B5 fetch_inbox 网络前 loopback fail-closed（远端 Hub 不得收到任何 capability）。
B6 scope_key 单射；created_ts 字符串不得毒化去重。
P1-7 G6 同 mail_name 多 pane 歧义时禁止自动改绑。
P1-8 真非回环 ASGI client 地址证明鉴权门。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from agent_cockpit import b0_wiring
from agent_cockpit import hub_client
from agent_cockpit import leader_binding

from tests.test_b0_wiring import (
    AUTH, ISSUER, _RecordingAdapter, _make_coordinator, _msg, _stub_fetch,
    _write_identity, binding_db, client, registry,  # noqa: F401  fixtures
)


# ---------------------------------------------------------------------------
# B1：拒绝后重启必须补投（durable 标记与 accept 闭合）
# ---------------------------------------------------------------------------

def test_reject_then_restart_reprompts(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry, accept=False)
    _stub_fetch(monkeypatch, [_msg(71)])
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")
    coord.poll_once()
    assert adapter.calls  # 尝试过但被拒绝（accept=False）
    assert coord.core.state(scope)["pending_count"] == 1

    # 重启：新协调器 accept=True，同一 unread 未 claim → 必须补投
    adapter2 = _RecordingAdapter(True)
    coord2 = b0_wiring.B0Coordinator(adapter2, ISSUER)
    coord2.sync_bindings()
    coord2.set_target_status(scope, "ready")
    coord2.rebuild()
    assert len(adapter2.calls) >= 1, "adapter 拒绝过的消息重启后永久丢失"


def test_accept_then_restart_still_no_reprompt(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry, accept=True)
    _stub_fetch(monkeypatch, [_msg(72)])
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")
    coord.poll_once()
    assert coord.core.state(scope)["delivered_count"] == 1
    adapter2 = _RecordingAdapter(True)
    coord2 = b0_wiring.B0Coordinator(adapter2, ISSUER)
    coord2.sync_bindings()
    coord2.set_target_status(scope, "ready")
    coord2.rebuild()
    assert len(adapter2.calls) == 0, "已 accept 的消息重启后不得重投"


# ---------------------------------------------------------------------------
# B2：F6/F7 Hub control message transport
# ---------------------------------------------------------------------------

def test_fanout_sends_hub_control_message_with_version(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry)
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")
    events = leader_binding.undelivered_control_events(ISSUER)
    assert events
    version = int(events[0]["binding_version"])

    sent: list[dict[str, Any]] = []

    def transport(event: dict[str, Any]) -> bool:
        sent.append(event)
        return True

    monkeypatch.setattr(
        b0_wiring, "active_run_participants",
        lambda: [("/tmp/proj-x", "dev-a"), ("/tmp/proj-x", "dev-b")],
    )
    n = coord.fanout_control_events(transport=transport)
    assert n == len(events)
    assert sent, "F6/F7 transport 未触发"
    ev = events[0]
    assert sent[0]["event_id"] == ev["event_id"]
    # transport 侧真实发送：Hub control message 携 binding version、可 claim、
    # 面向 active run 参与者，不附 task/revision
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        hub_client, "send_message",
        lambda **kw: calls.append(kw) or {"deliveries": []},
    )
    ok = b0_wiring.send_control_message_to_participants(
        ev, sender_identity={
            "name": "agent-a", "registration_token": "rt-a",
            "project_key": "/tmp/proj-x",
        },
    )
    assert ok and len(calls) == 1
    body = calls[0]["body_md"]
    assert f"v{version}" in body or str(version) in body
    assert "task" not in calls[0] and "revision" not in calls[0]
    assert sorted(calls[0]["to"]) == ["dev-a", "dev-b"]
    assert calls[0]["ack_required"] is True
    # 无参与者时空真（vacuous）成功
    monkeypatch.setattr(b0_wiring, "active_run_participants", lambda: [])
    assert b0_wiring.send_control_message_to_participants(
        ev, sender_identity={"name": "x", "registration_token": "t",
                             "project_key": "/tmp/p"},
    ) is True


def test_fanout_transport_failure_keeps_unmarked(
    binding_db: Path, registry: Path,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry)
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")
    assert leader_binding.undelivered_control_events(ISSUER)
    coord.fanout_control_events(transport=lambda event: False)
    assert leader_binding.undelivered_control_events(ISSUER), \
        "transport 失败不得 mark（可重试）"


# ---------------------------------------------------------------------------
# B3：W6 canonical 授权门前置于 hard C-c
# ---------------------------------------------------------------------------

def test_w6_blocks_noncanonical_stop_before_interrupt(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_cockpit import coordination
    import server

    _make_coordinator(binding_db, registry)  # active mail_name=agent-a
    monkeypatch.setattr(server, "B0_ISSUER", ISSUER)
    monkeypatch.setattr(server, "B0_MODE", "on")
    active = leader_binding.get_active_binding(ISSUER, "user", "default")
    control_meta = {
        "intent": "stop", "run_id": "run-1",
        "binding_issuer": ISSUER, "binding_scope_kind": "user",
        "binding_scope_id": "default",
        "binding_version": int(active["binding_version"]),
    }
    monkeypatch.setattr(
        coordination, "active_context",
        lambda pk, rc: {
            "run_id": "run-1", "session": "sess-1", "pane_id": "p1",
            "agent_type": "qodercn", "workdir": "/tmp",
        },
    )
    monkeypatch.setattr(
        coordination, "request_pause", lambda **kw: {"saved": True},
    )
    sends: list[tuple] = []
    monkeypatch.setattr(
        server.herdr_client, "pane_send",
        lambda session, pane, text, mode: sends.append(
            (session, pane, text, mode)) or {"available": True},
    )
    # 非 canonical 发送者尝试 hard stop：必须拒绝且不发 C-c
    result = server._notify_coordination_message(
        "/tmp/proj-x", "agent-x", 900, "stop now",
        control_meta, hard=True,
        sender="intruder-mail",
    )
    assert result["notified"] is False
    assert result["reason"] == b0_wiring.REASON_STALE_BINDING_VERSION
    assert sends == [], "canonical 门前不得发出 C-c/prompt"
    # canonical 发送者：放行且正文携 binding version
    result = server._notify_coordination_message(
        "/tmp/proj-x", "agent-x", 901, "stop now",
        control_meta, hard=True,
        sender="agent-a",
    )
    assert result["notified"] is True
    texts = [s[2] for s in sends]
    assert any("canonical binding v" in t for t in texts)
    assert any(s[3] == "keys" for s in sends)  # hard C-c 在授权后发出


# ---------------------------------------------------------------------------
# B4：rebind 预校验（CAS 前零变更）
# ---------------------------------------------------------------------------

def test_rebind_prevalidation_zero_change(client, registry: Path) -> None:
    base = {"mail_name": "mail-b", "expected_version": 0,
            "session": "s", "pane_id": "p"}
    # selector 不存在 → 400 且无 active
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={**base, "registry_selector": "proj-x/missing.json"},
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    assert leader_binding.get_active_binding(ISSUER, "user", "default") is None
    # identity name 与 mail_name 不符 → 400
    _write_identity(registry, "proj-x/m--main.json", name="someone-else")
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={**base, "registry_selector": "proj-x/m--main.json"},
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    assert leader_binding.get_active_binding(ISSUER, "user", "default") is None
    # 非本地 Hub → 400
    _write_identity(registry, "proj-x/r--main.json", name="mail-b",
                    hub="https://example.invalid")
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={**base, "registry_selector": "proj-x/r--main.json"},
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    # pane/session 缺失 → 400
    _write_identity(registry, "proj-x/ok--main.json", name="mail-b")
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={"mail_name": "mail-b", "expected_version": 0,
              "registry_selector": "proj-x/ok--main.json"},
        headers=AUTH,
    )
    assert resp.status_code == 400, resp.text
    # 全部合法 → 200
    resp = client.post(
        "/api/binding/user/default/rebind",
        json={**base, "registry_selector": "proj-x/ok--main.json"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# B5：fetch_inbox 网络前 loopback fail-closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "result_body",
    [
        {"content": [{
            "type": "text", "text": json.dumps([{"id": 41}]),
        }]},
        {"content": [], "structuredContent": {"result": [{"id": 41}]}},
    ],
    ids=("content-text", "structured-content"),
)
def test_fetch_inbox_accepts_mcp_response_formats(
    monkeypatch: pytest.MonkeyPatch, result_body: dict[str, Any],
) -> None:
    seen: dict[str, Any] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            result = {
                "jsonrpc": "2.0", "id": 1,
                "result": result_body,
            }
            raw = json.dumps(result)
            split = raw.index('"result"')
            return (
                "event: message\n"
                f"data: {raw[:split]}\n"
                f"data: {raw[split:]}\n\n"
            ).encode()

    def open_request(request, **_kwargs):
        seen["accept"] = request.get_header("Accept")
        seen["format"] = json.loads(request.data)["params"]["arguments"].get(
            "format"
        )
        return Response()

    monkeypatch.setattr(b0_wiring.urllib.request, "urlopen", open_request)
    monkeypatch.setattr(hub_client, "HUB", "http://127.0.0.1:8765")
    monkeypatch.setattr(hub_client, "TOKEN", "client-token")

    messages = b0_wiring.fetch_inbox_for({
        "project_key": "/tmp/p", "name": "agent-a",
        "registration_token": "registration-token",
        "hub": "http://127.0.0.1:8765",
    })

    assert messages == [{"id": 41}]
    assert seen["accept"] == "application/json, text/event-stream"
    assert seen["format"] == "json"


def test_fetch_inbox_refuses_remote_hub_before_network(
    registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.request

    called: list[str] = []
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: called.append(req.full_url),
    )
    monkeypatch.setattr(hub_client, "HUB", "https://example.invalid")
    monkeypatch.setattr(hub_client, "TOKEN", "client-token")
    ident = {
        "project_key": "/tmp/p", "name": "agent-r",
        "registration_token": "rt-r", "hub": "https://example.invalid",
    }
    with pytest.raises(b0_wiring.CredentialUnavailable):
        b0_wiring.fetch_inbox_for(ident)
    assert called == [], "远端 Hub 不得收到 Bearer/registration_token"


# ---------------------------------------------------------------------------
# B6：scope_key 单射；created_ts 字符串不得毒化去重
# ---------------------------------------------------------------------------

def test_scope_key_injective() -> None:
    a = b0_wiring.scope_key("x", "team", "user/z")
    b = b0_wiring.scope_key("x/team", "user", "z")
    assert a != b
    assert b0_wiring.split_scope_key(a) == ("x", "team", "user/z")
    assert b0_wiring.split_scope_key(b) == ("x/team", "user", "z")


def test_created_ts_string_does_not_poison(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry)
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")
    bad = _msg(81)
    bad["created_ts"] = "2026-08-10T12:00:00+08:00"  # 字符串时间
    _stub_fetch(monkeypatch, [bad])
    stats = coord.poll_once()
    assert stats["ingested"] == 1, stats
    assert len(adapter.calls) == 1, "字符串 created_ts 不得导致 0 prompt"


# ---------------------------------------------------------------------------
# P1-7：G6 多 pane 歧义门
# ---------------------------------------------------------------------------

def test_g6_multi_pane_same_mail_ambiguous_no_rebind(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    _make_coordinator(binding_db, registry)  # pane_id=pane-1, mail=agent-a
    monkeypatch.setattr(server, "B0_ISSUER", ISSUER)
    monkeypatch.setattr(server, "B0_MODE", "on")
    monkeypatch.setattr(server, "_b0_coordinator", None)
    snap = {
        "available": True,
        "panes": [
            {"session": "s1", "pane_id": "pA", "mail_name": "agent-a",
             "agent_status": "ready"},
            {"session": "s2", "pane_id": "pB", "mail_name": "agent-a",
             "agent_status": "ready"},
        ],
    }
    server._b0_apply_live_status(snap)
    row = leader_binding.get_active_binding(ISSUER, "user", "default")
    assert row["pane_id"] == "pane-1", "歧义多 pane 不得自动改绑"


# ---------------------------------------------------------------------------
# P1-8：真非回环 ASGI client 地址
# ---------------------------------------------------------------------------

def _fake_request(client_addr: tuple[str, int], headers: dict | None = None):
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "POST", "path": "/api/binding/user/x/rebind",
        "client": client_addr, "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "query_string": b"",
    }
    return Request(scope)


def test_user_gate_with_real_client_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    import server

    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    # 真非回环地址（TEST-NET-3）无 token → 拒绝
    assert server._b0_user_request(_fake_request(("203.0.113.7", 51234))) is False
    # 真回环地址无 token → 用户
    assert server._b0_user_request(_fake_request(("127.0.0.1", 51234))) is True
    assert server._b0_user_request(_fake_request(("::1", 51234))) is True
    # token 模式下：非回环 + 正确 Bearer → 用户；无凭据 → 拒绝
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    assert server._b0_user_request(
        _fake_request(("203.0.113.7", 51234),
                      {"authorization": "Bearer secret"}),
    ) is True
    assert server._b0_user_request(
        _fake_request(("203.0.113.7", 51234)),
    ) is False
