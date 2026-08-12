from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


OFFICIAL_RELEASE_DOWNLOAD_PREFIX = (
    "https://github.com/fyc0451/agent-cockpit/releases/download"
)
MAX_INDEX_BYTES = 256 * 1024
MAX_ASSETS = 64
MAX_ASSET_BYTES = 8 * 1024 * 1024 * 1024
MAX_ASSET_NAME_BYTES = 128
MAX_LAUNCHER_BYTES = 8 * 1024 * 1024 * 1024
SERVER_LAUNCHER_PATH = "bin/agent-cockpit"
SERVER_LAUNCHER_FORMATS = {"linux": "elf", "macos": "mach-o"}
# Fixed filenames persisted inside a generation so the install verifier can
# re-verify the signed release-index without an external trust channel.
PERSISTED_INDEX_NAME = ".release-index.json"
PERSISTED_SIGNATURE_NAME = ".release-index.json.sig"

_INDEX_FIELDS = {
    "schema_version",
    "tag",
    "version",
    "source_sha",
    "draft",
    "prerelease",
    "assets",
}
_ASSET_FIELDS = {
    "name",
    "edition",
    "platform",
    "arch",
    "size",
    "sha256",
    "launcher",
}
_LAUNCHER_FIELDS = {"path", "size", "sha256", "format"}
_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ASSET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_PLATFORMS = {"linux", "macos"}
_ARCHITECTURES = {"x86_64", "arm64"}


class ReleaseIndexError(ValueError):
    """A public, stable release-index rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _DuplicateKeyError(ValueError):
    pass


def _reject(code: str = "invalid_target") -> None:
    raise ReleaseIndexError(code)


def canonical_bytes(data: dict[str, Any]) -> bytes:
    """Serialize an index using the only byte representation accepted for signing."""
    if type(data) is not dict:
        _reject()
    try:
        encoded = json.dumps(
            data,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _reject()
    if len(encoded) > MAX_INDEX_BYTES:
        _reject()
    return encoded


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _parse_canonical(index_bytes: bytes) -> dict[str, Any]:
    if type(index_bytes) is not bytes or not index_bytes or len(index_bytes) > MAX_INDEX_BYTES:
        _reject()
    try:
        parsed = json.loads(
            index_bytes.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
        _reject()
    if type(parsed) is not dict or canonical_bytes(parsed) != index_bytes:
        _reject()
    return parsed


def _verify_signature(
    index_bytes: bytes, signature_bytes: bytes, public_key_bytes: bytes
) -> None:
    if (
        type(signature_bytes) is not bytes
        or len(signature_bytes) != 64
        or type(public_key_bytes) is not bytes
        or len(public_key_bytes) != 32
    ):
        _reject("index_signature_invalid")
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            signature_bytes, index_bytes
        )
    except (InvalidSignature, ValueError, TypeError):
        _reject("index_signature_invalid")


def _require_string(value: Any, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _reject()
    return value


def _validate_asset(asset: Any) -> dict[str, Any]:
    if type(asset) is not dict or set(asset) != _ASSET_FIELDS:
        _reject()

    name = _require_string(asset["name"], _ASSET_NAME_RE)
    try:
        name_bytes = name.encode("ascii")
    except UnicodeEncodeError:
        _reject()
    if (
        len(name_bytes) > MAX_ASSET_NAME_BYTES
        or ".." in name
        or "/" in name
        or "\\" in name
    ):
        _reject()
    if asset["edition"] != "server" or type(asset["edition"]) is not str:
        _reject()
    if type(asset["platform"]) is not str or asset["platform"] not in _PLATFORMS:
        _reject()
    if type(asset["arch"]) is not str or asset["arch"] not in _ARCHITECTURES:
        _reject()
    if (
        type(asset["size"]) is not int
        or asset["size"] <= 0
        or asset["size"] > MAX_ASSET_BYTES
    ):
        _reject()
    _require_string(asset["sha256"], _SHA256_RE)
    launcher = asset["launcher"]
    if type(launcher) is not dict or set(launcher) != _LAUNCHER_FIELDS:
        _reject()
    if type(launcher["path"]) is not str or launcher["path"] != SERVER_LAUNCHER_PATH:
        _reject()
    if (
        type(launcher["size"]) is not int
        or launcher["size"] <= 0
        or launcher["size"] > MAX_LAUNCHER_BYTES
    ):
        _reject()
    _require_string(launcher["sha256"], _SHA256_RE)
    if (
        type(launcher["format"]) is not str
        or launcher["format"] != SERVER_LAUNCHER_FORMATS[asset["platform"]]
    ):
        _reject()
    return asset


def _validate_index(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    if set(parsed) != _INDEX_FIELDS or type(parsed["schema_version"]) is not int:
        _reject()
    if parsed["schema_version"] != 2:
        _reject()

    version = _require_string(parsed["version"], _VERSION_RE)
    expected_tag = f"agent-cockpit-v{version}"
    if type(parsed["tag"]) is not str or parsed["tag"] != expected_tag:
        _reject("tag_version_mismatch")
    if type(parsed["source_sha"]) is not str or _SOURCE_SHA_RE.fullmatch(parsed["source_sha"]) is None:
        _reject("source_sha_mismatch")
    if type(parsed["draft"]) is not bool or type(parsed["prerelease"]) is not bool:
        _reject()
    if parsed["draft"] or parsed["prerelease"]:
        _reject("release_not_final")

    raw_assets = parsed["assets"]
    if type(raw_assets) is not list or not raw_assets or len(raw_assets) > MAX_ASSETS:
        _reject()
    assets = [_validate_asset(asset) for asset in raw_assets]
    names = [asset["name"] for asset in assets]
    if len(set(names)) != len(names):
        _reject()
    identities = [
        (asset["edition"], asset["platform"], asset["arch"]) for asset in assets
    ]
    if len(set(identities)) != len(identities):
        _reject()
    return assets


def verify_release_index(
    index_bytes: bytes,
    signature_bytes: bytes,
    public_key_bytes: bytes,
    *,
    platform: str,
    arch: str,
) -> dict[str, Any]:
    """Verify a canonical v2 index and return its single matching server asset."""
    parsed = _parse_canonical(index_bytes)
    _verify_signature(index_bytes, signature_bytes, public_key_bytes)
    assets = _validate_index(parsed)

    if type(platform) is not str or platform not in _PLATFORMS:
        _reject("asset_unsupported")
    if type(arch) is not str or arch not in _ARCHITECTURES:
        _reject("asset_unsupported")
    matches = [
        asset
        for asset in assets
        if asset["edition"] == "server"
        and asset["platform"] == platform
        and asset["arch"] == arch
    ]
    if len(matches) != 1:
        _reject("asset_unsupported")

    selected = dict(matches[0])
    selected["url"] = (
        f"{OFFICIAL_RELEASE_DOWNLOAD_PREFIX}/{quote(parsed['tag'], safe='')}/"
        f"{quote(selected['name'], safe='')}"
    )
    result = dict(parsed)
    result["selected_asset"] = selected
    return result


__all__ = [
    "MAX_ASSET_BYTES",
    "MAX_ASSET_NAME_BYTES",
    "MAX_ASSETS",
    "MAX_INDEX_BYTES",
    "MAX_LAUNCHER_BYTES",
    "OFFICIAL_RELEASE_DOWNLOAD_PREFIX",
    "PERSISTED_INDEX_NAME",
    "PERSISTED_SIGNATURE_NAME",
    "ReleaseIndexError",
    "SERVER_LAUNCHER_FORMATS",
    "SERVER_LAUNCHER_PATH",
    "canonical_bytes",
    "verify_release_index",
]
