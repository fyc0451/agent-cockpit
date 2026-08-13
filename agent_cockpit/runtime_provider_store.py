"""Provider-owned identity observations for Runtime Provider v1."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


SCHEMA_VERSION = 1
_TABLES = frozenset({"provider_identity_observations", "schema_metadata"})
_SCHEMA = (
"""CREATE TABLE provider_identity_observations (
    provider_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    runtime_identity TEXT,
    identity_status TEXT NOT NULL CHECK (identity_status IN ('verified', 'identity_unverified')),
    epoch INTEGER,
    observed_at TEXT NOT NULL,
    watermark INTEGER NOT NULL CHECK (watermark >= 1),
    PRIMARY KEY (provider_id, node_id),
    CHECK ((identity_status = 'verified' AND runtime_identity IS NOT NULL AND epoch IS NOT NULL AND epoch >= 0)
        OR (identity_status = 'identity_unverified' AND runtime_identity IS NULL AND epoch IS NULL))
);""",
"""CREATE TABLE schema_metadata (
    schema_version INTEGER PRIMARY KEY,
    schema_name TEXT NOT NULL,
    installed_at TEXT NOT NULL
);""",
)


class RuntimeProviderStoreError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise RuntimeProviderStoreError(code) from None


@dataclass(frozen=True)
class IdentityObservation:
    provider_id: str
    node_id: str
    runtime_identity: str | None
    identity_status: str
    epoch: int | None
    observed_at: str
    watermark: int


class RuntimeProviderStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def close(self) -> None:
        """The store owns no persistent connection."""

    def get_observation(
        self, *, provider_id: str, node_id: str,
    ) -> IdentityObservation | None:
        _identity(provider_id, "provider_id")
        _identity(node_id, "node_id")
        connection = _connect(self.path, readonly=True)
        try:
            _validate_schema(connection)
            row = connection.execute(
                "SELECT provider_id, node_id, runtime_identity, identity_status, "
                "epoch, observed_at, watermark FROM provider_identity_observations "
                "WHERE provider_id=? AND node_id=?",
                (provider_id, node_id),
            ).fetchone()
        except RuntimeProviderStoreError:
            raise
        except sqlite3.Error:
            _fail("store_read_failed")
        finally:
            connection.close()
        if row is None:
            return None
        return IdentityObservation(
            str(row[0]), str(row[1]), row[2], str(row[3]), row[4],
            str(row[5]), int(row[6]),
        )

    def record_observation(
        self, *, provider_id: str, node_id: str,
        runtime_identity: str | None, epoch: int | None, observed_at: str,
    ) -> IdentityObservation:
        provider_id = _identity(provider_id, "provider_id")
        node_id = _identity(node_id, "node_id")
        observed_at = _utc_timestamp(observed_at)
        if (runtime_identity is None) != (epoch is None):
            _fail("invalid_observation")
        if runtime_identity is None or epoch is None:
            runtime_identity = None
            epoch = None
            status = "identity_unverified"
        else:
            runtime_identity = _identity(runtime_identity, "runtime_identity", maximum=128)
            if type(epoch) is not int or epoch < 0:
                _fail("invalid_observation")
            status = "verified"
        connection = _connect(self.path, readonly=False)
        try:
            _validate_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT watermark FROM provider_identity_observations "
                "WHERE provider_id=? AND node_id=?",
                (provider_id, node_id),
            ).fetchone()
            watermark = 1 if row is None else int(row[0]) + 1
            connection.execute(
                "INSERT INTO provider_identity_observations "
                "(provider_id,node_id,runtime_identity,identity_status,epoch,observed_at,watermark) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider_id,node_id) DO UPDATE SET "
                "runtime_identity=excluded.runtime_identity, identity_status=excluded.identity_status, "
                "epoch=excluded.epoch, observed_at=excluded.observed_at, watermark=excluded.watermark",
                (provider_id, node_id, runtime_identity, status, epoch, observed_at, watermark),
            )
            connection.execute("COMMIT")
        except RuntimeProviderStoreError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            _fail("store_write_failed")
        finally:
            connection.close()
        return IdentityObservation(
            provider_id, node_id, runtime_identity, status, epoch, observed_at, watermark,
        )


def initialize(path: Path, *, installed_at: str) -> RuntimeProviderStore:
    path = _absolute(path)
    installed_at = _utc_timestamp(installed_at)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existed = path.exists()
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        if existed:
            _validate_schema(connection)
            return RuntimeProviderStore(path)
        connection.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_metadata VALUES (?,?,?)",
            (SCHEMA_VERSION, "runtime_provider", installed_at),
        )
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.execute("COMMIT")
    except RuntimeProviderStoreError:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    except sqlite3.Error:
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        _fail("store_write_failed")
    finally:
        connection.close()
    return RuntimeProviderStore(path)


def open_existing(path: Path) -> RuntimeProviderStore:
    path = _absolute(path)
    connection = _connect(path, readonly=True)
    try:
        _validate_schema(connection)
    finally:
        connection.close()
    return RuntimeProviderStore(path)


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly and not path.is_file():
        _fail("schema_missing")
    try:
        if readonly:
            return sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
        return sqlite3.connect(path, isolation_level=None)
    except sqlite3.Error:
        _fail("store_read_failed" if readonly else "store_write_failed")


def _validate_schema(connection: sqlite3.Connection) -> None:
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        metadata = connection.execute(
            "SELECT schema_version, schema_name FROM schema_metadata"
        ).fetchall()
        tables = frozenset(
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )
    except sqlite3.Error:
        _fail("schema_missing")
    if (
        version != SCHEMA_VERSION
        or metadata != [(SCHEMA_VERSION, "runtime_provider")]
        or tables != _TABLES
    ):
        _fail("schema_mismatch")


def _absolute(path: Path) -> Path:
    value = Path(path)
    if not value.is_absolute() or ".." in value.parts:
        _fail("store_unsafe")
    return value


def _identity(value: object, field: str, *, maximum: int = 64) -> str:
    if (
        type(value) is not str or not value or len(value) > maximum
        or any(not (char.isascii() and (char.isalnum() or char in "_-.:")) for char in value)
    ):
        _fail("invalid_observation")
    return value


def _utc_timestamp(value: object) -> str:
    if type(value) is not str or not value.endswith("Z") or len(value) > 32:
        _fail("invalid_observation")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        _fail("invalid_observation")
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _fail("invalid_observation")
    return value
