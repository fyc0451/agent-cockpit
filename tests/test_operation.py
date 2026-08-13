from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import operation_api as api
from agent_cockpit import operation_store as store_module


def _sha(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


@pytest.fixture()
def store(tmp_path: Path):
    value = store_module.initialize(tmp_path / "operation.sqlite3")
    yield value
    value.close()


def _create(store, *, key: str = "request-1", request: object | None = None):
    return store.create_operation(
        scope="project:prj_1",
        idempotency_key=key,
        request={"goal": "deploy", "options": {"safe": True}} if request is None else request,
        kind="workspace.create",
        project_id="prj_1",
        workspace_id="ws_1",
        subject_type="workspace",
        subject_id="ws_1",
        plan_digest=_sha("plan"),
        approval_required=False,
        preconditions=(
            store_module.Precondition(
                "runtime.generation", "runtime", "local",
                expected_revision=7,
                expected_generation="generation_9",
                expected_epoch="epoch_3",
                expected_digest=_sha("runtime"),
            ),
        ),
        steps=(
            store_module.Step("allocate", "runtime.allocate", "runtime.release"),
            store_module.Step("persist", "workspace.persist"),
        ),
    )


def _error(code: str):
    return pytest.raises(store_module.OperationError, match=f"^{code}$")


def test_initialize_is_private_strict_and_read_does_not_create(tmp_path: Path):
    root = tmp_path / "private"
    path = root / "operation.sqlite3"
    with _error("schema_missing"):
        store_module.open_existing(path)
    assert not root.exists()

    store_module.initialize(path)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_nlink == 1
    assert not any(Path(f"{path}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        receipt = connection.execute(
            "SELECT migration_id,schema_version,schema_digest FROM schema_migrations"
        ).fetchone()
        assert receipt == (
            store_module.MIGRATION_ID,
            store_module.SCHEMA_VERSION,
            store_module.SCHEMA_DIGEST,
        )
    finally:
        connection.close()
@pytest.mark.parametrize("path_kind", ["relative", "parent", "leaf", "mode", "hardlink"])
def test_store_path_invalid_input_fails_closed(tmp_path: Path, path_kind: str):
    if path_kind == "relative":
        with _error("store_unsafe"):
            store_module.initialize(Path("operation.sqlite3"))
        return
    if path_kind == "parent":
        target = tmp_path / "target"
        target.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(target, target_is_directory=True)
        with _error("store_unsafe"):
            store_module.initialize(alias / "operation.sqlite3")
        return
    path = tmp_path / "operation.sqlite3"
    store_module.initialize(path)
    if path_kind == "leaf":
        target = tmp_path / "target.sqlite3"
        path.rename(target)
        path.symlink_to(target)
    elif path_kind == "mode":
        path.chmod(0o644)
    else:
        os.link(path, tmp_path / "second.sqlite3")
    with _error("store_unsafe"):
        store_module.open_existing(path)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda connection: connection.execute("CREATE TABLE drift(x TEXT) STRICT"), "schema_fingerprint_mismatch"),
        (lambda connection: connection.execute("PRAGMA user_version=0"), "migration_required"),
        (lambda connection: connection.execute("PRAGMA user_version=2"), "future_schema"),
        (lambda connection: connection.execute(
            "UPDATE schema_migrations SET schema_digest='sha256:broken'"
        ), "schema_fingerprint_mismatch"),
    ],
)
def test_schema_drift_is_read_only_and_sanitized(tmp_path: Path, mutation, code: str):
    path = tmp_path / "operation.sqlite3"
    store_module.initialize(path)
    connection = sqlite3.connect(path)
    try:
        receipt_mutation = any(
            isinstance(value, str) and value.startswith("UPDATE schema_migrations")
            for value in mutation.__code__.co_consts
        )
        if receipt_mutation:
            connection.execute("DROP TRIGGER schema_migrations_no_update")
        mutation(connection)
        if receipt_mutation:
            connection.execute("""
                CREATE TRIGGER schema_migrations_no_update
                BEFORE UPDATE ON schema_migrations
                BEGIN SELECT RAISE(ABORT, 'append_only'); END
            """)
        connection.commit()
    finally:
        connection.close()
    before = path.read_bytes()
    with _error(code):
        store_module.open_existing(path)
    assert path.read_bytes() == before
    assert not any(Path(f"{path}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))


def test_cached_store_revalidates_schema_before_every_read(tmp_path: Path):
    path = tmp_path / "operation.sqlite3"
    store = store_module.initialize(path)
    operation_id = _create(store).operation_id
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE late_drift(x TEXT) STRICT")
        connection.commit()
    finally:
        connection.close()
    with _error("schema_fingerprint_mismatch"):
        store.get_operation(operation_id)


def test_initialize_requires_private_parent_and_cleans_fault(tmp_path: Path, monkeypatch):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with _error("store_unsafe"):
        store_module.initialize(public / "operation.sqlite3")

    root = tmp_path / "new-private"
    path = root / "operation.sqlite3"

    def fail(_connection):
        raise RuntimeError("injected")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_after_schema_hook", fail)
        with pytest.raises(RuntimeError, match="injected"):
            store_module.initialize(path)
    assert not path.exists()
    assert root.is_dir()
    assert len(list(root.glob(".operation.sqlite3.init-*.tmp"))) == 1
    assert store_module.initialize(path).path == path


def test_initialize_cleans_directories_when_preflight_fails(tmp_path: Path, monkeypatch):
    root = tmp_path / "one" / "two"
    path = root / "operation.sqlite3"

    def fail(_path):
        raise store_module.OperationError("store_unsafe")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_require_no_sidecars", fail)
        with _error("store_unsafe"):
            store_module.initialize(path)
    assert not path.exists()
    assert root.is_dir()
    assert store_module.initialize(path).path == path


def test_initialize_cleans_directory_when_chmod_fails(tmp_path: Path, monkeypatch):
    root = tmp_path / "private"
    path = root / "operation.sqlite3"

    def fail(_path, _mode):
        raise OSError("injected")

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "chmod", fail)
        with _error("store_unsafe"):
            store_module.initialize(path)
    assert not root.exists()
    assert len(list(tmp_path.glob(".private.operation-init-*.tmp"))) == 1
    assert store_module.initialize(path).path == path


def test_initialize_cleans_directory_when_validate_fails_then_retries(
    tmp_path: Path, monkeypatch,
):
    root = tmp_path / "private"
    path = root / "operation.sqlite3"
    original = store_module._validate_directory

    def fail(candidate, *, exact_private=False):
        if candidate == root and exact_private:
            raise store_module.OperationError("store_unsafe")
        return original(candidate, exact_private=exact_private)

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_validate_directory", fail)
        with _error("store_unsafe"):
            store_module.initialize(path)
    assert root.is_dir()
    assert store_module.initialize(path).path == path


def test_initialize_does_not_remove_concurrently_replaced_directory(
    tmp_path: Path, monkeypatch,
):
    root = tmp_path / "private"
    path = root / "operation.sqlite3"
    original = store_module._validate_directory

    def replace(candidate, *, exact_private=False):
        if candidate == root and exact_private:
            candidate.rmdir()
            candidate.mkdir(mode=0o700)
            candidate.chmod(0o700)
            raise store_module.OperationError("store_unsafe")
        return original(candidate, exact_private=exact_private)

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_validate_directory", replace)
        with _error("store_unsafe"):
            store_module.initialize(path)
    assert root.is_dir()
    assert store_module.initialize(path).path == path


def test_initialize_never_removes_concurrent_temp_directory_replacement(
    tmp_path: Path, monkeypatch,
):
    root = tmp_path / "private"
    path = root / "operation.sqlite3"
    original = tmp_path / "original-temp-directory"
    replacement: Path | None = None
    validate = store_module._validate_directory

    def replace(candidate, *, exact_private=False):
        nonlocal replacement
        if candidate.name.startswith(".private.operation-init-") and exact_private:
            replacement = candidate
            candidate.rename(original)
            candidate.mkdir(mode=0o700)
            candidate.chmod(0o700)
            raise store_module.OperationError("store_unsafe")
        return validate(candidate, exact_private=exact_private)

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_validate_directory", replace)
        with _error("store_unsafe"):
            store_module.initialize(path)
    assert replacement is not None and replacement.is_dir()
    assert original.is_dir()
    assert store_module.initialize(path).path == path


def test_initialize_never_removes_concurrent_temp_leaf_replacement(
    tmp_path: Path, monkeypatch,
):
    root = tmp_path / "private"
    path = root / "operation.sqlite3"
    original = root / "original-temp.sqlite3"
    replacement = b"temp leaf replacement"

    def replace(_connection):
        [temp] = root.glob(".operation.sqlite3.init-*.tmp")
        temp.rename(original)
        temp.write_bytes(replacement)
        temp.chmod(0o600)
        raise store_module.OperationError("store_unsafe")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_after_schema_hook", replace)
        with _error("store_unsafe"):
            store_module.initialize(path)
    [replacement_path] = root.glob(".operation.sqlite3.init-*.tmp")
    assert replacement_path.read_bytes() == replacement
    assert original.exists()
    assert store_module.initialize(path).path == path


def test_initialize_never_unlinks_concurrent_final_leaf_replacement(
    tmp_path: Path, monkeypatch,
):
    root = tmp_path / "private"
    path = root / "operation.sqlite3"
    original = root / "original.sqlite3"
    replacement = b"concurrent replacement"

    def replace(published: Path):
        published.rename(original)
        published.write_bytes(replacement)
        published.chmod(0o600)
        raise store_module.OperationError("store_unsafe")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_after_publish_hook", replace)
        with _error("store_unsafe"):
            store_module.initialize(path)
    assert path.read_bytes() == replacement
    assert original.exists()


def test_create_projection_saves_typed_fences_without_reading_domain(store):
    result = _create(store)
    assert result.replayed is False
    assert result.operation_id.startswith("op_")
    assert result.request_digest.startswith("sha256:")
    assert set(result.projection) == {
        "operation", "preconditions", "steps", "attempts", "receipts",
    }
    operation = result.projection["operation"]
    assert operation["status"] == "planned"
    assert operation["revision"] == 1
    assert "idempotency_key" not in operation
    assert result.projection["preconditions"] == [{
        "operation_id": result.operation_id,
        "ordinal": 0,
        "precondition_type": "runtime.generation",
        "subject_type": "runtime",
        "subject_id": "local",
        "expected_revision": 7,
        "expected_generation": "generation_9",
        "expected_epoch": "epoch_3",
        "expected_digest": _sha("runtime"),
    }]
    assert [item["step_id"] for item in result.projection["steps"]] == ["allocate", "persist"]
    assert result.projection["attempts"] == []
    assert result.projection["receipts"] == []


def test_create_idempotency_replays_canonical_request_and_conflicts(store):
    first = _create(store, request={"b": 2, "a": 1})
    replay = _create(store, request={"a": 1, "b": 2})
    assert replay.replayed is True
    assert replay.operation_id == first.operation_id
    assert replay.request_digest == first.request_digest
    assert replay.projection == first.projection
    with _error("idempotency_conflict"):
        _create(store, request={"a": 9, "b": 2})
    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
    finally:
        connection.close()


def test_create_invalid_input_is_rejected_without_partial_rows(store):
    with _error("invalid_argument"):
        _create(store, key="bad key")
    with _error("invalid_argument"):
        store.create_operation(
            scope="scope", idempotency_key="key", request={}, kind="kind",
            subject_type="workspace", subject_id="ws", plan_digest="bad",
            approval_required=False,
        )
    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    finally:
        connection.close()


def test_sqlite_integer_inputs_are_bounded_before_bind(store):
    maximum = 2**63 - 1
    valid = store.create_operation(
        scope="scope_max", idempotency_key="key_max", request={}, kind="kind",
        subject_type="workspace", subject_id="ws_max", plan_digest=_sha("max"),
        approval_required=False,
        preconditions=(store_module.Precondition(
            "revision", "workspace", "ws_max", expected_revision=maximum,
        ),),
        steps=(store_module.Step("step_max", "kind"),),
    )
    assert valid.projection["preconditions"][0]["expected_revision"] == maximum

    for index, invalid in enumerate((True, -1, 2**63)):
        with _error("invalid_argument"):
            store.create_operation(
                scope="scope_int", idempotency_key=f"key_{index}", request={}, kind="kind",
                subject_type="workspace", subject_id="ws", plan_digest=_sha("int"),
                approval_required=False,
                preconditions=(store_module.Precondition(
                    "revision", "workspace", "ws", expected_revision=invalid,
                ),),
            )

    operation_id = _create(store, key="revision-bounds").operation_id
    before = store.get_operation(operation_id)
    for invalid in (True, 0, 2**63):
        with _error("invalid_argument"):
            store.transition(
                operation_id, expected_operation_revision=invalid, status="running",
            )
        assert store.get_operation(operation_id) == before

    store.transition(operation_id, expected_operation_revision=1, status="running")
    running = store.get_operation(operation_id)
    with _error("invalid_argument"):
        store.prepare_attempt(
            operation_id, "allocate", expected_operation_revision=2,
            expected_step_revision=2**63, mode="execute", provider_kind="runtime",
        )
    assert store.get_operation(operation_id) == running

    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM operations WHERE scope='scope_int'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_revision_and_attempt_number_exhaustion_are_stable_zero_change(store):
    maximum = 2**63 - 1
    operation_id = _create(store).operation_id
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "UPDATE operations SET revision=? WHERE operation_id=?",
            (maximum, operation_id),
        )
        connection.commit()
    finally:
        connection.close()
    with _error("revision_exhausted"):
        store.transition(
            operation_id, expected_operation_revision=maximum, status="running",
        )
    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute(
            "SELECT revision,status FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone() == (maximum, "planned")
    finally:
        connection.close()

    operation_id = _create(store, key="step-exhaustion").operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "UPDATE operation_steps SET revision=? WHERE operation_id=? AND step_id='allocate'",
            (maximum, operation_id),
        )
        connection.commit()
    finally:
        connection.close()
    with _error("revision_exhausted"):
        store.prepare_attempt(
            operation_id, "allocate", expected_operation_revision=2,
            expected_step_revision=maximum, mode="execute", provider_kind="runtime",
        )
    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM operation_attempts WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0] == 0
        connection.execute(
            "UPDATE operation_steps SET revision=1 WHERE operation_id=? AND step_id='allocate'",
            (operation_id,),
        )
        connection.execute(
            "INSERT INTO operation_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                operation_id, "allocate", maximum, "exec_max_attempt", "execute",
                "failed", "runtime", None, "confirmed", "2026-08-14T00:00:00Z",
                "2026-08-14T00:00:01Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    with _error("attempt_number_exhausted"):
        store.prepare_attempt(
            operation_id, "allocate", expected_operation_revision=2,
            expected_step_revision=1, mode="execute", provider_kind="runtime",
        )
    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM operation_attempts WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_state_machine_cas_and_terminal_immutability(store):
    operation_id = _create(store).operation_id
    running = store.transition(operation_id, expected_operation_revision=1, status="running")
    assert running["operation"]["revision"] == 2
    assert running["operation"]["status"] == "running"
    before = json.dumps(running, sort_keys=True)
    with _error("revision_conflict"):
        store.transition(
            operation_id, expected_operation_revision=1, status="failed",
            failure_code="confirmed",
        )
    assert json.dumps(store.get_operation(operation_id), sort_keys=True) == before
    terminal = store.transition(
        operation_id, expected_operation_revision=2, status="failed",
        failure_code="confirmed",
    )
    assert terminal["operation"]["revision"] == 3
    assert terminal["operation"]["terminal_at"] is not None
    with _error("invalid_transition"):
        store.transition(operation_id, expected_operation_revision=3, status="running")


def test_attempt_helpers_cannot_mutate_terminal_operation(store):
    operation_id = _create(store).operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    prepared = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=2,
        expected_step_revision=1, mode="execute", provider_kind="runtime",
    )
    with _error("attempt_active"):
        store.transition(operation_id, expected_operation_revision=3, status="succeeded")
    store.dispatch_attempt(
        operation_id, prepared.step_execution_id, expected_operation_revision=3,
    )
    with _error("attempt_active"):
        store.transition(operation_id, expected_operation_revision=4, status="succeeded")


def test_illegal_transition_and_required_reason_are_zero_change(store):
    operation_id = _create(store).operation_id
    before = store.get_operation(operation_id)
    with _error("invalid_transition"):
        store.transition(operation_id, expected_operation_revision=1, status="succeeded")
    with _error("invalid_argument"):
        store.transition(operation_id, expected_operation_revision=1, status="failed")
    with _error("invalid_argument"):
        store.transition(operation_id, expected_operation_revision=1, status="needs_attention")
    assert store.get_operation(operation_id) == before


def test_two_connection_cas_race_only_one_wins(store):
    operation_id = _create(store).operation_id

    def change(status: str):
        try:
            store.transition(operation_id, expected_operation_revision=1, status=status,
                             failure_code="confirmed" if status == "failed" else None)
            return "ok"
        except store_module.OperationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change, ("running", "failed")))
    assert sorted(results) == ["ok", "revision_conflict"]
    assert store.get_operation(operation_id)["operation"]["revision"] == 2


def test_execution_id_is_durable_before_dispatch_and_active_is_unique(store):
    operation_id = _create(store).operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    prepared = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=2,
        expected_step_revision=1, mode="execute", provider_kind="runtime",
    )
    assert prepared.step_execution_id.startswith("exec_")
    assert prepared.projection["operation"]["revision"] == 3
    attempt = prepared.projection["attempts"][0]
    assert attempt["status"] == "prepared"
    assert attempt["step_execution_id"] == prepared.step_execution_id
    with _error("attempt_active"):
        store.prepare_attempt(
            operation_id, "allocate", expected_operation_revision=3,
            expected_step_revision=2, mode="execute", provider_kind="runtime",
        )
    dispatched = store.dispatch_attempt(
        operation_id, prepared.step_execution_id,
        expected_operation_revision=3, provider_operation_ref="provider_ref_1",
    )
    assert dispatched["operation"]["revision"] == 4
    assert dispatched["attempts"][0]["status"] == "dispatched"


def test_outcome_requires_dispatch_and_step_cas_then_blocks_blind_retry(store):
    operation_id = _create(store).operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    prepared = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=2,
        expected_step_revision=1, mode="execute", provider_kind="runtime",
    )
    with _error("attempt_conflict"):
        store.record_attempt_outcome(
            operation_id, prepared.step_execution_id,
            expected_operation_revision=3, expected_step_revision=2,
            receipt_id="receipt_early", receipt_type="provider_outcome",
            outcome="succeeded", evidence_kind="opaque_digest",
            evidence_digest=_sha("early"),
        )
    store.dispatch_attempt(
        operation_id, prepared.step_execution_id, expected_operation_revision=3,
    )
    before = store.get_operation(operation_id)
    with _error("revision_conflict"):
        store.record_attempt_outcome(
            operation_id, prepared.step_execution_id,
            expected_operation_revision=4, expected_step_revision=1,
            receipt_id="receipt_stale", receipt_type="provider_outcome",
            outcome="succeeded", evidence_kind="opaque_digest",
            evidence_digest=_sha("stale"),
        )
    assert store.get_operation(operation_id) == before
    succeeded = store.record_attempt_outcome(
        operation_id, prepared.step_execution_id,
        expected_operation_revision=4, expected_step_revision=2,
        receipt_id="receipt_success", receipt_type="provider_outcome",
        outcome="succeeded", evidence_kind="opaque_digest",
        evidence_digest=_sha("success"),
    )
    assert succeeded["steps"][0]["status"] == "succeeded"
    with _error("invalid_transition"):
        store.prepare_attempt(
            operation_id, "allocate", expected_operation_revision=5,
            expected_step_revision=3, mode="execute", provider_kind="runtime",
        )


def test_response_lost_receipt_is_exactly_replayed_and_blocks_retry(store):
    operation_id = _create(store).operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    prepared = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=2,
        expected_step_revision=1, mode="execute", provider_kind="runtime",
    )
    store.dispatch_attempt(
        operation_id, prepared.step_execution_id, expected_operation_revision=3,
    )
    first = store.record_attempt_outcome(
        operation_id, prepared.step_execution_id,
        expected_operation_revision=4,
        expected_step_revision=2,
        receipt_id="receipt_unknown_1",
        receipt_type="provider_response_lost",
        outcome="outcome_unknown",
        evidence_kind="provider_execution",
        evidence_ref="opaque_ref_1",
        evidence_digest=_sha("unknown"),
        summary=None,
    )
    assert first["receipt_replayed"] is False
    assert first["operation"]["status"] == "needs_attention"
    assert first["operation"]["revision"] == 5
    assert first["attempts"][0]["status"] == "outcome_unknown"
    replay = store.record_attempt_outcome(
        operation_id, prepared.step_execution_id,
        expected_operation_revision=4,
        expected_step_revision=2,
        receipt_id="receipt_unknown_1",
        receipt_type="provider_response_lost",
        outcome="outcome_unknown",
        evidence_kind="provider_execution",
        evidence_ref="opaque_ref_1",
        evidence_digest=_sha("unknown"),
        summary=None,
    )
    assert replay["receipt_replayed"] is True
    assert replay["operation"]["revision"] == 5
    assert len(replay["receipts"]) == 1
    with _error("idempotency_conflict"):
        store.record_attempt_outcome(
            operation_id, prepared.step_execution_id,
            expected_operation_revision=5,
            expected_step_revision=3,
            receipt_id="receipt_unknown_1",
            receipt_type="provider_response_lost",
            outcome="outcome_unknown",
            evidence_kind="provider_execution",
            evidence_digest=_sha("different"),
        )
    with _error("invalid_transition"):
        store.prepare_attempt(
            operation_id, "allocate", expected_operation_revision=5,
            expected_step_revision=3, mode="execute", provider_kind="runtime",
        )
    reconciled = store.record_not_executed(
        operation_id, prepared.step_execution_id,
        expected_operation_revision=5, expected_step_revision=3,
        receipt_id="receipt_not_executed_1",
        evidence_digest=_sha("provider-query"),
        evidence_ref="provider_query_1",
        summary=None,
    )
    assert reconciled["operation"]["status"] == "needs_attention"
    assert reconciled["operation"]["revision"] == 6
    assert reconciled["steps"][0]["status"] == "pending"
    assert reconciled["steps"][0]["active_attempt_no"] is None
    assert reconciled["attempts"][0]["failure_code"] == "not_executed"
    assert len(reconciled["receipts"]) == 2
    replay = store.record_not_executed(
        operation_id, prepared.step_execution_id,
        expected_operation_revision=5, expected_step_revision=3,
        receipt_id="receipt_not_executed_1",
        evidence_digest=_sha("provider-query"),
        evidence_ref="provider_query_1",
        summary=None,
    )
    assert replay["receipt_replayed"] is True
    assert replay["operation"]["revision"] == 6
    unknown_replay = store.record_attempt_outcome(
        operation_id, prepared.step_execution_id,
        expected_operation_revision=4, expected_step_revision=2,
        receipt_id="receipt_unknown_1",
        receipt_type="provider_response_lost",
        outcome="outcome_unknown",
        evidence_kind="provider_execution",
        evidence_ref="opaque_ref_1",
        evidence_digest=_sha("unknown"),
        summary=None,
    )
    assert unknown_replay["receipt_replayed"] is True
    assert unknown_replay["operation"]["revision"] == 6


def test_receipts_are_append_only_and_bounded(store):
    operation_id = _create(store).operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    prepared = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=2,
        expected_step_revision=1, mode="execute", provider_kind="runtime",
    )
    with _error("invalid_argument"):
        store.record_attempt_outcome(
            operation_id, prepared.step_execution_id,
            expected_operation_revision=3,
            expected_step_revision=2,
            receipt_id="receipt_1", receipt_type="provider",
            outcome="succeeded", evidence_kind="opaque",
            evidence_digest=_sha("ok"), summary="x" * 1025,
        )
    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 0
    finally:
        connection.close()
    store.dispatch_attempt(
        operation_id, prepared.step_execution_id, expected_operation_revision=3,
    )
    store.record_attempt_outcome(
        operation_id, prepared.step_execution_id,
        expected_operation_revision=4,
        expected_step_revision=2,
        receipt_id="receipt_1", receipt_type="provider_outcome",
        outcome="succeeded", evidence_kind="opaque_digest",
        evidence_digest=_sha("ok"), summary=None,
    )
    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            connection.execute("DELETE FROM operation_receipts")
    finally:
        connection.close()


def test_late_sibling_receipt_is_journaled_after_needs_attention(store):
    operation_id = _create(store).operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    first = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=2,
        expected_step_revision=1, mode="execute", provider_kind="runtime",
    )
    store.dispatch_attempt(operation_id, first.step_execution_id, expected_operation_revision=3)
    second = store.prepare_attempt(
        operation_id, "persist", expected_operation_revision=4,
        expected_step_revision=1, mode="execute", provider_kind="registry",
    )
    store.dispatch_attempt(operation_id, second.step_execution_id, expected_operation_revision=5)
    unknown = store.record_attempt_outcome(
        operation_id, first.step_execution_id,
        expected_operation_revision=6, expected_step_revision=2,
        receipt_id="receipt_first_unknown", receipt_type="provider_response_lost",
        outcome="outcome_unknown", evidence_kind="provider_execution",
        evidence_digest=_sha("first-unknown"),
    )
    assert unknown["operation"]["status"] == "needs_attention"
    late = store.record_attempt_outcome(
        operation_id, second.step_execution_id,
        expected_operation_revision=7, expected_step_revision=2,
        receipt_id="receipt_second_late", receipt_type="provider_outcome",
        outcome="succeeded", evidence_kind="opaque_digest",
        evidence_digest=_sha("second-success"),
    )
    assert late["operation"]["status"] == "needs_attention"
    assert late["operation"]["revision"] == 8
    assert len(late["receipts"]) == 2
    assert [item["status"] for item in late["attempts"]] == [
        "outcome_unknown", "succeeded",
    ]


def test_parallel_successes_remain_running_until_explicit_completion(store):
    operation_id = _create(store).operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    first = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=2,
        expected_step_revision=1, mode="execute", provider_kind="runtime",
    )
    store.dispatch_attempt(operation_id, first.step_execution_id, expected_operation_revision=3)
    second = store.prepare_attempt(
        operation_id, "persist", expected_operation_revision=4,
        expected_step_revision=1, mode="execute", provider_kind="registry",
    )
    store.dispatch_attempt(operation_id, second.step_execution_id, expected_operation_revision=5)
    first_done = store.record_attempt_outcome(
        operation_id, first.step_execution_id,
        expected_operation_revision=6, expected_step_revision=2,
        receipt_id="receipt_first_success", receipt_type="provider_outcome",
        outcome="succeeded", evidence_kind="opaque_digest",
        evidence_digest=_sha("first-success"),
    )
    assert first_done["operation"]["status"] == "running"
    second_done = store.record_attempt_outcome(
        operation_id, second.step_execution_id,
        expected_operation_revision=7, expected_step_revision=2,
        receipt_id="receipt_second_success", receipt_type="provider_outcome",
        outcome="succeeded", evidence_kind="opaque_digest",
        evidence_digest=_sha("second-success"),
    )
    assert second_done["operation"]["status"] == "running"
    completed = store.transition(
        operation_id, expected_operation_revision=8, status="succeeded",
    )
    assert completed["operation"]["status"] == "succeeded"


def test_unknown_settles_prepared_sibling_as_locally_not_dispatched(store):
    operation_id = _create(store).operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    first = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=2,
        expected_step_revision=1, mode="execute", provider_kind="runtime",
    )
    store.dispatch_attempt(operation_id, first.step_execution_id, expected_operation_revision=3)
    second = store.prepare_attempt(
        operation_id, "persist", expected_operation_revision=4,
        expected_step_revision=1, mode="execute", provider_kind="registry",
    )
    before = store.get_operation(operation_id)
    with _error("invalid_argument"):
        store.record_attempt_outcome(
            operation_id, first.step_execution_id,
            expected_operation_revision=5, expected_step_revision=2,
            expected_prepared_step_revisions={"persist": 2**63},
            receipt_id="receipt_unknown_with_prepared", receipt_type="provider_response_lost",
            outcome="outcome_unknown", evidence_kind="provider_execution",
            evidence_digest=_sha("unknown-with-prepared"),
        )
    assert store.get_operation(operation_id) == before
    with _error("revision_conflict"):
        store.record_attempt_outcome(
            operation_id, first.step_execution_id,
            expected_operation_revision=5, expected_step_revision=2,
            expected_prepared_step_revisions={"persist": 1},
            receipt_id="receipt_unknown_with_prepared", receipt_type="provider_response_lost",
            outcome="outcome_unknown", evidence_kind="provider_execution",
            evidence_digest=_sha("unknown-with-prepared"),
        )
    assert store.get_operation(operation_id) == before
    unknown = store.record_attempt_outcome(
        operation_id, first.step_execution_id,
        expected_operation_revision=5, expected_step_revision=2,
        expected_prepared_step_revisions={"persist": 2},
        receipt_id="receipt_unknown_with_prepared", receipt_type="provider_response_lost",
        outcome="outcome_unknown", evidence_kind="provider_execution",
        evidence_digest=_sha("unknown-with-prepared"),
    )
    by_execution = {item["step_execution_id"]: item for item in unknown["attempts"]}
    assert by_execution[second.step_execution_id]["status"] == "failed"
    assert by_execution[second.step_execution_id]["failure_code"] == "not_dispatched"
    assert unknown["steps"][1]["status"] == "pending"
    assert unknown["steps"][1]["active_attempt_no"] is None
    assert {item["outcome"] for item in unknown["receipts"]} == {
        "outcome_unknown", "not_executed",
    }


def test_sibling_settlement_races_dispatch_with_one_atomic_winner(store):
    operation_id = _create(store).operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    first = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=2,
        expected_step_revision=1, mode="execute", provider_kind="runtime",
    )
    store.dispatch_attempt(operation_id, first.step_execution_id, expected_operation_revision=3)
    second = store.prepare_attempt(
        operation_id, "persist", expected_operation_revision=4,
        expected_step_revision=1, mode="execute", provider_kind="registry",
    )

    def settle():
        try:
            store.record_attempt_outcome(
                operation_id, first.step_execution_id,
                expected_operation_revision=5, expected_step_revision=2,
                expected_prepared_step_revisions={"persist": 2},
                receipt_id="receipt_race_unknown",
                receipt_type="provider_response_lost",
                outcome="outcome_unknown", evidence_kind="provider_execution",
                evidence_digest=_sha("race-unknown"),
            )
            return "settled"
        except store_module.OperationError as exc:
            return exc.code

    def dispatch():
        try:
            store.dispatch_attempt(
                operation_id, second.step_execution_id,
                expected_operation_revision=5,
            )
            return "dispatched"
        except store_module.OperationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(settle), pool.submit(dispatch))
        results = sorted(future.result() for future in futures)
    assert results in (
        ["dispatched", "revision_conflict"],
        ["attempt_conflict", "settled"],
        ["revision_conflict", "settled"],
    )
    projection = store.get_operation(operation_id)
    assert projection["operation"]["revision"] == 6
    by_execution = {item["step_execution_id"]: item for item in projection["attempts"]}
    if "settled" in results:
        assert projection["operation"]["status"] == "needs_attention"
        assert by_execution[first.step_execution_id]["status"] == "outcome_unknown"
        assert by_execution[second.step_execution_id]["status"] == "failed"
        assert len(projection["receipts"]) == 2
    else:
        assert projection["operation"]["status"] == "running"
        assert by_execution[first.step_execution_id]["status"] == "dispatched"
        assert by_execution[second.step_execution_id]["status"] == "dispatched"
        assert projection["receipts"] == []


def test_compensation_not_executed_restores_succeeded_step(store):
    operation_id = _create(store).operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    execute = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=2,
        expected_step_revision=1, mode="execute", provider_kind="runtime",
    )
    store.dispatch_attempt(operation_id, execute.step_execution_id, expected_operation_revision=3)
    store.record_attempt_outcome(
        operation_id, execute.step_execution_id,
        expected_operation_revision=4, expected_step_revision=2,
        receipt_id="receipt_execute", receipt_type="provider_outcome",
        outcome="succeeded", evidence_kind="opaque_digest",
        evidence_digest=_sha("execute"),
    )
    store.transition(operation_id, expected_operation_revision=5, status="compensating")
    compensate = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=6,
        expected_step_revision=3, mode="compensate", provider_kind="runtime",
    )
    store.dispatch_attempt(
        operation_id, compensate.step_execution_id, expected_operation_revision=7,
    )
    store.record_attempt_outcome(
        operation_id, compensate.step_execution_id,
        expected_operation_revision=8, expected_step_revision=4,
        receipt_id="receipt_comp_unknown", receipt_type="provider_response_lost",
        outcome="outcome_unknown", evidence_kind="provider_execution",
        evidence_digest=_sha("comp-unknown"),
    )
    reconciled = store.record_not_executed(
        operation_id, compensate.step_execution_id,
        expected_operation_revision=9, expected_step_revision=5,
        receipt_id="receipt_comp_not_executed",
        evidence_digest=_sha("comp-not-executed"),
    )
    assert reconciled["operation"]["status"] == "needs_attention"
    assert reconciled["steps"][0]["status"] == "succeeded"
    with _error("invalid_transition"):
        store.prepare_attempt(
            operation_id, "allocate", expected_operation_revision=10,
            expected_step_revision=6, mode="execute", provider_kind="runtime",
        )


def test_receipt_type_private_content_and_composite_attempt_identity_are_strict(store):
    operation_id = _create(store).operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    first = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=2,
        expected_step_revision=1, mode="execute", provider_kind="runtime",
    )
    store.dispatch_attempt(
        operation_id, first.step_execution_id, expected_operation_revision=3,
    )
    for receipt_type, evidence_kind, summary in (
        ("stdout", "opaque_digest", None),
        ("provider_outcome", "environment", None),
        ("provider_outcome", "opaque_digest", "secret token value"),
        ("provider_outcome", "opaque_digest", "/private/path"),
    ):
        with _error("invalid_argument"):
            store.record_attempt_outcome(
                operation_id, first.step_execution_id,
                expected_operation_revision=4, expected_step_revision=2,
                receipt_id="receipt_private", receipt_type=receipt_type,
                outcome="succeeded", evidence_kind=evidence_kind,
                evidence_digest=_sha("private"), summary=summary,
            )

    connection = sqlite3.connect(store.path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO operation_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "receipt_forged", operation_id, "allocate", 1,
                    "exec_" + "0" * 32, "provider_outcome", "succeeded",
                    "opaque_digest", None, _sha("forged"), None,
                    "2026-08-14T00:00:00Z",
                ),
            )
    finally:
        connection.close()


def _client(provider):
    app = FastAPI()
    api.install(app, api.ApiService(provider))
    return TestClient(app), app


def test_api_installs_only_get_and_returns_exact_g3_projection(store):
    operation_id = _create(store).operation_id
    client, app = _client(lambda: store)
    operation_routes = [route for route in app.routes if route.path.startswith("/api/operations")]
    assert [(route.path, route.methods) for route in operation_routes] == [
        ("/api/operations/{operation_id}", {"GET"}),
    ]
    response = client.get(f"/api/operations/{operation_id}")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"data", "meta"}
    assert set(payload["data"]) == {
        "operation", "preconditions", "steps", "attempts", "receipts",
    }
    meta = payload["meta"]
    assert set(meta) == {
        "request_id", "generated_at", "partial", "sources", "warnings", "capabilities",
    }
    assert meta["partial"] is False
    assert meta["sources"] == [{
        "name": "operation_journal", "status": "available",
        "observed_at": None, "reason": None,
    }]
    assert meta["capabilities"] == {
        "operations.read": {"available": True, "reason": None},
        "operations.execute": {"available": False, "reason": "operation_executor_not_wired"},
        "operations.retry": {"available": False, "reason": "operation_executor_not_wired"},
        "operations.reconcile": {"available": False, "reason": "operation_executor_not_wired"},
    }
    assert client.post(f"/api/operations/{operation_id}/execute").status_code == 404
    assert client.post(f"/api/operations/{operation_id}/retry").status_code == 404


def test_api_not_found_and_missing_store_are_sanitized_and_read_only(tmp_path: Path):
    store = store_module.initialize(tmp_path / "operation.sqlite3")
    client, _ = _client(lambda: store)
    response = client.get("/api/operations/op_missing")
    assert response.status_code == 404
    error = response.json()["error"]
    assert set(error) == {"code", "message", "retryable", "request_id", "details"}
    assert error["code"] == "operation_not_found"

    missing = tmp_path / "missing" / "operation.sqlite3"
    missing_client, _ = _client(lambda: store_module.open_existing(missing))
    response = missing_client.get("/api/operations/op_missing")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "schema_missing"
    assert not missing.parent.exists()
    assert str(tmp_path) not in response.text


def test_api_schema_drift_is_stable_503(tmp_path: Path):
    path = tmp_path / "operation.sqlite3"
    store_module.initialize(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE unknown_table(x TEXT) STRICT")
        connection.commit()
    finally:
        connection.close()
    client, _ = _client(lambda: store_module.open_existing(path))
    response = client.get("/api/operations/op_missing")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "schema_fingerprint_mismatch"
    assert "unknown_table" not in response.text
    assert str(path) not in response.text


@pytest.mark.parametrize(
    ("assignment", "corrupt_value"),
    [
        ("request_digest=?", "not-a-digest"),
        ("created_at=?", "not-a-timestamp"),
    ],
)
def test_materialized_scalar_corruption_is_store_corrupt_and_api_503(
    tmp_path: Path, assignment: str, corrupt_value: str,
):
    path = tmp_path / "operation.sqlite3"
    store = store_module.initialize(path)
    operation_id = _create(store).operation_id
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            f"UPDATE operations SET {assignment} WHERE operation_id=?",
            (corrupt_value, operation_id),
        )
        connection.commit()
    finally:
        connection.close()
    with _error("store_corrupt"):
        store.get_operation(operation_id)
    client, _ = _client(lambda: store)
    response = client.get(f"/api/operations/{operation_id}")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "store_corrupt"
    assert set(error) == {"code", "message", "retryable", "request_id", "details"}
    assert corrupt_value not in response.text
    assert str(path) not in response.text


def test_materialized_cross_row_corruption_is_store_corrupt_and_api_503(
    tmp_path: Path,
):
    path = tmp_path / "operation.sqlite3"
    store = store_module.initialize(path)
    operation_id = _create(store).operation_id
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE operation_steps SET status='running',active_attempt_no=99 "
            "WHERE operation_id=? AND step_id='allocate'",
            (operation_id,),
        )
        connection.commit()
    finally:
        connection.close()
    with _error("store_corrupt"):
        store.get_operation(operation_id)
    client, _ = _client(lambda: store)
    response = client.get(f"/api/operations/{operation_id}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "store_corrupt"
    assert str(path) not in response.text


@pytest.mark.parametrize("corruption", ["reverse-active", "receipt-outcome"])
def test_materialized_legal_scalar_contradictions_fail_closed(
    tmp_path: Path, corruption: str,
):
    path = tmp_path / "operation.sqlite3"
    store = store_module.initialize(path)
    operation_id = _create(store).operation_id
    store.transition(operation_id, expected_operation_revision=1, status="running")
    prepared = store.prepare_attempt(
        operation_id, "allocate", expected_operation_revision=2,
        expected_step_revision=1, mode="execute", provider_kind="runtime",
    )
    connection = sqlite3.connect(path)
    try:
        if corruption == "reverse-active":
            connection.execute(
                "UPDATE operation_steps SET status='pending',active_attempt_no=NULL "
                "WHERE operation_id=? AND step_id='allocate'",
                (operation_id,),
            )
        else:
            connection.execute(
                "UPDATE operation_attempts SET status='succeeded',finished_at=? "
                "WHERE step_execution_id=?",
                ("2026-08-14T00:00:01Z", prepared.step_execution_id),
            )
            connection.execute(
                "UPDATE operation_steps SET status='succeeded',active_attempt_no=NULL "
                "WHERE operation_id=? AND step_id='allocate'",
                (operation_id,),
            )
        connection.commit()
    finally:
        connection.close()
    with _error("store_corrupt"):
        store.get_operation(operation_id)
    client, _ = _client(lambda: store)
    response = client.get(f"/api/operations/{operation_id}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "store_corrupt"
