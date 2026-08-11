"""Immutable Server schema evidence and online readiness contract."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import release_readiness
import runtime_paths
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
    static.mkdir(parents=True, exist_ok=True)
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


def _inventory_entry(name: str) -> dict:
    if name == "uploads":
        logical_root, rel = "uploads", "."
    else:
        logical_root, rel = runtime_paths.STORES[name][0:2]
    if name in release_readiness._SQLITE_STORES:
        kind, policy, reason = "sqlite", "snapshot", "source_absent"
    elif name in release_readiness._JSON_STORES:
        kind, policy, reason = "json", "snapshot", "source_absent"
    elif name == "vapid":
        kind, policy, reason = "key", "snapshot", "source_absent"
    else:
        kind, policy = "dir", "preserve_in_place"
        reason = release_readiness._PRESERVED[name]
    return {
        "name": name,
        "logical_root": logical_root,
        "source_relpath": rel,
        "kind": kind,
        "policy": policy,
        "source_state": "absent",
        "capture": "none",
        "snapshot_relpath": f"{logical_root}/{rel}" if policy == "snapshot" else None,
        "size_bytes": None,
        "sha256": None,
        "mode": None,
        "reason": reason,
    }


def _inventory() -> dict:
    entries = [
        _inventory_entry(name)
        for name in release_readiness._EXPECTED_INVENTORY_NAMES
    ]
    return {
        "schema_version": 1,
        "engine": "immutable-upgrade-controller",
        "snapshot_id": "snapshot-1",
        "request_id": "request-1",
        "source_sha": "c" * 40,
        "target_digest": "d" * 64,
        "captured_at": "2026-08-11T00:00:00Z",
        "consistency_scope": "per_store_atomic",
        "entry_count": len(entries),
        "total_snapshot_bytes": 0,
        "entries": entries,
    }


def _write_inventory(snapshot: Path, inventory: dict | None = None) -> tuple[Path, str]:
    path = snapshot / release_readiness.INVENTORY_NAME
    raw = release_readiness.canonical_inventory_bytes(inventory or _inventory())
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return path, hashlib.sha256(raw).hexdigest()


def _mark_present(inventory: dict, name: str, payload: bytes) -> dict:
    entry = next(item for item in inventory["entries"] if item["name"] == name)
    entry.update({
        "source_state": "present",
        "capture": "sqlite_backup" if entry["kind"] == "sqlite" else "stable_file",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "mode": 0o600,
        "reason": None,
    })
    inventory["total_snapshot_bytes"] = sum(
        item["size_bytes"] or 0 for item in inventory["entries"]
    )
    return entry


def _build_with_inventory(
    tmp_path: Path,
    snapshot: Path,
    inventory_path: Path,
    inventory_digest: str,
) -> dict:
    return release_readiness.build_schema_evidence(
        snapshot_root=snapshot,
        artifact_root=_make_artifact(tmp_path),
        identity=IDENTITY,
        backup_inventory_path=inventory_path,
        backup_inventory_sha256=inventory_digest,
    )


def _build(tmp_path: Path) -> tuple[dict, Path]:
    artifact = _make_artifact(tmp_path)
    snapshot = _make_snapshot(tmp_path)
    inventory_path, inventory_digest = _write_inventory(snapshot)
    evidence = release_readiness.build_schema_evidence(
        snapshot_root=snapshot,
        artifact_root=artifact,
        identity=IDENTITY,
        backup_inventory_path=inventory_path,
        backup_inventory_sha256=inventory_digest,
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
    assert evidence["backup_inventory_sha256"] == hashlib.sha256(
        release_readiness.canonical_inventory_bytes(_inventory())
    ).hexdigest()
    assert [row["name"] for row in evidence["stores"]] == list(
        store_schema._APP_OWNED_STORES,
    )
    assert all(row["state"] == "absent" for row in evidence["stores"])


def test_inventory_present_file_is_bound_to_snapshot_bytes(tmp_path):
    snapshot = _make_snapshot(tmp_path)
    data = snapshot / "data"
    data.mkdir(mode=0o700)
    payload = b"{}\n"
    settings = data / "settings.json"
    settings.write_bytes(payload)
    os.chmod(settings, 0o600)
    inventory = _inventory()
    _mark_present(inventory, "settings", payload)
    path, digest = _write_inventory(snapshot, inventory)

    evidence = _build_with_inventory(tmp_path, snapshot, path, digest)

    settings_row = next(item for item in evidence["stores"] if item["name"] == "settings")
    assert settings_row["state"] == "compatible"


def test_empty_snapshot_without_inventory_fails_closed(tmp_path):
    snapshot = _make_snapshot(tmp_path)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as error:
        _build_with_inventory(
            tmp_path,
            snapshot,
            snapshot / release_readiness.INVENTORY_NAME,
            "b" * 64,
        )
    assert error.value.reason == release_readiness.REASON_SNAPSHOT_UNSAFE


def test_inventory_digest_and_canonical_encoding_are_required(tmp_path):
    snapshot = _make_snapshot(tmp_path)
    path, digest = _write_inventory(snapshot)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as mismatch:
        _build_with_inventory(tmp_path, snapshot, path, "0" * 64)
    assert mismatch.value.reason == release_readiness.REASON_INVENTORY_DIGEST_MISMATCH

    raw = json.dumps(_inventory(), indent=2).encode("ascii")
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as noncanonical:
        _build_with_inventory(tmp_path, snapshot, path, hashlib.sha256(raw).hexdigest())
    assert noncanonical.value.reason == release_readiness.REASON_INVENTORY_INVALID

    duplicate_key = b'{"schema_version":1,"schema_version":1}\n'
    path.write_bytes(duplicate_key)
    os.chmod(path, 0o600)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as duplicate:
        _build_with_inventory(
            tmp_path, snapshot, path, hashlib.sha256(duplicate_key).hexdigest(),
        )
    assert duplicate.value.reason == release_readiness.REASON_INVENTORY_INVALID


@pytest.mark.parametrize("case", ["missing", "extra", "duplicate"])
def test_inventory_requires_exact_fifteen_entry_closed_set(tmp_path, case):
    snapshot = _make_snapshot(tmp_path)
    inventory = _inventory()
    if case == "missing":
        inventory["entries"].pop()
        inventory["entry_count"] -= 1
    elif case == "extra":
        inventory["entries"].append({**inventory["entries"][-1], "name": "unknown"})
        inventory["entry_count"] += 1
    else:
        inventory["entries"][-1] = dict(inventory["entries"][-2])
    path, digest = _write_inventory(snapshot, inventory)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as error:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert error.value.reason == release_readiness.REASON_INVENTORY_INCOMPLETE


def test_inventory_rejects_unknown_fields_and_bool_counts(tmp_path):
    snapshot = _make_snapshot(tmp_path)
    inventory = _inventory()
    inventory["unknown"] = True
    path, digest = _write_inventory(snapshot, inventory)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as unknown:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert unknown.value.reason == release_readiness.REASON_INVENTORY_INVALID

    inventory = _inventory()
    inventory["entry_count"] = True
    path, digest = _write_inventory(snapshot, inventory)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as wrong_type:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert wrong_type.value.reason == release_readiness.REASON_INVENTORY_INCOMPLETE

    inventory = _inventory()
    inventory["entries"][0]["source_state"] = []
    path, digest = _write_inventory(snapshot, inventory)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as malformed:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert malformed.value.reason == release_readiness.REASON_INVENTORY_POLICY_MISMATCH


def test_inventory_path_and_leaf_symlinks_are_rejected(tmp_path):
    snapshot = _make_snapshot(tmp_path)
    real = tmp_path / "real-inventory.json"
    raw = release_readiness.canonical_inventory_bytes(_inventory())
    real.write_bytes(raw)
    os.chmod(real, 0o600)
    linked = snapshot / release_readiness.INVENTORY_NAME
    linked.symlink_to(real)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as inventory_link:
        _build_with_inventory(tmp_path, snapshot, linked, hashlib.sha256(raw).hexdigest())
    assert inventory_link.value.reason == release_readiness.REASON_SNAPSHOT_UNSAFE

    linked.unlink()
    data = snapshot / "data"
    data.mkdir(mode=0o700)
    target = tmp_path / "settings.json"
    target.write_bytes(b"{}\n")
    os.chmod(target, 0o600)
    (data / "settings.json").symlink_to(target)
    inventory = _inventory()
    _mark_present(inventory, "settings", b"{}\n")
    path, digest = _write_inventory(snapshot, inventory)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as snapshot_link:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert snapshot_link.value.reason == release_readiness.REASON_SNAPSHOT_UNSAFE


def test_snapshot_root_inventory_mode_and_owner_are_exact(
    tmp_path, monkeypatch,
):
    snapshot = _make_snapshot(tmp_path)
    path, digest = _write_inventory(snapshot)
    os.chmod(snapshot, 0o750)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as root_mode:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert root_mode.value.reason == release_readiness.REASON_SNAPSHOT_UNSAFE

    os.chmod(snapshot, 0o700)
    os.chmod(path, 0o640)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as inventory_mode:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert inventory_mode.value.reason == release_readiness.REASON_SNAPSHOT_UNSAFE

    os.chmod(path, 0o600)
    original_fstat = release_readiness.os.fstat
    real_uid = os.getuid()
    calls = 0

    def wrong_inventory_owner(fd):
        nonlocal calls
        calls += 1
        value = original_fstat(fd)
        if calls == 1:
            fields = list(value)
            fields[4] = real_uid + 1
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(release_readiness.os, "fstat", wrong_inventory_owner)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as owner:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert owner.value.reason == release_readiness.REASON_SNAPSHOT_UNSAFE


@pytest.mark.parametrize("mismatch", ["absent_has_file", "present_missing", "size", "digest", "mode"])
def test_inventory_snapshot_presence_and_metadata_must_match(tmp_path, mismatch):
    snapshot = _make_snapshot(tmp_path)
    data = snapshot / "data"
    data.mkdir(mode=0o700)
    settings = data / "settings.json"
    payload = b"{}\n"
    inventory = _inventory()
    if mismatch != "present_missing":
        settings.write_bytes(payload)
        os.chmod(settings, 0o600)
    if mismatch != "absent_has_file":
        entry = _mark_present(inventory, "settings", payload)
        if mismatch == "size":
            entry["size_bytes"] += 1
            inventory["total_snapshot_bytes"] += 1
        elif mismatch == "digest":
            entry["sha256"] = "0" * 64
        elif mismatch == "mode":
            os.chmod(settings, 0o644)
    path, digest = _write_inventory(snapshot, inventory)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as error:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    expected = (
        release_readiness.REASON_SNAPSHOT_UNSAFE
        if mismatch == "mode"
        else release_readiness.REASON_INVENTORY_SNAPSHOT_MISMATCH
    )
    assert error.value.reason == expected


def test_inventory_rejects_wrong_fixed_path_policy_and_extra_snapshot_content(tmp_path):
    snapshot = _make_snapshot(tmp_path)
    inventory = _inventory()
    settings = next(item for item in inventory["entries"] if item["name"] == "settings")
    settings["snapshot_relpath"] = "data/../settings.json"
    path, digest = _write_inventory(snapshot, inventory)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as wrong_path:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert wrong_path.value.reason == release_readiness.REASON_INVENTORY_POLICY_MISMATCH

    inventory = _inventory()
    worktrees = next(item for item in inventory["entries"] if item["name"] == "worktrees")
    worktrees["policy"] = "snapshot"
    path, digest = _write_inventory(snapshot, inventory)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as policy:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert policy.value.reason == release_readiness.REASON_INVENTORY_POLICY_MISMATCH

    inventory = _inventory()
    extra = snapshot / "data" / "extra.json"
    extra.parent.mkdir(mode=0o700)
    extra.write_text("{}\n", encoding="ascii")
    os.chmod(extra, 0o600)
    path, digest = _write_inventory(snapshot, inventory)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as extra_file:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert extra_file.value.reason == release_readiness.REASON_INVENTORY_SNAPSHOT_MISMATCH


def test_inventory_and_store_size_limits_fail_closed_without_allocating_payloads(tmp_path):
    snapshot = _make_snapshot(tmp_path)
    oversized = b" " * (release_readiness.MAX_INVENTORY_BYTES + 1)
    path = snapshot / release_readiness.INVENTORY_NAME
    path.write_bytes(oversized)
    os.chmod(path, 0o600)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as inventory_limit:
        _build_with_inventory(tmp_path, snapshot, path, hashlib.sha256(oversized).hexdigest())
    assert inventory_limit.value.reason == release_readiness.REASON_SNAPSHOT_LIMIT_EXCEEDED

    inventory = _inventory()
    entry = next(item for item in inventory["entries"] if item["name"] == "settings")
    entry.update({
        "source_state": "present", "capture": "stable_file",
        "size_bytes": release_readiness.MAX_JSON_BYTES + 1,
        "sha256": "e" * 64, "mode": 0o600, "reason": None,
    })
    inventory["total_snapshot_bytes"] = entry["size_bytes"]
    path, digest = _write_inventory(snapshot, inventory)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as store_limit:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert store_limit.value.reason == release_readiness.REASON_SNAPSHOT_LIMIT_EXCEEDED


@pytest.mark.parametrize(
    ("name", "limit"),
    [
        ("settings", release_readiness.MAX_JSON_BYTES),
        ("vapid", release_readiness.MAX_VAPID_BYTES),
        ("tasks", release_readiness.MAX_SQLITE_BYTES),
    ],
)
def test_each_store_class_has_a_hard_size_limit(tmp_path, name, limit):
    snapshot = _make_snapshot(tmp_path)
    inventory = _inventory()
    entry = next(item for item in inventory["entries"] if item["name"] == name)
    entry.update({
        "source_state": "present",
        "capture": "sqlite_backup" if entry["kind"] == "sqlite" else "stable_file",
        "size_bytes": limit + 1,
        "sha256": "e" * 64,
        "mode": 0o600,
        "reason": None,
    })
    inventory["total_snapshot_bytes"] = entry["size_bytes"]
    path, digest = _write_inventory(snapshot, inventory)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as error:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert error.value.reason == release_readiness.REASON_SNAPSHOT_LIMIT_EXCEEDED


def test_total_snapshot_size_limit_is_independent_of_per_store_limits(tmp_path):
    snapshot = _make_snapshot(tmp_path)
    inventory = _inventory()
    sizes = {
        "tasks": release_readiness.MAX_SQLITE_BYTES,
        "coordination": release_readiness.MAX_SQLITE_BYTES,
        "push": 1,
    }
    for name, size in sizes.items():
        entry = next(item for item in inventory["entries"] if item["name"] == name)
        entry.update({
            "source_state": "present",
            "capture": "sqlite_backup",
            "size_bytes": size,
            "sha256": "e" * 64,
            "mode": 0o600,
            "reason": None,
        })
    inventory["total_snapshot_bytes"] = sum(sizes.values())
    path, digest = _write_inventory(snapshot, inventory)
    with pytest.raises(release_readiness.ReadinessEvidenceError) as error:
        _build_with_inventory(tmp_path, snapshot, path, digest)
    assert error.value.reason == release_readiness.REASON_SNAPSHOT_LIMIT_EXCEEDED


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
    snapshot = _make_snapshot(tmp_path)
    inventory_path, inventory_digest = _write_inventory(snapshot)
    static = artifact / "static"
    real_static = artifact / "static-real"
    static.rename(real_static)
    static.symlink_to(real_static, target_is_directory=True)
    try:
        release_readiness.build_schema_evidence(
            snapshot_root=snapshot,
            artifact_root=artifact,
            identity=IDENTITY,
            backup_inventory_path=inventory_path,
            backup_inventory_sha256=inventory_digest,
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
    inventory_path, inventory_digest = _write_inventory(snapshot)
    script = Path(release_readiness.__file__).resolve()
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--snapshot-root", str(snapshot),
            "--artifact-root", str(artifact),
            "--version", "1.2.3",
            "--source-sha", "a" * 40,
            "--backup-inventory-path", str(inventory_path),
            "--backup-inventory-sha256", inventory_digest,
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["target"]["source_sha"] == "a" * 40
    assert list(snapshot.rglob("*")) == [inventory_path]

    invalid = subprocess.run(
        [
            sys.executable,
            str(script),
            "--snapshot-root", str(snapshot),
            "--artifact-root", str(artifact),
            "--version", "1.2.3",
            "--source-sha", "bad",
            "--backup-inventory-path", str(inventory_path),
            "--backup-inventory-sha256", inventory_digest,
        ],
        capture_output=True,
        check=False,
    )
    assert invalid.returncode == 1
    assert json.loads(invalid.stderr) == {
        "error_code": release_readiness.REASON_EVIDENCE_IDENTITY_MISMATCH,
        "ok": False,
    }
