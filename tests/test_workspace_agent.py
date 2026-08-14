from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cockpit import herdr_client, workspace_agent


PROJECT = "prj_" + "a" * 32
WORKSPACE_A = "ws_" + "b" * 32
WORKSPACE_B = "ws_" + "c" * 32
LOCATION = "loc_" + "d" * 32
AGENT_A = "i-" + "a" * 26
AGENT_B = "i-" + "b" * 26
SESSION = "github-agent-cockpit-next"
PATH = "/repo/shared"


class Registry:
    def __init__(self, *, project_lifecycle: str = "active") -> None:
        self.project = SimpleNamespace(
            project=SimpleNamespace(lifecycle=project_lifecycle),
        )
        self.workspaces = {
            WORKSPACE_A: SimpleNamespace(
                workspace_id=WORKSPACE_A,
                project_id=PROJECT,
                repo_location_id=LOCATION,
                lifecycle="active",
                isolation_kind="shared",
            ),
            WORKSPACE_B: SimpleNamespace(
                workspace_id=WORKSPACE_B,
                project_id=PROJECT,
                repo_location_id=LOCATION,
                lifecycle="active",
                isolation_kind="shared",
            ),
        }
        self.location = SimpleNamespace(
            repo_location_id=LOCATION,
            lifecycle="active",
            node_id="local",
            availability="available",
            canonical_path=PATH,
        )

    def get_project_by_id(self, project_id: str):
        return self.project if project_id == PROJECT else None

    def get_workspace(self, project_id: str, workspace_id: str):
        if project_id != PROJECT:
            return None
        return self.workspaces.get(workspace_id)

    def list_repo_locations(self, project_id: str):
        return (self.location,) if project_id == PROJECT else None


class Runtime:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="workspace-agent-")
        self.receipt_path = Path(self._temporary.name) / "receipts.sqlite3"
        self.descriptors: dict[str, dict[str, object]] = {}
        self.live: dict[str, dict[str, object]] = {}
        self.start_calls: list[dict[str, object]] = []
        self.bootstrap_calls: list[str] = []
        self.bootstrap_creates = 0
        self.session_running = False
        self.bootstrap_error: object | None = None
        self.send_calls: list[tuple[str, str, str, str]] = []
        self.read_calls: list[tuple[str, str, int, bool]] = []
        self.start_error: object | None = None
        self.send_error: object | None = None
        self.send_delay = 0.02
        self.read_error: object | None = None
        self.send_active = 0
        self.max_send_active = 0
        self.recovery_calls: list[tuple[str, str, str]] = []
        self._send_lock = threading.Lock()

    def bootstrap(self, session: str):
        self.bootstrap_calls.append(session)
        if isinstance(self.bootstrap_error, BaseException):
            raise self.bootstrap_error
        if self.bootstrap_error is not None:
            return self.bootstrap_error
        created = not self.session_running
        if created:
            self.bootstrap_creates += 1
            self.session_running = True
        return {"available": True, "session": session, "created": created}

    def recover(self, session: str, project_id: str, workspace_id: str) -> None:
        self.recovery_calls.append((session, project_id, workspace_id))
        for agent_id, descriptor in self.descriptors.items():
            if (
                descriptor.get("state") == "pending"
                and descriptor.get("session") == session
                and descriptor.get("project_id") == project_id
                and descriptor.get("workspace_id") == workspace_id
                and agent_id in self.live
            ):
                descriptor["state"] = "active"

    def descriptor_list(self, session: str, project_id: str, workspace_id: str):
        return tuple(
            dict(value)
            for value in self.descriptors.values()
            if value.get("session") == session
            and value.get("project_id") == project_id
            and value.get("workspace_id") == workspace_id
            and value.get("state") == "active"
        )

    def descriptor(self, agent_id: str):
        value = self.descriptors.get(agent_id)
        return dict(value) if value is not None else None

    def snapshot(self, session: str):
        panes = []
        agents = []
        for agent_id, value in self.live.items():
            pane_id = str(value["pane_id"])
            kind = str(value["kind"])
            panes.append({
                "pane_id": pane_id,
                "session": session,
                "agent": kind,
                "agent_status": value.get("status", "idle"),
            })
            agents.append({"name": agent_id, "pane_id": pane_id, "agent": kind})
        return {"session": session, "status": "running", "panes": panes, "agents": agents}

    def start(self, session: str, workdir: str, agent: str, **kwargs):
        self.start_calls.append({
            "session": session, "workdir": workdir, "agent": agent, **kwargs,
        })
        if isinstance(self.start_error, BaseException):
            raise self.start_error
        if self.start_error is not None:
            return self.start_error
        agent_id = str(kwargs["instance_id"])
        pane_id = f"pane-{len(self.start_calls)}"
        self.descriptors[agent_id] = {
            "session": session,
            "name": agent_id,
            "kind": agent,
            "args": [],
            "agent": agent,
            "pane_id": pane_id,
            "workdir": workdir,
            "instance_id": agent_id,
            "state": "active",
            "project_id": kwargs["project_id"],
            "workspace_id": kwargs["workspace_id"],
        }
        self.live[agent_id] = {"pane_id": pane_id, "kind": agent, "status": "idle"}
        return {
            "available": True,
            "pane_id": pane_id,
            "instance_id": agent_id,
            "kind": agent,
        }

    def send(self, session: str, pane_id: str, prompt: str, mode: str = "prompt"):
        with self._send_lock:
            self.send_active += 1
            self.max_send_active = max(self.max_send_active, self.send_active)
        try:
            time.sleep(self.send_delay)
            self.send_calls.append((session, pane_id, prompt, mode))
            if isinstance(self.send_error, BaseException):
                raise self.send_error
            if self.send_error is not None:
                return self.send_error
            return {"available": True, "sent": prompt, "mode": mode}
        finally:
            with self._send_lock:
                self.send_active -= 1

    def read(self, session: str, pane_id: str, lines: int = 100, is_agent: bool = False):
        self.read_calls.append((session, pane_id, lines, is_agent))
        if isinstance(self.read_error, BaseException):
            raise self.read_error
        if self.read_error is not None:
            return self.read_error
        return {"available": True, "output": "user\nassistant"}


def controller(runtime: Runtime, *, registry: Registry | None = None):
    return workspace_agent.WorkspaceAgentController(
        registry_provider=lambda: registry or Registry(),
        session_provider=lambda: SESSION,
        session_bootstrap_provider=runtime.bootstrap,
        descriptor_list_provider=runtime.descriptor_list,
        descriptor_provider=runtime.descriptor,
        snapshot_provider=runtime.snapshot,
        start_provider=runtime.start,
        send_provider=runtime.send,
        read_provider=runtime.read,
        instance_id_factory=lambda: next(
            value for value in (AGENT_A, AGENT_B, "i-" + "c" * 26)
            if value not in runtime.descriptors
        ),
        receipt_path=runtime.receipt_path,
        pending_recovery_provider=runtime.recover,
    )


def assert_public(value: dict[str, object]) -> None:
    assert set(value) == {
        "agent_id", "project_id", "workspace_id", "kind", "status", "transcript",
    }
    encoded = repr(value)
    for forbidden in (PATH, SESSION, "pane-", "workdir", "argv", "env", "pid"):
        assert forbidden not in encoded


def test_start_attach_refresh_get_and_same_cwd_workspace_isolation() -> None:
    runtime = Runtime()
    first = controller(runtime)

    created = first.start(PROJECT, WORKSPACE_A, kind="codex", idempotency_key="start-a")
    assert_public(created)
    assert created["agent_id"] == AGENT_A
    assert created["status"] == "idle"
    assert len(runtime.start_calls) == 1
    assert runtime.bootstrap_calls == [SESSION]
    assert runtime.bootstrap_creates == 1
    assert runtime.start_calls[0] == {
        "session": SESSION,
        "workdir": PATH,
        "agent": "codex",
        "layout": "tab",
        "label": "codex",
        "args": "",
        "instance_id": AGENT_A,
        "project_id": PROJECT,
        "workspace_id": WORKSPACE_A,
    }
    assert first.start(
        PROJECT, WORKSPACE_A, kind="codex", idempotency_key="start-a",
    ) == created
    assert len(runtime.start_calls) == 1

    runtime.session_running = False
    assert first.start(
        PROJECT, WORKSPACE_A, kind="codex", idempotency_key="recover-session",
    ) == created
    assert runtime.bootstrap_creates == 2
    assert len(runtime.start_calls) == 1

    refreshed = controller(runtime)
    attached = refreshed.start(
        PROJECT, WORKSPACE_A, kind="codex", idempotency_key="attach-a",
    )
    assert attached == created
    assert refreshed.get(PROJECT, WORKSPACE_A, AGENT_A) == created
    assert len(runtime.start_calls) == 1

    other = refreshed.start(
        PROJECT, WORKSPACE_B, kind="codex", idempotency_key="start-b",
    )
    assert other["agent_id"] == AGENT_B
    assert len(runtime.start_calls) == 2
    with pytest.raises(workspace_agent.WorkspaceAgentError) as rejected:
        refreshed.get(PROJECT, WORKSPACE_B, AGENT_A)
    assert rejected.value.code == "agent_not_found"
    with pytest.raises(workspace_agent.WorkspaceAgentError) as rejected:
        refreshed.prompt(
            PROJECT, WORKSPACE_B, AGENT_A,
            prompt="hello", idempotency_key="cross-workspace",
        )
    assert rejected.value.code == "agent_not_found"


def test_first_install_session_bootstrap_is_concurrent_idempotent_and_fail_closed() -> None:
    runtime = Runtime()
    value = controller(runtime)
    results: list[dict[str, object]] = []

    def first_start(key: str) -> None:
        results.append(value.start(
            PROJECT, WORKSPACE_A, kind="claude", idempotency_key=key,
        ))

    threads = [
        threading.Thread(target=first_start, args=("first-a",)),
        threading.Thread(target=first_start, args=("first-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert len(results) == 2
    assert results[0]["agent_id"] == results[1]["agent_id"] == AGENT_A
    assert runtime.bootstrap_calls == [SESSION, SESSION]
    assert runtime.bootstrap_creates == 1
    assert len(runtime.start_calls) == 1

    failed = Runtime()
    failed.bootstrap_error = {
        "available": False,
        "error": "/private/herdr/session bootstrap failed",
    }
    with pytest.raises(workspace_agent.WorkspaceAgentError) as rejected:
        controller(failed).start(
            PROJECT, WORKSPACE_A, kind="codex", idempotency_key="first",
        )
    assert rejected.value.code == "workspace_agent_unavailable"
    assert str(rejected.value) == "workspace_agent_unavailable"
    assert failed.bootstrap_calls == [SESSION]
    assert failed.start_calls == []
    assert failed.descriptors == {}


def test_stable_instance_snapshot_reconciliation_ignores_stale_descriptor_pane() -> None:
    runtime = Runtime()
    runtime.descriptors[AGENT_A] = {
        "session": SESSION,
        "name": AGENT_A,
        "kind": "claude",
        "args": [],
        "agent": "claude",
        "pane_id": "stale-pane",
        "workdir": PATH,
        "instance_id": AGENT_A,
        "state": "active",
        "project_id": PROJECT,
        "workspace_id": WORKSPACE_A,
    }
    runtime.live[AGENT_A] = {
        "pane_id": "current-pane", "kind": "claude", "status": "working",
    }
    value = controller(runtime).get(PROJECT, WORKSPACE_A, AGENT_A)
    assert value["status"] == "working"
    assert runtime.read_calls[-1][1] == "current-pane"
    assert_public(value)

    runtime.live.clear()
    with pytest.raises(workspace_agent.WorkspaceAgentError) as missing:
        controller(runtime).get(PROJECT, WORKSPACE_A, AGENT_A)
    assert missing.value.code == "agent_not_found"

    runtime.live[AGENT_A] = {
        "pane_id": "current-pane", "kind": "claude", "status": "starting",
    }
    unknown = controller(runtime).get(PROJECT, WORKSPACE_A, AGENT_A)
    assert unknown["status"] == "unknown"
    assert_public(unknown)
    assert controller(runtime).prompt(
        PROJECT, WORKSPACE_A, AGENT_A,
        prompt="still live", idempotency_key="unknown-status",
    )["status"] == "unknown"
    assert [item[2] for item in runtime.send_calls] == ["still live"]


def test_prompt_replay_conflict_concurrency_and_prompt_only_transport() -> None:
    runtime = Runtime()
    value = controller(runtime)
    value.start(PROJECT, WORKSPACE_A, kind="kimi", idempotency_key="start")

    sent = value.prompt(
        PROJECT, WORKSPACE_A, AGENT_A,
        prompt="Enter ; --model $(id)", idempotency_key="prompt-one",
    )
    assert_public(sent)
    assert value.prompt(
        PROJECT, WORKSPACE_A, AGENT_A,
        prompt="Enter ; --model $(id)", idempotency_key="prompt-one",
    ) == sent
    assert runtime.send_calls == [(
        SESSION, "pane-1", "Enter ; --model $(id)", "prompt",
    )]
    with pytest.raises(workspace_agent.WorkspaceAgentError) as conflict:
        value.prompt(
            PROJECT, WORKSPACE_A, AGENT_A,
            prompt="different", idempotency_key="prompt-one",
        )
    assert conflict.value.code == "idempotency_conflict"

    results: list[dict[str, object]] = []
    failures: list[str] = []

    def concurrent_send() -> None:
        try:
            results.append(value.prompt(
                PROJECT, WORKSPACE_A, AGENT_A,
                prompt="concurrent", idempotency_key="prompt-concurrent",
            ))
        except workspace_agent.WorkspaceAgentError as exc:
            failures.append(exc.code)

    threads = [threading.Thread(target=concurrent_send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert failures == [] and len(results) == 2 and results[0] == results[1]
    assert [item[2] for item in runtime.send_calls].count("concurrent") == 1
    assert runtime.max_send_active == 1

    runtime.read_error = {
        "available": True, "error": "/private/read failed after send",
    }
    sent_without_read = value.prompt(
        PROJECT, WORKSPACE_A, AGENT_A,
        prompt="read may lag", idempotency_key="prompt-read-lag",
    )
    assert sent_without_read["transcript"] == ""
    assert value.prompt(
        PROJECT, WORKSPACE_A, AGENT_A,
        prompt="read may lag", idempotency_key="prompt-read-lag",
    ) == sent_without_read
    assert [item[2] for item in runtime.send_calls].count("read may lag") == 1


def test_prompt_receipts_survive_new_controller_eviction_and_crash_window() -> None:
    runtime = Runtime()
    runtime.send_delay = 0
    first = controller(runtime)
    first.start(PROJECT, WORKSPACE_A, kind="codex", idempotency_key="start")
    original = first.prompt(
        PROJECT, WORKSPACE_A, AGENT_A,
        prompt="durable", idempotency_key="durable-key",
    )

    marker = Path(runtime._temporary.name) / "unexpected-send"
    source = r'''
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from agent_cockpit.workspace_agent import WorkspaceAgentController

project, workspace, location, agent, session, workdir, receipt, marker = sys.argv[1:]

class Registry:
    def get_project_by_id(self, value):
        if value != project:
            return None
        return SimpleNamespace(project=SimpleNamespace(lifecycle="active"))
    def get_workspace(self, project_id, workspace_id):
        if project_id != project or workspace_id != workspace:
            return None
        return SimpleNamespace(
            workspace_id=workspace, project_id=project,
            repo_location_id=location, lifecycle="active",
            isolation_kind="shared",
        )
    def list_repo_locations(self, value):
        if value != project:
            return None
        return (SimpleNamespace(
            repo_location_id=location, lifecycle="active", node_id="local",
            availability="available", canonical_path=workdir,
        ),)

descriptor = {
    "session": session, "name": agent, "kind": "codex", "args": [],
    "agent": "codex", "pane_id": "stale", "workdir": workdir,
    "instance_id": agent, "state": "active", "project_id": project,
    "workspace_id": workspace,
}
snapshot = {
    "session": session,
    "panes": [{
        "pane_id": "live-pane", "session": session, "agent": "codex",
        "agent_status": "idle",
    }],
    "agents": [{"name": agent, "pane_id": "live-pane", "agent": "codex"}],
}

def forbidden_send(*args, **kwargs):
    Path(marker).write_text("dispatched", encoding="utf-8")
    return {"available": True}

controller = WorkspaceAgentController(
    registry_provider=Registry,
    session_provider=lambda: session,
    session_bootstrap_provider=lambda value: {
        "available": True, "session": value,
    },
    descriptor_list_provider=lambda *args: (descriptor,),
    descriptor_provider=lambda value: descriptor if value == agent else None,
    snapshot_provider=lambda value: snapshot,
    start_provider=lambda *args, **kwargs: {"available": False},
    send_provider=forbidden_send,
    read_provider=lambda *args, **kwargs: {"available": True, "output": ""},
    receipt_path=Path(receipt),
    pending_recovery_provider=lambda *args: None,
)
print(json.dumps(controller.prompt(
    project, workspace, agent, prompt="durable", idempotency_key="durable-key",
), sort_keys=True))
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    restarted_process = subprocess.run(
        [
            sys.executable, "-c", source, PROJECT, WORKSPACE_A, LOCATION,
            AGENT_A, SESSION, PATH, str(runtime.receipt_path), str(marker),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(restarted_process.stdout) == original
    assert not marker.exists()

    restarted = controller(runtime)
    assert restarted.prompt(
        PROJECT, WORKSPACE_A, AGENT_A,
        prompt="durable", idempotency_key="durable-key",
    ) == original
    assert [item[2] for item in runtime.send_calls].count("durable") == 1
    with pytest.raises(workspace_agent.WorkspaceAgentError) as conflict:
        restarted.prompt(
            PROJECT, WORKSPACE_A, AGENT_A,
            prompt="changed", idempotency_key="durable-key",
        )
    assert conflict.value.code == "idempotency_conflict"

    for index in range(260):
        restarted.prompt(
            PROJECT, WORKSPACE_A, AGENT_A,
            prompt=f"later-{index}", idempotency_key=f"later-{index}",
        )
    after_eviction_pressure = controller(runtime)
    assert after_eviction_pressure.prompt(
        PROJECT, WORKSPACE_A, AGENT_A,
        prompt="durable", idempotency_key="durable-key",
    ) == original
    assert [item[2] for item in runtime.send_calls].count("durable") == 1

    crash_digest = workspace_agent.WorkspaceAgentController._digest({
        "prompt": "crash-window",
    })
    restarted._prompt_receipts.reserve(
        PROJECT, WORKSPACE_A, AGENT_A, "crash-key", crash_digest,
    )
    after_crash = controller(runtime)
    with pytest.raises(workspace_agent.WorkspaceAgentError) as unknown:
        after_crash.prompt(
            PROJECT, WORKSPACE_A, AGENT_A,
            prompt="crash-window", idempotency_key="crash-key",
        )
    assert unknown.value.code == "agent_send_outcome_unknown"
    assert "crash-window" not in [item[2] for item in runtime.send_calls]
    with pytest.raises(workspace_agent.WorkspaceAgentError) as conflict:
        after_crash.prompt(
            PROJECT, WORKSPACE_A, AGENT_A,
            prompt="different", idempotency_key="crash-key",
        )
    assert conflict.value.code == "idempotency_conflict"


def test_invalid_inputs_authority_and_provider_failures_are_sanitized() -> None:
    runtime = Runtime()
    value = controller(runtime)
    for kind in ("qoder", "zcode", "", True, None):
        with pytest.raises(workspace_agent.WorkspaceAgentError) as rejected:
            value.start(
                PROJECT, WORKSPACE_A, kind=kind, idempotency_key="bad-kind",
            )
        assert rejected.value.code == "invalid_argument"
    for prompt in ("", "x" * (workspace_agent.MAX_PROMPT_LENGTH + 1), 7):
        with pytest.raises(workspace_agent.WorkspaceAgentError) as rejected:
            value.prompt(
                PROJECT, WORKSPACE_A, AGENT_A,
                prompt=prompt, idempotency_key="bad-prompt",
            )
        assert rejected.value.code == "invalid_argument"
    with pytest.raises(workspace_agent.WorkspaceAgentError) as rejected:
        value.get("prj_short", WORKSPACE_A, AGENT_A)
    assert rejected.value.code == "invalid_argument"
    with pytest.raises(workspace_agent.WorkspaceAgentError) as rejected:
        value.get(PROJECT, "ws_" + "0" * 32, AGENT_A)
    assert rejected.value.code == "project_or_workspace_not_found"

    runtime.start_error = {"available": True, "error": "/private/start detail"}
    failed_start = controller(runtime)
    with pytest.raises(workspace_agent.WorkspaceAgentError) as rejected:
        failed_start.start(
            PROJECT, WORKSPACE_A, kind="grok", idempotency_key="start-fail",
        )
    assert rejected.value.code == "agent_start_failed"
    assert str(rejected.value) == "agent_start_failed"
    with pytest.raises(workspace_agent.WorkspaceAgentError) as conflict:
        failed_start.start(
            PROJECT, WORKSPACE_A, kind="kimi", idempotency_key="start-fail",
        )
    assert conflict.value.code == "idempotency_conflict"
    runtime.start_error = None
    recovered = failed_start.start(
        PROJECT, WORKSPACE_A, kind="grok", idempotency_key="start-fail",
    )
    assert recovered["kind"] == "grok"
    assert len(runtime.start_calls) == 2
    assert len(runtime.live) == 1

    runtime = Runtime()
    live = controller(runtime)
    live.start(PROJECT, WORKSPACE_A, kind="opencode", idempotency_key="start-ok")
    runtime.send_error = {"available": True, "error": "/private/send detail"}
    for _attempt in range(2):
        with pytest.raises(workspace_agent.WorkspaceAgentError) as rejected:
            live.prompt(
                PROJECT, WORKSPACE_A, AGENT_A,
                prompt="hello", idempotency_key="send-fail",
            )
        assert rejected.value.code == "agent_send_outcome_unknown"
        assert str(rejected.value) == "agent_send_outcome_unknown"
    assert [item[2] for item in runtime.send_calls].count("hello") == 1


def test_retryable_start_cleanup_and_bootstrap_errors_reconcile_same_key() -> None:
    unavailable_runtime = Runtime()
    unavailable_runtime.bootstrap_error = {
        "available": False,
        "error": "/private/bootstrap unavailable detail",
    }
    unavailable = controller(unavailable_runtime)
    with pytest.raises(workspace_agent.WorkspaceAgentError) as failed:
        unavailable.start(
            PROJECT, WORKSPACE_A, kind="codex", idempotency_key="unavailable",
        )
    assert failed.value.code == "workspace_agent_unavailable"
    unavailable_runtime.bootstrap_error = None
    assert unavailable.start(
        PROJECT, WORKSPACE_A, kind="codex", idempotency_key="unavailable",
    )["agent_id"] == AGENT_A
    assert len(unavailable_runtime.bootstrap_calls) == 2
    assert len(unavailable_runtime.start_calls) == 1

    runtime = Runtime()
    runtime.bootstrap_error = {
        "available": False,
        "error_code": "session_cleanup_incomplete",
        "error": "/private/bootstrap cleanup detail",
    }
    value = controller(runtime)
    with pytest.raises(workspace_agent.WorkspaceAgentError) as cleanup:
        value.start(
            PROJECT, WORKSPACE_A, kind="codex", idempotency_key="bootstrap",
        )
    assert cleanup.value.code == "workspace_agent_cleanup_incomplete"
    runtime.bootstrap_error = None
    assert value.start(
        PROJECT, WORKSPACE_A, kind="codex", idempotency_key="bootstrap",
    )["agent_id"] == AGENT_A
    assert len(runtime.bootstrap_calls) == 2
    assert len(runtime.start_calls) == 1

    pending_runtime = Runtime()

    def uncertain_start(session: str, workdir: str, agent: str, **kwargs):
        pending_runtime.start_calls.append({
            "session": session, "workdir": workdir, "agent": agent, **kwargs,
        })
        agent_id = str(kwargs["instance_id"])
        pending_runtime.descriptors[agent_id] = {
            "session": session,
            "name": agent_id,
            "kind": agent,
            "args": [],
            "agent": agent,
            "pane_id": "pane-pending",
            "workdir": workdir,
            "instance_id": agent_id,
            "state": "pending",
            "project_id": kwargs["project_id"],
            "workspace_id": kwargs["workspace_id"],
        }
        pending_runtime.live[agent_id] = {
            "pane_id": "pane-pending", "kind": agent, "status": "idle",
        }
        return {
            "available": True,
            "error_code": "descriptor_cleanup_incomplete",
            "error": "/private/pending cleanup detail",
        }

    pending_runtime.start = uncertain_start
    pending = controller(pending_runtime)
    with pytest.raises(workspace_agent.WorkspaceAgentError) as cleanup:
        pending.start(
            PROJECT, WORKSPACE_A, kind="codex", idempotency_key="pending",
        )
    assert cleanup.value.code == "agent_start_cleanup_incomplete"
    attached = pending.start(
        PROJECT, WORKSPACE_A, kind="codex", idempotency_key="pending",
    )
    assert attached["agent_id"] == AGENT_A
    assert len(pending_runtime.start_calls) == 1
    assert pending_runtime.descriptors[AGENT_A]["state"] == "active"


def test_fresh_process_recovers_unbound_launch_label_without_orphan_duplicate(
    monkeypatch, tmp_path: Path,
) -> None:
    descriptor_path = tmp_path / "launch.json"
    monkeypatch.setenv("COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(descriptor_path))
    reserved = herdr_client.reserve_workspace_launch_descriptor(
        session=SESSION,
        name=AGENT_A,
        kind="codex",
        agent="codex",
        workdir=PATH,
        instance_id=AGENT_A,
        display_name="codex",
        project_id=PROJECT,
        workspace_id=WORKSPACE_A,
    )
    assert reserved["pane_id"] == ""
    launch_label = reserved["launch_label"]
    source = r'''
import json
import os
import sys
from agent_cockpit import herdr_client

descriptor_path, session, project, workspace, agent, workdir, label = sys.argv[1:]
os.environ["COCKPIT_LAUNCH_DESCRIPTORS_PATH"] = descriptor_path
before = {
    "session": session,
    "tabs": [
        {"tab_id": "tab-other", "workspace_id": "herdr-w1", "label": "other", "pane_count": 1},
        {"tab_id": "tab-target", "workspace_id": "herdr-w1", "label": label, "pane_count": 1},
    ],
    "panes": [
        {"pane_id": "pane-other", "tab_id": "tab-other", "cwd": "/repo/other", "agent": None},
        {"pane_id": "pane-target", "tab_id": "tab-target", "cwd": workdir, "agent": None},
    ],
    "agents": [],
}
after = {
    "session": session,
    "tabs": [
        {"tab_id": "tab-other", "workspace_id": "herdr-w1", "label": "other", "pane_count": 1},
    ],
    "panes": [
        {"pane_id": "pane-other", "tab_id": "tab-other", "cwd": "/repo/other", "agent": None},
    ],
    "agents": [],
}
calls = []
herdr_client.session_snapshot = lambda value: before
herdr_client._snapshot_session = lambda value: after
herdr_client._run = lambda args, timeout=10: calls.append(args) or ""
herdr_client.recover_workspace_launch_descriptors(session, project, workspace)
print(json.dumps({
    "calls": calls,
    "descriptor": herdr_client.get_launch_descriptor_by_instance(
        agent, include_retired=True,
    ),
}, sort_keys=True))
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    recovered = subprocess.run(
        [
            sys.executable, "-c", source, str(descriptor_path), SESSION,
            PROJECT, WORKSPACE_A, AGENT_A, PATH, launch_label,
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    evidence = json.loads(recovered.stdout)
    assert evidence == {
        "calls": [["--session", SESSION, "pane", "close", "pane-target"]],
        "descriptor": None,
    }

    runtime = Runtime()
    restarted = controller(runtime)
    created = restarted.start(
        PROJECT, WORKSPACE_A, kind="codex", idempotency_key="same-key",
    )
    assert restarted.start(
        PROJECT, WORKSPACE_A, kind="codex", idempotency_key="same-key",
    ) == created
    assert len(runtime.start_calls) == 1


@pytest.mark.parametrize(
    "case", ["absent", "absent_conflict", "duplicate", "cwd_mismatch"],
)
def test_unbound_pending_launch_label_recovery_is_exact_and_fail_closed(
    monkeypatch, tmp_path: Path, case: str,
) -> None:
    descriptor_path = tmp_path / f"{case}.json"
    monkeypatch.setenv("COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(descriptor_path))
    record = herdr_client.reserve_workspace_launch_descriptor(
        session=SESSION,
        name=AGENT_A,
        kind="codex",
        agent="codex",
        workdir=PATH,
        instance_id=AGENT_A,
        display_name="codex",
        project_id=PROJECT,
        workspace_id=WORKSPACE_A,
    )
    label = record["launch_label"]
    tabs: list[dict[str, object]] = []
    panes: list[dict[str, object]] = []
    if case not in {"absent", "absent_conflict"}:
        tabs.append({
            "tab_id": "tab-target",
            "workspace_id": "herdr-w1",
            "label": label,
            "pane_count": 1,
        })
        panes.append({
            "pane_id": "pane-target",
            "tab_id": "tab-target",
            "cwd": PATH if case == "duplicate" else "/repo/wrong",
            "agent": None,
        })
    if case == "duplicate":
        tabs.append({
            "tab_id": "tab-duplicate",
            "workspace_id": "herdr-w2",
            "label": label,
            "pane_count": 1,
        })
        panes.append({
            "pane_id": "pane-duplicate",
            "tab_id": "tab-duplicate",
            "cwd": PATH,
            "agent": None,
        })
    agents = (
        [{"name": AGENT_A, "pane_id": "pane-foreign", "agent": "codex"}]
        if case == "absent_conflict" else []
    )
    monkeypatch.setattr(
        herdr_client,
        "session_snapshot",
        lambda session: {
            "session": session, "tabs": tabs, "panes": panes, "agents": agents,
        },
    )
    if case == "absent":
        herdr_client.recover_workspace_launch_descriptors(
            SESSION, PROJECT, WORKSPACE_A,
        )
        assert herdr_client.get_launch_descriptor_by_instance(
            AGENT_A, include_retired=True,
        ) is None
    else:
        with pytest.raises(RuntimeError, match="pending descriptor launch"):
            herdr_client.recover_workspace_launch_descriptors(
                SESSION, PROJECT, WORKSPACE_A,
            )
        pending = herdr_client.get_launch_descriptor_by_instance(
            AGENT_A, include_retired=True,
        )
        assert pending["state"] == "pending"
        assert pending["pane_id"] == ""


def test_workspace_descriptor_helper_requires_exact_authority(monkeypatch, tmp_path) -> None:
    path = tmp_path / "descriptors.json"
    monkeypatch.setenv("COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(path))
    herdr_client.save_launch_descriptor(
        session=SESSION,
        pane_id="pane-a",
        name=AGENT_A,
        kind="codex",
        args=[],
        agent="codex",
        workdir=PATH,
        instance_id=AGENT_A,
        display_name="codex",
        project_id=PROJECT,
        workspace_id=WORKSPACE_A,
    )
    herdr_client.save_launch_descriptor(
        session=SESSION,
        pane_id="pane-b",
        name=AGENT_B,
        kind="codex",
        args=[],
        agent="codex",
        workdir=PATH,
        instance_id=AGENT_B,
        display_name="codex",
        project_id=PROJECT,
        workspace_id=WORKSPACE_B,
    )

    values = herdr_client.list_workspace_launch_descriptors(
        SESSION, PROJECT, WORKSPACE_A,
    )
    assert [item["instance_id"] for item in values] == [AGENT_A]
    assert values[0]["project_id"] == PROJECT
    assert values[0]["workspace_id"] == WORKSPACE_A
    with pytest.raises(ValueError):
        herdr_client.save_launch_descriptor(
            session=SESSION,
            pane_id="pane-c",
            name="i-" + "c" * 26,
            kind="codex",
            args=[],
            instance_id="i-" + "c" * 26,
            project_id=PROJECT,
        )

    path.write_text('{"schema":2,"descriptors":', encoding="utf-8")
    with pytest.raises(ValueError, match="store 损坏"):
        herdr_client.list_workspace_launch_descriptors(
            SESSION, PROJECT, WORKSPACE_A,
        )


def test_fixed_session_bootstrap_helper_handles_missing_existing_and_failure(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(herdr_client.next_profile, "require_session", lambda value: value)
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    expected_root = herdr_client._herdr_sessions_root() / SESSION
    monkeypatch.setattr(herdr_client, "list_sessions", lambda: [{
        "name": SESSION,
        "status": "running",
        "directory": str(expected_root),
        "socket": str(expected_root / "herdr.sock"),
    }])
    monkeypatch.setattr(
        herdr_client, "_snapshot_session",
        lambda session: {"session": session, "panes": [], "agents": []},
    )
    assert herdr_client.ensure_session(SESSION) == {
        "available": True, "session": SESSION, "created": False,
    }


def test_workspace_managed_start_finalize_failure_closes_exact_live_agent(
    monkeypatch, tmp_path: Path,
) -> None:
    descriptor_path = tmp_path / "launch.json"
    monkeypatch.setenv("COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(descriptor_path))
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "require_herdr_capabilities", lambda: {})
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: "/bin/true")
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    snapshots = iter([
        {"session": SESSION, "panes": [], "agents": []},
        {
            "session": SESSION,
            "panes": [{"pane_id": "pane-new", "tab_id": "tab-new"}],
            "agents": [],
        },
        {"session": SESSION, "panes": [], "agents": []},
    ])
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: next(snapshots),
    )
    calls: list[list[str]] = []

    def run(args, timeout=10):
        calls.append(args)
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"pane-new"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", run)
    monkeypatch.setattr(
        herdr_client, "activate_pending_workspace_launch_descriptor",
        lambda *_args: (_ for _ in ()).throw(OSError("private finalize detail")),
    )
    result = herdr_client.start_agent(
        SESSION, PATH, "codex", layout="tab", label="codex", args="",
        instance_id=AGENT_A, project_id=PROJECT, workspace_id=WORKSPACE_A,
    )
    assert result["descriptor_error"] == "launch descriptor unavailable"
    assert result["rolled_back"] is True
    assert [
        "--session", SESSION, "tab", "create", "--cwd", PATH,
        "--label", herdr_client._workspace_launch_label(AGENT_A),
    ] in calls
    assert ["--session", SESSION, "pane", "close", "pane-new"] in calls
    assert herdr_client.get_launch_descriptor_by_instance(
        AGENT_A, include_retired=True,
    ) is None

    monkeypatch.setattr(
        herdr_client, "_snapshot_session",
        lambda session: {"session": session, "panes": [], "agents": []},
    )
    rejected = herdr_client.start_agent(
        SESSION, PATH, "codex", layout="right", label="codex", args="",
        instance_id=AGENT_A, project_id=PROJECT, workspace_id=WORKSPACE_A,
    )
    assert rejected["error_code"] == "workspace_agent_layout_forbidden"

    monkeypatch.setattr(
        herdr_client,
        "_snapshot_session",
        lambda session: {
            "session": session,
            "panes": [],
            "agents": [],
            "tabs": [{
                "tab_id": "tab-collision",
                "label": herdr_client._workspace_launch_label(AGENT_A),
                "pane_count": 1,
            }],
        },
    )
    call_count = len(calls)
    collision = herdr_client.start_agent(
        SESSION, PATH, "codex", layout="tab", label="codex", args="",
        instance_id=AGENT_A, project_id=PROJECT, workspace_id=WORKSPACE_A,
    )
    assert collision["error_code"] == "descriptor_prepare_failed"
    assert len(calls) == call_count


def test_workspace_managed_start_keeps_and_recovers_pending_when_cleanup_unknown(
    monkeypatch, tmp_path: Path,
) -> None:
    descriptor_path = tmp_path / "launch.json"
    monkeypatch.setenv("COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(descriptor_path))
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "require_herdr_capabilities", lambda: {})
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: "/bin/true")
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    live = {
        "session": SESSION,
        "panes": [{
            "pane_id": "pane-new", "tab_id": "tab-new", "agent": "codex",
            "agent_status": "idle", "cwd": PATH,
        }],
        "agents": [{
            "name": AGENT_A, "pane_id": "pane-new", "agent": "codex",
        }],
    }
    snapshots = iter([
        {"session": SESSION, "panes": [], "agents": []},
        {
            "session": SESSION,
            "panes": [{"pane_id": "pane-new", "tab_id": "tab-new"}],
            "agents": [],
        },
        live,
    ])
    monkeypatch.setattr(
        herdr_client, "_snapshot_session", lambda session: next(snapshots),
    )

    def run(args, timeout=10):
        if "create" in args:
            return 'data: {"result":{"tab":{"focused_pane_id":"pane-new"}}}'
        return ""

    monkeypatch.setattr(herdr_client, "_run", run)
    original_activate = herdr_client.activate_pending_workspace_launch_descriptor
    monkeypatch.setattr(
        herdr_client, "activate_pending_workspace_launch_descriptor",
        lambda *_args: (_ for _ in ()).throw(OSError("private finalize detail")),
    )
    result = herdr_client.start_agent(
        SESSION, PATH, "codex", layout="tab", label="codex", args="",
        instance_id=AGENT_A, project_id=PROJECT, workspace_id=WORKSPACE_A,
    )
    assert result["error_code"] == "descriptor_cleanup_incomplete"
    pending = herdr_client.get_launch_descriptor_by_instance(
        AGENT_A, include_retired=True,
    )
    assert pending["state"] == "pending"
    assert pending["project_id"] == PROJECT
    assert pending["workspace_id"] == WORKSPACE_A
    assert pending["pane_id"] == "pane-new"

    monkeypatch.setattr(
        herdr_client, "activate_pending_workspace_launch_descriptor",
        original_activate,
    )
    monkeypatch.setattr(herdr_client, "session_snapshot", lambda session: live)
    herdr_client.recover_workspace_launch_descriptors(
        SESSION, PROJECT, WORKSPACE_A,
    )
    active = herdr_client.get_launch_descriptor_by_instance(AGENT_A)
    assert active["state"] == "active"
    assert active["pane_id"] == "pane-new"


def test_real_absent_session_bootstrap_uses_scoped_headless_server(
    monkeypatch,
) -> None:
    temporary = tempfile.TemporaryDirectory(prefix="ca-", dir="/tmp")
    isolated_root = Path(temporary.name)
    session = "ephemeral-" + uuid.uuid4().hex[:8]
    config_root = isolated_root / "x"
    state_root = isolated_root / "s"
    data_root = isolated_root / "d"
    config_path = isolated_root / "h" / "config.toml"
    config_path.parent.mkdir(parents=True, mode=0o700)
    state_root.mkdir(mode=0o700)
    data_root.mkdir(mode=0o700)
    config_path.write_text("onboarding = false\n", encoding="utf-8")
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_root))
    monkeypatch.setattr(
        herdr_client.next_profile, "require_session", lambda value: value,
    )
    monkeypatch.setattr(herdr_client.next_profile, "session", lambda: session)

    expected_root = config_root / "herdr" / "sessions" / session
    assert config_path.parent != expected_root.parents[1]
    try:
        results: list[dict[str, object]] = []

        def bootstrap() -> None:
            results.append(herdr_client.ensure_session(session))

        threads = [threading.Thread(target=bootstrap) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert len(results) == 2
        assert all(
            result.get("available") is True and result.get("session") == session
            for result in results
        )
        assert sum(result.get("created") is True for result in results) == 1
        owned = herdr_client._SESSION_BOOTSTRAP_PROCESSES[session]
        assert owned.poll() is None
        rows = herdr_client.list_sessions()
        assert rows == [{
            "name": session,
            "status": "running",
            "directory": str(expected_root),
            "socket": str(expected_root / "herdr.sock"),
        }]
        snapshot = herdr_client.session_snapshot(session)
        assert snapshot["session"] == session
        assert snapshot.get("error") is None
    finally:
        environment = dict(os.environ)
        subprocess.run(
            [herdr_client.HERDR_BIN, "--session", session, "server", "stop"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        owned = herdr_client._SESSION_BOOTSTRAP_PROCESSES.pop(session, None)
        if owned is not None:
            try:
                owned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                herdr_client._terminate_bootstrap_process(owned)
        temporary.cleanup()


def test_bootstrap_cleanup_keeps_process_owned_until_exit_is_confirmed(
    monkeypatch,
) -> None:
    session = "ephemeral-cleanup-test"

    class Process:
        def poll(self):
            return None

    process = Process()
    monkeypatch.setattr(
        herdr_client.next_profile, "require_session", lambda value: value,
    )
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_session_bootstrap_ready",
        lambda _session, _deadline: False,
    )
    monkeypatch.setattr(herdr_client, "SESSION_BOOTSTRAP_TIMEOUT_S", 0.01)
    monkeypatch.setattr(herdr_client.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(
        herdr_client, "_terminate_bootstrap_process", lambda value: False,
    )
    try:
        result = herdr_client.ensure_session(session)
        assert result == {
            "available": False,
            "error_code": "session_cleanup_incomplete",
            "error": "session bootstrap cleanup incomplete",
        }
        assert herdr_client._SESSION_BOOTSTRAP_PROCESSES[session] is process
    finally:
        herdr_client._SESSION_BOOTSTRAP_PROCESSES.pop(session, None)
