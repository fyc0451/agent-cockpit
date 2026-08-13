"""Frozen Project Registry v1 schema and readiness fingerprint."""
from __future__ import annotations

import hashlib


SCHEMA_VERSION = 1
MIGRATION_ID = "project-registry-v1"

SCHEMA_STATEMENTS = (
    """CREATE TABLE schema_migrations (
        migration_id TEXT NOT NULL PRIMARY KEY,
        schema_version INTEGER NOT NULL UNIQUE CHECK(schema_version = 1),
        schema_digest TEXT NOT NULL CHECK(length(schema_digest) = 64),
        applied_at TEXT NOT NULL
    )""",
    """CREATE TABLE projects (
        project_id TEXT NOT NULL PRIMARY KEY CHECK(
            length(project_id) = 36 AND substr(project_id, 1, 4) = 'prj_' AND
            substr(project_id, 5) NOT GLOB '*[^0-9a-f]*'
        ),
        slug TEXT NOT NULL UNIQUE CHECK(
            length(slug) BETWEEN 1 AND 64 AND
            slug NOT GLOB '*[^a-z0-9-]*' AND
            slug NOT LIKE '-%' AND slug NOT LIKE '%-' AND
            slug NOT LIKE '%--%'
        ),
        display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 256),
        goal TEXT CHECK(goal IS NULL OR length(goal) <= 4096),
        lifecycle TEXT NOT NULL DEFAULT 'active'
            CHECK(lifecycle IN ('active', 'archived')),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER projects_slug_immutable
        BEFORE UPDATE OF slug ON projects
        WHEN NEW.slug <> OLD.slug
        BEGIN SELECT RAISE(ABORT, 'project_slug_immutable'); END""",
    """CREATE TABLE repo_locations (
        repo_location_id TEXT NOT NULL PRIMARY KEY CHECK(
            length(repo_location_id) = 36 AND
            substr(repo_location_id, 1, 4) = 'loc_' AND
            substr(repo_location_id, 5) NOT GLOB '*[^0-9a-f]*'
        ),
        project_id TEXT NOT NULL,
        node_id TEXT NOT NULL CHECK(length(node_id) BETWEEN 1 AND 128),
        canonical_path TEXT NOT NULL CHECK(
            length(canonical_path) BETWEEN 1 AND 4096 AND
            substr(canonical_path, 1, 1) = '/' AND
            canonical_path NOT LIKE '%/' AND
            canonical_path NOT LIKE '%//%' AND
            canonical_path NOT LIKE '%/./%' AND
            canonical_path NOT LIKE '%/../%' AND
            canonical_path NOT LIKE '%/.' AND
            canonical_path NOT LIKE '%/..' AND
            instr(canonical_path, char(0)) = 0
        ),
        lifecycle TEXT NOT NULL DEFAULT 'active'
            CHECK(lifecycle IN ('active', 'archived')),
        vcs_kind TEXT NOT NULL CHECK(vcs_kind IN ('git', 'none')),
        git_root TEXT CHECK(git_root IS NULL OR length(git_root) <= 4096),
        git_remote_fingerprint TEXT CHECK(
            git_remote_fingerprint IS NULL OR length(git_remote_fingerprint) <= 256
        ),
        default_ref_observed TEXT CHECK(
            default_ref_observed IS NULL OR length(default_ref_observed) <= 1024
        ),
        availability TEXT NOT NULL DEFAULT 'unknown'
            CHECK(availability IN ('available', 'offline', 'missing', 'unknown')),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(project_id) REFERENCES projects(project_id),
        UNIQUE(project_id, repo_location_id)
    )""",
    """CREATE UNIQUE INDEX repo_locations_active_node_path
        ON repo_locations(node_id, canonical_path)
        WHERE lifecycle = 'active'""",
    """CREATE TABLE workspaces (
        workspace_id TEXT NOT NULL PRIMARY KEY CHECK(
            length(workspace_id) = 35 AND
            substr(workspace_id, 1, 3) = 'ws_' AND
            substr(workspace_id, 4) NOT GLOB '*[^0-9a-f]*'
        ),
        project_id TEXT NOT NULL,
        repo_location_id TEXT NOT NULL,
        name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 256),
        goal TEXT CHECK(goal IS NULL OR length(goal) <= 4096),
        isolation_kind TEXT NOT NULL CHECK(isolation_kind IN (
            'shared', 'isolated_worktree', 'review_detached'
        )),
        lifecycle TEXT NOT NULL DEFAULT 'active'
            CHECK(lifecycle IN ('active', 'archived')),
        active_run_id TEXT CHECK(
            active_run_id IS NULL OR length(active_run_id) BETWEEN 1 AND 128
        ),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(project_id) REFERENCES projects(project_id),
        FOREIGN KEY(project_id, repo_location_id)
            REFERENCES repo_locations(project_id, repo_location_id)
    )""",
    """CREATE UNIQUE INDEX workspaces_active_project_name
        ON workspaces(project_id, name)
        WHERE lifecycle = 'active'""",
    """CREATE TABLE legacy_project_bindings (
        binding_id TEXT NOT NULL PRIMARY KEY CHECK(
            length(binding_id) = 36 AND substr(binding_id, 1, 4) = 'bnd_' AND
            substr(binding_id, 5) NOT GLOB '*[^0-9a-f]*'
        ),
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        source_kind TEXT NOT NULL CHECK(source_kind IN (
            'agent_mail_project', 'mail_projects_session',
            'herdr_session', 'coordination_run'
        )),
        source_key TEXT NOT NULL CHECK(length(source_key) BETWEEN 1 AND 4096),
        source_digest TEXT NOT NULL CHECK(length(source_digest) BETWEEN 1 AND 256),
        imported_at TEXT NOT NULL,
        UNIQUE(source_kind, source_key)
    )""",
    """CREATE TABLE idempotency_records (
        scope TEXT NOT NULL CHECK(length(scope) BETWEEN 1 AND 128),
        idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 128),
        request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
        status_code INTEGER NOT NULL CHECK(status_code BETWEEN 100 AND 599),
        response_json TEXT NOT NULL CHECK(length(response_json) BETWEEN 2 AND 65536),
        created_at TEXT NOT NULL,
        PRIMARY KEY(scope, idempotency_key)
    )""",
)

SCHEMA_DIGEST = hashlib.sha256(
    "\n".join(" ".join(statement.split()) for statement in SCHEMA_STATEMENTS).encode("ascii")
).hexdigest()

# Public probe contract consumed by store_schema/release-readiness wiring.
PROJECT_REGISTRY_TABLES = {
    "schema_migrations": (
        ("migration_id", "TEXT", 1, 1),
        ("schema_version", "INTEGER", 1, 0),
        ("schema_digest", "TEXT", 1, 0),
        ("applied_at", "TEXT", 1, 0),
    ),
    "projects": (
        ("project_id", "TEXT", 1, 1), ("slug", "TEXT", 1, 0),
        ("display_name", "TEXT", 1, 0), ("goal", "TEXT", 0, 0),
        ("lifecycle", "TEXT", 1, 0), ("version", "INTEGER", 1, 0),
        ("created_at", "TEXT", 1, 0), ("updated_at", "TEXT", 1, 0),
    ),
    "repo_locations": (
        ("repo_location_id", "TEXT", 1, 1), ("project_id", "TEXT", 1, 0),
        ("node_id", "TEXT", 1, 0), ("canonical_path", "TEXT", 1, 0),
        ("lifecycle", "TEXT", 1, 0), ("vcs_kind", "TEXT", 1, 0),
        ("git_root", "TEXT", 0, 0),
        ("git_remote_fingerprint", "TEXT", 0, 0),
        ("default_ref_observed", "TEXT", 0, 0),
        ("availability", "TEXT", 1, 0), ("version", "INTEGER", 1, 0),
        ("created_at", "TEXT", 1, 0), ("updated_at", "TEXT", 1, 0),
    ),
    "workspaces": (
        ("workspace_id", "TEXT", 1, 1), ("project_id", "TEXT", 1, 0),
        ("repo_location_id", "TEXT", 1, 0), ("name", "TEXT", 1, 0),
        ("goal", "TEXT", 0, 0), ("isolation_kind", "TEXT", 1, 0),
        ("lifecycle", "TEXT", 1, 0), ("active_run_id", "TEXT", 0, 0),
        ("version", "INTEGER", 1, 0), ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "legacy_project_bindings": (
        ("binding_id", "TEXT", 1, 1), ("project_id", "TEXT", 1, 0),
        ("source_kind", "TEXT", 1, 0), ("source_key", "TEXT", 1, 0),
        ("source_digest", "TEXT", 1, 0), ("imported_at", "TEXT", 1, 0),
    ),
    "idempotency_records": (
        ("scope", "TEXT", 1, 1), ("idempotency_key", "TEXT", 1, 2),
        ("request_digest", "TEXT", 1, 0), ("status_code", "INTEGER", 1, 0),
        ("response_json", "TEXT", 1, 0), ("created_at", "TEXT", 1, 0),
    ),
}

PROJECT_REGISTRY_DEFAULTS = {
    "projects": {"lifecycle": "active", "version": "1"},
    "repo_locations": {
        "lifecycle": "active", "availability": "unknown", "version": "1",
    },
    "workspaces": {"lifecycle": "active", "version": "1"},
}

PROJECT_REGISTRY_INDEXES = {
    "schema_migrations": frozenset(),
    "projects": frozenset(),
    "repo_locations": frozenset({
        ("repo_locations_active_node_path", 1, 1, ("node_id", "canonical_path")),
    }),
    "workspaces": frozenset({
        ("workspaces_active_project_name", 1, 1, ("project_id", "name")),
    }),
    "legacy_project_bindings": frozenset(),
    "idempotency_records": frozenset(),
}

PROJECT_REGISTRY_FOREIGN_KEYS = {
    "schema_migrations": frozenset(),
    "projects": frozenset(),
    "repo_locations": frozenset({
        (("project_id", "projects", "project_id"),),
    }),
    "workspaces": frozenset({
        (("project_id", "projects", "project_id"),),
        (
            ("project_id", "repo_locations", "project_id"),
            ("repo_location_id", "repo_locations", "repo_location_id"),
        ),
    }),
    "legacy_project_bindings": frozenset({
        (("project_id", "projects", "project_id"),),
    }),
    "idempotency_records": frozenset(),
}

PROJECT_REGISTRY_TRIGGERS = frozenset({"projects_slug_immutable"})
PROJECT_REGISTRY_MIGRATION_RECEIPT = (
    MIGRATION_ID,
    SCHEMA_VERSION,
    SCHEMA_DIGEST,
)
