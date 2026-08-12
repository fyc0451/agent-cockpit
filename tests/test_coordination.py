import json
import threading

from agent_cockpit import coordination
import pytest


def _participants(tmp_path, *, lead_task="实现", dev_task="验证"):
    return [
        {
            "id": "lead", "agent": "codex", "mail_name": "codex-main",
            "pane_id": "w1:p1", "role": "lead", "task": lead_task,
            "workdir": str(tmp_path / "lead"),
        },
        {
            "id": "dev", "agent": "kimi", "mail_name": "kimi-main",
            "pane_id": "w1:p2", "role": "developer", "task": dev_task,
            "workdir": str(tmp_path / "dev"),
        },
    ]


def _run(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setattr(coordination, "DB_PATH", tmp_path / "coordination.sqlite3")
    for name in ("lead", "dev"):
        (tmp_path / name).mkdir(exist_ok=True)
    return coordination.start_run(
        project_key=str(tmp_path), session="demo", session_dir=str(tmp_path),
        participants=_participants(tmp_path, **kwargs), now=100,
    )


def _message(message_id, body, *, sender="codex-main", created=110):
    return {
        "id": message_id, "from": sender, "body_md": body,
        "importance": "normal", "created_ts": created,
    }


def _meta(tmp_path, *, sender="codex-main", recipient="kimi-main", intent="blocking"):
    meta, warnings = coordination.prepare_metadata(
        project_key=str(tmp_path), sender=sender, recipients=[recipient],
        intent=intent, now=105,
    )
    assert warnings == []
    return meta


def test_connection_retries_locked_first_initialization(tmp_path, monkeypatch):
    monkeypatch.setattr(coordination, "DB_PATH", tmp_path / "coordination.sqlite3")
    original = coordination._initialize_connection
    calls = 0

    def locked_once(con):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise coordination.sqlite3.OperationalError("database is locked")
        original(con)

    monkeypatch.setattr(coordination, "_initialize_connection", locked_once)
    monkeypatch.setattr(coordination, "CONNECT_RETRY_BASE", 0)

    con = coordination._connect()
    con.close()
    assert calls == 2


def test_concurrent_first_connections_initialize_one_database(tmp_path, monkeypatch):
    monkeypatch.setattr(coordination, "DB_PATH", tmp_path / "coordination.sqlite3")
    barrier = threading.Barrier(8)
    errors = []

    def connect():
        try:
            barrier.wait()
            con = coordination._connect()
            con.execute("SELECT COUNT(*) FROM receipts").fetchone()
            con.close()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=connect) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


def test_start_run_is_idempotent_and_changed_task_supersedes_old(tmp_path, monkeypatch):
    first = _run(tmp_path, monkeypatch)
    same = coordination.start_run(
        project_key=str(tmp_path), session="demo", session_dir=str(tmp_path),
        participants=_participants(tmp_path), now=101,
    )
    changed = coordination.start_run(
        project_key=str(tmp_path), session="demo", session_dir=str(tmp_path),
        participants=_participants(tmp_path, dev_task="新任务"), now=102,
    )

    assert same["run_id"] == first["run_id"]
    assert same["created"] is False
    assert changed["run_id"] != first["run_id"]
    assert changed["revision"] == 2
    assert coordination.run_context(first["run_id"], "kimi-main")["state"] == "superseded"


def test_add_participant_joins_active_run_and_receives_run_message(tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch)
    qoder_dir = tmp_path / "qoder"
    qoder_dir.mkdir()

    added = coordination.add_participant(
        session="demo", participant_id="qodercli-1", agent="qodercli",
        mail_name="qodercn-main", pane_id="w1:pA", workdir=str(qoder_dir), now=101,
    )
    repeated = coordination.add_participant(
        session="demo", participant_id="qodercli-1", agent="qodercli",
        mail_name="qodercn-main", pane_id="w1:pA", workdir=str(qoder_dir), now=102,
    )
    meta, warnings = coordination.prepare_metadata(
        project_key=str(tmp_path), sender="codex-main",
        recipients=["qodercn-main"], intent="action", now=103,
    )
    claimed = coordination.claim_message(
        project_key=str(tmp_path), recipient="qodercn-main",
        message=_message(90, coordination.add_metadata("验证", meta), created=104),
        claimant="qoder", cwd=str(qoder_dir), now=104,
    )

    assert warnings == []
    assert added == {
        "joined": True, "reused": False, "run_id": run["run_id"],
        "participant_id": "qodercli-1", "project_key": str(tmp_path.resolve()),
        "run_revision": 1, "task_revision": 1,
    }
    assert repeated["reused"] is True
    assert meta["targets"]["qodercn-main"] == {
        "task_id": "qodercli-1", "task_revision": 1,
    }
    assert claimed["deliver"] is True
    assert claimed["reason"] is None


def test_add_participant_rejects_invalid_or_conflicting_identity(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="缺少"):
        coordination.add_participant(
            session="demo", participant_id="", agent="qodercli",
            pane_id="w1:pA", workdir=str(tmp_path),
        )
    with pytest.raises(ValueError, match="id 已存在"):
        coordination.add_participant(
            session="demo", participant_id="lead", agent="codex",
            pane_id="w1:pA", workdir=str(tmp_path),
        )


def test_duplicate_unread_is_effectively_once_even_when_ack_is_pending(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    body = coordination.add_metadata("执行", _meta(tmp_path))
    message = _message(1, body)

    first = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="one", cwd=str(tmp_path), now=110,
    )
    completed = coordination.complete_message(
        str(tmp_path), "kimi-main", 1,
        claim_token=first["claim_token"], now=111,
    )
    repeated = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="two", cwd=str(tmp_path), now=112,
    )

    assert first["deliver"] is True
    assert completed["ack_pending"] == 1
    assert repeated["deliver"] is False
    assert repeated["state"] == "processed"
    coordination.mark_acked(str(tmp_path), "kimi-main", 1)
    assert coordination.receipt(str(tmp_path), "kimi-main", 1)["ack_pending"] == 0


def test_concurrent_claim_has_exactly_one_winner(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    message = _message(2, coordination.add_metadata("执行", _meta(tmp_path)))
    barrier = threading.Barrier(2)
    results = []

    def claim(name):
        barrier.wait()
        results.append(coordination.claim_message(
            project_key=str(tmp_path), recipient="kimi-main", message=message,
            claimant=name, cwd=str(tmp_path), now=110,
        )["deliver"])

    threads = [threading.Thread(target=claim, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]


def test_old_run_revision_and_legacy_before_current_run_are_stale(tmp_path, monkeypatch):
    first = _run(tmp_path, monkeypatch)
    old_meta = _meta(tmp_path)
    coordination.start_run(
        project_key=str(tmp_path), session="demo", session_dir=str(tmp_path),
        participants=_participants(tmp_path, dev_task="新任务"), now=120,
    )

    old = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main",
        message=_message(3, coordination.add_metadata("旧", old_meta), created=121),
        claimant="x", cwd=str(tmp_path), now=122,
    )
    legacy = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main",
        message=_message(4, "历史", created=90), claimant="x",
        cwd=str(tmp_path), now=122,
    )

    assert old["run_id"] == first["run_id"]
    assert old["deliver"] is False
    assert old["reason"] == "run_not_active"
    assert legacy["deliver"] is False
    assert legacy["reason"] == "legacy_before_run"


def test_superseded_message_is_filtered_before_batch_order(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    new_meta = _meta(tmp_path)
    new_meta["supersedes"] = [5]
    messages = [
        _message(5, coordination.add_metadata("旧", _meta(tmp_path)), created=110),
        _message(6, coordination.add_metadata("新", new_meta), created=111),
    ]

    coordination.observe_messages(str(tmp_path), "kimi-main", messages, now=112)
    old = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=messages[0],
        claimant="x", cwd=str(tmp_path), now=113,
    )
    new = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=messages[1],
        claimant="x", cwd=str(tmp_path), now=113,
    )

    assert old["deliver"] is False
    assert old["reason"] == "superseded_by:6"
    assert old["ack_pending"] == 1
    assert new["deliver"] is True


def test_live_pane_renews_slow_claim_and_dead_pane_requeues(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    message = _message(7, coordination.add_metadata("慢任务", _meta(tmp_path)))
    first_claim = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="x", cwd=str(tmp_path), now=110, ttl=10,
    )

    renewed = coordination.maintain_live_claims({
        "panes": [{
            "session": "demo", "pane_id": "w1:p2",
            "agent_status": "working",
        }]
    }, now=121)
    assert renewed == 1
    assert coordination.receipt(str(tmp_path), "kimi-main", 7)["claim_expires_ts"] == 421

    coordination.maintain_live_claims({"panes": []}, now=422)
    assert coordination.receipt(str(tmp_path), "kimi-main", 7)["state"] == "pending"
    second_claim = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="y", cwd=str(tmp_path), now=423, ttl=10,
    )
    with pytest.raises(ValueError, match="claim 已失效"):
        coordination.complete_message(
            str(tmp_path), "kimi-main", 7,
            claim_token=first_claim["claim_token"], now=424,
        )
    assert coordination.complete_message(
        str(tmp_path), "kimi-main", 7,
        claim_token=second_claim["claim_token"], now=425,
    )["completed"] is True


def test_expired_claim_cannot_complete_before_watcher_requeues(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    message = _message(12, coordination.add_metadata("慢任务", _meta(tmp_path)))
    claimed = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="x", cwd=str(tmp_path), now=110, ttl=10,
    )

    with pytest.raises(ValueError, match="claim 已过期"):
        coordination.complete_message(
            str(tmp_path), "kimi-main", 12,
            claim_token=claimed["claim_token"], now=120,
        )


def test_failed_handler_releases_claim_for_retry(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    message = _message(13, coordination.add_metadata("重试", _meta(tmp_path)))
    claimed = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="x", cwd=str(tmp_path), now=110,
    )

    assert coordination.fail_message(
        str(tmp_path), "kimi-main", 13, "外部依赖失败",
        claim_token=claimed["claim_token"], now=111,
    ) is True
    retried = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="y", cwd=str(tmp_path), now=112,
    )
    assert retried["deliver"] is True
    assert retried["claim_token"] != claimed["claim_token"]


def test_developer_stop_is_downgraded_but_trusted_user_stop_does_not_resume(
    tmp_path, monkeypatch,
):
    _run(tmp_path, monkeypatch)
    downgraded, warnings = coordination.prepare_metadata(
        project_key=str(tmp_path), sender="kimi-main", recipients=["codex-main"],
        intent="stop", now=105,
    )
    assert downgraded["intent"] == "blocking"
    assert "无权" in warnings[0]

    user_meta, _ = coordination.prepare_metadata(
        project_key=str(tmp_path), sender="codex-main", recipients=["kimi-main"],
        intent="stop", authority="user", now=105,
    )
    coordination.register_message(
        project_key=str(tmp_path), message_id=8, sender="codex-main",
        meta=user_meta, trusted_user=True, now=106,
    )
    claimed = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main",
        message=_message(8, coordination.add_metadata("停止", user_meta)),
        claimant="x", cwd=str(tmp_path), now=110,
    )
    completed = coordination.complete_message(
        str(tmp_path), "kimi-main", 8,
        claim_token=claimed["claim_token"], now=111,
    )
    resumed = coordination.resume_message(str(tmp_path), "kimi-main", 8, now=112)

    assert claimed["intent"] == "stop"
    assert completed["needs_resume"] is False
    assert resumed == {"resumed": False, "reason": "stop"}
    assert coordination.active_context(str(tmp_path), "kimi-main")["participant_state"] == "stopped"


def test_uncertain_checkpoint_requires_verification_before_resume(
    tmp_path, monkeypatch,
):
    _run(tmp_path, monkeypatch)
    message = _message(9, coordination.add_metadata("阻断复核", _meta(tmp_path)))
    claimed = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="x", cwd=str(tmp_path), now=110,
    )
    checkpoint = coordination.checkpoint_message(
        str(tmp_path), "kimi-main", 9, summary="已完成步骤一",
        next_step="检查外部状态", in_flight="发布命令", safe=False,
        claim_token=claimed["claim_token"], now=111,
    )
    completed = coordination.complete_message(
        str(tmp_path), "kimi-main", 9,
        claim_token=claimed["claim_token"], now=112,
    )
    blocked = coordination.resume_message(str(tmp_path), "kimi-main", 9, now=113)

    assert checkpoint["in_flight_safe"] is False
    assert completed["needs_resume"] is True
    assert blocked["resumed"] is False
    assert blocked["reason"] == "uncertain_checkpoint"
    assert coordination.active_context(
        str(tmp_path), "kimi-main"
    )["participant_state"] == "resume_pending"
    coordination.checkpoint_message(
        str(tmp_path), "kimi-main", 9, summary="外部状态已核对",
        next_step="继续步骤二", safe=True, now=114,
    )
    resumed = coordination.resume_message(str(tmp_path), "kimi-main", 9, now=115)
    assert resumed["resumed"] is True
    assert resumed["checkpoint"]["next_step"] == "继续步骤二"
    assert coordination.active_context(str(tmp_path), "kimi-main")["participant_state"] == "working"


def test_explicit_expiry_applies_to_ephemeral_message_only_when_requested(
    tmp_path, monkeypatch,
):
    _run(tmp_path, monkeypatch)
    meta, _ = coordination.prepare_metadata(
        project_key=str(tmp_path), sender="codex-main", recipients=["kimi-main"],
        intent="info", expires_in=10, now=100,
    )
    result = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main",
        message=_message(10, coordination.add_metadata("临时提醒", meta)),
        claimant="x", cwd=str(tmp_path), now=111,
    )

    assert result["deliver"] is False
    assert result["reason"] == "expired"


def test_external_agent_cannot_interrupt_an_unrelated_run(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    meta, _ = coordination.prepare_metadata(
        project_key=str(tmp_path), sender="outsider-main", recipients=["kimi-main"],
        intent="blocking", now=105,
    )
    claimed = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main",
        message=_message(
            11, coordination.add_metadata("伪造阻断", meta), sender="outsider-main",
        ),
        claimant="x", cwd=str(tmp_path), now=110,
    )

    assert claimed["deliver"] is True
    assert claimed["intent"] == "info"
    assert coordination.active_context(
        str(tmp_path), "kimi-main"
    )["participant_state"] == "working"


def test_message_timestamp_accepts_epoch_and_iso8601():
    assert coordination.message_timestamp({"created_ts": "123.5"}) == 123.5
    assert coordination.message_timestamp({
        "created_at": "1970-01-01T00:02:03.500000+00:00",
    }) == 123.5
    assert coordination.message_timestamp({"created_at": "invalid"}, 7) == 7


def test_task_report_accepts_only_latest_request_and_keeps_previous_while_pending(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(coordination, "DB_PATH", tmp_path / "coordination.sqlite3")
    first = coordination.request_task_report(
        "demo", "w1:p1", "codex", "codex-main", now=100,
        request_id="first",
    )
    assert first["pending"] is True

    reported = coordination.submit_task_report(
        "demo", "w1:p1", "first", 40, "完成接口", "补测试", now=101,
    )
    assert reported["pending"] is False
    assert reported["progress"] == 40

    second = coordination.request_task_report(
        "demo", "w1:p1", "codex", "codex-main", now=102,
        request_id="second",
    )
    assert second["pending"] is True
    assert second["summary"] == "完成接口"
    with pytest.raises(ValueError, match="已过期"):
        coordination.submit_task_report(
            "demo", "w1:p1", "first", 90, "旧报告", now=103,
        )

    latest = coordination.submit_task_report(
        "demo", "w1:p1", "second", 70, "测试通过", "发布", "", now=104,
    )
    assert latest["pending"] is False
    assert latest["summary"] == "测试通过"
    assert latest["next_step"] == "发布"


def test_task_report_rejects_invalid_content_and_clears_reused_pane_identity(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(coordination, "DB_PATH", tmp_path / "coordination.sqlite3")
    coordination.request_task_report(
        "demo", "w1:p1", "codex", "codex-main", request_id="one", now=1,
    )
    with pytest.raises(ValueError, match="0-100"):
        coordination.submit_task_report(
            "demo", "w1:p1", "one", 101, "错误", now=2,
        )
    with pytest.raises(ValueError, match="summary 不能为空"):
        coordination.submit_task_report(
            "demo", "w1:p1", "one", 10, "", now=2,
        )
    coordination.submit_task_report(
        "demo", "w1:p1", "one", 100, "旧 Agent 完成", now=2,
    )

    changed = coordination.request_task_report(
        "demo", "w1:p1", "opencode", "opencode-main",
        request_id="two", now=3,
    )
    assert changed["agent_type"] == "opencode"
    assert changed["reported_ts"] is None
    assert changed["summary"] is None
    assert changed["pending"] is True


def test_concurrent_start_run_same_config_never_hits_unique(tmp_path, monkeypatch):
    """M1: 同 (session, session_dir) 并发 start_run 必须 BEGIN IMMEDIATE 串行，
    绝不撞 UNIQUE(session, session_dir, revision)；相同配置只产出一个 active run。"""
    monkeypatch.setattr(coordination, "DB_PATH", tmp_path / "coordination.sqlite3")
    for name in ("lead", "dev"):
        (tmp_path / name).mkdir(exist_ok=True)
    participants = _participants(tmp_path)
    barrier = threading.Barrier(8)
    results = []
    errors = []

    def start():
        try:
            barrier.wait()
            results.append(coordination.start_run(
                project_key=str(tmp_path), session="demo", session_dir=str(tmp_path),
                participants=participants, now=100,
            ))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []  # 不得出现 IntegrityError
    assert len(results) == 8
    assert len({run["run_id"] for run in results}) == 1  # 全部复用首个 active run
    assert sum(1 for run in results if run["created"]) == 1
    assert sum(1 for run in results if run["reused"]) == 7
    final = coordination.run_by_session("demo")
    assert final["state"] == "active"
    assert final["revision"] == 1


def test_stale_claim_token_cannot_checkpoint_or_fail_after_reclaim(tmp_path, monkeypatch):
    """M2: claim 过期→reclaim 后，旧 claim_token 的 checkpoint/fail 必须被拒，
    不得覆盖新持有者的检查点或把消息错误标失败。"""
    _run(tmp_path, monkeypatch)
    message = _message(20, coordination.add_metadata("阻断", _meta(tmp_path)))
    first = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="x", cwd=str(tmp_path), now=110, ttl=10,
    )
    coordination.maintain_live_claims({"panes": []}, now=121)  # 过期 → pending，token 清空
    assert coordination.receipt(str(tmp_path), "kimi-main", 20)["state"] == "pending"
    second = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="y", cwd=str(tmp_path), now=122, ttl=10,
    )

    with pytest.raises(ValueError, match="claim 已失效"):
        coordination.checkpoint_message(
            str(tmp_path), "kimi-main", 20, summary="旧持有者",
            claim_token=first["claim_token"], now=123,
        )
    with pytest.raises(ValueError, match="claim 已失效"):
        coordination.fail_message(
            str(tmp_path), "kimi-main", 20, "旧持有者失败",
            claim_token=first["claim_token"], now=124,
        )

    # 新持有者照常写入；旧 token 没有覆盖它
    coordination.checkpoint_message(
        str(tmp_path), "kimi-main", 20, summary="新持有者",
        claim_token=second["claim_token"], now=125,
    )
    stored = json.loads(
        coordination.receipt(str(tmp_path), "kimi-main", 20)["checkpoint_json"]
    )
    assert stored["summary"] == "新持有者"


def test_pending_window_rejects_stale_checkpoint_before_reclaim(tmp_path, monkeypatch):
    """codex 复审: claim 过期为 pending 后、reclaim 前，旧 token 的 checkpoint 必须
    失败且 checkpoint_json 不变；reclaim 后旧 token 仍失败、新 token 成功。"""
    _run(tmp_path, monkeypatch)
    message = _message(22, coordination.add_metadata("阻断", _meta(tmp_path)))
    first = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="x", cwd=str(tmp_path), now=110, ttl=10,
    )
    coordination.checkpoint_message(
        str(tmp_path), "kimi-main", 22, summary="首次进度",
        claim_token=first["claim_token"], now=111,
    )
    before = coordination.receipt(str(tmp_path), "kimi-main", 22)["checkpoint_json"]
    coordination.maintain_live_claims({"panes": []}, now=121)  # 过期 → pending，token 清空
    assert coordination.receipt(str(tmp_path), "kimi-main", 22)["state"] == "pending"

    with pytest.raises(ValueError, match="pending"):
        coordination.checkpoint_message(
            str(tmp_path), "kimi-main", 22, summary="过期后的写入",
            claim_token=first["claim_token"], now=122,
        )
    # checkpoint_json 未被覆盖
    assert coordination.receipt(str(tmp_path), "kimi-main", 22)["checkpoint_json"] == before

    # reclaim 后旧 token 仍失败、新 token 成功
    second = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="y", cwd=str(tmp_path), now=123, ttl=10,
    )
    with pytest.raises(ValueError, match="claim 已失效"):
        coordination.checkpoint_message(
            str(tmp_path), "kimi-main", 22, summary="旧 token 再试",
            claim_token=first["claim_token"], now=124,
        )
    coordination.checkpoint_message(
        str(tmp_path), "kimi-main", 22, summary="新 token 写入",
        claim_token=second["claim_token"], now=125,
    )
    stored = json.loads(
        coordination.receipt(str(tmp_path), "kimi-main", 22)["checkpoint_json"]
    )
    assert stored["summary"] == "新 token 写入"


def test_resume_refuses_when_run_has_advanced(tmp_path, monkeypatch):
    """M2: resume 的状态判定放入事务；run 被 supersede 后不得把旧 participant 改回 working。"""
    _run(tmp_path, monkeypatch)
    message = _message(21, coordination.add_metadata("复核", _meta(tmp_path)))
    claimed = coordination.claim_message(
        project_key=str(tmp_path), recipient="kimi-main", message=message,
        claimant="x", cwd=str(tmp_path), now=110,
    )
    coordination.complete_message(
        str(tmp_path), "kimi-main", 21, claim_token=claimed["claim_token"], now=111,
    )
    coordination.start_run(  # 配置变化 → 当前 run 被 supersede
        project_key=str(tmp_path), session="demo", session_dir=str(tmp_path),
        participants=_participants(tmp_path, dev_task="新任务"), now=112,
    )

    resumed = coordination.resume_message(str(tmp_path), "kimi-main", 21, now=113)
    assert resumed["resumed"] is False
    assert resumed["reason"] == "run_not_active"
