from __future__ import annotations

import hashlib
import errno
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from agent_cockpit import runtime_paths
from agent_cockpit import upgrade_snapshot


SOURCE_SHA = "a" * 40
TARGET_DIGEST = "b" * 64


@pytest.fixture()
def runtime_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    roots = {
        "data": tmp_path / "live-data",
        "config": tmp_path / "live-config",
        "state": tmp_path / "live-state",
        "uploads": tmp_path / "live-uploads",
    }
    for path in roots.values():
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    monkeypatch.setenv(runtime_paths.ENV_DATA_DIR, str(roots["data"]))
    monkeypatch.setenv(runtime_paths.ENV_CONFIG_DIR, str(roots["config"]))
    monkeypatch.setenv(runtime_paths.ENV_STATE_DIR, str(roots["state"]))
    monkeypatch.setenv(runtime_paths.ENV_UPLOADS_DIR, str(roots["uploads"]))
    monkeypatch.delenv(runtime_paths.ENV_COORDINATION_DB, raising=False)
    runtime_paths.reset_cache()
    yield roots
    runtime_paths.reset_cache()


def _create(runtime_tree: dict[str, Path], **kwargs: object) -> dict[str, object]:
    return upgrade_snapshot.create_backup_snapshot(
        snapshot_root=runtime_tree["data"].parent / "snapshot",
        snapshot_id="snapshot-test",
        request_id="request-test",
        source_sha=SOURCE_SHA,
        target_digest=TARGET_DIGEST,
        **kwargs,
    )


def _entry(result: dict[str, object], name: str) -> dict[str, object]:
    inventory = result["inventory"]
    return next(row for row in inventory["entries"] if row["name"] == name)  # type: ignore[index,union-attr,no-any-return]


def _write_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_bytes(data)
    os.chmod(path, mode)


def test_empty_sources_seal_exact_canonical_closed_inventory(
    runtime_tree: dict[str, Path],
) -> None:
    result = _create(runtime_tree)
    root = result["snapshot_root"]
    inventory_path = result["inventory_path"]
    raw = inventory_path.read_bytes()

    assert root.is_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(inventory_path.stat().st_mode) == 0o600
    assert raw == upgrade_snapshot.canonical_inventory_bytes(result["inventory"])
    assert hashlib.sha256(raw).hexdigest() == result["inventory_sha256"]
    inventory = result["inventory"]
    assert inventory["entry_count"] == 15
    assert inventory["total_snapshot_bytes"] == 0
    names = [row["name"] for row in inventory["entries"]]
    assert names == sorted(set(runtime_paths.STORES) | {"uploads"})
    assert len(names) == len(set(names)) == 15
    assert inventory["consistency_scope"] == "per_store_atomic"

    for name in upgrade_snapshot.SNAPSHOT_STORE_NAMES:
        row = _entry(result, name)
        assert row["source_state"] == "absent"
        assert row["capture"] == "none"
        assert row["reason"] == "source_absent"
        assert row["size_bytes"] is None
        assert row["sha256"] is None
        assert not (root / row["snapshot_relpath"]).exists()
    for name, reason in upgrade_snapshot.PRESERVE_REASONS.items():
        row = _entry(result, name)
        assert row["policy"] == "preserve_in_place"
        assert row["kind"] == "dir"
        assert row["reason"] == reason
        assert row["snapshot_relpath"] is None


def test_stable_json_and_key_are_copied_with_digest_and_mode(
    runtime_tree: dict[str, Path],
) -> None:
    settings = runtime_tree["data"] / "settings.json"
    vapid = runtime_tree["data"] / "vapid-private.pem"
    _write_file(settings, b'{"language":"zh"}\n')
    _write_file(vapid, b"private-key-bytes\n")

    result = _create(runtime_tree)

    for name, source in (("settings", settings), ("vapid", vapid)):
        row = _entry(result, name)
        target = result["snapshot_root"] / row["snapshot_relpath"]
        assert row["source_state"] == "present"
        assert row["capture"] == "stable_file"
        assert row["size_bytes"] == len(source.read_bytes())
        assert row["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert target.read_bytes() == source.read_bytes()
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_sqlite_online_backup_keeps_live_wal_sidecars_unchanged(
    runtime_tree: dict[str, Path],
) -> None:
    source = runtime_tree["data"] / "tasks.sqlite3"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, value TEXT)")
    writer.execute("INSERT INTO tasks(value) VALUES ('kept')")
    writer.commit()
    os.chmod(source, 0o644)
    wal = Path(f"{source}-wal")
    shm = Path(f"{source}-shm")
    before_wal = (wal.read_bytes(), wal.stat().st_mtime_ns)
    try:
        result = _create(runtime_tree)
        assert before_wal == (wal.read_bytes(), wal.stat().st_mtime_ns)
        assert shm.exists()
    finally:
        writer.close()
    row = _entry(result, "tasks")
    target = result["snapshot_root"] / row["snapshot_relpath"]
    assert row["capture"] == "sqlite_backup"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not Path(f"{target}-wal").exists()
    assert not Path(f"{target}-shm").exists()
    with sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True) as copied:
        assert copied.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert copied.execute("SELECT value FROM tasks").fetchone() == ("kept",)


def test_sqlite_backup_uses_verified_fd_when_path_is_replaced_then_restored(
    runtime_tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = runtime_tree["data"] / "tasks.sqlite3"
    replacement = runtime_tree["data"] / "replacement.sqlite3"
    held = runtime_tree["data"] / "held.sqlite3"
    real_connect = sqlite3.connect

    for path, marker in ((source, "original"), (replacement, "replacement")):
        with real_connect(path) as connection:
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.execute("INSERT INTO marker VALUES (?)", (marker,))
        os.chmod(path, 0o644)

    swapped = False

    def swapping_connect(database: object, *args: object, **kwargs: object):
        nonlocal swapped
        if (
            not swapped
            and isinstance(database, str)
            and database.startswith("file:")
            and "/fd/" in database
        ):
            source.rename(held)
            replacement.rename(source)
            try:
                connection = real_connect(database, *args, **kwargs)
            finally:
                source.rename(replacement)
                held.rename(source)
            swapped = True
            return connection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(upgrade_snapshot.sqlite3, "connect", swapping_connect)

    result = _create(runtime_tree)

    assert swapped is True
    row = _entry(result, "tasks")
    target = result["snapshot_root"] / row["snapshot_relpath"]
    with real_connect(target) as copied:
        assert copied.execute("SELECT value FROM marker").fetchone() == ("original",)
    with real_connect(source) as live:
        assert live.execute("SELECT value FROM marker").fetchone() == ("original",)
    with real_connect(replacement) as other:
        assert other.execute("SELECT value FROM marker").fetchone() == ("replacement",)


def test_external_coordination_source_uses_fixed_snapshot_layout(
    runtime_tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    external_dir = runtime_tree["data"].parent / "external"
    external_dir.mkdir(mode=0o700)
    external = external_dir / "coordination.sqlite3"
    with sqlite3.connect(external) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('external')")
    os.chmod(external, 0o600)
    monkeypatch.setenv(runtime_paths.ENV_COORDINATION_DB, str(external))
    runtime_paths.reset_cache()

    result = _create(runtime_tree)

    row = _entry(result, "coordination")
    assert row["source_relpath"] == "coordination.sqlite3"
    assert row["snapshot_relpath"] == "data/coordination.sqlite3"
    target = result["snapshot_root"] / row["snapshot_relpath"]
    with sqlite3.connect(target) as copied:
        assert copied.execute("SELECT value FROM marker").fetchone() == ("external",)
    assert str(external) not in json.dumps(result["inventory"])


def test_preserve_directories_are_recorded_but_never_copied(
    runtime_tree: dict[str, Path],
) -> None:
    worktrees = runtime_tree["data"] / "worktrees"
    upgrade = runtime_tree["data"] / "upgrade"
    for directory in (worktrees, upgrade, runtime_tree["uploads"]):
        directory.mkdir(exist_ok=True, mode=0o700)
        os.chmod(directory, 0o775 if directory == runtime_tree["uploads"] else 0o755)
        (directory / "must-stay-live").write_text("live", encoding="ascii")

    result = _create(runtime_tree)

    assert not list(result["snapshot_root"].rglob("must-stay-live"))
    for name in upgrade_snapshot.PRESERVE_REASONS:
        row = _entry(result, name)
        assert row["source_state"] == "present"
        assert row["kind"] == "dir"


def test_undeclared_source_mode_rejects_writable_but_allows_readable(
    runtime_tree: dict[str, Path],
) -> None:
    tasks = runtime_tree["data"] / "tasks.sqlite3"
    with sqlite3.connect(tasks) as connection:
        connection.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY)")
    os.chmod(tasks, 0o666)

    with pytest.raises(upgrade_snapshot.SnapshotError) as exc_info:
        _create(runtime_tree)

    assert exc_info.value.code == "backup_snapshot_unsafe"


@pytest.mark.parametrize("unsafe", ["symlink", "mode", "directory"])
def test_unsafe_file_source_fails_without_publishing_snapshot(
    runtime_tree: dict[str, Path], unsafe: str
) -> None:
    settings = runtime_tree["data"] / "settings.json"
    if unsafe == "symlink":
        real = runtime_tree["data"] / "real-settings.json"
        _write_file(real, b"{}\n")
        settings.symlink_to(real)
    elif unsafe == "mode":
        _write_file(settings, b"{}\n", mode=0o644)
    else:
        settings.mkdir(mode=0o700)

    with pytest.raises(upgrade_snapshot.SnapshotError) as exc_info:
        _create(runtime_tree)

    assert exc_info.value.code == "backup_snapshot_unsafe"
    assert not (runtime_tree["data"].parent / "snapshot").exists()


def test_source_change_retries_three_times_then_fails(
    runtime_tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_file(runtime_tree["data"] / "settings.json", b"{}\n")
    attempts = 0
    original = upgrade_snapshot._copy_stable_file_once

    def unstable(*args: object, **kwargs: object) -> tuple[int, str]:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise upgrade_snapshot._SourceChanged
        return original(*args, **kwargs)

    monkeypatch.setattr(upgrade_snapshot, "_copy_stable_file_once", unstable)
    with pytest.raises(upgrade_snapshot.SnapshotError) as exc_info:
        _create(runtime_tree)

    assert exc_info.value.code == "snapshot_source_unstable"
    assert attempts == 3
    assert not (runtime_tree["data"].parent / "snapshot").exists()


def test_failure_after_an_earlier_copy_never_publishes_inventory(
    runtime_tree: dict[str, Path],
) -> None:
    _write_file(runtime_tree["data"] / "settings.json", b"{}\n")
    _write_file(runtime_tree["data"] / "tasks.sqlite3", b"not sqlite")

    with pytest.raises(upgrade_snapshot.SnapshotError) as exc_info:
        _create(runtime_tree)

    assert exc_info.value.code == "sqlite_snapshot_invalid"
    assert not (runtime_tree["data"].parent / "snapshot").exists()
    assert not list(runtime_tree["data"].parent.glob(".snapshot.tmp-*"))


def test_entry_limit_and_inventory_inputs_reject_bool_or_oversize(
    runtime_tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_file(runtime_tree["data"] / "settings.json", b"12345")
    monkeypatch.setattr(upgrade_snapshot, "MAX_JSON_BYTES", 4)
    with pytest.raises(upgrade_snapshot.SnapshotError) as exc_info:
        _create(runtime_tree)
    assert exc_info.value.code == "snapshot_limit_exceeded"

    with pytest.raises(upgrade_snapshot.SnapshotError) as exc_info:
        upgrade_snapshot.create_backup_snapshot(
            snapshot_root=runtime_tree["data"].parent / "other-snapshot",
            snapshot_id=True,
            request_id="request-test",
            source_sha=SOURCE_SHA,
            target_digest=TARGET_DIGEST,
        )
    assert exc_info.value.code == "backup_inventory_invalid"


def test_controller_ids_follow_h1_bounded_string_contract(
    runtime_tree: dict[str, Path],
) -> None:
    result = upgrade_snapshot.create_backup_snapshot(
        snapshot_root=runtime_tree["data"].parent / "snapshot",
        snapshot_id="controller snapshot 42",
        request_id="job:42/no-prefix-required",
        source_sha=SOURCE_SHA,
        target_digest=TARGET_DIGEST,
    )
    assert result["inventory"]["snapshot_id"] == "controller snapshot 42"
    assert result["inventory"]["request_id"] == "job:42/no-prefix-required"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snapshot_id", True),
        ("request_id", True),
        ("snapshot_id", "bad\nvalue"),
        ("request_id", "bad\x00value"),
        ("snapshot_id", "x" * 201),
        ("request_id", "x" * 201),
        ("snapshot_id", ""),
        ("request_id", ""),
    ],
)
def test_controller_ids_reject_bool_control_empty_or_oversize(
    runtime_tree: dict[str, Path], field: str, value: object
) -> None:
    kwargs = {
        "snapshot_root": runtime_tree["data"].parent / "snapshot",
        "snapshot_id": "safe-snapshot",
        "request_id": "safe-request",
        "source_sha": SOURCE_SHA,
        "target_digest": TARGET_DIGEST,
    }
    kwargs[field] = value
    with pytest.raises(upgrade_snapshot.SnapshotError) as exc_info:
        upgrade_snapshot.create_backup_snapshot(**kwargs)
    assert exc_info.value.code == "backup_inventory_invalid"


def test_inventory_size_cap_fails_before_snapshot_publication(
    runtime_tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upgrade_snapshot, "MAX_INVENTORY_BYTES", 32)

    with pytest.raises(upgrade_snapshot.SnapshotError) as exc_info:
        _create(runtime_tree)

    assert exc_info.value.code == "snapshot_limit_exceeded"
    assert not (runtime_tree["data"].parent / "snapshot").exists()


def test_disk_full_is_a_stable_limit_error(
    runtime_tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_file(runtime_tree["data"] / "settings.json", b"{}\n")

    def no_space(_fd: int, _data: object) -> int:
        raise OSError(errno.ENOSPC, "path must not leak")

    monkeypatch.setattr(upgrade_snapshot.os, "write", no_space)
    with pytest.raises(upgrade_snapshot.SnapshotError) as exc_info:
        _create(runtime_tree)

    assert exc_info.value.code == "snapshot_limit_exceeded"
    assert str(exc_info.value) == "snapshot_limit_exceeded"


def test_symlinked_runtime_root_is_rejected_as_unsafe(
    runtime_tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = runtime_tree["data"]
    link = real.parent / "data-link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv(runtime_paths.ENV_DATA_DIR, str(link))
    runtime_paths.reset_cache()

    with pytest.raises(upgrade_snapshot.SnapshotError) as exc_info:
        _create(runtime_tree)

    assert exc_info.value.code == "backup_snapshot_unsafe"


def test_existing_snapshot_leaf_is_rejected_without_touching_it(
    runtime_tree: dict[str, Path],
) -> None:
    root = runtime_tree["data"].parent / "snapshot"
    root.mkdir(mode=0o700)
    marker = root / "owned-by-caller"
    marker.write_text("keep", encoding="ascii")

    with pytest.raises(upgrade_snapshot.SnapshotError) as exc_info:
        _create(runtime_tree)

    assert exc_info.value.code == "backup_snapshot_unsafe"
    assert marker.read_text(encoding="ascii") == "keep"


def test_failure_after_inventory_seal_does_not_clean_sealed_root(
    runtime_tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original = upgrade_snapshot._fsync_directory

    def fail_after_seal(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "post-seal fsync")
        original(path)

    monkeypatch.setattr(upgrade_snapshot, "_fsync_directory", fail_after_seal)
    with pytest.raises(upgrade_snapshot.SnapshotError):
        _create(runtime_tree)

    root = runtime_tree["data"].parent / "snapshot"
    assert root.is_dir()
    assert (root / upgrade_snapshot.INVENTORY_NAME).is_file()
