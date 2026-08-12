from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_cockpit.release_index import canonical_bytes, verify_release_index


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _python_blocks() -> list[str]:
    blocks = re.findall(r"<<'PY'\n(.*?)\n\s+PY$", _workflow(), flags=re.M | re.S)
    result = []
    for block in blocks:
        indentation = min(
            len(line) - len(line.lstrip()) for line in block.splitlines() if line.strip()
        )
        result.append("\n".join(line[indentation:] for line in block.splitlines()))
    return result


def test_release_workflow_builds_and_transfers_only_unsigned_native_assets() -> None:
    raw = _workflow()

    assert "runs-on: ubuntu-22.04" in raw
    assert "pip install -r requirements-dev.txt -r requirements-build.txt" in raw
    assert "P3A_BUILD_PYTHON" in raw
    assert "scripts/build_server_artifact.py" in raw
    assert "--source-sha \"${GITHUB_SHA}\"" in raw
    assert "--source-date-epoch" in raw
    assert "release-index.json.sig" not in raw.split("jobs:\n", 1)[1].split("publish:", 1)[0]
    assert "actions/upload-artifact@" in raw
    assert "actions/download-artifact@" in raw
    assert raw.count("${{ github.run_id }}-${{ github.run_attempt }}") == 2
    assert "retention-days: 1" in raw


def test_release_workflow_signs_only_in_protected_environment() -> None:
    raw = _workflow()
    publish = raw.split("  publish:\n", 1)[1]

    assert "environment:\n      name: server-release" in publish
    assert "SERVER_RELEASE_ED25519_PRIVATE_KEY_B64" in publish
    assert "secrets.SERVER_RELEASE_ED25519_PRIVATE_KEY_B64" in publish
    assert "base64.b64decode" in publish and "validate=True" in publish
    assert "Ed25519PrivateKey.from_private_bytes" in publish
    assert "len(private_key_bytes) != 32" in publish
    assert "len(signature) != 64" in publish
    assert "release-index.json.sig" in publish
    build = raw.split("  build:\n", 1)[1].split("  publish:\n", 1)[0]
    assert "SERVER_RELEASE_ED25519_PRIVATE_KEY_B64" not in build


def test_publish_reverifies_remote_tag_after_environment_gate_before_signing() -> None:
    publish = _workflow().split("  publish:\n", 1)[1]

    verify_step = publish.index("Reverify remote tag before signing")
    fetch = publish.index(
        'remote_tag="refs/tags/${GITHUB_REF_NAME}"',
        verify_step,
    )
    fetch_exact = publish.index(
        'git fetch --no-tags --force origin "${remote_tag}:${verification_ref}"',
        fetch,
    )
    dereference = publish.index('^{commit}', fetch_exact)
    compare = publish.index('= "${GITHUB_SHA}"', dereference)
    sign = publish.index("Sign canonical release index", compare)
    create = publish.index("gh release create", sign)
    assert verify_step < fetch < fetch_exact < dereference < compare < sign < create


def test_release_workflow_keeps_draft_private_until_remote_assets_verify() -> None:
    raw = _workflow()
    publish = raw.split("  publish:\n", 1)[1]

    create = publish.index("gh release create")
    download = publish.index("gh release download")
    verify = publish.index("verify_release_index", download)
    extract = publish.index("extract_verified_tarball", verify)
    launcher = publish.index("verify_server_launcher", extract)
    make_final = publish.index("gh api --method PATCH", launcher)
    assert create < download < verify < extract < launcher < make_final
    assert "--draft" in publish[create:download]
    assert "-F draft=false -f make_latest=true" in publish[make_final:]
    assert "gh release delete" in publish
    assert "draft_identity" in publish and "release_title" in publish
    assert "--cleanup-tag" not in publish
    assert "--clobber" not in publish
    assert "expected_names" in publish
    assert "remote.read_bytes() != local.read_bytes()" in publish
    assert "release-index.json.sig" in publish[create:download]
    assert "release-index.json\"" in publish[create:download]
    assert '"${asset}" "${index}" "${signature}"' in publish[create:download]


def test_release_workflow_serializes_same_tag_and_inline_python_compiles() -> None:
    raw = _workflow()

    assert "group: release-${{ github.ref }}" in raw
    assert "cancel-in-progress: false" in raw
    blocks = _python_blocks()
    assert len(blocks) == 5
    for block in blocks:
        compile(block, "release.yml", "exec")


def test_release_workflow_signing_block_produces_verified_raw_signature(
    tmp_path: Path,
) -> None:
    signing_block = next(
        block for block in _python_blocks()
        if "SERVER_RELEASE_ED25519_PRIVATE_KEY_B64" in block
    )
    assets = tmp_path / "release-assets"
    assets.mkdir()
    index = {
        "schema_version": 2,
        "tag": "agent-cockpit-v1.2.3",
        "version": "1.2.3",
        "source_sha": "a" * 40,
        "draft": False,
        "prerelease": False,
        "assets": [{
            "name": "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz",
            "edition": "server",
            "platform": "linux",
            "arch": "x86_64",
            "size": 1,
            "sha256": "b" * 64,
            "launcher": {
                "path": "bin/agent-cockpit",
                "size": 1,
                "sha256": "c" * 64,
                "format": "elf",
            },
        }],
    }
    index_bytes = canonical_bytes(index)
    (assets / "release-index.json").write_bytes(index_bytes)
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    result = subprocess.run(
        [sys.executable, "-c", signing_block],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "RUNNER_TEMP": str(tmp_path),
            "SERVER_RELEASE_ED25519_PRIVATE_KEY_B64": base64.b64encode(private).decode("ascii"),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    signature = (assets / "release-index.json.sig").read_bytes()
    assert len(signature) == 64
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    assert verify_release_index(
        index_bytes, signature, public, platform="linux", arch="x86_64",
    )["source_sha"] == "a" * 40


def test_release_workflow_signing_block_fails_without_secret(tmp_path: Path) -> None:
    signing_block = next(
        block for block in _python_blocks()
        if "SERVER_RELEASE_ED25519_PRIVATE_KEY_B64" in block
    )
    assets = tmp_path / "release-assets"
    assets.mkdir()
    (assets / "release-index.json").write_bytes(b"{}")
    env = {
        key: value for key, value in os.environ.items()
        if key != "SERVER_RELEASE_ED25519_PRIVATE_KEY_B64"
    }
    env.update({"PYTHONPATH": str(ROOT), "RUNNER_TEMP": str(tmp_path)})

    result = subprocess.run(
        [sys.executable, "-c", signing_block],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "release_signing_key_invalid" in result.stderr
    assert not (assets / "release-index.json.sig").exists()


def test_release_workflow_pins_every_action_to_full_sha() -> None:
    raw = _workflow()
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)\s*$", raw, flags=re.M)
    assert uses
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}", ref) for ref in uses)
    assert any(ref.startswith("actions/upload-artifact@") for ref in uses)
    assert any(ref.startswith("actions/download-artifact@") for ref in uses)
    assert not any(ref.startswith("softprops/action-gh-release@") for ref in uses)
