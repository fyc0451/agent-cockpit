import asyncio
import subprocess
import threading

import pytest
import server
from fastapi import HTTPException


INSTANCE_ID = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"


def _pending_descriptor(
    monkeypatch, tmp_path, *, instance_id=INSTANCE_ID, pane_id="w1:p2",
    team=False,
):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(tmp_path / "descriptors.json"),
    )
    server.herdr_client.save_launch_descriptor(
        session="demo", pane_id=pane_id, name=instance_id, kind="codex",
        args=(
            ["--disable", "hooks", "--sandbox", "read-only"] if team else []
        ),
        agent="codex", workdir=str(project), instance_id=instance_id,
        display_name="夜班",
        launch_mode="team_readonly" if team else None,
    )
    server.herdr_client.update_launch_descriptor_by_instance(
        instance_id, mail_agent="codex", mail_instance=instance_id,
        mail_name="FreshMailbox", mail_project=str(project),
    )
    server.herdr_client.mark_launch_descriptor_retirement_pending("demo", pane_id)
    return project


def test_retire_agent_instance_calls_exact_mail_identity_and_finalizes(
    monkeypatch, tmp_path,
):
    project = _pending_descriptor(monkeypatch, tmp_path)
    retire_script = tmp_path / "am-retire"
    retire_script.touch()
    monkeypatch.setattr(server, "AM_RETIRE_SCRIPT", retire_script)
    calls = []
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda args, **kwargs: calls.append((args, kwargs))
        or subprocess.CompletedProcess(args, 0, "retired", ""),
    )

    result = server._retire_agent_instance(INSTANCE_ID)

    assert result == {"instance_id": INSTANCE_ID, "retired": True}
    assert calls == [
        (
            [
                str(retire_script), "--agent", "codex", "--instance", INSTANCE_ID,
                "--project", str(project),
            ],
            {
                "cwd": str(server.ROOT_DIR), "capture_output": True,
                "text": True, "timeout": 60,
            },
        )
    ]
    descriptor = server.herdr_client.get_launch_descriptor_by_instance(
        INSTANCE_ID, include_retired=True,
    )
    assert descriptor["state"] == "retired"
    assert descriptor.get("retirement_error") is None


def test_retire_team_codex_removes_scoped_exec_rule_before_finalize(
    monkeypatch, tmp_path,
):
    _pending_descriptor(monkeypatch, tmp_path, team=True)
    retire_script = tmp_path / "am-retire"
    retire_script.touch()
    monkeypatch.setattr(server, "AM_RETIRE_SCRIPT", retire_script)
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "retired", ""),
    )
    removed = []
    monkeypatch.setattr(
        server.herdr_client, "remove_team_codex_exec_rule",
        lambda instance_id: removed.append(instance_id) or True,
    )

    result = server._retire_agent_instance(INSTANCE_ID)

    assert result == {"instance_id": INSTANCE_ID, "retired": True}
    assert removed == [INSTANCE_ID]


def test_retire_agent_instance_failure_stays_pending_for_retry(
    monkeypatch, tmp_path,
):
    _pending_descriptor(monkeypatch, tmp_path)
    retire_script = tmp_path / "am-retire"
    retire_script.touch()
    monkeypatch.setattr(server, "AM_RETIRE_SCRIPT", retire_script)
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 7, "", "hub down"),
    )

    result = server._retire_agent_instance(INSTANCE_ID)

    assert result["instance_id"] == INSTANCE_ID
    assert result["retired"] is False
    assert "hub down" in result["error"]
    descriptor = server.herdr_client.get_launch_descriptor_by_instance(
        INSTANCE_ID, include_retired=True,
    )
    assert descriptor["state"] == "retirement_pending"
    assert descriptor["retirement_attempts"] == 1
    assert "hub down" in descriptor["retirement_error"]


def test_slow_retirement_does_not_block_another_instance(monkeypatch, tmp_path):
    second_id = "i-bbbbbbbbbbbbbbbbbbbbbbbbbb"
    _pending_descriptor(monkeypatch, tmp_path)
    _pending_descriptor(
        monkeypatch, tmp_path, instance_id=second_id, pane_id="w1:p3",
    )
    retire_script = tmp_path / "am-retire"
    retire_script.touch()
    monkeypatch.setattr(server, "AM_RETIRE_SCRIPT", retire_script)
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def run(args, **_kwargs):
        instance_id = args[args.index("--instance") + 1]
        if instance_id == INSTANCE_ID:
            first_started.set()
            release_first.wait(timeout=2)
        else:
            second_started.set()
        return subprocess.CompletedProcess(args, 0, "retired", "")

    monkeypatch.setattr(server.subprocess, "run", run)
    results = {}
    first = threading.Thread(
        target=lambda: results.setdefault(
            INSTANCE_ID, server._retire_agent_instance(INSTANCE_ID),
        )
    )
    second = threading.Thread(
        target=lambda: results.setdefault(
            second_id, server._retire_agent_instance(second_id),
        )
    )

    first.start()
    assert first_started.wait(timeout=1)
    second.start()
    second_was_independent = second_started.wait(timeout=1)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert second_was_independent
    assert not first.is_alive()
    assert not second.is_alive()
    assert results == {
        INSTANCE_ID: {"instance_id": INSTANCE_ID, "retired": True},
        second_id: {"instance_id": second_id, "retired": True},
    }


def test_delete_session_reports_partial_when_identity_retirement_fails(monkeypatch):
    events = []
    retirement = {
        "requested": [INSTANCE_ID], "retired": [], "pending": [INSTANCE_ID],
        "errors": {INSTANCE_ID: "hub down"}, "complete": False,
    }
    monkeypatch.setattr(
        server.herdr_client, "stop_session",
        lambda session: events.append(("stop", session))
        or {"available": True, "stopped": session},
    )
    monkeypatch.setattr(
        server, "_mail_project_state",
        lambda session: events.append(("project", session))
        or {"bound": True, "project": "/project"},
    )
    monkeypatch.setattr(
        server.herdr_client, "delete_session",
        lambda session: events.append(("delete", session))
        or {"available": True, "deleted": session, "retirement_pending": [INSTANCE_ID]},
    )
    monkeypatch.setattr(
        server, "_retire_pending_agent_instances",
        lambda ids, project_hint=None: events.append(
            ("retire", list(ids), project_hint)
        ) or retirement,
    )
    monkeypatch.setattr(
        server.coordination, "close_session",
        lambda session, reason: events.append(("coordination", session, reason)),
    )
    monkeypatch.setattr(
        server.mail_projects, "unbind",
        lambda session: events.append(("unbind", session)),
    )

    result = server.api_herdr_session_delete("demo")

    assert result["deleted"] == "demo"
    assert result["partial"] is True
    assert result["identity_retirement"] == retirement
    assert events == [
        ("stop", "demo"), ("project", "demo"), ("delete", "demo"),
        ("retire", [INSTANCE_ID], "/project"),
        ("coordination", "demo", "deleted"), ("unbind", "demo"),
    ]


def test_delete_session_stops_before_delete_and_aborts_on_stop_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.team_sessions, "managed_binding_for_session", lambda _session: None,
    )
    monkeypatch.setattr(
        server.herdr_client, "stop_session",
        lambda session: calls.append(("stop", session))
        or {"available": True, "error": "stop failed"},
    )
    monkeypatch.setattr(
        server.herdr_client, "delete_session",
        lambda session: calls.append(("delete", session))
        or {"available": True, "deleted": session},
    )

    result = server.api_herdr_session_delete("demo")

    assert result["error"] == "stop failed"
    assert calls == [("stop", "demo")]


def test_delete_pane_retires_only_after_close_succeeds(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server, "_mail_project_state",
        lambda session: {"bound": True, "project": "/project"},
    )
    monkeypatch.setattr(
        server.herdr_client, "close_pane",
        lambda session, pane_id: calls.append((session, pane_id))
        or {"available": True, "closed": pane_id, "retirement_pending": [INSTANCE_ID]},
    )
    monkeypatch.setattr(
        server, "_retire_pending_agent_instances",
        lambda ids, project_hint=None: {
            "requested": list(ids), "retired": list(ids), "pending": [],
            "errors": {}, "complete": True,
        },
    )

    result = server.api_herdr_pane_delete("demo", "w1:p2")

    assert result["closed"] == "w1:p2"
    assert result["identity_retirement"]["retired"] == [INSTANCE_ID]
    assert result.get("partial") is None
    assert calls == [("demo", "w1:p2")]


def test_failed_pane_close_never_attempts_retirement(monkeypatch):
    monkeypatch.setattr(
        server.herdr_client, "close_pane",
        lambda *_: {"available": True, "error": "busy"},
    )
    monkeypatch.setattr(
        server, "_retire_pending_agent_instances",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("关闭失败时不得退休身份")
        ),
    )

    assert server.api_herdr_pane_delete("demo", "w1:p2")["error"] == "busy"


def test_stop_session_never_retires_identity(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.herdr_client, "stop_session",
        lambda session: {"available": True, "stopped": session},
    )
    monkeypatch.setattr(
        server.coordination, "close_session",
        lambda session, reason: calls.append((session, reason)),
    )
    monkeypatch.setattr(
        server, "_retire_pending_agent_instances",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("stop 不是销毁，不得退休身份")
        ),
    )

    assert server.api_herdr_session_stop("demo")["stopped"] == "demo"
    assert calls == [("demo", "stopped")]


@pytest.mark.parametrize("action", ["stop", "delete"])
def test_active_managed_team_session_cannot_be_stopped_or_deleted(
    monkeypatch, action,
):
    monkeypatch.setattr(
        server.team_sessions,
        "managed_binding_for_session",
        lambda session: {"project_slug": "ready"} if session == "team-ready-1" else None,
    )
    monkeypatch.setattr(
        server.herdr_client,
        f"{action}_session",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("当前 Topic Agent 不得被普通会话接口停止或删除")
        ),
    )

    endpoint = (
        server.api_herdr_session_stop
        if action == "stop"
        else server.api_herdr_session_delete
    )
    with pytest.raises(HTTPException) as exc_info:
        endpoint("team-ready-1")

    assert exc_info.value.status_code == 409
    assert "Topic ready" in exc_info.value.detail
    assert "先在 Topic 中改绑" in exc_info.value.detail


def test_identity_retirement_loop_waits_before_retry(monkeypatch):
    events = []

    async def wait_first():
        events.append("wait")
        if events.count("wait") > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(server, "_wait_identity_retirement_interval", wait_first)
    monkeypatch.setattr(
        server, "_retry_pending_agent_retirements",
        lambda: events.append("retry") or {"pending": []},
    )

    async def exercise():
        with pytest.raises(asyncio.CancelledError):
            await server._identity_retirement_loop()

    asyncio.run(exercise())

    assert events == ["wait", "retry", "wait"]
