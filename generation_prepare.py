"""Compose verified release primitives into a prepared immutable generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from artifact_download import Transport, download_verified_artifact
from artifact_extract import extract_verified_tarball
from generation_switch import GenerationIdentity
from release_index import verify_release_index


@dataclass(frozen=True)
class PreparedGeneration:
    version: str
    source_sha: str
    artifact_digest: str
    generation_id: str
    generation_path: Path
    launcher_path: Path


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
    launcher_path = generation_path / asset["launcher"]["path"]
    return PreparedGeneration(
        version=verified["version"],
        source_sha=identity.source_sha,
        artifact_digest=identity.artifact_digest,
        generation_id=identity.generation_id,
        generation_path=generation_path,
        launcher_path=launcher_path,
    )


__all__ = ["PreparedGeneration", "prepare_generation"]
