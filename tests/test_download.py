from fastapi.testclient import TestClient

import files
import server


def test_download_path_accepts_file(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_HOME", tmp_path.resolve())
    upload_dir = tmp_path / "dashboard-uploads"
    upload_dir.mkdir()
    target = upload_dir / "example.bin"
    target.write_bytes(b"download me")

    assert files.download_path(str(target)) == target.resolve()


def test_download_path_rejects_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_HOME", tmp_path.resolve())
    target = tmp_path / "dashboard-uploads" / "subdir"
    target.mkdir(parents=True)

    try:
        files.download_path(str(target))
    except ValueError as exc:
        assert "不是文件" in str(exc)
    else:
        raise AssertionError("directory should not be downloadable")


def test_download_endpoint_streams_attachment(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_HOME", tmp_path.resolve())
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    upload_dir = tmp_path / "dashboard-uploads"
    upload_dir.mkdir()
    target = upload_dir / "example.txt"
    target.write_text("hello", encoding="utf-8")

    response = TestClient(server.app).get(
        "/api/files/download",
        params={"path": str(target)},
        headers={"authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["content-disposition"] == 'attachment; filename="example.txt"'
