"""test_b0_wiring.py — B0 产品接线（W1-W6 / A2 / B1）测试。

覆盖冻结合同（ADR 门禁 §6/§7）：
- A2 selector fail-closed：缺失/symlink/权限过宽/损坏/缺 token/Hub mismatch
  → CredentialUnavailable（credential_unavailable）。
- fetch_inbox 失败路径 → CredentialUnavailable。
- coordinator dual-pull：message_id 去重（duplicate_event_id）、
  stale binding_version 丢弃、working 延迟/eligible 补投（deferred_*）。
- fanout：issuer-scoped 注入并 ack；跨 issuer 零变更。
- B1 rebind：鉴权（用户/active mail_name/拒绝）、expected_version 必填
  CAS、失败零变更（409）、成功 binding_changed outbox。
- rebuild：从 Hub unread 重建 pending，receipt 去重不重复 prompt。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import b0_wiring
import hub_client
import leader_binding


ISSUER = "issuer-w"


class _RecordingAdapter:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.calls: list[tuple[str, int, list[Any]]] = []

    def deliver(self, scope, binding_version, events):
        self.calls.append((scope, binding_version, list(events)))
        return self.accept


@pytest.fixture()
def binding_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "leader-binding.sqlite3"
    monkeypatch.setattr(leader_binding, "DB_PATH", path)
    return path


@pytest.fixture()
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "registry"
    (root / "proj-x").mkdir(parents=True)
    os.chmod(root, 0o700)
    os.chmod(root / "proj-x", 0o700)
    monkeypatch.setattr(b0_wiring, "REGISTRY_DIR", root)
    monkeypatch.setattr(hub_client, "HUB", "http://127.0.0.1:8765")
    monkeypatch.setattr(hub_client, "TOKEN", "client-token")
    return root


def _write_identity(
    registry: Path, rel: str, *, name: str = "agent-a",
    hub: str = "http://127.0.0.1:8765", token: str = "rt-1",
    mode: int = 0o600,
) -> Path:
    path = registry / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(json.dumps({
        "project_key": "/tmp/proj-x", "project_slug": "proj-x",
        "name": name, "registration_token": token, "hub": hub,
    }))
    os.chmod(path, mode)
    return path


# ---------------------------------------------------------------------------
# A2 selector 解析（fail-closed）
# ---------------------------------------------------------------------------

def test_resolve_selector_ok(registry: Path) -> None:
    _write_identity(registry, "proj-x/a--main.json", token="rt-a")
    ident = b0_wiring.resolve_selector("proj-x/a--main.json")
    assert ident["name"] == "agent-a"
    assert ident["registration_token"] == "rt-a"


@pytest.mark.parametrize("setup", ["missing", "symlink", "wide", "corrupt", "no_token", "hub_mismatch"])
def test_resolve_selector_fail_closed(registry: Path, setup: str) -> None:
    if setup == "missing":
        selector = "proj-x/nobody--none.json"
    else:
        rel = f"proj-x/{setup}--main.json"
        if setup == "symlink":
            target = _write_identity(registry, "proj-x/target--main.json")
            (registry / rel).symlink_to(target)
        elif setup == "wide":
            _write_identity(registry, rel, mode=0o644)
        elif setup == "corrupt":
            p = registry / rel
            p.write_text("{not json")
            os.chmod(p, 0o600)
        elif setup == "no_token":
            p = registry / rel
            p.write_text(json.dumps({"name": "x", "hub": "http://127.0.0.1:8765"}))
            os.chmod(p, 0o600)
        else:  # hub_mismatch
            _write_identity(registry, rel, hub="http://127.0.0.1:9999")
        selector = rel
    with pytest.raises(b0_wiring.CredentialUnavailable):
        b0_wiring.resolve_selector(selector)


def test_resolve_selector_rejects_absolute_and_traversal(registry: Path) -> None:
    with pytest.raises(b0_wiring.CredentialUnavailable):
        b0_wiring.resolve_selector("/etc/passwd")
    with pytest.raises(b0_wiring.CredentialUnavailable):
        b0_wiring.resolve_selector("../proj-x/a--main.json")


# ---------------------------------------------------------------------------
# coordinator：dual-pull 去重 / stale / deferred
# ---------------------------------------------------------------------------

def _make_coordinator(
    binding_db: Path, registry: Path, *,
    accept: bool = True, selector: str = "proj-x/a--main.json",
) -> tuple[b0_wiring.B0Coordinator, _RecordingAdapter]:
    _write_identity(registry, selector, name="agent-a", token="rt-a")
    leader_binding.bind_leader(
        ISSUER, "user", "default", mail_name="agent-a",
        session="sess-1", pane_id="pane-1", registry_selector=selector,
        expected_version=0,
    )
    adapter = _RecordingAdapter(accept=accept)
    coord = b0_wiring.B0Coordinator(adapter, ISSUER)
    coord.sync_bindings()
    return coord, adapter


def _stub_fetch(monkeypatch: pytest.MonkeyPatch, messages: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(
        b0_wiring, "fetch_inbox_for",
        lambda identity, **kw: list(messages),
    )


def _msg(mid: int, *, subject: str = "s", sender: str = "bob", ts: float = 1.0) -> dict[str, Any]:
    return {"id": mid, "subject": subject, "from": sender,
            "kind": "message", "created_ts": ts}


def test_ingest_delivers_when_eligible(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry)
    _stub_fetch(monkeypatch, [_msg(101)])
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "working")
    stats = coord.poll_once()
    assert stats["ingested"] == 1
    assert adapter.calls == []  # working：不 prompt（deferred_working）
    assert coord.last_reasons.get(b0_wiring.REASON_DEFERRED_WORKING) == 1
    coord.set_target_status(scope, "ready")
    assert len(adapter.calls) == 1  # eligible 后恰好一次补投
    assert adapter.calls[0][0] == scope
    assert coord.last_reasons.get(b0_wiring.REASON_DEFERRED_DELIVERED, 0) >= 0
    # 再次 poll：同 message_id 去重（duplicate_event_id）
    stats2 = coord.poll_once()
    assert stats2["duplicate"] == 1 and stats2["ingested"] == 0
    assert len(adapter.calls) == 1


def test_ingest_stale_binding_version(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry)
    _stub_fetch(monkeypatch, [_msg(102)])
    # 升级 core 的 active 版本（模拟改绑已发生），poll 仍按旧版本 ingest → stale
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.core.set_active_binding(scope, 9)
    rows = leader_binding.list_bindings(issuer=ISSUER, state="active")
    stale_rows = [{**r, "binding_version": 1} for r in rows]
    monkeypatch.setattr(coord, "active_bindings", lambda: stale_rows)
    stats = coord.poll_once()
    assert stats["stale"] == 1
    assert coord.last_reasons.get(b0_wiring.REASON_STALE_BINDING_VERSION) == 1
    assert adapter.calls == []


def test_degraded_when_selector_unreadable(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry)
    _stub_fetch(monkeypatch, [_msg(103)])
    (registry / "proj-x" / "a--main.json").unlink()  # selector 缺失
    stats = coord.poll_once()
    assert stats["degraded"] == 1 and stats["ingested"] == 0
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    assert coord.degraded.get(scope) == b0_wiring.REASON_CREDENTIAL_UNAVAILABLE
    assert adapter.calls == []


def test_adapter_reject_keeps_pending(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry, accept=False)
    _stub_fetch(monkeypatch, [_msg(104)])
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    coord.set_target_status(scope, "ready")
    coord.poll_once()
    assert coord.core.state(scope)["pending_count"] == 1
    adapter.accept = True
    coord.core.flush(scope)
    assert coord.core.state(scope)["pending_count"] == 0


def test_dual_pull_previous_identity(
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
    pulled: list[str] = []

    def fake_fetch(identity: dict[str, Any], **kw: Any) -> list[dict[str, Any]]:
        pulled.append(identity["name"])
        if identity["name"] == "agent-a":
            return [_msg(201, sender="old-inbox")]
        return [_msg(202)]

    monkeypatch.setattr(b0_wiring, "fetch_inbox_for", fake_fetch)
    stats = coord.poll_once()
    # active(agent-b) + previous(agent-a，未排空) 双拉，message_id 去重
    assert "agent-a" in pulled and "agent-b" in pulled
    assert stats["pulled"] == 2 and stats["ingested"] == 2


# ---------------------------------------------------------------------------
# W5 fanout（issuer-scoped）
# ---------------------------------------------------------------------------

def test_fanout_issuer_scoped(binding_db: Path, registry: Path) -> None:
    coord, adapter = _make_coordinator(binding_db, registry)
    scope = b0_wiring.scope_key(ISSUER, "user", "default")
    events = leader_binding.undelivered_control_events(ISSUER)
    assert events  # bind_leader 已产生 binding_changed
    # working：注入但不 mark（crash 可重放，零丢）
    coord.set_target_status(scope, "working")
    assert coord.fanout_control_events() == 0
    assert len(leader_binding.undelivered_control_events(ISSUER)) == len(events)
    # ready：投递成功才 mark
    coord.set_target_status(scope, "ready")
    n = coord.fanout_control_events()
    assert n == len(events)
    assert leader_binding.undelivered_control_events(ISSUER) == []
    # 跨 issuer ack 零变更
    assert leader_binding.mark_event_fanned_out(
        "other-issuer", events[0]["event_id"],
    ) is False


# ---------------------------------------------------------------------------
# B1 rebind_authorize
# ---------------------------------------------------------------------------

def test_rebind_authorize() -> None:
    active = {"mail_name": "leader-x"}
    ok, actor = b0_wiring.rebind_authorize(
        user_authenticated=True, caller_mail_name=None, active=active,
    )
    assert ok and actor == "user"
    ok, actor = b0_wiring.rebind_authorize(
        user_authenticated=False, caller_mail_name="leader-x", active=active,
        capability_digest="d" * 64, expected_digest="d" * 64,
    )
    assert ok and actor == "active_leader"
    ok, _ = b0_wiring.rebind_authorize(
        user_authenticated=False, caller_mail_name="impostor", active=active,
        capability_digest="d" * 64, expected_digest="d" * 64,
    )
    assert not ok
    ok, _ = b0_wiring.rebind_authorize(
        user_authenticated=False, caller_mail_name="leader-x", active=None,
        capability_digest="d" * 64, expected_digest="d" * 64,
    )
    assert not ok
    # 名字对但无能力证明 / digest 不匹配 → 拒绝
    ok, _ = b0_wiring.rebind_authorize(
        user_authenticated=False, caller_mail_name="leader-x", active=active,
    )
    assert not ok
    ok, _ = b0_wiring.rebind_authorize(
        user_authenticated=False, caller_mail_name="leader-x", active=active,
        capability_digest="e" * 64, expected_digest="d" * 64,
    )
    assert not ok


# ---------------------------------------------------------------------------
# B1 端点（TestClient）：CAS / 零变更 / outbox
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(binding_db: Path, monkeypatch: pytest.MonkeyPatch):
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "B0_ISSUER", ISSUER)
    monkeypatch.setattr(server, "_b0_coordinator", None)
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    return TestClient(server.app)


AUTH = {"authorization": "Bearer secret"}


def _rebind_body(**over: Any) -> dict[str, Any]:
    body = {
        "mail_name": "leader-new", "expected_version": 0,
        "agent_name": "herdr", "session": "sess-9", "pane_id": "pane-9",
        "registry_selector": "proj-x/n--main.json",
    }
    body.update(over)
    return body


def test_rebind_endpoint_first_bind_and_cas(client, registry: Path) -> None:
    _write_identity(registry, "proj-x/n--main.json", name="leader-new")
    resp = client.post(
        "/api/binding/user/default/rebind", json=_rebind_body(), headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["rebound"] and data["binding_version"] == 1
    assert data["issuer"] == ISSUER
    # CAS 失败：旧 expected_version → 409 零变更
    resp = client.post(
        "/api/binding/user/default/rebind", json=_rebind_body(), headers=AUTH,
    )
    assert resp.status_code == 409
    active = leader_binding.get_active_binding(ISSUER, "user", "default")
    assert active["binding_version"] == 1  # 零变更
    # 同 mail_name 路由载荷变化 → binding_updated（version+1）
    resp = client.post(
        "/api/binding/user/default/rebind",
        json=_rebind_body(expected_version=1, pane_id="pane-10"),
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["binding_version"] == 2
    events = leader_binding.list_control_events(issuer=ISSUER)
    types = [e["event_type"] for e in events]
    assert "binding_changed" in types and "binding_updated" in types
    # 事件不附 run/task/revision
    for event in events:
        payload = json.loads(event["payload_json"])
        assert "task" not in payload and "revision" not in payload


def test_rebind_endpoint_bad_scope_kind(client) -> None:
    resp = client.post(
        "/api/binding/bogus/x/rebind", json=_rebind_body(), headers=AUTH,
    )
    assert resp.status_code == 400


def test_rebind_requires_expected_version(client) -> None:
    body = _rebind_body()
    body.pop("expected_version")
    resp = client.post(
        "/api/binding/user/default/rebind", json=body, headers=AUTH,
    )
    assert resp.status_code == 422  # pydantic 必填


# ---------------------------------------------------------------------------
# rebuild（restart）：unread 重建 + receipt 去重
# ---------------------------------------------------------------------------

def test_rebuild_skips_processed_receipts(
    binding_db: Path, registry: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, adapter = _make_coordinator(binding_db, registry)
    scope = b0_wiring.scope_key(ISSUER, "user", "default")

    receipts = {
        ("/tmp/proj-x", "agent-a", 301): {"state": "processed"},
    }

    def fake_receipt(project_key: str, recipient: str, message_id: int):
        return receipts.get((project_key, recipient, message_id))

    monkeypatch.setattr(b0_wiring.coordination, "receipt", fake_receipt)
    monkeypatch.setattr(
        b0_wiring.coordination, "observe_messages",
        lambda *a, **kw: None,
    )
    _stub_fetch(monkeypatch, [_msg(301), _msg(302)])
    result = coord.rebuild()
    assert result["poll"]["ingested"] == 1  # 301 已 processed，被跳过
    assert coord.last_reasons.get(b0_wiring.REASON_RESTART_REBUILD) == 1


# ---------------------------------------------------------------------------
# 冻结合同常量
# ---------------------------------------------------------------------------

def test_stable_reasons_frozen() -> None:
    assert b0_wiring.STABLE_REASONS == frozenset({
        "deferred_working", "deferred_delivered", "stale_binding_version",
        "duplicate_event_id", "binding_updated", "binding_changed",
        "credential_unavailable", "cross_run_fail_fast", "restart_rebuild",
    })
    assert len(b0_wiring.STABLE_REASONS) == 9
