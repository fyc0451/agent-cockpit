from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Iterator

import pytest

import artifact_download
from artifact_download import ArtifactDownloadError, download_verified_artifact


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.chunks = chunks
        self.status_code = status_code
        self.headers = headers or {}
        self.stream_error = stream_error

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def iter_bytes(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.stream_error is not None:
            raise self.stream_error


class FakeTransport:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[str] = []

    def __call__(self, url: str) -> FakeResponse:
        self.calls.append(url)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _asset(payload: bytes = b"verified artifact") -> dict[str, object]:
    name = "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz"
    return {
        "name": name,
        "edition": "server",
        "platform": "linux",
        "arch": "x86_64",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "url": (
            "https://github.com/fyc0451/agent-cockpit/releases/download/"
            f"agent-cockpit-v1.2.3/{name}"
        ),
    }


def _assert_code(code: str, call: object) -> None:
    with pytest.raises(ArtifactDownloadError) as exc_info:
        call()  # type: ignore[operator]
    assert exc_info.value.code == code
    assert str(exc_info.value) == code


def test_downloads_to_content_addressed_cache_and_reuses_verified_file(
    tmp_path: Path,
) -> None:
    payload = b"verified artifact"
    asset = _asset(payload)
    transport = FakeTransport(
        FakeResponse(
            [payload[:4], payload[4:]],
            headers={"content-length": str(len(payload))},
        )
    )

    result = download_verified_artifact(asset, tmp_path / "cache", transport=transport)

    assert result == tmp_path / "cache" / asset["sha256"]
    assert result.read_bytes() == payload
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.parent.stat().st_mode) == 0o700
    assert transport.calls == [asset["url"]]

    no_network = FakeTransport(AssertionError("cache hit must not use transport"))
    assert (
        download_verified_artifact(asset, result.parent, transport=no_network)
        == result
    )
    assert no_network.calls == []


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda asset: asset.update(name="../artifact.tar.gz"), "invalid_asset"),
        (lambda asset: asset.update(name="artifact..tar.gz"), "invalid_asset"),
        (lambda asset: asset.update(name="x" * 129), "invalid_asset"),
        (lambda asset: asset.update(size=True), "invalid_asset"),
        (lambda asset: asset.update(size=0), "invalid_asset"),
        (
            lambda asset: asset.update(
                size=artifact_download.MAX_ASSET_BYTES + 1
            ),
            "invalid_asset",
        ),
        (lambda asset: asset.update(sha256="A" * 64), "invalid_asset"),
        (lambda asset: asset.update(sha256="a" * 63), "invalid_asset"),
        (
            lambda asset: asset.update(
                url="http://github.com/fyc0451/agent-cockpit/releases/download/"
                "agent-cockpit-v1.2.3/artifact"
            ),
            "invalid_url",
        ),
        (
            lambda asset: asset.update(
                url="https://evil.invalid/fyc0451/agent-cockpit/releases/download/"
                "agent-cockpit-v1.2.3/artifact"
            ),
            "invalid_url",
        ),
        (
            lambda asset: asset.update(
                url="https://user@github.com/fyc0451/agent-cockpit/releases/"
                "download/agent-cockpit-v1.2.3/artifact"
            ),
            "invalid_url",
        ),
        (
            lambda asset: asset.update(
                url="https://github.com:444/fyc0451/agent-cockpit/releases/"
                "download/agent-cockpit-v1.2.3/artifact"
            ),
            "invalid_url",
        ),
        (
            lambda asset: asset.update(
                url="https://github.com/fyc0451/agent-cockpit/releases/download/"
                "agent-cockpit-v1.2.3/artifact?x=1"
            ),
            "invalid_url",
        ),
        (
            lambda asset: asset.update(
                url="https://github.com/fyc0451/agent-cockpit/releases/download/"
                "agent-cockpit-v1.2.3/artifact#fragment"
            ),
            "invalid_url",
        ),
        (
            lambda asset: asset.update(
                url="https://github.com/fyc0451/other/releases/download/"
                "agent-cockpit-v1.2.3/artifact"
            ),
            "invalid_url",
        ),
        (
            lambda asset: asset.update(
                url="https://github.com/fyc0451/agent-cockpit/releases/download/"
                "agent-cockpit-v1.2.3/other.tar.gz"
            ),
            "invalid_url",
        ),
    ],
)
def test_rejects_invalid_metadata_before_network(
    tmp_path: Path, mutation: object, code: str
) -> None:
    asset = _asset()
    mutation(asset)  # type: ignore[operator]
    transport = FakeTransport(AssertionError("invalid input must not use transport"))

    _assert_code(
        code,
        lambda: download_verified_artifact(
            asset, tmp_path / "cache", transport=transport
        ),
    )
    assert transport.calls == []


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_rejects_redirects_without_writing(tmp_path: Path, status: int) -> None:
    asset = _asset()
    _assert_code(
        "download_redirect",
        lambda: download_verified_artifact(
            asset,
            tmp_path / "cache",
            transport=FakeTransport(FakeResponse([], status_code=status)),
        ),
    )
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (FakeResponse([], status_code=500), "download_failed"),
        (FakeResponse([b"short"]), "size_mismatch"),
        (FakeResponse([b"verified artifact plus"]), "size_mismatch"),
        (FakeResponse([b"wrong artifact!!!"]), "digest_mismatch"),
        (
            FakeResponse(
                [b"verified artifact"], headers={"content-length": "1"}
            ),
            "size_mismatch",
        ),
        (FakeResponse([], stream_error=OSError("stream failed")), "download_failed"),
    ],
)
def test_download_failures_are_closed_and_remove_part_files(
    tmp_path: Path, response: FakeResponse, code: str
) -> None:
    cache = tmp_path / "cache"
    _assert_code(
        code,
        lambda: download_verified_artifact(
            _asset(), cache, transport=FakeTransport(response)
        ),
    )
    assert list(cache.iterdir()) == []


def test_transport_open_failure_is_closed_and_removes_part(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _assert_code(
        "download_failed",
        lambda: download_verified_artifact(
            _asset(), cache, transport=FakeTransport(OSError("offline"))
        ),
    )
    assert list(cache.iterdir()) == []


def test_rejects_symlink_cache_directory_before_network(tmp_path: Path) -> None:
    real_cache = tmp_path / "real"
    real_cache.mkdir()
    cache = tmp_path / "cache"
    cache.symlink_to(real_cache, target_is_directory=True)
    transport = FakeTransport(AssertionError("unsafe cache must not use transport"))

    _assert_code(
        "cache_path_invalid",
        lambda: download_verified_artifact(_asset(), cache, transport=transport),
    )
    assert transport.calls == []


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_rejects_non_regular_existing_cache_object_without_removing_it(
    tmp_path: Path, kind: str
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    target = cache / _asset()["sha256"]  # type: ignore[operator]
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"do not touch")
        target.symlink_to(outside)
    elif kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)

    _assert_code(
        "cache_invalid",
        lambda: download_verified_artifact(
            _asset(), cache, transport=FakeTransport(AssertionError("no network"))
        ),
    )
    assert target.exists() or target.is_symlink()


def test_rejects_corrupt_existing_cache_object_without_deleting_it(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    target = cache / _asset()["sha256"]  # type: ignore[operator]
    target.write_bytes(b"corrupt")

    _assert_code(
        "cache_invalid",
        lambda: download_verified_artifact(
            _asset(), cache, transport=FakeTransport(AssertionError("no network"))
        ),
    )
    assert target.read_bytes() == b"corrupt"


def test_success_fsyncs_file_and_directory_and_publishes_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsync_calls: list[int] = []
    link_calls: list[tuple[object, object]] = []
    real_fsync = os.fsync
    real_link = os.link

    def tracking_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    def tracking_link(source: object, target: object, **kwargs: object) -> None:
        link_calls.append((source, target))
        real_link(source, target, **kwargs)

    monkeypatch.setattr(artifact_download.os, "fsync", tracking_fsync)
    monkeypatch.setattr(artifact_download.os, "link", tracking_link)

    result = download_verified_artifact(
        _asset(),
        tmp_path / "cache",
        transport=FakeTransport(FakeResponse([b"verified artifact"])),
    )

    assert result.exists()
    assert len(fsync_calls) == 2
    assert len(link_calls) == 1


@pytest.mark.parametrize("raced_payload", [b"verified artifact", b"corrupt"])
def test_concurrent_cache_publish_never_overwrites_existing_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raced_payload: bytes,
) -> None:
    cache = tmp_path / "cache"

    def racing_link(source: object, target: object, **kwargs: object) -> None:
        Path(target).write_bytes(raced_payload)
        raise FileExistsError

    monkeypatch.setattr(artifact_download.os, "link", racing_link)
    call = lambda: download_verified_artifact(
        _asset(),
        cache,
        transport=FakeTransport(FakeResponse([b"verified artifact"])),
    )

    if raced_payload == b"verified artifact":
        assert call().read_bytes() == raced_payload
    else:
        _assert_code("cache_invalid", call)
    assert (cache / _asset()["sha256"]).read_bytes() == raced_payload
    assert not list(cache.glob("*.part"))
