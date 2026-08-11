from __future__ import annotations

import hashlib
import io
import tarfile
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import release_prepare
from generation_prepare import PreparedGeneration
from release_fetch import ReleasePayloads
from release_index import OFFICIAL_RELEASE_DOWNLOAD_PREFIX, canonical_bytes


TAG = "agent-cockpit-v1.2.3"
SOURCE_SHA = "a" * 40


class Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.status_code = 200
        self.headers = {"content-length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_bytes(self) -> Iterator[bytes]:
        yield self.payload


class QueueTransport:
    def __init__(self, payloads: list[bytes]) -> None:
        self.responses = [Response(payload) for payload in payloads]
        self.calls: list[str] = []

    def __call__(self, url: str) -> AbstractContextManager[Response]:
        self.calls.append(url)
        return self.responses.pop(0)


def _archive(launcher: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo("bin/")
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
        for name, payload in (
            ("bin/agent-cockpit", launcher),
            ("VERSION", b"1.2.3\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _layout(tmp_path: Path) -> Path:
    deploy_root = tmp_path / "deploy"
    deploy_root.mkdir(mode=0o700)
    (deploy_root / "generations").mkdir(mode=0o700)
    return deploy_root


def test_passes_exact_payloads_key_and_same_transport_to_generation_prepare(
    monkeypatch, tmp_path: Path,
) -> None:
    transport = QueueTransport([])
    public_key = b"p" * 32
    payloads = ReleasePayloads(
        tag=TAG,
        index_bytes=b"index",
        signature_bytes=b"s" * 64,
        transport=transport,
    )
    deploy_root = tmp_path / "deploy"
    generation = PreparedGeneration(
        version="1.2.3",
        source_sha=SOURCE_SHA,
        artifact_digest="b" * 64,
        generation_id=f"{SOURCE_SHA}-{'b' * 64}",
        generation_path=deploy_root / "generations" / f"{SOURCE_SHA}-{'b' * 64}",
        launcher_path=deploy_root / "generations" / f"{SOURCE_SHA}-{'b' * 64}/bin/agent-cockpit",
    )
    calls: list[tuple] = []

    def fetch(tag, *, transport=None):
        calls.append(("fetch", tag, transport))
        return payloads

    def prepare(index, signature, key, **kwargs):
        calls.append(("prepare", index, signature, key, kwargs))
        return generation

    monkeypatch.setattr(release_prepare, "fetch_release_payloads", fetch)
    monkeypatch.setattr(release_prepare, "prepare_generation", prepare)

    receipt = release_prepare.prepare_release_generation(
        TAG,
        public_key,
        deploy_root=deploy_root,
        platform="linux",
        arch="x86_64",
        transport=transport,
    )

    assert receipt == release_prepare.PreparedRelease(TAG, generation)
    assert calls == [
        ("fetch", TAG, transport),
        (
            "prepare", b"index", b"s" * 64, public_key,
            {
                "deploy_root": deploy_root,
                "platform": "linux",
                "arch": "x86_64",
                "transport": transport,
            },
        ),
    ]
    with pytest.raises(FrozenInstanceError):
        receipt.tag = "agent-cockpit-v9.9.9"  # type: ignore[misc]


@pytest.mark.parametrize("public_key", [b"", b"x" * 31, b"x" * 33, bytearray(32), None])
def test_rejects_invalid_public_key_before_fetch(monkeypatch, tmp_path, public_key) -> None:
    monkeypatch.setattr(
        release_prepare,
        "fetch_release_payloads",
        lambda *_args, **_kwargs: pytest.fail("fetch must not run"),
    )

    with pytest.raises(release_prepare.ReleasePrepareError, match="invalid_public_key"):
        release_prepare.prepare_release_generation(
            TAG,
            public_key,
            deploy_root=tmp_path / "deploy",
            platform="linux",
            arch="x86_64",
        )


def test_real_fetch_verify_download_extract_prepare_end_to_end(tmp_path: Path) -> None:
    deploy_root = _layout(tmp_path)
    launcher = b"\x7fELFnative-server"
    archive = _archive(launcher)
    artifact_digest = hashlib.sha256(archive).hexdigest()
    launcher_digest = hashlib.sha256(launcher).hexdigest()
    asset_name = "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz"
    index = {
        "schema_version": 2,
        "tag": TAG,
        "version": "1.2.3",
        "source_sha": SOURCE_SHA,
        "draft": False,
        "prerelease": False,
        "assets": [{
            "name": asset_name,
            "edition": "server",
            "platform": "linux",
            "arch": "x86_64",
            "size": len(archive),
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
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    transport = QueueTransport([index_bytes, signature, archive])

    receipt = release_prepare.prepare_release_generation(
        TAG,
        public_key,
        deploy_root=deploy_root,
        platform="linux",
        arch="x86_64",
        transport=transport,
    )

    assert receipt.tag == TAG
    assert receipt.generation.version == "1.2.3"
    assert receipt.generation.source_sha == SOURCE_SHA
    assert receipt.generation.artifact_digest == artifact_digest
    assert receipt.generation.launcher_path.read_bytes() == launcher
    assert transport.calls == [
        f"{OFFICIAL_RELEASE_DOWNLOAD_PREFIX}/{TAG}/release-index.json",
        f"{OFFICIAL_RELEASE_DOWNLOAD_PREFIX}/{TAG}/release-index.json.sig",
        f"{OFFICIAL_RELEASE_DOWNLOAD_PREFIX}/{TAG}/{asset_name}",
    ]
    assert not (deploy_root / "current").exists()
