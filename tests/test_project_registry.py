from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_cockpit import project_registry_contracts as contracts
from agent_cockpit import project_registry_store as registry_store


def _code(exc_info: pytest.ExceptionInfo[BaseException]) -> str | None:
    return getattr(exc_info.value, "code", None)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "project-registry.sqlite3"


@pytest.fixture()
def registry(db_path: Path):
    return registry_store.initialize(db_path)


def test_initialize_creates_strict_v1_schema_and_mode(db_path: Path):
    registry_store.initialize(db_path).close()

    assert db_path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        receipt = connection.execute(
            "SELECT migration_id, schema_version, schema_digest "
            "FROM schema_migrations"
        ).fetchone()
    assert receipt == (
        contracts.MIGRATION_ID,
        contracts.SCHEMA_VERSION,
        contracts.SCHEMA_DIGEST,
    )
    assert receipt == contracts.PROJECT_REGISTRY_MIGRATION_RECEIPT
    with sqlite3.connect(db_path) as connection:
        triggers = frozenset(
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        )
    assert triggers == contracts.PROJECT_REGISTRY_TRIGGERS


def test_active_location_uniqueness_is_node_path_pair(registry):
    first = registry.create_project(slug="alpha", display_name="Alpha", goal=None)
    second = registry.create_project(slug="beta", display_name="Beta", goal=None)
    registry.add_repo_location(
        project_id=first.project_id,
        node_id="local",
        canonical_path="/repo/a",
        vcs_kind="none",
        availability="available",
    )
    registry.add_repo_location(
        project_id=first.project_id,
        node_id="local",
        canonical_path="/repo/b",
        vcs_kind="none",
        availability="available",
    )
    registry.add_repo_location(
        project_id=second.project_id,
        node_id="remote-1",
        canonical_path="/repo/a",
        vcs_kind="git",
        availability="offline",
    )

    with pytest.raises(registry_store.ProjectRegistryError) as conflict:
        registry.add_repo_location(
            project_id=second.project_id,
            node_id="local",
            canonical_path="/repo/a",
            vcs_kind="none",
            availability="missing",
        )
    assert _code(conflict) == "location_already_registered"


@pytest.mark.parametrize("availability", ["missing", "offline"])
def test_archived_location_releases_slot_but_unavailable_does_not(
    registry, db_path, availability: str
):
    first = registry.create_project(slug="alpha", display_name="Alpha", goal=None)
    second = registry.create_project(slug="beta", display_name="Beta", goal=None)
    old = registry.add_repo_location(
        project_id=first.project_id,
        node_id="local",
        canonical_path="/repo/shared",
        vcs_kind="none",
        availability="available",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE repo_locations SET availability=? "
            "WHERE repo_location_id=?",
            (availability, old.repo_location_id),
        )
    with pytest.raises(registry_store.ProjectRegistryError) as unavailable:
        registry.add_repo_location(
            project_id=second.project_id,
            node_id="local",
            canonical_path="/repo/shared",
            vcs_kind="none",
            availability="available",
        )
    assert _code(unavailable) == "location_already_registered"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE repo_locations SET lifecycle='archived' "
            "WHERE repo_location_id=?",
            (old.repo_location_id,),
        )
    replacement = registry.add_repo_location(
        project_id=second.project_id,
        node_id="local",
        canonical_path="/repo/shared",
        vcs_kind="none",
        availability="available",
    )
    assert replacement.repo_location_id != old.repo_location_id

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE repo_locations SET lifecycle='active' "
                "WHERE repo_location_id=?",
                (old.repo_location_id,),
            )


def test_concurrent_registration_creates_one_active_location(registry, db_path):
    first = registry.create_project(slug="alpha", display_name="Alpha", goal=None)
    second = registry.create_project(slug="beta", display_name="Beta", goal=None)

    def register(project_id: str):
        try:
            return registry.add_repo_location(
                project_id=project_id,
                node_id="local",
                canonical_path="/repo/race",
                vcs_kind="none",
                availability="available",
            ).repo_location_id
        except registry_store.ProjectRegistryError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(register, (first.project_id, second.project_id)))
    assert outcomes.count("location_already_registered") == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM repo_locations WHERE lifecycle='active'"
        ).fetchone()[0] == 1


def test_slug_is_immutable_and_never_reused(registry, db_path):
    project = registry.create_project(slug="stable-slug", display_name="Stable", goal=None)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE projects SET lifecycle='archived' WHERE project_id=?",
            (project.project_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE projects SET slug='changed' WHERE project_id=?",
                (project.project_id,),
            )

    with pytest.raises(registry_store.ProjectRegistryError) as conflict:
        registry.create_project(slug="stable-slug", display_name="Again", goal=None)
    assert _code(conflict) == "project_slug_conflict"


def test_aggregate_rows_reject_physical_delete(registry, db_path: Path):
    project = registry.create_project(
        slug="durable", display_name="Durable", goal=None
    )
    location = registry.add_repo_location(
        project_id=project.project_id,
        node_id="local",
        canonical_path="/repo/durable",
        vcs_kind="none",
        availability="available",
    )
    workspace = registry.create_workspace(
        project_id=project.project_id,
        repo_location_id=location.repo_location_id,
        name="durable",
        goal=None,
        isolation_kind="shared",
    )

    with sqlite3.connect(db_path) as connection:
        for table, identity, value in (
            ("workspaces", "workspace_id", workspace.workspace_id),
            ("repo_locations", "repo_location_id", location.repo_location_id),
            ("projects", "project_id", project.project_id),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"DELETE FROM {table} WHERE {identity}=?", (value,)
                )


@pytest.mark.parametrize(
    ("table", "identity", "column", "replacement"),
    [
        ("projects", "project_id", "project_id", "prj_" + "f" * 32),
        ("projects", "project_id", "slug", "rebound"),
        (
            "repo_locations", "repo_location_id", "repo_location_id",
            "loc_" + "f" * 32,
        ),
        ("repo_locations", "repo_location_id", "project_id", "prj_" + "f" * 32),
        ("repo_locations", "repo_location_id", "node_id", "other-node"),
        ("repo_locations", "repo_location_id", "canonical_path", "/repo/rebound"),
        ("workspaces", "workspace_id", "workspace_id", "ws_" + "f" * 32),
        ("workspaces", "workspace_id", "project_id", "prj_" + "f" * 32),
        (
            "workspaces", "workspace_id", "repo_location_id",
            "loc_" + "f" * 32,
        ),
    ],
)
def test_aggregate_identity_and_ownership_reject_direct_sql_rebinding(
    registry,
    db_path: Path,
    table: str,
    identity: str,
    column: str,
    replacement: str,
):
    project = registry.create_project(
        slug="identity", display_name="Identity", goal=None
    )
    location = registry.add_repo_location(
        project_id=project.project_id,
        node_id="local",
        canonical_path="/repo/identity",
        vcs_kind="none",
        availability="available",
    )
    workspace = registry.create_workspace(
        project_id=project.project_id,
        repo_location_id=location.repo_location_id,
        name="identity",
        goal=None,
        isolation_kind="shared",
    )
    row_identity = {
        "projects": project.project_id,
        "repo_locations": location.repo_location_id,
        "workspaces": workspace.workspace_id,
    }[table]

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE {table} SET {column}=? WHERE {identity}=?",
                (replacement, row_identity),
            )


@pytest.mark.parametrize(
    ("table", "update_sql", "delete_sql"),
    [
        (
            "schema_migrations",
            "UPDATE schema_migrations SET schema_digest='" + "0" * 64 + "'",
            "DELETE FROM schema_migrations",
        ),
        (
            "legacy_project_bindings",
            "UPDATE legacy_project_bindings SET source_digest='sha256:forged'",
            "DELETE FROM legacy_project_bindings",
        ),
        (
            "idempotency_records",
            "UPDATE idempotency_records SET status_code=204",
            "DELETE FROM idempotency_records",
        ),
    ],
)
def test_ledgers_reject_update_and_delete(
    registry, db_path: Path, table: str, update_sql: str, delete_sql: str
):
    project = registry.create_project(
        slug="ledger", display_name="Ledger", goal=None
    )
    registry.bind_legacy_source(
        project_id=project.project_id,
        source_kind="agent_mail_project",
        source_key="project:ledger",
        source_digest="sha256:original",
    )
    registry.idempotent_create_project(
        scope="project.create",
        idempotency_key="ledger",
        payload={"slug": "ledger-created", "display_name": "Ledger Created"},
    )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
        with pytest.raises(
            sqlite3.IntegrityError,
            match=f"^{table}_update_forbidden$",
        ):
            connection.execute(update_sql)
        with pytest.raises(
            sqlite3.IntegrityError,
            match=f"^{table}_delete_forbidden$",
        ):
            connection.execute(delete_sql)


@pytest.mark.parametrize(
    ("table", "column", "malformed"),
    [
        ("projects", "project_id", "prj_a" + "Z" * 31),
        ("repo_locations", "repo_location_id", "loc_a" + "Z" * 31),
        ("workspaces", "workspace_id", "ws_a" + "Z" * 31),
        ("legacy_project_bindings", "binding_id", "bnd_a" + "Z" * 31),
    ],
)
def test_opaque_id_checks_reject_non_hex_suffix(
    registry, db_path: Path, table: str, column: str, malformed: str
):
    project = registry.create_project(slug="alpha", display_name="Alpha", goal=None)
    location = registry.add_repo_location(
        project_id=project.project_id,
        node_id="local",
        canonical_path="/repo/a",
        vcs_kind="none",
        availability="available",
    )
    statements = {
        "projects": (
            "INSERT INTO projects VALUES (?, 'beta', 'Beta', NULL, 'active', 1, "
            "'2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')",
            (malformed,),
        ),
        "repo_locations": (
            "INSERT INTO repo_locations VALUES (?, ?, 'local', '/repo/b', 'active', "
            "'none', NULL, NULL, NULL, 'available', 1, "
            "'2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')",
            (malformed, project.project_id),
        ),
        "workspaces": (
            "INSERT INTO workspaces VALUES (?, ?, ?, 'bad-id', NULL, 'shared', "
            "'active', NULL, 1, '2026-08-13T00:00:00Z', "
            "'2026-08-13T00:00:00Z')",
            (malformed, project.project_id, location.repo_location_id),
        ),
        "legacy_project_bindings": (
            "INSERT INTO legacy_project_bindings VALUES (?, ?, "
            "'agent_mail_project', 'bad-id', 'sha256:x', '2026-08-13T00:00:00Z')",
            (malformed, project.project_id),
        ),
    }
    sql, params = statements[table]
    assert column.endswith("_id")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(sql, params)


def test_database_checks_reject_null_ids_and_path_aliases(registry, db_path):
    project = registry.create_project(slug="alpha", display_name="Alpha", goal=None)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO projects VALUES (NULL, 'null-id', 'Null', NULL, "
                "'active', 1, '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')"
            )
        for path in ("/repo//a", "/repo/./a", "/repo/../a", "/repo/.."):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO repo_locations VALUES "
                    "(?, ?, 'local', ?, 'active', 'none', NULL, NULL, NULL, "
                    "'available', 1, '2026-08-13T00:00:00Z', "
                    "'2026-08-13T00:00:00Z')",
                    ("loc_" + secrets.token_hex(16), project.project_id, path),
                )


def test_workspace_requires_location_from_same_project(registry, db_path):
    first = registry.create_project(slug="alpha", display_name="Alpha", goal=None)
    second = registry.create_project(slug="beta", display_name="Beta", goal=None)
    location = registry.add_repo_location(
        project_id=first.project_id,
        node_id="local",
        canonical_path="/repo/a",
        vcs_kind="none",
        availability="available",
    )
    with pytest.raises(registry_store.ProjectRegistryError) as mismatch:
        registry.create_workspace(
            project_id=second.project_id,
            repo_location_id=location.repo_location_id,
            name="escape",
            goal=None,
            isolation_kind="shared",
        )
    assert _code(mismatch) == "repo_location_not_found"

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO workspaces "
                "(workspace_id, project_id, repo_location_id, name, goal, "
                "isolation_kind, lifecycle, active_run_id, version, "
                "created_at, updated_at) VALUES "
                "('ws_00000000000000000000000000000000', ?, ?, 'escape', NULL, "
                "'shared', 'active', NULL, 1, '2026-08-13T00:00:00Z', "
                "'2026-08-13T00:00:00Z')",
                (second.project_id, location.repo_location_id),
            )


def test_legacy_provenance_is_authority_scoped_and_idempotent(registry):
    first = registry.create_project(slug="alpha", display_name="Alpha", goal=None)
    second = registry.create_project(slug="beta", display_name="Beta", goal=None)
    original = registry.bind_legacy_source(
        project_id=first.project_id,
        source_kind="agent_mail_project",
        source_key="project:17",
        source_digest="sha256:a",
    )
    replay = registry.bind_legacy_source(
        project_id=first.project_id,
        source_kind="agent_mail_project",
        source_key="project:17",
        source_digest="sha256:a",
    )
    assert replay == original
    registry.bind_legacy_source(
        project_id=first.project_id,
        source_kind="coordination_run",
        source_key="project:17",
        source_digest="sha256:b",
    )
    with pytest.raises(registry_store.ProjectRegistryError) as conflict:
        registry.bind_legacy_source(
            project_id=second.project_id,
            source_kind="agent_mail_project",
            source_key="project:17",
            source_digest="sha256:c",
        )
    assert _code(conflict) == "legacy_binding_conflict"


def test_idempotency_replay_and_conflict_are_atomic(registry, db_path):
    payload = {"slug": "alpha", "display_name": "Alpha"}
    first = registry.idempotent_create_project(
        scope="project.create", idempotency_key="same", payload=payload
    )
    assert registry.idempotent_create_project(
        scope="project.create", idempotency_key="same", payload=payload
    ) == first
    with pytest.raises(registry_store.ProjectRegistryError) as conflict:
        registry.idempotent_create_project(
            scope="project.create",
            idempotency_key="same",
            payload={"slug": "beta"},
        )
    assert _code(conflict) == "idempotency_conflict"

    registry.create_project(slug="taken", display_name="Taken", goal=None)
    with pytest.raises(registry_store.ProjectRegistryError) as duplicate:
        registry.idempotent_create_project(
            scope="project.create",
            idempotency_key="failed",
            payload={"slug": "taken"},
        )
    assert _code(duplicate) == "project_slug_conflict"
    with sqlite3.connect(db_path) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "idempotency_records")
        }
    assert counts == {"projects": 2, "idempotency_records": 1}


def test_validation_rejects_unknown_enums_and_noncanonical_identity(registry):
    with pytest.raises(registry_store.ProjectRegistryError) as bad_slug:
        registry.create_project(slug="Not Normal", display_name="Name", goal=None)
    assert _code(bad_slug) == "invalid_argument"
    project = registry.create_project(slug="alpha", display_name="Alpha", goal=None)
    with pytest.raises(registry_store.ProjectRegistryError) as bad_path:
        registry.add_repo_location(
            project_id=project.project_id,
            node_id="local",
            canonical_path="relative/repo",
            vcs_kind="none",
            availability="available",
        )
    assert _code(bad_path) == "invalid_argument"
    with pytest.raises(registry_store.ProjectRegistryError) as bad_enum:
        registry.add_repo_location(
            project_id=project.project_id,
            node_id="local",
            canonical_path="/repo/a",
            vcs_kind="svn",
            availability="available",
        )
    assert _code(bad_enum) == "invalid_argument"


@pytest.mark.parametrize(
    ("version", "expected"),
    [(0, "migration_required"), (999, "future_schema")],
)
def test_open_existing_versions_are_read_only(
    db_path: Path, version: int, expected: str
):
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute(f"PRAGMA user_version={version}")
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    with pytest.raises(registry_store.ProjectRegistryError) as failure:
        registry_store.open_existing(db_path)
    assert _code(failure) == expected
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


def test_open_existing_rejects_unknown_current_schema_without_writes(db_path: Path):
    registry_store.initialize(db_path).close()
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE unknown_extension(value TEXT)")
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    with pytest.raises(registry_store.ProjectRegistryError) as failure:
        registry_store.open_existing(db_path)
    assert _code(failure) == "schema_fingerprint_mismatch"
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


@pytest.mark.parametrize(
    "tamper_statements",
    [
        (
            "DROP TRIGGER schema_migrations_update_forbidden",
            "UPDATE schema_migrations SET schema_digest='" + "0" * 64 + "'",
        ),
        (
            "CREATE TRIGGER unknown_trigger AFTER INSERT ON projects "
            "BEGIN SELECT 1; END",
        ),
        ("CREATE INDEX unknown_index ON projects(display_name)",),
    ],
)
def test_open_existing_rejects_ledger_and_schema_fingerprint_tampering(
    db_path: Path, tamper_statements: tuple[str, ...]
):
    registry_store.initialize(db_path).close()
    with sqlite3.connect(db_path) as connection:
        for statement in tamper_statements:
            connection.execute(statement)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    with pytest.raises(registry_store.ProjectRegistryError) as failure:
        registry_store.open_existing(db_path)
    assert _code(failure) == "schema_fingerprint_mismatch"
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_initialize_failure_rolls_back_schema_and_ledger(db_path: Path, monkeypatch):
    monkeypatch.setattr(registry_store, "_after_schema_hook", lambda _connection: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        registry_store.initialize(db_path)
    assert not db_path.exists()
    assert not Path(f"{db_path}-journal").exists()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()

    monkeypatch.setattr(registry_store, "_after_schema_hook", lambda _connection: None)
    registry_store.initialize(db_path).close()
    registry_store.open_existing(db_path).close()


@pytest.mark.parametrize("version", [0, 999])
def test_initialize_never_deletes_or_mutates_existing_versioned_store(
    db_path: Path, version: int
):
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute(f"PRAGMA user_version={version}")
    os.chmod(db_path, 0o600)
    before = db_path.read_bytes()

    with pytest.raises(registry_store.ProjectRegistryError):
        registry_store.initialize(db_path)
    assert db_path.read_bytes() == before


def test_open_missing_and_unsafe_store_fail_closed(db_path: Path):
    with pytest.raises(registry_store.ProjectRegistryError) as missing:
        registry_store.open_existing(db_path)
    assert _code(missing) == "schema_missing"

    registry_store.initialize(db_path).close()
    os.chmod(db_path, 0o644)
    with pytest.raises(registry_store.ProjectRegistryError) as unsafe:
        registry_store.open_existing(db_path)
    assert _code(unsafe) == "store_unsafe"


def test_initialize_rejects_ancestor_and_direct_symlinks(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    ancestor_link = tmp_path / "linked-parent"
    ancestor_link.symlink_to(real, target_is_directory=True)
    through_ancestor = ancestor_link / "nested" / "project-registry.sqlite3"

    with pytest.raises(registry_store.ProjectRegistryError) as ancestor:
        registry_store.initialize(through_ancestor)
    assert _code(ancestor) == "store_unsafe"
    assert not (real / "nested").exists()

    target = real / "target.sqlite3"
    target.write_bytes(b"do-not-touch")
    os.chmod(target, 0o600)
    direct_link = tmp_path / "project-registry.sqlite3"
    direct_link.symlink_to(target)
    with pytest.raises(registry_store.ProjectRegistryError) as direct:
        registry_store.initialize(direct_link)
    assert _code(direct) == "store_unsafe"
    assert target.read_bytes() == b"do-not-touch"


def test_initialize_rejects_unsafe_parent_mode_without_writing(tmp_path: Path):
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o777)
    path = parent / "project-registry.sqlite3"

    with pytest.raises(registry_store.ProjectRegistryError) as unsafe:
        registry_store.initialize(path)
    assert _code(unsafe) == "store_unsafe"
    assert not path.exists()


def test_initialize_rejects_parent_alias_without_writing(tmp_path: Path):
    path = tmp_path / "private" / ".." / "project-registry.sqlite3"
    with pytest.raises(registry_store.ProjectRegistryError) as unsafe:
        registry_store.initialize(path)
    assert _code(unsafe) == "store_unsafe"
    assert not (tmp_path / "project-registry.sqlite3").exists()


def test_initialize_rejects_parent_and_existing_leaf_owner_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    parent_path = tmp_path / "parent-owner" / "project-registry.sqlite3"
    fake_uid = os.getuid() + 1
    monkeypatch.setattr(registry_store.os, "getuid", lambda: fake_uid)
    with pytest.raises(registry_store.ProjectRegistryError) as parent:
        registry_store.initialize(parent_path)
    assert _code(parent) == "store_unsafe"
    assert not parent_path.exists()

    monkeypatch.undo()
    existing = tmp_path / "existing.sqlite3"
    registry_store.initialize(existing).close()
    before = existing.read_bytes()
    monkeypatch.setattr(registry_store.os, "getuid", lambda: fake_uid)
    with pytest.raises(registry_store.ProjectRegistryError) as leaf:
        registry_store.open_existing(existing)
    assert _code(leaf) == "store_unsafe"
    assert existing.read_bytes() == before


def test_initialize_creates_private_parent_chain_and_reopens_existing_leaf(
    tmp_path: Path,
):
    first = tmp_path / "private-a"
    second = first / "private-b"
    path = second / "project-registry.sqlite3"
    registry_store.initialize(path).close()

    assert first.stat().st_mode & 0o777 == 0o700
    assert second.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    registry_store.open_existing(path).close()


def test_transaction_begin_failure_closes_connection_and_store_recovers(
    registry, db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    real_connect = registry_store._connect_write
    captured: list[sqlite3.Connection] = []

    def capture_connection(path: Path) -> sqlite3.Connection:
        connection = real_connect(path)
        connection.execute("PRAGMA busy_timeout=1")
        captured.append(connection)
        return connection

    monkeypatch.setattr(registry_store, "_connect_write", capture_connection)
    with pytest.raises(sqlite3.OperationalError):
        registry.create_project(slug="locked", display_name="Locked", goal=None)
    assert len(captured) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        captured[0].execute("SELECT 1")

    blocker.execute("ROLLBACK")
    blocker.close()
    project = registry.create_project(slug="recovered", display_name="Recovered", goal=None)
    assert project.slug == "recovered"


@pytest.mark.parametrize("connector", ["_connect_write", "_connect_read"])
def test_post_connect_setup_failure_closes_connection(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, connector: str
):
    registry_store.initialize(db_path).close()
    real_connect = registry_store.sqlite3.connect
    wrappers = []

    class FailingConnection:
        def __init__(self, connection):
            object.__setattr__(self, "connection", connection)
            object.__setattr__(self, "closed", False)

        def __setattr__(self, name, value):
            if name in {"connection", "closed"}:
                object.__setattr__(self, name, value)
            else:
                setattr(self.connection, name, value)

        def execute(self, _sql, _parameters=()):
            raise RuntimeError("injected setup failure")

        def close(self):
            object.__setattr__(self, "closed", True)
            self.connection.close()

    def failing_connect(*args, **kwargs):
        wrapper = FailingConnection(real_connect(*args, **kwargs))
        wrappers.append(wrapper)
        return wrapper

    monkeypatch.setattr(registry_store.sqlite3, "connect", failing_connect)
    with pytest.raises(RuntimeError, match="injected setup failure"):
        getattr(registry_store, connector)(db_path)
    assert len(wrappers) == 1
    assert wrappers[0].closed is True
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        wrappers[0].connection.execute("SELECT 1")


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_initialize_preserves_preexisting_sidecar(
    tmp_path: Path, suffix: str
):
    path = tmp_path / "project-registry.sqlite3"
    sidecar = Path(f"{path}{suffix}")
    sidecar.write_bytes(b"preexisting-sidecar")
    os.chmod(sidecar, 0o600)
    before = sidecar.lstat()

    with pytest.raises(registry_store.ProjectRegistryError) as unsafe:
        registry_store.initialize(path)
    assert _code(unsafe) == "store_unsafe"
    after = sidecar.lstat()
    assert (
        after.st_dev, after.st_ino, after.st_uid, after.st_mode,
        after.st_nlink, after.st_size,
    ) == (
        before.st_dev, before.st_ino, before.st_uid, before.st_mode,
        before.st_nlink, before.st_size,
    )
    assert sidecar.read_bytes() == b"preexisting-sidecar"
    assert not path.exists()


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_connect_failure_preserves_preexisting_empty_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
):
    path = tmp_path / "project-registry.sqlite3"
    sidecar = Path(f"{path}{suffix}")
    sidecar.write_bytes(b"")
    os.chmod(sidecar, 0o600)
    before = sidecar.lstat()

    def fail_connect(_path: Path):
        raise RuntimeError("injected connection setup failure")

    monkeypatch.setattr(registry_store, "_connect_write", fail_connect)
    with pytest.raises(RuntimeError, match="injected connection setup failure"):
        registry_store.initialize(path)
    after = sidecar.lstat()
    assert (
        after.st_dev, after.st_ino, after.st_uid, after.st_mode,
        after.st_nlink, after.st_size,
    ) == (
        before.st_dev, before.st_ino, before.st_uid, before.st_mode,
        before.st_nlink, before.st_size,
    )
    assert not path.exists()


def test_connect_failure_never_removes_concurrently_created_final_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "project-registry.sqlite3"
    replacement = b"concurrent-final-leaf"

    def fail_after_replacement(_temp_path: Path):
        path.write_bytes(replacement)
        os.chmod(path, 0o600)
        raise RuntimeError("injected connection setup failure")

    monkeypatch.setattr(registry_store, "_connect_write", fail_after_replacement)
    with pytest.raises(RuntimeError, match="injected connection setup failure"):
        registry_store.initialize(path)
    assert path.read_bytes() == replacement
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", [0o400, 0o700, 0o644])
def test_existing_registry_requires_exact_0600_mode(db_path: Path, mode: int):
    registry_store.initialize(db_path).close()
    before = db_path.read_bytes()
    os.chmod(db_path, mode)

    with pytest.raises(registry_store.ProjectRegistryError) as unsafe:
        registry_store.open_existing(db_path)
    assert _code(unsafe) == "store_unsafe"
    assert db_path.read_bytes() == before
    assert db_path.stat().st_mode & 0o777 == mode


def test_initialize_crash_before_temp_connect_never_publishes_partial_store(
    tmp_path: Path,
):
    path = tmp_path / "project-registry.sqlite3"
    script = "\n".join((
        "import os",
        "from pathlib import Path",
        "from agent_cockpit import project_registry_store as store",
        "store._connect_write = lambda _path: os._exit(73)",
        f"store.initialize(Path({str(path)!r}))",
    ))
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )

    assert result.returncode == 73
    assert not path.exists()
    registry_store.initialize(path).close()
    registry_store.open_existing(path).close()


def test_concurrent_initialize_publishes_one_complete_registry(tmp_path: Path):
    path = tmp_path / "project-registry.sqlite3"

    def initialize_once(_index: int) -> None:
        registry_store.initialize(path).close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(initialize_once, range(2)))

    registry_store.open_existing(path).close()
    assert not Path(f"{path}-journal").exists()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()
