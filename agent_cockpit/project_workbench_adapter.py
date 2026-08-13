"""Read-only compatibility projection for the legacy Project workbench."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Callable

from . import project_registry_domain


COMPATIBILITY_CONFLICT_DETAIL = "项目兼容绑定冲突"
SESSION_PROVENANCE_KINDS = frozenset({
    "mail_projects_session", "herdr_session",
})
ASSIGNMENT_FIELDS = (
    "assignment_id", "assignment", "assignee", "expected_reply", "deadline",
    "status", "closed_at", "version", "created_at", "updated_at",
)
PANE_FIELDS = (
    "pane_id", "agent", "agent_status", "focused", "revision",
)


@dataclass(frozen=True)
class WorkbenchReadError(RuntimeError):
    status_code: int
    detail: str


def legacy_source_key(identity: object) -> str:
    canonical = project_registry_domain.canonical_json(identity).encode("ascii")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _compatibility_conflict() -> WorkbenchReadError:
    return WorkbenchReadError(409, COMPATIBILITY_CONFLICT_DETAIL)


def _binding_pair(binding: object) -> tuple[str, str]:
    kind = getattr(binding, "source_kind", None)
    key = getattr(binding, "source_key", None)
    if not isinstance(kind, str) or not isinstance(key, str):
        raise _compatibility_conflict()
    return kind, key


def read_workbench(
    slug: str,
    *,
    mail_status_provider: Callable[[], Mapping[str, Any]],
    legacy_project_provider: Callable[[str], Mapping[str, Any] | None],
    registry_provider: Callable[[], Any],
    assignments_provider: Callable[[str], list[Mapping[str, Any]]],
    runtime_snapshot_provider: Callable[[], object],
    live_binding_provider: Callable[[str, str], str | None],
    observed_at: float,
) -> dict[str, Any]:
    try:
        mail_status = mail_status_provider()
        if not isinstance(mail_status, Mapping):
            raise TypeError("invalid mail status")
        mail_available = mail_status.get("available") is True
    except Exception:
        raise WorkbenchReadError(503, "Agent Mail 查询失败") from None
    if not mail_available:
        raise WorkbenchReadError(503, "Agent Mail 不可用")

    try:
        project = legacy_project_provider(slug)
    except Exception:
        raise WorkbenchReadError(503, "Agent Mail 查询失败") from None
    if project is None:
        raise WorkbenchReadError(404, f"项目不存在: {slug}")
    try:
        if (
            not isinstance(project, Mapping)
            or type(project.get("id")) is not int
            or not isinstance(project.get("slug"), str)
            or project.get("slug") != slug
            or not isinstance(project.get("human_key"), str)
            or not project.get("human_key")
        ):
            raise _compatibility_conflict()
    except WorkbenchReadError:
        raise
    except Exception:
        raise _compatibility_conflict() from None

    try:
        registry = registry_provider()
        registry_project = registry.get_project_by_slug(slug)
        project_id = getattr(registry_project, "project_id", None)
        registry_slug = getattr(registry_project, "slug", None)
        if (
            not isinstance(project_id, str) or not project_id
            or not isinstance(registry_slug, str) or registry_slug != slug
        ):
            raise _compatibility_conflict()
        raw_bindings = registry.list_legacy_bindings(project_id)
        if raw_bindings is None:
            raise _compatibility_conflict()
        bindings = tuple(_binding_pair(binding) for binding in raw_bindings)
        legacy_id = project["id"]
        expected_mail_key = legacy_source_key({"project_id": legacy_id})
        mail_keys = tuple(
            key for kind, key in bindings if kind == "agent_mail_project"
        )
        if mail_keys != (expected_mail_key,):
            raise _compatibility_conflict()
        session_keys = {
            key for kind, key in bindings if kind in SESSION_PROVENANCE_KINDS
        }
    except WorkbenchReadError:
        raise
    except Exception:
        raise _compatibility_conflict() from None

    project_key = project["human_key"]
    try:
        assignment_rows = assignments_provider(project_key)
        if (
            isinstance(assignment_rows, (str, bytes, Mapping))
            or not isinstance(assignment_rows, Iterable)
        ):
            raise TypeError("invalid assignment rows")
        assignments = []
        for item in assignment_rows:
            if not isinstance(item, Mapping):
                raise TypeError("invalid assignment row")
            assignments.append({
                field: item.get(field) for field in ASSIGNMENT_FIELDS
            })
    except Exception:
        raise WorkbenchReadError(503, "Coordination 查询失败") from None

    try:
        snapshot = runtime_snapshot_provider()
    except Exception:
        snapshot = {"available": False, "degraded": True}
    if not isinstance(snapshot, dict):
        snapshot = {"available": False, "degraded": True}
    available = snapshot.get("available") is True
    degraded = not available or snapshot.get("degraded") is True
    snapshot_sessions = snapshot.get("sessions")
    if not degraded and not isinstance(snapshot_sessions, (list, tuple)):
        degraded = True

    sessions: list[dict[str, Any]] = []
    seen_generations: set[tuple[str, str]] = set()
    if not degraded:
        for item in snapshot_sessions:
            if not isinstance(item, dict):
                continue
            session = item.get("session")
            directory = item.get("directory")
            if not isinstance(session, str) or not session:
                continue
            if not isinstance(directory, str) or not directory:
                continue
            generation = (session, directory)
            if generation in seen_generations:
                raise _compatibility_conflict()
            seen_generations.add(generation)
            try:
                bound_project = live_binding_provider(session, directory)
            except Exception:
                raise _compatibility_conflict() from None
            if bound_project != project_key:
                continue
            expected_session_key = legacy_source_key({
                "session": session, "session_dir": directory,
            })
            if expected_session_key not in session_keys:
                raise _compatibility_conflict()
            panes = [
                {field: pane.get(field) for field in PANE_FIELDS}
                for pane in item.get("panes") or []
                if isinstance(pane, dict)
            ]
            sessions.append({
                "session": session,
                "status": item.get("status"),
                "focused_pane_id": item.get("focused_pane_id"),
                "panes": panes,
            })

    return {
        "project": {
            "id": project["id"],
            "slug": project["slug"],
            "created_at": project.get("created_at"),
        },
        "assignments": assignments,
        "sessions": sessions,
        "source": {
            "available": available,
            "degraded": degraded,
            "observed_at": observed_at,
        },
    }
