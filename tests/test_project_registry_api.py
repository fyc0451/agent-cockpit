from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import project_discovery as discovery
from agent_cockpit import project_registry_api as api
from agent_cockpit import project_registry_store as store_module
from agent_cockpit import server


def _hash(seed: str) -> str:
    return "sha256:" + seed.encode("ascii").hex().ljust(64, "0")[:64]


@dataclass
class FakeDiscovery:
    result: discovery.DiscoveryResult
    calls: int = 0

    def list_roots(self):
        return (discovery.RootDescriptor("local", "root_" + "a" * 24, "projects"),)

    def list_directories(self, locator, query=None):
        return discovery.DirectoryListing(locator, ())

    def discover(self, locator):
        self.calls += 1
        return discovery.DiscoveryResult(
            locator=locator,
            display_path=self.result.display_path,
            canonical_path_digest=self.result.canonical_path_digest,
            vcs=self.result.vcs,
            exact_match=self.result.exact_match,
            possible_projects=self.result.possible_projects,
            discovery_fingerprint=self.result.discovery_fingerprint,
            observed_at=self.result.observed_at,
            complete=self.result.complete,
            sources=self.result.sources,
            warnings=self.result.warnings,
            _canonical_path="/private/" + locator.path,
        )


def _result(*, complete: bool = True, exact=None, kind: str = "git"):
    locator = discovery.ProjectLocator("local", "root_" + "a" * 24, "alpha")
    return discovery.DiscoveryResult(
        locator=locator,
        display_path="projects/alpha",
        canonical_path_digest=_hash("path"),
        vcs=discovery.VcsObservation(
            kind=kind,
            remote_fingerprint=_hash("remote") if kind == "git" else None,
            repository_fingerprint=_hash("repository") if kind == "git" else None,
        ),
        exact_match=exact,
        possible_projects=(),
        discovery_fingerprint=_hash("discovery"),
        observed_at="2026-08-13T12:00:00Z",
        complete=complete,
        sources=("local_files", "local_git", "project_registry") if complete else ("local_files", "local_git"),
        warnings=() if complete else ("project_registry_unavailable",),
        _canonical_path="/private/alpha",
    )


@pytest.fixture()
def registry(tmp_path: Path):
    value = store_module.initialize(tmp_path / "project-registry.sqlite3")
    yield value
    value.close()


@pytest.fixture()
def client(registry):
    discovery_service = FakeDiscovery(_result())
    app = FastAPI()
    api.install(app, api.ApiService(lambda: registry, lambda: discovery_service))
    return TestClient(app), discovery_service


def _body():
    return {
        "display_name": "Alpha",
        "slug": "alpha",
        "goal": None,
        "locator": {"node_id": "local", "root_id": "root_" + "a" * 24, "path": "alpha"},
        "expected_discovery_fingerprint": _hash("discovery"),
    }


def _assert_g3(response):
    payload = response.json()
    assert set(payload) == {"data", "meta"}
    assert set(payload["meta"]) >= {"request_id", "generated_at", "partial", "sources", "capabilities"}
    assert payload["meta"]["request_id"].startswith("req_")
    return payload


def _assert_error(response, status: int, code: str):
    assert response.status_code == status
    error = response.json()["error"]
    assert set(error) == {"code", "message", "retryable", "request_id", "details"}
    assert error["code"] == code


def test_server_wires_new_routes_without_changing_legacy_route():
    paths = {route.path for route in server.app.routes}
    assert {
        "/api/runtime-nodes",
        "/api/runtime-nodes/{node_id}/roots",
        "/api/runtime-nodes/{node_id}/directories",
        "/api/project-discovery",
        "/api/project-registry/projects",
        "/api/project-registry/projects/{project_id}",
        "/api/project-registry/projects/{project_id}/repo-locations",
        "/api/projects/{slug}",
    } <= paths


def test_reads_are_g3_redacted_and_cursor_is_bound(client, registry):
    registry.idempotent_register_project(
        scope="seed", idempotency_key="seed", payload={"seed": 1}, slug="alpha",
        display_name="Alpha", goal=None, node_id="local", canonical_path="/private/alpha",
        vcs_kind="none", availability="available", git_remote_fingerprint=None,
    )
    registry.idempotent_register_project(
        scope="seed", idempotency_key="seed2", payload={"seed": 2}, slug="beta",
        display_name="Beta", goal=None, node_id="local", canonical_path="/private/beta",
        vcs_kind="none", availability="available", git_remote_fingerprint=None,
    )
    http, _service = client
    first = http.get("/api/project-registry/projects?limit=1")
    payload = _assert_g3(first)
    assert len(payload["data"]["items"]) == 1
    assert "/private" not in first.text
    cursor = payload["data"]["next_cursor"]
    assert cursor and not cursor.startswith("prj_")
    second = http.get(f"/api/project-registry/projects?limit=1&cursor={cursor}")
    assert len(_assert_g3(second)["data"]["items"]) == 1
    _assert_error(http.get(f"/api/project-registry/projects?lifecycle=archived&cursor={cursor}"), 400, "invalid_argument")
    _assert_error(http.get("/api/project-registry/projects?limit=101"), 400, "invalid_argument")
    _assert_error(http.get("/api/project-registry/projects?lifecycle=unknown"), 400, "invalid_argument")
    assert payload["meta"]["capabilities"]["projectRegistry.read"]["available"] is True
    assert payload["meta"]["capabilities"]["projectRegistry.write"]["available"] is False


def test_create_preflights_before_discovery_and_replays_receipt(client):
    http, service = client
    first = http.post("/api/project-registry/projects", json=_body(), headers={"Idempotency-Key": "same"})
    first_data = _assert_g3(first)["data"]
    assert first.status_code == 201
    assert first_data["project_id"]
    assert _assert_g3(first)["meta"]["capabilities"]["projectRegistry.write"]["available"] is True
    assert service.calls == 1
    replay = http.post("/api/project-registry/projects", json=_body(), headers={"Idempotency-Key": "same"})
    assert replay.status_code == 201
    assert _assert_g3(replay)["data"] == first_data
    assert service.calls == 1
    changed = _body() | {"display_name": "Changed"}
    _assert_error(http.post("/api/project-registry/projects", json=changed, headers={"Idempotency-Key": "same"}), 409, "idempotency_conflict")
    _assert_error(http.post("/api/project-registry/projects", json=_body()), 400, "idempotency_key_required")


def test_invalid_write_input_does_not_run_preflight_or_discovery(client, registry):
    http, service = client
    invalid = _body() | {"slug": "Not valid"}
    _assert_error(http.post("/api/project-registry/projects", json=invalid, headers={"Idempotency-Key": "bad"}), 400, "invalid_argument")
    assert service.calls == 0
    assert registry.preflight_idempotency(scope="project-registry.projects.create.v1", idempotency_key="bad", payload=invalid) is None


def test_create_stale_and_partial_fail_closed_without_receipt(client, registry):
    http, service = client
    service.result = _result()
    stale = _body() | {"expected_discovery_fingerprint": _hash("other")}
    _assert_error(http.post("/api/project-registry/projects", json=stale, headers={"Idempotency-Key": "stale"}), 409, "discovery_stale")
    assert registry.preflight_idempotency(scope="project-registry.projects.create.v1", idempotency_key="stale", payload=stale) is None
    service.result = _result(complete=False)
    _assert_error(http.post("/api/project-registry/projects", json=_body(), headers={"Idempotency-Key": "partial"}), 503, "discovery_unavailable")
    assert registry.preflight_idempotency(scope="project-registry.projects.create.v1", idempotency_key="partial", payload=_body()) is None


def test_partial_discovery_is_explicit_g3_source_state(client):
    http, service = client
    service.result = _result(complete=False)
    response = http.post("/api/project-discovery", json={"locator": _body()["locator"]})
    payload = _assert_g3(response)
    assert payload["data"]["complete"] is False
    assert payload["meta"]["partial"] is True
    assert payload["meta"]["capabilities"]["projectRegistry.write"]["available"] is False
    assert {item["status"] for item in payload["meta"]["sources"]} == {"available", "unavailable"}


def test_attach_requires_discovery_then_returns_incremented_project(client, registry):
    http, _service = client
    created = http.post("/api/project-registry/projects", json=_body(), headers={"Idempotency-Key": "create"})
    project_id = _assert_g3(created)["data"]["project_id"]
    body = {
        "locator": {"node_id": "local", "root_id": "root_" + "a" * 24, "path": "clone"},
        "expected_discovery_fingerprint": _hash("discovery"),
        "expected_project_version": 1,
    }
    attached = http.post(f"/api/project-registry/projects/{project_id}/repo-locations", json=body, headers={"Idempotency-Key": "attach"})
    assert attached.status_code == 201
    assert _assert_g3(attached)["data"]["project"]["version"] == 2


def test_detail_locations_and_discovery_routes_are_g3(client):
    http, _service = client
    created = http.post("/api/project-registry/projects", json=_body(), headers={"Idempotency-Key": "create"})
    project_id = _assert_g3(created)["data"]["project_id"]
    detail = _assert_g3(http.get(f"/api/project-registry/projects/{project_id}"))
    assert detail["data"]["project"]["project_id"] == project_id
    assert "/private" not in str(detail)
    locations = _assert_g3(http.get(f"/api/project-registry/projects/{project_id}/repo-locations"))
    assert len(locations["data"]["items"]) == 1
    _assert_error(http.get("/api/project-registry/projects/prj_" + "0" * 32), 404, "project_not_found")
    root = _assert_g3(http.get("/api/runtime-nodes"))
    assert root["data"]["nodes"][0]["node_id"] == "local"
    public = _assert_g3(http.post("/api/project-discovery", json={"locator": _body()["locator"]}))
    assert "/private" not in str(public)
