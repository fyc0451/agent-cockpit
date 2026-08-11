from fastapi.testclient import TestClient

import files
import runtime_paths
import server


def _isolate_download_roots(tmp_path, monkeypatch):
    """Align files allowlist with tmp_path under hermetic basetemp (/tmp/...).

    uploads/data roots come from runtime_paths, not files._HOME alone.
    """
    home = tmp_path.resolve()
    data = home / "dashboard-data"
    uploads = home / "dashboard-uploads"
    config = home / ".config" / "agent-cockpit"
    state = home / ".local" / "state" / "agent-cockpit"
    for path in (data, uploads, config, state):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(files, "_HOME", home)
    monkeypatch.setenv("COCKPIT_DATA_DIR", str(data))
    monkeypatch.setenv("COCKPIT_UPLOADS_DIR", str(uploads))
    monkeypatch.setenv("COCKPIT_CONFIG_DIR", str(config))
    monkeypatch.setenv("COCKPIT_STATE_DIR", str(state))
    monkeypatch.delenv("COCKPIT_COORDINATION_DB", raising=False)
    runtime_paths.reset_cache()
    return uploads


def test_download_path_accepts_file(tmp_path, monkeypatch):
    uploads = _isolate_download_roots(tmp_path, monkeypatch)
    target = uploads / "example.bin"
    target.write_bytes(b"download me")

    assert files.download_path(str(target)) == target.resolve()


def test_download_path_rejects_directory(tmp_path, monkeypatch):
    uploads = _isolate_download_roots(tmp_path, monkeypatch)
    target = uploads / "subdir"
    target.mkdir(parents=True)

    try:
        files.download_path(str(target))
    except ValueError as exc:
        assert "不是文件" in str(exc)
    else:
        raise AssertionError("directory should not be downloadable")


def test_download_endpoint_streams_attachment(tmp_path, monkeypatch):
    uploads = _isolate_download_roots(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    target = uploads / "example.txt"
    target.write_text("hello", encoding="utf-8")

    response = TestClient(server.app).get(
        "/api/files/download",
        params={"path": str(target)},
        headers={"authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["content-disposition"] == 'attachment; filename="example.txt"'
