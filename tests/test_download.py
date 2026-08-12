from pathlib import Path

from fastapi.testclient import TestClient

from agent_cockpit import files
from agent_cockpit import runtime_paths
import server


def _isolate_download_roots(tmp_path, monkeypatch):
    """Align files allowlist with tmp_path under hermetic basetemp (/tmp/...).

    uploads/data roots come from runtime_paths, not files._HOME alone.
    Per-test empty ``_resolved_roots`` is swapped in via monkeypatch so teardown
    restores the original module object without in-place clear/pollution.
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
    # Do not reset_cache()/clear the live module dict — swap a fresh mapping.
    monkeypatch.setattr(runtime_paths, "_resolved_roots", {})
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


def test_isolate_download_roots_does_not_pollute_original_resolved_roots(
    tmp_path, monkeypatch,
):
    """Helper swaps a fresh dict; must not mutate a pre-seeded mapping in place.

    Do not touch the live module dict: seed an independent object, install it
    first, then let the helper setattr a second empty mapping. Teardown restores
    the real original (never written by this test).
    """
    real_original = runtime_paths._resolved_roots
    real_original_id = id(real_original)
    real_snapshot = dict(real_original)

    seeded: dict[str, Path] = {"data": Path("/seed-data-must-not-mutate")}
    seeded_id = id(seeded)
    monkeypatch.setattr(runtime_paths, "_resolved_roots", seeded)
    assert runtime_paths._resolved_roots is seeded

    uploads = _isolate_download_roots(tmp_path, monkeypatch)
    # Helper's second setattr replaces seeded with a new empty/filled dict.
    assert runtime_paths._resolved_roots is not seeded
    assert runtime_paths._resolved_roots is not real_original
    assert id(seeded) == seeded_id
    assert seeded == {"data": Path("/seed-data-must-not-mutate")}

    target = uploads / "probe.bin"
    target.write_bytes(b"x")
    assert files.download_path(str(target)) == target.resolve()
    # Seeded mapping stays pristine after helper use.
    assert seeded == {"data": Path("/seed-data-must-not-mutate")}
    # Live original was never the test subject and remains byte-identical.
    assert id(real_original) == real_original_id
    assert dict(real_original) == real_snapshot
