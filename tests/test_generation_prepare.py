from __future__ import annotations

import hashlib
import io
import stat
import tarfile
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import generation_prepare
from artifact_extract import ArtifactExtractError
from release_index import (
    PERSISTED_INDEX_NAME,
    PERSISTED_SIGNATURE_NAME,
    canonical_bytes,
)


SOURCE_SHA = "a" * 40
ARTIFACT_DIGEST = "b" * 64
LAUNCHER_DIGEST = "c" * 64


class _Response:
    status_code = 200

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers = {"content-length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_bytes(self) -> Iterator[bytes]:
        yield self.payload


def _archive(launcher: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in ("bin/", "static/"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        for name, payload in (
            ("bin/agent-cockpit", launcher),
            ("VERSION", b"1.2.3\n"),
            ("static/index.html", b"ready\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _verified_index() -> dict[str, object]:
    asset = {
        "name": "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz",
        "edition": "server",
        "platform": "linux",
        "arch": "x86_64",
        "size": 123,
        "sha256": ARTIFACT_DIGEST,
        "launcher": {
            "path": "bin/agent-cockpit",
            "size": 45,
            "sha256": LAUNCHER_DIGEST,
            "format": "elf",
        },
        "url": "https://github.com/example/artifact",
    }
    return {
        "schema_version": 2,
        "tag": "agent-cockpit-v1.2.3",
        "version": "1.2.3",
        "source_sha": SOURCE_SHA,
        "draft": False,
        "prerelease": False,
        "assets": [asset],
        "selected_asset": asset,
    }


def _layout(tmp_path: Path) -> Path:
    deploy_root = tmp_path / "deploy"
    deploy_root.mkdir(mode=0o700)
    (deploy_root / "generations").mkdir(mode=0o700)
    return deploy_root


def test_real_primitives_prepare_once_and_cache_reuse_fails_closed(
    tmp_path: Path,
) -> None:
    deploy_root = _layout(tmp_path)
    launcher = b"\x7fELF" + b"native-server"
    payload = _archive(launcher)
    artifact_digest = hashlib.sha256(payload).hexdigest()
    launcher_digest = hashlib.sha256(launcher).hexdigest()
    asset_name = "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz"
    index = {
        "schema_version": 2,
        "tag": "agent-cockpit-v1.2.3",
        "version": "1.2.3",
        "source_sha": SOURCE_SHA,
        "draft": False,
        "prerelease": False,
        "assets": [{
            "name": asset_name,
            "edition": "server",
            "platform": "linux",
            "arch": "x86_64",
            "size": len(payload),
            "sha256": artifact_digest,
            "launcher": {
                "path": "bin/agent-cockpit",
                "size": len(launcher),
                "sha256": launcher_digest,
                "format": "elf",
            },
        }],
    }
    index_bytes = canonical_bytes(index)
    key = Ed25519PrivateKey.generate()
    signature = key.sign(index_bytes)
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    calls: list[str] = []

    def transport(url: str) -> _Response:
        calls.append(url)
        return _Response(payload)

    receipt = generation_prepare.prepare_generation(
        index_bytes,
        signature,
        public,
        deploy_root=deploy_root,
        platform="linux",
        arch="x86_64",
        transport=transport,
    )

    assert receipt.generation_id == f"{SOURCE_SHA}-{artifact_digest}"
    assert receipt.generation_path == deploy_root / "generations" / receipt.generation_id
    assert receipt.launcher_path.read_bytes() == launcher
    assert stat.S_IMODE(receipt.launcher_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((receipt.generation_path / "VERSION").stat().st_mode) == 0o600
    persisted_index = receipt.generation_path / PERSISTED_INDEX_NAME
    persisted_sig = receipt.generation_path / PERSISTED_SIGNATURE_NAME
    assert persisted_index.is_file() and persisted_index.read_bytes() == index_bytes
    assert persisted_sig.is_file() and persisted_sig.read_bytes() == signature
    assert stat.S_IMODE(persisted_index.stat().st_mode) == 0o600
    assert stat.S_IMODE(persisted_sig.stat().st_mode) == 0o600
    assert (deploy_root / "artifact-cache" / artifact_digest).is_file()
    assert len(calls) == 1
    assert not (deploy_root / "current").exists()

    with pytest.raises(ArtifactExtractError, match="destination_exists"):
        generation_prepare.prepare_generation(
            index_bytes,
            signature,
            public,
            deploy_root=deploy_root,
            platform="linux",
            arch="x86_64",
            transport=lambda _url: pytest.fail("verified cache must be reused"),
        )
    assert len(calls) == 1


def test_prepares_canonical_generation_and_frozen_receipt(monkeypatch, tmp_path: Path) -> None:
    deploy_root = _layout(tmp_path)
    verified = _verified_index()
    calls: list[tuple] = []

    def verify(*args, **kwargs):
        calls.append(("verify", args, kwargs))
        return verified

    def download(asset, cache_dir, *, transport=None):
        calls.append(("download", asset, cache_dir, transport))
        cache_dir.mkdir(mode=0o700)
        artifact = cache_dir / ARTIFACT_DIGEST
        artifact.write_bytes(b"archive")
        return artifact

    def extract(artifact, asset, destination):
        calls.append(("extract", artifact, asset, destination))
        destination.mkdir(mode=0o700)
        launcher = destination / "bin/agent-cockpit"
        launcher.parent.mkdir(mode=0o700)
        launcher.write_bytes(b"launcher")
        return destination

    monkeypatch.setattr(generation_prepare, "verify_release_index", verify)
    monkeypatch.setattr(generation_prepare, "download_verified_artifact", download)
    monkeypatch.setattr(generation_prepare, "extract_verified_tarball", extract)
    transport = object()

    receipt = generation_prepare.prepare_generation(
        b"index", b"signature", b"public-key",
        deploy_root=deploy_root,
        platform="linux",
        arch="x86_64",
        transport=transport,
    )

    generation_id = f"{SOURCE_SHA}-{ARTIFACT_DIGEST}"
    generation_path = deploy_root / "generations" / generation_id
    assert receipt == generation_prepare.PreparedGeneration(
        version="1.2.3",
        source_sha=SOURCE_SHA,
        artifact_digest=ARTIFACT_DIGEST,
        generation_id=generation_id,
        generation_path=generation_path,
        launcher_path=generation_path / "bin/agent-cockpit",
    )
    assert [call[0] for call in calls] == ["verify", "download", "extract"]
    assert calls[0][1] == (b"index", b"signature", b"public-key")
    assert calls[0][2] == {"platform": "linux", "arch": "x86_64"}
    assert calls[1][2] == deploy_root / "artifact-cache"
    assert calls[1][3] is transport
    assert calls[2][1] == deploy_root / "artifact-cache" / ARTIFACT_DIGEST
    assert calls[2][3] == generation_path
    assert not (deploy_root / "current").exists()
    with pytest.raises(FrozenInstanceError):
        receipt.version = "9.9.9"  # type: ignore[misc]


def test_invalid_index_stops_before_download_or_layout(monkeypatch, tmp_path: Path) -> None:
    deploy_root = tmp_path / "deploy"

    def reject(*_args, **_kwargs):
        raise ValueError("invalid index")

    monkeypatch.setattr(generation_prepare, "verify_release_index", reject)
    monkeypatch.setattr(
        generation_prepare,
        "download_verified_artifact",
        lambda *_args, **_kwargs: pytest.fail("download must not run"),
    )

    with pytest.raises(ValueError, match="invalid index"):
        generation_prepare.prepare_generation(
            b"bad", b"signature", b"public-key",
            deploy_root=deploy_root,
            platform="linux",
            arch="x86_64",
        )

    assert not deploy_root.exists()


def test_existing_destination_propagates_extractor_and_reuses_cache(
    monkeypatch, tmp_path: Path,
) -> None:
    deploy_root = _layout(tmp_path)
    verified = _verified_index()
    generation_id = f"{SOURCE_SHA}-{ARTIFACT_DIGEST}"
    destination = deploy_root / "generations" / generation_id
    destination.mkdir(mode=0o700)
    cache = deploy_root / "artifact-cache"
    cache.mkdir(mode=0o700)
    artifact = cache / ARTIFACT_DIGEST
    artifact.write_bytes(b"cached")
    downloads = 0

    monkeypatch.setattr(
        generation_prepare, "verify_release_index", lambda *_args, **_kwargs: verified,
    )

    def reuse(_asset, cache_dir, *, transport=None):
        nonlocal downloads
        downloads += 1
        assert cache_dir == cache
        return artifact

    monkeypatch.setattr(generation_prepare, "download_verified_artifact", reuse)

    def reject_existing(_artifact, _asset, selected_destination):
        assert selected_destination == destination
        raise ArtifactExtractError("destination_exists")

    monkeypatch.setattr(generation_prepare, "extract_verified_tarball", reject_existing)

    for _attempt in range(2):
        with pytest.raises(ArtifactExtractError, match="destination_exists"):
            generation_prepare.prepare_generation(
                b"index", b"signature", b"public-key",
                deploy_root=deploy_root,
                platform="linux",
                arch="x86_64",
            )

    assert downloads == 2
    assert destination.is_dir()
    assert not (deploy_root / "current").exists()


def test_extraction_failure_leaves_forensic_destination_and_never_activates(
    monkeypatch, tmp_path: Path,
) -> None:
    deploy_root = _layout(tmp_path)
    verified = _verified_index()
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"cached")
    monkeypatch.setattr(
        generation_prepare, "verify_release_index", lambda *_args, **_kwargs: verified,
    )
    monkeypatch.setattr(
        generation_prepare, "download_verified_artifact",
        lambda *_args, **_kwargs: artifact,
    )

    def fail_partial(_artifact, _asset, destination):
        destination.mkdir(mode=0o700)
        (destination / "partial").write_bytes(b"evidence")
        raise ArtifactExtractError("extract_failed")

    monkeypatch.setattr(generation_prepare, "extract_verified_tarball", fail_partial)

    with pytest.raises(ArtifactExtractError, match="extract_failed"):
        generation_prepare.prepare_generation(
            b"index", b"signature", b"public-key",
            deploy_root=deploy_root,
            platform="linux",
            arch="x86_64",
        )

    generation = deploy_root / "generations" / f"{SOURCE_SHA}-{ARTIFACT_DIGEST}"
    assert (generation / "partial").read_bytes() == b"evidence"
    assert not (deploy_root / "current").exists()


def test_persist_verified_release_raises_when_generation_unwritable(tmp_path: Path) -> None:
    with pytest.raises(generation_prepare.GenerationPrepareError):
        generation_prepare._persist_verified_release(
            tmp_path / "does-not-exist", b"{}", b"\x00" * 64,
        )
