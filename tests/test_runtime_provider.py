from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_cockpit import runtime_provider_api as api
from agent_cockpit import runtime_provider_store as store


NOW = "2026-08-14T00:00:00Z"


@dataclass
class FakeTransport:
    installed: bool = True
    protocol: int = 1
    identity: str | None = "runtime-alpha"
    epoch: int | None = 7
    sessions: object = field(default_factory=lambda: [
        {"session_id": "session-1", "name": "alpha", "status": "running"},
    ])
    snapshots: dict[str, object] = field(default_factory=lambda: {
        "alpha": {"session_id": "session-1", "process_state": "running", "agent_count": 1},
    })
    failures: dict[str, str] = field(default_factory=dict)

    def _fail(self, method: str):
        if method in self.failures:
            raise api.ProviderTransportError(self.failures[method])

    def capabilities(self):
        self._fail("capabilities")
        return {
            "provider_id": "local_herdr", "protocol": self.protocol,
            "installed": self.installed,
            "methods": ["handshake", "list_sessions", "snapshot"],
        }

    def handshake(self):
        self._fail("handshake")
        return {
            "provider_id": "local_herdr", "protocol": self.protocol,
            "runtime_identity": self.identity, "epoch": self.epoch,
        }

    def list_sessions(self):
        self._fail("list_sessions")
        return {"sessions": self.sessions}

    def snapshot(self, session_name: str):
        self._fail(f"snapshot:{session_name}")
        return self.snapshots[session_name]


def _initialized(tmp_path: Path) -> store.RuntimeProviderStore:
    return store.initialize(tmp_path / "provider.sqlite3", installed_at=NOW)


def _verified(tmp_path: Path, transport: FakeTransport | None = None):
    transport = transport or FakeTransport()
    observations = _initialized(tmp_path)
    observations.record_observation(
        provider_id="local_herdr", node_id="local",
        runtime_identity="runtime-alpha", epoch=7, observed_at=NOW,
    )
    return api.LocalHerdrProvider(transport, observations), observations


def _error(callable_, code: str, status: int):
    with pytest.raises(api.RuntimeProviderError) as caught:
        callable_()
    actual_status, payload = api.error_response(caught.value)
    assert actual_status == status
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {
        "code", "message", "retryable", "request_id", "details",
    }
    assert payload["error"]["code"] == code
    assert payload["error"]["details"] == {}
    assert payload["error"]["request_id"].startswith("req_")
    return payload


def test_available_capabilities_are_local_read_only():
    payload = api.LocalHerdrProvider(FakeTransport()).capabilities()
    assert payload["data"]["node_id"] == "local"
    assert payload["data"]["provider_id"] == "local_herdr"
    capabilities = payload["data"]["capabilities"]
    assert capabilities["runtime.read"] == {"available": True, "reason": None}
    assert capabilities["runtime.attach"]["available"] is False
    assert capabilities["runtime.terminal"]["available"] is False
    assert capabilities["runtime.recovery"]["available"] is False


@pytest.mark.parametrize(
    ("transport", "code", "status"),
    (
        (FakeTransport(installed=False), "provider_not_installed", 503),
        (FakeTransport(protocol=2), "protocol_mismatch", 503),
        (FakeTransport(failures={"capabilities": "timeout"}), "transport_timeout", 504),
        (FakeTransport(failures={"capabilities": "unavailable"}), "transport_unavailable", 503),
    ),
)
def test_transport_capability_failures_are_stable_and_redacted(transport, code, status):
    payload = _error(api.LocalHerdrProvider(transport).capabilities, code, status)
    assert "timeout" not in str(payload["error"]["details"])


def test_protocol_method_mismatch_fails_closed():
    transport = FakeTransport()
    transport.capabilities = lambda: {
        "provider_id": "local_herdr", "protocol": 1,
        "installed": True, "methods": ["handshake"],
    }
    _error(api.LocalHerdrProvider(transport).capabilities, "protocol_mismatch", 503)


def test_unproven_identity_is_null_and_writes_stay_unavailable(tmp_path: Path):
    observations = _initialized(tmp_path)
    payload = api.LocalHerdrProvider(FakeTransport(), observations).handshake()
    assert payload["data"]["identity_status"] == "identity_unverified"
    assert payload["data"]["runtime_identity"] is None
    assert payload["data"]["epoch"] is None
    assert payload["meta"]["partial"] is True
    assert payload["meta"]["warnings"] == ["identity_unverified"]
    assert all(
        payload["data"]["capabilities"][name]["available"] is False
        for name in ("runtime.attach", "runtime.terminal", "runtime.recovery")
    )


def test_verified_handshake_uses_provider_observation(tmp_path: Path):
    provider, observations = _verified(tmp_path)
    payload = provider.handshake()
    assert payload["data"] | {} == payload["data"]
    assert payload["data"]["runtime_identity"] == "runtime-alpha"
    assert payload["data"]["identity_status"] == "verified"
    assert payload["data"]["epoch"] == 7
    assert payload["data"]["observation_watermark"] == 1
    observations.close()


@pytest.mark.parametrize(
    ("identity", "epoch"),
    (("runtime-alpha", None), (None, 7), ("/private/runtime", 7), ("runtime-alpha", True)),
)
def test_malformed_handshake_identity_shape_fails_closed(
    tmp_path: Path, identity, epoch,
):
    observations = _initialized(tmp_path)
    provider = api.LocalHerdrProvider(
        FakeTransport(identity=identity, epoch=epoch), observations,
    )
    _error(provider.handshake, "source_malformed", 503)


def test_list_sessions_normalizes_source_and_process_state(tmp_path: Path):
    provider, _ = _verified(tmp_path)
    payload = provider.list_sessions()
    assert payload["data"]["empty"] is False
    assert payload["data"]["session_errors"] == []
    assert payload["data"]["sessions"] == [{
        "session_id": "session-1", "name": "alpha", "status": "running",
        "snapshot": {"process_state": "running", "agent_count": 1},
        "state": "available",
    }]
    assert payload["meta"]["partial"] is False
    assert payload["meta"]["sources"][0]["status"] == "available"


def test_single_session_failure_is_partial_not_empty(tmp_path: Path):
    transport = FakeTransport(
        sessions=[
            {"session_id": "session-1", "name": "alpha", "status": "running"},
            {"session_id": "session-2", "name": "beta", "status": "stopped"},
        ],
        snapshots={
            "alpha": {"session_id": "session-1", "process_state": "running", "agent_count": 1},
            "beta": {"session_id": "session-2", "process_state": "stopped", "agent_count": 0},
        },
        failures={"snapshot:beta": "timeout"},
    )
    provider, _ = _verified(tmp_path, transport)
    payload = provider.snapshot()
    assert payload["data"]["empty"] is False
    assert payload["meta"]["partial"] is True
    assert payload["meta"]["sources"][0]["status"] == "partial"
    assert payload["data"]["session_errors"] == [{
        "session_id": "session-2", "code": "transport_timeout",
    }]
    assert [item["state"] for item in payload["data"]["sessions"]] == [
        "available", "unavailable",
    ]


def test_true_zero_sessions_is_empty_success(tmp_path: Path):
    provider, _ = _verified(tmp_path, FakeTransport(sessions=[], snapshots={}))
    payload = provider.list_sessions()
    assert payload["data"]["sessions"] == []
    assert payload["data"]["session_errors"] == []
    assert payload["data"]["empty"] is True
    assert payload["meta"]["partial"] is False


@pytest.mark.parametrize(
    "rows",
    (
        "not-a-list",
        [{"session_id": "bad", "name": "alpha", "status": "future"}],
        [{"session_id": "bad", "name": "/absolute/path", "status": "running"}],
    ),
)
def test_malformed_session_source_fails_closed(tmp_path: Path, rows):
    provider, _ = _verified(tmp_path, FakeTransport(sessions=rows))
    _error(provider.list_sessions, "source_malformed", 503)


@pytest.mark.parametrize("duplicate_field", ("session_id", "name"))
def test_duplicate_session_identity_fails_closed(tmp_path: Path, duplicate_field: str):
    second = {"session_id": "session-2", "name": "beta", "status": "running"}
    second[duplicate_field] = "session-1" if duplicate_field == "session_id" else "alpha"
    transport = FakeTransport(sessions=[
        {"session_id": "session-1", "name": "alpha", "status": "running"}, second,
    ])
    provider, _ = _verified(tmp_path, transport)
    _error(provider.list_sessions, "source_malformed", 503)


def test_malformed_individual_snapshot_is_partial(tmp_path: Path):
    transport = FakeTransport(snapshots={"alpha": {
        "session_id": "wrong", "process_state": "running", "agent_count": 1,
    }})
    provider, _ = _verified(tmp_path, transport)
    payload = provider.list_sessions()
    assert payload["meta"]["partial"] is True
    assert payload["data"]["session_errors"] == [{
        "session_id": "session-1", "code": "source_malformed",
    }]


@pytest.mark.parametrize("process_state", ([], {}, ["running"]))
def test_malformed_process_state_is_normalized_as_partial(
    tmp_path: Path, process_state: object,
):
    transport = FakeTransport(snapshots={"alpha": {
        "session_id": "session-1", "process_state": process_state, "agent_count": 1,
    }})
    provider, _ = _verified(tmp_path, transport)
    payload = provider.list_sessions()
    assert payload["meta"]["partial"] is True
    assert payload["meta"]["sources"][0]["status"] == "partial"
    assert payload["data"]["sessions"][0]["state"] == "unavailable"
    assert payload["data"]["session_errors"] == [{
        "session_id": "session-1", "code": "source_malformed",
    }]


def test_non_local_provider_fails_closed_before_transport():
    transport = FakeTransport()
    _error(api.LocalHerdrProvider(transport, node_id="remote").capabilities, "invalid_node", 404)


def test_error_response_never_exposes_exception_or_sensitive_values():
    status, payload = api.error_response(RuntimeError(
        "token=secret /home/private herdr --session raw stderr"
    ))
    assert status == 503
    rendered = str(payload)
    for forbidden in ("secret", "/home/private", "stderr", "--session"):
        assert forbidden not in rendered


def test_initialize_is_idempotent_and_store_schema_is_narrow(tmp_path: Path):
    path = tmp_path / "provider.sqlite3"
    store.initialize(path, installed_at=NOW).close()
    before = path.read_bytes()
    store.initialize(path, installed_at="2026-08-14T01:00:00Z").close()
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        objects = " ".join(
            str(row[0]) + " " + str(row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        ).lower()
    assert "provider_identity_observations" in objects
    for forbidden in (
        "project", "workspace", "agent", "operation", "event", "memory",
        "terminal", "token", "absolute_path",
    ):
        assert forbidden not in objects


def test_initialize_creates_exact_private_regular_leaf_under_open_umask(tmp_path: Path):
    path = tmp_path / "provider.sqlite3"
    previous = os.umask(0)
    try:
        store.initialize(path, installed_at=NOW).close()
    finally:
        os.umask(previous)
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_nlink == 1


@pytest.mark.parametrize("mutation", ("mode", "hardlink"))
@pytest.mark.parametrize("operation", ("initialize", "open_existing"))
def test_existing_leaf_safety_drift_fails_closed(
    tmp_path: Path, mutation: str, operation: str,
):
    path = tmp_path / "provider.sqlite3"
    store.initialize(path, installed_at=NOW).close()
    if mutation == "mode":
        path.chmod(0o640)
    else:
        os.link(path, tmp_path / "provider-copy.sqlite3")
    with pytest.raises(store.RuntimeProviderStoreError) as unsafe:
        if operation == "initialize":
            store.initialize(path, installed_at=NOW)
        else:
            store.open_existing(path)
    assert unsafe.value.code == "store_unsafe"


def test_observation_watermark_is_provider_owned_and_atomic(tmp_path: Path):
    observations = _initialized(tmp_path)
    first = observations.record_observation(
        provider_id="local_herdr", node_id="local",
        runtime_identity="runtime-alpha", epoch=7, observed_at=NOW,
    )
    second = observations.record_observation(
        provider_id="local_herdr", node_id="local",
        runtime_identity=None, epoch=None, observed_at="2026-08-14T00:01:00Z",
    )
    assert first.watermark == 1
    assert second.watermark == 2
    assert second.identity_status == "identity_unverified"
    assert second.runtime_identity is None and second.epoch is None


@pytest.mark.parametrize(
    ("identity", "epoch", "observed_at"),
    (
        ("runtime-alpha", None, NOW),
        (None, 7, NOW),
        ("/private/runtime", 7, NOW),
        ("runtime-alpha", 7, "/private/runtime"),
    ),
)
def test_invalid_observation_shape_never_writes(
    tmp_path: Path, identity, epoch, observed_at,
):
    observations = _initialized(tmp_path)
    before = observations.path.read_bytes()
    with pytest.raises(store.RuntimeProviderStoreError) as invalid:
        observations.record_observation(
            provider_id="local_herdr", node_id="local",
            runtime_identity=identity, epoch=epoch, observed_at=observed_at,
        )
    assert invalid.value.code == "invalid_observation"
    assert observations.path.read_bytes() == before


def test_read_paths_do_not_create_or_modify_store(tmp_path: Path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(store.RuntimeProviderStoreError) as absent:
        store.open_existing(missing)
    assert absent.value.code == "schema_missing"
    assert not missing.exists()

    path = tmp_path / "provider.sqlite3"
    initialized = store.initialize(path, installed_at=NOW)
    initialized.record_observation(
        provider_id="local_herdr", node_id="local",
        runtime_identity="runtime-alpha", epoch=7, observed_at=NOW,
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    readonly = store.open_existing(path)
    assert readonly.get_observation(
        provider_id="local_herdr", node_id="local"
    ).runtime_identity == "runtime-alpha"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_future_or_missing_schema_fails_closed_without_writes(tmp_path: Path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute("PRAGMA user_version=999")
    before = path.read_bytes()
    with pytest.raises(store.RuntimeProviderStoreError) as mismatch:
        store.open_existing(path)
    assert mismatch.value.code in {"schema_missing", "schema_mismatch"}
    assert path.read_bytes() == before


def test_extra_schema_object_is_rejected_without_writes(tmp_path: Path):
    path = tmp_path / "provider.sqlite3"
    store.initialize(path, installed_at=NOW).close()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unexpected(value TEXT)")
    before = path.read_bytes()
    with pytest.raises(store.RuntimeProviderStoreError) as mismatch:
        store.open_existing(path)
    assert mismatch.value.code == "schema_mismatch"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "ddl",
    (
        "CREATE INDEX unexpected_index ON provider_identity_observations(observed_at)",
        "CREATE TRIGGER unexpected_trigger AFTER UPDATE ON provider_identity_observations BEGIN SELECT 1; END",
    ),
)
def test_extra_index_or_trigger_is_rejected_without_writes(tmp_path: Path, ddl: str):
    path = tmp_path / "provider.sqlite3"
    store.initialize(path, installed_at=NOW).close()
    with sqlite3.connect(path) as connection:
        connection.execute(ddl)
    before = path.read_bytes()
    with pytest.raises(store.RuntimeProviderStoreError) as mismatch:
        store.open_existing(path)
    assert mismatch.value.code == "schema_mismatch"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "replacement",
    (
        "watermark TEXT NOT NULL CHECK (watermark >= 1)",
        "watermark INTEGER CHECK (watermark >= 1)",
        "watermark INTEGER NOT NULL DEFAULT 1 CHECK (watermark >= 1)",
    ),
)
def test_column_type_null_and_default_drift_is_rejected(
    tmp_path: Path, replacement: str,
):
    path = tmp_path / "provider.sqlite3"
    store.initialize(path, installed_at=NOW).close()
    with sqlite3.connect(path) as connection:
        original = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='provider_identity_observations'"
        ).fetchone()[0]
        connection.execute("ALTER TABLE provider_identity_observations RENAME TO old_observations")
        connection.execute(str(original).replace(
            "watermark INTEGER NOT NULL CHECK (watermark >= 1)", replacement,
        ))
        connection.execute("DROP TABLE old_observations")
    before = path.read_bytes()
    with pytest.raises(store.RuntimeProviderStoreError) as mismatch:
        store.open_existing(path)
    assert mismatch.value.code == "schema_mismatch"
    assert path.read_bytes() == before
