"""Compose verified release primitives into a prepared immutable generation."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_download import Transport, download_verified_artifact
from .artifact_extract import extract_verified_tarball
from .generation_switch import GenerationIdentity
from .release_index import (
    PERSISTED_INDEX_NAME,
    PERSISTED_SIGNATURE_NAME,
    verify_release_index,
)


class GenerationPrepareError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedGeneration:
    version: str
    source_sha: str
    artifact_digest: str
    generation_id: str
    generation_path: Path
    launcher_path: Path


def _persist_verified_release(
    generation_path: Path, index_bytes: bytes, signature_bytes: bytes,
) -> None:
    """Persist the already-verified canonical index + signature inside the
    generation (mode 0600, atomic + fsync). Raises on any failure so that
    ``prepare_generation`` never returns a half-prepared generation."""
    for name, payload in (
        (PERSISTED_INDEX_NAME, index_bytes),
        (PERSISTED_SIGNATURE_NAME, signature_bytes),
    ):
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=name + ".", dir=generation_path)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, generation_path / name)
            tmp = None
        except BaseException:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise GenerationPrepareError(f"persist_failed:{name}")


def prepare_generation(
    index_bytes: bytes,
    signature_bytes: bytes,
    public_key_bytes: bytes,
    *,
    deploy_root: Path,
    platform: str,
    arch: str,
    transport: Transport | None = None,
) -> PreparedGeneration:
    """Verify, download, and extract one release without activating it."""
    verified = verify_release_index(
        index_bytes,
        signature_bytes,
        public_key_bytes,
        platform=platform,
        arch=arch,
    )
    asset: dict[str, Any] = verified["selected_asset"]
    identity = GenerationIdentity(
        source_sha=verified["source_sha"],
        artifact_digest=asset["sha256"],
    )
    artifact_path = download_verified_artifact(
        asset,
        deploy_root / "artifact-cache",
        transport=transport,
    )
    generation_path = extract_verified_tarball(
        artifact_path,
        asset,
        deploy_root / "generations" / identity.generation_id,
    )
    _persist_verified_release(generation_path, index_bytes, signature_bytes)
    launcher_path = generation_path / asset["launcher"]["path"]
    return PreparedGeneration(
        version=verified["version"],
        source_sha=identity.source_sha,
        artifact_digest=identity.artifact_digest,
        generation_id=identity.generation_id,
        generation_path=generation_path,
        launcher_path=launcher_path,
    )


__all__ = ["GenerationPrepareError", "PreparedGeneration", "prepare_generation"]
