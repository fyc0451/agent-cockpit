from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture
def web_roots(tmp_path: Path, monkeypatch):
    root = tmp_path / "root"
    legacy = root / "static"
    dist = root / "web" / "dist"
    assets = dist / "assets"
    legacy.mkdir(parents=True)
    assets.mkdir(parents=True)
    (legacy / "index.html").write_text("legacy-ui", encoding="ascii")
    (legacy / "sw.js").write_text("legacy-worker", encoding="ascii")
    (legacy / "manifest.webmanifest").write_text("{}", encoding="ascii")
    (dist / "index.html").write_text(
        '<div id="root"></div><script src="./assets/app-123.js"></script>',
        encoding="ascii",
    )
    (assets / "app-123.js").write_text("window.nextWeb = true", encoding="ascii")
    (assets / "app-123.css").write_text(":root { color: black; }", encoding="ascii")
    monkeypatch.setattr(server, "ROOT_DIR", root)
    monkeypatch.setattr(server, "STATIC_DIR", legacy)
    monkeypatch.setattr(server, "NEXT_WEB_DIR", dist)
    return root, legacy, dist


def _client() -> TestClient:
    return TestClient(server.app, client=("127.0.0.1", 50000))


def test_next_profile_serves_react_index_and_hashed_assets(web_roots, monkeypatch):
    monkeypatch.setattr(server.next_profile, "enabled", lambda *_: True)
    client = _client()

    index = client.get("/#/projects/demo/workspaces/local/files")
    assert index.status_code == 200
    assert '<div id="root">' in index.text
    assert "legacy-ui" not in index.text
    assert index.headers["cache-control"] == "no-cache"

    script = client.get("/assets/app-123.js")
    assert script.status_code == 200
    assert script.text == "window.nextWeb = true"
    assert "javascript" in script.headers["content-type"]
    assert script.headers["cache-control"] == "public, max-age=31536000, immutable"

    stylesheet = client.get("/assets/app-123.css")
    assert stylesheet.status_code == 200
    assert "text/css" in stylesheet.headers["content-type"]


def test_next_profile_fails_closed_when_build_is_missing(web_roots, monkeypatch):
    _, _, dist = web_roots
    monkeypatch.setattr(server.next_profile, "enabled", lambda *_: True)
    (dist / "index.html").unlink()

    response = _client().get("/")

    assert response.status_code == 503
    assert response.json() == {"detail": "next_web_build_unavailable"}
    assert "legacy-ui" not in response.text


def test_next_profile_rejects_symlinked_index(web_roots, monkeypatch):
    root, _, dist = web_roots
    monkeypatch.setattr(server.next_profile, "enabled", lambda *_: True)
    outside = root / "outside.html"
    outside.write_text("outside", encoding="ascii")
    (dist / "index.html").unlink()
    (dist / "index.html").symlink_to(outside)

    response = _client().get("/")

    assert response.status_code == 503
    assert "outside" not in response.text


def test_next_assets_reject_unknown_directory_traversal_and_symlink(
    web_roots, monkeypatch,
):
    root, _, dist = web_roots
    monkeypatch.setattr(server.next_profile, "enabled", lambda *_: True)
    outside = root / "secret.js"
    outside.write_text("secret", encoding="ascii")
    (dist / "assets" / "escape.js").symlink_to(outside)
    outside_dir = root / "outside-assets"
    outside_dir.mkdir()
    (outside_dir / "nested.js").write_text("secret", encoding="ascii")
    (dist / "assets" / "linked").symlink_to(outside_dir, target_is_directory=True)
    (dist / "assets" / "directory").mkdir()
    client = _client()

    for path in (
        "/assets/missing.js",
        "/assets/directory",
        "/assets/escape.js",
        "/assets/linked/nested.js",
        "/assets/%2e%2e/secret.js",
        "/assets/%2e%2e%2fsecret.js",
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code in {404, 405}, path
        assert "secret" not in response.text


def test_non_next_profile_keeps_legacy_root_and_hides_next_assets(
    web_roots, monkeypatch,
):
    monkeypatch.setattr(server.next_profile, "enabled", lambda *_: False)
    client = _client()

    index = client.get("/")
    assert index.status_code == 200
    assert index.content == b"legacy-ui"
    assert index.headers["cache-control"] == "no-cache"
    assert client.get("/assets/app-123.js").status_code == 404

    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert worker.content == b"legacy-worker"
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json() == {}


def test_next_web_routes_do_not_shadow_api_or_health(web_roots, monkeypatch):
    monkeypatch.setattr(server.next_profile, "enabled", lambda *_: True)
    client = _client()

    auth = client.get("/api/auth/status")
    assert auth.status_code == 200
    assert auth.headers["content-type"].startswith("application/json")
    live = client.get("/health/live")
    assert live.status_code in {200, 503}
    assert live.headers["content-type"].startswith("application/json")
    assert '<div id="root">' not in live.text
