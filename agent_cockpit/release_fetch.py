"""Fetch bounded signed release metadata through a reusable GitHub transport."""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Protocol
from urllib.parse import urlsplit

import httpx

from .artifact_download import Transport
from .release_index import MAX_INDEX_BYTES, OFFICIAL_RELEASE_DOWNLOAD_PREFIX


_TAG_RE = re.compile(
    r"agent-cockpit-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\Z"
)
_DOWNLOAD_PATH_RE = re.compile(
    r"/fyc0451/agent-cockpit/releases/download/"
    r"agent-cockpit-v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)/[A-Za-z0-9][A-Za-z0-9._-]*\Z"
)
_CDN_HOSTS = {
    "github-releases.githubusercontent.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_SIGNATURE_BYTES = 64


class ReleaseFetchError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Response(Protocol):
    status_code: int
    headers: Any

    def iter_bytes(self) -> Any: ...


def _reject(code: str) -> None:
    raise ReleaseFetchError(code)


def _parsed_url(url: str) -> Any:
    if type(url) is not str:
        _reject("url_invalid")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        _reject("url_invalid")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        _reject("url_invalid")
    return parsed


def _validate_initial_url(url: str) -> None:
    parsed = _parsed_url(url)
    if (
        parsed.netloc != "github.com"
        or parsed.hostname != "github.com"
        or parsed.query
        or _DOWNLOAD_PATH_RE.fullmatch(parsed.path) is None
    ):
        _reject("url_invalid")


def _validate_redirect_url(url: str) -> None:
    try:
        parsed = _parsed_url(url)
    except ReleaseFetchError:
        _reject("redirect_invalid")
    if (
        parsed.hostname not in _CDN_HOSTS
        or parsed.netloc not in _CDN_HOSTS
        or not parsed.path.startswith("/")
        or parsed.path == "/"
    ):
        _reject("redirect_invalid")


class GitHubReleaseTransport:
    """Yield a final GitHub release response after at most one CDN redirect."""

    @contextmanager
    def __call__(self, url: str) -> Iterator[_Response]:
        _validate_initial_url(url)
        try:
            with httpx.stream(
                "GET", url, follow_redirects=False, timeout=httpx.Timeout(30.0)
            ) as first:
                if first.status_code == 200:
                    yield first
                    return
                if first.status_code not in _REDIRECT_CODES:
                    _reject("fetch_failed")
                location = first.headers.get("location")
                if type(location) is not str:
                    _reject("redirect_invalid")
                _validate_redirect_url(location)
            with httpx.stream(
                "GET", location, follow_redirects=False, timeout=httpx.Timeout(30.0)
            ) as final:
                if final.status_code != 200:
                    _reject("redirect_invalid")
                yield final
        except ReleaseFetchError:
            raise
        except httpx.HTTPError as exc:
            raise ReleaseFetchError("fetch_failed") from exc


@dataclass(frozen=True)
class ReleasePayloads:
    tag: str
    index_bytes: bytes
    signature_bytes: bytes
    transport: Transport


def _read_payload(response: _Response, *, limit: int, exact: bool = False) -> bytes:
    if response.status_code != 200:
        _reject("fetch_failed")
    content_length = response.headers.get("content-length")
    declared: int | None = None
    if content_length is not None:
        if (
            type(content_length) is not str
            or not content_length.isascii()
            or not content_length.isdecimal()
        ):
            _reject("fetch_failed")
        declared = int(content_length)
        if declared > limit:
            _reject("signature_size_invalid" if exact else "payload_too_large")
        if exact and declared != limit:
            _reject("signature_size_invalid")
    output = bytearray()
    try:
        for chunk in response.iter_bytes():
            if type(chunk) is not bytes:
                _reject("fetch_failed")
            if len(chunk) > limit - len(output):
                _reject("signature_size_invalid" if exact else "payload_too_large")
            output.extend(chunk)
    except ReleaseFetchError:
        raise
    except Exception as exc:
        raise ReleaseFetchError("fetch_failed") from exc
    if exact and len(output) != limit:
        _reject("signature_size_invalid")
    if declared is not None and len(output) != declared:
        _reject("fetch_failed")
    if not output:
        _reject("payload_empty")
    return bytes(output)


def fetch_release_payloads(
    tag: str,
    *,
    transport: Transport | None = None,
) -> ReleasePayloads:
    """Fetch the canonical index and its exact raw Ed25519 signature."""
    if type(tag) is not str or _TAG_RE.fullmatch(tag) is None:
        _reject("invalid_tag")
    selected_transport = transport if transport is not None else GitHubReleaseTransport()
    index_url = f"{OFFICIAL_RELEASE_DOWNLOAD_PREFIX}/{tag}/release-index.json"
    signature_url = f"{OFFICIAL_RELEASE_DOWNLOAD_PREFIX}/{tag}/release-index.json.sig"
    with selected_transport(index_url) as response:
        index_bytes = _read_payload(response, limit=MAX_INDEX_BYTES)
    with selected_transport(signature_url) as response:
        signature_bytes = _read_payload(
            response, limit=_SIGNATURE_BYTES, exact=True
        )
    return ReleasePayloads(
        tag=tag,
        index_bytes=index_bytes,
        signature_bytes=signature_bytes,
        transport=selected_transport,
    )


__all__ = [
    "GitHubReleaseTransport",
    "ReleaseFetchError",
    "ReleasePayloads",
    "Transport",
    "fetch_release_payloads",
]
