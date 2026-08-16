from __future__ import annotations

import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

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

    def attach_readonly(
        self, *, session, checkout_path, project_id, workspace_id,
        instance_id=None, display_name="codex", **_kwargs,
    ):
        self.calls.append("attach")
        self.authority = (project_id, workspace_id)
        if getattr(self, "fail_once", False):
            self.fail_once = False
            raise harness_mod.HarnessError("runtime_unavailable")
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
        mode = getattr(self, "fail_detach", None)
        if mode == "known":
            raise harness_mod.HarnessError("runtime_unavailable")
        if mode == "unknown":
            raise RuntimeError("detach lost")
        if pane_id not in self.panes:
            raise harness_mod.HarnessError("process_exited")
        self.panes.pop(pane_id)

    def confirm_absent(self, *, session, pane_id, instance_id=None) -> bool:
        return pane_id not in self.panes


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
    assert harness.authority == (project.project_id, workspace.workspace_id)
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


def test_concurrent_same_key_prepare_creates_one_worktree(tmp_path: Path) -> None:
    service, project, workspace, item, source, _harness, _operations = _world(tmp_path)
    member = service.create_member(
        project.project_id, workspace.workspace_id, display_name="Atlas",
        idempotency_key="member",
    )
    barrier = Barrier(2)

    def worker():
        barrier.wait()
        return service.prepare(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            identity_id=member.item.identity_id, idempotency_key="same",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(worker) for _ in range(2)]]
    assert results[0] == results[1]
    assert results[0]["state"] == "prepared"
    root = tmp_path / "worktrees" / "managed-checkouts"
    dests = [path for path in root.iterdir() if path.is_dir()]
    assert len(dests) == 1
    assert dests[0].name == results[0]["checkout"]["checkout_id"]
    with sqlite3.connect(service.store.path) as connection:
        assert connection.execute("SELECT count(*) FROM work_item_preparations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM managed_checkouts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM idempotency_records").fetchone()[0] == 2
    after = checkout_mod.GitCheckoutProvider().inspect_source(source)
    assert after.clean is True


def test_known_attach_failure_returns_to_retryable_prepared(tmp_path: Path) -> None:
    service, project, workspace, item, _source, harness, _operations = _world(tmp_path)
    member = service.create_member(
        project.project_id, workspace.workspace_id, display_name="Atlas",
        idempotency_key="member",
    )
    prepared = service.prepare(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        identity_id=member.item.identity_id, idempotency_key="prep",
    )
    harness.fail_once = True
    with pytest.raises(service_mod.ExecutionServiceError) as failed:
        service.attach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=prepared["revision"], idempotency_key="att-fail",
        )
    assert failed.value.code == "runtime_unavailable"
    after_fail = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert after_fail.state == "prepared"
    assert after_fail.lease is not None
    assert after_fail.lease.status == "reserved"
    retried = service.attach(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        expected_revision=after_fail.revision, idempotency_key="att-retry",
    )
    assert retried["state"] == "connected_readonly"
    assert retried["attachment"]["identity_verified"] is True


class _WiredHerdr:
    def __init__(self, *, descriptors: bool = True, close: str = "ok", explode: bool = False) -> None:
        self.descriptors = descriptors
        self.close = close
        self.explode = explode
        self.panes: dict[str, str] = {}
        self.started = 0
        self.closed = 0
        self.seq = 0

    def start_workspace_codex_home(
        self, *, session: str, workdir: str, instance_id: str,
        project_id: str, workspace_id: str, codex_home: str,
        label: str | None = None, display_name: str | None = None,
    ) -> dict[str, object]:
        self.seq += 1
        pane_id = f"pane-{self.seq}"
        self.started += 1
        self.panes[pane_id] = workdir
        self.last_home = codex_home
        return {
            "available": True, "pane_id": pane_id, "instance_id": instance_id,
            "cwd": workdir,
        }

    def get_launch_descriptor(self, session: str, pane_id: str) -> dict[str, object] | None:
        if not self.descriptors or pane_id not in self.panes:
            return None
        return {
            "session": session, "pane_id": pane_id,
            "instance_id": "i-abcdefghijklmnopqrstuvwxyz",
            "workdir": self.panes.get(pane_id), "kind": "codex",
        }

    def get_launch_descriptor_by_instance(
        self, instance_id: str, *, include_retired: bool = False,
    ) -> dict[str, object] | None:
        if not self.descriptors or not self.panes:
            return None
        pane_id = next(iter(self.panes))
        return {
            "session": "cockpit-b-readonly", "pane_id": pane_id,
            "instance_id": instance_id, "workdir": self.panes.get(pane_id),
            "kind": "codex",
        }

    def session_snapshot(self, session: str) -> dict[str, object]:
        if self.explode:
            raise RuntimeError("snapshot lost")
        return {
            "panes": [
                {"pane_id": pane, "cwd": cwd} for pane, cwd in self.panes.items()
            ],
        }

    def close_pane(self, session: str, pane_id: str) -> dict[str, object]:
        if self.close == "raise":
            raise RuntimeError("close lost")
        if self.close == "fail":
            return {"available": False}
        if self.close == "zombie":
            self.closed += 1
            return {"available": True, "closed": pane_id}
        self.closed += 1
        self.panes.pop(pane_id, None)
        return {"available": True, "closed": pane_id}

    def ensure_session(self, session: str) -> dict[str, object]:
        return {"ok": True}


def _wired_world(tmp_path: Path, herdr: _WiredHerdr):
    service, project, workspace, item, source, _fake, operations = _world(tmp_path)
    harness = harness_mod.LocalCodexHarness(
        capability_root=tmp_path / "caps",
        ensure_session=herdr.ensure_session,
        start_workspace_codex_home=herdr.start_workspace_codex_home,
        get_launch_descriptor=herdr.get_launch_descriptor,
        get_launch_descriptor_by_instance=herdr.get_launch_descriptor_by_instance,
        snapshot=herdr.session_snapshot,
        close_pane=herdr.close_pane,
        new_instance_id=lambda: "i-abcdefghijklmnopqrstuvwxyz",
    )
    wired = service_mod.ExecutionService(
        registry_provider=service.registry_provider,
        work_provider=service.work_provider,
        store=service.store,
        operations=operations,
        checkout=service.checkout,
        harness=harness,
        worktrees_root=service.worktrees_root,
    )
    return wired, project, workspace, item, herdr


def test_known_unverified_attach_closes_pane_then_prepared(tmp_path: Path) -> None:
    herdr = _WiredHerdr(descriptors=False, close="ok")
    service, project, workspace, item, herdr = _wired_world(tmp_path, herdr)
    member = service.create_member(
        project.project_id, workspace.workspace_id, display_name="Atlas",
        idempotency_key="member",
    )
    prepared = service.prepare(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        identity_id=member.item.identity_id, idempotency_key="prep",
    )
    with pytest.raises(service_mod.ExecutionServiceError) as failed:
        service.attach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=prepared["revision"], idempotency_key="att-bad",
        )
    assert failed.value.code == "runtime_identity_unverified"
    after = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert after.state == "prepared"
    assert after.principal["generation"] == after.lease.generation == 1
    assert herdr.started == 1


def test_attach_passes_current_workspace_authority_to_harness(tmp_path: Path) -> None:
    herdr = _WiredHerdr()
    seen: list[tuple[object, object]] = []
    original = herdr.start_workspace_codex_home

    def start_workspace_codex_home(*args, **kwargs):
        seen.append((kwargs.get("project_id"), kwargs.get("workspace_id")))
        return original(*args, **kwargs)

    herdr.start_workspace_codex_home = start_workspace_codex_home  # type: ignore[method-assign]
    service, project, workspace, item, herdr = _wired_world(tmp_path, herdr)
    member = service.create_member(
        project.project_id, workspace.workspace_id, display_name="Atlas",
        idempotency_key="member",
    )
    prepared = service.prepare(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        identity_id=member.item.identity_id, idempotency_key="prep",
    )
    attached = service.attach(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        expected_revision=prepared["revision"], idempotency_key="att",
    )
    assert attached["state"] == "connected_readonly"
    assert seen == [(project.project_id, workspace.workspace_id)]


def test_unknown_close_after_identity_fail_is_outcome_unknown(tmp_path: Path) -> None:
    herdr = _WiredHerdr(descriptors=False, close="raise")
    service, project, workspace, item, herdr = _wired_world(tmp_path, herdr)
    member = service.create_member(
        project.project_id, workspace.workspace_id, display_name="Atlas",
        idempotency_key="member",
    )
    prepared = service.prepare(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        identity_id=member.item.identity_id, idempotency_key="prep",
    )
    with pytest.raises(service_mod.ExecutionServiceError) as failed:
        service.attach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=prepared["revision"], idempotency_key="att-unk",
        )
    assert failed.value.code == "runtime_unavailable"
    after = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert after.state == "outcome_unknown"
    assert after.principal["generation"] == after.lease.generation == 1
    assert herdr.panes == {"pane-1": herdr.panes["pane-1"]}
    with pytest.raises(service_mod.ExecutionServiceError) as replay:
        service.attach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=prepared["revision"], idempotency_key="att-unk",
        )
    assert replay.value.code == "runtime_unavailable"
    assert herdr.started == 1
    again = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert again.state == "outcome_unknown"
    assert again.principal["generation"] == again.lease.generation == 1
    assert again.attachment is not None
    assert again.attachment.generation == 1
    assert herdr.panes.keys() == {"pane-1"}


def test_unknown_attach_retry_keeps_one_pane_and_same_generation(tmp_path: Path) -> None:
    herdr = _WiredHerdr(descriptors=True, close="ok", explode=True)
    service, project, workspace, item, herdr = _wired_world(tmp_path, herdr)
    member = service.create_member(
        project.project_id, workspace.workspace_id, display_name="Atlas",
        idempotency_key="member",
    )
    prepared = service.prepare(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        identity_id=member.item.identity_id, idempotency_key="prep",
    )
    with pytest.raises(service_mod.ExecutionServiceError):
        service.attach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=prepared["revision"], idempotency_key="att-lost",
        )
    first = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert first.state == "outcome_unknown"
    assert first.principal["generation"] == first.lease.generation == 1
    herdr.explode = False
    with pytest.raises(
        (service_mod.ExecutionServiceError, exec_store.WorkspaceExecutionError),
    ) as stale:
        service.attach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=prepared["revision"], idempotency_key="att-new",
        )
    assert stale.value.code == "stale_revision"
    recovered = service.attach(
        project.project_id, workspace.workspace_id,
        item.work_item["work_item_id"],
        expected_revision=first.revision, idempotency_key="att-new",
    )
    assert recovered["state"] == "connected_readonly"
    assert recovered["principal"]["generation"] == recovered["lease"]["generation"] == 1
    assert recovered["attachment"]["generation"] == 1
    assert herdr.started == 2
    assert len(herdr.panes) == 1


def test_detach_known_close_failure_restores_connected(tmp_path: Path) -> None:
    service, project, workspace, item, _source, harness, _operations = _world(tmp_path)
    member = service.create_member(
        project.project_id, workspace.workspace_id, display_name="Atlas",
        idempotency_key="member",
    )
    prepared = service.prepare(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        identity_id=member.item.identity_id, idempotency_key="prep",
    )
    attached = service.attach(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        expected_revision=prepared["revision"], idempotency_key="att",
    )
    harness.fail_detach = "known"
    with pytest.raises(service_mod.ExecutionServiceError) as failed:
        service.detach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=attached["revision"], idempotency_key="det-fail",
        )
    assert failed.value.code == "runtime_unavailable"
    after = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert after.state == "connected_readonly"
    assert after.lease.status == "reserved"
    assert after.principal["generation"] == after.lease.generation == 1
    assert "pane-live" in harness.panes
    with pytest.raises(service_mod.ExecutionServiceError) as replay:
        service.detach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=attached["revision"], idempotency_key="det-fail",
        )
    assert replay.value.code == "runtime_unavailable"
    harness.fail_detach = None
    closed = service.detach(
        project.project_id, workspace.workspace_id,
        item.work_item["work_item_id"],
        expected_revision=after.revision, idempotency_key="det-retry",
    )
    assert closed["state"] == "detached"
    assert closed["lease"]["status"] == "revoked"
    assert harness.panes == {}


def test_detach_unknown_keeps_outcome_unknown_same_generation(tmp_path: Path) -> None:
    service, project, workspace, item, _source, harness, _operations = _world(tmp_path)
    member = service.create_member(
        project.project_id, workspace.workspace_id, display_name="Atlas",
        idempotency_key="member",
    )
    prepared = service.prepare(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        identity_id=member.item.identity_id, idempotency_key="prep",
    )
    attached = service.attach(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        expected_revision=prepared["revision"], idempotency_key="att",
    )
    harness.fail_detach = "unknown"
    with pytest.raises(service_mod.ExecutionServiceError) as failed:
        service.detach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=attached["revision"], idempotency_key="det-lost",
        )
    assert failed.value.code == "runtime_unavailable"
    after = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert after.state == "outcome_unknown"
    assert after.lease.status == "reserved"
    assert after.principal["generation"] == after.lease.generation == 1
    assert after.attachment is not None
    assert after.attachment.generation == 1
    with pytest.raises(service_mod.ExecutionServiceError) as replay:
        service.detach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=attached["revision"], idempotency_key="det-lost",
        )
    assert replay.value.code == "runtime_unavailable"
    again = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert again.state == "outcome_unknown"
    assert again.principal["generation"] == 1
    assert "pane-live" in harness.panes


def _member_prepared(service, project, workspace, item):
    member = service.create_member(
        project.project_id, workspace.workspace_id, display_name="Atlas",
        idempotency_key="member",
    )
    prepared = service.prepare(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        identity_id=member.item.identity_id, idempotency_key="prep",
    )
    return prepared


def test_unknown_retry_zombie_close_does_not_start_second_pane(tmp_path: Path) -> None:
    herdr = _WiredHerdr(descriptors=True, close="ok", explode=True)
    service, project, workspace, item, herdr = _wired_world(tmp_path, herdr)
    prepared = _member_prepared(service, project, workspace, item)
    with pytest.raises(service_mod.ExecutionServiceError):
        service.attach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=prepared["revision"], idempotency_key="att-lost",
        )
    first = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert first.state == "outcome_unknown"
    herdr.explode = False
    herdr.close = "zombie"
    with pytest.raises(service_mod.ExecutionServiceError) as blocked:
        service.attach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=first.revision, idempotency_key="att-retry",
        )
    assert blocked.value.code == "runtime_unavailable"
    after = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert after.state == "outcome_unknown"
    assert after.principal["generation"] == after.lease.generation == 1
    assert herdr.started == 1
    assert list(herdr.panes) == ["pane-1"]


def test_finish_attach_write_failure_is_replayable_not_stale(tmp_path: Path, monkeypatch) -> None:
    service, project, workspace, item, _source, harness, _operations = _world(tmp_path)
    prepared = _member_prepared(service, project, workspace, item)
    real = service.store.finish_attach

    def boom(**kwargs):
        raise exec_store.WorkspaceExecutionError("store_write_failed")

    monkeypatch.setattr(service.store, "finish_attach", boom)
    with pytest.raises(
        (service_mod.ExecutionServiceError, exec_store.WorkspaceExecutionError),
    ) as failed:
        service.attach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=prepared["revision"], idempotency_key="att-write",
        )
    assert failed.value.code == "store_write_failed"
    after = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert after.state in {"prepared", "outcome_unknown"}
    assert after.state != "attaching"
    with pytest.raises(
        (service_mod.ExecutionServiceError, exec_store.WorkspaceExecutionError),
    ) as replay:
        service.attach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=prepared["revision"], idempotency_key="att-write",
        )
    assert replay.value.code == "store_write_failed"
    monkeypatch.setattr(service.store, "finish_attach", real)
    if after.state == "prepared":
        recovered = service.attach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=after.revision, idempotency_key="att-after",
        )
        assert recovered["state"] == "connected_readonly"
        assert len(harness.panes) <= 1


def test_stale_lease_generation_is_rejected_with_zero_side_effects(tmp_path: Path) -> None:
    service, project, workspace, item, _source, harness, _operations = _world(tmp_path)
    prepared = _member_prepared(service, project, workspace, item)
    with sqlite3.connect(service.store.path) as connection:
        connection.execute("UPDATE writer_leases SET generation=2")
        connection.commit()
    with pytest.raises(
        (service_mod.ExecutionServiceError, exec_store.WorkspaceExecutionError),
    ) as conflict:
        service.attach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=prepared["revision"], idempotency_key="att-fence",
        )
    assert conflict.value.code == "lease_conflict"
    after = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert after.state == "prepared"
    assert after.principal["generation"] == 1
    assert after.lease.generation == 2
    assert harness.panes == {}
    assert "attach" not in harness.calls


def test_real_harness_detach_transport_loss_is_outcome_unknown(tmp_path: Path) -> None:
    herdr = _WiredHerdr(descriptors=True, close="ok")
    service, project, workspace, item, herdr = _wired_world(tmp_path, herdr)
    prepared = _member_prepared(service, project, workspace, item)
    attached = service.attach(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
        expected_revision=prepared["revision"], idempotency_key="att",
    )
    herdr.close = "raise"
    with pytest.raises(service_mod.ExecutionServiceError) as failed:
        service.detach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=attached["revision"], idempotency_key="det-loss",
        )
    assert failed.value.code == "runtime_unavailable"
    after = service.get_preparation(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert after.state == "outcome_unknown"
    assert after.lease.status == "reserved"
    assert after.principal["generation"] == after.lease.generation == 1
    assert list(herdr.panes) == ["pane-1"]
    with pytest.raises(service_mod.ExecutionServiceError) as replay:
        service.detach(
            project.project_id, workspace.workspace_id,
            item.work_item["work_item_id"],
            expected_revision=attached["revision"], idempotency_key="det-loss",
        )
    assert replay.value.code == "runtime_unavailable"
