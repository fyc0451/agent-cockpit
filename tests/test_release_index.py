from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from release_index import (
    MAX_LAUNCHER_BYTES,
    OFFICIAL_RELEASE_DOWNLOAD_PREFIX,
    ReleaseIndexError,
    SERVER_LAUNCHER_PATH,
    canonical_bytes,
    verify_release_index,
)


@pytest.fixture()
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _index() -> dict[str, object]:
    return {
        "schema_version": 2,
        "tag": "agent-cockpit-v1.2.3",
        "version": "1.2.3",
        "source_sha": "1" * 40,
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz",
                "edition": "server",
                "platform": "linux",
                "arch": "x86_64",
                "size": 123456,
                "sha256": "a" * 64,
                "launcher": {
                    "path": SERVER_LAUNCHER_PATH,
                    "size": 45678,
                    "sha256": "c" * 64,
                    "format": "elf",
                },
            },
            {
                "name": "agent-cockpit-server-1.2.3-macos-arm64.tar.gz",
                "edition": "server",
                "platform": "macos",
                "arch": "arm64",
                "size": 234567,
                "sha256": "b" * 64,
                "launcher": {
                    "path": SERVER_LAUNCHER_PATH,
                    "size": 56789,
                    "sha256": "d" * 64,
                    "format": "mach-o",
                },
            },
        ],
    }


def _public_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _verify(
    data: dict[str, object], signing_key: Ed25519PrivateKey, **kwargs: object
) -> dict[str, object]:
    payload = canonical_bytes(data)
    return verify_release_index(
        payload,
        signing_key.sign(payload),
        _public_bytes(signing_key),
        platform=kwargs.get("platform", "linux"),
        arch=kwargs.get("arch", "x86_64"),
    )


def _assert_code(code: str, call: object) -> None:
    with pytest.raises(ReleaseIndexError) as exc_info:
        call()  # type: ignore[operator]
    assert exc_info.value.code == code
    assert str(exc_info.value) == code


def test_verifies_signature_and_selects_one_official_asset(
    signing_key: Ed25519PrivateKey,
) -> None:
    verified = _verify(_index(), signing_key)

    selected = verified["selected_asset"]
    assert isinstance(selected, dict)
    assert selected["name"] == "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz"
    assert selected["url"] == (
        f"{OFFICIAL_RELEASE_DOWNLOAD_PREFIX}/agent-cockpit-v1.2.3/"
        "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz"
    )


def test_rejects_noncanonical_bytes_before_use(signing_key: Ed25519PrivateKey) -> None:
    data = _index()
    payload = json.dumps(data, indent=2).encode()

    _assert_code(
        "invalid_target",
        lambda: verify_release_index(
            payload,
            signing_key.sign(payload),
            _public_bytes(signing_key),
            platform="linux",
            arch="x86_64",
        ),
    )

@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update(extra=True), "invalid_target"),
        (lambda value: value.update(tag="v1.2.3"), "tag_version_mismatch"),
        (lambda value: value.update(version="1.2.4"), "tag_version_mismatch"),
        (lambda value: value.update(source_sha="A" * 40), "source_sha_mismatch"),
        (lambda value: value.update(draft=True), "release_not_final"),
        (lambda value: value.update(prerelease=True), "release_not_final"),
    ],
)
def test_rejects_invalid_release_fields(
    signing_key: Ed25519PrivateKey, mutation: object, code: str
) -> None:
    data = _index()
    mutation(data)  # type: ignore[operator]
    _assert_code(code, lambda: _verify(data, signing_key))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda asset: asset.update(url="https://evil.invalid/redirect"),
        lambda asset: asset.update(size=True),
        lambda asset: asset.update(size=0),
        lambda asset: asset.update(sha256="A" * 64),
        lambda asset: asset.update(edition="desktop"),
        lambda asset: asset.update(name="../agent-cockpit.tar.gz"),
        lambda asset: asset.update(name="agent-cockpit%2ftar.gz"),
        lambda asset: asset.update(name="agent-cockpit..tar.gz"),
    ],
)
def test_rejects_invalid_or_redirect_asset_fields(
    signing_key: Ed25519PrivateKey, mutation: object
) -> None:
    data = _index()
    asset = data["assets"][0]  # type: ignore[index]
    mutation(asset)  # type: ignore[operator]
    _assert_code("invalid_target", lambda: _verify(data, signing_key))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda launcher: launcher.update(extra=True),
        lambda launcher: launcher.pop("path"),
        lambda launcher: launcher.update(path="app/bin/agent-cockpit"),
        lambda launcher: launcher.update(path=True),
        lambda launcher: launcher.update(size=True),
        lambda launcher: launcher.update(size=0),
        lambda launcher: launcher.update(size=MAX_LAUNCHER_BYTES + 1),
        lambda launcher: launcher.update(sha256="A" * 64),
        lambda launcher: launcher.update(format="mach-o"),
        lambda launcher: launcher.update(format=True),
    ],
)
def test_rejects_invalid_linux_launcher_contract(
    signing_key: Ed25519PrivateKey, mutation: object
) -> None:
    data = _index()
    launcher = data["assets"][0]["launcher"]  # type: ignore[index]
    mutation(launcher)  # type: ignore[operator]

    _assert_code("invalid_target", lambda: _verify(data, signing_key))


def test_rejects_launcher_larger_than_its_artifact(
    signing_key: Ed25519PrivateKey,
) -> None:
    data = _index()
    asset = data["assets"][0]  # type: ignore[index]
    asset["launcher"]["size"] = asset["size"] + 1

    _assert_code("invalid_target", lambda: _verify(data, signing_key))


def test_rejects_v1_index_without_launcher_for_server_upgrade(
    signing_key: Ed25519PrivateKey,
) -> None:
    data = _index()
    data["schema_version"] = 1
    for asset in data["assets"]:  # type: ignore[union-attr]
        asset.pop("launcher")

    _assert_code("invalid_target", lambda: _verify(data, signing_key))


def test_rejects_duplicate_launcher_json_key(
    signing_key: Ed25519PrivateKey,
) -> None:
    payload = canonical_bytes(_index())
    duplicated = payload.replace(
        b'"path":"bin/agent-cockpit"',
        b'"path":"bin/agent-cockpit","path":"bin/agent-cockpit"',
        1,
    )

    _assert_code(
        "invalid_target",
        lambda: verify_release_index(
            duplicated,
            signing_key.sign(duplicated),
            _public_bytes(signing_key),
            platform="linux",
            arch="x86_64",
        ),
    )


def test_rejects_duplicate_asset_name(signing_key: Ed25519PrivateKey) -> None:
    data = _index()
    assets = data["assets"]
    assets[1]["name"] = assets[0]["name"]  # type: ignore[index]

    _assert_code("invalid_target", lambda: _verify(data, signing_key))


def test_rejects_zero_or_multiple_target_assets(
    signing_key: Ed25519PrivateKey,
) -> None:
    _assert_code(
        "asset_unsupported",
        lambda: _verify(_index(), signing_key, platform="linux", arch="arm64"),
    )
    data = _index()
    data["assets"].append(dict(data["assets"][0], name="other.tar.gz"))  # type: ignore[union-attr,index]
    _assert_code("invalid_target", lambda: _verify(data, signing_key))


def test_rejects_duplicate_json_object_key(signing_key: Ed25519PrivateKey) -> None:
    payload = canonical_bytes(_index())
    duplicated = payload.replace(b'"tag":', b'"tag":"agent-cockpit-v9.9.9","tag":', 1)

    _assert_code(
        "invalid_target",
        lambda: verify_release_index(
            duplicated,
            signing_key.sign(duplicated),
            _public_bytes(signing_key),
            platform="linux",
            arch="x86_64",
        ),
    )


def test_rejects_tampered_signature_and_wrong_key(
    signing_key: Ed25519PrivateKey,
) -> None:
    payload = canonical_bytes(_index())
    signature = bytearray(signing_key.sign(payload))
    signature[0] ^= 1

    _assert_code(
        "index_signature_invalid",
        lambda: verify_release_index(
            payload,
            bytes(signature),
            _public_bytes(signing_key),
            platform="linux",
            arch="x86_64",
        ),
    )
    other_key = Ed25519PrivateKey.generate()
    _assert_code(
        "index_signature_invalid",
        lambda: verify_release_index(
            payload,
            signing_key.sign(payload),
            _public_bytes(other_key),
            platform="linux",
            arch="x86_64",
        ),
    )


def test_rejects_oversized_inputs(signing_key: Ed25519PrivateKey) -> None:
    payload = b" " * (256 * 1024 + 1)
    _assert_code(
        "invalid_target",
        lambda: verify_release_index(
            payload,
            signing_key.sign(payload),
            _public_bytes(signing_key),
            platform="linux",
            arch="x86_64",
        ),
    )
