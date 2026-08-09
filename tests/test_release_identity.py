"""test_release_identity.py — Wiki13 J1A R3 release identity + /health/live 测试。

R3 覆盖：ReleaseIdentityError allowlist + 任意异常脱敏 + 无 fork 平台可导入 + 真实 os.fork。
"""
from __future__ import annotations

import os
import sys
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


# ── A: 生产 SHA fail-closed ─────────────────────────────────────────────

def test_server_edition_missing_sha_fail_closed(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    with pytest.raises(release_identity.ReleaseIdentityError, match="missing_source_sha"):
        release_identity.get_release_identity()


def test_desktop_edition_missing_sha_fail_closed(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "desktop")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    with pytest.raises(release_identity.ReleaseIdentityError, match="missing_source_sha"):
        release_identity.get_release_identity()


def test_invalid_edition_fail_closed(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "bogus")
    with pytest.raises(release_identity.ReleaseIdentityError, match="invalid_edition"):
        release_identity.get_release_identity()


def test_short_sha_rejected(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", "abc1234")
    with pytest.raises(release_identity.ReleaseIdentityError, match="malformed_source_sha"):
        release_identity.get_release_identity()


def test_64_bit_sha_rejected(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", "a" * 64)
    with pytest.raises(release_identity.ReleaseIdentityError, match="malformed_source_sha"):
        release_identity.get_release_identity()


def test_uppercase_sha_rejected(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", "A" * 40)
    with pytest.raises(release_identity.ReleaseIdentityError, match="malformed_source_sha"):
        release_identity.get_release_identity()


# ── R3 A: 任意异常脱敏 ─────────────────────────────────────────────────

def test_health_live_arbitrary_value_error_no_leak(monkeypatch):
    """patch get_release_identity 抛 ValueError("secret-MARKER/path") → 503，
    marker/path 均不出现。"""
    _reset()
    with patch.object(release_identity, "get_release_identity",
                      side_effect=ValueError("secret-MARKER/path")):
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        response = client.get("/health/live")
    assert response.status_code == 503
    body = response.text
    assert "secret" not in body.lower()
    assert "marker" not in body.lower()
    assert "path" not in body.lower()
    assert "release_identity_error" in body
    assert "unexpected" in body


def test_health_live_arbitrary_exception_no_leak(monkeypatch):
    """patch get_release_identity 抛 RuntimeError("evil/data") → 503，不泄露。"""
    _reset()
    with patch.object(release_identity, "get_release_identity",
                      side_effect=RuntimeError("evil/data")):
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        response = client.get("/health/live")
    assert response.status_code == 503
    assert "evil" not in response.text.lower()
    assert "data" not in response.text.lower()


def test_health_live_known_reason_precise(monkeypatch):
    """既有四个固定 reason 继续精确暴露。"""
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    response = client.get("/health/live")
    assert response.status_code == 503
    assert "missing_source_sha" in response.text


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
    assert "path" not in body.lower()
    assert "token" not in body.lower()


def test_health_live_malicious_sha_not_leaked(monkeypatch):
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "server")
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", "evil-secret-token")
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    response = client.get("/health/live")
    assert response.status_code == 503
    assert "evil" not in response.text
    assert "secret" not in response.text.lower()


def test_health_live_version_missing_503(monkeypatch):
    """缺失 VERSION → 503 stable（不是泛 500）。"""
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    with patch("release_identity.read_current_version", side_effect=FileNotFoundError):
        with patch.object(release_identity, "_cached", None):
            from fastapi.testclient import TestClient
            client = TestClient(server.app)
            response = client.get("/health/live")
    assert response.status_code == 503
    assert "version_unavailable" in response.text


# ── R3 B: 无 fork 平台可导入（真实子进程隔离）────────────────────────────

def test_module_imports_without_register_at_fork():
    """真实隔离：子进程删除 os.register_at_fork 后全新 import release_identity，
    断言退出 0 且 identity 可取。"""
    import subprocess
    import sys as _sys
    script = (
        "import os, sys\n"
        "if hasattr(os, 'register_at_fork'):\n"
        "    delattr(os, 'register_at_fork')\n"
        "if hasattr(os, 'fork'):\n"
        "    delattr(os, 'fork')\n"
        "sys.path.insert(0, '.')\n"
        "import release_identity\n"
        "release_identity.reset_cache()\n"
        "os.environ['COCKPIT_EDITION'] = 'source'\n"
        "os.environ.pop('COCKPIT_SOURCE_SHA', None)\n"
        "idn = release_identity.get_release_identity()\n"
        "assert idn['edition'] == 'source'\n"
        "assert idn['source_sha'] == 'unknown'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [_sys.executable, "-c", script],
        capture_output=True, text=True, timeout=10,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 0, (
        f"subprocess failed: {result.stderr}"
    )
    assert "OK" in result.stdout
    assert "Traceback" not in result.stderr


# ── R3 C: 真实 os.fork + pipe ───────────────────────────────────────────

def test_real_fork_child_different_identity(monkeypatch):
    """真实 os.fork + pipe 通信：child 有不同 instance_id + child pid。
    平台无 fork 时 skip。"""
    if not hasattr(os, "fork"):
        pytest.skip("os.fork not available on this platform")
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    parent_identity = release_identity.get_release_identity()
    parent_pid = os.getpid()

    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        # child
        os.close(r_fd)
        try:
            child_identity = release_identity.get_release_identity()
            data = f"{child_identity['instance_id']},{child_identity['pid']}".encode()
            os.write(w_fd, data)
        except Exception as e:
            os.write(w_fd, f"ERROR:{e}".encode())
        finally:
            os.close(w_fd)
            os._exit(0)
    else:
        # parent
        os.close(w_fd)
        data = os.read(r_fd, 4096).decode()
        os.close(r_fd)
        os.waitpid(pid, 0)

    if data.startswith("ERROR:"):
        pytest.fail(f"child failed: {data}")
    child_instance_id, child_pid_str = data.split(",")
    child_pid = int(child_pid_str)
    assert child_instance_id != parent_identity["instance_id"]
    assert child_pid != parent_pid


def test_pid_fallback_via_creator_pid(monkeypatch):
    """PID fallback 单测：_creator_pid 不匹配 → 重算（保留为单测）。"""
    _reset()
    monkeypatch.setenv("COCKPIT_EDITION", "source")
    monkeypatch.delenv("COCKPIT_SOURCE_SHA", raising=False)
    parent_identity = release_identity.get_release_identity()
    release_identity._creator_pid = -1  # 模拟 pid 变化
    child_identity = release_identity.get_release_identity()
    assert child_identity["instance_id"] != parent_identity["instance_id"]
    assert child_identity["pid"] == os.getpid()


# ── 无外部依赖 + identity 不泄露 ─────────────────────────────────────────

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
