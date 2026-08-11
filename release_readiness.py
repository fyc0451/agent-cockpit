"""Sealed schema evidence for immutable Server release readiness.

The upgrade controller runs the target release's schema probe against a
complete backup tree before activation. The Server process later validates the
canonical evidence and its configured SHA-256 without opening active stores.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

import runtime_paths
import store_schema

EVIDENCE_PATH_ENV = "COCKPIT_SCHEMA_EVIDENCE_PATH"
EVIDENCE_SHA256_ENV = "COCKPIT_SCHEMA_EVIDENCE_SHA256"
EVIDENCE_SCHEMA_VERSION = 1
MAX_EVIDENCE_BYTES = 128 * 1024
MAX_INVENTORY_BYTES = 256 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_VAPID_BYTES = 64 * 1024
MAX_SQLITE_BYTES = 16 * 1024 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024 * 1024
INVENTORY_NAME = "backup-inventory.json"

REASON_EVIDENCE_MISSING = "schema_evidence_missing"
REASON_EVIDENCE_UNSAFE = "schema_evidence_unsafe"
REASON_EVIDENCE_DIGEST_MISMATCH = "schema_evidence_digest_mismatch"
REASON_EVIDENCE_INVALID = "schema_evidence_invalid"
REASON_EVIDENCE_IDENTITY_MISMATCH = "schema_evidence_identity_mismatch"
REASON_EVIDENCE_MANIFEST_MISMATCH = "schema_evidence_manifest_mismatch"
REASON_SCHEMA_INCOMPATIBLE = "schema_incompatible"
REASON_INVENTORY_INVALID = "backup_inventory_invalid"
REASON_INVENTORY_INCOMPLETE = "backup_inventory_incomplete"
REASON_INVENTORY_DIGEST_MISMATCH = "backup_inventory_digest_mismatch"
REASON_INVENTORY_SNAPSHOT_MISMATCH = "backup_inventory_snapshot_mismatch"
REASON_INVENTORY_POLICY_MISMATCH = "backup_inventory_policy_mismatch"
REASON_SNAPSHOT_UNSAFE = "backup_snapshot_unsafe"
REASON_SNAPSHOT_LIMIT_EXCEEDED = "snapshot_limit_exceeded"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$",
)
_EVIDENCE_KEYS = frozenset({
    "schema_version", "compat_family", "target",
    "release_manifest_sha256", "backup_inventory_sha256", "stores",
})
_TARGET_KEYS = frozenset({"version", "source_sha", "edition"})
_STORE_KEYS = frozenset({"name", "compat_family", "state", "reason"})
_INVENTORY_KEYS = frozenset({
    "schema_version", "engine", "snapshot_id", "request_id", "source_sha",
    "target_digest", "captured_at", "consistency_scope", "entry_count",
    "total_snapshot_bytes", "entries",
})
_INVENTORY_ENTRY_KEYS = frozenset({
    "name", "logical_root", "source_relpath", "kind", "policy",
    "source_state", "capture", "snapshot_relpath", "size_bytes", "sha256",
    "mode", "reason",
})
_SQLITE_STORES = frozenset({
    "tasks", "coordination", "leader_binding", "push", "delivery_outbox",
})
_JSON_STORES = frozenset({
    "settings", "mail_projects", "team_sessions", "inbox_route", "typing",
    "file_roots",
})
_PRESERVED = {
    "worktrees": "live_task_workspaces",
    "upgrade": "controller_evidence",
    "uploads": "live_upload_payloads",
}
_EXPECTED_INVENTORY_NAMES = tuple(sorted((*runtime_paths.STORES, "uploads")))
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class ReadinessEvidenceError(ValueError):
    """Fail-closed evidence error carrying only a stable reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _result(state: str, reason: str) -> dict[str, Any]:
    return {
        "name": "schema_evidence",
        "compat_family": store_schema.COMPAT_FAMILY,
        "state": state,
        "reason": reason,
    }


def canonical_evidence_bytes(evidence: dict[str, Any]) -> bytes:
    try:
        text = json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID) from exc
    return (text + "\n").encode("ascii")


def evidence_sha256(evidence: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_evidence_bytes(evidence)).hexdigest()


def canonical_inventory_bytes(inventory: dict[str, Any]) -> bytes:
    try:
        text = json.dumps(
            inventory,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID) from exc
    return (text + "\n").encode("ascii")


def _target(identity: Mapping[str, Any]) -> dict[str, str]:
    version = identity.get("version")
    source_sha = identity.get("source_sha")
    edition = identity.get("edition")
    if (
        not isinstance(version, str)
        or not _SEMVER_RE.fullmatch(version)
        or not isinstance(source_sha, str)
        or not _GIT_SHA_RE.fullmatch(source_sha)
        or edition != "server"
    ):
        raise ReadinessEvidenceError(REASON_EVIDENCE_IDENTITY_MISMATCH)
    return {
        "version": version,
        "source_sha": source_sha,
        "edition": "server",
    }


def _has_symlink_component(path: Path) -> bool:
    try:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            if current.is_symlink():
                return True
    except OSError:
        return True
    return False


def _secure_root(path: Path) -> Path:
    root = Path(path)
    if not root.is_absolute() or Path(os.path.abspath(root)) != root:
        raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE)
    if _has_symlink_component(root):
        raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE)
    try:
        info = root.lstat()
    except OSError as exc:
        raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE)
    return root


def _secure_snapshot_root(path: Path) -> Path:
    root = Path(path)
    if not root.is_absolute() or Path(os.path.abspath(root)) != root:
        raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE)
    if _has_symlink_component(root):
        raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE)
    try:
        info = root.lstat()
    except OSError as exc:
        raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE)
    return root


def _read_inventory(path: Path) -> tuple[bytes, tuple[int, int, int, int]]:
    if _has_symlink_component(path):
        raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE) from exc
    try:
        before = os.fstat(fd)
        if before.st_size > MAX_INVENTORY_BYTES:
            raise ReadinessEvidenceError(REASON_SNAPSHOT_LIMIT_EXCEEDED)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
        ):
            raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE)
        raw = bytearray()
        while len(raw) <= MAX_INVENTORY_BYTES:
            chunk = os.read(fd, min(65536, MAX_INVENTORY_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if len(raw) != before.st_size or signature != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE)
        try:
            current = path.lstat()
        except OSError as exc:
            raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE) from exc
        if (current.st_dev, current.st_ino) != signature[:2]:
            raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE)
        return bytes(raw), signature
    finally:
        os.close(fd)


def _parse_inventory(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("ascii")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID) from exc
    if not isinstance(value, dict) or canonical_inventory_bytes(value) != raw:
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    return value


def _expected_entry(name: str) -> tuple[str, str, str, str, str | None]:
    if name == "uploads":
        return "uploads", ".", "dir", "preserve_in_place", _PRESERVED[name]
    logical_root, rel = runtime_paths.STORES[name][0:2]
    if name in _SQLITE_STORES:
        return logical_root, rel, "sqlite", "snapshot", None
    if name in _JSON_STORES:
        return logical_root, rel, "json", "snapshot", None
    if name == "vapid":
        return logical_root, rel, "key", "snapshot", None
    return logical_root, rel, "dir", "preserve_in_place", _PRESERVED[name]


def _validate_inventory_entry(entry: Any, expected_name: str) -> None:
    if not isinstance(entry, dict) or set(entry) != _INVENTORY_ENTRY_KEYS:
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    if entry.get("name") != expected_name:
        raise ReadinessEvidenceError(REASON_INVENTORY_INCOMPLETE)
    logical_root, rel, kind, policy, preserved_reason = _expected_entry(expected_name)
    source_state = entry.get("source_state")
    if (
        entry.get("logical_root") != logical_root
        or entry.get("source_relpath") != rel
        or entry.get("kind") != kind
        or entry.get("policy") != policy
        or type(source_state) is not str
        or source_state not in {"present", "absent"}
    ):
        raise ReadinessEvidenceError(REASON_INVENTORY_POLICY_MISMATCH)

    if policy == "preserve_in_place":
        if any(entry.get(key) is not None for key in ("snapshot_relpath", "size_bytes", "sha256", "mode")):
            raise ReadinessEvidenceError(REASON_INVENTORY_POLICY_MISMATCH)
        if entry.get("capture") != "none" or entry.get("reason") != preserved_reason:
            raise ReadinessEvidenceError(REASON_INVENTORY_POLICY_MISMATCH)
        return

    snapshot_relpath = f"{logical_root}/{rel}"
    if entry.get("snapshot_relpath") != snapshot_relpath:
        raise ReadinessEvidenceError(REASON_INVENTORY_POLICY_MISMATCH)
    if entry["source_state"] == "absent":
        if (
            entry.get("capture") != "none"
            or any(entry.get(key) is not None for key in ("size_bytes", "sha256", "mode"))
            or entry.get("reason") != "source_absent"
        ):
            raise ReadinessEvidenceError(REASON_INVENTORY_POLICY_MISMATCH)
        return

    expected_capture = "sqlite_backup" if kind == "sqlite" else "stable_file"
    size = entry.get("size_bytes")
    digest = entry.get("sha256")
    if (
        entry.get("capture") != expected_capture
        or type(size) is not int
        or size < 0
        or not isinstance(digest, str)
        or not _SHA256_RE.fullmatch(digest)
        or type(entry.get("mode")) is not int
        or entry["mode"] != 0o600
        or entry.get("reason") is not None
    ):
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    limit = MAX_SQLITE_BYTES if kind == "sqlite" else (
        MAX_VAPID_BYTES if kind == "key" else MAX_JSON_BYTES
    )
    if size > limit:
        raise ReadinessEvidenceError(REASON_SNAPSHOT_LIMIT_EXCEEDED)


def _validate_inventory(value: dict[str, Any]) -> None:
    if set(value) != _INVENTORY_KEYS:
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    if value.get("engine") != "immutable-upgrade-controller":
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    for key in ("snapshot_id", "request_id"):
        item = value.get(key)
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 200
            or any(ord(char) < 32 for char in item)
        ):
            raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    if not isinstance(value.get("source_sha"), str) or not _GIT_SHA_RE.fullmatch(value["source_sha"]):
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    if not isinstance(value.get("target_digest"), str) or not _SHA256_RE.fullmatch(value["target_digest"]):
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    if not isinstance(value.get("captured_at"), str) or not _UTC_RE.fullmatch(value["captured_at"]):
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    if value.get("consistency_scope") != "per_store_atomic":
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    if type(value.get("entry_count")) is not int or value["entry_count"] != len(_EXPECTED_INVENTORY_NAMES):
        raise ReadinessEvidenceError(REASON_INVENTORY_INCOMPLETE)
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) != len(_EXPECTED_INVENTORY_NAMES):
        raise ReadinessEvidenceError(REASON_INVENTORY_INCOMPLETE)
    if [item.get("name") if isinstance(item, dict) else None for item in entries] != list(
        _EXPECTED_INVENTORY_NAMES
    ):
        raise ReadinessEvidenceError(REASON_INVENTORY_INCOMPLETE)
    for entry, expected_name in zip(entries, _EXPECTED_INVENTORY_NAMES, strict=True):
        _validate_inventory_entry(entry, expected_name)
    total = value.get("total_snapshot_bytes")
    calculated = sum(
        entry["size_bytes"]
        for entry in entries
        if entry["policy"] == "snapshot" and entry["source_state"] == "present"
    )
    if type(total) is not int or total != calculated:
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    if total > MAX_SNAPSHOT_BYTES:
        raise ReadinessEvidenceError(REASON_SNAPSHOT_LIMIT_EXCEEDED)


def _sha256_regular_file(path: Path, *, max_bytes: int | None = None) -> str:
    if _has_symlink_component(path):
        raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE) from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or (max_bytes is not None and info.st_size > max_bytes)
        ):
            raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE)
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _stores_compatible(stores: list[dict[str, Any]]) -> bool:
    if [item.get("name") for item in stores] != list(store_schema._APP_OWNED_STORES):
        return False
    for item in stores:
        state = item.get("state")
        reason = item.get("reason")
        if state == "compatible" and reason == store_schema.REASON_COMPATIBLE:
            continue
        if state == "absent" and reason == store_schema.REASON_MISSING_CREATABLE:
            continue
        return False
    return True


def _secure_snapshot_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE)


def _snapshot_file_signature(
    path: Path,
    *,
    expected_size: int,
    expected_digest: str,
    limit: int,
) -> tuple[int, int, int, int]:
    if _has_symlink_component(path):
        raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH) from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE)
        if before.st_size > limit:
            raise ReadinessEvidenceError(REASON_SNAPSHOT_LIMIT_EXCEEDED)
        if before.st_size != expected_size:
            raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ReadinessEvidenceError(REASON_SNAPSHOT_LIMIT_EXCEEDED)
            digest.update(chunk)
        after = os.fstat(fd)
        signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if total != expected_size or signature != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH)
        try:
            current = path.lstat()
        except OSError as exc:
            raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH) from exc
        if (current.st_dev, current.st_ino) != signature[:2]:
            raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH)
        if not hmac.compare_digest(digest.hexdigest(), expected_digest):
            raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH)
        return signature
    finally:
        os.close(fd)


def _validate_snapshot_layout(
    root: Path,
    inventory: dict[str, Any],
) -> tuple[dict[Path, tuple[int, int, int, int]], tuple[Path, ...]]:
    entries = {entry["name"]: entry for entry in inventory["entries"]}
    signatures: dict[Path, tuple[int, int, int, int]] = {}
    absent: list[Path] = []
    expected_files = {root / INVENTORY_NAME}

    for name in _EXPECTED_INVENTORY_NAMES:
        entry = entries[name]
        if entry["policy"] == "preserve_in_place":
            logical_root, rel, _, _, _ = _expected_entry(name)
            preserved_path = root / (name if name == "uploads" else f"{logical_root}/{rel}")
            try:
                preserved_path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE) from exc
            raise ReadinessEvidenceError(REASON_INVENTORY_POLICY_MISMATCH)

        path = root / entry["snapshot_relpath"]
        parent = path.parent
        if parent.exists():
            _secure_snapshot_dir(parent)
        if entry["source_state"] == "absent":
            try:
                path.lstat()
            except FileNotFoundError:
                absent.append(path)
                continue
            except OSError as exc:
                raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE) from exc
            raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH)
        if not parent.exists():
            raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH)
        expected_files.add(path)
        limit = MAX_SQLITE_BYTES if entry["kind"] == "sqlite" else (
            MAX_VAPID_BYTES if entry["kind"] == "key" else MAX_JSON_BYTES
        )
        signatures[path] = _snapshot_file_signature(
            path,
            expected_size=entry["size_bytes"],
            expected_digest=entry["sha256"],
            limit=limit,
        )

    allowed_dirs = {root / name for name in ("data", "config", "state")}
    try:
        root_children = list(root.iterdir())
    except OSError as exc:
        raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE) from exc
    for child in root_children:
        if child == root / INVENTORY_NAME:
            continue
        if child not in allowed_dirs:
            raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH)
        _secure_snapshot_dir(child)
        try:
            children = list(child.iterdir())
        except OSError as exc:
            raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE) from exc
        for item in children:
            if item not in expected_files:
                raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH)

    if sum(signature[2] for signature in signatures.values()) != inventory["total_snapshot_bytes"]:
        raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH)
    return signatures, tuple(absent)


def _verify_snapshot_unchanged(
    signatures: Mapping[Path, tuple[int, int, int, int]],
    absent: tuple[Path, ...],
) -> None:
    for path, expected in signatures.items():
        try:
            current = path.lstat()
        except OSError as exc:
            raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH) from exc
        actual = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        if actual != expected:
            raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH)
    for path in absent:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ReadinessEvidenceError(REASON_SNAPSHOT_UNSAFE) from exc
        raise ReadinessEvidenceError(REASON_INVENTORY_SNAPSHOT_MISMATCH)


def _verify_backup_inventory(
    *,
    snapshot_root: Path,
    backup_inventory_path: Path,
    backup_inventory_sha256: str,
) -> tuple[Path, dict[Path, tuple[int, int, int, int]], tuple[Path, ...]]:
    root = _secure_snapshot_root(snapshot_root)
    inventory_path = Path(backup_inventory_path)
    if (
        inventory_path != root / INVENTORY_NAME
        or not isinstance(backup_inventory_sha256, str)
        or not _SHA256_RE.fullmatch(backup_inventory_sha256)
    ):
        raise ReadinessEvidenceError(REASON_INVENTORY_INVALID)
    raw, _ = _read_inventory(inventory_path)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), backup_inventory_sha256):
        raise ReadinessEvidenceError(REASON_INVENTORY_DIGEST_MISMATCH)
    inventory = _parse_inventory(raw)
    _validate_inventory(inventory)
    signatures, absent = _validate_snapshot_layout(root, inventory)
    return root, signatures, absent


def build_schema_evidence(
    *,
    snapshot_root: Path,
    artifact_root: Path,
    identity: Mapping[str, Any],
    backup_inventory_path: Path,
    backup_inventory_sha256: str,
) -> dict[str, Any]:
    """Build controller input from a backup tree using target release code."""
    target = _target(identity)
    artifact = _secure_root(artifact_root)
    manifest = store_schema.probe_manifest(
        "server",
        identity=dict(identity),
        root=artifact,
    )
    if manifest.get("state") != "compatible":
        raise ReadinessEvidenceError(REASON_EVIDENCE_MANIFEST_MISMATCH)

    verified_root, signatures, absent = _verify_backup_inventory(
        snapshot_root=Path(snapshot_root),
        backup_inventory_path=Path(backup_inventory_path),
        backup_inventory_sha256=backup_inventory_sha256,
    )
    stores = store_schema.probe_snapshot_stores(verified_root)
    if not _stores_compatible(stores):
        raise ReadinessEvidenceError(REASON_SCHEMA_INCOMPATIBLE)
    _verify_snapshot_unchanged(signatures, absent)
    sanitized = [
        {key: item[key] for key in ("name", "compat_family", "state", "reason")}
        for item in stores
    ]
    evidence = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "compat_family": store_schema.COMPAT_FAMILY,
        "target": target,
        "release_manifest_sha256": _sha256_regular_file(
            artifact / "release-manifest.json",
        ),
        "backup_inventory_sha256": backup_inventory_sha256,
        "stores": sanitized,
    }
    canonical_evidence_bytes(evidence)
    return evidence


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _parse_canonical(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("ascii")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID) from exc
    if not isinstance(value, dict) or canonical_evidence_bytes(value) != raw:
        raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID)
    return value


def _validate_payload(value: dict[str, Any], target: dict[str, str]) -> None:
    if set(value) != _EVIDENCE_KEYS:
        raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID)
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID)
    if value.get("compat_family") != store_schema.COMPAT_FAMILY:
        raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID)
    if value.get("target") != target or set(value["target"]) != _TARGET_KEYS:
        raise ReadinessEvidenceError(REASON_EVIDENCE_IDENTITY_MISMATCH)
    manifest_digest = value.get("release_manifest_sha256")
    if not isinstance(manifest_digest, str) or not _SHA256_RE.fullmatch(manifest_digest):
        raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID)
    inventory_digest = value.get("backup_inventory_sha256")
    if not isinstance(inventory_digest, str) or not _SHA256_RE.fullmatch(inventory_digest):
        raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID)

    stores = value.get("stores")
    if not isinstance(stores, list) or len(stores) != len(store_schema._APP_OWNED_STORES):
        raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID)
    for item in stores:
        if not isinstance(item, dict) or set(item) != _STORE_KEYS:
            raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID)
        if item.get("compat_family") != store_schema.COMPAT_FAMILY:
            raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID)
    if not _stores_compatible(stores):
        raise ReadinessEvidenceError(REASON_SCHEMA_INCOMPATIBLE)


def _read_evidence(path: Path) -> bytes:
    if _has_symlink_component(path):
        raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE) from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > MAX_EVIDENCE_BYTES
        ):
            raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE)
        raw = bytearray()
        while len(raw) <= MAX_EVIDENCE_BYTES:
            chunk = os.read(fd, min(65536, MAX_EVIDENCE_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        if (
            len(raw) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE)
        try:
            current = path.lstat()
        except OSError as exc:
            raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE) from exc
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE)
        return bytes(raw)
    finally:
        os.close(fd)


def _configured_path(environ: Mapping[str, str]) -> tuple[Path, str]:
    raw_path = environ.get(EVIDENCE_PATH_ENV, "")
    expected_digest = environ.get(EVIDENCE_SHA256_ENV, "")
    if not raw_path or not expected_digest:
        raise ReadinessEvidenceError(REASON_EVIDENCE_MISSING)
    if (
        len(raw_path) > 4096
        or "\x00" in raw_path
        or any(ord(char) < 32 for char in raw_path)
        or not isinstance(expected_digest, str)
        or not _SHA256_RE.fullmatch(expected_digest)
    ):
        raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID)
    path = Path(raw_path)
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise ReadinessEvidenceError(REASON_EVIDENCE_UNSAFE)
    return path, expected_digest


def _validate_server_evidence(
    identity: Mapping[str, Any],
    *,
    artifact_root: Path,
    environ: Mapping[str, str],
) -> None:
    target = _target(identity)
    path, expected_digest = _configured_path(environ)
    raw = _read_evidence(path)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_digest):
        raise ReadinessEvidenceError(REASON_EVIDENCE_DIGEST_MISMATCH)
    value = _parse_canonical(raw)
    _validate_payload(value, target)

    artifact = _secure_root(artifact_root)
    before = _sha256_regular_file(artifact / "release-manifest.json")
    manifest = store_schema.probe_manifest(
        "server",
        identity=dict(identity),
        root=artifact,
    )
    after = _sha256_regular_file(artifact / "release-manifest.json")
    if (
        manifest.get("state") != "compatible"
        or before != after
        or before != value["release_manifest_sha256"]
    ):
        raise ReadinessEvidenceError(REASON_EVIDENCE_MANIFEST_MISMATCH)


def probe_server_evidence(
    identity: Mapping[str, Any],
    *,
    artifact_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Pure-read Server readiness probe with sanitized failure reasons."""
    try:
        _validate_server_evidence(
            identity,
            artifact_root=(
                artifact_root
                if artifact_root is not None
                else Path(__file__).resolve().parent
            ),
            environ=environ if environ is not None else os.environ,
        )
    except ReadinessEvidenceError as exc:
        state = "missing" if exc.reason == REASON_EVIDENCE_MISSING else "error"
        return _result(state, exc.reason)
    except Exception:
        return _result("error", REASON_EVIDENCE_INVALID)
    return _result("compatible", store_schema.REASON_COMPATIBLE)


def main(argv: list[str] | None = None) -> int:
    """Candidate-side probe CLI; the fixed controller remains the file writer."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Build sealed Server schema evidence")
    parser.add_argument("--snapshot-root", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--backup-inventory-path", required=True)
    parser.add_argument("--backup-inventory-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        evidence = build_schema_evidence(
            snapshot_root=Path(args.snapshot_root),
            artifact_root=Path(args.artifact_root),
            identity={
                "version": args.version,
                "source_sha": args.source_sha,
                "edition": "server",
            },
            backup_inventory_path=Path(args.backup_inventory_path),
            backup_inventory_sha256=args.backup_inventory_sha256,
        )
    except ReadinessEvidenceError as exc:
        error = {"error_code": exc.reason, "ok": False}
        sys.stderr.buffer.write(canonical_evidence_bytes(error))
        return 1
    sys.stdout.buffer.write(canonical_evidence_bytes(evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
