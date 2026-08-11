"""U1a：VERSION / SemVer / GitHub latest / 缓存单飞 / /api/version。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import server
import version


def _release(
    tag: str = "v0.2.0",
    *,
    name: str | None = None,
    url: str | None = None,
    published_at: str = "2026-08-01T00:00:00Z",
    draft: bool = False,
    prerelease: bool = False,
) -> dict:
    return {
        "tag_name": tag,
        "name": name if name is not None else tag,
        "html_url": url
        or f"https://github.com/fyc0451/agent-cockpit/releases/tag/{tag}",
        "published_at": published_at,
        "draft": draft,
        "prerelease": prerelease,
    }


@pytest.fixture(autouse=True)
def _clear_version_cache():
    version.clear_cache()
    yield
    version.clear_cache()


def test_read_current_version_from_file_without_git(tmp_path):
    """无 Git 目录时仍可读 VERSION 文件。"""
    path = tmp_path / "VERSION"
    path.write_text("0.2.0\n", encoding="utf-8")
    assert not (tmp_path / ".git").exists()
    assert version.read_current_version(path) == "0.2.0"


def test_repo_version_file_is_0_2_0():
    assert version.read_current_version() == "0.2.0"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.2.0", (0, 2, 0)),
        ("v0.2.0", (0, 2, 0)),
        ("v1.0.10", (1, 0, 10)),
        ("1.2", None),
        ("v1.2.3-beta", None),
        ("10.0.0.1", None),
        ("", None),
        ("latest", None),
    ],
)
def test_parse_semver(raw, expected):
    assert version.parse_semver(raw) == expected


def test_compare_semver_major_minor_patch():
    assert version.compare_semver((0, 2, 0), (0, 2, 0)) == 0
    assert version.compare_semver((0, 2, 1), (0, 2, 0)) == 1
    assert version.compare_semver((0, 1, 9), (0, 2, 0)) == -1
    assert version.compare_semver((1, 0, 0), (0, 9, 9)) == 1


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://github.com/fyc0451/agent-cockpit/releases/tag/v0.2.0", True),
        ("https://github.com/fyc0451/agent-cockpit/releases/latest", True),
        ("http://github.com/fyc0451/agent-cockpit/releases/tag/v0.2.0", False),
        ("https://evil.com/fyc0451/agent-cockpit/releases/tag/v0.2.0", False),
        ("https://github.com/other/agent-cockpit/releases/tag/v0.2.0", False),
        ("https://github.com/fyc0451/agent-cockpit/releases/../tags/x", False),
    ],
)
def test_release_url_allowlist(url, ok):
    assert version.is_allowed_release_url(url) is ok


def test_parse_release_same_newer_older(monkeypatch, tmp_path):
    ver = tmp_path / "VERSION"
    ver.write_text("0.2.0", encoding="utf-8")

    def apply(tag: str, expected_status: str, expected_version: str):
        version.clear_cache()
        payload = _release(tag)
        monkeypatch.setattr(
            version, "_http_get_latest",
            lambda: version._parse_release_payload(payload),
        )
        info = version.get_version_info(version_path=ver)
        assert info["current"]["version"] == "0.2.0"
        assert info["status"] == expected_status
        if expected_status == "unavailable":
            assert info["latest"] is None
        else:
            assert info["latest"]["version"] == expected_version
            assert info["latest"]["url"] == payload["html_url"]

    apply("v0.2.0", "up_to_date", "0.2.0")
    apply("0.2.0", "up_to_date", "0.2.0")
    apply("v0.3.0", "update_available", "0.3.0")
    apply("agent-cockpit-v0.3.0", "update_available", "0.3.0")
    apply("agent-cockpit-v0.1.9", "up_to_date", "0.1.9")


def test_invalid_tag_json_url_yields_unavailable(monkeypatch, tmp_path):
    ver = tmp_path / "VERSION"
    ver.write_text("0.2.0", encoding="utf-8")

    cases = [
        _release("not-a-semver"),
        _release("other-product-v0.3.0"),
        _release("agent-cockpit-vv0.3.0"),
        _release("agent-cockpit-v 0.3.0"),
        _release(
            "v0.3.0",
            url="https://evil.example/releases/tag/v0.3.0",
        ),
        {"not": "a release"},
        None,
    ]
    for payload in cases:
        version.clear_cache()
        monkeypatch.setattr(
            version, "_http_get_latest",
            lambda p=payload: version._parse_release_payload(p)
            if isinstance(p, dict)
            else None,
        )
        info = version.get_version_info(version_path=ver)
        assert info["status"] == "unavailable"
        assert info["latest"] is None
        assert info["checked_at"].endswith("Z")


def test_draft_and_prerelease_rejected(monkeypatch, tmp_path):
    ver = tmp_path / "VERSION"
    ver.write_text("0.2.0", encoding="utf-8")
    for payload in (
        _release("v0.9.0", draft=True),
        _release("v0.9.0", prerelease=True),
    ):
        version.clear_cache()
        monkeypatch.setattr(
            version, "_http_get_latest",
            lambda p=payload: version._parse_release_payload(p),
        )
        assert version.get_version_info(version_path=ver)["status"] == "unavailable"


def test_http_timeout_and_rate_limit_unavailable(monkeypatch, tmp_path):
    ver = tmp_path / "VERSION"
    ver.write_text("0.2.0", encoding="utf-8")

    class BoomClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(version.httpx, "Client", BoomClient)
    info = version.get_version_info(version_path=ver, refresh=True)
    assert info["status"] == "unavailable"
    assert info["latest"] is None

    version.clear_cache()

    class RateLimitClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return httpx.Response(403, json={"message": "rate limit"})

    monkeypatch.setattr(version.httpx, "Client", RateLimitClient)
    info = version.get_version_info(version_path=ver, refresh=True)
    assert info["status"] == "unavailable"
    # 不透传 github body
    assert "rate limit" not in json.dumps(info)


def test_bad_json_unavailable(monkeypatch, tmp_path):
    ver = tmp_path / "VERSION"
    ver.write_text("0.2.0", encoding="utf-8")

    class BadJsonClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return httpx.Response(200, text="not-json{")

    monkeypatch.setattr(version.httpx, "Client", BadJsonClient)
    info = version.get_version_info(version_path=ver, refresh=True)
    assert info["status"] == "unavailable"


def test_cache_hit_and_refresh_bypass(monkeypatch, tmp_path):
    ver = tmp_path / "VERSION"
    ver.write_text("0.2.0", encoding="utf-8")
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return version._parse_release_payload(_release("v0.2.0"))

    monkeypatch.setattr(version, "_http_get_latest", fetch)
    version.get_version_info(version_path=ver, refresh=True)
    version.get_version_info(version_path=ver, refresh=False)
    version.get_version_info(version_path=ver, refresh=False)
    assert calls["n"] == 1
    version.get_version_info(version_path=ver, refresh=True)
    assert calls["n"] == 2


def test_concurrent_refresh_single_flight(monkeypatch, tmp_path):
    ver = tmp_path / "VERSION"
    ver.write_text("0.2.0", encoding="utf-8")
    calls = {"n": 0}
    entered = threading.Event()
    release = threading.Event()

    def slow_fetch():
        calls["n"] += 1
        entered.set()
        assert release.wait(timeout=3)
        return version._parse_release_payload(_release("v0.3.0"))

    monkeypatch.setattr(version, "_http_get_latest", slow_fetch)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(version.get_version_info(version_path=ver, refresh=True))
        except BaseException as exc:  # noqa: BLE001 — 收集测试线程异常
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    assert entered.wait(timeout=2)
    # 其余线程应堵在 refresh_lock 上；放行后单飞合并
    time.sleep(0.05)
    release.set()
    for t in threads:
        t.join(timeout=3)
    assert not errors
    assert calls["n"] == 1
    assert len(results) == 5
    assert all(r["status"] == "update_available" for r in results)


def test_api_version_requires_auth(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    assert client.get("/api/version").status_code == 401


def test_api_version_ok_and_unavailable_degrade(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    ver = tmp_path / "VERSION"
    ver.write_text("0.2.0", encoding="utf-8")
    monkeypatch.setattr(version, "VERSION_PATH", ver)
    monkeypatch.setattr(
        version, "_http_get_latest",
        lambda: version._parse_release_payload(_release("v0.4.0")),
    )
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}
    response = client.get("/api/version", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["current"] == {"version": "0.2.0"}
    assert body["status"] == "update_available"
    assert body["latest"]["version"] == "0.4.0"
    assert body["latest"]["url"].startswith(
        "https://github.com/fyc0451/agent-cockpit/releases/"
    )
    assert "checked_at" in body

    version.clear_cache()
    monkeypatch.setattr(version, "_http_get_latest", lambda: None)
    response = client.get("/api/version?refresh=true", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["latest"] is None
