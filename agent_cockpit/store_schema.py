"""store_schema — pure-read app-owned store fingerprints for /health/ready (Wiki13 J1).

Contract (decisions/2026-08-10-agent-cockpit-j1-ready-schema-policy.md):
- No server import and no mkdir/DDL/WAL enable. Accepted complex stores are
  imported lazily only after missing/path/sidecar gates and opened read-only.
- SQLite: URI mode=ro + query_only; WAL without SHM → probe_requires_quiescence.
- Responses use stable reason enums only (no paths/exception text).
- Missing stores may be creatable (parent safe) but probe never creates them.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import ipaddress

from . import project_registry_contracts
from . import runtime_paths
from . import team_message_routing
from .artifact_root import resolve_artifact_root

COMPAT_FAMILY = "0.3.x"

# Stable machine-readable reasons (ready response only).
REASON_COMPATIBLE = "compatible"
REASON_MISSING_CREATABLE = "missing_creatable"
REASON_MISSING_BLOCKED = "missing_blocked"
REASON_MIGRATION_REQUIRED = "migration_required"
REASON_FUTURE_SCHEMA = "future_schema"
REASON_CORRUPT = "corrupt"
REASON_FINGERPRINT_MISMATCH = "fingerprint_mismatch"
REASON_UNSAFE = "unsafe"
REASON_PROBE_REQUIRES_QUIESCENCE = "probe_requires_quiescence"
REASON_UNREADABLE = "unreadable"
REASON_NOT_APPLICABLE = "not_applicable"
REASON_PRODUCTION_MANIFEST_MISSING = "production_manifest_missing"
REASON_PATHS_NOT_READY = "paths_not_ready"
REASON_IDENTITY_ERROR = "identity_error"
REASON_INVALID_JSON = "invalid_json"
REASON_UNKNOWN_FIELDS = "unknown_fields"
REASON_UNSAFE_ROOT = "unsafe_root"

# Frozen SQLite fingerprints: (table → ordered (name, type, notnull, pk)).
# Captured from current CREATE TABLE / migrations in this tree.
_TASKS_COLUMNS: tuple[tuple[str, str, int, int], ...] = (
    ("id", "TEXT", 0, 1),
    ("workdir", "TEXT", 1, 0),
    ("prompt", "TEXT", 1, 0),
    ("images", "TEXT", 0, 0),
    ("model", "TEXT", 0, 0),
    ("status", "TEXT", 1, 0),
    ("pid", "INTEGER", 0, 0),
    ("exit_code", "INTEGER", 0, 0),
    ("created_ts", "REAL", 1, 0),
    ("started_ts", "REAL", 0, 0),
    ("finished_ts", "REAL", 0, 0),
    ("output_tail", "TEXT", 0, 0),
    ("source_workdir", "TEXT", 0, 0),
    ("base_sha", "TEXT", 0, 0),
    ("run_workdir", "TEXT", 0, 0),
    ("preview_hash", "TEXT", 0, 0),
)
_PUSH_COLUMNS: tuple[tuple[str, str, int, int], ...] = (
    ("endpoint", "TEXT", 0, 1),
    ("payload", "TEXT", 1, 0),
    ("created_ts", "REAL", 1, 0),
)
_DELIVERY_OUTBOX_COLUMNS: tuple[tuple[str, str, int, int], ...] = (
    ("job_id", "TEXT", 0, 1),
    ("idempotency_key", "TEXT", 1, 0),
    ("job_kind", "TEXT", 1, 0),
    ("target", "TEXT", 1, 0),
    ("payload_json", "TEXT", 1, 0),
    ("payload_digest", "TEXT", 1, 0),
    ("attempt", "INTEGER", 1, 0),
    ("next_attempt_at", "REAL", 1, 0),
    ("status", "TEXT", 1, 0),
    ("created_ts", "REAL", 1, 0),
    ("updated_ts", "REAL", 1, 0),
    ("last_error_summary", "TEXT", 0, 0),
)
_DELIVERY_OUTBOX_DEFAULTS: dict[str, dict[str, str]] = {
    "delivery_jobs": {"attempt": "0", "status": "pending"},
}
_IndexFingerprint = tuple[str, int, int, tuple[str, ...]]
_ForeignKeyColumn = tuple[str, str, str]
_ForeignKeyFingerprint = tuple[_ForeignKeyColumn, ...]


_DELIVERY_OUTBOX_INDEXES: dict[str, frozenset[_IndexFingerprint]] = {
    "delivery_jobs": frozenset(
        {("delivery_jobs_idempotency", 1, 0, ("idempotency_key",))}
    ),
}


_DELIVERY_OUTBOX_FKS: dict[str, frozenset[_ForeignKeyFingerprint]] = {
    "delivery_jobs": frozenset(),
}
_COORD_TABLES: dict[str, tuple[tuple[str, str, int, int], ...]] = {
    "runs": (
        ("run_id", "TEXT", 0, 1),
        ("project_key", "TEXT", 1, 0),
        ("session", "TEXT", 1, 0),
        ("session_dir", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("state", "TEXT", 1, 0),
        ("config_hash", "TEXT", 1, 0),
        ("started_ts", "REAL", 1, 0),
        ("closed_ts", "REAL", 0, 0),
    ),
    "participants": (
        ("run_id", "TEXT", 1, 1),
        ("participant_id", "TEXT", 1, 2),
        ("agent_type", "TEXT", 1, 0),
        ("mail_name", "TEXT", 0, 0),
        ("pane_id", "TEXT", 0, 0),
        ("role", "TEXT", 1, 0),
        ("task_text", "TEXT", 1, 0),
        ("task_revision", "INTEGER", 1, 0),
        ("workdir", "TEXT", 1, 0),
        ("state", "TEXT", 1, 0),
        ("updated_ts", "REAL", 1, 0),
    ),
    "message_meta": (
        ("project_key", "TEXT", 1, 1),
        ("message_id", "INTEGER", 1, 2),
        ("sender", "TEXT", 1, 0),
        ("meta_json", "TEXT", 1, 0),
        ("trusted_user", "INTEGER", 1, 0),
        ("created_ts", "REAL", 1, 0),
    ),
    "receipts": (
        ("project_key", "TEXT", 1, 1),
        ("recipient", "TEXT", 1, 2),
        ("message_id", "INTEGER", 1, 3),
        ("sender", "TEXT", 0, 0),
        ("run_id", "TEXT", 0, 0),
        ("task_id", "TEXT", 0, 0),
        ("task_revision", "INTEGER", 0, 0),
        ("intent", "TEXT", 1, 0),
        ("importance", "TEXT", 1, 0),
        ("state", "TEXT", 1, 0),
        ("claim_owner", "TEXT", 0, 0),
        ("claim_token", "TEXT", 0, 0),
        ("claim_expires_ts", "REAL", 0, 0),
        ("reason", "TEXT", 0, 0),
        ("checkpoint_json", "TEXT", 0, 0),
        ("ack_pending", "INTEGER", 1, 0),
        ("created_ts", "REAL", 1, 0),
        ("updated_ts", "REAL", 1, 0),
    ),
    "task_reports": (
        ("session", "TEXT", 1, 1),
        ("pane_id", "TEXT", 1, 2),
        ("agent_type", "TEXT", 1, 0),
        ("mail_name", "TEXT", 0, 0),
        ("request_id", "TEXT", 1, 0),
        ("requested_ts", "REAL", 1, 0),
        ("request_error", "TEXT", 0, 0),
        ("report_request_id", "TEXT", 0, 0),
        ("progress", "INTEGER", 0, 0),
        ("summary", "TEXT", 0, 0),
        ("next_step", "TEXT", 0, 0),
        ("blocker", "TEXT", 0, 0),
        ("reported_ts", "REAL", 0, 0),
    ),
    "assignments": (
        ("assignment_id", "TEXT", 0, 1),
        ("project_key", "TEXT", 1, 0),
        ("assignment", "TEXT", 1, 0),
        ("assignee", "TEXT", 1, 0),
        ("expected_reply", "TEXT", 0, 0),
        ("deadline", "REAL", 0, 0),
        ("status", "TEXT", 1, 0),
        ("closed_at", "REAL", 0, 0),
        ("version", "INTEGER", 1, 0),
        ("created_at", "REAL", 1, 0),
        ("updated_at", "REAL", 1, 0),
    ),
}
_LEADER_BINDING_TABLES: dict[str, tuple[tuple[str, str, int, int], ...]] = {
    "leader_bindings": (
        ("issuer", "TEXT", 1, 1),
        ("scope_kind", "TEXT", 1, 2),
        ("scope_id", "TEXT", 1, 3),
        ("mail_name", "TEXT", 1, 4),
        ("binding_id", "TEXT", 1, 0),
        ("previous_mail_name", "TEXT", 0, 0),
        ("previous_state", "TEXT", 0, 0),
        ("agent_name", "TEXT", 0, 0),
        ("agent_kind", "TEXT", 0, 0),
        ("session", "TEXT", 0, 0),
        ("pane_id", "TEXT", 0, 0),
        ("registry_selector", "TEXT", 0, 0),
        ("binding_version", "INTEGER", 1, 0),
        ("state", "TEXT", 1, 0),
        ("degraded_reason", "TEXT", 0, 0),
        ("updated_ts", "REAL", 1, 0),
        ("route_epoch", "INTEGER", 1, 0),
        ("migration_id", "TEXT", 0, 0),
        ("drain_revision", "INTEGER", 1, 0),
        ("drain_remaining", "INTEGER", 1, 0),
        ("drain_pending", "INTEGER", 1, 0),
        ("drain_claimed", "INTEGER", 1, 0),
        ("drain_ack_pending", "INTEGER", 1, 0),
    ),
    "binding_migrations": (
        ("migration_id", "TEXT", 0, 1),
        ("issuer", "TEXT", 1, 0),
        ("scope_kind", "TEXT", 1, 0),
        ("scope_id", "TEXT", 1, 0),
        ("from_binding_id", "TEXT", 0, 0),
        ("to_binding_id", "TEXT", 0, 0),
        ("route_epoch", "INTEGER", 1, 0),
        ("created_ts", "REAL", 1, 0),
    ),
    "control_events": (
        ("seq", "INTEGER", 0, 1),
        ("event_id", "TEXT", 1, 0),
        ("issuer", "TEXT", 1, 0),
        ("scope_kind", "TEXT", 1, 0),
        ("scope_id", "TEXT", 1, 0),
        ("event_type", "TEXT", 1, 0),
        ("binding_version", "INTEGER", 1, 0),
        ("migration_id", "TEXT", 0, 0),
        ("payload_json", "TEXT", 1, 0),
        ("created_ts", "REAL", 1, 0),
        ("fanned_out", "INTEGER", 1, 0),
    ),
}
_WORKSPACE_WORK_TABLES: dict[str, tuple[tuple[str, str, int, int], ...]] = {
    "schema_migrations": (
        ("migration_id", "TEXT", 1, 1),
        ("schema_version", "INTEGER", 1, 0),
        ("schema_digest", "TEXT", 1, 0),
        ("applied_at", "TEXT", 1, 0),
    ),
    "message_threads": (
        ("thread_id", "TEXT", 1, 1),
        ("project_id", "TEXT", 1, 0),
        ("workspace_id", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "messages": (
        ("message_id", "TEXT", 1, 1),
        ("thread_id", "TEXT", 1, 0),
        ("ordinal", "INTEGER", 1, 0),
        ("message_kind", "TEXT", 1, 0),
        ("author_kind", "TEXT", 1, 0),
        ("author_ref", "TEXT", 0, 0),
        ("author_generation", "INTEGER", 0, 0),
        ("reply_to_message_id", "TEXT", 0, 0),
        ("body", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "work_items": (
        ("work_item_id", "TEXT", 1, 1),
        ("source_message_id", "TEXT", 1, 0),
        ("status", "TEXT", 1, 0),
        ("acceptance", "TEXT", 0, 0),
        ("constraints", "TEXT", 0, 0),
        ("revision", "INTEGER", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "work_item_claims": (
        ("claim_id", "TEXT", 1, 1),
        ("work_item_id", "TEXT", 1, 0),
        ("identity_id", "TEXT", 1, 0),
        ("generation", "INTEGER", 1, 0),
        ("state", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "message_receipts": (
        ("receipt_id", "TEXT", 1, 1),
        ("project_id", "TEXT", 1, 0),
        ("workspace_id", "TEXT", 1, 0),
        ("work_item_id", "TEXT", 1, 0),
        ("claim_id", "TEXT", 0, 0),
        ("message_id", "TEXT", 0, 0),
        ("identity_id", "TEXT", 0, 0),
        ("generation", "INTEGER", 0, 0),
        ("kind", "TEXT", 1, 0),
        ("outcome", "TEXT", 1, 0),
        ("reason", "TEXT", 0, 0),
        ("evidence_digest", "TEXT", 0, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "idempotency_records": (
        ("project_id", "TEXT", 1, 1),
        ("workspace_id", "TEXT", 1, 2),
        ("command_scope", "TEXT", 1, 3),
        ("idempotency_key", "TEXT", 1, 4),
        ("request_digest", "TEXT", 1, 0),
        ("response_json", "TEXT", 1, 0),
    ),
}
_WORKSPACE_WORK_DEFAULTS: dict[str, dict[str, str]] = {
    "schema_migrations": {},
    "message_threads": {},
    "messages": {},
    "work_items": {},
    "work_item_claims": {},
    "message_receipts": {},
    "idempotency_records": {},
}
_WORKSPACE_WORK_INDEXES: dict[str, frozenset[_IndexFingerprint]] = {
    "schema_migrations": frozenset(),
    "message_threads": frozenset(),
    "messages": frozenset({
        ("messages_one_root", 1, 1, ("thread_id",)),
    }),
    "work_items": frozenset(),
    "work_item_claims": frozenset({
        ("work_item_claims_one_current", 1, 1, ("work_item_id",)),
    }),
    "message_receipts": frozenset(),
    "idempotency_records": frozenset(),
}
_WORKSPACE_WORK_FKS: dict[str, frozenset[_ForeignKeyFingerprint]] = {
    "schema_migrations": frozenset(),
    "message_threads": frozenset(),
    "messages": frozenset({
        (("thread_id", "message_threads", "thread_id"),),
        (("reply_to_message_id", "messages", "message_id"),),
    }),
    "work_items": frozenset({
        (("source_message_id", "messages", "message_id"),),
    }),
    "work_item_claims": frozenset({
        (("work_item_id", "work_items", "work_item_id"),),
    }),
    "message_receipts": frozenset({
        (("work_item_id", "work_items", "work_item_id"),),
        (("claim_id", "work_item_claims", "claim_id"),),
        (("message_id", "messages", "message_id"),),
    }),
    "idempotency_records": frozenset(),
}
_WORKSPACE_EXECUTION_TABLES: dict[str, tuple[tuple[str, str, int, int], ...]] = {
    "schema_migrations": (
        ("migration_id", "TEXT", 1, 1),
        ("schema_version", "INTEGER", 1, 0),
        ("schema_digest", "TEXT", 1, 0),
        ("applied_at", "TEXT", 1, 0),
    ),
    "agent_identities": (
        ("identity_id", "TEXT", 1, 1),
        ("project_id", "TEXT", 1, 0),
        ("workspace_id", "TEXT", 1, 0),
        ("display_name", "TEXT", 1, 0),
        ("role", "TEXT", 1, 0),
        ("lifecycle", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "work_item_preparations": (
        ("preparation_id", "TEXT", 1, 1),
        ("project_id", "TEXT", 1, 0),
        ("workspace_id", "TEXT", 1, 0),
        ("work_item_id", "TEXT", 1, 0),
        ("identity_id", "TEXT", 1, 0),
        ("generation", "INTEGER", 1, 0),
        ("checkout_id", "TEXT", 0, 0),
        ("lease_id", "TEXT", 0, 0),
        ("attachment_id", "TEXT", 0, 0),
        ("state", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "managed_checkouts": (
        ("checkout_id", "TEXT", 1, 1),
        ("preparation_id", "TEXT", 1, 0),
        ("source_head", "TEXT", 1, 0),
        ("source_tree", "TEXT", 1, 0),
        ("internal_path", "TEXT", 1, 0),
        ("ref_name", "TEXT", 0, 0),
        ("preflight", "TEXT", 1, 0),
        ("status", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "writer_leases": (
        ("lease_id", "TEXT", 1, 1),
        ("checkout_id", "TEXT", 1, 0),
        ("identity_id", "TEXT", 1, 0),
        ("generation", "INTEGER", 1, 0),
        ("status", "TEXT", 1, 0),
        ("fence_digest", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("claim_id", "TEXT", 0, 0),
        ("active_operation_id", "TEXT", 0, 0),
        ("active_operation_digest", "TEXT", 0, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "runtime_attachments": (
        ("attachment_id", "TEXT", 1, 1),
        ("identity_id", "TEXT", 1, 0),
        ("generation", "INTEGER", 1, 0),
        ("checkout_id", "TEXT", 1, 0),
        ("provider", "TEXT", 1, 0),
        ("harness", "TEXT", 1, 0),
        ("pane_id", "TEXT", 0, 0),
        ("instance_id", "TEXT", 0, 0),
        ("session_name", "TEXT", 0, 0),
        ("native_receipt", "TEXT", 0, 0),
        ("status", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "idempotency_records": (
        ("project_id", "TEXT", 1, 1),
        ("workspace_id", "TEXT", 1, 2),
        ("scope", "TEXT", 1, 3),
        ("idempotency_key", "TEXT", 1, 4),
        ("request_digest", "TEXT", 1, 0),
        ("response_json", "TEXT", 1, 0),
    ),
}
_WORKSPACE_EXECUTION_DEFAULTS: dict[str, dict[str, str]] = {
    name: {} for name in _WORKSPACE_EXECUTION_TABLES
}
_WORKSPACE_EXECUTION_INDEXES: dict[str, frozenset[_IndexFingerprint]] = {
    name: frozenset() for name in _WORKSPACE_EXECUTION_TABLES
}
_WORKSPACE_EXECUTION_FKS: dict[str, frozenset[_ForeignKeyFingerprint]] = {
    "schema_migrations": frozenset(),
    "agent_identities": frozenset(),
    "work_item_preparations": frozenset({
        (("identity_id", "agent_identities", "identity_id"),),
    }),
    "managed_checkouts": frozenset({
        (("preparation_id", "work_item_preparations", "preparation_id"),),
    }),
    "writer_leases": frozenset({
        (("identity_id", "agent_identities", "identity_id"),),
        (("checkout_id", "managed_checkouts", "checkout_id"),),
    }),
    "runtime_attachments": frozenset({
        (("identity_id", "agent_identities", "identity_id"),),
        (("checkout_id", "managed_checkouts", "checkout_id"),),
    }),
    "idempotency_records": frozenset(),
}

# Known previous user_version values (recognized but not ready).
_PREVIOUS_USER_VERSIONS: frozenset[int] = frozenset()

_SETTINGS_KEYS = frozenset({
    "language", "dir_agents", "enabled_agents", "upload_max_mb",
    "team_hub_url", "human_auth_url", "term",
})
_JSON_VERSIONED: dict[str, int] = {
    "mail_projects": 1,
    "team_messages": 1,
    "team_sessions": 1,
    "inbox_route": 8,
    "typing": 1,
}

# upgrade/ is V1 diagnostic — excluded from ready inventory.
_APP_OWNED_STORES = (
    "tasks", "push", "coordination", "delivery_outbox", "leader_binding",
    "project_registry", "workspace_work", "workspace_execution",
    "runtime_provider", "event_journal", "operation_journal",
    "project_memory", "terminal_ticket",
    "settings", "mail_projects", "chat_ledger", "team_messages",
    "team_sessions", "inbox_route", "typing",
    "file_roots", "vapid",
)
_ACCEPTED_SQLITE_STORES = (
    "runtime_provider", "event_journal", "operation_journal",
    "project_memory", "terminal_ticket", "chat_ledger",
)


def _store_result(
    name: str, state: str, reason: str, *, family: str = COMPAT_FAMILY,
) -> dict[str, Any]:
    return {
        "name": name,
        "compat_family": family,
        "state": state,
        "reason": reason,
    }


def _path_creatable(path: Path) -> bool:
    """Pure-read: can current uid create path under nearest existing ancestor?"""
    uid = os.getuid()
    cur = path if path.exists() else path.parent
    while True:
        try:
            if cur.exists():
                st = cur.stat()
                if not cur.is_dir():
                    return False
                if st.st_uid != uid and uid != 0:
                    return False
                if st.st_mode & 0o022:
                    return False
                return bool(os.access(cur, os.W_OK | os.X_OK))
        except OSError:
            return False
        if cur.parent == cur:
            return False
        cur = cur.parent


def _sqlite_wal_shm_state(db_path: Path) -> str | None:
    """Any live WAL or SHM sidecar forbids connect (OpenCode #2054).

    mode=ro + query_only still mutates -shm on first open of a live WAL DB.
    This slice has no controlled snapshot path → always probe_requires_quiescence
    when either sidecar exists. Only no-sidecar DBs may open with immutable=1.
    """
    wal = Path(str(db_path) + "-wal")
    shm = Path(str(db_path) + "-shm")
    try:
        if wal.exists() or shm.exists():
            return REASON_PROBE_REQUIRES_QUIESCENCE
    except OSError:
        return REASON_UNREADABLE
    return None


def _open_sqlite_ro(db_path: Path) -> sqlite3.Connection:
    """Open only after sidecar gate; immutable=1 keeps true zero-write."""
    uri = db_path.resolve().as_uri() + "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=1.0)
    con.execute("PRAGMA query_only=ON")
    return con


def _norm_dflt(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    # SQLite may quote string defaults
    if len(s) >= 2 and s[0] == s[-1] == "'":
        s = s[1:-1]
    return s


def _table_columns(con: sqlite3.Connection, table: str) -> dict[str, tuple[str, int, int, str | None]]:
    """name → (type, notnull, pk, dflt)."""
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    out: dict[str, tuple[str, int, int, str | None]] = {}
    for r in rows:
        out[str(r[1])] = (
            str(r[2] or "").upper(),
            int(r[3]),
            int(r[5]),
            _norm_dflt(r[4]),
        )
    return out


def _named_indexes(
    con: sqlite3.Connection, table: str,
) -> frozenset[_IndexFingerprint]:
    """Non-autoindex indexes: (name, unique, partial, ordered columns)."""
    items: list[_IndexFingerprint] = []
    for row in con.execute(f"PRAGMA index_list({table})").fetchall():
        # seq, name, unique, origin, partial
        name = str(row[1])
        if name.startswith("sqlite_autoindex_"):
            continue
        unique = int(row[2])
        partial = int(row[4])
        cols = tuple(
            str(c[2])
            for c in con.execute(f"PRAGMA index_info({name})").fetchall()
        )
        items.append((name, unique, partial, cols))
    return frozenset(items)


def _foreign_keys(
    con: sqlite3.Connection, table: str,
) -> frozenset[_ForeignKeyFingerprint]:
    """Group by PRAGMA id and order by seq to preserve composite identity."""
    groups: dict[int, list[tuple[int, _ForeignKeyColumn]]] = {}
    for row in con.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        # id, seq, table, from, to, on_update, on_delete, match
        groups.setdefault(int(row[0]), []).append((
            int(row[1]), (str(row[3]), str(row[2]), str(row[4])),
        ))
    return frozenset(
        tuple(column for _seq, column in sorted(group))
        for group in groups.values()
    )


def _schema_objects(
    con: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (str(kind), str(name), str(table), " ".join(str(sql).split()).lower())
        for kind, name, table, sql in con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    )


def _expected_schema_objects(
    statements: tuple[str, ...],
) -> tuple[tuple[str, str, str, str], ...]:
    con = sqlite3.connect(":memory:")
    try:
        con.execute("PRAGMA foreign_keys=ON")
        for statement in statements:
            con.execute(statement)
        return _schema_objects(con)
    finally:
        con.close()


# Expected defaults / indexes / FKs for current writers (user_version=0).
_TASKS_DEFAULTS = {"status": "pending"}
_PUSH_DEFAULTS: dict[str, str] = {}
_COORD_DEFAULTS: dict[str, dict[str, str]] = {
    "message_meta": {"trusted_user": "0"},
    "receipts": {"ack_pending": "0"},
    "assignments": {"status": "assigned", "version": "1"},
}
_COORD_INDEXES: dict[str, frozenset[_IndexFingerprint]] = {
    "runs": frozenset({("runs_project_state", 0, 0, ("project_key", "state"))}),
    "participants": frozenset({("participants_mail", 0, 0, ("mail_name", "run_id"))}),
    "message_meta": frozenset(),
    "receipts": frozenset({("receipts_claims", 0, 0, ("state", "claim_expires_ts"))}),
    "task_reports": frozenset(),
    "assignments": frozenset({
        ("assignments_project_status", 0, 0, ("project_key", "status", "deadline")),
        ("assignments_assignee_status", 0, 0, ("assignee", "status", "deadline")),
    }),
}
_COORD_FKS: dict[str, frozenset[_ForeignKeyFingerprint]] = {
    "runs": frozenset(),
    "participants": frozenset({
        (("run_id", "runs", "run_id"),),
    }),
    "message_meta": frozenset(),
    "receipts": frozenset(),
    "task_reports": frozenset(),
    "assignments": frozenset(),
}
_LEADER_BINDING_DEFAULTS: dict[str, dict[str, str]] = {
    "leader_bindings": {
        "route_epoch": "0", "drain_revision": "0",
        "drain_remaining": "0", "drain_pending": "0",
        "drain_claimed": "0", "drain_ack_pending": "0",
    },
    "binding_migrations": {},
    "control_events": {"fanned_out": "0"},
}
_LEADER_BINDING_INDEXES: dict[str, frozenset[_IndexFingerprint]] = {
    "leader_bindings": frozenset({
        ("leader_bindings_active_once", 1, 1, ("issuer", "scope_kind", "scope_id")),
        ("leader_bindings_scope", 0, 0, ("issuer", "scope_kind", "scope_id", "state")),
    }),
    "binding_migrations": frozenset(),
    "control_events": frozenset({
        ("control_events_scope", 0, 0, ("issuer", "scope_kind", "scope_id", "seq")),
        ("control_events_pending", 0, 0, ("fanned_out", "seq")),
    }),
}
_LEADER_BINDING_FKS = {
    name: frozenset() for name in _LEADER_BINDING_TABLES
}
_TASKS_INDEXES: frozenset[_IndexFingerprint] = frozenset()
_PUSH_INDEXES: frozenset[_IndexFingerprint] = frozenset()


def _check_sqlite(
    name: str,
    expected_tables: dict[str, tuple[tuple[str, str, int, int], ...]],
    *,
    expected_defaults: dict[str, dict[str, str]] | None = None,
    expected_indexes: dict[str, frozenset[_IndexFingerprint]] | None = None,
    expected_fks: dict[str, frozenset[_ForeignKeyFingerprint]] | None = None,
    expected_user_version: int = 0,
    expected_schema_statements: tuple[str, ...] | None = None,
    expected_migration: tuple[str, int, str] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    path = path if path is not None else runtime_paths.store(name)
    if not path.exists():
        if _path_creatable(path):
            return _store_result(name, "absent", REASON_MISSING_CREATABLE)
        return _store_result(name, "absent", REASON_MISSING_BLOCKED)
    try:
        st = path.stat()
    except OSError:
        return _store_result(name, "error", REASON_UNREADABLE)
    if not path.is_file():
        return _store_result(name, "error", REASON_UNSAFE)
    if st.st_uid != os.getuid() and os.getuid() != 0:
        return _store_result(name, "error", REASON_UNSAFE)
    if st.st_mode & 0o022:
        return _store_result(name, "error", REASON_UNSAFE)

    quiet = _sqlite_wal_shm_state(path)
    if quiet:
        # Do not open the DB at all (would mutate -shm under mode=ro).
        return _store_result(name, "blocked", quiet)

    try:
        con = _open_sqlite_ro(path)
    except sqlite3.Error:
        return _store_result(name, "error", REASON_CORRUPT)
    try:
        try:
            user_version = int(con.execute("PRAGMA user_version").fetchone()[0])
        except (TypeError, ValueError, sqlite3.Error):
            return _store_result(name, "error", REASON_CORRUPT)
        if user_version < 0:
            return _store_result(name, "error", REASON_CORRUPT)
        # Most current writers freeze user_version=0; versioned stores pass
        # their explicit current version. Known previous values remain a
        # migration requirement and higher values fail closed as future.
        if user_version in _PREVIOUS_USER_VERSIONS:
            return _store_result(name, "migration_required", REASON_MIGRATION_REQUIRED)
        if user_version < expected_user_version:
            return _store_result(name, "migration_required", REASON_MIGRATION_REQUIRED)
        if user_version > expected_user_version:
            return _store_result(name, "future", REASON_FUTURE_SCHEMA)
        if (
            expected_schema_statements is not None
            and _schema_objects(con)
            != _expected_schema_objects(expected_schema_statements)
        ):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        if expected_migration is not None:
            try:
                receipts = con.execute(
                    "SELECT migration_id, schema_version, schema_digest "
                    "FROM schema_migrations"
                ).fetchall()
            except sqlite3.Error:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            current_receipts = [
                tuple(row) for row in receipts
                if row[0] == expected_migration[0]
            ]
            if current_receipts != [expected_migration]:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        try:
            check = con.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                return _store_result(name, "error", REASON_CORRUPT)
        except sqlite3.Error:
            return _store_result(name, "error", REASON_CORRUPT)
        # An exact sqlite_master fingerprint is stronger than the duplicated
        # table/index/FK maps below and follows the versioned stores' own
        # open_existing contract.  Do not let those secondary maps drift and
        # reject a valid append-only migration history.
        if expected_schema_statements is not None:
            return _store_result(name, "compatible", REASON_COMPATIBLE)
        # Discover tables (exclude sqlite_*)
        names = {
            str(r[0])
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        expected_names = set(expected_tables)
        if not expected_names.issubset(names):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        extras = names - expected_names
        if extras:
            return _store_result(name, "future", REASON_FUTURE_SCHEMA)
        exp_defaults = expected_defaults or {}
        exp_indexes = expected_indexes or {}
        exp_fks = expected_fks or {}
        for table, expected_cols in expected_tables.items():
            actual = _table_columns(con, table)
            exp_map = {
                n: (t.upper(), nn, pk)
                for n, t, nn, pk in expected_cols
            }
            if set(exp_map) - set(actual):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if set(actual) - set(exp_map):
                return _store_result(name, "future", REASON_FUTURE_SCHEMA)
            for col, exp in exp_map.items():
                typ, nn, pk = exp
                atyp, ann, apk, adflt = actual[col]
                if (atyp, ann, apk) != (typ, nn, pk):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
                want_dflt = exp_defaults.get(table, {}).get(col)
                if want_dflt is not None and adflt != want_dflt:
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
                if want_dflt is None and adflt is not None and col not in exp_defaults.get(table, {}):
                    # Unexpected default on column that should have none → future/mismatch
                    # Only enforce when we listed defaults for the table
                    if table in exp_defaults:
                        return _store_result(name, "future", REASON_FUTURE_SCHEMA)
            if table in exp_indexes and _named_indexes(con, table) != exp_indexes[table]:
                # Extra named index → future; missing → mismatch
                act_idx = _named_indexes(con, table)
                if exp_indexes[table].issubset(act_idx) and act_idx - exp_indexes[table]:
                    return _store_result(name, "future", REASON_FUTURE_SCHEMA)
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if table in exp_fks and _foreign_keys(con, table) != exp_fks[table]:
                act_fk = _foreign_keys(con, table)
                if exp_fks[table].issubset(act_fk) and act_fk - exp_fks[table]:
                    return _store_result(name, "future", REASON_FUTURE_SCHEMA)
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        return _store_result(name, "compatible", REASON_COMPATIBLE)
    finally:
        con.close()


def _accepted_sqlite_opener(name: str):
    if name == "runtime_provider":
        from . import runtime_provider_store as module
        return module.open_existing, module.RuntimeProviderStoreError
    if name == "event_journal":
        from . import event_store as module
        return module.open_existing, module.EventStoreError
    if name == "operation_journal":
        from . import operation_store as module
        return module.open_existing, module.OperationError
    if name == "project_memory":
        from . import memory_store as module
        return module.open_existing, module.MemoryStoreError
    if name == "terminal_ticket":
        from . import terminal_ticket_store as module
        return module.open_existing, module.TerminalTicketError
    if name == "chat_ledger":
        from . import chat_ledger as module
        return module.open_existing, module.ChatLedgerError
    raise KeyError(name)


def _check_accepted_sqlite(
    name: str, path: Path | None = None,
) -> dict[str, Any]:
    path = path if path is not None else runtime_paths.store(name)
    if not path.exists():
        reason = REASON_MISSING_CREATABLE if _path_creatable(path) else REASON_MISSING_BLOCKED
        return _store_result(name, "absent", reason)
    try:
        info = path.stat()
    except OSError:
        return _store_result(name, "error", REASON_UNREADABLE)
    if (
        not path.is_file()
        or (info.st_uid != os.getuid() and os.getuid() != 0)
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        return _store_result(name, "error", REASON_UNSAFE)
    quiet = _sqlite_wal_shm_state(path)
    if quiet:
        return _store_result(name, "blocked", quiet)
    try:
        opener, error_type = _accepted_sqlite_opener(name)
    except Exception:
        return _store_result(name, "error", REASON_UNREADABLE)
    try:
        opened = opener(path)
        opened.close()
    except error_type as exc:
        code = getattr(exc, "code", "")
        state, reason = {
            "migration_required": ("migration_required", REASON_MIGRATION_REQUIRED),
            "future_schema": ("future", REASON_FUTURE_SCHEMA),
            "schema_fingerprint_mismatch": ("error", REASON_FINGERPRINT_MISMATCH),
            "schema_mismatch": ("error", REASON_FINGERPRINT_MISMATCH),
            "schema_missing": ("error", REASON_FINGERPRINT_MISMATCH),
            "store_corrupt": ("error", REASON_CORRUPT),
            "store_unsafe": ("error", REASON_UNSAFE),
            "store_read_failed": ("error", REASON_UNREADABLE),
        }.get(code, ("error", REASON_UNREADABLE))
        return _store_result(name, state, reason)
    except Exception:
        return _store_result(name, "error", REASON_UNREADABLE)
    return _store_result(name, "compatible", REASON_COMPATIBLE)


def _read_json_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


# Known agents (mirror settings.KNOWN_AGENTS without importing settings writer).
_KNOWN_AGENTS = frozenset({
    "codex", "kimi", "claude", "qodercli", "grok", "opencode",
})
_LANGUAGES = frozenset({"zh", "en", "ja"})
_PRIVATE_HTTP_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_PRIVATE_HTTP_V6 = ipaddress.ip_network("fc00::/7")


def _is_finite_number(value: Any) -> bool:
    if type(value) is bool:
        return False
    if type(value) is int:
        return True
    if type(value) is float:
        return math.isfinite(value)
    return False


def _as_int(value: Any) -> int | None:
    """Strict int conversion; never raises OverflowError/TypeError to caller."""
    if type(value) is bool:
        return None
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value) or not value.is_integer():
            return None
        if abs(value) > 2**53:
            return None
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    if type(value) is bool:
        return None
    if type(value) is int:
        return float(value)
    if type(value) is float:
        return value if math.isfinite(value) else None
    return None


def _service_url_ok(value: Any, *, allow_empty: bool) -> bool:
    """Mirror settings.normalize_service_url contract without raising."""
    if not isinstance(value, str):
        return False
    endpoint = value.strip().rstrip("/")
    if not endpoint:
        return bool(allow_empty)
    if any(ch.isspace() for ch in endpoint):
        return False
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        return False
    host = parsed.hostname or ""
    try:
        address = ipaddress.ip_address(host)
        private_http = address.is_loopback or (
            address.version == 4
            and any(address in network for network in _PRIVATE_HTTP_V4)
        ) or (
            address.version == 6 and address in _PRIVATE_HTTP_V6
        )
    except ValueError:
        private_http = host.lower() == "localhost"
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
        or (parsed.scheme == "http" and not private_http)
    ):
        return False
    return True


def _check_settings(path: Path | None = None) -> dict[str, Any]:
    """Sparse settings: only validate keys that appear (writer sparse store).

    Unknown keys → unknown_fields. Present keys: type/range equivalent to
    settings._validate, without calling the writer module. Never raises.
    """
    name = "settings"
    path = path if path is not None else runtime_paths.store(name)
    if not path.exists():
        reason = REASON_MISSING_CREATABLE if _path_creatable(path) else REASON_MISSING_BLOCKED
        return _store_result(name, "absent", reason)
    try:
        data = _read_json_file(path)
    except (OSError, UnicodeError):
        return _store_result(name, "error", REASON_UNREADABLE)
    except (ValueError, TypeError):
        return _store_result(name, "error", REASON_INVALID_JSON)
    if not isinstance(data, dict):
        return _store_result(name, "error", REASON_INVALID_JSON)
    try:
        keys = set(data)
        if keys - _SETTINGS_KEYS:
            return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
        if "language" in data:
            lang = data["language"]
            if not isinstance(lang, str) or lang not in _LANGUAGES:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        if "dir_agents" in data:
            da = data["dir_agents"]
            if not isinstance(da, dict):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            for d, a in da.items():
                if not isinstance(d, str) or not isinstance(a, str):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
                if not Path(d).is_absolute() or a not in _KNOWN_AGENTS:
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        if "enabled_agents" in data:
            agents = data["enabled_agents"]
            if not isinstance(agents, list) or not agents:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if any(not isinstance(a, str) or a not in _KNOWN_AGENTS for a in agents):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        if "upload_max_mb" in data:
            mb = _as_int(data["upload_max_mb"])
            if mb is None or mb < 1 or mb > 2048:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        if "team_hub_url" in data:
            if not _service_url_ok(data["team_hub_url"], allow_empty=True):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        if "human_auth_url" in data:
            if not _service_url_ok(data["human_auth_url"], allow_empty=True):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        if "term" in data:
            term = data["term"]
            if not isinstance(term, dict):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            allowed_term = {"max_terms", "idle_ttl", "write_timeout"}
            if set(term) - allowed_term:
                return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
            if "max_terms" in term:
                v = _as_int(term["max_terms"])
                if v is None or v < 1 or v > 64:
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "idle_ttl" in term:
                v = _as_float(term["idle_ttl"])
                if v is None or v < 60.0 or v > 86400.0:
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "write_timeout" in term:
                v = _as_float(term["write_timeout"])
                if v is None or v < 0.2 or v > 30.0:
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        return _store_result(name, "compatible", REASON_COMPATIBLE)
    except Exception:
        # Ready must never 500 on malformed settings payloads.
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)


# Exact top-level shapes for versioned JSON (0.3.x).
_JSON_SHAPES: dict[str, frozenset[str]] = {
    "mail_projects": frozenset({"version", "sessions"}),
    "team_messages": frozenset({"version", "messages"}),
    "team_sessions": frozenset({"version", "bindings"}),
    "inbox_route": frozenset({
        "version", "work_items", "consult_requests", "reply_evidence",
    }),
}
_JSON_OPTIONAL_SHAPES: dict[str, frozenset[str]] = {
    "team_sessions": frozenset({"managed_sessions"}),
}


def _check_versioned_json(
    name: str,
    version_key: str = "version",
    path: Path | None = None,
) -> dict[str, Any]:
    expected_v = _JSON_VERSIONED[name]
    path = path if path is not None else runtime_paths.store(name)
    if not path.exists():
        reason = REASON_MISSING_CREATABLE if _path_creatable(path) else REASON_MISSING_BLOCKED
        return _store_result(name, "absent", reason)
    try:
        data = _read_json_file(path)
    except (OSError, UnicodeError):
        return _store_result(name, "error", REASON_UNREADABLE)
    except (ValueError, TypeError):
        return _store_result(name, "error", REASON_INVALID_JSON)
    if not isinstance(data, dict):
        return _store_result(name, "error", REASON_INVALID_JSON)
    ver = data.get(version_key)
    # bool is a subclass of int — require exact int type.
    if type(ver) is not int:
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    if ver > expected_v:
        return _store_result(name, "future", REASON_FUTURE_SCHEMA)
    if ver < expected_v:
        return _store_result(name, "migration_required", REASON_MIGRATION_REQUIRED)
    allowed = _JSON_SHAPES[name]
    optional = _JSON_OPTIONAL_SHAPES.get(name, frozenset())
    keys = set(data)
    if not allowed.issubset(keys):
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    if keys - allowed - optional:
        return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
    # Core members required + exact member shapes (writer-aligned 0.3.x).
    if name == "mail_projects":
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        entry_keys = frozenset({"project", "session_dir"})
        for key, entry in sessions.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            ek = set(entry)
            if not entry_keys.issubset(ek):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if ek - entry_keys:
                return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
            if not isinstance(entry.get("project"), str):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if not isinstance(entry.get("session_dir"), str):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    elif name == "team_messages":
        rows = data.get("messages")
        if not isinstance(rows, list):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        required = frozenset({
            "id", "topic", "hub", "kind", "sender", "text", "to", "ts",
        })
        optional = frozenset({"handed_to_leader"})
        for entry in rows:
            if not isinstance(entry, dict):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            keys = set(entry)
            if not required <= keys:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            extra = keys - required - optional
            if extra:
                return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
            if (
                not isinstance(entry["id"], str)
                or re.fullmatch(r"tmsg_[0-9a-f]{12}", entry["id"]) is None
                or not isinstance(entry["topic"], str)
                or re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", entry["topic"],
                ) is None
                or not isinstance(entry["hub"], str)
                or not entry["hub"]
                or entry["kind"] not in {"me", "agent", "event", "error"}
                or not isinstance(entry["sender"], str)
                or not entry["sender"]
                or not isinstance(entry["text"], str)
                or not entry["text"]
                or not isinstance(entry["to"], list)
                or any(not isinstance(item, str) or not item for item in entry["to"])
                or type(entry["ts"]) is not int
                or entry["ts"] < 0
            ):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "handed_to_leader" in entry and type(entry["handed_to_leader"]) is not bool:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    elif name == "team_sessions":
        managed_sessions = data.get("managed_sessions", [])
        if (
            not isinstance(managed_sessions, list)
            or any(
                not isinstance(session, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", session) is None
                for session in managed_sessions
            )
            or len(set(managed_sessions)) != len(managed_sessions)
        ):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        bindings = data.get("bindings")
        if not isinstance(bindings, list):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        required_b = frozenset({
            "hub", "human_id", "project_slug", "session", "session_generation",
            "session_dir", "mail_project", "lead", "client_session_id",
            "agent_id", "updated_ts",
        })
        optional_b = frozenset({
            "reply_token", "reply_mode", "auth_expires_at", "managed_runtime",
            "consult_target",
        })
        lead_keys = frozenset({"pane_id", "agent", "mail_name", "participant_id"})
        for item in bindings:
            if not isinstance(item, dict):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            ik = set(item)
            if not required_b.issubset(ik):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if ik - required_b - optional_b:
                return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
            if not isinstance(item.get("hub"), str):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if type(item.get("human_id")) is not int:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if type(item.get("agent_id")) is not int:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if not isinstance(item.get("updated_ts"), (int, float)) or type(item.get("updated_ts")) is bool:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            for sk in (
                "project_slug", "session", "session_generation",
                "session_dir", "mail_project", "client_session_id",
            ):
                if not isinstance(item.get(sk), str):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            lead = item.get("lead")
            if not isinstance(lead, dict):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if set(lead) - lead_keys:
                return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
            if not lead_keys.issubset(set(lead)):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if any(not isinstance(lead.get(k), str) for k in lead_keys):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "reply_token" in item:
                rt = item["reply_token"]
                if not isinstance(rt, str) or not rt or len(rt) > 128:
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "reply_mode" in item and item["reply_mode"] not in {"confirm", "auto"}:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "auth_expires_at" in item and (
                not isinstance(item["auth_expires_at"], (int, float))
                or type(item["auth_expires_at"]) is bool
                or item["auth_expires_at"] < 0
            ):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "managed_runtime" in item and type(item["managed_runtime"]) is not bool:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "consult_target" in item:
                target = item["consult_target"]
                target_keys = frozenset({
                    "session", "session_generation", "mail_project", "lead", "updated_ts",
                })
                target_lead_keys = frozenset({"agent", "mail_name", "participant_id"})
                if not isinstance(target, dict) or set(target) != target_keys:
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
                if any(
                    not isinstance(target.get(key), str) or not target.get(key)
                    for key in ("session", "session_generation", "mail_project")
                ):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
                target_lead = target.get("lead")
                if (
                    not isinstance(target_lead, dict)
                    or set(target_lead) != target_lead_keys
                    or any(not isinstance(target_lead.get(key), str) for key in target_lead_keys)
                    or not target_lead.get("mail_name")
                    or type(target.get("updated_ts")) is bool
                    or not isinstance(target.get("updated_ts"), (int, float))
                ):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    elif name == "inbox_route":
        work_items = data.get("work_items")
        consult_requests = data.get("consult_requests")
        reply_evidence = data.get("reply_evidence")
        if (
            not isinstance(work_items, list)
            or not isinstance(consult_requests, list)
            or not isinstance(reply_evidence, list)
        ):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        required = frozenset({
            "work_id", "hub", "project_slug", "client_session_id", "session",
            "session_generation", "mail_project", "lead_mail_name", "pane_id",
            "reply_mode", "inbox_item_id", "claim_token", "claim_expires_at",
            "message", "state", "notified", "created_ts",
        })
        optional = frozenset({
            "response", "context_pack", "routing", "progress_phase",
            "progress_summary", "progress_sequence", "progress_baseline_summary",
        })
        message_keys = frozenset({
            "message_id", "subject", "body_md", "importance", "sender_name",
            "sender_handle", "created_ts", "attachments",
        })
        response_keys = frozenset({
            "subject", "body_md", "importance", "mention_handles",
        })
        for item in work_items:
            if not isinstance(item, dict):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            keys = set(item)
            if not required.issubset(keys):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if keys - required - optional:
                return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
            for key in (
                "work_id", "hub", "project_slug", "client_session_id", "session",
                "session_generation", "mail_project", "lead_mail_name", "pane_id",
                "claim_token", "claim_expires_at",
            ):
                if not isinstance(item.get(key), str) or not item.get(key):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if item.get("reply_mode") not in {"confirm", "auto"}:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if item.get("state") not in {"pending", "responding"}:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if type(item.get("inbox_item_id")) is not int or item["inbox_item_id"] < 1:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if type(item.get("notified")) is not bool:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            created = item.get("created_ts")
            if (
                type(created) is bool or not isinstance(created, (int, float))
                or not math.isfinite(float(created))
            ):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            message = item.get("message")
            if not isinstance(message, dict) or set(message) != message_keys:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            for key in ("subject", "body_md", "importance", "sender_name", "created_ts"):
                if not isinstance(message.get(key), str):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if message.get("sender_handle") is not None and not isinstance(message["sender_handle"], str):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if type(message.get("message_id")) is not int:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            attachments = message.get("attachments")
            if not isinstance(attachments, list) or len(attachments) > 4:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            attachment_keys = frozenset({
                "id", "filename", "media_type", "size", "sha256",
            })
            for attachment in attachments:
                if not isinstance(attachment, dict) or set(attachment) != attachment_keys:
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
                if (
                    not isinstance(attachment.get("id"), str)
                    or re.fullmatch(r"[0-9a-f]{32}", attachment["id"]) is None
                    or not isinstance(attachment.get("filename"), str)
                    or not attachment["filename"]
                    or not isinstance(attachment.get("media_type"), str)
                    or type(attachment.get("size")) is not int
                    or not 1 <= attachment["size"] <= 10 * 1024 * 1024
                    or not isinstance(attachment.get("sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", attachment["sha256"]) is None
                ):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            response = item.get("response")
            if response is not None:
                if not isinstance(response, dict) or set(response) != response_keys:
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
                if any(not isinstance(response.get(key), str) for key in ("subject", "body_md", "importance")):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
                handles = response.get("mention_handles")
                if not isinstance(handles, list) or any(not isinstance(value, str) for value in handles):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "context_pack" in item and not isinstance(item["context_pack"], dict):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "routing" in item:
                routing = item["routing"]
                if not team_message_routing.valid_routing(routing):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "progress_phase" in item and item["progress_phase"] not in {
                "working", "waiting", "blocked",
            }:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            for progress_key in ("progress_summary", "progress_baseline_summary"):
                if progress_key in item and not isinstance(item[progress_key], str):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "progress_sequence" in item and (
                type(item["progress_sequence"]) is not int
                or item["progress_sequence"] < 1
            ):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        consult_required = frozenset({
            "request_id", "work_id", "source_hub", "source_project_slug",
            "source_human_id", "source_client_session_id", "source_session",
            "source_session_generation", "source_mail_project", "source_lead_mail_name",
            "source_lead_agent", "target_session",
            "target_session_generation", "target_mail_project", "target_lead_mail_name",
            "target_lead_agent", "kind", "question", "state", "notified",
            "created_ts", "expires_ts",
        })
        consult_optional = frozenset({
            "response", "failure_reason", "answer_notified",
        })
        for item in consult_requests:
            if not isinstance(item, dict):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            keys = set(item)
            if not consult_required.issubset(keys):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if keys - consult_required - consult_optional:
                return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
            string_keys = consult_required - {
                "source_human_id", "notified", "created_ts", "expires_ts",
            }
            if any(not isinstance(item.get(key), str) or not item.get(key) for key in string_keys):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if type(item.get("source_human_id")) is not int:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if item.get("kind") not in {"status", "decision", "evidence", "blocker"}:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if item.get("state") not in {"pending", "claimed", "responded", "expired", "invalidated"}:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if type(item.get("notified")) is not bool:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if any(
                type(item.get(key)) is bool or not isinstance(item.get(key), (int, float))
                for key in ("created_ts", "expires_ts")
            ):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "response" in item and not isinstance(item["response"], str):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "answer_notified" in item and type(item["answer_notified"]) is not bool:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "failure_reason" in item and item["failure_reason"] not in {
                "source_identity_changed", "target_missing", "target_ambiguous",
                "target_identity_changed",
            }:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        evidence_keys = frozenset({
            "hub", "project_slug", "message_id", "work_id", "context_available",
            "context_fingerprint", "sha", "dirty", "handoff_updated",
            "consulted", "answer_source", "created_ts",
        })
        for item in reply_evidence:
            if not isinstance(item, dict) or set(item) != evidence_keys:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if any(
                not isinstance(item.get(key), str) or not item.get(key)
                for key in ("hub", "project_slug", "work_id")
            ):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if type(item.get("message_id")) is not int or item["message_id"] < 1:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if re.fullmatch(r"[0-9a-f]{32}", item["work_id"]) is None:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if type(item.get("context_available")) is not bool or type(item.get("consulted")) is not bool:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if item.get("answer_source") not in {
                "team_agent", "context_pack", "local_lead",
            }:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            fingerprint = item.get("context_fingerprint")
            if fingerprint is not None and (
                not isinstance(fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            ):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if item["context_available"] != (fingerprint is not None):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            sha = item.get("sha")
            if sha is not None and (
                not isinstance(sha, str)
                or re.fullmatch(r"[0-9a-f]{40,64}", sha) is None
            ):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if item.get("dirty") is not None and type(item["dirty"]) is not bool:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            handoff_updated = item.get("handoff_updated")
            if handoff_updated is not None and (
                not isinstance(handoff_updated, str)
                or re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\S{1,64})?", handoff_updated) is None
            ):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            created_ts = item.get("created_ts")
            if (
                type(created_ts) is bool
                or not isinstance(created_ts, (int, float))
                or not math.isfinite(float(created_ts))
            ):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    return _store_result(name, "compatible", REASON_COMPATIBLE)


def _check_file_roots(path: Path | None = None) -> dict[str, Any]:
    """Validate persisted custom roots with same policy as files reader (fail-closed)."""
    name = "file_roots"
    # Path must match production reader (files._custom_roots_file → runtime_paths.store).
    from . import files  # local module; no server import

    path = path if path is not None else files._custom_roots_file()  # noqa: SLF001
    if not path.exists():
        reason = REASON_MISSING_CREATABLE if _path_creatable(path) else REASON_MISSING_BLOCKED
        return _store_result(name, "absent", reason)
    try:
        files._read_custom_roots(path)  # noqa: SLF001 — intentional contract probe
    except files.CustomRootsError as exc:
        reason = getattr(exc, "reason", None)
        code = getattr(reason, "value", None) or "unreadable"
        if code in {"broad_root", "sensitive_root", "relative_path", "noncanonical_path"}:
            return _store_result(name, "error", REASON_UNSAFE_ROOT)
        if code in {"invalid_json", "invalid_shape", "invalid_entry_type", "too_many_entries"}:
            return _store_result(name, "error", REASON_INVALID_JSON)
        if code == "missing_path":
            return _store_result(name, "error", REASON_CORRUPT)
        return _store_result(name, "error", REASON_UNREADABLE)
    except Exception:
        return _store_result(name, "error", REASON_UNREADABLE)
    return _store_result(name, "compatible", REASON_COMPATIBLE)


def _check_vapid(path: Path | None = None) -> dict[str, Any]:
    """True EC P-256 PKCS8 parse (no generate; no marker-only accept)."""
    name = "vapid"
    path = path if path is not None else runtime_paths.store(name)
    if not path.exists():
        reason = REASON_MISSING_CREATABLE if _path_creatable(path) else REASON_MISSING_BLOCKED
        return _store_result(name, "absent", reason)
    if path.is_symlink():
        return _store_result(name, "error", REASON_UNSAFE)
    try:
        st = path.stat()
    except OSError:
        return _store_result(name, "error", REASON_UNREADABLE)
    if not path.is_file():
        return _store_result(name, "error", REASON_UNSAFE)
    if st.st_uid != os.getuid() and os.getuid() != 0:
        return _store_result(name, "error", REASON_UNSAFE)
    if (st.st_mode & 0o777) & ~0o600:
        return _store_result(name, "error", REASON_UNSAFE)
    if st.st_size <= 0:
        return _store_result(name, "error", REASON_CORRUPT)
    try:
        pem = path.read_bytes()
    except OSError:
        return _store_result(name, "error", REASON_UNREADABLE)
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        if not isinstance(key.curve, ec.SECP256R1):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    except Exception:
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    return _store_result(name, "compatible", REASON_COMPATIBLE)


def _check_typing(path: Path | None = None) -> dict[str, Any]:
    """typing.json: legacy session→float or session→{panes, unknown?} (terminal writer)."""
    name = "typing"
    path = path if path is not None else runtime_paths.store(name)
    if not path.exists():
        reason = REASON_MISSING_CREATABLE if _path_creatable(path) else REASON_MISSING_BLOCKED
        return _store_result(name, "absent", reason)
    try:
        data = _read_json_file(path)
    except (OSError, UnicodeError):
        return _store_result(name, "error", REASON_UNREADABLE)
    except (ValueError, TypeError):
        return _store_result(name, "error", REASON_INVALID_JSON)
    if not isinstance(data, dict):
        return _store_result(name, "error", REASON_INVALID_JSON)
    for key, value in data.items():
        if not isinstance(key, str):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        if isinstance(value, dict):
            allowed = {"panes", "unknown"}
            if set(value) - allowed:
                return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
            panes = value.get("panes", {})
            if "panes" in value and not isinstance(panes, dict):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if isinstance(panes, dict):
                for pk, pv in panes.items():
                    if not isinstance(pk, str):
                        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
                    if type(pv) is bool or not isinstance(pv, (int, float)):
                        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            if "unknown" in value:
                uv = value["unknown"]
                if type(uv) is bool or not isinstance(uv, (int, float)):
                    return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
            # must have panes or unknown
            if "panes" not in value and "unknown" not in value:
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        else:
            # legacy float timestamp
            if type(value) is bool or not isinstance(value, (int, float)):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    return _store_result(name, "compatible", REASON_COMPATIBLE)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_KEYS = frozenset({"version", "source_sha", "edition", "digests"})


def _file_sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _path_has_symlink_component(path: Path) -> bool:
    """True if any path component is a symlink (not only the leaf)."""
    try:
        if not path.is_absolute():
            path = path.resolve(strict=False)
        cur = Path(path.anchor)
        for part in path.parts[1:]:
            cur = cur / part
            if cur.is_symlink():
                return True
    except OSError:
        return True
    return False


def required_manifest_digest_paths(root: Path | None = None) -> tuple[str, ...]:
    """Real inventory of runtime served/loaded artifacts under package root.

    Includes VERSION + every regular file under static/ (fonts, vendor, sw,
    webmanifest, index). Tests must call this rather than hard-coding the set.
    """
    base = root if root is not None else resolve_artifact_root()
    paths: list[str] = []
    version = base / "VERSION"
    if version.is_file():
        paths.append("VERSION")
    static = base / "static"
    if static.is_dir():
        for p in sorted(static.rglob("*")):
            if not p.is_file():
                continue
            if _path_has_symlink_component(p):
                continue
            rel = str(p.relative_to(base)).replace("\\", "/")
            paths.append(rel)
    return tuple(paths)


def probe_manifest(
    edition: str,
    identity: dict[str, Any] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Artifact/release manifest gate (production fail-closed; source N/A).

    Production must bind identity version/source_sha/edition and verify
    static asset digests. Wrong/future/missing → not compatible.
    """
    if edition == "source":
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "not_applicable",
            "reason": REASON_NOT_APPLICABLE,
        }
    root = root if root is not None else resolve_artifact_root()
    manifest = root / "release-manifest.json"
    if not manifest.exists():
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "missing",
            "reason": REASON_PRODUCTION_MANIFEST_MISSING,
        }
    # Reject symlink for the manifest file itself.
    if manifest.is_symlink() or not manifest.is_file():
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "error",
            "reason": REASON_UNSAFE,
        }
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "error",
            "reason": REASON_CORRUPT,
        }
    if not isinstance(data, dict):
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "error",
            "reason": REASON_INVALID_JSON,
        }
    keys = set(data)
    if not _MANIFEST_KEYS.issubset(keys):
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "error",
            "reason": REASON_FINGERPRINT_MISMATCH,
        }
    if keys - _MANIFEST_KEYS:
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "future",
            "reason": REASON_FUTURE_SCHEMA,
        }
    man_version = data.get("version")
    man_sha = data.get("source_sha")
    man_edition = data.get("edition")
    digests = data.get("digests")
    if not isinstance(man_version, str) or not man_version:
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "error",
            "reason": REASON_FINGERPRINT_MISMATCH,
        }
    if not isinstance(man_sha, str) or not _GIT_SHA_RE.fullmatch(man_sha):
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "error",
            "reason": REASON_FINGERPRINT_MISMATCH,
        }
    if man_edition not in {"server", "desktop"}:
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "error",
            "reason": REASON_FINGERPRINT_MISMATCH,
        }
    if edition != man_edition:
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "error",
            "reason": REASON_FINGERPRINT_MISMATCH,
        }
    if identity is not None:
        if str(identity.get("version")) != man_version:
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_FINGERPRINT_MISMATCH,
            }
        if str(identity.get("source_sha")) != man_sha:
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_FINGERPRINT_MISMATCH,
            }
        if str(identity.get("edition")) != man_edition:
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_FINGERPRINT_MISMATCH,
            }
    if not isinstance(digests, dict) or not digests:
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "error",
            "reason": REASON_FINGERPRINT_MISMATCH,
        }
    static_root = root / "static"
    try:
        static_paths = (static_root, *static_root.rglob("*"))
        if any(path.is_symlink() for path in static_paths):
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_UNSAFE,
            }
    except OSError:
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "error",
            "reason": REASON_UNREADABLE,
        }
    required = set(required_manifest_digest_paths(root))
    if not required:
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "error",
            "reason": REASON_FINGERPRINT_MISMATCH,
        }
    digest_keys = set(digests)
    if not required.issubset(digest_keys):
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "error",
            "reason": REASON_FINGERPRINT_MISMATCH,
        }
    if digest_keys - required:
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "future",
            "reason": REASON_FUTURE_SCHEMA,
        }
    for rel, digest in digests.items():
        if not isinstance(rel, str) or not isinstance(digest, str):
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_FINGERPRINT_MISMATCH,
            }
        if not _SHA256_RE.fullmatch(digest):
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_FINGERPRINT_MISMATCH,
            }
        if ".." in rel.split("/") or rel.startswith("/"):
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_UNSAFE,
            }
        raw_target = root / rel
        if _path_has_symlink_component(raw_target) or raw_target.is_symlink():
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_UNSAFE,
            }
        try:
            target = raw_target.resolve(strict=True)
        except (OSError, RuntimeError, ValueError, FileNotFoundError):
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_FINGERPRINT_MISMATCH,
            }
        try:
            target.relative_to(root)
        except ValueError:
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_UNSAFE,
            }
        try:
            st = target.stat()
        except OSError:
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_UNREADABLE,
            }
        if not stat.S_ISREG(st.st_mode):
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_UNSAFE,
            }
        if _file_sha256(target) != digest:
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_FINGERPRINT_MISMATCH,
            }

    return {
        "name": "release_manifest",
        "compat_family": COMPAT_FAMILY,
        "state": "compatible",
        "reason": REASON_COMPATIBLE,
    }


def _workspace_execution_sqlite_spec() -> tuple[
    str,
    dict[str, tuple[tuple[str, str, int, int], ...]],
    dict[str, dict[str, str]],
    dict[str, frozenset[_IndexFingerprint]],
    dict[str, frozenset[_ForeignKeyFingerprint]],
    int,
    tuple[str, ...] | None,
    tuple[str, int, str] | None,
]:
    from . import workspace_execution_store as exec_store

    return (
        "workspace_execution",
        _WORKSPACE_EXECUTION_TABLES,
        _WORKSPACE_EXECUTION_DEFAULTS,
        _WORKSPACE_EXECUTION_INDEXES,
        _WORKSPACE_EXECUTION_FKS,
        exec_store.SCHEMA_VERSION,
        exec_store._SCHEMA,
        (
            exec_store.MIGRATION_ID,
            exec_store.SCHEMA_VERSION,
            exec_store.SCHEMA_DIGEST,
        ),
    )


def _workspace_work_sqlite_spec() -> tuple[
    str,
    dict[str, tuple[tuple[str, str, int, int], ...]],
    dict[str, dict[str, str]],
    dict[str, frozenset[_IndexFingerprint]],
    dict[str, frozenset[_ForeignKeyFingerprint]],
    int,
    tuple[str, ...] | None,
    tuple[str, int, str] | None,
]:
    from . import workspace_work_store as work_store

    return (
        "workspace_work",
        _WORKSPACE_WORK_TABLES,
        _WORKSPACE_WORK_DEFAULTS,
        _WORKSPACE_WORK_INDEXES,
        _WORKSPACE_WORK_FKS,
        work_store.SCHEMA_VERSION,
        work_store._SCHEMA,
        (
            work_store.MIGRATION_ID,
            work_store.SCHEMA_VERSION,
            work_store.SCHEMA_DIGEST,
        ),
    )


def _sqlite_probe_specs(
    *, include_leader_binding: bool,
) -> list[tuple[
    str,
    dict[str, tuple[tuple[str, str, int, int], ...]],
    dict[str, dict[str, str]],
    dict[str, frozenset[_IndexFingerprint]],
    dict[str, frozenset[_ForeignKeyFingerprint]],
    int,
    tuple[str, ...] | None,
    tuple[str, int, str] | None,
]]:
    specs = [
        (
            "tasks",
            {"tasks": _TASKS_COLUMNS},
            {"tasks": _TASKS_DEFAULTS},
            {"tasks": _TASKS_INDEXES},
            {"tasks": frozenset()},
            0, None, None,
        ),
        (
            "push",
            {"subscriptions": _PUSH_COLUMNS},
            {"subscriptions": _PUSH_DEFAULTS},
            {"subscriptions": _PUSH_INDEXES},
            {"subscriptions": frozenset()},
            0, None, None,
        ),
        (
            "coordination",
            _COORD_TABLES,
            _COORD_DEFAULTS,
            _COORD_INDEXES,
            _COORD_FKS,
            0, None, None,
        ),
        (
            "delivery_outbox",
            {"delivery_jobs": _DELIVERY_OUTBOX_COLUMNS},
            _DELIVERY_OUTBOX_DEFAULTS,
            _DELIVERY_OUTBOX_INDEXES,
            _DELIVERY_OUTBOX_FKS,
            0, None, None,
        ),
        (
            "project_registry",
            project_registry_contracts.PROJECT_REGISTRY_TABLES,
            project_registry_contracts.PROJECT_REGISTRY_DEFAULTS,
            project_registry_contracts.PROJECT_REGISTRY_INDEXES,
            project_registry_contracts.PROJECT_REGISTRY_FOREIGN_KEYS,
            project_registry_contracts.SCHEMA_VERSION,
            project_registry_contracts.SCHEMA_STATEMENTS,
            project_registry_contracts.PROJECT_REGISTRY_MIGRATION_RECEIPT,
        ),
        _workspace_work_sqlite_spec(),
        _workspace_execution_sqlite_spec(),
    ]
    if include_leader_binding:
        specs.append((
            "leader_binding",
            _LEADER_BINDING_TABLES,
            _LEADER_BINDING_DEFAULTS,
            _LEADER_BINDING_INDEXES,
            _LEADER_BINDING_FKS,
            0, None, None,
        ))
    return specs


def _snapshot_root_is_safe(root: Path) -> bool:
    if not root.is_absolute() or Path(os.path.abspath(root)) != root:
        return False
    if _path_has_symlink_component(root):
        return False
    try:
        info = root.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and (stat.S_IMODE(info.st_mode) & 0o077) == 0
    )


def _snapshot_file_gate(
    name: str,
    root: Path,
    path: Path,
) -> dict[str, Any] | None:
    """Validate a controller-owned backup file before parsing it."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return _store_result(name, "error", REASON_UNSAFE)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            parent_info = current.lstat()
        except FileNotFoundError:
            break
        except OSError:
            return _store_result(name, "error", REASON_UNREADABLE)
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.getuid()
            or (stat.S_IMODE(parent_info.st_mode) & 0o077) != 0
        ):
            return _store_result(name, "error", REASON_UNSAFE)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _store_result(name, "absent", REASON_MISSING_CREATABLE)
    except OSError:
        return _store_result(name, "error", REASON_UNREADABLE)
    if (
        _path_has_symlink_component(path)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        return _store_result(name, "error", REASON_UNSAFE)
    return None


def probe_snapshot_stores(snapshot_root: Path) -> list[dict[str, Any]]:
    """Probe a sealed backup tree without consulting active runtime stores.

    The controller supplies ``data/``, ``config/`` and ``state/`` below one
    user-owned root. Existing files must be regular, non-symlink and mode 0600.
    Missing named stores represent stores that were absent in the complete
    backup inventory and remain compatible with first boot.
    """
    root = Path(snapshot_root)
    if not _snapshot_root_is_safe(root):
        return [
            _store_result(name, "error", REASON_UNSAFE)
            for name in _APP_OWNED_STORES
        ]

    sqlite_specs = {
        name: (tables, defaults, indexes, fks, version, statements, migration)
        for (
            name, tables, defaults, indexes, fks, version, statements, migration
        ) in _sqlite_probe_specs(
            include_leader_binding=True,
        )
    }
    results: list[dict[str, Any]] = []
    for name in _APP_OWNED_STORES:
        root_name, rel = runtime_paths.STORES[name][0:2]
        path = root / root_name / rel
        blocked = _snapshot_file_gate(name, root, path)
        if blocked is not None:
            results.append(blocked)
            continue
        try:
            if name in sqlite_specs:
                (
                    tables, defaults, indexes, fks, version, statements, migration,
                ) = sqlite_specs[name]
                result = _check_sqlite(
                    name,
                    tables,
                    expected_defaults=defaults,
                    expected_indexes=indexes,
                    expected_fks=fks,
                    expected_user_version=version,
                    expected_schema_statements=statements,
                    expected_migration=migration,
                    path=path,
                )
            elif name in _ACCEPTED_SQLITE_STORES:
                result = _check_accepted_sqlite(name, path)
            elif name == "settings":
                result = _check_settings(path)
            elif name == "typing":
                result = _check_typing(path)
            elif name in _JSON_VERSIONED:
                result = _check_versioned_json(name, path=path)
            elif name == "file_roots":
                result = _check_file_roots(path)
            else:
                result = _check_vapid(path)
        except Exception:
            result = _store_result(name, "error", REASON_UNREADABLE)
        results.append(result)
    return results


def probe_all_stores() -> list[dict[str, Any]]:
    """Probe every app-owned store; pure-read, never creates files."""
    results: list[dict[str, Any]] = []
    # Path-level readiness first (from runtime_paths.inspect, strip absolute paths).
    try:
        inspection = runtime_paths.inspect()
    except Exception:
        inspection = {"ready": False, "stores": []}
    path_by_name = {
        str(item.get("name")): item
        for item in inspection.get("stores", [])
        if isinstance(item, dict)
    }

    def path_gate(name: str) -> dict[str, Any] | None:
        """Unified runtime_paths.inspect fail-closed for EVERY app-owned store.

        creatable (ready=True, reason=creatable) → allow content probe (may be
        missing_creatable). Any other not-ready reason blocks before content
        probe (OpenCode #2054: non-SQLite symlink_escape must not report
        compatible).
        """
        item = path_by_name.get(name)
        if not item:
            return _store_result(name, "error", REASON_PATHS_NOT_READY)
        reason = str(item.get("reason") or REASON_PATHS_NOT_READY)
        if item.get("ready") is True:
            return None
        if reason == "creatable":
            return None
        if "symlink" in reason or "owner" in reason or "mode" in reason:
            return _store_result(name, "error", REASON_UNSAFE)
        return _store_result(name, "error", REASON_PATHS_NOT_READY)

    def gated(name: str, probe):
        blocked = path_gate(name)
        if blocked is not None:
            results.append(blocked)
            return
        results.append(probe())

    # SQLite
    include_leader_binding = (
        (os.environ.get("COCKPIT_B0_MODE") or "off").strip().lower() != "off"
    )
    sqlite_specs = _sqlite_probe_specs(
        include_leader_binding=include_leader_binding,
    )
    for (
        name, tables, defaults, indexes, fks, version, statements, migration,
    ) in sqlite_specs:
        gated(
            name,
            lambda n=name, t=tables, d=defaults, i=indexes, f=fks,
            v=version, s=statements, m=migration: _check_sqlite(
                n, t,
                expected_defaults=d,
                expected_indexes=i,
                expected_fks=f,
                expected_user_version=v,
                expected_schema_statements=s,
                expected_migration=m,
            ),
        )

    for name in _ACCEPTED_SQLITE_STORES:
        gated(name, lambda n=name: _check_accepted_sqlite(n))

    gated("settings", _check_settings)
    for jname in (
        "mail_projects", "team_messages",
        "team_sessions", "inbox_route",
    ):
        gated(jname, lambda n=jname: _check_versioned_json(n))
    gated("typing", _check_typing)
    gated("file_roots", _check_file_roots)
    gated("vapid", _check_vapid)
    return results


def evaluate_ready(identity: dict[str, Any]) -> dict[str, Any]:
    """Build sanitized ready evaluation from identity + store probes.

    Does not raise; caller maps to HTTP 200/503.
    """
    edition = str(identity.get("edition") or "source")
    manifest = probe_manifest(edition, identity=identity)
    if edition == "server":
        from . import release_readiness

        items = [manifest, release_readiness.probe_server_evidence(identity)]
    else:
        items = list(probe_all_stores()) + [manifest]

    not_ready_reasons: list[str] = []
    for item in items:
        st = item.get("state")
        reason = str(item.get("reason") or "")
        if st in ("compatible", "absent") and reason in (
            REASON_COMPATIBLE, REASON_MISSING_CREATABLE, REASON_NOT_APPLICABLE,
        ):
            # absent+creatable is allowed for first boot ready
            continue
        if st == "not_applicable" and reason == REASON_NOT_APPLICABLE:
            continue
        not_ready_reasons.append(reason)

    ready = not not_ready_reasons
    return {
        "status": "ready" if ready else "not_ready",
        "compat_family": COMPAT_FAMILY,
        "identity": {
            "version": identity.get("version"),
            "source_sha": identity.get("source_sha"),
            "edition": identity.get("edition"),
            "instance_id": identity.get("instance_id"),
            # pid intentionally omitted from ready body (process detail)
        },
        "stores": [
            {
                "name": s["name"],
                "compat_family": s["compat_family"],
                "state": s["state"],
                "reason": s["reason"],
            }
            for s in items
        ],
        "ready": ready,
    }
