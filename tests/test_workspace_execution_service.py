from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent_cockpit import git_checkout_provider as checkout_mod
from agent_cockpit import local_codex_harness as harness_mod
from agent_cockpit import operation_store as operation_mod
from agent_cockpit import project_registry_store as registry_store
from agent_cockpit import workspace_execution_service as service_mod
from agent_cockpit import workspace_execution_store as exec_store
from agent_cockpit import workspace_work_store as work_store


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "dev@example.com")
    _git(path, "config", "user.name", "dev")
    (path / "README").write_text("hello\n")
    _git(path, "add", "README")
    _git(path, "commit", "-m", "init")
    return path


class _FakeHarness:
    def __init__(self) -> None:
        self.panes: dict[str, str] = {}
        self.calls: list[str] = []

    def build_launch_spec(self, checkout_path: Path) -> harness_mod.LaunchSpec:
        spec = harness_mod.LaunchSpec(
            "codex", "read-only", str(checkout_path), ("--sandbox", "read-only"),
            writable=False,
        )
        spec.assert_readonly()
        return spec

    def attach_readonly(self, *, session, checkout_path, instance_id=None, display_name="codex"):
        self.calls.append("attach")
        pane = "pane-live"
        self.panes[pane] = str(checkout_path)
        return harness_mod.AttachmentEvidence(
            session, instance_id or "inst-1", pane, str(checkout_path),
            "local_herdr", "codex_terminal_managed_v1", True,
        )

    def observe(self, *, session, instance_id, pane_id, checkout_path):
        self.calls.append("observe")
        return harness_mod.AttachmentEvidence(
            session, instance_id, pane_id, str(checkout_path),
            "local_herdr", "codex_terminal_managed_v1", True,
        )

    def detach(self, *, session, pane_id):
        self.calls.append("detach")
        if pane_id not in self.panes:
            raise harness_mod.HarnessError("process_exited")
        self.panes.pop(pane_id)


def _world(tmp_path: Path):
    source = _repo(tmp_path / "repo")
    registry = registry_store.initialize(tmp_path / "project-registry.sqlite3")
    project = registry.create_project(slug="alpha", display_name="Alpha", goal=None)
    location = registry.add_repo_location(
        project_id=project.project_id, node_id="local",
        canonical_path=str(source), vcs_kind="git", availability="available",
    )
    workspace = registry.create_workspace(
        project_id=project.project_id, repo_location_id=location.repo_location_id,
        name="main", goal=None, isolation_kind="shared",
    )
    work = work_store.initialize(tmp_path / "workspace-work.sqlite3")
    created = work.create_work_item(
        project_id=project.project_id, workspace_id=workspace.workspace_id,
        body="Fix login", acceptance=None, constraints=None,
        idempotency_key="work-1",
    )
    execution = exec_store.initialize(tmp_path / "workspace-execution.sqlite3")
    operations = operation_mod.initialize(tmp_path / "ops" / "operation.sqlite3")
    harness = _FakeHarness()
    service = service_mod.ExecutionService(
        registry_provider=lambda: registry,
        work_provider=lambda: work,
        store=execution,
        operations=operations,
        checkout=checkout_mod.GitCheckoutProvider(),
        harness=harness,
        worktrees_root=tmp_path / "worktrees",
    )
    return service, project, workspace, created.item, source, harness, operations


def test_prepare_attach_detach_leaves_source_and_operation_receipts(tmp_path: Path) -> None:
    service, project, workspace, item, source, harness, operations = _world(tmp_path)
    before = checkout_mod.GitCheckoutProvider().inspect_source(source)
    member = service.create_member(
        project.project_id, workspace.workspace_id, display_name="Atlas",
        idempotency_key="member",
    )
    prepared = service.prepare(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        identity_id=member.item.identity_id, idempotency_key="prep",
    )
    assert prepared["state"] == "prepared"
    assert prepared["lease"]["status"] == "reserved"
    assert prepared["work_item_status"] == "unassigned"
    replay = service.prepare(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        identity_id=member.item.identity_id, idempotency_key="prep",
    )
    assert replay == prepared
    attached = service.attach(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        expected_revision=prepared["revision"], idempotency_key="att",
    )
    assert attached["state"] == "connected_readonly"
    assert attached["attachment"]["harness"] == "codex_terminal_managed_v1"
    assert attached["attachment"]["identity_verified"] is True
    assert "pane-live" not in str(attached)
    detached = service.detach(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        expected_revision=attached["revision"], idempotency_key="det",
    )
    assert detached["state"] == "detached"
    assert detached["lease"]["status"] == "revoked"
    assert detached["identity"]["identity_id"] == member.item.identity_id
    assert detached["checkout"]["checkout_id"] == prepared["checkout"]["checkout_id"]
    assert harness.calls == ["attach", "detach"]
    assert harness.panes == {}
    after = checkout_mod.GitCheckoutProvider().inspect_source(source)
    assert after == before
    with sqlite3.connect(operations.path) as connection:
        kinds = {
            row[0] for row in connection.execute("SELECT kind FROM operations")
        }
        receipts = connection.execute("SELECT count(*) FROM operation_receipts").fetchone()[0]
    assert "checkout.create" in kinds
    assert "runtime.attach" in kinds
    assert "runtime.detach" in kinds
    assert receipts >= 3


def test_dirty_source_is_zero_write(tmp_path: Path) -> None:
    service, project, workspace, item, source, _harness, operations = _world(tmp_path)
    member = service.create_member(
        project.project_id, workspace.workspace_id, display_name="Atlas",
        idempotency_key="member",
    )
    (source / "README").write_text("dirty\n")
    with pytest.raises(service_mod.ExecutionServiceError) as dirty:
        service.prepare(
            project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
            identity_id=member.item.identity_id, idempotency_key="prep",
        )
    assert dirty.value.code == "source_dirty"
    assert service.store.get_preparation(
        project_id=project.project_id, workspace_id=workspace.workspace_id,
        work_item_id=item.work_item["work_item_id"],
    ) is None
    with sqlite3.connect(operations.path) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
