import asyncio
from io import BytesIO

import pytest

import uploads


class AsyncReader:
    def __init__(self, data: bytes):
        self.data = BytesIO(data)

    async def read(self, size: int) -> bytes:
        return self.data.read(size)


def test_streaming_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path)
    result = asyncio.run(
        uploads.save_upload_file("example.txt", AsyncReader(b"hello"))
    )

    assert result["size"] == 5
    assert (tmp_path / result["path"].split("/")[-1]).read_bytes() == b"hello"


def test_streaming_upload_removes_partial_file_when_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(uploads, "MAX_SIZE", 4)

    with pytest.raises(uploads.UploadTooLarge):
        asyncio.run(uploads.save_upload_file("example.txt", AsyncReader(b"hello")))

    assert list(tmp_path.iterdir()) == []


def test_upload_requires_allowed_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path)

    with pytest.raises(ValueError):
        asyncio.run(uploads.save_upload_file("no-extension", AsyncReader(b"hello")))


def test_upload_runs_disk_sync_in_worker_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path)
    threaded_functions = []

    async def fake_to_thread(func, *args):
        threaded_functions.append(func)
        return func(*args)

    monkeypatch.setattr(uploads.asyncio, "to_thread", fake_to_thread)

    asyncio.run(uploads.save_upload_file("example.txt", AsyncReader(b"hello")))

    assert uploads.os.fsync in threaded_functions
