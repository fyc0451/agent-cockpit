"""Dormant reliable-delivery outbox persistence contract."""
from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import delivery_outbox
import runtime_paths


@pytest.fixture()
def outbox_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("COCKPIT_DATA_DIR", str(data))
    runtime_paths.reset_cache()
    path = runtime_paths.store("delivery_outbox")
    monkeypatch.setattr(delivery_outbox, "DB_PATH", path)
    yield path
    runtime_paths.reset_cache()


def _enqueue(*, key: str = "send-1", now: float = 100.0) -> dict:
    return delivery_outbox.enqueue(
        job_kind="send_message",
        target="project-a/agent-b",
        payload={"subject": "status", "body_md": "done", "to": ["agent-b"]},
        idempotency_key=key,
        now=now,
    )


def test_enqueue_persists_canonical_payload_and_retry_metadata(outbox_db: Path) -> None:
    job = _enqueue()

    assert job["job_kind"] == "send_message"
    assert job["target"] == "project-a/agent-b"
    assert job["payload"] == {
        "body_md": "done", "subject": "status", "to": ["agent-b"],
    }
    canonical = json.dumps(
        job["payload"], ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    assert job["payload_digest"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert job["attempt"] == 0
    assert job["next_attempt_at"] == 100.0
    assert job["status"] == "pending"
    assert job["created_ts"] == job["updated_ts"] == 100.0
    assert job["last_error_summary"] is None

    with sqlite3.connect(outbox_db) as con:
        stored = con.execute(
            "SELECT payload_json FROM delivery_jobs WHERE job_id=?", (job["job_id"],),
        ).fetchone()[0]
    assert stored == canonical


def test_duplicate_enqueue_reuses_record_but_payload_conflict_fails(
    outbox_db: Path,
) -> None:
    first = _enqueue(now=100.0)
    repeated = _enqueue(now=200.0)
    assert repeated == first

    with pytest.raises(delivery_outbox.IdempotencyConflict):
        delivery_outbox.enqueue(
            job_kind="send_message",
            target="project-a/agent-b",
            payload={"subject": "different"},
            idempotency_key="send-1",
            now=300.0,
        )

    with sqlite3.connect(outbox_db) as con:
        assert con.execute("SELECT COUNT(*) FROM delivery_jobs").fetchone()[0] == 1


def test_concurrent_duplicate_enqueue_creates_one_record(outbox_db: Path) -> None:
    barrier = threading.Barrier(8)

    def enqueue_one(_: int) -> dict:
        barrier.wait()
        return _enqueue(key="concurrent-send")

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(enqueue_one, range(8)))

    assert len({job["job_id"] for job in jobs}) == 1
    with sqlite3.connect(outbox_db) as con:
        assert con.execute("SELECT COUNT(*) FROM delivery_jobs").fetchone()[0] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"Authorization": "Bearer private"},
        {"headers": {"registration_token": "private"}},
        {"items": [{"client_secret": "private"}]},
    ],
)
def test_enqueue_rejects_nested_credential_fields(
    outbox_db: Path, payload: dict,
) -> None:
    with pytest.raises(delivery_outbox.OutboxValidationError, match="credential"):
        delivery_outbox.enqueue(
            job_kind="send_message", target="project-a/agent-b",
            payload=payload, idempotency_key="unsafe", now=100.0,
        )
    assert not outbox_db.exists()


def test_public_api_has_no_credential_parameters() -> None:
    params = set(inspect.signature(delivery_outbox.enqueue).parameters)
    assert params == {"job_kind", "target", "payload", "idempotency_key", "now"}
    assert not any(
        word in name.lower()
        for name in params
        for word in ("token", "authorization", "password", "secret", "credential")
    )


def test_legacy_schema_migrates_forward_and_preserves_row(outbox_db: Path) -> None:
    outbox_db.parent.mkdir(parents=True)
    payload_json = '{"subject": "old"}'
    with sqlite3.connect(outbox_db) as con:
        con.executescript(
            """
            CREATE TABLE delivery_jobs (
              job_id TEXT PRIMARY KEY,
              idempotency_key TEXT NOT NULL,
              job_kind TEXT NOT NULL,
              target TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_ts REAL NOT NULL
            );
            INSERT INTO delivery_jobs VALUES(
              'legacy-job', 'legacy-key', 'send_message', 'project-a/agent-b',
              '{"subject": "old"}', 12.5
            );
            """
        )

    job = delivery_outbox.get_job("legacy-job")

    assert job is not None
    assert job["payload"] == {"subject": "old"}
    assert job["payload_digest"] == hashlib.sha256(
        json.dumps(
            json.loads(payload_json), sort_keys=True, separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert job["attempt"] == 0
    assert job["next_attempt_at"] == 12.5
    assert job["status"] == "pending"
    assert job["updated_ts"] == 12.5

    with sqlite3.connect(outbox_db) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(delivery_jobs)")}
        indexes = {row[1]: row[2] for row in con.execute("PRAGMA index_list(delivery_jobs)")}
    assert columns >= {
        "payload_digest", "attempt", "next_attempt_at", "status",
        "updated_ts", "last_error_summary",
    }
    assert indexes["delivery_jobs_idempotency"] == 1


def test_record_is_readable_after_reopening_store(outbox_db: Path) -> None:
    created = _enqueue()
    reopened = delivery_outbox.get_job(created["job_id"])
    assert reopened == created


def test_schema_contains_no_credential_columns(outbox_db: Path) -> None:
    _enqueue()
    with sqlite3.connect(outbox_db) as con:
        columns = {row[1].lower() for row in con.execute("PRAGMA table_info(delivery_jobs)")}
    for name in columns:
        assert not any(
            word in name
            for word in ("token", "authorization", "password", "secret", "credential")
        )


# ── R1.1 reviewer #2120: 必须项覆盖 ──────────────────────────────


def test_store_file_held_at_0600(outbox_db: Path) -> None:
    _enqueue()
    assert (outbox_db.stat().st_mode & 0o777) == 0o600


def test_enqueue_rejects_final_symlink_escape(
    outbox_db: Path, tmp_path: Path,
) -> None:
    outbox_db.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.sqlite3"
    outbox_db.symlink_to(outside)
    with pytest.raises(runtime_paths.PathResolutionError) as exc:
        delivery_outbox.enqueue(
            job_kind="send_message", target="t",
            payload={"subject": "s"}, idempotency_key="sym", now=1.0,
        )
    assert exc.value.reason == "symlink_escape"
    assert not outside.exists()


def test_duplicate_key_different_kind_conflicts(outbox_db: Path) -> None:
    _enqueue()
    with pytest.raises(delivery_outbox.IdempotencyConflict):
        delivery_outbox.enqueue(
            job_kind="other_kind", target="project-a/agent-b",
            payload={"subject": "status", "body_md": "done", "to": ["agent-b"]},
            idempotency_key="send-1", now=200.0,
        )


def test_duplicate_key_different_target_conflicts(outbox_db: Path) -> None:
    _enqueue()
    with pytest.raises(delivery_outbox.IdempotencyConflict):
        delivery_outbox.enqueue(
            job_kind="send_message", target="other/target",
            payload={"subject": "status", "body_md": "done", "to": ["agent-b"]},
            idempotency_key="send-1", now=200.0,
        )


def test_concurrent_different_content_single_winner(outbox_db: Path) -> None:
    barrier = threading.Barrier(8)
    results: list = []
    errors: list = []

    def submit(idx: int) -> None:
        barrier.wait()
        try:
            results.append(delivery_outbox.enqueue(
                job_kind="send_message", target="project-a/agent-b",
                payload={"subject": f"msg-{idx}"}, idempotency_key="race",
                now=100.0,
            ))
        except delivery_outbox.IdempotencyConflict as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(submit, range(8)))

    assert len(results) == 1
    assert len(errors) == 7
    with sqlite3.connect(outbox_db) as con:
        assert con.execute("SELECT COUNT(*) FROM delivery_jobs").fetchone()[0] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"subject": float("nan")},
        {"subject": float("inf")},
        {"subject": float("-inf")},
    ],
)
def test_enqueue_rejects_non_finite_numbers(
    outbox_db: Path, payload: dict,
) -> None:
    with pytest.raises(delivery_outbox.OutboxValidationError, match="non-finite"):
        delivery_outbox.enqueue(
            job_kind="send_message", target="t", payload=payload,
            idempotency_key="nf", now=1.0,
        )
    assert not outbox_db.exists()


def test_enqueue_rejects_non_string_key(outbox_db: Path) -> None:
    with pytest.raises(delivery_outbox.OutboxValidationError, match="non-string key"):
        delivery_outbox.enqueue(
            job_kind="send_message", target="t",
            payload={1: "v"}, idempotency_key="nk", now=1.0,
        )
    assert not outbox_db.exists()


def test_enqueue_rejects_non_serializable_value(outbox_db: Path) -> None:
    with pytest.raises(delivery_outbox.OutboxValidationError, match="non-serializable"):
        delivery_outbox.enqueue(
            job_kind="send_message", target="t",
            payload={"subject": object()}, idempotency_key="ns", now=1.0,
        )
    assert not outbox_db.exists()


def test_credential_error_does_not_echo_payload(outbox_db: Path) -> None:
    secret = "super-secret-value-123"
    with pytest.raises(delivery_outbox.OutboxValidationError) as exc:
        delivery_outbox.enqueue(
            job_kind="send_message", target="t",
            payload={"api_token": secret}, idempotency_key="c", now=1.0,
        )
    assert secret not in str(exc.value)


def test_delivery_outbox_dormant_not_imported_by_server_or_hub_client() -> None:
    root = Path(delivery_outbox.__file__).resolve().parent
    for name in ("server.py", "hub_client.py"):
        p = root / name
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        assert "import delivery_outbox" not in src, name
        assert "from delivery_outbox" not in src, name


# ── REVIEW_BLOCK #2154 B1: 0600 fail-closed ──────────────────────


def test_chmod_failure_fail_closed(outbox_db: Path, monkeypatch) -> None:
    outbox_db.parent.mkdir(parents=True, exist_ok=True)
    outbox_db.touch()
    outbox_db.chmod(0o644)

    def boom(path, mode):
        raise PermissionError("chmod denied")

    monkeypatch.setattr(delivery_outbox.os, "chmod", boom)
    with pytest.raises(delivery_outbox.OutboxStoreError, match="chmod"):
        delivery_outbox.enqueue(
            job_kind="send_message", target="t", payload={"s": 1},
            idempotency_key="x", now=1.0,
        )
    # fail-closed: no schema created, no payload written
    con = sqlite3.connect(outbox_db)
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    con.close()
    assert "delivery_jobs" not in tables


def test_stat_failure_fail_closed(outbox_db: Path, monkeypatch) -> None:
    _enqueue()  # DB at 0600, 1 row

    def stat_boom(*a, **k):
        raise OSError("stat boom")

    monkeypatch.setattr(delivery_outbox.os, "stat", stat_boom)
    # stat failure anywhere in the write path (guard_store symlink probe or
    # mode enforcement) must fail closed — never silently proceed to write.
    with pytest.raises((OSError, delivery_outbox.OutboxStoreError), match="stat"):
        delivery_outbox.enqueue(
            job_kind="send_message", target="t", payload={"s": 2},
            idempotency_key="y", now=2.0,
        )
    # no new payload: still 1 row
    with sqlite3.connect(outbox_db) as con:
        assert con.execute("SELECT COUNT(*) FROM delivery_jobs").fetchone()[0] == 1
