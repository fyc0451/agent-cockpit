from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import project_registry_api as api
from agent_cockpit import project_registry_store as store_module


WORKSPACE_KEYS = {
    "workspace_id", "project_id", "repo_location_id", "name", "goal",
    "isolation_kind", "lifecycle", "active_run_id", "version", "created_at",
    "updated_at", "repo_location",
}


class NoDiscovery:
    def __getattr__(self, name):
        raise AssertionError(f"workspace create must not use discovery: {name}")


@pytest.fixture()
def registry(tmp_path: Path):
    value = store_module.initialize(tmp_path / "project-registry.sqlite3")
    yield value
    value.close()


@pytest.fixture()
def client(registry):
    app = FastAPI()
    api.install(app, api.ApiService(lambda: registry, lambda: NoDiscovery()))
    return TestClient(app)


def _seed(registry, slug: str = "alpha", *, node_id: str = "local",
          availability: str = "available"):
    project = registry.create_project(
        slug=slug, display_name=slug.title(), goal=None,
    )
    location = registry.add_repo_location(
        project_id=project.project_id,
        node_id=node_id,
        canonical_path=f"/private/{slug}",
        vcs_kind="none",
        availability=availability,
    )
    return project, location


def _body(location, *, name: str = "Shared / main", goal: str | None = None):
    return {
        "repo_location_id": location.repo_location_id,
        "name": name,
        "goal": goal,
        "isolation_kind": "shared",
    }


def _post(client: TestClient, project_id: str, body: dict, key: str | None = "create"):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(
        f"/api/project-registry/projects/{project_id}/workspaces",
        json=body,
        headers=headers,
    )


def _error(response, status: int, code: str):
    assert response.status_code == status
    error = response.json()["error"]
    assert set(error) == {"code", "message", "retryable", "request_id", "details"}
    assert error["code"] == code
    assert "canonical_path" not in response.text


def _rows(registry) -> dict[str, int]:
    with sqlite3.connect(registry.path) as connection:
        tables = [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }


def test_shared_workspace_create_and_replay_return_exact_workspace_summary(
    client, registry,
) -> None:
    project, location = _seed(registry)
    body = _body(location, goal="Keep this record shared")

    first = _post(client, project.project_id, body)
    assert first.status_code == 201
    payload = first.json()
    assert set(payload) == {"data", "meta"}
    assert set(payload["data"]) == WORKSPACE_KEYS
    assert payload["data"]["project_id"] == project.project_id
    assert payload["data"]["repo_location_id"] == location.repo_location_id
    assert payload["data"]["name"] == "Shared / main"
    assert payload["data"]["goal"] == "Keep this record shared"
    assert payload["data"]["isolation_kind"] == "shared"
    assert payload["data"]["lifecycle"] == "active"
    assert payload["data"]["active_run_id"] is None
    assert payload["data"]["version"] == 1
    assert payload["data"]["repo_location"] == {
        "node_id": "local", "availability": "available",
    }
    assert "canonical_path" not in first.text

    replay = _post(client, project.project_id, body)
    assert replay.status_code == 201
    assert replay.content == first.content
    assert registry.list_workspaces(project.project_id)[0].workspace_id == payload["data"]["workspace_id"]


def test_store_create_is_atomic_idempotent_and_only_appends_expected_rows(registry) -> None:
    project, location = _seed(registry)
    body = _body(location)
    before = _rows(registry)

    first = registry.idempotent_create_workspace(
        scope=api.WORKSPACE_SCOPE,
        idempotency_key="store-create",
        payload={"project_id": project.project_id, **body},
        project_id=project.project_id,
        response_meta={"request_id": "req_store", "generated_at": "fixed"},
        **body,
    )
    replay = registry.idempotent_create_workspace(
        scope=api.WORKSPACE_SCOPE,
        idempotency_key="store-create",
        payload={"project_id": project.project_id, **body},
        project_id=project.project_id,
        response_meta={"request_id": "req_replay", "generated_at": "changed"},
        **body,
    )
    assert first.status_code == replay.status_code == 201
    assert first.response == replay.response
    assert first.response["meta"] == {
        "generated_at": "fixed", "request_id": "req_store",
    }

    after = _rows(registry)
    changed = {table: after[table] - before[table] for table in before}
    assert changed == {
        **{table: 0 for table in before},
        "workspaces": 1,
        "idempotency_records": 1,
    }


def test_workspace_create_requires_key_strict_body_and_shared_isolation(client, registry) -> None:
    project, location = _seed(registry)
    body = _body(location)
    _error(_post(client, project.project_id, body, None), 400, "idempotency_key_required")
    _error(_post(client, project.project_id, body | {"canonical_path": "/escape"}), 400, "invalid_argument")
    _error(_post(client, project.project_id, body | {"isolation_kind": "isolated_worktree"}), 400, "unsupported_isolation_kind")
    _error(_post(client, project.project_id, body | {"repo_location_id": "/private/alpha"}), 400, "invalid_argument")
    assert registry.list_workspaces(project.project_id) == ()


def test_workspace_create_requires_active_project_and_owned_active_repo(client, registry) -> None:
    project, location = _seed(registry)
    other, other_location = _seed(registry, "other")

    _error(_post(client, "prj_" + "0" * 32, _body(location), "missing-project"), 404, "project_not_found")
    _error(_post(client, project.project_id, _body(other_location), "wrong-owner"), 404, "repo_location_not_found")

    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            "UPDATE repo_locations SET lifecycle='archived' WHERE repo_location_id=?",
            (location.repo_location_id,),
        )
    _error(_post(client, project.project_id, _body(location), "archived-location"), 404, "repo_location_not_found")

    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            "UPDATE projects SET lifecycle='archived' WHERE project_id=?",
            (other.project_id,),
        )
    _error(_post(client, other.project_id, _body(other_location), "archived-project"), 404, "project_not_found")


@pytest.mark.parametrize(
    ("node_id", "availability", "code"),
    (
        ("remote", "available", "repo_location_not_local"),
        ("local", "offline", "repo_location_unavailable"),
        ("local", "missing", "repo_location_unavailable"),
        ("local", "unknown", "repo_location_unavailable"),
    ),
)
def test_workspace_create_requires_local_available_repo(
    client, registry, node_id: str, availability: str, code: str,
) -> None:
    project, location = _seed(
        registry, f"{node_id}-{availability}",
        node_id=node_id, availability=availability,
    )
    _error(_post(client, project.project_id, _body(location)), 409, code)
    assert registry.list_workspaces(project.project_id) == ()


def test_workspace_create_name_and_idempotency_conflicts_are_stable(client, registry) -> None:
    project, location = _seed(registry)
    first = _post(client, project.project_id, _body(location), "same-key")
    assert first.status_code == 201
    _error(
        _post(client, project.project_id, _body(location, name="Changed"), "same-key"),
        409, "idempotency_conflict",
    )
    _error(
        _post(client, project.project_id, _body(location), "different-key"),
        409, "workspace_name_conflict",
    )
    assert registry.preflight_idempotency(
        scope=api.WORKSPACE_SCOPE,
        idempotency_key="different-key",
        payload={"project_id": project.project_id, **_body(location)},
    ) is None


def test_workspace_name_is_display_only_and_not_path_validated(client, registry) -> None:
    project, location = _seed(registry)
    display_name = "release/../main\nlabel"
    created = _post(
        client, project.project_id, _body(location, name=display_name), "display-name",
    )
    assert created.status_code == 201
    assert created.json()["data"]["name"] == display_name
    assert created.json()["data"]["repo_location"] == {
        "node_id": "local", "availability": "available",
    }


def test_workspace_idempotency_key_is_global_without_cross_project_replay(client, registry) -> None:
    first_project, first_location = _seed(registry, "first")
    second_project, second_location = _seed(registry, "second")
    first = _post(client, first_project.project_id, _body(first_location), "global-key")
    assert first.status_code == 201
    first_workspace_id = first.json()["data"]["workspace_id"]

    conflict = _post(
        client, second_project.project_id, _body(second_location), "global-key",
    )
    _error(conflict, 409, "idempotency_conflict")
    assert first_workspace_id not in conflict.text
    assert registry.list_workspaces(second_project.project_id) == ()

    first_body = _body(first_location)
    expected = hashlib.sha256(json.dumps(
        {"project_id": first_project.project_id, **first_body},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    body_only = hashlib.sha256(json.dumps(
        first_body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    with sqlite3.connect(registry.path) as connection:
        digest = connection.execute(
            "SELECT request_digest FROM idempotency_records "
            "WHERE scope=? AND idempotency_key=?",
            (api.WORKSPACE_SCOPE, "global-key"),
        ).fetchone()[0]
    assert digest == expected
    assert digest != body_only


def test_workspace_replay_still_requires_current_project_and_repo_preconditions(
    client, registry,
) -> None:
    project, location = _seed(registry)
    body = _body(location)
    assert _post(client, project.project_id, body, "precondition-key").status_code == 201
    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            "UPDATE repo_locations SET lifecycle='archived' WHERE repo_location_id=?",
            (location.repo_location_id,),
        )
    _error(
        _post(client, project.project_id, body, "precondition-key"),
        404, "repo_location_not_found",
    )


def test_workspace_store_atomically_rechecks_preconditions_after_api_preflight(
    client, registry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, location = _seed(registry)
    body = _body(location)
    assert _post(client, project.project_id, body, "race-key").status_code == 201
    original = registry.preflight_idempotency

    def archive_after_preflight(**kwargs):
        replay = original(**kwargs)
        with sqlite3.connect(registry.path) as connection:
            connection.execute(
                "UPDATE repo_locations SET lifecycle='archived' "
                "WHERE repo_location_id=?",
                (location.repo_location_id,),
            )
        return replay

    monkeypatch.setattr(registry, "preflight_idempotency", archive_after_preflight)
    _error(
        _post(client, project.project_id, body, "race-key"),
        404, "repo_location_not_found",
    )
