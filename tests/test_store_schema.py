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
    import leader_binding

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


def test_coordination_assignment_schema_migrates_existing_store(
    isolated_roots, monkeypatch,
):
    import coordination

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


def test_inbox_route_exact_value_shape(isolated_roots):
    path = runtime_paths.store("inbox_route")
    path.write_text(json.dumps({
        "version": 2,
        "routes": {
            "h:1": {"delivered": [], "last_delivered": [], "pending": []},
        },
    }), encoding="utf-8")
    assert store_schema._check_versioned_json("inbox_route")["reason"] == (
        store_schema.REASON_COMPATIBLE
    )
    path.write_text(json.dumps({
        "version": 2,
        "routes": {"h:1": {"delivered": [], "opaque": 1}},
    }), encoding="utf-8")
    r = store_schema._check_versioned_json("inbox_route")
    assert r["reason"] in {
        store_schema.REASON_FINGERPRINT_MISMATCH,
        store_schema.REASON_UNKNOWN_FIELDS,
    }


def test_inbox_route_nested_entries_fail_closed(isolated_roots):
    """Lead R4 counter-example (1): garbage nested entries not compatible."""
    path = runtime_paths.store("inbox_route")
    path.write_text(json.dumps({
        "version": 2,
        "routes": {
            "h:1": {
                "delivered": [True],
                "last_delivered": [{"future": 1}],
                "pending": ["garbage"],
            },
        },
    }), encoding="utf-8")
    r = store_schema._check_versioned_json("inbox_route")
    assert r["state"] != "compatible"
    assert r["reason"] in {
        store_schema.REASON_FINGERPRINT_MISMATCH,
        store_schema.REASON_UNKNOWN_FIELDS,
    }


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


def test_manifest_wrong_identity_binding_fail_closed():
    """Lead R2 counter-example (3): WRONG version / not-a-sha → not compatible."""
    root = Path(store_schema.__file__).resolve().parent
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
