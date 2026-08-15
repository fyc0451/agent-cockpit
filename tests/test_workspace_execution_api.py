from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import git_checkout_provider as checkout_mod
from agent_cockpit import local_codex_harness as harness_mod
from agent_cockpit import operation_store as operation_mod
from agent_cockpit import project_registry_store as registry_store
from agent_cockpit import workspace_execution_api as api
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

    def build_launch_spec(self, checkout_path: Path) -> harness_mod.LaunchSpec:
        spec = harness_mod.LaunchSpec(
            "codex", "read-only", str(checkout_path), ("--sandbox", "read-only"),
            writable=False,
        )
        spec.assert_readonly()
        return spec

    def attach_readonly(self, *, session, checkout_path, instance_id=None, display_name="codex"):
        self.panes["pane-live"] = str(checkout_path)
        return harness_mod.AttachmentEvidence(
            session, instance_id or "inst-1", "pane-live", str(checkout_path),
            "local_herdr", "codex_terminal_managed_v1", True,
        )

    def observe(self, *, session, instance_id, pane_id, checkout_path):
        return harness_mod.AttachmentEvidence(
            session, instance_id, pane_id, str(checkout_path),
            "local_herdr", "codex_terminal_managed_v1", True,
        )

    def detach(self, *, session, pane_id):
        self.panes.pop(pane_id, None)


def _client(tmp_path: Path):
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
    service = service_mod.ExecutionService(
        registry_provider=lambda: registry,
        work_provider=lambda: work,
        store=exec_store.initialize(tmp_path / "workspace-execution.sqlite3"),
        operations=operation_mod.initialize(tmp_path / "ops" / "operation.sqlite3"),
        checkout=checkout_mod.GitCheckoutProvider(),
        harness=_FakeHarness(),
        worktrees_root=tmp_path / "worktrees",
    )
    app = FastAPI()
    api.install(app, service)
    return TestClient(app), project, workspace, created.item


def _members(project_id: str, workspace_id: str) -> str:
    return f"/api/projects/{project_id}/workspaces/{workspace_id}/members"


def _prep(project_id: str, workspace_id: str, work_item_id: str) -> str:
    return (
        f"/api/projects/{project_id}/workspaces/{workspace_id}"
        f"/work-items/{work_item_id}/preparation"
    )


def test_http_contract_prepare_attach_detach_and_hidden_fields(tmp_path: Path) -> None:
    http, project, workspace, item = _client(tmp_path)
    members = _members(project.project_id, workspace.workspace_id)
    created = http.post(
        members, json={"display_name": "Atlas"},
        headers={"Idempotency-Key": "member"},
    )
    assert created.status_code == 201
    identity = created.json()["data"]
    assert set(identity) == {
        "identity_id", "display_name", "role", "lifecycle", "revision",
    }
    listed = http.get(members)
    assert listed.status_code == 200
    assert listed.json()["data"]["items"] == [identity]
    prep_url = _prep(
        project.project_id, workspace.workspace_id, item.work_item["work_item_id"],
    )
    assert http.post(
        f"/api/project-registry/projects/{project.project_id}/workspaces/"
        f"{workspace.workspace_id}/members",
        json={"display_name": "Atlas"},
        headers={"Idempotency-Key": "old"},
    ).status_code == 404
    missing_key = http.post(prep_url, json={"identity_id": identity["identity_id"]})
    assert missing_key.json()["error"]["code"] == "idempotency_key_required"
    prepared = http.post(
        prep_url, json={"identity_id": identity["identity_id"]},
        headers={"Idempotency-Key": "prep"},
    )
    assert prepared.status_code == 201
    payload = prepared.json()["data"]
    assert payload["state"] == "prepared"
    assert payload["work_item_status"] == "unassigned"
    assert payload["lease"]["status"] == "reserved"
    text = prepared.text
    assert "fence_digest" not in text
    assert "pane_id" not in text
    assert "internal_path" not in text
    assert "/repo" not in text
    loaded = http.get(prep_url)
    assert loaded.status_code == 200
    assert loaded.json()["data"]["checkout"]["checkout_id"] == payload["checkout"]["checkout_id"]
    attached = http.post(
        prep_url + "/attach", json={"expected_revision": payload["revision"]},
        headers={"Idempotency-Key": "att"},
    )
    assert attached.status_code == 200
    assert attached.json()["data"]["state"] == "connected_readonly"
    assert attached.json()["data"]["attachment"]["identity_verified"] is True
    assert "pane-live" not in attached.text
    stale = http.post(
        prep_url + "/detach", json={"expected_revision": 1},
        headers={"Idempotency-Key": "stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"
    detached = http.post(
        prep_url + "/detach",
        json={"expected_revision": attached.json()["data"]["revision"]},
        headers={"Idempotency-Key": "det"},
    )
    assert detached.status_code == 200
    data = detached.json()["data"]
    assert data["state"] == "detached"
    assert data["identity"]["display_name"] == "Atlas"
    assert data["checkout"] is not None
