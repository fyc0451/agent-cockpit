from __future__ import annotations

import sqlite3
import traceback
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_cockpit import memory_api, memory_store


PROJECT_A = "prj_" + "a" * 32
PROJECT_B = "prj_" + "b" * 32
ACTOR = {"type": "human", "id": "boss-local"}
SOURCE = {"type": "user", "id": "boss-local"}
PROPOSER = {"type": "agent", "id": "agent-1"}


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / "project-memory.sqlite3"


@pytest.fixture()
def store(path: Path) -> memory_store.MemoryStore:
    return memory_store.initialize(path)


def _append_fact(
    store: memory_store.MemoryStore, *, project_id: str = PROJECT_A,
    fact_key: str = "goal.current", expected_version: int = 0,
    summary: str = "Current goal", value: dict | None = None,
    kind: str = "state", status: str = "current",
) -> memory_store.FactRecord:
    return store.append_fact(
        project_id=project_id,
        fact_key=fact_key,
        kind=kind,
        status=status,
        summary=summary,
        value=value or {"goal": summary},
        source=SOURCE,
        actor=ACTOR,
        expected_version=expected_version,
    )


def _candidate(
    store: memory_store.MemoryStore, *, project_id: str = PROJECT_A,
    candidate_id: str = "mca_" + "c" * 32,
    target_fact_key: str = "goal.current", expected_fact_version: int = 0,
    summary: str = "Candidate goal", value: dict | None = None,
) -> memory_store.CandidateRecord:
    return store.create_candidate(
        candidate_id=candidate_id,
        project_id=project_id,
        target_fact_key=target_fact_key,
        kind="state",
        summary=summary,
        value=value or {"goal": summary},
        source={"type": "run", "id": "run-1"},
        proposer=PROPOSER,
        expected_fact_version=expected_fact_version,
        confidence=0.75,
    )


def _sanitized(
    error: pytest.ExceptionInfo[memory_store.MemoryStoreError],
    code: str, private: str,
) -> None:
    assert error.value.code == code
    assert str(error.value) == code
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert private not in "".join(traceback.format_exception(error.value))


def test_initialize_is_explicit_private_validated_and_retryable(
    path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(memory_store.MemoryStoreError) as missing:
        memory_store.open_existing(path)
    assert missing.value.code == "schema_missing"
    assert not path.exists()

    real_initialize = memory_store._initialize_connection
    private = "private ddl failure"

    def fail(_connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError(private)

    monkeypatch.setattr(memory_store, "_initialize_connection", fail)
    with pytest.raises(memory_store.MemoryStoreError) as failure:
        memory_store.initialize(path)
    _sanitized(failure, "store_write_failed", private)
    assert not path.exists()
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []

    monkeypatch.setattr(memory_store, "_initialize_connection", real_initialize)
    created = memory_store.initialize(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert created.summary(PROJECT_A).public_dict() == {
        "project_id": PROJECT_A,
        "revision": 0,
        "current_facts": 0,
        "stale_facts": 0,
        "conflicts": 0,
        "retired_facts": 0,
        "pending_candidates": 0,
    }
    memory_store.open_existing(path).close()


def test_initialize_post_publish_failure_removes_only_own_leaf_and_retries(
    path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_fsync_parent = memory_store._fsync_parent
    private = "private post-publish failure"

    def fail_fsync(_path: Path) -> None:
        raise OSError(private)

    monkeypatch.setattr(memory_store, "_fsync_parent", fail_fsync)
    with pytest.raises(memory_store.MemoryStoreError) as failure:
        memory_store.initialize(path)
    _sanitized(failure, "store_write_failed", private)
    assert not path.exists()
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []

    monkeypatch.setattr(memory_store, "_fsync_parent", real_fsync_parent)
    retried = memory_store.initialize(path)
    assert retried.summary(PROJECT_A).revision == 0
    assert path.stat().st_mode & 0o777 == 0o600


def test_initialize_file_exists_race_preserves_published_winner(
    path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def publish_winner(source: Path, destination: Path, **_kwargs) -> None:
        destination = Path(destination)
        destination.write_bytes(Path(source).read_bytes())
        destination.chmod(0o600)
        raise FileExistsError

    monkeypatch.setattr(memory_store.os, "link", publish_winner)
    winner = memory_store.initialize(path)
    assert winner.summary(PROJECT_A).revision == 0
    assert path.exists()
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_owned_leaf_cleanup_preserves_identity_mismatch(
    path: Path, tmp_path: Path,
) -> None:
    first = memory_store.initialize(path)
    other_path = tmp_path / "other-memory.sqlite3"
    memory_store.initialize(other_path).close()

    memory_store._unlink_owned_leaf(
        other_path, memory_store._leaf_signature(first.path),
    )
    assert other_path.exists()
    memory_store.open_existing(other_path).close()


@pytest.mark.parametrize("kind", ["view", "trigger"])
def test_unknown_schema_objects_fail_closed(path: Path, kind: str) -> None:
    memory_store.initialize(path).close()
    with sqlite3.connect(path) as connection:
        if kind == "view":
            connection.execute(
                "CREATE VIEW extra_memory_view AS SELECT project_id FROM memory_projects"
            )
        else:
            connection.execute(
                "CREATE TRIGGER extra_memory_trigger AFTER INSERT ON memory_projects "
                "BEGIN SELECT 1; END"
            )
    before = path.read_bytes()
    with pytest.raises(memory_store.MemoryStoreError) as mismatch:
        memory_store.open_existing(path)
    assert mismatch.value.code == "schema_fingerprint_mismatch"
    assert path.read_bytes() == before


def test_fact_revisions_are_append_only_cas_and_advance_project(store) -> None:
    first = _append_fact(store)
    second = _append_fact(
        store, expected_version=1, summary="Revised goal",
        value={"goal": "revised"}, status="stale",
    )
    assert (first.version, second.version, second.status) == (1, 2, "stale")
    facts, cursor = store.list_facts(project_id=PROJECT_A)
    assert facts == (second,)
    assert cursor is None
    assert store.summary(PROJECT_A).revision == 2
    assert [event.project_revision for event in store.timeline(project_id=PROJECT_A)[0]] == [1, 2]

    with pytest.raises(memory_store.MemoryStoreError) as stale:
        _append_fact(store, expected_version=1, summary="Lost update")
    assert stale.value.code == "fact_version_conflict"
    assert store.summary(PROJECT_A).revision == 2
    assert store.list_facts(project_id=PROJECT_A)[0] == (second,)


def test_candidate_replay_approve_and_decision_receipt_are_atomic(store) -> None:
    _append_fact(store)
    candidate = _candidate(store, expected_fact_version=1)
    assert _candidate(store, expected_fact_version=1) == candidate
    with pytest.raises(memory_store.MemoryStoreError) as changed:
        _candidate(store, expected_fact_version=1, summary="Changed request")
    assert changed.value.code == "idempotency_conflict"

    decided = store.decide_candidate(
        project_id=PROJECT_A,
        candidate_id=candidate.candidate_id,
        decision="approve",
        expected_candidate_revision=1,
        expected_fact_version=1,
        decided_by=ACTOR,
    )
    assert decided.status == "approved"
    assert decided.revision == 2
    assert decided.decision is not None
    assert decided.decision.decision == "approve"
    assert decided.decision.result_fact_version == 2
    assert store.list_facts(project_id=PROJECT_A)[0][0].value == candidate.value
    assert store.summary(PROJECT_A).public_dict() == {
        "project_id": PROJECT_A,
        "revision": 3,
        "current_facts": 1,
        "stale_facts": 0,
        "conflicts": 0,
        "retired_facts": 0,
        "pending_candidates": 0,
    }
    assert [event.event_type for event in store.timeline(project_id=PROJECT_A)[0]] == [
        "memory.fact.revised",
        "memory.candidate.created",
        "memory.candidate.approved",
    ]

    with pytest.raises(memory_store.MemoryStoreError) as repeated:
        store.decide_candidate(
            project_id=PROJECT_A,
            candidate_id=candidate.candidate_id,
            decision="approve",
            expected_candidate_revision=1,
            expected_fact_version=1,
            decided_by=ACTOR,
        )
    assert repeated.value.code == "candidate_version_conflict"
    assert store.summary(PROJECT_A).revision == 3


def test_reject_and_merge_have_distinct_fact_results(store) -> None:
    rejected = _candidate(
        store, candidate_id="mca_" + "1" * 32,
        target_fact_key="risk.one",
    )
    rejected = store.decide_candidate(
        project_id=PROJECT_A,
        candidate_id=rejected.candidate_id,
        decision="reject",
        expected_candidate_revision=1,
        expected_fact_version=0,
        decided_by=ACTOR,
    )
    assert rejected.status == "rejected"
    assert rejected.decision is not None
    assert rejected.decision.result_fact_key is None
    assert store.list_facts(project_id=PROJECT_A)[0] == ()

    merged = _candidate(
        store, candidate_id="mca_" + "2" * 32,
        target_fact_key="goal.merged",
    )
    merged = store.decide_candidate(
        project_id=PROJECT_A,
        candidate_id=merged.candidate_id,
        decision="merge",
        expected_candidate_revision=1,
        expected_fact_version=0,
        decided_by=ACTOR,
        merged_summary="Human merged goal",
        merged_value={"goal": "merged"},
    )
    assert merged.status == "merged"
    facts = {item.fact_key: item for item in store.list_facts(project_id=PROJECT_A)[0]}
    assert facts["goal.merged"].summary == "Human merged goal"
    assert facts["goal.merged"].value == {"goal": "merged"}


def test_candidate_decision_rejects_changed_fact_without_partial_write(store) -> None:
    _append_fact(store)
    candidate = _candidate(store, expected_fact_version=1)
    _append_fact(store, expected_version=1, summary="Concurrent fact")
    before = store.summary(PROJECT_A)
    before_events = store.timeline(project_id=PROJECT_A)[0]

    with pytest.raises(memory_store.MemoryStoreError) as stale:
        store.decide_candidate(
            project_id=PROJECT_A,
            candidate_id=candidate.candidate_id,
            decision="reject",
            expected_candidate_revision=1,
            expected_fact_version=2,
            decided_by=ACTOR,
        )
    assert stale.value.code == "fact_version_conflict"
    assert store.summary(PROJECT_A) == before
    assert store.timeline(project_id=PROJECT_A)[0] == before_events
    current = store.list_candidates(project_id=PROJECT_A)[0][0]
    assert current.status == "pending" and current.decision is None


def test_write_failure_rolls_back_fact_head_revision_and_event(
    store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = "private event insert detail"

    def fail_event(*_args, **_kwargs) -> None:
        raise sqlite3.OperationalError(private)

    monkeypatch.setattr(memory_store, "_append_event", fail_event)
    with pytest.raises(memory_store.MemoryStoreError) as failure:
        _append_fact(store)
    _sanitized(failure, "store_write_failed", private)
    assert store.summary(PROJECT_A).revision == 0
    assert store.list_facts(project_id=PROJECT_A)[0] == ()
    assert store.timeline(project_id=PROJECT_A)[0] == ()


def test_project_scoped_lists_filters_and_paginate_deterministically(store) -> None:
    _append_fact(store, project_id=PROJECT_A, fact_key="a", status="current")
    _append_fact(store, project_id=PROJECT_A, fact_key="b", status="stale")
    _append_fact(store, project_id=PROJECT_B, fact_key="other")
    _candidate(
        store, project_id=PROJECT_A, candidate_id="mca_" + "1" * 32,
        target_fact_key="new-a",
    )
    _candidate(
        store, project_id=PROJECT_A, candidate_id="mca_" + "2" * 32,
        target_fact_key="new-b",
    )
    _candidate(
        store, project_id=PROJECT_B, candidate_id="mca_" + "3" * 32,
        target_fact_key="new-other",
    )

    facts, fact_cursor = store.list_facts(project_id=PROJECT_A, limit=1)
    assert [item.fact_key for item in facts] == ["a"]
    assert fact_cursor == "a"
    facts, fact_cursor = store.list_facts(
        project_id=PROJECT_A, after_key=fact_cursor, statuses=("stale",),
    )
    assert [item.fact_key for item in facts] == ["b"]
    assert fact_cursor is None

    candidates, candidate_cursor = store.list_candidates(
        project_id=PROJECT_A, limit=1,
    )
    assert [item.candidate_id for item in candidates] == ["mca_" + "1" * 32]
    assert candidate_cursor == candidates[0].candidate_id
    candidates, candidate_cursor = store.list_candidates(
        project_id=PROJECT_A, after_candidate_id=candidate_cursor,
    )
    assert [item.candidate_id for item in candidates] == ["mca_" + "2" * 32]
    assert candidate_cursor is None

    events, event_cursor = store.timeline(project_id=PROJECT_A, limit=1)
    assert len(events) == 1 and event_cursor == events[0].seq
    events, _ = store.timeline(project_id=PROJECT_A, after_seq=event_cursor)
    assert events and all(event.project_id == PROJECT_A for event in events)
    assert PROJECT_B not in repr(store.summary(PROJECT_A).public_dict())


def test_invalid_json_integer_and_query_inputs_fail_before_write(store) -> None:
    operations = [
        lambda: _append_fact(store, value={"secret": "never"}),
        lambda: _append_fact(store, expected_version=memory_store.MAX_SQLITE_INTEGER + 1),
        lambda: store.append_fact(
            project_id=PROJECT_A, fact_key="x", kind="state", summary="x",
            value={"x": float("nan")}, source=SOURCE, actor=ACTOR,
            expected_version=0,
        ),
        lambda: store.timeline(
            project_id=PROJECT_A,
            after_seq=memory_store.MAX_SQLITE_INTEGER + 1,
        ),
        lambda: store.list_facts(
            project_id=PROJECT_A, statuses=("current", "current"),
        ),
        lambda: store.list_candidates(project_id=PROJECT_A, limit=101),
    ]
    for operation in operations:
        with pytest.raises(memory_store.MemoryStoreError) as invalid:
            operation()
        assert invalid.value.code == "invalid_argument"
    assert store.summary(PROJECT_A).revision == 0


@pytest.mark.parametrize("corruption", ["missing_fact_revision", "candidate_json"])
def test_materialization_corruption_fails_closed(
    store, path: Path, corruption: str,
) -> None:
    if corruption == "missing_fact_revision":
        _append_fact(store)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE memory_facts SET current_version=2 WHERE project_id=?",
                (PROJECT_A,),
            )
        operation = lambda: store.list_facts(project_id=PROJECT_A)
    else:
        _candidate(store)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE memory_candidates SET value_json='[]' WHERE project_id=?",
                (PROJECT_A,),
            )
        operation = lambda: store.list_candidates(project_id=PROJECT_A)

    with pytest.raises(memory_store.MemoryStoreError) as corrupt:
        operation()
    assert corrupt.value.code == "store_corrupt"


@pytest.mark.parametrize("target", ["project_revision", "fact_head"])
def test_internal_stored_values_fail_as_corruption(
    store, target: str,
) -> None:
    _append_fact(store)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        if target == "project_revision":
            connection.execute(
                "CREATE TABLE memory_projects (project_id TEXT, revision)"
            )
            connection.execute(
                "INSERT INTO memory_projects VALUES (?, ?)", (PROJECT_A, "broken"),
            )
            operation = lambda: memory_store._bump_project(
                connection, PROJECT_A, "2026-08-14T00:00:00Z",
            )
        else:
            connection.execute(
                "CREATE TABLE memory_facts "
                "(project_id TEXT, fact_key TEXT, current_version, kind TEXT)"
            )
            connection.execute(
                "INSERT INTO memory_facts VALUES (?, ?, ?, ?)",
                (PROJECT_A, "goal.current", "broken", "state"),
            )
            operation = lambda: memory_store._current_fact_version(
                connection, PROJECT_A, "goal.current",
            )

        with pytest.raises(memory_store.MemoryStoreError) as corrupt:
            operation()
        assert corrupt.value.code == "store_corrupt"
    finally:
        connection.close()


def test_reads_are_query_only_close_connections_and_write_no_sidecars(
    store, path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _append_fact(store)
    real_connect = memory_store._connect
    observed: list[int] = []

    def checked(target: Path, *, readonly: bool):
        connection = real_connect(target, readonly=readonly)
        if readonly:
            observed.append(connection.execute("PRAGMA query_only").fetchone()[0])
        return connection

    monkeypatch.setattr(memory_store, "_connect", checked)
    before = path.read_bytes()
    assert store.list_facts(project_id=PROJECT_A)[0]
    assert observed == [1]
    assert path.read_bytes() == before
    for suffix in ("-journal", "-wal", "-shm"):
        assert not Path(f"{path}{suffix}").exists()


def test_injectable_g3_reads_are_project_scoped_and_write_is_unavailable(store) -> None:
    _append_fact(store, project_id=PROJECT_A, fact_key="a")
    _append_fact(store, project_id=PROJECT_A, fact_key="b", status="stale")
    _append_fact(store, project_id=PROJECT_B, fact_key="other")
    candidate = _candidate(store, expected_fact_version=0, target_fact_key="new")
    app = FastAPI()
    memory_api.install(app, memory_api.MemoryApiService(lambda: store))
    client = TestClient(app)

    routes = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set())
        if "/memory/" in route.path
    }
    assert routes == {
        ("/api/projects/{project_id}/memory/summary", "GET"),
        ("/api/projects/{project_id}/memory/facts", "GET"),
        ("/api/projects/{project_id}/memory/candidates", "GET"),
        ("/api/projects/{project_id}/memory/timeline", "GET"),
    }

    summary = client.get(f"/api/projects/{PROJECT_A}/memory/summary")
    assert summary.status_code == 200
    payload = summary.json()
    assert set(payload) == {"data", "meta"}
    assert payload["data"]["project_id"] == PROJECT_A
    assert payload["meta"]["capabilities"]["memory.read"]["available"] is True
    assert payload["meta"]["capabilities"]["memory.write"] == {
        "available": False,
        "reason": "authenticated_human_boundary_deferred",
    }

    facts = client.get(
        f"/api/projects/{PROJECT_A}/memory/facts?status=current&limit=1"
    ).json()["data"]
    assert len(facts["items"]) == 1
    assert facts["next_cursor"] is None
    assert facts["items"][0]["project_id"] == PROJECT_A
    assert PROJECT_B not in repr(facts)

    candidates = client.get(
        f"/api/projects/{PROJECT_A}/memory/candidates?status=pending"
    ).json()["data"]
    assert [item["candidate_id"] for item in candidates["items"]] == [
        candidate.candidate_id,
    ]
    timeline = client.get(
        f"/api/projects/{PROJECT_A}/memory/timeline?limit=2"
    ).json()["data"]
    assert timeline["items"]
    assert all(item["project_id"] == PROJECT_A for item in timeline["items"])


def test_api_rejects_noncanonical_queries_and_sanitizes_store_errors(store) -> None:
    app = FastAPI()
    memory_api.install(app, memory_api.MemoryApiService(lambda: store))
    client = TestClient(app)
    invalid_urls = [
        f"/api/projects/{PROJECT_A}/memory/facts?limit=01",
        f"/api/projects/{PROJECT_A}/memory/facts?limit=1&limit=2",
        f"/api/projects/{PROJECT_A}/memory/facts?unknown=x",
        f"/api/projects/{PROJECT_A}/memory/candidates?status=unknown",
        f"/api/projects/{PROJECT_A}/memory/timeline?after_seq=-1",
    ]
    for url in invalid_urls:
        response = client.get(url)
        assert response.status_code == 400
        error = response.json()["error"]
        assert set(error) == {
            "code", "message", "retryable", "request_id", "details",
        }
        assert error["code"] == "invalid_argument"

    private = "private sqlite failure"

    class FailingStore:
        def summary(self, _project_id: str):
            try:
                raise sqlite3.OperationalError(private)
            except sqlite3.Error:
                raise memory_store.MemoryStoreError("store_read_failed") from None

    failed_app = FastAPI()
    memory_api.install(
        failed_app, memory_api.MemoryApiService(lambda: FailingStore()),
    )
    failed = TestClient(failed_app).get(
        f"/api/projects/{PROJECT_A}/memory/summary"
    )
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "store_read_failed"
    assert failed.json()["error"]["retryable"] is True
    assert private not in failed.text
