from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import files
from agent_cockpit import local_readonly_api
from agent_cockpit import project_registry_store
from agent_cockpit import server


@pytest.fixture()
def local_slice(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("hello local slice\n", encoding="utf-8")
    (root / "docs" / "guide.txt").write_text("guide\n", encoding="utf-8")

    registry = project_registry_store.initialize(tmp_path / "registry.sqlite3")
    project = registry.create_project(slug="alpha", display_name="Alpha", goal=None)
    location = registry.add_repo_location(
        project_id=project.project_id,
        node_id="local",
        canonical_path=str(root),
        vcs_kind="none",
        availability="available",
    )
    workspace = registry.create_workspace(
        project_id=project.project_id,
        repo_location_id=location.repo_location_id,
        name="main",
        goal="Read local files",
        isolation_kind="shared",
    )
    other = registry.create_project(slug="beta", display_name="Beta", goal=None)
    app = FastAPI()
    app.state.registry = registry
    local_readonly_api.install(app, local_readonly_api.ApiService(lambda: registry))
    yield TestClient(app), project, workspace, other, root
    registry.close()


def _g3(response, *, files_available: bool = True, files_reason: str | None = None):
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"data", "meta"}
    assert payload["meta"]["request_id"].startswith("req_")
    assert payload["meta"]["capabilities"]["files.read"] == {
        "available": files_available,
        "reason": files_reason,
    }
    assert payload["meta"]["capabilities"]["terminal.pty"] == {
        "available": False,
        "reason": "workspace_terminal_ticket_deferred",
    }
    return payload


def _error(response, status: int, code: str):
    assert response.status_code == status
    error = response.json()["error"]
    assert set(error) == {"code", "message", "retryable", "request_id", "details"}
    assert error["code"] == code


def test_persisted_workspace_list_and_detail_are_g3_without_internal_root(local_slice):
    client, project, workspace, _other, root = local_slice

    listed = _g3(
        client.get(f"/api/project-registry/projects/{project.project_id}/workspaces"),
        files_available=False,
        files_reason="workspace_selection_required",
    )
    assert [item["workspace_id"] for item in listed["data"]["items"]] == [
        workspace.workspace_id
    ]
    assert [source["name"] for source in listed["meta"]["sources"]] == [
        "project_registry"
    ]

    detail = _g3(client.get(
        f"/api/project-registry/projects/{project.project_id}/workspaces/"
        f"{workspace.workspace_id}"
    ))
    assert detail["data"]["workspace_id"] == workspace.workspace_id
    assert detail["data"]["repo_location"] == {
        "node_id": "local",
        "availability": "available",
    }
    rendered = str(detail)
    assert str(root) not in rendered
    assert "canonical_path" not in rendered
    assert "cwd" not in rendered
    assert "human_key" not in rendered


@pytest.mark.parametrize(
    ("kind", "reason"),
    (
        ("remote", "repo_location_not_local"),
        ("offline", "repo_location_unavailable"),
        ("archived-location", "repo_location_not_active"),
        ("archived-workspace", "workspace_not_active"),
    ),
)
def test_workspace_detail_files_capability_fails_closed(
    tmp_path: Path, kind: str, reason: str,
):
    root = tmp_path / kind
    root.mkdir()
    registry = project_registry_store.initialize(tmp_path / f"{kind}.sqlite3")
    project = registry.create_project(slug=kind, display_name=kind.title(), goal=None)
    location = registry.add_repo_location(
        project_id=project.project_id,
        node_id="remote-1" if kind == "remote" else "local",
        canonical_path=str(root),
        vcs_kind="none",
        availability="offline" if kind == "offline" else "available",
    )
    workspace = registry.create_workspace(
        project_id=project.project_id,
        repo_location_id=location.repo_location_id,
        name="main",
        goal=None,
        isolation_kind="shared",
    )
    if kind.startswith("archived"):
        with sqlite3.connect(registry.path) as connection:
            if kind == "archived-location":
                connection.execute(
                    "UPDATE repo_locations SET lifecycle='archived' "
                    "WHERE repo_location_id=?",
                    (location.repo_location_id,),
                )
            else:
                connection.execute(
                    "UPDATE workspaces SET lifecycle='archived' WHERE workspace_id=?",
                    (workspace.workspace_id,),
                )
    app = FastAPI()
    local_readonly_api.install(app, local_readonly_api.ApiService(lambda: registry))

    payload = _g3(
        TestClient(app).get(
            f"/api/project-registry/projects/{project.project_id}/workspaces/"
            f"{workspace.workspace_id}"
        ),
        files_available=False,
        files_reason=reason,
    )
    assert payload["meta"]["capabilities"]["terminal.pty"]["available"] is False
    registry.close()


def test_workspace_detail_cross_project_and_unknown_are_same_404(local_slice):
    client, project, workspace, other, _root = local_slice
    base = "/api/project-registry/projects"

    _error(client.get(
        f"{base}/{other.project_id}/workspaces/{workspace.workspace_id}"
    ), 404, "project_or_workspace_not_found")
    _error(client.get(
        f"{base}/{project.project_id}/workspaces/ws_{'0' * 32}"
    ), 404, "project_or_workspace_not_found")
    _error(client.get(
        f"{base}/prj_{'0' * 32}/workspaces/{workspace.workspace_id}"
    ), 404, "project_or_workspace_not_found")
    _error(client.get(
        f"{base}/prj_{'0' * 32}/workspaces"
    ), 404, "project_not_found")


def test_next_terminal_capability_uses_controller_authority(local_slice):
    client, project, workspace, _other, _root = local_slice
    app = FastAPI()
    local_readonly_api.install(app, local_readonly_api.ApiService(
        lambda: client.app.state.registry,
        lambda _workspace, _location: (True, None),
    ))
    payload = TestClient(app).get(
        f"/api/project-registry/projects/{project.project_id}/workspaces/"
        f"{workspace.workspace_id}"
    ).json()
    assert payload["meta"]["capabilities"]["terminal.pty"] == {
        "available": True,
        "reason": None,
    }


def test_relative_tree_content_and_search_are_read_only_public_projections(local_slice):
    client, project, workspace, _other, root = local_slice
    base = (
        f"/api/project-registry/projects/{project.project_id}/workspaces/"
        f"{workspace.workspace_id}/files"
    )

    tree = _g3(client.get(base, params={"path": ""}))
    assert tree["data"]["path"] == ""
    assert {item["name"] for item in tree["data"]["entries"]} == {
        "docs", "README.md",
    }
    assert [source["name"] for source in tree["meta"]["sources"]] == [
        "project_registry", "local_files",
    ]

    content = _g3(client.get(base + "/content", params={"path": "README.md"}))
    assert content["data"] == {
        "path": "README.md",
        "text": "hello local slice\n",
        "size": len("hello local slice\n"),
        "binary": False,
    }

    search = _g3(client.get(
        base + "/search", params={"path": "", "q": "guide", "limit": "10"}
    ))
    assert search["data"]["results"] == [{
        "name": "guide.txt",
        "path": "docs/guide.txt",
        "type": "file",
        "size": len("guide\n"),
        "ext": "txt",
    }]
    rendered = str({"tree": tree, "content": content, "search": search})
    assert str(root) not in rendered
    assert "modifiable" not in rendered


@pytest.mark.parametrize(
    "malformed",
    (
        [],
        {"entries": {}},
        {"entries": ["not-an-item"]},
        {"entries": [{"name": "a", "type": "file", "size": 1}]},
        {"entries": [{"name": 1, "type": "file", "size": 1, "ext": "txt"}]},
        {"entries": [{"name": "a", "type": 1, "size": 1, "ext": "txt"}]},
        {"entries": [{"name": "a", "type": "file", "size": True, "ext": "txt"}]},
        {"entries": [{"name": "a", "type": "file", "size": 1, "ext": 1}]},
    ),
)
def test_tree_malformed_source_shape_is_fixed_g3_error(
    local_slice, monkeypatch: pytest.MonkeyPatch, malformed: object,
):
    client, project, workspace, _other, _root = local_slice
    monkeypatch.setattr(
        local_readonly_api.files,
        "list_dir_from_trusted_root",
        lambda *_args: malformed,
    )
    response = client.get(
        f"/api/project-registry/projects/{project.project_id}/workspaces/"
        f"{workspace.workspace_id}/files",
        params={"path": ""},
    )
    _error(response, 503, "local_files_unavailable")


@pytest.mark.parametrize(
    "malformed",
    (
        [],
        {"binary": False, "text": "value"},
        {"size": -1, "binary": False, "text": "value"},
        {"size": True, "binary": False, "text": "value"},
        {"size": 1, "binary": "false", "text": "value"},
        {"size": 1, "binary": False},
        {"size": 1, "binary": False, "text": 1},
    ),
)
def test_content_malformed_source_shape_is_fixed_g3_error(
    local_slice, monkeypatch: pytest.MonkeyPatch, malformed: object,
):
    client, project, workspace, _other, _root = local_slice
    monkeypatch.setattr(
        local_readonly_api.files,
        "read_file_from_trusted_root",
        lambda *_args: malformed,
    )
    response = client.get(
        f"/api/project-registry/projects/{project.project_id}/workspaces/"
        f"{workspace.workspace_id}/files/content",
        params={"path": "README.md"},
    )
    _error(response, 503, "local_files_unavailable")


def test_binary_content_never_forwards_source_text(
    local_slice, monkeypatch: pytest.MonkeyPatch,
):
    client, project, workspace, _other, _root = local_slice
    monkeypatch.setattr(
        local_readonly_api.files,
        "read_file_from_trusted_root",
        lambda *_args: {"size": 2, "binary": True, "text": "not public"},
    )
    payload = _g3(client.get(
        f"/api/project-registry/projects/{project.project_id}/workspaces/"
        f"{workspace.workspace_id}/files/content",
        params={"path": "image.bin"},
    ))
    assert payload["data"] == {"path": "image.bin", "size": 2, "binary": True}


@pytest.mark.parametrize(
    ("field", "malformed"),
    (
        ("result", []),
        ("path", 1),
        ("query", 1),
        ("results", {}),
        ("truncated", 0),
        ("item", "not-an-item"),
        ("item-path", 1),
        ("item-name", 1),
        ("item-type", 1),
        ("item-size", True),
        ("item-ext", 1),
        ("item-missing", None),
    ),
)
def test_search_malformed_source_shape_is_fixed_g3_error(
    local_slice, monkeypatch: pytest.MonkeyPatch, field: str, malformed: object,
):
    client, project, workspace, _other, root = local_slice
    item = {
        "path": str(root / "README.md"),
        "name": "README.md",
        "type": "file",
        "size": 1,
        "ext": "md",
    }
    result: object = {
        "path": str(root),
        "query": "readme",
        "results": [item],
        "truncated": False,
    }
    if field == "result":
        result = malformed
    elif field == "item":
        result["results"] = [malformed]
    elif field == "item-missing":
        item.pop("ext")
    elif field.startswith("item-"):
        item[field.removeprefix("item-")] = malformed
    else:
        result[field] = malformed
    monkeypatch.setattr(
        local_readonly_api.files,
        "search_files_from_trusted_root",
        lambda *_args: result,
    )
    response = client.get(
        f"/api/project-registry/projects/{project.project_id}/workspaces/"
        f"{workspace.workspace_id}/files/search",
        params={"path": "", "q": "readme", "limit": "10"},
    )
    _error(response, 503, "local_files_unavailable")


def test_files_cross_project_mismatch_and_benign_invalid_path_are_g3(local_slice):
    client, project, workspace, other, _root = local_slice
    mismatch = (
        f"/api/project-registry/projects/{other.project_id}/workspaces/"
        f"{workspace.workspace_id}/files"
    )
    _error(client.get(mismatch, params={"path": ""}), 404,
           "project_or_workspace_not_found")

    valid = (
        f"/api/project-registry/projects/{project.project_id}/workspaces/"
        f"{workspace.workspace_id}/files"
    )
    _error(client.get(valid, params={"path": "docs/../README.md"}), 400,
           "invalid_relative_path")
    _error(client.get(valid + "/search", params={
        "path": "", "q": "guide", "limit": "101",
    }), 400, "invalid_argument")


def test_server_installs_only_get_routes_and_keeps_legacy_workbench_route():
    methods_by_path = {
        route.path: set(route.methods or ())
        for route in server.app.routes
        if hasattr(route, "methods")
    }
    expected = {
        "/api/project-registry/projects/{project_id}/workspaces",
        "/api/project-registry/projects/{project_id}/workspaces/{workspace_id}",
        "/api/project-registry/projects/{project_id}/workspaces/{workspace_id}/files",
        "/api/project-registry/projects/{project_id}/workspaces/{workspace_id}/files/content",
        "/api/project-registry/projects/{project_id}/workspaces/{workspace_id}/files/search",
    }
    assert expected <= methods_by_path.keys()
    assert all(methods_by_path[path] == {"GET"} for path in expected)
    assert methods_by_path["/api/projects/{slug}/workbench"] == {"GET"}


def test_server_scoped_auth_boundary_returns_g3(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "local-token")
    response = TestClient(server.app).get(
        "/api/project-registry/projects/prj_00000000000000000000000000000000/"
        "workspaces"
    )
    _error(response, 401, "unauthenticated")


def test_server_local_get_opens_existing_store_without_initialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    missing = tmp_path / "missing.sqlite3"
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "local-token")
    monkeypatch.setattr(server, "_project_registry_store", None)
    monkeypatch.setattr(server.runtime_paths, "store", lambda _name: missing)
    monkeypatch.setattr(
        server.project_registry_store,
        "initialize",
        lambda _path: pytest.fail("local readonly GET must not initialize Store"),
    )

    response = TestClient(server.app).get(
        "/api/project-registry/projects/prj_00000000000000000000000000000000/"
        "workspaces",
        headers={"authorization": "Bearer local-token"},
    )

    _error(response, 503, "schema_missing")
    assert not missing.exists()


def test_trusted_root_file_operations_stay_on_descriptor_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    (root / "docs" / "guide.txt").write_text("guide\n", encoding="utf-8")
    monkeypatch.setattr(
        files, "_list_dir_path", lambda *_: pytest.fail("legacy path list")
    )
    monkeypatch.setattr(
        files, "_read_file_path", lambda *_: pytest.fail("legacy path read")
    )
    monkeypatch.setattr(
        files, "_search_files_path", lambda *_: pytest.fail("legacy path search")
    )

    listed = files.list_dir_from_trusted_root(root, "")
    content = files.read_file_from_trusted_root(root, "README.md")
    searched = files.search_files_from_trusted_root(root, "", "guide", 10)

    assert [(item["name"], item["type"]) for item in listed["entries"]] == [
        ("docs", "dir"),
        ("README.md", "file"),
    ]
    assert content["text"] == "hello\n"
    assert [(item["relative"], item["type"]) for item in searched["results"]] == [
        ("docs/guide.txt", "file"),
    ]
    by_dir = files.search_files_from_trusted_root(root, "", "docs/guide", 10)
    assert [(item["relative"], item["type"]) for item in by_dir["results"]] == [
        ("docs/guide.txt", "file"),
    ]
    under_docs = files.search_files_from_trusted_root(root, "", "docs/", 10)
    assert [(item["relative"], item["type"]) for item in under_docs["results"]] == [
        ("docs", "dir"),
        ("docs/guide.txt", "file"),
    ]


def test_trusted_root_descriptor_errors_use_stable_codes(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(files.TrustedRootError) as missing_entry:
        files.read_file_from_trusted_root(root, "missing.txt")
    assert missing_entry.value.code == "file_not_found"

    with pytest.raises(files.TrustedRootError) as missing_root:
        files.list_dir_from_trusted_root(tmp_path / "missing-root", "")
    assert missing_root.value.code == "local_files_unavailable"

    (root / "plain.txt").write_text("plain\n", encoding="utf-8")
    with pytest.raises(files.TrustedRootError) as intermediate_file:
        files.read_file_from_trusted_root(root, "plain.txt/child")
    assert intermediate_file.value.code == "file_not_found"


def test_trusted_root_search_preserves_order_and_limit_semantics(tmp_path: Path):
    root = tmp_path / "root"
    (root / "A-match-dir").mkdir(parents=True)
    (root / "b-match-dir").mkdir()
    (root / ".github").mkdir()
    (root / "A-match.txt").write_text("a\n", encoding="utf-8")
    (root / "b-match.txt").write_text("b\n", encoding="utf-8")
    (root / ".gitignore").write_text("cache\n", encoding="utf-8")

    exact = files.search_files_from_trusted_root(root, "", "match", 4)
    overflow = files.search_files_from_trusted_root(root, "", "match", 3)

    assert [(item["name"], item["type"]) for item in exact["results"]] == [
        ("A-match-dir", "dir"),
        ("b-match-dir", "dir"),
        ("A-match.txt", "file"),
        ("b-match.txt", "file"),
    ]
    assert exact["truncated"] is False
    assert len(overflow["results"]) == 3
    assert overflow["truncated"] is True
    dot_git = files.search_files_from_trusted_root(root, "", ".git", 10)
    assert [(item["name"], item["type"]) for item in dot_git["results"]] == [
        (".github", "dir"),
        (".gitignore", "file"),
    ]


def test_trusted_root_static_metadata_does_not_open_known_files_or_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "root"
    root.mkdir()
    known = root / "known.txt"
    known.write_text("known\n", encoding="utf-8")
    known.chmod(0)
    fifo = root / "pipe"
    os.mkfifo(fifo)
    real_open = files.os.open

    def checked_open(path, flags, *args, **kwargs):
        if path == "known.txt":
            pytest.fail("known text metadata must not open the file")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", checked_open)
    monkeypatch.setattr(
        files.os, "supports_dir_fd", files.os.supports_dir_fd | {checked_open}
    )
    try:
        listed = files.list_dir_from_trusted_root(root, "")
        searched = files.search_files_from_trusted_root(root, "", "known", 10)
    finally:
        known.chmod(0o600)

    assert [(item["name"], item["modifiable"]) for item in listed["entries"]] == [
        ("known.txt", True),
    ]
    assert [item["name"] for item in searched["results"]] == ["known.txt"]


def test_trusted_root_search_uses_iterative_descriptor_walk(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    current = root
    for index in range(180):
        current /= f"d{index:03d}"
        current.mkdir()
    (current / "needle.txt").write_text("needle\n", encoding="utf-8")

    result = files.search_files_from_trusted_root(root, "", "needle", 10)

    assert len(result["results"]) == 1
    assert result["results"][0]["relative"].endswith("/needle.txt")


def test_trusted_root_requires_descriptor_platform_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.delattr(files.os, "O_NOFOLLOW")

    with pytest.raises(files.TrustedRootError) as unavailable:
        files.list_dir_from_trusted_root(root, "")

    assert unavailable.value.code == "local_files_unavailable"


def test_trusted_root_dup_failure_uses_stable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        files.os, "dup", lambda _fd: (_ for _ in ()).throw(OSError("dup failed"))
    )

    with pytest.raises(files.TrustedRootError) as unavailable:
        files.list_dir_from_trusted_root(root, "")

    assert unavailable.value.code == "local_files_unavailable"


def test_trusted_root_close_failure_cleans_open_child_once(
    monkeypatch: pytest.MonkeyPatch,
):
    opened = iter((10, 11))
    close_calls: list[int] = []

    def fake_open(*_args, **_kwargs):
        return next(opened)

    def fake_close(fd: int):
        close_calls.append(fd)
        if fd == 10:
            raise OSError("parent close failed")

    monkeypatch.setattr(files.os, "open", fake_open)
    monkeypatch.setattr(files.os, "close", fake_close)
    monkeypatch.setattr(
        files.os, "supports_dir_fd", files.os.supports_dir_fd | {fake_open}
    )

    with pytest.raises(files.TrustedRootError) as unavailable:
        files.list_dir_from_trusted_root(Path("/root"), "")

    assert unavailable.value.code == "local_files_unavailable"
    assert close_calls == [10, 11]


def test_trusted_relative_close_failure_cleans_child_and_root_once(
    monkeypatch: pytest.MonkeyPatch,
):
    opened = iter((20, 22))
    close_calls: list[int] = []

    def fake_open(*_args, **_kwargs):
        return next(opened)

    def fake_close(fd: int):
        close_calls.append(fd)
        if fd == 21:
            raise OSError("parent close failed")

    monkeypatch.setattr(files.os, "open", fake_open)
    monkeypatch.setattr(files.os, "dup", lambda _fd: 21)
    monkeypatch.setattr(files.os, "close", fake_close)
    monkeypatch.setattr(
        files.os, "supports_dir_fd", files.os.supports_dir_fd | {fake_open}
    )

    with pytest.raises(files.TrustedRootError) as unavailable:
        files.read_file_from_trusted_root(Path("/"), "file.txt")

    assert unavailable.value.code == "local_files_unavailable"
    assert close_calls == [21, 22, 20]
