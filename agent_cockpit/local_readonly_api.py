"""G3 boundary for persisted Workspace and Registry-authorized local Files."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import files
from . import project_registry_api
from . import project_registry_domain as domain
from . import project_registry_store


_STATUS = {
    "invalid_argument": 400,
    "invalid_relative_path": 400,
    "project_not_found": 404,
    "project_or_workspace_not_found": 404,
    "file_not_found": 404,
    "workspace_files_unavailable": 412,
    "local_files_unavailable": 503,
    "store_read_failed": 503,
    "schema_missing": 503,
    "migration_required": 503,
    "future_schema": 503,
    "schema_fingerprint_mismatch": 503,
    "store_corrupt": 503,
    "store_unsafe": 503,
}
_RETRYABLE = frozenset({
    "local_files_unavailable", "store_read_failed", "schema_missing",
    "migration_required",
})


class ApiError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ApiService:
    registry_provider: Callable[[], project_registry_store.ProjectRegistryStore]


def install(app: FastAPI, service: ApiService) -> None:
    @app.get("/api/project-registry/projects/{project_id}/workspaces")
    def workspaces(project_id: str, request: Request):
        def operation():
            registry = service.registry_provider()
            values = registry.list_workspaces(project_id)
            if values is None:
                raise ApiError("project_not_found")
            locations = registry.list_repo_locations(project_id)
            if locations is None:
                raise ApiError("project_not_found")
            by_id = {item.repo_location_id: item for item in locations}
            data = {"items": [_workspace(item, by_id) for item in values]}
            return _success(data, _meta(
                request,
                files_source=False,
                files_available=False,
                files_reason="workspace_selection_required",
            ))
        return _run(operation, request)

    @app.get("/api/project-registry/projects/{project_id}/workspaces/{workspace_id}")
    def workspace(project_id: str, workspace_id: str, request: Request):
        def operation():
            value, location = _workspace_context(service, project_id, workspace_id)
            available, reason = _files_capability(value, location)
            return _success(_workspace(value, {
                location.repo_location_id: location,
            }), _meta(
                request,
                files_source=False,
                files_available=available,
                files_reason=reason,
            ))
        return _run(operation, request)

    @app.get(
        "/api/project-registry/projects/{project_id}/workspaces/{workspace_id}/files"
    )
    def file_tree(project_id: str, workspace_id: str, request: Request):
        def operation():
            relative = _single_query(request, "path", "")
            root = _files_root(service, project_id, workspace_id)
            result = files.list_dir_from_trusted_root(root, relative)
            return _success(_tree(result, relative), _meta(
                request, files_source=True, files_available=True,
            ))
        return _run(operation, request)

    @app.get(
        "/api/project-registry/projects/{project_id}/workspaces/{workspace_id}/files/content"
    )
    def file_content(project_id: str, workspace_id: str, request: Request):
        def operation():
            relative = _single_query(request, "path", "")
            root = _files_root(service, project_id, workspace_id)
            result = files.read_file_from_trusted_root(root, relative)
            return _success(_content(result, relative), _meta(
                request, files_source=True, files_available=True,
            ))
        return _run(operation, request)

    @app.get(
        "/api/project-registry/projects/{project_id}/workspaces/{workspace_id}/files/search"
    )
    def file_search(project_id: str, workspace_id: str, request: Request):
        def operation():
            relative = _single_query(request, "path", "")
            query = _single_query(request, "q", None)
            limit = _limit(_single_query(request, "limit", "100"))
            if query is None or not query.strip() or len(query.strip()) > 128:
                raise ApiError("invalid_argument")
            root = _files_root(service, project_id, workspace_id)
            result = files.search_files_from_trusted_root(
                root, relative, query, limit,
            )
            return _success(
                _search(result, Path(root).resolve(strict=True)),
                _meta(request, files_source=True, files_available=True),
            )
        return _run(operation, request)


def _workspace_context(
    service: ApiService, project_id: str, workspace_id: str,
) -> tuple[domain.WorkspaceRecord, domain.RepoLocationRecord]:
    registry = service.registry_provider()
    workspace = registry.get_workspace(project_id, workspace_id)
    if workspace is None:
        raise ApiError("project_or_workspace_not_found")
    locations = registry.list_repo_locations(project_id)
    location = next(
        (item for item in locations or ()
         if item.repo_location_id == workspace.repo_location_id),
        None,
    )
    if location is None:
        raise ApiError("project_or_workspace_not_found")
    return workspace, location


def _files_root(service: ApiService, project_id: str, workspace_id: str) -> str:
    workspace, location = _workspace_context(service, project_id, workspace_id)
    available, _reason = _files_capability(workspace, location)
    if not available:
        raise ApiError("workspace_files_unavailable")
    return location.canonical_path


def _files_capability(
    workspace: domain.WorkspaceRecord, location: domain.RepoLocationRecord,
) -> tuple[bool, str | None]:
    if workspace.lifecycle != "active":
        return False, "workspace_not_active"
    if location.lifecycle != "active":
        return False, "repo_location_not_active"
    if location.node_id != "local":
        return False, "repo_location_not_local"
    if location.availability != "available":
        return False, "repo_location_unavailable"
    return True, None


def _workspace(
    value: domain.WorkspaceRecord,
    locations: dict[str, domain.RepoLocationRecord],
) -> dict[str, object]:
    location = locations.get(value.repo_location_id)
    if location is None:
        raise ApiError("project_or_workspace_not_found")
    return {
        "workspace_id": value.workspace_id,
        "project_id": value.project_id,
        "repo_location_id": value.repo_location_id,
        "name": value.name,
        "goal": value.goal,
        "isolation_kind": value.isolation_kind,
        "lifecycle": value.lifecycle,
        "active_run_id": value.active_run_id,
        "version": value.version,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
        "repo_location": {
            "node_id": location.node_id,
            "availability": location.availability,
        },
    }


def _tree(result: dict[str, object], relative: str) -> dict[str, object]:
    entries = result.get("entries")
    if not isinstance(entries, list):
        raise ApiError("local_files_unavailable")
    return {
        "path": relative,
        "entries": [{
            key: item[key]
            for key in ("name", "type", "size", "ext")
        } for item in entries if isinstance(item, dict)],
    }


def _content(result: dict[str, object], relative: str) -> dict[str, object]:
    public = {
        "path": relative,
        "size": result["size"],
        "binary": result["binary"],
    }
    if result.get("binary") is False:
        public["text"] = result["text"]
    return public


def _search(result: dict[str, object], root: Path) -> dict[str, object]:
    values = result.get("results")
    if not isinstance(values, list):
        raise ApiError("local_files_unavailable")
    public = []
    for item in values:
        if not isinstance(item, dict):
            raise ApiError("local_files_unavailable")
        try:
            relative = Path(str(item["path"])).relative_to(root).as_posix()
        except (KeyError, ValueError):
            raise ApiError("local_files_unavailable") from None
        public.append({
            "name": item["name"],
            "path": relative,
            "type": item["type"],
            "size": item["size"],
            "ext": item["ext"],
        })
    return {
        "path": Path(str(result["path"])).relative_to(root).as_posix()
        if Path(str(result["path"])) != root else "",
        "query": result["query"],
        "results": public,
        "truncated": result["truncated"],
    }


def _single_query(request: Request, key: str, default: str | None) -> str | None:
    values = request.query_params.getlist(key)
    if len(values) > 1:
        raise ApiError("invalid_argument")
    return values[0] if values else default


def _limit(value: str | None) -> int:
    try:
        parsed = int(value) if value is not None else 100
    except ValueError:
        raise ApiError("invalid_argument") from None
    if not 1 <= parsed <= 100 or str(parsed) != value:
        raise ApiError("invalid_argument")
    return parsed


def _meta(
    request: Request, *, files_source: bool, files_available: bool,
    files_reason: str | None = None,
) -> dict[str, object]:
    names = ["project_registry"]
    if files_source:
        names.append("local_files")
    return {
        "request_id": project_registry_api.request_id_for(request),
        "generated_at": datetime.now(UTC).isoformat(),
        "partial": False,
        "sources": [{
            "name": name,
            "status": "available",
            "observed_at": None,
            "reason": None,
        } for name in names],
        "warnings": [],
        "capabilities": {
            "files.read": {
                "available": files_available,
                "reason": files_reason,
            },
            "terminal.pty": {
                "available": False,
                "reason": "workspace_terminal_ticket_deferred",
            },
        },
    }


def _success(data: object, meta: dict[str, object]) -> JSONResponse:
    return JSONResponse({"data": data, "meta": meta})


def _error(request: Request, code: str) -> JSONResponse:
    public = code if code in _STATUS else "internal_error"
    return project_registry_api.g3_error(
        request,
        status=_STATUS.get(public, 500),
        code=public,
        message=public.replace("_", " "),
        retryable=public in _RETRYABLE,
    )


def _run(operation, request: Request) -> JSONResponse:
    try:
        return operation()
    except ApiError as exc:
        return _error(request, exc.code)
    except files.TrustedRootError as exc:
        return _error(request, exc.code)
    except project_registry_store.ProjectRegistryError as exc:
        return _error(request, exc.code)
