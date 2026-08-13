"""Validation and immutable value records for Project Registry v1."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
OPAQUE_RE = re.compile(r"^[A-Za-z0-9._:@/-]+$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
LEGACY_SOURCE_KINDS = frozenset({
    "agent_mail_project", "mail_projects_session", "herdr_session",
    "coordination_run",
})


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    slug: str
    display_name: str
    goal: str | None
    lifecycle: str
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RepoLocationRecord:
    repo_location_id: str
    project_id: str
    node_id: str
    canonical_path: str
    lifecycle: str
    vcs_kind: str
    availability: str
    version: int


@dataclass(frozen=True)
class WorkspaceRecord:
    workspace_id: str
    project_id: str
    repo_location_id: str
    name: str
    goal: str | None
    isolation_kind: str
    lifecycle: str
    active_run_id: str | None
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class LegacyBindingRecord:
    binding_id: str
    project_id: str
    source_kind: str
    source_key: str
    source_digest: str
    imported_at: str


@dataclass(frozen=True)
class LegacySourceInput:
    source_kind: str
    source_key: str
    source_digest: str


@dataclass(frozen=True)
class LegacyImportResult:
    project: ProjectRecord
    repo_location: RepoLocationRecord
    bindings: tuple[LegacyBindingRecord, ...]
    replayed: bool


@dataclass(frozen=True)
class CommandResult:
    status_code: int
    response: Mapping[str, Any]


def text(value: object, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid text")
    if (not allow_empty and not value) or len(value) > maximum:
        raise ValueError("invalid text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid text")
    return value


def optional_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return text(value, maximum=maximum, allow_empty=True)


def slug(value: object) -> str:
    value = text(value, maximum=64)
    if not SLUG_RE.fullmatch(value) or "--" in value:
        raise ValueError("invalid slug")
    return value


def opaque(value: object, *, maximum: int) -> str:
    value = text(value, maximum=maximum)
    if not OPAQUE_RE.fullmatch(value):
        raise ValueError("invalid opaque identity")
    return value


def sha256_ref(value: object) -> str:
    value = text(value, maximum=71)
    if not SHA256_RE.fullmatch(value):
        raise ValueError("invalid sha256 reference")
    return value


def canonical_path(value: object) -> str:
    value = text(value, maximum=4096)
    if not value.startswith("/") or value == "/" or value.endswith("/"):
        raise ValueError("invalid canonical path")
    if any(part in ("", ".", "..") for part in value.split("/")[1:]):
        raise ValueError("invalid canonical path")
    return value


def enum(value: object, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("invalid enum")
    return value


def canonical_json(value: object) -> str:
    if isinstance(value, Mapping) and not all(isinstance(key, str) for key in value):
        raise ValueError("mapping keys must be strings")
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not canonical JSON") from exc
