"""J1 store_schema pure-read probes + /health/ready contract."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_cockpit import runtime_paths
from agent_cockpit import store_schema


@pytest.fixture()
def isolated_roots(tmp_path, monkeypatch):
    data = tmp_path / "data"
    config = tmp_path / "config"
    state = tmp_path / "state"
    uploads = tmp_path / "uploads"
    for p in (data, config, state, uploads):
        p.mkdir()
    monkeypatch.setenv("COCKPIT_DATA_DIR", str(data))
    monkeypatch.setenv("COCKPIT_CONFIG_DIR", str(config))
    monkeypatch.setenv("COCKPIT_STATE_DIR", str(state))
    monkeypatch.setenv("COCKPIT_UPLOADS_DIR", str(uploads))
    monkeypatch.delenv("COCKPIT_COORDINATION_DB", raising=False)
    runtime_paths.reset_cache()
    return {"data": data, "config": config, "state": state, "uploads": uploads}


def test_missing_stores_are_creatable_no_writes(isolated_roots, tmp_path):
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    results = {r["name"]: r for r in store_schema.probe_all_stores()}
    assert results["tasks"]["reason"] == store_schema.REASON_MISSING_CREATABLE
    assert results["project_registry"]["reason"] == store_schema.REASON_MISSING_CREATABLE
    assert results["workspace_work"]["reason"] == store_schema.REASON_MISSING_CREATABLE
    assert results["workspace_execution"]["reason"] == store_schema.REASON_MISSING_CREATABLE
    assert results["coordination"]["reason"] == store_schema.REASON_MISSING_CREATABLE
    assert results["delivery_outbox"]["reason"] == store_schema.REASON_MISSING_CREATABLE
    for name in store_schema._ACCEPTED_SQLITE_STORES:
        assert results[name]["reason"] == store_schema.REASON_MISSING_CREATABLE
    after_files = list(tmp_path.rglob("*"))
    # probe must not create db/json/wal/shm
    created = [p for p in after_files if p.is_file() and p not in before]
    assert created == []


def test_accepted_sqlite_stores_use_lazy_read_only_openers(
    isolated_roots, monkeypatch,
):
    from agent_cockpit import event_store
    from agent_cockpit import memory_store
    from agent_cockpit import operation_store
    from agent_cockpit import runtime_provider_store
    from agent_cockpit import terminal_ticket_store
    from agent_cockpit import chat_ledger

    initializers = {
        "runtime_provider": lambda path: runtime_provider_store.initialize(
            path, installed_at="2026-08-14T00:00:00Z",
        ),
        "event_journal": event_store.initialize,
        "operation_journal": operation_store.initialize,
        "project_memory": memory_store.initialize,
        "terminal_ticket": terminal_ticket_store.initialize,
        "chat_ledger": chat_ledger.initialize,
    }
    for name, initialize in initializers.items():
        initialize(runtime_paths.store(name)).close()
        before = (
            runtime_paths.store(name).read_bytes(),
            runtime_paths.store(name).stat().st_mtime_ns,
        )
        result = store_schema._check_accepted_sqlite(name)
        assert result["reason"] == store_schema.REASON_COMPATIBLE
        assert (
            runtime_paths.store(name).read_bytes(),
            runtime_paths.store(name).stat().st_mtime_ns,
        ) == before
        assert not Path(f"{runtime_paths.store(name)}-wal").exists()
        assert not Path(f"{runtime_paths.store(name)}-shm").exists()
    assert len(store_schema._APP_OWNED_STORES) == 22
    assert "workspace_work" in store_schema._APP_OWNED_STORES
    assert "workspace_work" not in store_schema._ACCEPTED_SQLITE_STORES
    assert "workspace_execution" in store_schema._APP_OWNED_STORES
    assert "workspace_execution" not in store_schema._ACCEPTED_SQLITE_STORES


def test_accepted_sqlite_sidecar_blocks_before_lazy_import(
    isolated_roots, monkeypatch,
):
    path = runtime_paths.store("event_journal")
    path.write_bytes(b"sqlite")
    path.chmod(0o600)
    Path(f"{path}-wal").write_bytes(b"live")
    monkeypatch.setattr(
        store_schema, "_accepted_sqlite_opener",
        lambda _name: pytest.fail("opener must not be imported"),
    )
    result = store_schema._check_accepted_sqlite("event_journal")
    assert result["reason"] == store_schema.REASON_PROBE_REQUIRES_QUIESCENCE


def test_accepted_sqlite_lazy_import_failure_is_sanitized_zero_write(
    isolated_roots, monkeypatch,
):
    path = runtime_paths.store("event_journal")
    path.write_bytes(b"not-opened")
    path.chmod(0o600)
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    def fail(_name):
        raise ImportError("private module detail")

    monkeypatch.setattr(store_schema, "_accepted_sqlite_opener", fail)
    result = store_schema._check_accepted_sqlite("event_journal")
    assert result == {
        "name": "event_journal",
        "compat_family": store_schema.COMPAT_FAMILY,
        "state": "error",
        "reason": store_schema.REASON_UNREADABLE,
    }
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_delivery_outbox_current_store_fingerprint_compatible(isolated_roots, monkeypatch):
    from agent_cockpit import delivery_outbox

    path = runtime_paths.store("delivery_outbox")
    monkeypatch.setattr(delivery_outbox, "DB_PATH", path)
    delivery_outbox.enqueue(
        job_kind="send_message", target="project-a/agent-b",
        payload={"subject": "status"}, idempotency_key="ready-check", now=1.0,
    )
    result = {
        row["name"]: row for row in store_schema.probe_all_stores()
    }["delivery_outbox"]
    assert result["reason"] == store_schema.REASON_COMPATIBLE


def _project_registry_check(**kwargs):
    from agent_cockpit import project_registry_contracts

    return store_schema._check_sqlite(
        "project_registry",
        project_registry_contracts.PROJECT_REGISTRY_TABLES,
        expected_defaults=project_registry_contracts.PROJECT_REGISTRY_DEFAULTS,
        expected_indexes=project_registry_contracts.PROJECT_REGISTRY_INDEXES,
        expected_fks=project_registry_contracts.PROJECT_REGISTRY_FOREIGN_KEYS,
        expected_user_version=project_registry_contracts.SCHEMA_VERSION,
        expected_schema_statements=project_registry_contracts.SCHEMA_STATEMENTS,
        expected_migration=(
            project_registry_contracts.PROJECT_REGISTRY_MIGRATION_RECEIPT
        ),
        **kwargs,
    )


def test_project_registry_current_store_fingerprint_compatible(isolated_roots):
    from agent_cockpit import project_registry_store

    path = runtime_paths.store("project_registry")
    project_registry_store.initialize(path).close()
    assert _project_registry_check()["reason"] == store_schema.REASON_COMPATIBLE


@pytest.mark.parametrize("damage", ["receipt", "trigger", "partial_index"])
def test_project_registry_strict_schema_damage_fails_closed(
    isolated_roots, damage,
):
    from agent_cockpit import project_registry_contracts
    from agent_cockpit import project_registry_store

    path = runtime_paths.store("project_registry")
    project_registry_store.initialize(path).close()
    connection = sqlite3.connect(path)
    if damage == "receipt":
        damaged_digest = "0" * 64
        connection.execute("DROP TRIGGER schema_migrations_update_forbidden")
        connection.execute(
            "UPDATE schema_migrations SET schema_digest=?", (damaged_digest,),
        )
        update_guard = next(
            statement
            for statement in project_registry_contracts.SCHEMA_STATEMENTS
            if "CREATE TRIGGER schema_migrations_update_forbidden" in statement
        )
        connection.execute(update_guard)
        actual_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert actual_triggers == project_registry_contracts.PROJECT_REGISTRY_TRIGGERS
        assert connection.execute(
            "SELECT schema_digest FROM schema_migrations"
        ).fetchone() == (damaged_digest,)
    elif damage == "trigger":
        existing_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert existing_triggers == (
            project_registry_contracts.PROJECT_REGISTRY_TRIGGERS
        )
        trigger_names = sorted(
            existing_triggers & project_registry_contracts.PROJECT_REGISTRY_TRIGGERS
        )
        assert trigger_names, "project registry has no contract trigger to damage"
        trigger_name = trigger_names[0]
        connection.execute(f'DROP TRIGGER "{trigger_name}"')
        remaining_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert remaining_triggers == (
            project_registry_contracts.PROJECT_REGISTRY_TRIGGERS - {trigger_name}
        )
    else:
        connection.execute("DROP INDEX repo_locations_active_node_path")
        connection.execute(
            "CREATE UNIQUE INDEX repo_locations_active_node_path "
            "ON repo_locations(node_id, canonical_path)"
        )
    connection.commit()
    connection.close()
    result = _project_registry_check()
    assert result["state"] == "error"
    assert result["reason"] == store_schema.REASON_FINGERPRINT_MISMATCH


def _workspace_work_check(**kwargs):
    from agent_cockpit import workspace_work_store

    return store_schema._check_sqlite(
        "workspace_work",
        store_schema._WORKSPACE_WORK_TABLES,
        expected_defaults=store_schema._WORKSPACE_WORK_DEFAULTS,
        expected_indexes=store_schema._WORKSPACE_WORK_INDEXES,
        expected_fks=store_schema._WORKSPACE_WORK_FKS,
        expected_user_version=workspace_work_store.SCHEMA_VERSION,
        expected_schema_statements=workspace_work_store._SCHEMA,
        expected_migration=(
            workspace_work_store.MIGRATION_ID,
            workspace_work_store.SCHEMA_VERSION,
            workspace_work_store.SCHEMA_DIGEST,
        ),
        **kwargs,
    )


def test_workspace_work_current_store_fingerprint_compatible(isolated_roots):
    from agent_cockpit import workspace_work_store

    path = runtime_paths.store("workspace_work")
    workspace_work_store.initialize(path).close()
    assert _workspace_work_check()["reason"] == store_schema.REASON_COMPATIBLE
    assert {
        row["name"]: row for row in store_schema.probe_all_stores()
    }["workspace_work"]["reason"] == store_schema.REASON_COMPATIBLE


def test_workspace_work_future_and_corrupt_are_pure_read_fail_closed(isolated_roots):
    path = runtime_paths.store("workspace_work")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE future_only (value TEXT)")
    connection.execute("PRAGMA user_version=999")
    connection.commit()
    connection.close()
    os.chmod(path, 0o600)
    before = path.read_bytes()

    future = {
        row["name"]: row for row in store_schema.probe_all_stores()
    }["workspace_work"]
    assert future["state"] == "future"
    assert future["reason"] == store_schema.REASON_FUTURE_SCHEMA
    assert path.read_bytes() == before
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()

    path.write_text("not-a-database", encoding="utf-8")
    os.chmod(path, 0o600)
    damaged = path.read_bytes()
    corrupt = _workspace_work_check()
    assert corrupt["reason"] in {
        store_schema.REASON_CORRUPT, store_schema.REASON_UNREADABLE,
    }
    assert path.read_bytes() == damaged


def _workspace_execution_check(**kwargs):
    from agent_cockpit import workspace_execution_store

    return store_schema._check_sqlite(
        "workspace_execution",
        store_schema._WORKSPACE_EXECUTION_TABLES,
        expected_defaults=store_schema._WORKSPACE_EXECUTION_DEFAULTS,
        expected_indexes=store_schema._WORKSPACE_EXECUTION_INDEXES,
        expected_fks=store_schema._WORKSPACE_EXECUTION_FKS,
        expected_user_version=workspace_execution_store.SCHEMA_VERSION,
        expected_schema_statements=workspace_execution_store._SCHEMA,
        expected_migration=(
            workspace_execution_store.MIGRATION_ID,
            workspace_execution_store.SCHEMA_VERSION,
            workspace_execution_store.SCHEMA_DIGEST,
        ),
        **kwargs,
    )


def test_workspace_execution_current_store_fingerprint_compatible(isolated_roots):
    from agent_cockpit import workspace_execution_store

    path = runtime_paths.store("workspace_execution")
    workspace_execution_store.initialize(path).close()
    assert _workspace_execution_check()["reason"] == store_schema.REASON_COMPATIBLE
    assert {
        row["name"]: row for row in store_schema.probe_all_stores()
    }["workspace_execution"]["reason"] == store_schema.REASON_COMPATIBLE


def test_workspace_execution_future_and_corrupt_are_pure_read_fail_closed(
    isolated_roots,
):
    path = runtime_paths.store("workspace_execution")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE future_only (value TEXT)")
    connection.execute("PRAGMA user_version=999")
    connection.commit()
    connection.close()
    os.chmod(path, 0o600)
    before = path.read_bytes()

    future = {
        row["name"]: row for row in store_schema.probe_all_stores()
    }["workspace_execution"]
    assert future["state"] == "future"
    assert future["reason"] == store_schema.REASON_FUTURE_SCHEMA
    assert path.read_bytes() == before
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()

    path.write_text("not-a-database", encoding="utf-8")
    os.chmod(path, 0o600)
    damaged = path.read_bytes()
    corrupt = _workspace_execution_check()
    assert corrupt["reason"] in {
        store_schema.REASON_CORRUPT, store_schema.REASON_UNREADABLE,
    }
    assert path.read_bytes() == damaged


def test_future_project_registry_schema_is_pure_read_fail_closed(isolated_roots):
    path = runtime_paths.store("project_registry")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE future_only (value TEXT)")
    connection.execute("PRAGMA user_version=999")
    connection.commit()
    connection.close()
    os.chmod(path, 0o600)
    before = path.read_bytes()

    result = {
        row["name"]: row for row in store_schema.probe_all_stores()
    }["project_registry"]

    assert result["state"] == "future"
    assert result["reason"] == store_schema.REASON_FUTURE_SCHEMA
    assert path.read_bytes() == before
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_b0_store_is_conditional_and_missing_creatable(
    isolated_roots, monkeypatch,
):
    monkeypatch.setenv("COCKPIT_B0_MODE", "off")
    off = {r["name"]: r for r in store_schema.probe_all_stores()}
    assert "leader_binding" not in off

    monkeypatch.setenv("COCKPIT_B0_MODE", "shadow")
    shadow = {r["name"]: r for r in store_schema.probe_all_stores()}
    assert shadow["leader_binding"]["reason"] == store_schema.REASON_MISSING_CREATABLE


def test_b0_legacy_store_requires_migration(isolated_roots, monkeypatch):
    monkeypatch.setenv("COCKPIT_B0_MODE", "shadow")
    path = runtime_paths.store("leader_binding")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE leader_bindings (scope_kind TEXT, scope_id TEXT, "
        "mail_name TEXT, state TEXT, binding_version INTEGER)"
    )
    con.commit()
    con.close()
    result = {
        row["name"]: row for row in store_schema.probe_all_stores()
    }["leader_binding"]
    assert result["reason"] == store_schema.REASON_FINGERPRINT_MISMATCH
    assert result["state"] == "error"


def test_b0_current_store_fingerprint_compatible(isolated_roots, monkeypatch):
    from agent_cockpit import leader_binding

    monkeypatch.setenv("COCKPIT_B0_MODE", "canary")
    path = runtime_paths.store("leader_binding")
    monkeypatch.setattr(leader_binding, "DB_PATH", path)
    con = leader_binding._connect()
    con.close()
    result = {
        row["name"]: row for row in store_schema.probe_all_stores()
    }["leader_binding"]
    assert result["reason"] == store_schema.REASON_COMPATIBLE


def _mk_tasks_db(path: Path, *, user_version: int = 0, extra_col: str | None = None) -> None:
    con = sqlite3.connect(path)
    extra = f", {extra_col} TEXT" if extra_col else ""
    con.executescript(
        f"""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            workdir TEXT NOT NULL,
            prompt TEXT NOT NULL,
            images TEXT,
            model TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            pid INTEGER,
            exit_code INTEGER,
            created_ts REAL NOT NULL,
            started_ts REAL,
            finished_ts REAL,
            output_tail TEXT,
            source_workdir TEXT,
            base_sha TEXT,
            run_workdir TEXT,
            preview_hash TEXT
            {extra}
        );
        PRAGMA user_version={int(user_version)};
        """
    )
    con.close()


def _tasks_check(**kwargs):
    return store_schema._check_sqlite(
        "tasks",
        {"tasks": store_schema._TASKS_COLUMNS},
        expected_defaults={"tasks": store_schema._TASKS_DEFAULTS},
        expected_indexes={"tasks": store_schema._TASKS_INDEXES},
        expected_fks={"tasks": frozenset()},
        **kwargs,
    )


def _coordination_check(**kwargs):
    return store_schema._check_sqlite(
        "coordination",
        store_schema._COORD_TABLES,
        expected_defaults=store_schema._COORD_DEFAULTS,
        expected_indexes=store_schema._COORD_INDEXES,
        expected_fks=store_schema._COORD_FKS,
        **kwargs,
    )


def test_tasks_fingerprint_compatible(isolated_roots):
    path = runtime_paths.store("tasks")
    _mk_tasks_db(path)
    r = _tasks_check()
    assert r["reason"] == store_schema.REASON_COMPATIBLE


def test_foreign_key_fingerprint_preserves_composite_identity(tmp_path):
    composite_path = tmp_path / "composite.sqlite3"
    independent_path = tmp_path / "independent.sqlite3"
    for path, clause in (
        (
            composite_path,
            "FOREIGN KEY(project_id, location_id) "
            "REFERENCES locations(project_id, location_id)",
        ),
        (
            independent_path,
            "FOREIGN KEY(project_id) REFERENCES locations(project_id), "
            "FOREIGN KEY(location_id) REFERENCES locations(location_id)",
        ),
    ):
        connection = sqlite3.connect(path)
        connection.executescript(
            "CREATE TABLE locations ("
            "project_id TEXT NOT NULL, location_id TEXT NOT NULL, "
            "UNIQUE(project_id, location_id), UNIQUE(location_id)); "
            f"CREATE TABLE workspaces (project_id TEXT, location_id TEXT, {clause});"
        )
        connection.close()

    composite = sqlite3.connect(composite_path)
    independent = sqlite3.connect(independent_path)
    try:
        expected = frozenset({
            (
                ("project_id", "locations", "project_id"),
                ("location_id", "locations", "location_id"),
            ),
        })
        assert store_schema._foreign_keys(composite, "workspaces") == expected
        assert store_schema._foreign_keys(independent, "workspaces") != expected
    finally:
        composite.close()
        independent.close()


def test_index_fingerprint_preserves_partial_predicate_flag(tmp_path):
    partial_path = tmp_path / "partial.sqlite3"
    unconditional_path = tmp_path / "unconditional.sqlite3"
    for path, predicate in (
        (partial_path, " WHERE lifecycle='active'"),
        (unconditional_path, ""),
    ):
        connection = sqlite3.connect(path)
        connection.executescript(
            "CREATE TABLE locations (node_id TEXT, path TEXT, lifecycle TEXT); "
            "CREATE UNIQUE INDEX active_location ON locations(node_id, path)"
            f"{predicate};"
        )
        connection.close()

    partial = sqlite3.connect(partial_path)
    unconditional = sqlite3.connect(unconditional_path)
    try:
        expected = frozenset({
            ("active_location", 1, 1, ("node_id", "path")),
        })
        assert store_schema._named_indexes(partial, "locations") == expected
        assert store_schema._named_indexes(unconditional, "locations") != expected
    finally:
        partial.close()
        unconditional.close()


def test_sqlite_expected_user_version_is_store_specific(isolated_roots):
    path = runtime_paths.store("tasks")
    _mk_tasks_db(path, user_version=1)
    current = _tasks_check(expected_user_version=1)
    assert current["reason"] == store_schema.REASON_COMPATIBLE
    older = _tasks_check(expected_user_version=2)
    assert older["reason"] == store_schema.REASON_MIGRATION_REQUIRED


def test_coordination_assignment_schema_migrates_existing_store(
    isolated_roots, monkeypatch,
):
    from agent_cockpit import coordination

    path = runtime_paths.store("coordination")
    monkeypatch.setattr(coordination, "DB_PATH", path)

    con = coordination._connect()
    con.execute("DROP TABLE assignments")
    con.commit()
    con.close()
    assert _coordination_check()["reason"] == store_schema.REASON_FINGERPRINT_MISMATCH

    con = coordination._connect()
    con.close()
    assert _coordination_check()["reason"] == store_schema.REASON_COMPATIBLE


def test_user_version_nonzero_is_future_schema(isolated_roots):
    """Lead R2 counter-example (1): user_version=999 + full columns → future_schema."""
    path = runtime_paths.store("tasks")
    _mk_tasks_db(path, user_version=999)
    r = _tasks_check()
    assert r["reason"] == store_schema.REASON_FUTURE_SCHEMA
    assert r["state"] == "future"


def test_future_column_fail_closed(isolated_roots):
    path = runtime_paths.store("tasks")
    _mk_tasks_db(path, extra_col="evil_future")
    r = _tasks_check()
    assert r["reason"] == store_schema.REASON_FUTURE_SCHEMA


def test_corrupt_sqlite(isolated_roots):
    path = runtime_paths.store("push")
    path.write_text("not-a-database", encoding="utf-8")
    r = store_schema._check_sqlite(
        "push",
        {"subscriptions": store_schema._PUSH_COLUMNS},
        expected_defaults={"subscriptions": store_schema._PUSH_DEFAULTS},
        expected_indexes={"subscriptions": store_schema._PUSH_INDEXES},
        expected_fks={"subscriptions": frozenset()},
    )
    assert r["reason"] in {
        store_schema.REASON_CORRUPT, store_schema.REASON_UNREADABLE,
    }


def test_wal_without_shm_requires_quiescence(isolated_roots):
    path = runtime_paths.store("tasks")
    _mk_tasks_db(path)
    Path(str(path) + "-wal").write_bytes(b"x" * 32)
    r = _tasks_check()
    assert r["reason"] == store_schema.REASON_PROBE_REQUIRES_QUIESCENCE


def test_wal_plus_shm_requires_quiescence_no_shm_mutation(isolated_roots):
    """OpenCode #2054: live WAL+SHM must not be opened (mode=ro mutates -shm)."""
    import hashlib
    path = runtime_paths.store("tasks")
    _mk_tasks_db(path)
    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    wal.write_bytes(b"W" * 64)
    shm.write_bytes(b"S" * 64)
    before = hashlib.sha256(shm.read_bytes()).hexdigest()
    mtime = shm.stat().st_mtime_ns
    r = _tasks_check()
    assert r["reason"] == store_schema.REASON_PROBE_REQUIRES_QUIESCENCE
    assert hashlib.sha256(shm.read_bytes()).hexdigest() == before
    assert shm.stat().st_mtime_ns == mtime


def test_settings_unknown_field_future(isolated_roots):
    path = runtime_paths.store("settings")
    path.write_text(json.dumps({
        "language": "zh", "unexpected_key": 1,
    }), encoding="utf-8")
    r = store_schema._check_settings()
    assert r["reason"] == store_schema.REASON_UNKNOWN_FIELDS


def test_settings_sparse_language_only_ok(isolated_roots):
    """Writer sparse store: {language: en} must be compatible."""
    path = runtime_paths.store("settings")
    path.write_text(json.dumps({"language": "en"}), encoding="utf-8")
    r = store_schema._check_settings()
    assert r["reason"] == store_schema.REASON_COMPATIBLE


def test_settings_empty_object_ok(isolated_roots):
    path = runtime_paths.store("settings")
    path.write_text("{}", encoding="utf-8")
    r = store_schema._check_settings()
    assert r["reason"] == store_schema.REASON_COMPATIBLE


def test_bool_version_rejected(isolated_roots):
    path = runtime_paths.store("mail_projects")
    path.write_text(json.dumps({"version": True, "sessions": {}}), encoding="utf-8")
    r = store_schema._check_versioned_json("mail_projects")
    assert r["reason"] == store_schema.REASON_FINGERPRINT_MISMATCH


def test_mail_projects_future_version(isolated_roots):
    path = runtime_paths.store("mail_projects")
    path.write_text(json.dumps({"version": 99, "sessions": {}}), encoding="utf-8")
    r = store_schema._check_versioned_json("mail_projects")
    assert r["reason"] == store_schema.REASON_FUTURE_SCHEMA


def test_mail_projects_unknown_field_future(isolated_roots):
    """Lead R2 counter-example (2): version=1 + unexpected field → future."""
    path = runtime_paths.store("mail_projects")
    path.write_text(
        json.dumps({
            "version": 1,
            "sessions": {},
            "unexpected_future_payload": True,
        }),
        encoding="utf-8",
    )
    r = store_schema._check_versioned_json("mail_projects")
    assert r["reason"] == store_schema.REASON_UNKNOWN_FIELDS
    assert r["state"] == "future"


def test_mail_projects_missing_sessions_not_compatible(isolated_roots):
    """#2054: versioned JSON missing core sessions is not compatible."""
    path = runtime_paths.store("mail_projects")
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    r = store_schema._check_versioned_json("mail_projects")
    assert r["reason"] == store_schema.REASON_FINGERPRINT_MISMATCH
    assert r["state"] != "compatible"


def test_mail_projects_entry_shape_exact(isolated_roots):
    path = runtime_paths.store("mail_projects")
    path.write_text(json.dumps({
        "version": 1,
        "sessions": {"s1": {"project": "/p", "session_dir": "/d", "extra": 1}},
    }), encoding="utf-8")
    r = store_schema._check_versioned_json("mail_projects")
    assert r["reason"] == store_schema.REASON_UNKNOWN_FIELDS


def test_team_sessions_real_binding_shape(isolated_roots):
    path = runtime_paths.store("team_sessions")
    path.write_text(json.dumps({
        "version": 1,
        "bindings": [{
            "hub": "http://h",
            "human_id": 1,
            "project_slug": "p",
            "session": "s",
            "session_generation": "g",
            "session_dir": "/tmp/s",
            "mail_project": "/tmp/p",
            "lead": {
                "pane_id": "w1:p1", "agent": "codex",
                "mail_name": "codex-main", "participant_id": "a1",
            },
            "client_session_id": "c1",
            "agent_id": 2,
            "updated_ts": 1.0,
            "reply_token": "tok",
        }],
    }), encoding="utf-8")
    r = store_schema._check_versioned_json("team_sessions")
    assert r["reason"] == store_schema.REASON_COMPATIBLE


def test_inbox_route_v2_requires_worker_migration(isolated_roots):
    path = runtime_paths.store("inbox_route")
    path.write_text(json.dumps({"version": 2, "routes": {}}), encoding="utf-8")

    result = store_schema._check_versioned_json("inbox_route")

    assert result["reason"] == store_schema.REASON_MIGRATION_REQUIRED


def test_inbox_route_v3_work_queue_exact_value_shape(isolated_roots):
    path = runtime_paths.store("inbox_route")
    path.write_text(json.dumps({
        "version": 3,
        "work_items": [{
            "work_id": "a" * 32,
            "hub": "https://team.example",
            "project_slug": "core",
            "client_session_id": "client-1",
            "session": "demo",
            "session_generation": "run-1",
            "mail_project": "/work/demo",
            "lead_mail_name": "codex-main",
            "pane_id": "pane-1",
            "reply_mode": "confirm",
            "inbox_item_id": 31,
            "claim_token": "claim-secret",
            "claim_expires_at": "2026-08-23 12:00:00",
            "message": {
                "message_id": 41,
                "subject": "subject",
                "body_md": "body",
                "importance": "normal",
                "sender_name": "Alice",
                "sender_handle": "alice",
                "created_ts": "2026-08-23 11:00:00",
            },
            "state": "pending",
            "notified": False,
            "created_ts": 1.0,
        }],
    }), encoding="utf-8")

    assert store_schema._check_versioned_json("inbox_route")["reason"] == (
        store_schema.REASON_COMPATIBLE
    )


def test_settings_invalid_payloads_never_raise(isolated_roots):
    """Lead R4 counter-example (2): no TypeError/OverflowError; NaN rejected."""
    path = runtime_paths.store("settings")
    cases = [
        {"language": []},
        {"enabled_agents": [{}]},
        {"upload_max_mb": float("inf")},
        {"term": {"idle_ttl": float("nan")}},
        {"team_hub_url": "http://example.com"},  # public HTTP rejected
    ]
    for payload in cases:
        path.write_text(json.dumps(payload), encoding="utf-8")
        r = store_schema._check_settings()
        assert r["state"] != "compatible", payload
        assert r["reason"] in {
            store_schema.REASON_FINGERPRINT_MISMATCH,
            store_schema.REASON_UNKNOWN_FIELDS,
        }


def test_required_manifest_inventory_covers_static_tree():
    """Lead R4 (3): inventory from real static tree, not hand-written 4-item set."""
    inv = store_schema.required_manifest_digest_paths()
    assert "VERSION" in inv
    assert "static/index.html" in inv
    assert "static/sw.js" in inv
    assert "static/manifest.webmanifest" in inv
    assert "static/fonts/CascadiaMono.woff2" in inv
    assert "static/fonts/CascadiaMonoNF.woff2" in inv
    assert "static/vendor/xterm/xterm.js" in inv
    assert "static/vendor/xterm/xterm.css" in inv
    assert "static/vendor/xterm/addon-fit.js" in inv
    assert "static/vendor/xterm/addon-webgl.js" in inv
    # fonts(2)+vendor(>=4 runtime)+index+sw+webmanifest+VERSION
    assert len(inv) >= 10


def test_typing_legacy_float_and_panes(isolated_roots):
    path = runtime_paths.store("typing")
    path.write_text(json.dumps({"demo": 1.5}), encoding="utf-8")
    assert store_schema._check_typing()["reason"] == store_schema.REASON_COMPATIBLE
    path.write_text(json.dumps({
        "demo": {"panes": {"w1:p1": 1.5}, "unknown": 2.0},
    }), encoding="utf-8")
    assert store_schema._check_typing()["reason"] == store_schema.REASON_COMPATIBLE


def test_vapid_garbage_pem_rejected(isolated_roots):
    path = runtime_paths.store("vapid")
    path.write_bytes(
        b"-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n"
    )
    os.chmod(path, 0o600)
    r = store_schema._check_vapid()
    assert r["reason"] == store_schema.REASON_FINGERPRINT_MISMATCH


def test_vapid_real_ec_p256_ok(isolated_roots):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    path = runtime_paths.store("vapid")
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    os.chmod(path, 0o600)
    assert store_schema._check_vapid()["reason"] == store_schema.REASON_COMPATIBLE


def test_path_gate_blocks_non_sqlite_symlink_escape(isolated_roots, monkeypatch):
    """#2054: path_gate must cover settings/etc, not only SQLite."""
    # Force inspect to report symlink_escape for settings.
    fake = {
        "ready": False,
        "stores": [
            {"name": n, "ready": True, "reason": "ok"}
            for n in (
                "tasks", "coordination", "push", "vapid", "mail_projects",
                "team_sessions", "inbox_route", "typing", "file_roots",
                "settings", "worktrees", "upgrade",
            )
        ],
    }
    for item in fake["stores"]:
        if item["name"] == "settings":
            item["ready"] = False
            item["reason"] = "symlink_escape"
    monkeypatch.setattr(runtime_paths, "inspect", lambda: fake)
    # Content would look compatible if probe ran
    path = runtime_paths.store("settings")
    path.write_text(json.dumps({
        "language": "zh", "dir_agents": {}, "enabled_agents": [],
        "upload_max_mb": 1, "team_hub_url": "", "human_auth_url": "",
        "term": {"max_terms": 1, "idle_ttl": 1, "write_timeout": 1.0},
    }), encoding="utf-8")
    results = {r["name"]: r for r in store_schema.probe_all_stores()}
    assert results["settings"]["reason"] == store_schema.REASON_UNSAFE
    assert results["settings"]["state"] == "error"


def test_file_roots_unsafe_slash(isolated_roots):
    from agent_cockpit import files
    # Must use the same path the production reader uses (conftest may redirect it).
    path = files._custom_roots_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(["/"]), encoding="utf-8")
    r = store_schema._check_file_roots()
    assert r["reason"] == store_schema.REASON_UNSAFE_ROOT


def test_manifest_source_not_applicable(monkeypatch):
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    m = store_schema.probe_manifest("source")
    assert m["reason"] == store_schema.REASON_NOT_APPLICABLE


def test_manifest_production_missing(monkeypatch):
    m = store_schema.probe_manifest("server")
    assert m["reason"] == store_schema.REASON_PRODUCTION_MANIFEST_MISSING


def test_manifest_wrong_identity_binding_fail_closed():
    """Lead R2 counter-example (3): WRONG version / not-a-sha → not compatible."""
    root = runtime_paths.INSTALL_ROOT
    man = root / "release-manifest.json"
    payload = {
        "version": "WRONG",
        "source_sha": "not-a-sha",
        "edition": "server",
        "digests": {"static/index.html": "0" * 64, "VERSION": "0" * 64},
    }
    man.write_text(json.dumps(payload), encoding="utf-8")
    try:
        identity = {
            "version": "1.2.3", "source_sha": "a" * 40,
            "edition": "server", "instance_id": "x",
        }
        m = store_schema.probe_manifest("server", identity=identity)
        assert m["reason"] == store_schema.REASON_FINGERPRINT_MISMATCH
        assert m["state"] != "compatible"
        m2 = store_schema.probe_manifest("server")
        assert m2["reason"] == store_schema.REASON_FINGERPRINT_MISMATCH
    finally:
        man.unlink(missing_ok=True)


def test_evaluate_ready_source_empty_profile(isolated_roots, monkeypatch):
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    identity = {
        "version": "0.0.0-test",
        "source_sha": "unknown",
        "edition": "source",
        "instance_id": "abc",
        "pid": os.getpid(),
    }
    body = store_schema.evaluate_ready(identity)
    assert body["ready"] is True
    assert body["status"] == "ready"
    blob = json.dumps(body)
    assert str(isolated_roots["data"]) not in blob
    assert "dashboard-data" not in blob or body["ready"]


def test_health_ready_endpoint_200(isolated_roots, monkeypatch):
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    monkeypatch.setenv("COCKPIT_TOKEN", "")
    # Import app after env set
    import importlib
    import server
    runtime_paths.reset_cache()
    importlib.reload(store_schema)
    importlib.reload(server)
    client = TestClient(
        server.app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    )
    resp = client.get("/health/ready")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ready"
    assert data["ready"] is True
    text = resp.text
    assert str(isolated_roots["data"]) not in text
    assert "Traceback" not in text


def test_health_ready_production_manifest_503(isolated_roots, monkeypatch):
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", "a" * 40)
    import importlib
    from agent_cockpit import release_identity
    import server
    release_identity._cached = None  # noqa: SLF001
    runtime_paths.reset_cache()
    importlib.reload(store_schema)
    importlib.reload(server)
    client = TestClient(
        server.app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    )
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    detail = resp.json().get("detail", resp.json())
    assert detail["status"] == "not_ready"
    reasons = {s["reason"] for s in detail["stores"]}
    assert store_schema.REASON_PRODUCTION_MANIFEST_MISSING in reasons


def test_health_degraded_still_200(monkeypatch):
    import importlib
    import server
    importlib.reload(server)
    client = TestClient(
        server.app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    )
    # Even if herdr unavailable, /health stays 200
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] in {"ok", "degraded"}


def test_no_server_import_in_store_schema():
    src = Path(store_schema.__file__).read_text(encoding="utf-8")
    assert "import server" not in src
    assert "from server" not in src


def test_delivery_outbox_future_column_rejected(isolated_roots):
    path = runtime_paths.store("delivery_outbox")
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE delivery_jobs ("
        "job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, "
        "job_kind TEXT NOT NULL, target TEXT NOT NULL, "
        "payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, "
        "attempt INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending', created_ts REAL NOT NULL, "
        "updated_ts REAL NOT NULL, last_error_summary TEXT, evil_future TEXT)"
    )
    con.close()
    os.chmod(path, 0o600)
    r = store_schema._check_sqlite(
        "delivery_outbox",
        {"delivery_jobs": store_schema._DELIVERY_OUTBOX_COLUMNS},
        expected_defaults=store_schema._DELIVERY_OUTBOX_DEFAULTS,
        expected_indexes=store_schema._DELIVERY_OUTBOX_INDEXES,
        expected_fks=store_schema._DELIVERY_OUTBOX_FKS,
    )
    assert r["reason"] == store_schema.REASON_FUTURE_SCHEMA
    assert r["state"] == "future"


def test_delivery_outbox_missing_column_mismatch(isolated_roots):
    path = runtime_paths.store("delivery_outbox")
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE delivery_jobs ("
        "job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, "
        "job_kind TEXT NOT NULL, target TEXT NOT NULL, payload_json TEXT NOT NULL)"
    )
    con.close()
    os.chmod(path, 0o600)
    r = store_schema._check_sqlite(
        "delivery_outbox",
        {"delivery_jobs": store_schema._DELIVERY_OUTBOX_COLUMNS},
        expected_defaults=store_schema._DELIVERY_OUTBOX_DEFAULTS,
        expected_indexes=store_schema._DELIVERY_OUTBOX_INDEXES,
        expected_fks=store_schema._DELIVERY_OUTBOX_FKS,
    )
    assert r["reason"] == store_schema.REASON_FINGERPRINT_MISMATCH


def test_delivery_outbox_legacy_migration_then_compatible(
    isolated_roots, monkeypatch,
):
    from agent_cockpit import delivery_outbox

    path = runtime_paths.store("delivery_outbox")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE delivery_jobs (
          job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL,
          job_kind TEXT NOT NULL, target TEXT NOT NULL,
          payload_json TEXT NOT NULL, created_ts REAL NOT NULL
        );
        INSERT INTO delivery_jobs VALUES(
          'legacy-job', 'legacy-key', 'send_message', 'project-a/agent-b',
          '{"subject": "old"}', 12.5
        );
        """
    )
    con.close()
    os.chmod(path, 0o600)
    monkeypatch.setattr(delivery_outbox, "DB_PATH", path)
    job = delivery_outbox.get_job("legacy-job")
    assert job is not None
    assert job["payload"] == {"subject": "old"}
    result = {
        r["name"]: r for r in store_schema.probe_all_stores()
    }["delivery_outbox"]
    assert result["reason"] == store_schema.REASON_COMPATIBLE


def test_delivery_outbox_legacy_migration_failure_atomic(
    isolated_roots, monkeypatch,
):
    from agent_cockpit import delivery_outbox

    path = runtime_paths.store("delivery_outbox")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE delivery_jobs (
          job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL,
          job_kind TEXT NOT NULL, target TEXT NOT NULL,
          payload_json TEXT NOT NULL, created_ts REAL NOT NULL
        );
        INSERT INTO delivery_jobs VALUES(
          'bad', 'k', 'send_message', 't', 'not-json', 12.5
        );
        """
    )
    con.close()
    os.chmod(path, 0o600)
    monkeypatch.setattr(delivery_outbox, "DB_PATH", path)
    with pytest.raises(json.JSONDecodeError):
        delivery_outbox.get_job("bad")
    con = sqlite3.connect(path)
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    cols = {r[1] for r in con.execute("PRAGMA table_info(delivery_jobs)")}
    con.close()
    assert "delivery_jobs" in tables
    assert "delivery_jobs_new" not in tables
    assert cols == {
        "job_id", "idempotency_key", "job_kind", "target",
        "payload_json", "created_ts",
    }
