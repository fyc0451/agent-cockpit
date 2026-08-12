import asyncio
from io import BytesIO

import pytest

from agent_cockpit import uploads


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
    monkeypatch.setattr(uploads, "_max_size", lambda: 4)

    with pytest.raises(uploads.UploadTooLarge):
        asyncio.run(uploads.save_upload_file("example.txt", AsyncReader(b"hello")))

    assert list(tmp_path.iterdir()) == []


def test_streaming_upload_removes_partial_file_when_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path)

    class CancelledReader:
        calls = 0

        async def read(self, size: int) -> bytes:
            self.calls += 1
            if self.calls == 1:
                return b"partial"
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(uploads.save_upload_file("example.txt", CancelledReader()))

    assert list(tmp_path.iterdir()) == []


def test_upload_allows_any_extension(tmp_path, monkeypatch):
    """所有格式放开:wav、无扩展名等任意文件都可上传。"""
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path)

    for name in ("audio.wav", "no-extension", "archive.apk", "视频.mp4"):
        result = asyncio.run(uploads.save_upload_file(name, AsyncReader(b"hello")))
        assert result["size"] == 5


def test_upload_runs_disk_sync_in_worker_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path)
    threaded_functions = []

    async def fake_to_thread(func, *args):
        threaded_functions.append(func)
        return func(*args)

    monkeypatch.setattr(uploads.asyncio, "to_thread", fake_to_thread)

    asyncio.run(uploads.save_upload_file("example.txt", AsyncReader(b"hello")))

    assert uploads.os.fsync in threaded_functions


def test_list_uploads_skips_file_deleted_during_listing(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path)
    vanished = tmp_path / "vanished.txt"
    kept = tmp_path / "kept.txt"
    vanished.write_text("gone")
    kept.write_text("keep")
    original_is_file = uploads.Path.is_file

    def delete_after_check(path):
        result = original_is_file(path)
        if path == vanished and result:
            vanished.unlink()
        return result

    monkeypatch.setattr(uploads.Path, "is_file", delete_after_check)

    result = uploads.list_uploads()

    assert [item["filename"] for item in result] == ["kept.txt"]
