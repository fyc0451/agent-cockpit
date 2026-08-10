"""store_schema — pure-read app-owned store fingerprints for /health/ready (Wiki13 J1).

Contract (decisions/2026-08-10-agent-cockpit-j1-ready-schema-policy.md):
- No writer imports, no server import, no mkdir/DDL/WAL enable.
- SQLite: URI mode=ro + query_only; WAL without SHM → probe_requires_quiescence.
- Responses use stable reason enums only (no paths/exception text).
- Missing stores may be creatable (parent safe) but probe never creates them.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

import runtime_paths

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
}

# Known previous user_version values (recognized but not ready).
_PREVIOUS_USER_VERSIONS: frozenset[int] = frozenset()

_SETTINGS_KEYS = frozenset({
    "language", "dir_agents", "enabled_agents", "upload_max_mb",
    "team_hub_url", "human_auth_url", "term",
})
_JSON_VERSIONED: dict[str, int] = {
    "mail_projects": 1,
    "team_sessions": 1,
    "inbox_route": 2,
    "typing": 1,
}

# upgrade/ is V1 diagnostic — excluded from ready inventory.
_APP_OWNED_STORES = (
    "settings", "tasks", "coordination", "push", "vapid",
    "mail_projects", "team_sessions", "inbox_route", "typing", "file_roots",
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
    """Return probe_requires_quiescence if WAL exists without SHM."""
    wal = Path(str(db_path) + "-wal")
    shm = Path(str(db_path) + "-shm")
    try:
        if wal.exists() and not shm.exists():
            return REASON_PROBE_REQUIRES_QUIESCENCE
    except OSError:
        return REASON_UNREADABLE
    return None


def _open_sqlite_ro(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
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


def _named_indexes(con: sqlite3.Connection, table: str) -> frozenset[tuple[str, int, tuple[str, ...]]]:
    """Non-autoindex indexes: (name, unique, columns)."""
    items: list[tuple[str, int, tuple[str, ...]]] = []
    for row in con.execute(f"PRAGMA index_list({table})").fetchall():
        # seq, name, unique, origin, partial
        name = str(row[1])
        if name.startswith("sqlite_autoindex_"):
            continue
        unique = int(row[2])
        cols = tuple(
            str(c[2])
            for c in con.execute(f"PRAGMA index_info({name})").fetchall()
        )
        items.append((name, unique, cols))
    return frozenset(items)


def _foreign_keys(con: sqlite3.Connection, table: str) -> frozenset[tuple[str, str, str]]:
    """(from_col, ref_table, to_col)."""
    items: list[tuple[str, str, str]] = []
    for row in con.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        # id, seq, table, from, to, on_update, on_delete, match
        items.append((str(row[3]), str(row[2]), str(row[4])))
    return frozenset(items)


# Expected defaults / indexes / FKs for current writers (user_version=0).
_TASKS_DEFAULTS = {"status": "pending"}
_PUSH_DEFAULTS: dict[str, str] = {}
_COORD_DEFAULTS: dict[str, dict[str, str]] = {
    "message_meta": {"trusted_user": "0"},
    "receipts": {"ack_pending": "0"},
}
_COORD_INDEXES: dict[str, frozenset[tuple[str, int, tuple[str, ...]]]] = {
    "runs": frozenset({("runs_project_state", 0, ("project_key", "state"))}),
    "participants": frozenset({("participants_mail", 0, ("mail_name", "run_id"))}),
    "message_meta": frozenset(),
    "receipts": frozenset({("receipts_claims", 0, ("state", "claim_expires_ts"))}),
    "task_reports": frozenset(),
}
_COORD_FKS: dict[str, frozenset[tuple[str, str, str]]] = {
    "runs": frozenset(),
    "participants": frozenset({("run_id", "runs", "run_id")}),
    "message_meta": frozenset(),
    "receipts": frozenset(),
    "task_reports": frozenset(),
}
_TASKS_INDEXES: frozenset[tuple[str, int, tuple[str, ...]]] = frozenset()
_PUSH_INDEXES: frozenset[tuple[str, int, tuple[str, ...]]] = frozenset()


def _check_sqlite(
    name: str,
    expected_tables: dict[str, tuple[tuple[str, str, int, int], ...]],
    *,
    expected_defaults: dict[str, dict[str, str]] | None = None,
    expected_indexes: dict[str, frozenset[tuple[str, int, tuple[str, ...]]]] | None = None,
    expected_fks: dict[str, frozenset[tuple[str, str, str]]] | None = None,
) -> dict[str, Any]:
    path = runtime_paths.store(name)
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
        # Current writers freeze user_version=0. Known previous → migration_required;
        # any other non-zero is future_schema (never compatible).
        if user_version in _PREVIOUS_USER_VERSIONS:
            return _store_result(name, "migration_required", REASON_MIGRATION_REQUIRED)
        if user_version != 0:
            return _store_result(name, "future", REASON_FUTURE_SCHEMA)
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
        try:
            check = con.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                return _store_result(name, "error", REASON_CORRUPT)
        except sqlite3.Error:
            return _store_result(name, "error", REASON_CORRUPT)
        return _store_result(name, "compatible", REASON_COMPATIBLE)
    finally:
        con.close()


def _read_json_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def _check_settings() -> dict[str, Any]:
    name = "settings"
    path = runtime_paths.store(name)
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
    keys = set(data)
    if not _SETTINGS_KEYS.issubset(keys):
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    if keys - _SETTINGS_KEYS:
        return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
    # Exact types for 0.3.x
    if not isinstance(data.get("language"), str):
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    if not isinstance(data.get("dir_agents"), dict):
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    if not isinstance(data.get("enabled_agents"), list):
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    if not isinstance(data.get("upload_max_mb"), (int, float)):
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    if not isinstance(data.get("team_hub_url"), str):
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    if not isinstance(data.get("human_auth_url"), str):
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    term = data.get("term")
    if not isinstance(term, dict):
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    term_keys = set(term)
    allowed_term = {"max_terms", "idle_ttl", "write_timeout"}
    if not allowed_term.issubset(term_keys):
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    if term_keys - allowed_term:
        return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
    return _store_result(name, "compatible", REASON_COMPATIBLE)


# Exact top-level shapes for versioned JSON (0.3.x).
_JSON_SHAPES: dict[str, frozenset[str]] = {
    "mail_projects": frozenset({"version", "sessions"}),
    "team_sessions": frozenset({"version", "bindings"}),
    "inbox_route": frozenset({"version", "routes"}),
}


def _check_versioned_json(name: str, version_key: str = "version") -> dict[str, Any]:
    expected_v = _JSON_VERSIONED[name]
    path = runtime_paths.store(name)
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
    if not isinstance(ver, int):
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    if ver > expected_v:
        return _store_result(name, "future", REASON_FUTURE_SCHEMA)
    if ver < expected_v:
        return _store_result(name, "migration_required", REASON_MIGRATION_REQUIRED)
    allowed = _JSON_SHAPES[name]
    keys = set(data)
    if not allowed.issubset(keys):
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    if keys - allowed:
        return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
    # Value shapes
    if name == "mail_projects":
        if not isinstance(data.get("sessions"), dict):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    elif name == "team_sessions":
        if not isinstance(data.get("bindings"), list):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    elif name == "inbox_route":
        if not isinstance(data.get("routes"), dict):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    return _store_result(name, "compatible", REASON_COMPATIBLE)


def _check_file_roots() -> dict[str, Any]:
    """Validate persisted custom roots with same policy as files reader (fail-closed)."""
    name = "file_roots"
    # Path must match production reader (files._custom_roots_file → runtime_paths.store).
    import files  # local module; no server import

    path = files._custom_roots_file()  # noqa: SLF001
    if not path.exists():
        reason = REASON_MISSING_CREATABLE if _path_creatable(path) else REASON_MISSING_BLOCKED
        return _store_result(name, "absent", reason)
    try:
        files._read_custom_roots()  # noqa: SLF001 — intentional contract probe
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


def _check_vapid() -> dict[str, Any]:
    name = "vapid"
    path = runtime_paths.store(name)
    if not path.exists():
        reason = REASON_MISSING_CREATABLE if _path_creatable(path) else REASON_MISSING_BLOCKED
        return _store_result(name, "absent", reason)
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
    # Algorithm gate: PEM EC private key (VAPID). Reject non-PEM / wrong algo markers.
    try:
        head = path.read_bytes()[:256]
    except OSError:
        return _store_result(name, "error", REASON_UNREADABLE)
    if b"BEGIN" not in head or b"PRIVATE KEY" not in head:
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    if b"RSA PRIVATE KEY" in head:
        return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    return _store_result(name, "compatible", REASON_COMPATIBLE)


def _check_typing() -> dict[str, Any]:
    # typing is rebuildable but still app-owned (ADR §5). Exact 0.3.x shapes only.
    name = "typing"
    path = runtime_paths.store(name)
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
    if "version" in data:
        ver = data.get("version")
        if not isinstance(ver, int):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        if ver > 1:
            return _store_result(name, "future", REASON_FUTURE_SCHEMA)
        if ver < 1:
            return _store_result(name, "migration_required", REASON_MIGRATION_REQUIRED)
        allowed = {"version", "sessions"}
        keys = set(data)
        if not {"version"}.issubset(keys):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
        if keys - allowed:
            return _store_result(name, "future", REASON_UNKNOWN_FIELDS)
        if "sessions" in data and not isinstance(data["sessions"], dict):
            return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    else:
        # Bare session map: values must be dicts; no reserved version key elsewhere.
        for key, val in data.items():
            if not isinstance(key, str) or not isinstance(val, dict):
                return _store_result(name, "error", REASON_FINGERPRINT_MISMATCH)
    return _store_result(name, "compatible", REASON_COMPATIBLE)


_SHA256_RE = __import__("re").compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = __import__("re").compile(r"^[0-9a-f]{40}$")
_MANIFEST_KEYS = frozenset({"version", "source_sha", "edition", "digests"})
_REQUIRED_DIGEST_PATHS = ("static/index.html", "VERSION")


def _file_sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_manifest(
    edition: str,
    identity: dict[str, Any] | None = None,
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
    root = Path(__file__).resolve().parent
    manifest = root / "release-manifest.json"
    if not manifest.is_file():
        return {
            "name": "release_manifest",
            "compat_family": COMPAT_FAMILY,
            "state": "missing",
            "reason": REASON_PRODUCTION_MANIFEST_MISSING,
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
    for rel in _REQUIRED_DIGEST_PATHS:
        if rel not in digests:
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_FINGERPRINT_MISMATCH,
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
        # Reject path traversal in digest keys
        if ".." in rel.split("/") or rel.startswith("/"):
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_UNSAFE,
            }
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_UNSAFE,
            }
        if not target.is_file():
            return {
                "name": "release_manifest",
                "compat_family": COMPAT_FAMILY,
                "state": "error",
                "reason": REASON_FINGERPRINT_MISMATCH,
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
        item = path_by_name.get(name)
        if not item:
            return None
        if item.get("ready") is True:
            return None
        reason = str(item.get("reason") or REASON_PATHS_NOT_READY)
        # Map path reasons into ready enums (no path leakage).
        if reason == "creatable":
            return None  # content-level may still be missing_creatable
        if "symlink" in reason or "owner" in reason or "mode" in reason:
            return _store_result(name, "error", REASON_UNSAFE)
        return _store_result(name, "error", REASON_PATHS_NOT_READY)

    # SQLite
    sqlite_specs = (
        (
            "tasks",
            {"tasks": _TASKS_COLUMNS},
            {"tasks": _TASKS_DEFAULTS},
            {"tasks": _TASKS_INDEXES},
            {"tasks": frozenset()},
        ),
        (
            "push",
            {"subscriptions": _PUSH_COLUMNS},
            {"subscriptions": _PUSH_DEFAULTS},
            {"subscriptions": _PUSH_INDEXES},
            {"subscriptions": frozenset()},
        ),
        (
            "coordination",
            _COORD_TABLES,
            _COORD_DEFAULTS,
            _COORD_INDEXES,
            _COORD_FKS,
        ),
    )
    for name, tables, defaults, indexes, fks in sqlite_specs:
        blocked = path_gate(name)
        if blocked and blocked["reason"] != REASON_PATHS_NOT_READY:
            results.append(blocked)
            continue
        results.append(
            _check_sqlite(
                name, tables,
                expected_defaults=defaults,
                expected_indexes=indexes,
                expected_fks=fks,
            )
        )

    results.append(_check_settings())
    for jname in ("mail_projects", "team_sessions", "inbox_route"):
        results.append(_check_versioned_json(jname))
    results.append(_check_typing())
    results.append(_check_file_roots())
    results.append(_check_vapid())
    return results


def evaluate_ready(identity: dict[str, Any]) -> dict[str, Any]:
    """Build sanitized ready evaluation from identity + store probes.

    Does not raise; caller maps to HTTP 200/503.
    """
    edition = str(identity.get("edition") or "source")
    stores = probe_all_stores()
    manifest = probe_manifest(edition, identity=identity)
    items = list(stores) + [manifest]

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
