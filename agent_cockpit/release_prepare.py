"""Compose bounded release fetch with verified generation preparation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .artifact_download import Transport
from .generation_prepare import PreparedGeneration, prepare_generation
from .release_fetch import fetch_release_payloads


class ReleasePrepareError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PreparedRelease:
    tag: str
    generation: PreparedGeneration


def prepare_release_generation(
    tag: str,
    public_key_bytes: bytes,
    *,
    deploy_root: Path,
    platform: str,
    arch: str,
    transport: Transport | None = None,
) -> PreparedRelease:
    """Fetch signed metadata and prepare its selected immutable generation."""
    if type(public_key_bytes) is not bytes or len(public_key_bytes) != 32:
        raise ReleasePrepareError("invalid_public_key")
    payloads = fetch_release_payloads(tag, transport=transport)
    generation = prepare_generation(
        payloads.index_bytes,
        payloads.signature_bytes,
        public_key_bytes,
        deploy_root=deploy_root,
        platform=platform,
        arch=arch,
        transport=payloads.transport,
    )
    return PreparedRelease(tag=payloads.tag, generation=generation)


__all__ = [
    "PreparedRelease",
    "ReleasePrepareError",
    "prepare_release_generation",
]
