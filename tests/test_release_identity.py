"""test_release_identity.py — Wiki13 J1A release identity + /health/live 测试。

覆盖：身份在同进程稳定、pid 准确、非法 edition/畸形 sha fail-closed、
无外部依赖调用、source 模式 fallback、/health/live 端点。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import release_identity
import server


def _reset():
    release_identity.reset_cache()


def test_identity_stable_within_process(monkeypatch):
    """同一进程内多次调用返回相同 identity（除 pid 外全部稳定）。"""
    _reset()
    a = release_identity.get_release_identity()
    b = release_identity.get_release_identity()
    assert a == b
    assert a["instance_id"] == b["instance_id"]
    assert a["version"] == b["version"]
    assert a["edition"] == b["edition"]
    assert a["source_sha"] == b["source_sha"]
    assert a["pid"] == b["pid"] == os.getpid()


def test_pid_accurate(monkeypatch):
    _reset()
    identity = release_identity.get_release_identity()
    assert identity["pid"] == os.getpid()


def test_invalid_edition_fail_closed(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "bogus")
    with pytest.raises(ValueError, match="非法 edition"):
        release_identity.get_release_identity()


def test_malformed_source_sha_fail_closed(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", "not-a-sha!!")
    with pytest.raises(ValueError, match="source_sha 畸形"):
        release_identity.get_release_identity()


def test_source_mode_fallback(monkeypatch):
    """无 COCKPIT_SOURCE_SHA → "unknown"（source 模式合理 fallback）。"""
    _reset()
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    identity = release_identity.get_release_identity()
    assert identity["source_sha"] == "unknown"
    assert identity["edition"] == "source"


def test_server_edition_with_sha(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", "abc1234")
    identity = release_identity.get_release_identity()
    assert identity["edition"] == "server"
    assert identity["source_sha"] == "abc1234"


def test_no_external_dependency_calls(monkeypatch):
    """identity 计算不调用 git/httpx/herdr/hub/push。"""
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    with patch("subprocess.run") as mock_run, \
         patch("httpx.Client") as mock_httpx, \
         patch("herdr_client.is_available") as mock_herdr:
        identity = release_identity.get_release_identity()
        assert not mock_run.called
        assert not mock_httpx.called
        assert not mock_herdr.called
    assert identity["version"]  # version read from VERSION file


def test_health_live_endpoint(monkeypatch):
    """/health/live 返回 status=live + identity；无外部探测。"""
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "live"
    assert "identity" in body
    ident = body["identity"]
    assert ident["version"]
    assert ident["edition"] == "source"
    assert ident["source_sha"] == "unknown"
    assert ident["instance_id"]
    assert ident["pid"] == os.getpid()


def test_health_live_no_auth_required(monkeypatch):
    """/health/live 无需认证（在 no-auth allowlist 中）。"""
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    # 不提供任何 auth header
    response = client.get("/health/live")
    assert response.status_code == 200


def test_health_live_invalid_edition_503(monkeypatch):
    """非法 edition → 503 fail-closed。"""
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "bogus")
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    response = client.get("/health/live")
    assert response.status_code == 503


def test_identity_does_not_leak_paths_or_tokens(monkeypatch):
    """identity 不含 path/token/secret 字段。"""
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    identity = release_identity.get_release_identity()
    for key in identity:
        assert "path" not in key.lower()
        assert "token" not in key.lower()
        assert "secret" not in key.lower()
        assert "password" not in key.lower()
