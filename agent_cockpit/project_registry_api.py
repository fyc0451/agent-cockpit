"""G3 HTTP boundary for the Local-only Project Registry."""
from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import project_discovery as discovery_contract
from . import project_registry_domain as registry_domain
from . import project_registry_store as registry_store


CREATE_SCOPE = "project-registry.projects.create.v1"
ATTACH_SCOPE = "project-registry.repo-locations.create.v1"
_RETRYABLE = frozenset({"discovery_unavailable", "store_read_failed", "store_write_failed"})
_STATUS = {
    "invalid_argument": 400,
    "invalid_locator": 400,
    "idempotency_key_required": 400,
    "root_forbidden": 403,
    "node_not_found": 404,
    "project_not_found": 404,
    "discovery_stale": 409,
    "project_slug_conflict": 409,
    "location_already_registered": 409,
    "version_conflict": 409,
    "repository_identity_unproven": 409,
    "idempotency_conflict": 409,
    "capability_unavailable": 412,
    "discovery_unavailable": 503,
    "store_read_failed": 503,
    "store_write_failed": 503,
    "schema_missing": 503,
    "migration_required": 503,
    "future_schema": 503,
    "schema_fingerprint_mismatch": 503,
    "store_corrupt": 503,
    "store_unsafe": 503,
}


class DiscoveryService(Protocol):
    def list_roots(self) -> tuple[discovery_contract.RootDescriptor, ...]: ...

    def list_directories(
        self, locator: discovery_contract.ProjectLocator, query: str | None = None,
    ) -> discovery_contract.DirectoryListing: ...

    def discover(
        self, locator: discovery_contract.ProjectLocator,
    ) -> discovery_contract.DiscoveryResult: ...


class ApiError(RuntimeError):
    def __init__(self, code: str, details: Mapping[str, object] | None = None):
        self.code = code
        self.details = dict(details or {})
        super().__init__(code)


@dataclass(frozen=True)
class ApiService:
    registry_provider: Callable[[], registry_store.ProjectRegistryStore]
    discovery_provider: Callable[[], DiscoveryService]

    def prepare(self) -> None:
        self.registry_provider()


def install(app: FastAPI, service: ApiService) -> None:
    """Install only Project/Discovery v1 routes; legacy server routes stay untouched."""

    @app.get("/api/runtime-nodes")
    def runtime_nodes(request: Request):
        return _run(lambda: _success(
            {"nodes": [{"node_id": "local", "display_name": "Local", "kind": "local"}]},
            _meta(), request,
        ), request)

    @app.get("/api/runtime-nodes/{node_id}/roots")
    def roots(node_id: str, request: Request):
        def operation():
            if node_id != "local":
                raise ApiError("capability_unavailable")
            values = [item.to_public_dict() for item in service.discovery_provider().list_roots()]
            return _success({"items": values}, _meta(), request)
        return _run(operation, request)

    @app.get("/api/runtime-nodes/{node_id}/directories")
    def directories(
        node_id: str, request: Request, root_id: str | None = None,
        path: str = "", query: str | None = None,
    ):
        def operation():
            if root_id is None:
                raise ApiError("invalid_locator")
            locator = _locator({"node_id": node_id, "root_id": root_id, "path": path})
            listing = service.discovery_provider().list_directories(locator, query)
            data = listing.to_public_dict()
            return _success(data, _meta(
                partial=data["partial"], sources=data["sources"], warnings=data["warnings"],
                write_available=not data["partial"],
            ), request)
        return _run(operation, request)

    @app.post("/api/project-discovery")
    async def discover(request: Request):
        async def operation():
            body = await _body(request, {"locator"})
            locator = _locator(body["locator"])
            result = service.discovery_provider().discover(locator)
            data = result.to_public_dict()
            return _success(data, _meta(
                partial=not result.complete, sources=result.sources, warnings=result.warnings,
                write_available=result.complete and not result.warnings and "project_registry" in result.sources,
            ), request)
        return await _run_async(operation, request)

    @app.get("/api/project-registry/projects")
    def list_projects(request: Request):
        def operation():
            lifecycle = _single_query(request, "lifecycle", "active")
            limit = _single_query(request, "limit", "50")
            cursor = _single_query(request, "cursor", None)
            if lifecycle not in {"active", "archived"}:
                raise ApiError("invalid_argument")
            try:
                parsed_limit = int(limit)
            except (TypeError, ValueError):
                raise ApiError("invalid_argument") from None
            if not 1 <= parsed_limit <= 100 or str(parsed_limit) != limit:
                raise ApiError("invalid_argument")
            after = _decode_cursor(cursor, lifecycle) if cursor else None
            page = service.registry_provider().list_projects(
                lifecycle=lifecycle, after_project_id=after, limit=parsed_limit,
            )
            next_cursor = _encode_cursor(lifecycle, page.next_project_id) if page.next_project_id else None
            return _success({
                "items": [_snapshot(item) for item in page.items],
                "next_cursor": next_cursor,
            }, _meta(), request)
        return _run(operation, request)

    @app.get("/api/project-registry/projects/{project_id}")
    def project_detail(project_id: str, request: Request):
        def operation():
            snapshot = service.registry_provider().get_project_by_id(project_id)
            if snapshot is None:
                raise ApiError("project_not_found")
            return _success(_snapshot(snapshot), _meta(), request)
        return _run(operation, request)

    @app.get("/api/project-registry/projects/{project_id}/repo-locations")
    def repo_locations(project_id: str, request: Request):
        def operation():
            locations = service.registry_provider().list_repo_locations(project_id)
            if locations is None:
                raise ApiError("project_not_found")
            return _success({"items": [_location(item) for item in locations]}, _meta(), request)
        return _run(operation, request)

    @app.post("/api/project-registry/projects")
    async def create_project(request: Request):
        async def operation():
            body = await _body(request, {
                "display_name", "slug", "goal", "locator", "expected_discovery_fingerprint",
            })
            _validate_create_body(body)
            key = _idempotency_key(request)
            registry = service.registry_provider()
            replay = registry.preflight_idempotency(
                scope=CREATE_SCOPE, idempotency_key=key, payload=body,
            )
            if replay is not None:
                return _success(dict(replay.response), _meta(write_available=True), request, replay.status_code)
            result = _secondary_discovery(service, body, require_unowned=True)
            created = registry.idempotent_register_project(
                scope=CREATE_SCOPE, idempotency_key=key, payload=body,
                slug=body["slug"], display_name=body["display_name"], goal=body["goal"],
                node_id=result.locator.node_id, canonical_path=result.canonical_path,
                vcs_kind=result.vcs.kind, availability="available",
                git_remote_fingerprint=result.vcs.remote_fingerprint,
            )
            return _success(dict(created.response), _meta(write_available=True), request, created.status_code)
        return await _run_async(operation, request)

    @app.post("/api/project-registry/projects/{project_id}/repo-locations")
    async def add_repo_location(project_id: str, request: Request):
        async def operation():
            body = await _body(request, {
                "locator", "expected_discovery_fingerprint", "expected_project_version",
            })
            _validate_attach_body(project_id, body)
            key = _idempotency_key(request)
            payload = {"project_id": project_id, **body}
            registry = service.registry_provider()
            replay = registry.preflight_idempotency(
                scope=ATTACH_SCOPE, idempotency_key=key, payload=payload,
            )
            if replay is not None:
                return _success(dict(replay.response), _meta(write_available=True), request, replay.status_code)
            result = _secondary_discovery(service, body, require_unowned=True)
            if result.vcs.kind != "git" or result.vcs.remote_fingerprint is None:
                raise ApiError("repository_identity_unproven")
            attached = registry.idempotent_add_repo_location(
                scope=ATTACH_SCOPE, idempotency_key=key, payload=payload,
                project_id=project_id, expected_project_version=body["expected_project_version"],
                node_id=result.locator.node_id, canonical_path=result.canonical_path,
                vcs_kind=result.vcs.kind, availability="available",
                git_remote_fingerprint=result.vcs.remote_fingerprint,
            )
            return _success(dict(attached.response), _meta(write_available=True), request, attached.status_code)
        return await _run_async(operation, request)


async def _body(request: Request, fields: set[str]) -> dict[str, Any]:
    try:
        value = json.loads((await request.body()).decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise ApiError("invalid_argument") from None
    if not isinstance(value, dict) or set(value) != fields:
        raise ApiError("invalid_argument")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _single_query(request: Request, key: str, default: str | None) -> str | None:
    values = request.query_params.getlist(key)
    if len(values) > 1:
        raise ApiError("invalid_argument")
    return values[0] if values else default


def _locator(value: object) -> discovery_contract.ProjectLocator:
    if not isinstance(value, dict) or set(value) != {"node_id", "root_id", "path"}:
        raise ApiError("invalid_locator")
    if not all(isinstance(value.get(key), str) for key in ("node_id", "root_id", "path")):
        raise ApiError("invalid_locator")
    return discovery_contract.ProjectLocator(value["node_id"], value["root_id"], value["path"])


def _idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    if not isinstance(key, str) or not key or len(key) > 128 or any(ord(char) < 33 or ord(char) == 127 for char in key):
        raise ApiError("idempotency_key_required")
    return key


def _validate_create_body(body: Mapping[str, Any]) -> None:
    try:
        registry_domain.slug(body["slug"])
        registry_domain.text(body["display_name"], maximum=256)
        registry_domain.optional_text(body["goal"], maximum=4096)
        discovery_contract.require_hash(body["expected_discovery_fingerprint"])
    except (KeyError, ValueError):
        raise ApiError("invalid_argument") from None
    _locator(body["locator"])


def _validate_attach_body(project_id: str, body: Mapping[str, Any]) -> None:
    try:
        registry_domain.opaque(project_id, maximum=64)
        discovery_contract.require_hash(body["expected_discovery_fingerprint"])
    except ValueError:
        raise ApiError("invalid_argument") from None
    if type(body.get("expected_project_version")) is not int or body["expected_project_version"] < 1:
        raise ApiError("invalid_argument")
    _locator(body["locator"])


def _secondary_discovery(service: ApiService, body: Mapping[str, Any], *, require_unowned: bool):
    locator = _locator(body.get("locator"))
    result = service.discovery_provider().discover(locator)
    expected = body["expected_discovery_fingerprint"]
    if not result.complete or result.warnings or "project_registry" not in result.sources:
        raise ApiError("discovery_unavailable")
    if result.discovery_fingerprint != expected:
        raise ApiError("discovery_stale")
    if require_unowned and result.exact_match is not None:
        raise ApiError("location_already_registered")
    return result


def _encode_cursor(lifecycle: str, project_id: str) -> str:
    raw = json.dumps({"v": 1, "l": lifecycle, "a": project_id}, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str, lifecycle: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw.decode("ascii"))
        if set(payload) != {"v", "l", "a"} or payload["v"] != 1 or payload["l"] != lifecycle:
            raise ValueError
        return registry_domain.opaque(payload["a"], maximum=64)
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ApiError("invalid_argument") from None


def _project(record: registry_domain.ProjectRecord) -> dict[str, object]:
    return {
        "project_id": record.project_id, "slug": record.slug,
        "display_name": record.display_name, "goal": record.goal,
        "lifecycle": record.lifecycle, "version": record.version,
        "created_at": record.created_at, "updated_at": record.updated_at,
    }


def _location(record: registry_domain.RepoLocationRecord) -> dict[str, object]:
    return {
        "repo_location_id": record.repo_location_id, "project_id": record.project_id,
        "node_id": record.node_id, "lifecycle": record.lifecycle,
        "vcs_kind": record.vcs_kind, "availability": record.availability,
        "version": record.version,
    }


def _snapshot(snapshot: registry_domain.ProjectSnapshot) -> dict[str, object]:
    return {"project": _project(snapshot.project), "repo_locations": [_location(item) for item in snapshot.repo_locations]}


def _meta(*, partial: bool = False, sources: tuple[str, ...] | list[str] = (), warnings: tuple[str, ...] | list[str] = (), write_available: bool = False) -> dict[str, object]:
    source_items = [
        {"name": name, "status": "available", "observed_at": None, "reason": None}
        for name in sources
    ]
    if partial and "project_registry" not in sources:
        source_items.append({
            "name": "project_registry", "status": "unavailable",
            "observed_at": None, "reason": "project_registry_unavailable",
        })
    if not source_items:
        source_items = [{"name": "project_registry", "status": "available", "observed_at": None, "reason": None}]
    capabilities = {
        "projectRegistry.read": {"available": True, "reason": None},
        "projectRegistry.write": {
            "available": write_available,
            "reason": None if write_available else "requires_complete_local_discovery",
        },
        "remote_herdr": {"available": False, "reason": "deferred_after_local_core"},
        "memory": {"available": False, "reason": "deferred_after_local_core"},
        "automation": {"available": False, "reason": "deferred_after_local_core"},
        "browser": {"available": False, "reason": "deferred_after_local_core"},
        "github": {"available": False, "reason": "deferred_after_local_core"},
        "electron": {"available": False, "reason": "deferred_after_local_core"},
    }
    return {
        "request_id": _request_id(), "generated_at": datetime.now(UTC).isoformat(),
        "partial": partial, "sources": source_items, "warnings": list(warnings),
        "capabilities": capabilities,
    }


def _request_id() -> str:
    return "req_" + secrets.token_hex(16)


def _success(data: object, meta: dict[str, object], _request: Request, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content={"data": data, "meta": meta})


def _error(code: str) -> JSONResponse:
    status = _STATUS.get(code, 500)
    public_code = code if code in _STATUS else "internal_error"
    return JSONResponse(status_code=status, content={"error": {
        "code": public_code,
        "message": public_code.replace("_", " "),
        "retryable": public_code in _RETRYABLE,
        "request_id": _request_id(), "details": {},
    }})


def _run(operation: Callable[[], JSONResponse], _request: Request) -> JSONResponse:
    try:
        return operation()
    except ApiError as exc:
        return _error(exc.code)
    except discovery_contract.DiscoveryError as exc:
        return _error(exc.code)
    except registry_store.ProjectRegistryError as exc:
        return _error(exc.code)
    except Exception:
        return _error("internal_error")


async def _run_async(operation: Callable[[], Any], request: Request) -> JSONResponse:
    try:
        return await operation()
    except ApiError as exc:
        return _error(exc.code)
    except discovery_contract.DiscoveryError as exc:
        return _error(exc.code)
    except registry_store.ProjectRegistryError as exc:
        return _error(exc.code)
    except Exception:
        return _error("internal_error")
