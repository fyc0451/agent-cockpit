from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "publish_local_release.py"


def _module():
    spec = importlib.util.spec_from_file_location("publish_local_release", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publish_local_release_help_is_read_only() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
    assert "--candidate" in result.stdout
    assert "--release-id" in result.stdout


def test_private_key_reader_rejects_mode_symlink_and_hardlink(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "key"
    path.write_bytes(b"k" * 32)
    path.chmod(0o600)
    assert module._private_file(path, 32) == b"k" * 32

    path.chmod(0o644)
    with pytest.raises(module.LocalReleaseError, match="release_key_unsafe"):
        module._private_file(path, 32)
    path.chmod(0o600)
    os.link(path, tmp_path / "hardlink")
    with pytest.raises(module.LocalReleaseError, match="release_key_unsafe"):
        module._private_file(path, 32)
    path.unlink()
    path.symlink_to(tmp_path / "hardlink")
    with pytest.raises(module.LocalReleaseError, match="release_key_unsafe"):
        module._private_file(path, 32)


def test_private_state_directory_requires_private_owned_directory(tmp_path: Path) -> None:
    module = _module()
    state = tmp_path / "state"
    module._private_directory(state)
    assert state.stat().st_mode & 0o777 == 0o700
    state.chmod(0o755)
    with pytest.raises(module.LocalReleaseError, match="release_state_unsafe"):
        module._private_directory(state)


def test_verify_assets_checks_signature_archive_and_launcher(tmp_path: Path) -> None:
    module = _module()
    build_spec = importlib.util.spec_from_file_location(
        "build_server_artifact", ROOT / "scripts" / "build_server_artifact.py",
    )
    assert build_spec is not None and build_spec.loader is not None
    build = importlib.util.module_from_spec(build_spec)
    build_spec.loader.exec_module(build)

    source = tmp_path / "source"
    (source / "static").mkdir(parents=True)
    (source / "VERSION").write_text("1.2.3\n")
    (source / "static/index.html").write_text("ok")
    onedir = tmp_path / "onedir"
    (onedir / "_internal").mkdir(parents=True)
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    header[18:20] = (62).to_bytes(2, "little")
    (onedir / "agent-cockpit").write_bytes(bytes(header) + b"launcher")
    (onedir / "_internal/runtime").write_bytes(b"runtime")
    generation = tmp_path / "generation"
    build.assemble_generation(source, onedir, generation, "a" * 40)
    assets = tmp_path / "assets"
    assets.mkdir()
    archive = assets / "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz"
    build.write_deterministic_tar(generation, archive, source_date_epoch=1)
    index_path = assets / "release-index.json"
    build.write_release_index(generation, archive, index_path, "a" * 40)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    (assets / "release-index.json.sig").write_bytes(key.sign(index_path.read_bytes()))

    verified = module._verify_assets(
        assets,
        archive_name=archive.name,
        public_key=public,
        verify_root=tmp_path / "verify",
    )
    assert verified["source_sha"] == "a" * 40
