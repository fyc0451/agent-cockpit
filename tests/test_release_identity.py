"""test_release_identity.py — Wiki13 J1A R2 release identity + /health/live 测试。

R2 覆盖：生产 SHA fail-closed、公开错误不泄露、fork 身份、barrier。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import release_identity
import server

_VALID_SHA = "a" * 40  # 40 位小写 hex


def _reset():
    release_identity.reset_cache()


def test_identity_stable_within_process(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    a = release_identity.get_release_identity()
    b = release_identity.get_release_identity()
    assert a == b
    assert a["pid"] == b["pid"] == os.getpid()


def test_pid_accurate(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    identity = release_identity.get_release_identity()
    assert identity["pid"] == os.getpid()


def test_source_unknown_fallback(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    identity = release_identity.get_release_identity()
    assert identity["source_sha"] == "unknown"
    assert identity["edition"] == "source"


def test_server_edition_with_valid_sha(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", _VALID_SHA)
    identity = release_identity.get_release_identity()
    assert identity["edition"] == "server"
    assert identity["source_sha"] == _VALID_SHA


# ── R2 A: 生产 SHA fail-closed ──────────────────────────────────────────

def test_server_edition_missing_sha_fail_closed(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    with pytest.raises(ValueError, match="missing_source_sha"):
        release_identity.get_release_identity()


def test_desktop_edition_missing_sha_fail_closed(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "desktop")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    with pytest.raises(ValueError, match="missing_source_sha"):
        release_identity.get_release_identity()


def test_invalid_edition_fail_closed(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "bogus")
    with pytest.raises(ValueError, match="invalid_edition"):
        release_identity.get_release_identity()


def test_short_sha_rejected(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", "abc1234")
    with pytest.raises(ValueError, match="malformed_source_sha"):
        release_identity.get_release_identity()


def test_64_bit_sha_rejected(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", "a" * 64)
    with pytest.raises(ValueError, match="malformed_source_sha"):
        release_identity.get_release_identity()


def test_uppercase_sha_rejected(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", "A" * 40)
    with pytest.raises(ValueError, match="malformed_source_sha"):
        release_identity.get_release_identity()


# ── R2 B: 公开错误不泄露 ───────────────────────────────────────────────

def test_health_live_no_raw_env_in_error(monkeypatch):
    """恶意 edition 字符串不出现在 503 响应中。"""
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "evil/path/../token")
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    response = client.get("/health/live")
    assert response.status_code == 503
    body = response.text
    assert "evil" not in body
    assert "path" not in body.lower() or "release_identity_error" in body
    assert "token" not in body.lower() or "release_identity_error" in body


def test_health_live_malicious_sha_not_leaked(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", "evil-secret-token")
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    response = client.get("/health/live")
    assert response.status_code == 503
    assert "evil" not in response.text
    assert "secret" not in response.text.lower() or "release_identity_error" in response.text


def test_health_live_version_missing_503(monkeypatch, tmp_path):
    """缺失 VERSION → 503 stable（不是泛 500）。"""
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    monkeypatch.setattr(release_identity, "reset_cache", lambda: None)
    with patch("release_identity.read_current_version", side_effect=FileNotFoundError("VERSION not found")):
        with patch.object(release_identity, "_cached", None):
            from fastapi.testclient import TestClient
            client = TestClient(server.app)
            response = client.get("/health/live")
    assert response.status_code == 503


# ── R2 C: fork 身份 ─────────────────────────────────────────────────────

def test_fork_child_gets_new_instance_id_and_pid(monkeypatch):
    """os.fork 后 child 首次调用有不同 instance_id 和 child pid。"""
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    parent_identity = release_identity.get_release_identity()
    # 模拟 fork child: 直接修改 pid + 调用（register_at_fork 在实际 fork 时触发）
    # 使用 _creator_pid 不匹配检测
    release_identity._creator_pid = -1  # 模拟 pid 变化
    child_identity = release_identity.get_release_identity()
    assert child_identity["instance_id"] != parent_identity["instance_id"]
    assert child_identity["pid"] == os.getpid()


# ── R2 D: 无外部依赖 + identity 不泄露 ───────────────────────────────────

def test_no_external_dependency_calls(monkeypatch):
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


def test_identity_no_secret_fields(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    identity = release_identity.get_release_identity()
    for key in identity:
        assert "path" not in key.lower()
        assert "token" not in key.lower()
        assert "secret" not in key.lower()
        assert "password" not in key.lower()


# ── 并发首次调用单 identity ──────────────────────────────────────────────

def test_concurrent_first_call_single_identity(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    barrier = threading.Barrier(4)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        barrier.wait()
        try:
            results.append(release_identity.get_release_identity())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(errors) == 0
    assert len(results) == 4
    # 所有 instance_id 相同（单次计算）
    ids = {r["instance_id"] for r in results}
    assert len(ids) == 1


# ── /health/live 端点 ────────────────────────────────────────────────────

def test_health_live_200(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "live"
    assert body["identity"]["edition"] == "source"
    assert body["identity"]["source_sha"] == "unknown"


def test_health_live_no_auth(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    response = client.get("/health/live")
    assert response.status_code == 200


def test_health_live_invalid_edition_503(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "bogus")
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    response = client.get("/health/live")
    assert response.status_code == 503
