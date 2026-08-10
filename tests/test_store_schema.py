"""J1 store_schema pure-read probes + /health/ready contract."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import runtime_paths
import store_schema


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
    assert results["coordination"]["reason"] == store_schema.REASON_MISSING_CREATABLE
    after_files = list(tmp_path.rglob("*"))
    # probe must not create db/json/wal/shm
    created = [p for p in after_files if p.is_file() and p not in before]
    assert created == []


def test_tasks_fingerprint_compatible(isolated_roots):
    path = runtime_paths.store("tasks")
    con = sqlite3.connect(path)
    con.executescript(
        """
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
        );
        """
    )
    con.close()
    r = store_schema._check_sqlite("tasks", {"tasks": store_schema._TASKS_COLUMNS})
    assert r["reason"] == store_schema.REASON_COMPATIBLE


def test_future_column_fail_closed(isolated_roots):
    path = runtime_paths.store("tasks")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, workdir TEXT NOT NULL, "
        "prompt TEXT NOT NULL, images TEXT, model TEXT, status TEXT NOT NULL, "
        "pid INTEGER, exit_code INTEGER, created_ts REAL NOT NULL, "
        "started_ts REAL, finished_ts REAL, output_tail TEXT, "
        "source_workdir TEXT, base_sha TEXT, run_workdir TEXT, "
        "preview_hash TEXT, evil_future TEXT)"
    )
    con.close()
    r = store_schema._check_sqlite("tasks", {"tasks": store_schema._TASKS_COLUMNS})
    assert r["reason"] == store_schema.REASON_FUTURE_SCHEMA


def test_corrupt_sqlite(isolated_roots):
    path = runtime_paths.store("push")
    path.write_text("not-a-database", encoding="utf-8")
    r = store_schema._check_sqlite(
        "push", {"subscriptions": store_schema._PUSH_COLUMNS},
    )
    assert r["reason"] in {
        store_schema.REASON_CORRUPT, store_schema.REASON_UNREADABLE,
    }


def test_wal_without_shm_requires_quiescence(isolated_roots):
    path = runtime_paths.store("tasks")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, workdir TEXT NOT NULL, "
        "prompt TEXT NOT NULL, images TEXT, model TEXT, status TEXT NOT NULL, "
        "pid INTEGER, exit_code INTEGER, created_ts REAL NOT NULL, "
        "started_ts REAL, finished_ts REAL, output_tail TEXT, "
        "source_workdir TEXT, base_sha TEXT, run_workdir TEXT, preview_hash TEXT)"
    )
    con.close()
    Path(str(path) + "-wal").write_bytes(b"x" * 32)
    # no -shm
    r = store_schema._check_sqlite("tasks", {"tasks": store_schema._TASKS_COLUMNS})
    assert r["reason"] == store_schema.REASON_PROBE_REQUIRES_QUIESCENCE


def test_settings_unknown_field_future(isolated_roots):
    path = runtime_paths.store("settings")
    path.write_text(json.dumps({
        "language": "zh", "dir_agents": {}, "enabled_agents": [],
        "upload_max_mb": 1, "team_hub_url": "", "human_auth_url": "",
        "term": {}, "unexpected_key": 1,
    }), encoding="utf-8")
    r = store_schema._check_settings()
    assert r["reason"] == store_schema.REASON_UNKNOWN_FIELDS


def test_mail_projects_future_version(isolated_roots):
    path = runtime_paths.store("mail_projects")
    path.write_text(json.dumps({"version": 99, "sessions": {}}), encoding="utf-8")
    r = store_schema._check_versioned_json("mail_projects")
    assert r["reason"] == store_schema.REASON_FUTURE_SCHEMA


def test_file_roots_unsafe_slash(isolated_roots):
    import files
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
    client = TestClient(server.app)
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
    import release_identity
    import server
    release_identity._cached = None  # noqa: SLF001
    runtime_paths.reset_cache()
    importlib.reload(store_schema)
    importlib.reload(server)
    client = TestClient(server.app)
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
    client = TestClient(server.app)
    # Even if herdr unavailable, /health stays 200
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] in {"ok", "degraded"}


def test_no_server_import_in_store_schema():
    src = Path(store_schema.__file__).read_text(encoding="utf-8")
    assert "import server" not in src
    assert "from server" not in src
