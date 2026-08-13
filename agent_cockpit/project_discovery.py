"""Pure value contracts for Local Project discovery."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping


FINGERPRINT_VERSION = 1
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class DiscoveryError(RuntimeError):
    """A stable discovery failure that does not expose paths or subprocess text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProjectLocator:
    node_id: str
    root_id: str
    path: str

    def to_public_dict(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "root_id": self.root_id,
            "path": self.path,
        }


@dataclass(frozen=True)
class RootDescriptor:
    node_id: str
    root_id: str
    display_name: str

    def to_public_dict(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "root_id": self.root_id,
            "display_name": self.display_name,
        }


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    path: str
    kind: str = "directory"
    vcs_hint: str = "unknown"
    registered_project: RegistryMatch | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
            "vcs_hint": self.vcs_hint,
            "registered_project": (
                self.registered_project.to_public_dict()
                if self.registered_project
                else None
            ),
        }


@dataclass(frozen=True)
class DirectoryListing:
    locator: ProjectLocator
    entries: tuple[DirectoryEntry, ...]
    complete: bool = True
    sources: tuple[str, ...] = ("local_files", "project_registry")
    warnings: tuple[str, ...] = ()

    @property
    def partial(self) -> bool:
        return not self.complete

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator.to_public_dict(),
            "entries": [entry.to_public_dict() for entry in self.entries],
            "complete": self.complete,
            "partial": self.partial,
            "sources": list(self.sources),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RegistryMatch:
    project_id: str
    slug: str
    display_name: str

    def to_public_dict(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "slug": self.slug,
            "display_name": self.display_name,
        }


@dataclass(frozen=True)
class VcsObservation:
    kind: str
    git_root_digest: str | None = None
    remote_fingerprint: str | None = None
    repository_fingerprint: str | None = None
    head: str | None = None
    branch_present: bool = False
    detached: bool = False
    unborn: bool = False
    dirty: bool = False
    status_digest: str | None = None
    refs_count: int = 0
    refs_digest: str | None = None
    upstream_present: bool = False
    ahead: int | None = None
    behind: int | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "git_root_digest": self.git_root_digest,
            "remote_fingerprint": self.remote_fingerprint,
            "repository_fingerprint": self.repository_fingerprint,
            "head": self.head,
            "branch_present": self.branch_present,
            "detached": self.detached,
            "unborn": self.unborn,
            "dirty": self.dirty,
            "status_digest": self.status_digest,
            "refs_count": self.refs_count,
            "refs_digest": self.refs_digest,
            "upstream_present": self.upstream_present,
            "ahead": self.ahead,
            "behind": self.behind,
        }

    def fingerprint_dict(self) -> dict[str, Any]:
        return self.to_public_dict()


@dataclass(frozen=True)
class DiscoveryResult:
    locator: ProjectLocator
    display_path: str
    canonical_path_digest: str
    vcs: VcsObservation
    exact_match: RegistryMatch | None
    possible_projects: tuple[RegistryMatch, ...]
    discovery_fingerprint: str
    observed_at: str
    complete: bool
    sources: tuple[str, ...] = ("local_files", "local_git", "project_registry")
    warnings: tuple[str, ...] = ()
    _canonical_path: str = field(default="", repr=False, compare=False)

    @property
    def canonical_path(self) -> str:
        """Trusted application-service input; never include in public serialization."""
        return self._canonical_path

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator.to_public_dict(),
            "display_path": self.display_path,
            "canonical_path_digest": self.canonical_path_digest,
            "vcs": self.vcs.to_public_dict(),
            "exact_match": (
                self.exact_match.to_public_dict() if self.exact_match else None
            ),
            "possible_projects": [
                match.to_public_dict() for match in self.possible_projects
            ],
            "discovery_fingerprint": self.discovery_fingerprint,
            "observed_at": self.observed_at,
            "complete": self.complete,
            "sources": list(self.sources),
            "warnings": list(self.warnings),
        }


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValueError("value is not canonical JSON") from None


def sha256_value(value: object) -> str:
    encoded = canonical_json(value).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def sha256_text(namespace: str, value: str) -> str:
    return sha256_value({"namespace": namespace, "value": value})


def discovery_fingerprint(payload: Mapping[str, object]) -> str:
    return sha256_value({"version": FINGERPRINT_VERSION, "evidence": payload})


def require_hash(value: str | None) -> None:
    if value is not None and not _HASH_RE.fullmatch(value):
        raise ValueError("invalid fingerprint")
