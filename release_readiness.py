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

import store_schema

EVIDENCE_PATH_ENV = "COCKPIT_SCHEMA_EVIDENCE_PATH"
EVIDENCE_SHA256_ENV = "COCKPIT_SCHEMA_EVIDENCE_SHA256"
EVIDENCE_SCHEMA_VERSION = 1
MAX_EVIDENCE_BYTES = 128 * 1024

REASON_EVIDENCE_MISSING = "schema_evidence_missing"
REASON_EVIDENCE_UNSAFE = "schema_evidence_unsafe"
REASON_EVIDENCE_DIGEST_MISMATCH = "schema_evidence_digest_mismatch"
REASON_EVIDENCE_INVALID = "schema_evidence_invalid"
REASON_EVIDENCE_IDENTITY_MISMATCH = "schema_evidence_identity_mismatch"
REASON_EVIDENCE_MANIFEST_MISMATCH = "schema_evidence_manifest_mismatch"
REASON_SCHEMA_INCOMPATIBLE = "schema_incompatible"

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


def build_schema_evidence(
    *,
    snapshot_root: Path,
    artifact_root: Path,
    identity: Mapping[str, Any],
    backup_inventory_sha256: str,
) -> dict[str, Any]:
    """Build controller input from a backup tree using target release code."""
    target = _target(identity)
    if (
        not isinstance(backup_inventory_sha256, str)
        or not _SHA256_RE.fullmatch(backup_inventory_sha256)
    ):
        raise ReadinessEvidenceError(REASON_EVIDENCE_INVALID)
    artifact = _secure_root(artifact_root)
    manifest = store_schema.probe_manifest(
        "server",
        identity=dict(identity),
        root=artifact,
    )
    if manifest.get("state") != "compatible":
        raise ReadinessEvidenceError(REASON_EVIDENCE_MANIFEST_MISMATCH)

    stores = store_schema.probe_snapshot_stores(snapshot_root)
    if not _stores_compatible(stores):
        raise ReadinessEvidenceError(REASON_SCHEMA_INCOMPATIBLE)
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
