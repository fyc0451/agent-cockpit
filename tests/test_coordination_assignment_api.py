import math

import pytest
from fastapi.testclient import TestClient

from agent_cockpit import coordination
import server


AUTH = {"authorization": "Bearer secret"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    monkeypatch.setattr(coordination, "DB_PATH", tmp_path / "coordination.sqlite3")
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)

    projects = {
        "demo": {"id": 1, "slug": "demo", "human_key": str(project)},
        "other": {"id": 2, "slug": "other", "human_key": str(other)},
    }
    monkeypatch.setattr(server.db, "project_by_slug", projects.get)
    return TestClient(server.app), project, other


def _create(client, slug="demo", **overrides):
    body = {
        "assignment": "实现 assignment HTTP API",
        "assignee": "codex-worker",
        "expected_reply": "提交 SHA 和测试结果",
        "deadline": 2_000.0,
    }
    body.update(overrides)
    return client.post(
        f"/api/coordination/projects/{slug}/assignments",
        headers=AUTH,
        json=body,
    )


def test_assignment_http_lifecycle(client):
    http, project, _ = client
    created_response = _create(http)
    assert created_response.status_code == 200, created_response.text
    created = created_response.json()
    assert created["project_key"] == str(project.resolve())
    assert (created["status"], created["version"]) == ("assigned", 1)

    listed = http.get(
        "/api/coordination/projects/demo/assignments",
        headers=AUTH,
        params=[("statuses", "assigned"), ("assignee", "codex-worker")],
    )
    assert listed.status_code == 200
    assert listed.json() == [created]

    item_url = f"/api/coordination/projects/demo/assignments/{created['assignment_id']}"
    assert http.get(item_url, headers=AUTH).json() == created

    started = http.patch(
        item_url, headers=AUTH,
        json={"status": "in_progress", "expected_version": 1},
    )
    assert started.status_code == 200
    assert (started.json()["status"], started.json()["version"]) == (
        "in_progress", 2,
    )

    closed = http.post(
        f"{item_url}/close", headers=AUTH, json={"expected_version": 2},
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert closed.json()["closed_at"] is not None


def test_project_slug_is_only_source_of_project_key(client):
    http, _, _ = client
    for forbidden in ({"project_key": "/tmp/escape"}, {"path": "/tmp/escape"}):
        response = _create(http, **forbidden)
        assert response.status_code == 400
    assert _create(http, slug="missing").status_code == 404


def test_cross_project_item_access_is_hidden(client):
    http, _, _ = client
    created = _create(http).json()
    other_url = (
        "/api/coordination/projects/other/assignments/"
        f"{created['assignment_id']}"
    )
    assert http.get(other_url, headers=AUTH).status_code == 404
    assert http.patch(
        other_url, headers=AUTH,
        json={"status": "in_progress", "expected_version": 1},
    ).status_code == 404
    assert http.post(
        f"{other_url}/close", headers=AUTH, json={"expected_version": 1},
    ).status_code == 404


@pytest.mark.parametrize("value", ["1", 1.0, True, False, 0, -1])
def test_expected_version_is_strict_positive_integer(client, value):
    http, _, _ = client
    created = _create(http).json()
    response = http.patch(
        "/api/coordination/projects/demo/assignments/"
        f"{created['assignment_id']}",
        headers=AUTH,
        json={"status": "in_progress", "expected_version": value},
    )
    assert response.status_code == 400


@pytest.mark.parametrize("value", [True, False, "2000", math.inf, -math.inf, math.nan])
def test_deadline_rejects_bool_strings_and_non_finite(client, value):
    http, _, _ = client
    if isinstance(value, float) and not math.isfinite(value):
        encoded = "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
        response = http.post(
            "/api/coordination/projects/demo/assignments",
            headers={**AUTH, "content-type": "application/json"},
            content=(
                '{"assignment":"task","assignee":"worker","deadline":'
                f"{encoded}}}"
            ),
        )
    else:
        response = _create(http, deadline=value)
    assert response.status_code == 400


def test_list_validates_statuses_and_assignee(client):
    http, _, _ = client
    assert http.get(
        "/api/coordination/projects/demo/assignments?statuses=unknown",
        headers=AUTH,
    ).status_code == 400
    for assignee in ("", " " * 2, "x" * 129):
        assert http.get(
            "/api/coordination/projects/demo/assignments",
            headers=AUTH,
            params={"assignee": assignee},
        ).status_code == 400


def test_invalid_transition_is_400_but_stale_and_closed_are_409(client):
    http, _, _ = client
    created = _create(http).json()
    item_url = f"/api/coordination/projects/demo/assignments/{created['assignment_id']}"

    illegal = http.patch(
        item_url, headers=AUTH,
        json={"status": "review", "expected_version": 1},
    )
    assert illegal.status_code == 400
    direct_close = http.patch(
        item_url, headers=AUTH,
        json={"status": "closed", "expected_version": 1},
    )
    assert direct_close.status_code == 400

    stale = http.patch(
        item_url, headers=AUTH,
        json={"status": "in_progress", "expected_version": 99},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_version"] == 1
    assert stale.json()["detail"]["current_status"] == "assigned"

    assert http.post(
        f"{item_url}/close", headers=AUTH, json={"expected_version": 1},
    ).status_code == 200
    closed = http.patch(
        item_url, headers=AUTH,
        json={"status": "in_progress", "expected_version": 2},
    )
    assert closed.status_code == 409
    assert closed.json()["detail"]["current_status"] == "closed"
    repeated_close = http.post(
        f"{item_url}/close", headers=AUTH, json={"expected_version": 2},
    )
    assert repeated_close.status_code == 409
    assert repeated_close.json()["detail"]["current_version"] == 2


def test_missing_item_is_404_and_api_requires_auth(client):
    http, _, _ = client
    item_url = "/api/coordination/projects/demo/assignments/a-missing"
    assert http.get(item_url, headers=AUTH).status_code == 404
    assert http.patch(
        item_url, headers=AUTH,
        json={"status": "in_progress", "expected_version": 1},
    ).status_code == 404
    assert http.get(
        "/api/coordination/projects/demo/assignments"
    ).status_code == 401
