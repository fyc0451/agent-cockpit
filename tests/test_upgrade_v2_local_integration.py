from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import artifact_download
import maintenance_ipc
import native_controller_install
import release_index
import release_prepare
import upgrade_layout


TAG = "agent-cockpit-v1.2.3"
VERSION = "1.2.3"
SOURCE_SHA = "a" * 40
ASSET_NAME = "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz"


class Response:
    def __init__(self, payload: bytes) -> None:
        self.status_code = 200
        self.headers = {"content-length": str(len(payload))}
        self._payload = payload

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_bytes(self) -> Iterator[bytes]:
        yield self._payload


class QueueTransport:
    def __init__(self, payloads: list[bytes]) -> None:
        self._responses = [Response(payload) for payload in payloads]
        self.calls: list[str] = []

    def __call__(self, url: str) -> AbstractContextManager[Response]:
        self.calls.append(url)
        return self._responses.pop(0)


def _archive(launcher: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in ("bin/", "bin/_internal/"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        for name, payload in (
            ("bin/agent-cockpit", launcher),
            ("bin/_internal/runtime.dat", b"native-runtime"),
            ("VERSION", f"{VERSION}\n".encode("ascii")),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _release_payloads(
    *, invalid_signature: bool = False, invalid_digest: bool = False
) -> tuple[bytes, bytes, bytes, bytes]:
    launcher = b"\x7fELF" + b"native-server" * 64
    archive = _archive(launcher)
    artifact_digest = hashlib.sha256(archive).hexdigest()
    index = {
        "schema_version": 2,
        "tag": TAG,
        "version": VERSION,
        "source_sha": SOURCE_SHA,
        "draft": False,
        "prerelease": False,
        "assets": [{
            "name": ASSET_NAME,
            "edition": "server",
            "platform": "linux",
            "arch": "x86_64",
            "size": len(archive),
            "sha256": "0" * 64 if invalid_digest else artifact_digest,
            "launcher": {
                "path": "bin/agent-cockpit",
                "size": len(launcher),
                "sha256": hashlib.sha256(launcher).hexdigest(),
                "format": "elf",
            },
        }],
    }
    index_bytes = release_index.canonical_bytes(index)
    private_key = Ed25519PrivateKey.generate()
    signature = private_key.sign(index_bytes)
    if invalid_signature:
        signature = bytes([signature[0] ^ 1]) + signature[1:]
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return index_bytes, signature, archive, public_key


def _layout(tmp_path: Path) -> upgrade_layout.UpgradeLayout:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    layout = upgrade_layout.default_upgrade_layout(home=home)
    (layout.deploy_root / "generations").mkdir(parents=True, mode=0o700)
    return layout


def test_real_local_primitives_prepare_install_and_spawn_controller(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    index_bytes, signature, archive, public_key = _release_payloads()
    transport = QueueTransport([index_bytes, signature, archive])

    prepared = release_prepare.prepare_release_generation(
        TAG,
        public_key,
        deploy_root=layout.deploy_root,
        platform="linux",
        arch="x86_64",
        transport=transport,
    ).generation
    assert prepared.launcher_path.read_bytes().startswith(b"\x7fELF")
    assert (
        prepared.generation_path / "bin/_internal/runtime.dat"
    ).read_bytes() == b"native-runtime"
    assert (
        layout.deploy_root / "artifact-cache" / prepared.artifact_digest
    ).read_bytes() == archive
    installed = native_controller_install.install_native_controller(
        layout, prepared, public_key,
    )

    previous_id = f"{'b' * 40}-{'c' * 64}"
    previous = layout.deploy_root / "generations" / previous_id
    previous.mkdir(mode=0o700)
    (previous / "VERSION").write_text("1.2.2\n", encoding="ascii")
    (previous / "VERSION").chmod(0o600)
    layout.current.symlink_to(Path("generations") / previous_id)

    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_popen(argv: tuple[str, ...], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        return type("Process", (), {"pid": 4321})()

    accepted = maintenance_ipc.spawn_maintenance_controller(
        plan=upgrade_layout.build_controller_plan(layout),
        prepared=prepared,
        request_id="local-integration-1",
        controller_launcher=layout.controller_launcher,
        popen=fake_popen,
    )

    assert accepted == maintenance_ipc.ControllerAccepted(pid=4321, accepted=True)
    assert installed == layout.controller_root
    assert not layout.controller_launcher.is_relative_to(prepared.generation_path)
    assert not layout.controller_launcher.is_relative_to(layout.deploy_root)
    assert layout.controller_launcher.read_bytes() == prepared.launcher_path.read_bytes()
    assert upgrade_layout.load_release_public_key(layout) == public_key
    argv, kwargs = calls[0]
    assert argv[:3] == (
        str(layout.controller_launcher), "maintenance-controller", "execute",
    )
    assert kwargs == {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "start_new_session": True,
        "shell": False,
    }
    prefix = f"{release_index.OFFICIAL_RELEASE_DOWNLOAD_PREFIX}/{TAG}"
    assert transport.calls == [
        f"{prefix}/release-index.json",
        f"{prefix}/release-index.json.sig",
        f"{prefix}/{ASSET_NAME}",
    ]


@pytest.mark.parametrize(
    ("invalid_signature", "invalid_digest", "error", "calls"),
    [
        (True, False, release_index.ReleaseIndexError, 2),
        (False, True, artifact_download.ArtifactDownloadError, 3),
    ],
)
def test_invalid_signature_or_digest_stops_before_install_and_ipc(
    tmp_path: Path,
    invalid_signature: bool,
    invalid_digest: bool,
    error: type[Exception],
    calls: int,
) -> None:
    layout = _layout(tmp_path)
    index_bytes, signature, archive, public_key = _release_payloads(
        invalid_signature=invalid_signature,
        invalid_digest=invalid_digest,
    )
    transport = QueueTransport([index_bytes, signature, archive])

    with pytest.raises(error) as exc_info:
        release_prepare.prepare_release_generation(
            TAG,
            public_key,
            deploy_root=layout.deploy_root,
            platform="linux",
            arch="x86_64",
            transport=transport,
        )

    expected = "index_signature_invalid" if invalid_signature else "digest_mismatch"
    assert str(exc_info.value) == expected
    assert len(transport.calls) == calls
    assert not layout.controller_root.exists()
    assert not any((layout.deploy_root / "generations").iterdir())
    cache = layout.deploy_root / "artifact-cache"
    assert not cache.exists() or not any(cache.iterdir())
    assert not layout.current.exists()
