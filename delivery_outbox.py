"""delivery_outbox — dormant reliable-delivery outbox persistence (R1).

Independent SQLite store for outbound jobs that must survive process restarts
until a worker picks them up. This slice implements persistence + idempotency
only; it stays dormant (not imported by server/hub_client, no worker started).

Contract: tests/test_delivery_outbox.py (red at 633f223) + R1.1 reviewer #2120.
- Canonical payload JSON + sha256 digest; a repeated idempotency key reuses the
  original record only when job_kind/target/digest all match — any difference
  fails closed as IdempotencyConflict.
- Credentials never enter the schema or API; nested credential-named fields,
  NaN/Inf, non-string keys and non-serializable values are rejected before any
  DB write (OutboxValidationError), and errors never echo payload content.
- Write guard via runtime_paths.validate_store (final/intermediate symlink
  escape fail-closed); store file is held at 0600.
- Legacy 6-column schema migrates forward in place, preserving rows.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
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


_CREDENTIAL_WORDS = ("token", "authorization", "password", "secret", "credential")
_STORE_MODE = 0o600

_CREATE_SQL = (
    "CREATE TABLE IF NOT EXISTS delivery_jobs ("
    "job_id TEXT PRIMARY KEY, "
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
    "last_error_summary TEXT"
    ")"
)
_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS delivery_jobs_idempotency "
    "ON delivery_jobs(idempotency_key)"
)
_COLUMNS = (
    "job_id", "idempotency_key", "job_kind", "target", "payload_json",
    "payload_digest", "attempt", "next_attempt_at", "status",
    "created_ts", "updated_ts", "last_error_summary",
)
_COL_INDEX = {name: i for i, name in enumerate(_COLUMNS)}
_SELECT = "SELECT " + ", ".join(_COLUMNS) + " FROM delivery_jobs"

# Legacy 6-column (job_id, idempotency_key, job_kind, target, payload_json,
# created_ts) → modern in-place migration additions.
_LEGACY_ADDITIONS = (
    ("payload_digest", "TEXT"),
    ("attempt", "INTEGER NOT NULL DEFAULT 0"),
    ("next_attempt_at", "REAL"),
    ("status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("updated_ts", "REAL"),
    ("last_error_summary", "TEXT"),
)


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _digest_for(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


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
            if any(word in key.lower() for word in _CREDENTIAL_WORDS):
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
    try:
        st = os.stat(str(DB_PATH))
        if (st.st_mode & 0o777) != _STORE_MODE:
            os.chmod(str(DB_PATH), _STORE_MODE)
    except OSError:
        pass


def _connect() -> sqlite3.Connection:
    path = DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    exists = con.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='delivery_jobs'"
    ).fetchone()
    if not exists:
        con.execute(_CREATE_SQL)
        con.execute(_INDEX_SQL)
        con.commit()
        _enforce_mode()
        return
    cols = {row[1] for row in con.execute("PRAGMA table_info(delivery_jobs)")}
    added = False
    for col, decl in _LEGACY_ADDITIONS:
        if col not in cols:
            con.execute(f"ALTER TABLE delivery_jobs ADD COLUMN {col} {decl}")
            added = True
    indexes = {row[1] for row in con.execute("PRAGMA index_list(delivery_jobs)")}
    if "delivery_jobs_idempotency" not in indexes:
        con.execute(_INDEX_SQL)
    if added:
        rows = con.execute(
            "SELECT job_id, payload_json, created_ts FROM delivery_jobs "
            "WHERE payload_digest IS NULL"
        ).fetchall()
        for job_id, payload_json, created_ts in rows:
            parsed = json.loads(payload_json)
            digest = _digest_for(parsed)
            ts = created_ts if created_ts is not None else 0.0
            con.execute(
                "UPDATE delivery_jobs SET payload_digest=?, "
                "next_attempt_at=?, updated_ts=? WHERE job_id=?",
                (digest, ts, ts, job_id),
            )
    con.commit()
    _enforce_mode()


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
                con.commit()
            except sqlite3.IntegrityError:
                con.rollback()
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
    """Read a job by id, migrating a legacy schema in place if needed."""
    _guard_store()
    with _lock:
        con = _connect()
        try:
            _ensure_schema(con)
            row = con.execute(_SELECT + " WHERE job_id=?", (job_id,)).fetchone()
            return _row_to_job(row) if row is not None else None
        finally:
            con.close()
