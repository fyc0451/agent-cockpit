from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import project_registry_domain as registry_domain
from agent_cockpit import project_registry_store as registry_store
from agent_cockpit import workspace_work_api as api
from agent_cockpit import workspace_work_store as work_store


PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
OTHER_PROJECT = "prj_" + "c" * 32
OTHER_WORKSPACE = "ws_" + "d" * 32
STAMP = "2026-08-15T00:00:00.000000Z"


def _snapshot(project_id: str, *, lifecycle: str = "active") -> registry_domain.ProjectSnapshot:
    return registry_domain.ProjectSnapshot(
        registry_domain.ProjectRecord(
            project_id, "alpha", "Alpha", None, lifecycle, 1, STAMP, STAMP,
        ),
        (),
    )


def _workspace(
    project_id: str, workspace_id: str, *, lifecycle: str = "active",
) -> registry_domain.WorkspaceRecord:
    return registry_domain.WorkspaceRecord(
        workspace_id, project_id, "loc_" + "0" * 32, "main", None, "shared",
        lifecycle, None, 1, STAMP, STAMP,
    )


class _Registry:
    def __init__(self) -> None:
        self.projects: dict[str, registry_domain.ProjectSnapshot] = {}
        self.workspaces: dict[tuple[str, str], registry_domain.WorkspaceRecord] = {}

    def add(
        self, project_id: str, workspace_id: str, *,
        project_lifecycle: str = "active", lifecycle: str = "active",
    ) -> None:
        self.projects.setdefault(project_id, _snapshot(project_id, lifecycle=project_lifecycle))
        self.workspaces[(project_id, workspace_id)] = _workspace(
            project_id, workspace_id, lifecycle=lifecycle,
        )

    def get_project_by_id(self, project_id: str) -> registry_domain.ProjectSnapshot | None:
        return self.projects.get(project_id)

    def get_workspace(
        self, project_id: str, workspace_id: str,
    ) -> registry_domain.WorkspaceRecord | None:
        return self.workspaces.get((project_id, workspace_id))


def _path(project_id: str, workspace_id: str) -> str:
    return f"/api/projects/{project_id}/workspaces/{workspace_id}/work-items"


def _body(**changes):
    payload = {"body": "Save the original Boss question", "acceptance": None, "constraints": None}
    payload.update(changes)
    return payload


def _assert_g3(response, status: int = 200):
    assert response.status_code == status
    payload = response.json()
    assert set(payload) == {"data", "meta"}
    assert set(payload["meta"]) >= {
        "request_id", "generated_at", "partial", "sources", "capabilities",
    }
    assert payload["meta"]["request_id"].startswith("req_")
    return payload


def _assert_error(response, status: int, code: str, *, retryable: bool = False):
    assert response.status_code == status
    error = response.json()["error"]
    assert set(error) == {"code", "message", "retryable", "request_id", "details"}
    assert error["code"] == code
    assert error["retryable"] is retryable
    assert error["request_id"].startswith("req_")
    return error


def _assert_aggregate(item: dict, *, project_id: str, workspace_id: str, body: str) -> None:
    assert set(item) == {"thread", "root_message", "work_item"}
    assert item["thread"]["project_id"] == project_id
    assert item["thread"]["workspace_id"] == workspace_id
    assert item["root_message"]["author_kind"] == "boss"
    assert item["root_message"]["author_ref"] is None
    assert item["root_message"]["body"] == body
    assert item["work_item"]["source_message_id"] == item["root_message"]["message_id"]
    assert item["work_item"]["status"] == "unassigned"
    assert "item" not in item
    assert "source_message" not in item
    assert "author_type" not in item["root_message"]
    assert "workspace_id" not in item["work_item"]


def _client(tmp_path: Path):
    registry = _Registry()
    registry.add(PROJECT, WORKSPACE)
    store = work_store.initialize(tmp_path / "workspace-work.sqlite3")
    app = FastAPI()
    api.install(app, api.ApiService(lambda: registry, lambda: store))
    return TestClient(app), registry, store


def test_post_then_get_returns_g3_aggregate_not_item_wrapper(tmp_path: Path) -> None:
    http, registry, store = _client(tmp_path)
    created = http.post(
        _path(PROJECT, WORKSPACE),
        json=_body(body="  keep spaces  ", acceptance="Must survive refresh"),
        headers={"Idempotency-Key": "save-1"},
    )
    payload = _assert_g3(created, 201)
    assert "item" not in payload["data"]
    item = payload["data"]
    _assert_aggregate(item, project_id=PROJECT, workspace_id=WORKSPACE, body="  keep spaces  ")
    listed = _assert_g3(http.get(_path(PROJECT, WORKSPACE)))
    assert listed["data"] == {"items": [item], "next_cursor": None}
    store.close()
    reopened = work_store.open_existing(tmp_path / "workspace-work.sqlite3")
    app = FastAPI()
    api.install(app, api.ApiService(lambda: registry, lambda: reopened))
    assert _assert_g3(TestClient(app).get(_path(PROJECT, WORKSPACE)))["data"] == {
        "items": [item], "next_cursor": None,
    }
    reopened.close()


def test_post_isolated_work_item_persists_allowed_paths(tmp_path: Path) -> None:
    http, _registry, store = _client(tmp_path)
    response = http.post(
        _path(PROJECT, WORKSPACE),
        json=_body(allowed_paths=["agent_cockpit/", "agent_cockpit/server.py"]),
        headers={"Idempotency-Key": "isolated"},
    )
    item = _assert_g3(response, 201)["data"]
    assert item["work_item"]["allowed_paths"] == ["agent_cockpit/"]
    work_id = item["work_item"]["work_item_id"]
    assert store.get_allowed_paths(
        project_id=PROJECT, workspace_id=WORKSPACE, work_item_id=work_id,
    ) == ("agent_cockpit/",)
    store.close()


def test_idempotency_replay_and_conflict(tmp_path: Path) -> None:
    http, _registry, store = _client(tmp_path)
    url = _path(PROJECT, WORKSPACE)
    first = _assert_g3(http.post(url, json=_body(), headers={"Idempotency-Key": "same"}), 201)["data"]
    assert _assert_g3(http.post(url, json=_body(), headers={"Idempotency-Key": "same"}), 201)["data"] == first
    _assert_error(
        http.post(url, json=_body(body="Different question"), headers={"Idempotency-Key": "same"}),
        409, "idempotency_conflict",
    )
    assert _assert_g3(http.get(url))["data"]["items"] == [first]
    store.close()


def test_old_url_and_body_shapes_are_rejected(tmp_path: Path) -> None:
    http, _registry, store = _client(tmp_path)
    url = _path(PROJECT, WORKSPACE)
    headers = {"Idempotency-Key": "bad"}
    assert http.post(
        f"/api/project-registry/projects/{PROJECT}/workspaces/{WORKSPACE}/work-items",
        json=_body(), headers=headers,
    ).status_code == 404
    bad = [
        http.post(url, json=_body()),
        http.post(url, json=_body(body="   "), headers=headers),
        http.post(url, json=_body(body="ok\x00no"), headers=headers),
        http.post(url, json=_body(extra="nope"), headers=headers),
        http.post(url, json={"message": {"body": "legacy"}, "acceptance": None, "constraints": None}, headers=headers),
        http.post(url, json=_body() | {"acceptance_criteria": "legacy"}, headers=headers),
        http.post(
            url, headers={**headers, "Content-Type": "application/json"},
            content=b'{"body":"ok","acceptance":null,"constraints":null,"constraints":null}',
        ),
    ]
    assert bad[0].json()["error"]["code"] == "idempotency_key_required"
    for response in bad[1:]:
        _assert_error(response, 400, "invalid_argument")
    assert store.list_work_items(project_id=PROJECT, workspace_id=WORKSPACE) == ()
    store.close()


def test_missing_and_archived_scopes_are_zero_write(tmp_path: Path) -> None:
    http, registry, store = _client(tmp_path)
    _assert_error(
        http.post(
            _path("prj_" + "0" * 32, WORKSPACE), json=_body(),
            headers={"Idempotency-Key": "missing-project"},
        ),
        404, "project_not_found",
    )
    registry.projects[OTHER_PROJECT] = _snapshot(OTHER_PROJECT)
    _assert_error(
        http.post(
            _path(OTHER_PROJECT, WORKSPACE), json=_body(),
            headers={"Idempotency-Key": "missing-workspace"},
        ),
        404, "workspace_not_found",
    )
    registry.add(PROJECT, OTHER_WORKSPACE, lifecycle="archived")
    archived_project = "prj_" + "e" * 32
    registry.projects[archived_project] = _snapshot(archived_project, lifecycle="archived")
    registry.workspaces[(archived_project, WORKSPACE)] = _workspace(
        archived_project, WORKSPACE,
    )
    _assert_error(
        http.post(
            _path(PROJECT, OTHER_WORKSPACE), json=_body(),
            headers={"Idempotency-Key": "archived"},
        ),
        409, "workspace_not_active",
    )
    _assert_error(
        http.post(
            _path(archived_project, WORKSPACE), json=_body(),
            headers={"Idempotency-Key": "archived-project"},
        ),
        409, "workspace_not_active",
    )
    listed = _assert_g3(http.get(_path(PROJECT, OTHER_WORKSPACE)))
    assert listed["data"] == {"items": [], "next_cursor": None}
    with sqlite3.connect(tmp_path / "workspace-work.sqlite3") as connection:
        for table in work_store.DOMAIN_TABLES:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    store.close()


def test_errors_are_g3_and_never_leak_secrets(tmp_path: Path, monkeypatch) -> None:
    http, _registry, store = _client(tmp_path)
    url = _path(PROJECT, WORKSPACE)

    def fail_write(*_args, **_kwargs):
        raise work_store.WorkspaceWorkError("store_write_failed")

    monkeypatch.setattr(store, "create_work_item", fail_write)
    leaked = _assert_error(
        http.post(url, json=_body(), headers={"Idempotency-Key": "write"}),
        503, "store_write_failed", retryable=True,
    )
    assert "/private/" not in http.post(
        url, json=_body(), headers={"Idempotency-Key": "write"},
    ).text
    assert leaked["details"] == {}

    def fail_read(*_args, **_kwargs):
        raise work_store.WorkspaceWorkError("store_read_failed")

    monkeypatch.setattr(store, "list_work_items", fail_read)
    _assert_error(http.get(url), 503, "store_read_failed", retryable=True)

    def explode(*_args, **_kwargs):
        raise RuntimeError("/secret/internal/path body=leak key=leak")

    monkeypatch.setattr(store, "list_work_items", explode)
    unknown = _assert_error(http.get(url), 500, "internal_error")
    assert "/secret/" not in http.get(url).text
    assert "body=leak" not in http.get(url).text
    assert unknown["details"] == {}
    store.close()


def test_injected_fault_after_each_insert_is_zero_write(
    tmp_path: Path, monkeypatch,
) -> None:
    http, _registry, store = _client(tmp_path)
    for name in (
        "_after_thread_insert",
        "_after_root_message_insert",
        "_after_work_item_insert",
        "_after_receipt_insert",
    ):
        monkeypatch.setattr(
            work_store, name,
            lambda _connection: (_ for _ in ()).throw(
                work_store.WorkspaceWorkError("store_write_failed")
            ),
        )
        _assert_error(
            http.post(
                _path(PROJECT, WORKSPACE), json=_body(),
                headers={"Idempotency-Key": name},
            ),
            503, "store_write_failed", retryable=True,
        )
        monkeypatch.undo()
        with sqlite3.connect(tmp_path / "workspace-work.sqlite3") as connection:
            for table in work_store.DOMAIN_TABLES:
                assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    store.close()


def _domain_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        return {
            table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in work_store.DOMAIN_TABLES
        }


def test_real_registry_active_post_and_archived_zero_write(tmp_path: Path) -> None:
    registry = registry_store.initialize(tmp_path / "project-registry.sqlite3")
    project = registry.create_project(slug="alpha", display_name="Alpha", goal=None)
    location = registry.add_repo_location(
        project_id=project.project_id,
        node_id="local",
        canonical_path="/repo/alpha",
        vcs_kind="none",
        availability="available",
    )
    workspace = registry.create_workspace(
        project_id=project.project_id,
        repo_location_id=location.repo_location_id,
        name="main",
        goal=None,
        isolation_kind="shared",
    )
    snapshot = registry.get_project_by_id(project.project_id)
    loaded = registry.get_workspace(project.project_id, workspace.workspace_id)
    assert isinstance(snapshot, registry_domain.ProjectSnapshot)
    assert not hasattr(snapshot, "lifecycle")
    assert snapshot.project.lifecycle == "active"
    assert isinstance(loaded, registry_domain.WorkspaceRecord)
    assert loaded.project_id == project.project_id
    assert loaded.workspace_id == workspace.workspace_id
    assert loaded.lifecycle == "active"

    work_path = tmp_path / "workspace-work.sqlite3"
    store = work_store.initialize(work_path)
    app = FastAPI()
    api.install(app, api.ApiService(lambda: registry, lambda: store))
    http = TestClient(app)
    url = _path(project.project_id, workspace.workspace_id)
    created = http.post(
        url, json=_body(body="真实 Registry 主链"),
        headers={"Idempotency-Key": "real-active"},
    )
    payload = _assert_g3(created, 201)
    _assert_aggregate(
        payload["data"],
        project_id=project.project_id,
        workspace_id=workspace.workspace_id,
        body="真实 Registry 主链",
    )
    with sqlite3.connect(work_path) as connection:
        assert connection.execute(
            "SELECT count(*), count(DISTINCT thread_id) FROM message_threads"
        ).fetchone() == (1, 1)
        assert connection.execute(
            "SELECT count(*), count(DISTINCT message_id) FROM messages"
        ).fetchone() == (1, 1)
        assert connection.execute(
            "SELECT count(*), count(DISTINCT work_item_id), "
            "count(DISTINCT source_message_id) FROM work_items"
        ).fetchone() == (1, 1, 1)
        assert connection.execute(
            "SELECT count(*) FROM idempotency_records"
        ).fetchone() == (1,)
    assert _domain_counts(work_path) == {
        "message_threads": 1, "messages": 1, "work_items": 1, "idempotency_records": 1,
    }

    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            "UPDATE workspaces SET lifecycle='archived' WHERE workspace_id=?",
            (workspace.workspace_id,),
        )
        connection.commit()
    _assert_error(
        http.post(url, json=_body(), headers={"Idempotency-Key": "archived-workspace"}),
        409, "workspace_not_active",
    )
    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            "UPDATE workspaces SET lifecycle='active' WHERE workspace_id=?",
            (workspace.workspace_id,),
        )
        connection.execute(
            "UPDATE projects SET lifecycle='archived' WHERE project_id=?",
            (project.project_id,),
        )
        connection.commit()
    archived = registry.get_project_by_id(project.project_id)
    assert archived is not None
    assert archived.project.lifecycle == "archived"
    _assert_error(
        http.post(url, json=_body(), headers={"Idempotency-Key": "archived-project"}),
        409, "workspace_not_active",
    )
    assert _domain_counts(work_path) == {
        "message_threads": 1, "messages": 1, "work_items": 1, "idempotency_records": 1,
    }
    store.close()
    registry.close()
