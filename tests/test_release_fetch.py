from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
from typing import Iterator

import pytest

from agent_cockpit import release_fetch
from agent_cockpit.artifact_download import ArtifactDownloadError, download_verified_artifact
from agent_cockpit.release_index import MAX_INDEX_BYTES, OFFICIAL_RELEASE_DOWNLOAD_PREFIX


TAG = "agent-cockpit-v1.2.3"
INDEX_URL = f"{OFFICIAL_RELEASE_DOWNLOAD_PREFIX}/{TAG}/release-index.json"
SIGNATURE_URL = f"{OFFICIAL_RELEASE_DOWNLOAD_PREFIX}/{TAG}/release-index.json.sig"
CDN_URL = (
    "https://objects.githubusercontent.com/github-production-release-asset-2e65be/"
    "123/release-index.json?download=1"
)
ASSET_API_URL = (
    "https://api.github.com/repos/fyc0451/agent-cockpit/releases/assets/123"
)


@pytest.fixture(autouse=True)
def _no_host_github_token(monkeypatch) -> None:
    monkeypatch.setattr(release_fetch, "load_github_release_token", lambda: None)


class Response:
    def __init__(
        self,
        status_code: int,
        chunks: list[bytes] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.chunks = chunks or []
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_bytes(self) -> Iterator[bytes]:
        yield from self.chunks


class QueueTransport:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str) -> AbstractContextManager[Response]:
        self.calls.append(url)
        return self.responses.pop(0)


def test_fetches_exact_release_payloads_and_returns_reusable_transport() -> None:
    index = b'{"schema_version":2}'
    signature = b"s" * 64
    transport = QueueTransport([
        Response(200, [index[:5], index[5:]], headers={"content-length": str(len(index))}),
        Response(200, [signature], headers={"content-length": "64"}),
    ])

    payloads = release_fetch.fetch_release_payloads(TAG, transport=transport)

    assert payloads == release_fetch.ReleasePayloads(
        tag=TAG,
        index_bytes=index,
        signature_bytes=signature,
        transport=transport,
    )
    assert transport.calls == [INDEX_URL, SIGNATURE_URL]
    with pytest.raises(FrozenInstanceError):
        payloads.tag = "agent-cockpit-v9.9.9"  # type: ignore[misc]


def test_default_transport_accepts_one_github_asset_cdn_redirect(monkeypatch) -> None:
    opened: list[tuple[str, bool, dict[str, str]]] = []
    metadata_headers: dict[str, str] = {}
    responses = [
        Response(302, headers={"location": CDN_URL}),
        Response(200, [b"payload"], headers={"content-length": "7"}),
    ]

    def stream(_method: str, url: str, **kwargs):
        opened.append((url, kwargs["follow_redirects"], kwargs["headers"]))
        return responses.pop(0)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, _url: str, *, headers: dict[str, str]):
            metadata_headers.update(headers)
            return __import__("httpx").Response(200, json={
                "tag_name": TAG,
                "draft": False,
                "prerelease": False,
                "assets": [{
                    "name": "release-index.json",
                    "url": ASSET_API_URL,
                    "browser_download_url": INDEX_URL,
                }],
            })

    monkeypatch.setattr(
        release_fetch, "load_github_release_token", lambda: "secret-token-value-123",
    )
    monkeypatch.setattr(release_fetch.httpx, "Client", Client)
    monkeypatch.setattr(release_fetch.httpx, "stream", stream)
    transport = release_fetch.GitHubReleaseTransport()

    with transport(INDEX_URL) as response:
        assert response.status_code == 200
        assert b"".join(response.iter_bytes()) == b"payload"

    assert metadata_headers["Authorization"] == "Bearer secret-token-value-123"
    assert opened[0][0:2] == (ASSET_API_URL, False)
    assert opened[0][2]["Authorization"] == "Bearer secret-token-value-123"
    assert opened[1][0:2] == (CDN_URL, False)
    assert "Authorization" not in opened[1][2]


def test_default_transport_is_reusable_by_artifact_downloader(monkeypatch, tmp_path) -> None:
    payload = b"server archive"
    asset_name = "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz"
    asset_url = f"{OFFICIAL_RELEASE_DOWNLOAD_PREFIX}/{TAG}/{asset_name}"
    responses = [
        Response(302, headers={"location": CDN_URL}),
        Response(200, [payload], headers={"content-length": str(len(payload))}),
    ]
    monkeypatch.setattr(release_fetch, "load_github_release_token", lambda: None)
    monkeypatch.setattr(
        release_fetch.httpx, "stream", lambda *_args, **_kwargs: responses.pop(0),
    )
    transport: release_fetch.Transport = release_fetch.GitHubReleaseTransport()
    asset = {
        "name": asset_name,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "url": asset_url,
    }

    path = download_verified_artifact(
        asset, tmp_path / "cache", transport=transport,
    )

    assert path.read_bytes() == payload


def test_transport_preserves_artifact_downloader_verification_errors(
    monkeypatch, tmp_path,
) -> None:
    payload = b"wrong archive"
    asset_name = "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz"
    responses = [Response(200, [payload], headers={"content-length": str(len(payload))})]
    monkeypatch.setattr(release_fetch, "load_github_release_token", lambda: None)
    monkeypatch.setattr(
        release_fetch.httpx, "stream", lambda *_args, **_kwargs: responses.pop(0),
    )
    asset = {
        "name": asset_name,
        "size": len(payload),
        "sha256": "a" * 64,
        "url": f"{OFFICIAL_RELEASE_DOWNLOAD_PREFIX}/{TAG}/{asset_name}",
    }

    with pytest.raises(ArtifactDownloadError, match="digest_mismatch"):
        download_verified_artifact(
            asset,
            tmp_path / "cache",
            transport=release_fetch.GitHubReleaseTransport(),
        )


@pytest.mark.parametrize(
    "tag",
    [
        "v1.2.3",
        "agent-cockpit-v01.2.3",
        "agent-cockpit-v1.2",
        "agent-cockpit-v1.2.3/other",
        "agent-cockpit-v1.2.3?x=1",
        "",
        None,
    ],
)
def test_rejects_invalid_tag_before_transport(tag) -> None:
    transport = QueueTransport([])

    with pytest.raises(release_fetch.ReleaseFetchError, match="invalid_tag"):
        release_fetch.fetch_release_payloads(tag, transport=transport)

    assert transport.calls == []


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (Response(404), "fetch_failed"),
        (Response(302), "redirect_invalid"),
        (Response(302, headers={"location": "http://objects.githubusercontent.com/x"}), "redirect_invalid"),
        (Response(302, headers={"location": "https://evil.invalid/x"}), "redirect_invalid"),
    ],
)
def test_default_transport_rejects_non_final_or_invalid_redirect(
    monkeypatch, response: Response, code: str,
) -> None:
    monkeypatch.setattr(release_fetch, "load_github_release_token", lambda: None)
    monkeypatch.setattr(
        release_fetch.httpx, "stream", lambda *_args, **_kwargs: response,
    )

    with pytest.raises(release_fetch.ReleaseFetchError, match=code):
        with release_fetch.GitHubReleaseTransport()(INDEX_URL):
            pass


def test_default_transport_rejects_second_redirect(monkeypatch) -> None:
    responses = [
        Response(302, headers={"location": CDN_URL}),
        Response(302, headers={"location": CDN_URL}),
    ]
    monkeypatch.setattr(release_fetch, "load_github_release_token", lambda: None)
    monkeypatch.setattr(
        release_fetch.httpx, "stream", lambda *_args, **_kwargs: responses.pop(0),
    )

    with pytest.raises(release_fetch.ReleaseFetchError, match="redirect_invalid"):
        with release_fetch.GitHubReleaseTransport()(INDEX_URL):
            pass


@pytest.mark.parametrize(
    ("index_chunks", "signature_chunks", "code"),
    [
        ([b"x" * MAX_INDEX_BYTES, b"x"], [b"s" * 64], "payload_too_large"),
        ([b"{}"], [b"s" * 63], "signature_size_invalid"),
        ([b"{}"], [b"s" * 65], "signature_size_invalid"),
        ([], [b"s" * 64], "payload_empty"),
    ],
)
def test_payload_bounds_fail_closed(index_chunks, signature_chunks, code) -> None:
    transport = QueueTransport([
        Response(200, index_chunks),
        Response(200, signature_chunks),
    ])

    with pytest.raises(release_fetch.ReleaseFetchError, match=code):
        release_fetch.fetch_release_payloads(TAG, transport=transport)


def test_content_length_is_bounded_before_streaming() -> None:
    response = Response(
        200, headers={"content-length": str(MAX_INDEX_BYTES + 1)}
    )
    response.iter_bytes = lambda: pytest.fail("must not stream")  # type: ignore[method-assign]
    transport = QueueTransport([response])

    with pytest.raises(release_fetch.ReleaseFetchError, match="payload_too_large"):
        release_fetch.fetch_release_payloads(TAG, transport=transport)


def test_content_length_mismatch_fails_closed() -> None:
    transport = QueueTransport([
        Response(200, [b"{}"], headers={"content-length": "3"}),
    ])

    with pytest.raises(release_fetch.ReleaseFetchError, match="fetch_failed"):
        release_fetch.fetch_release_payloads(TAG, transport=transport)


def test_transport_rejects_non_official_initial_url_without_request(monkeypatch) -> None:
    monkeypatch.setattr(
        release_fetch.httpx,
        "stream",
        lambda *_args, **_kwargs: pytest.fail("request must not run"),
    )

    with pytest.raises(release_fetch.ReleaseFetchError, match="url_invalid"):
        with release_fetch.GitHubReleaseTransport()("https://evil.invalid/release-index.json"):
            pass
