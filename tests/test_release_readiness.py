"""Immutable Server schema evidence and online readiness contract."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import release_readiness
import store_schema


IDENTITY = {
    "version": "1.2.3",
    "source_sha": "a" * 40,
    "edition": "server",
    "instance_id": "test-instance",
    "pid": 123,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_artifact(tmp_path: Path) -> Path:
    root = tmp_path / "artifact"
    static = root / "static"
    static.mkdir(parents=True)
    (root / "VERSION").write_text("1.2.3\n", encoding="ascii")
    (static / "index.html").write_text("ready\n", encoding="ascii")
    digests = {
        rel: _sha256(root / rel)
        for rel in store_schema.required_manifest_digest_paths(root)
    }
    (root / "release-manifest.json").write_text(
        json.dumps({
            "version": IDENTITY["version"],
            "source_sha": IDENTITY["source_sha"],
            "edition": "server",
            "digests": digests,
        }, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )
    return root


def _make_snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root


def _build(tmp_path: Path) -> tuple[dict, Path]:
    artifact = _make_artifact(tmp_path)
    evidence = release_readiness.build_schema_evidence(
        snapshot_root=_make_snapshot(tmp_path),
        artifact_root=artifact,
        identity=IDENTITY,
        backup_inventory_sha256="b" * 64,
    )
    return evidence, artifact


def _write_evidence(tmp_path: Path, evidence: dict) -> Path:
    path = tmp_path / "schema-evidence.json"
    path.write_bytes(release_readiness.canonical_evidence_bytes(evidence))
    os.chmod(path, 0o600)
    return path


def _environ(path: Path, digest: str) -> dict[str, str]:
    return {
        release_readiness.EVIDENCE_PATH_ENV: str(path),
        release_readiness.EVIDENCE_SHA256_ENV: digest,
    }


def test_build_evidence_binds_target_manifest_inventory_and_all_stores(tmp_path):
    evidence, artifact = _build(tmp_path)
    assert evidence["schema_version"] == 1
    assert evidence["target"] == {
        "version": "1.2.3", "source_sha": "a" * 40, "edition": "server",
    }
    assert evidence["release_manifest_sha256"] == _sha256(
        artifact / "release-manifest.json",
    )
    assert evidence["backup_inventory_sha256"] == "b" * 64
    assert [row["name"] for row in evidence["stores"]] == list(
        store_schema._APP_OWNED_STORES,
    )
    assert all(row["state"] == "absent" for row in evidence["stores"])


def test_server_probe_accepts_exact_canonical_evidence_without_writes(tmp_path):
    evidence, artifact = _build(tmp_path)
    path = _write_evidence(tmp_path, evidence)
    env = _environ(path, release_readiness.evidence_sha256(evidence))
    watched = [path, artifact / "release-manifest.json", artifact / "VERSION"]
    before = {item: (_sha256(item), item.stat().st_mtime_ns) for item in watched}

    result = release_readiness.probe_server_evidence(
        IDENTITY, artifact_root=artifact, environ=env,
    )

    assert result["state"] == "compatible"
    assert result["reason"] == store_schema.REASON_COMPATIBLE
    assert before == {
        item: (_sha256(item), item.stat().st_mtime_ns) for item in watched
    }


def test_server_probe_rejects_digest_and_identity_mismatch(tmp_path):
    evidence, artifact = _build(tmp_path)
    path = _write_evidence(tmp_path, evidence)
    bad_digest = release_readiness.probe_server_evidence(
        IDENTITY,
        artifact_root=artifact,
        environ=_environ(path, "0" * 64),
    )
    assert bad_digest["reason"] == (
        release_readiness.REASON_EVIDENCE_DIGEST_MISMATCH
    )

    wrong_identity = {**IDENTITY, "source_sha": "c" * 40}
    mismatch = release_readiness.probe_server_evidence(
        wrong_identity,
        artifact_root=artifact,
        environ=_environ(path, release_readiness.evidence_sha256(evidence)),
    )
    assert mismatch["reason"] == (
        release_readiness.REASON_EVIDENCE_IDENTITY_MISMATCH
    )


def test_server_probe_rejects_manifest_changed_after_seal(tmp_path):
    evidence, artifact = _build(tmp_path)
    path = _write_evidence(tmp_path, evidence)
    manifest = artifact / "release-manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")

    result = release_readiness.probe_server_evidence(
        IDENTITY,
        artifact_root=artifact,
        environ=_environ(path, release_readiness.evidence_sha256(evidence)),
    )
    assert result["reason"] == (
        release_readiness.REASON_EVIDENCE_MANIFEST_MISMATCH
    )


def test_build_rejects_symlinked_static_inventory(tmp_path):
    artifact = _make_artifact(tmp_path)
    static = artifact / "static"
    real_static = artifact / "static-real"
    static.rename(real_static)
    static.symlink_to(real_static, target_is_directory=True)
    try:
        release_readiness.build_schema_evidence(
            snapshot_root=_make_snapshot(tmp_path),
            artifact_root=artifact,
            identity=IDENTITY,
            backup_inventory_sha256="b" * 64,
        )
    except release_readiness.ReadinessEvidenceError as exc:
        assert exc.reason == release_readiness.REASON_EVIDENCE_MANIFEST_MISMATCH
    else:
        raise AssertionError("symlinked static inventory was accepted")


def test_server_probe_rejects_unsafe_mode_and_symlink(tmp_path):
    evidence, artifact = _build(tmp_path)
    path = _write_evidence(tmp_path, evidence)
    digest = release_readiness.evidence_sha256(evidence)
    os.chmod(path, 0o644)
    result = release_readiness.probe_server_evidence(
        IDENTITY, artifact_root=artifact, environ=_environ(path, digest),
    )
    assert result["reason"] == release_readiness.REASON_EVIDENCE_UNSAFE

    os.chmod(path, 0o600)
    link = tmp_path / "evidence-link.json"
    link.symlink_to(path)
    result = release_readiness.probe_server_evidence(
        IDENTITY, artifact_root=artifact, environ=_environ(link, digest),
    )
    assert result["reason"] == release_readiness.REASON_EVIDENCE_UNSAFE


def test_server_probe_rejects_noncanonical_or_unknown_evidence(tmp_path):
    evidence, artifact = _build(tmp_path)
    evidence["unexpected"] = True
    path = _write_evidence(tmp_path, evidence)
    digest = release_readiness.evidence_sha256(evidence)
    result = release_readiness.probe_server_evidence(
        IDENTITY, artifact_root=artifact, environ=_environ(path, digest),
    )
    assert result["reason"] == release_readiness.REASON_EVIDENCE_INVALID

    raw = b'{"schema_version":1,"schema_version":1}\n'
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    result = release_readiness.probe_server_evidence(
        IDENTITY,
        artifact_root=artifact,
        environ=_environ(path, hashlib.sha256(raw).hexdigest()),
    )
    assert result["reason"] == release_readiness.REASON_EVIDENCE_INVALID


def test_snapshot_probe_rejects_future_schema_and_live_sidecars(tmp_path):
    snapshot = _make_snapshot(tmp_path)
    data = snapshot / "data"
    data.mkdir(mode=0o700)
    tasks = data / "tasks.sqlite3"
    con = sqlite3.connect(tasks)
    con.execute("PRAGMA user_version=999")
    con.close()
    os.chmod(tasks, 0o600)
    result = {
        item["name"]: item for item in store_schema.probe_snapshot_stores(snapshot)
    }
    assert result["tasks"]["reason"] == store_schema.REASON_FUTURE_SCHEMA

    wal = Path(str(tasks) + "-wal")
    wal.write_bytes(b"W" * 64)
    before = (_sha256(wal), wal.stat().st_mtime_ns)
    result = {
        item["name"]: item for item in store_schema.probe_snapshot_stores(snapshot)
    }
    assert result["tasks"]["reason"] == (
        store_schema.REASON_PROBE_REQUIRES_QUIESCENCE
    )
    assert before == (_sha256(wal), wal.stat().st_mtime_ns)


def test_snapshot_probe_requires_controller_owned_0600_files(tmp_path):
    snapshot = _make_snapshot(tmp_path)
    data = snapshot / "data"
    data.mkdir(mode=0o700)
    settings = data / "settings.json"
    settings.write_text("{}\n", encoding="ascii")
    os.chmod(settings, 0o644)
    result = {
        item["name"]: item for item in store_schema.probe_snapshot_stores(snapshot)
    }
    assert result["settings"]["reason"] == store_schema.REASON_UNSAFE

    os.chmod(settings, 0o600)
    os.chmod(data, 0o770)
    result = {
        item["name"]: item for item in store_schema.probe_snapshot_stores(snapshot)
    }
    assert result["settings"]["reason"] == store_schema.REASON_UNSAFE


def test_server_ready_uses_evidence_without_opening_active_stores(monkeypatch):
    monkeypatch.setattr(
        store_schema,
        "probe_all_stores",
        lambda: (_ for _ in ()).throw(AssertionError("active store opened")),
    )
    monkeypatch.setattr(
        store_schema,
        "probe_manifest",
        lambda edition, identity=None, root=None: {
            "name": "release_manifest",
            "compat_family": store_schema.COMPAT_FAMILY,
            "state": "compatible",
            "reason": store_schema.REASON_COMPATIBLE,
        },
    )
    monkeypatch.setattr(
        release_readiness,
        "probe_server_evidence",
        lambda identity: {
            "name": "schema_evidence",
            "compat_family": store_schema.COMPAT_FAMILY,
            "state": "compatible",
            "reason": store_schema.REASON_COMPATIBLE,
        },
    )
    body = store_schema.evaluate_ready(IDENTITY)
    assert body["ready"] is True
    assert [item["name"] for item in body["stores"]] == [
        "release_manifest", "schema_evidence",
    ]


def test_candidate_cli_outputs_evidence_and_never_writes_it(tmp_path):
    artifact = _make_artifact(tmp_path)
    snapshot = _make_snapshot(tmp_path)
    script = Path(release_readiness.__file__).resolve()
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--snapshot-root", str(snapshot),
            "--artifact-root", str(artifact),
            "--version", "1.2.3",
            "--source-sha", "a" * 40,
            "--backup-inventory-sha256", "b" * 64,
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["target"]["source_sha"] == "a" * 40
    assert list(snapshot.rglob("*")) == []

    invalid = subprocess.run(
        [
            sys.executable,
            str(script),
            "--snapshot-root", str(snapshot),
            "--artifact-root", str(artifact),
            "--version", "1.2.3",
            "--source-sha", "bad",
            "--backup-inventory-sha256", "b" * 64,
        ],
        capture_output=True,
        check=False,
    )
    assert invalid.returncode == 1
    assert json.loads(invalid.stderr) == {
        "error_code": release_readiness.REASON_EVIDENCE_IDENTITY_MISMATCH,
        "ok": False,
    }
