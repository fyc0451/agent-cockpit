"""delivery_outbox — dormant reliable-delivery outbox persistence (R1).

Independent SQLite store for outbound jobs that must survive process restarts
until a worker picks them up. This slice implements persistence + idempotency
only; it stays dormant (not imported by server/hub_client, no worker started).

Contract: tests/test_delivery_outbox.py (red at 633f223) + R1.1 reviewer #2120
+ REVIEW_BLOCK #2154 (0600 fail-closed, legacy schema rebuild)
+ FIX #2184/#2169 (legacy allowlist + credential key normalization).
- Canonical payload JSON + sha256 digest; a repeated idempotency key reuses the
  original record only when job_kind/target/digest all match — any difference
  fails closed as IdempotencyConflict.
- Credentials never enter the schema or API; credential-named fields are
  detected after key normalization (lowercase, separators stripped) so api_key,
  x-api-key, access_key, cookie, set-cookie and the classic token/authorization/
  password/secret/credential are all rejected before any DB write
  (OutboxValidationError); errors are sanitized (no key, no value).
  NaN/Inf, non-string keys and non-serializable values are likewise rejected.
- Write guard via runtime_paths.validate_store (final/intermediate symlink
  escape fail-closed); store file mode is enforced to 0600 fail-closed before
  any payload write (OutboxStoreError on chmod/stat failure).
- Only the exact known 6-column legacy fingerprint may be rebuilt in a
  transaction (CREATE new + copy + DROP + RENAME); any future/extra/unknown
  schema difference raises OutboxStoreError fail-closed and mutates nothing
  (DB hash/schema/rows unchanged). Failure is atomic (rollback leaves legacy).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
from typing import Any

import runtime_paths

# Module-global store path; tests monkeypatch this to a tmp path. Computed via
# runtime_paths.store which is pure-read (no mkdir/DDL at import time).
DB_PATH = runtime_paths.store("delivery_outbox")

_lock = threading.Lock()


class OutboxValidationError(ValueError):
    """Payload rejected before any DB write (credentials / NaN / bad types)."""


class IdempotencyConflict(RuntimeError):
    """Same idempotency key resubmitted with differing kind/target/digest."""


class OutboxStoreError(RuntimeError):
    """Store integrity failure (mode/stat/unknown schema) — fail-closed."""


# Credential indicators matched against the alnum-normalized key (lowercase,
# separators stripped): catches api_key/api-key/APIKEY, x-api-key, access_key,
# cookie/set-cookie, plus token/authorization/password/secret/credential.
_CREDENTIAL_INDICATORS = (
    "apikey", "accesskey", "cookie", "token",
    "authorization", "password", "secret", "credential",
)
_STORE_MODE = 0o600

_CREATE_BODY = (
    "(job_id TEXT PRIMARY KEY, "
    "idempotency_key TEXT NOT NULL, "
    "job_kind TEXT NOT NULL, "
    "target TEXT NOT NULL, "
    "payload_json TEXT NOT NULL, "
    "payload_digest TEXT NOT NULL, "
    "attempt INTEGER NOT NULL DEFAULT 0, "
    "next_attempt_at REAL NOT NULL, "
    "status TEXT NOT NULL DEFAULT 'pending', "
    "created_ts REAL NOT NULL, "
    "updated_ts REAL NOT NULL, "
    "last_error_summary TEXT)"
)
_CREATE_SQL = "CREATE TABLE IF NOT EXISTS delivery_jobs " + _CREATE_BODY
_CREATE_NEW_SQL = "CREATE TABLE delivery_jobs_new " + _CREATE_BODY
_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS delivery_jobs_idempotency "
    "ON delivery_jobs(idempotency_key)"
)
# (name, type upper, notnull, pk) — must match PRAGMA table_info of _CREATE_SQL.
_FRESH_COLUMNS = (
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
# Exact known-legacy 6-column fingerprint — the ONLY shape rebuilt in place.
_LEGACY_COLUMNS = (
    ("job_id", "TEXT", 0, 1),
    ("idempotency_key", "TEXT", 1, 0),
    ("job_kind", "TEXT", 1, 0),
    ("target", "TEXT", 1, 0),
    ("payload_json", "TEXT", 1, 0),
    ("created_ts", "REAL", 1, 0),
)
_COLUMNS = (
    "job_id", "idempotency_key", "job_kind", "target", "payload_json",
    "payload_digest", "attempt", "next_attempt_at", "status",
    "created_ts", "updated_ts", "last_error_summary",
)
_COL_INDEX = {name: i for i, name in enumerate(_COLUMNS)}
_SELECT = "SELECT " + ", ".join(_COLUMNS) + " FROM delivery_jobs"


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _digest_for(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _credential_indicator(key: str) -> str | None:
    """Return the matched indicator if the normalized key looks credential-ish.

    Normalization lowercases and strips non-alphanumerics so api_key, Api-Key,
    APIKEY, x-api-key all collapse to a form containing 'apikey'.
    """
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    for indicator in _CREDENTIAL_INDICATORS:
        if indicator in normalized:
            return indicator
    return None


def _validate_value(obj: Any) -> None:
    # bool before int (bool is a subclass). Errors carry no payload content.
    if isinstance(obj, bool):
        return
    if obj is None or isinstance(obj, (str, int)):
        return
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise OutboxValidationError("non-finite number")
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise OutboxValidationError("non-string key")
            if _credential_indicator(key):
                raise OutboxValidationError("credential field not allowed")
            _validate_value(value)
        return
    if isinstance(obj, list):
        for item in obj:
            _validate_value(item)
        return
    raise OutboxValidationError("non-serializable value")


def _validate_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise OutboxValidationError("payload must be a dict")
    _validate_value(payload)


def _guard_store() -> None:
    """Write guard: reject final/intermediate symlink escape before any write."""
    runtime_paths.validate_store("delivery_outbox")


def _enforce_mode() -> None:
    """Fail-closed: store file must be 0600 before any DDL/payload write.

    Raises OutboxStoreError on stat/chmod failure or if mode cannot be set;
    never silently proceeds with an insecure file.
    """
    try:
        st = os.stat(str(DB_PATH))
    except OSError as exc:
        raise OutboxStoreError("store stat failed") from exc
    mode = st.st_mode & 0o777
    if mode != _STORE_MODE:
        try:
            os.chmod(str(DB_PATH), _STORE_MODE)
        except OSError as exc:
            raise OutboxStoreError("store chmod failed") from exc
        try:
            mode = os.stat(str(DB_PATH)).st_mode & 0o777
        except OSError as exc:
            raise OutboxStoreError("store stat failed after chmod") from exc
    if mode != _STORE_MODE:
        raise OutboxStoreError("store file mode not 0600")


def _connect() -> sqlite3.Connection:
    path = DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None → autocommit; explicit BEGIN/COMMIT for migrations.
    con = sqlite3.connect(str(path), isolation_level=None)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _schema_matches(con: sqlite3.Connection) -> bool:
    """True if delivery_jobs physical schema matches the current CREATE."""
    try:
        rows = con.execute("PRAGMA table_info(delivery_jobs)").fetchall()
    except sqlite3.Error:
        return False
    if len(rows) != len(_FRESH_COLUMNS):
        return False
    actual = {
        str(r[1]): (str(r[2] or "").upper(), int(r[3]), int(r[5])) for r in rows
    }
    expected = {
        name: (typ, nn, pk) for name, typ, nn, pk in _FRESH_COLUMNS
    }
    return actual == expected


def _is_known_legacy(con: sqlite3.Connection) -> bool:
    """True only for the exact known 6-column legacy fingerprint."""
    try:
        rows = con.execute("PRAGMA table_info(delivery_jobs)").fetchall()
    except sqlite3.Error:
        return False
    if len(rows) != len(_LEGACY_COLUMNS):
        return False
    actual = {
        str(r[1]): (str(r[2] or "").upper(), int(r[3]), int(r[5])) for r in rows
    }
    expected = {name: (typ, nn, pk) for name, typ, nn, pk in _LEGACY_COLUMNS}
    return actual == expected


def _rebuild_legacy(con: sqlite3.Connection) -> None:
    """Rebuild the known legacy schema in a transaction to exactly match the
    current CREATE, preserving rows (digest/next_attempt_at/updated_ts backfilled).

    Failure is atomic: any error rolls back so the legacy table is untouched.
    """
    con.execute("BEGIN")
    try:
        rows = con.execute(
            "SELECT job_id, idempotency_key, job_kind, target, payload_json, "
            "created_ts FROM delivery_jobs"
        ).fetchall()
        con.execute(_CREATE_NEW_SQL)
        for job_id, idempotency_key, job_kind, target, payload_json, created_ts in rows:
            parsed = json.loads(payload_json)
            digest = _digest_for(parsed)
            ts = created_ts if created_ts is not None else 0.0
            con.execute(
                "INSERT INTO delivery_jobs_new (job_id, idempotency_key, "
                "job_kind, target, payload_json, payload_digest, attempt, "
                "next_attempt_at, status, created_ts, updated_ts, "
                "last_error_summary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, idempotency_key, job_kind, target, payload_json,
                 digest, 0, ts, "pending", ts, ts, None),
            )
        con.execute("DROP TABLE delivery_jobs")
        con.execute("ALTER TABLE delivery_jobs_new RENAME TO delivery_jobs")
        con.execute(_INDEX_SQL)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def _ensure_schema(con: sqlite3.Connection) -> None:
    exists = con.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='delivery_jobs'"
    ).fetchone()
    if not exists:
        con.execute(_CREATE_SQL)
        con.execute(_INDEX_SQL)
        return
    if _schema_matches(con):
        indexes = {
            row[1] for row in con.execute("PRAGMA index_list(delivery_jobs)")
        }
        if "delivery_jobs_idempotency" not in indexes:
            con.execute(_INDEX_SQL)
        return
    if _is_known_legacy(con):
        _rebuild_legacy(con)
        return
    # Unknown/future/extra schema: fail closed — never rebuild or mutate.
    raise OutboxStoreError("unknown delivery_outbox schema; refusing to migrate")


def _row_to_job(row: tuple) -> dict:
    return {
        "job_id": row[_COL_INDEX["job_id"]],
        "idempotency_key": row[_COL_INDEX["idempotency_key"]],
        "job_kind": row[_COL_INDEX["job_kind"]],
        "target": row[_COL_INDEX["target"]],
        "payload": json.loads(row[_COL_INDEX["payload_json"]]),
        "payload_digest": row[_COL_INDEX["payload_digest"]],
        "attempt": row[_COL_INDEX["attempt"]],
        "next_attempt_at": row[_COL_INDEX["next_attempt_at"]],
        "status": row[_COL_INDEX["status"]],
        "created_ts": row[_COL_INDEX["created_ts"]],
        "updated_ts": row[_COL_INDEX["updated_ts"]],
        "last_error_summary": row[_COL_INDEX["last_error_summary"]],
    }


def _conflict(existing: tuple, job_kind: str, target: str, digest: str) -> bool:
    return (
        existing[_COL_INDEX["job_kind"]] != job_kind
        or existing[_COL_INDEX["target"]] != target
        or existing[_COL_INDEX["payload_digest"]] != digest
    )


def enqueue(*, job_kind, target, payload, idempotency_key, now):
    """Persist a delivery job idempotently. Keyword-only; no credential params."""
    _validate_payload(payload)
    _guard_store()
    canonical = _canonical_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with _lock:
        con = _connect()
        try:
            _enforce_mode()
            _ensure_schema(con)
            row = con.execute(
                _SELECT + " WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if row is not None:
                if _conflict(row, job_kind, target, digest):
                    raise IdempotencyConflict(
                        f"idempotency_key {idempotency_key!r} conflict"
                    )
                return _row_to_job(row)
            job_id = secrets.token_hex(16)
            try:
                con.execute(
                    "INSERT INTO delivery_jobs (job_id, idempotency_key, "
                    "job_kind, target, payload_json, payload_digest, attempt, "
                    "next_attempt_at, status, created_ts, updated_ts, "
                    "last_error_summary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, idempotency_key, job_kind, target, canonical,
                     digest, 0, now, "pending", now, now, None),
                )
            except sqlite3.IntegrityError:
                row = con.execute(
                    _SELECT + " WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if row is None:
                    raise
                if _conflict(row, job_kind, target, digest):
                    raise IdempotencyConflict(
                        f"idempotency_key {idempotency_key!r} conflict"
                    )
                return _row_to_job(row)
            row = con.execute(_SELECT + " WHERE job_id=?", (job_id,)).fetchone()
            return _row_to_job(row)
        finally:
            con.close()


def get_job(job_id):
    """Read a job by id, rebuilding a known legacy schema in place if needed."""
    _guard_store()
    with _lock:
        con = _connect()
        try:
            _enforce_mode()
            _ensure_schema(con)
            row = con.execute(_SELECT + " WHERE job_id=?", (job_id,)).fetchone()
            return _row_to_job(row) if row is not None else None
        finally:
            con.close()
