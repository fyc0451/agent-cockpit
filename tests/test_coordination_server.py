from fastapi.testclient import TestClient

from agent_cockpit import coordination
from agent_cockpit import leader_binding
import server


def _run(tmp_path):
    for name in ("lead", "dev"):
        (tmp_path / name).mkdir()
    return coordination.start_run(
        project_key=str(tmp_path), session="demo", session_dir=str(tmp_path),
        participants=[
            {
                "id": "lead", "agent": "codex", "mail_name": "codex-main",
                "pane_id": "w1:p1", "role": "lead", "task": "实现",
                "workdir": str(tmp_path / "lead"),
            },
            {
                "id": "dev", "agent": "kimi", "mail_name": "kimi-main",
                "pane_id": "w1:p2", "role": "developer", "task": "验证",
                "workdir": str(tmp_path / "dev"),
            },
        ], now=100,
    )


def _mail(monkeypatch, tmp_path, message_id=70):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    # P0-B0-test-isolation：本文件测 legacy 协调流，与 B0 授权门正交。
    # pin B0_MODE=off 并把 leader_binding store 隔离到 tmp（必须是 Path），
    # 否则 COCKPIT_B0_MODE=on 且机器真实 store 有 active binding 时，
    # 控制消息会被 canonical-leader 门按设计 403（fixture 依赖机器状态）。
    monkeypatch.setattr(server, "B0_MODE", "off")
    monkeypatch.setattr(
        leader_binding, "DB_PATH", tmp_path / "leader-binding.db",
    )
    monkeypatch.setattr(
        server, "_agent_mail_status",
        lambda: {"available": True, "write_available": True, "write_reason": None},
    )
    monkeypatch.setattr(
        server.db, "project_by_id",
        lambda _pid: {"id": 1, "human_key": str(tmp_path)},
    )
    monkeypatch.setattr(
        server.db, "agent_by_name",
        lambda _pid, name: {
            "name": name, "registration_token": "token",
        },
    )
    sent = {}

    def send_message(**kwargs):
        sent.update(kwargs)
        return {"deliveries": [{"payload": {"id": message_id, "to": kwargs["to"]}}]}

    monkeypatch.setattr(server.hub_client, "send_message", send_message)
    monkeypatch.setattr(server.hub_client, "allows_local_actions", lambda: True)
    return sent


def test_user_blocking_message_saves_checkpoint_and_queues_safe_resume(
    monkeypatch, tmp_path,
):
    _run(tmp_path)
    sent = _mail(monkeypatch, tmp_path)
    pane_calls = []
    monkeypatch.setattr(
        server.herdr_client, "pane_send",
        lambda *args: pane_calls.append(args) or {"available": True},
    )

    response = TestClient(server.app).post(
        "/api/send", headers={"authorization": "Bearer secret"},
        json={
            "project_id": 1, "sender_name": "codex-main", "to": ["kimi-main"],
            "subject": "先处理阻断", "body": "处理后继续", "intent": "blocking",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coordination"]["meta"]["run_id"]
    assert coordination.META_PREFIX in sent["body_md"]
    assert [call[3] for call in pane_calls] == ["prompt"]
    assert "--message 70" in pane_calls[0][2]
    receipt = coordination.receipt(str(tmp_path), "kimi-main", 70)
    assert receipt["state"] == "pending"
    assert coordination.active_context(str(tmp_path), "kimi-main")["participant_state"] == "pause_requested"


def test_hard_stop_is_user_only_explicit_and_sends_ctrl_c_before_prompt(
    monkeypatch, tmp_path,
):
    _run(tmp_path)
    _mail(monkeypatch, tmp_path, 71)
    pane_calls = []
    monkeypatch.setattr(
        server.herdr_client, "pane_send",
        lambda *args: pane_calls.append(args) or {"available": True},
    )
    client = TestClient(server.app)
    invalid = client.post(
        "/api/send", headers={"authorization": "Bearer secret"},
        json={
            "project_id": 1, "sender_name": "codex-main", "to": ["kimi-main"],
            "subject": "错误", "body": "不能硬打断后恢复", "intent": "blocking",
            "hard": True,
        },
    )
    assert invalid.status_code == 400

    response = client.post(
        "/api/send", headers={"authorization": "Bearer secret"},
        json={
            "project_id": 1, "sender_name": "codex-main", "to": ["kimi-main"],
            "subject": "停止", "body": "停止旧任务", "intent": "stop", "hard": True,
        },
    )

    assert response.status_code == 200
    assert [(call[2], call[3]) for call in pane_calls] == [
        ("C-c", "keys"),
        (pane_calls[1][2], "prompt"),
    ]
    checkpoint = coordination.receipt(str(tmp_path), "kimi-main", 71)["checkpoint_json"]
    assert '"step_state": "uncertain"' in checkpoint


def test_b0_on_rejects_non_canonical_control_sender(monkeypatch, tmp_path):
    """授权负例（固化 #3457 诊断）：B0=on 且隔离 binding DB 中存在他人 active
    binding 时，非 canonical sender 的控制消息必须 403，且授权门在 Hub/pane
    之前——pane_send 零调用。"""
    _run(tmp_path)
    _mail(monkeypatch, tmp_path, 73)
    monkeypatch.setattr(server, "B0_MODE", "on")
    # 隔离 store（_mail 已 pin DB_PATH 到 tmp_path）中种一条非 sender 的 active binding
    leader_binding.bind_leader(
        "local", "user", "default", mail_name="someone-else",
        session="s1", pane_id="p1", registry_selector="x/a.json",
        expected_version=0,
    )
    pane_calls = []
    monkeypatch.setattr(
        server.herdr_client, "pane_send",
        lambda *args: pane_calls.append(args) or {"available": True},
    )

    response = TestClient(server.app).post(
        "/api/send", headers={"authorization": "Bearer secret"},
        json={
            "project_id": 1, "sender_name": "codex-main", "to": ["kimi-main"],
            "subject": "停止", "body": "停止旧任务", "intent": "stop", "hard": True,
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "控制消息发送者不是 active canonical Leader"
    assert pane_calls == []


def test_ui_ack_marks_local_stale_before_hub_ack(tmp_path, monkeypatch):
    _run(tmp_path)
    _mail(monkeypatch, tmp_path, 72)
    monkeypatch.setattr(
        server.hub_client, "acknowledge_message", lambda **_kwargs: {"ok": True}
    )

    response = TestClient(server.app).post(
        "/api/ack", headers={"authorization": "Bearer secret"},
        json={"project_id": 1, "agent_name": "kimi-main", "message_id": 72},
    )

    assert response.status_code == 200
    receipt = coordination.receipt(str(tmp_path), "kimi-main", 72)
    assert receipt["state"] == "stale"
    assert receipt["reason"] == "user_ack"
    assert receipt["ack_pending"] == 0


def test_send_success_is_not_reported_as_failure_when_sidecar_write_fails(
    tmp_path, monkeypatch,
):
    _run(tmp_path)
    _mail(monkeypatch, tmp_path, 73)
    monkeypatch.setattr(
        coordination, "register_message",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    response = TestClient(server.app).post(
        "/api/send", headers={"authorization": "Bearer secret"},
        json={
            "project_id": 1, "sender_name": "codex-main", "to": ["kimi-main"],
            "subject": "已发送", "body": "不要诱发重试", "intent": "info",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "已发送" in body["coordination"]["warnings"][0]
    assert "登记失败" in body["coordination"]["warnings"][0]


def test_shared_hub_response_cannot_trigger_local_agent_actions(
    tmp_path, monkeypatch,
):
    _run(tmp_path)
    _mail(monkeypatch, tmp_path, 74)
    monkeypatch.setattr(server.hub_client, "allows_local_actions", lambda: False)
    pane_calls = []
    monkeypatch.setattr(
        server.herdr_client, "pane_send",
        lambda *args: pane_calls.append(args) or {"available": True},
    )

    response = TestClient(server.app).post(
        "/api/send", headers={"authorization": "Bearer secret"},
        json={
            "project_id": 1, "sender_name": "codex-main", "to": ["kimi-main"],
            "subject": "共享 Hub 内容", "body": "不能触发终端", "intent": "blocking",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coordination"]["notifications"] == []
    assert "仅作为只读数据处理" in body["coordination"]["warnings"][0]
    assert pane_calls == []
    assert coordination.receipt(str(tmp_path), "kimi-main", 74) is None


def test_messages_cleanup_batches_hub_delete(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server, "_agent_mail_status",
        lambda: {
            "read_available": True, "reason": None,
            "write_available": True, "write_reason": None,
        },
    )
    monkeypatch.setattr(server.hub_client, "allows_local_actions", lambda: True)
    monkeypatch.setattr(server.db, "project_by_id", lambda _project_id: {"id": 1})
    monkeypatch.setattr(
        server.db, "_rows",
        lambda _sql, _params: [{"id": i} for i in range(1, 1201)],
    )
    calls = []

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, n):
            self._n = n

        def json(self):
            return {"deleted_count": self._n}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["message_ids"])
        return _Resp(len(json["message_ids"]))

    monkeypatch.setattr(server.httpx, "post", fake_post)

    response = TestClient(server.app).post(
        "/api/messages/cleanup", headers={"authorization": "Bearer secret"},
        json={"project_id": 1, "older_than_days": 30},
    )

    assert response.status_code == 200
    assert response.json() == {"deleted": 1200}
    assert [len(batch) for batch in calls] == [500, 500, 200]


def test_messages_cleanup_rejects_bad_days_and_empty_set(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    bad = client.post(
        "/api/messages/cleanup", headers={"authorization": "Bearer secret"},
        json={"project_id": 1, "older_than_days": 0},
    )
    assert bad.status_code == 400

    monkeypatch.setattr(
        server, "_agent_mail_status",
        lambda: {
            "read_available": True, "reason": None,
            "write_available": True, "write_reason": None,
        },
    )
    monkeypatch.setattr(server.hub_client, "allows_local_actions", lambda: True)
    monkeypatch.setattr(server.db, "project_by_id", lambda _project_id: {"id": 1})
    monkeypatch.setattr(server.db, "_rows", lambda _sql, _params: [])
    empty = client.post(
        "/api/messages/cleanup", headers={"authorization": "Bearer secret"},
        json={"project_id": 1, "older_than_days": 30},
    )
    assert empty.status_code == 200
    assert empty.json() == {"deleted": 0}


def test_messages_cleanup_rejects_unknown_project_and_remote_hub(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        server, "_agent_mail_status",
        lambda: {
            "read_available": True, "reason": None,
            "write_available": True, "write_reason": None,
        },
    )
    monkeypatch.setattr(server.hub_client, "allows_local_actions", lambda: True)
    monkeypatch.setattr(server.db, "project_by_id", lambda _project_id: None)
    client = TestClient(server.app)

    unknown = client.post(
        "/api/messages/cleanup", headers={"authorization": "Bearer secret"},
        json={"project_id": 999, "older_than_days": 30},
    )
    assert unknown.status_code == 404

    monkeypatch.setattr(server.db, "project_by_id", lambda _project_id: {"id": 1})
    monkeypatch.setattr(server.hub_client, "allows_local_actions", lambda: False)
    remote = client.post(
        "/api/messages/cleanup", headers={"authorization": "Bearer secret"},
        json={"project_id": 1, "older_than_days": 30},
    )
    assert remote.status_code == 409
