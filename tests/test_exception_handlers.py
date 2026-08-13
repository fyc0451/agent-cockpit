"""O2：全局异常处理与通用 500 文案。"""
from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient

import server


def _client() -> TestClient:
    # 按真实 500 路径：不要把服务端异常冒泡成客户端未捕获异常
    return TestClient(
        server.app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
        raise_server_exceptions=False,
    )


def _mount(path: str, handler):
    """注册一次性测试路由（endpoint 已绑定，不能 monkeypatch 模块函数名）。"""
    server.app.add_api_route(path, handler, methods=["GET"])


def test_uncaught_runtime_error_returns_generic_500(caplog):
    """未捕获 RuntimeError → 500 + 稳定文案，响应不泄漏异常/路径。"""
    leak = "secret-token=/home/fyc/.secrets/token RuntimeError-path"
    path = f"/__o2_test_uncaught_{uuid.uuid4().hex}"

    def boom():
        raise RuntimeError(leak)

    _mount(path, boom)

    with caplog.at_level(logging.ERROR, logger="agent-cockpit"):
        response = _client().get(path)

    assert response.status_code == 500
    body = response.json()
    assert body == {"detail": server.INTERNAL_ERROR_DETAIL}
    text = response.text
    assert leak not in text
    assert "RuntimeError" not in text
    assert "/home/fyc" not in text
    assert "secret-token" not in text
    # 日志确有 exception 记录，且带 method/path，不含 query/body 敏感字段
    records = [r for r in caplog.records if r.name == "agent-cockpit"]
    assert any(r.exc_info for r in records)
    joined = " ".join(r.getMessage() for r in records)
    assert "method=GET" in joined
    assert f"path={path}" in joined
    assert "Authorization" not in joined
    assert "Cookie" not in joined


def test_explicit_http_exception_500_sanitized():
    """显式 HTTPException(500, detail=泄漏) 同样换成通用文案。"""
    leak = "发送失败: /var/lib/agent.db token=abc123"
    path = f"/__o2_test_http500_{uuid.uuid4().hex}"

    def boom():
        raise HTTPException(500, leak)

    _mount(path, boom)
    response = _client().get(path)

    assert response.status_code == 500
    assert response.json() == {"detail": server.INTERNAL_ERROR_DETAIL}
    assert leak not in response.text
    assert "token=abc123" not in response.text
    assert "/var/lib" not in response.text


def test_http_exception_400_detail_unchanged():
    """4xx 语义保持：detail 与 status 不被通用 500 改写。"""
    detail = "session 名仅允许字母、数字、下划线和连字符"
    path = f"/__o2_test_http400_{uuid.uuid4().hex}"

    def boom():
        raise HTTPException(400, detail)

    _mount(path, boom)
    response = _client().get(path)

    assert response.status_code == 400
    assert response.json() == {"detail": detail}


def test_http_exception_502_detail_unchanged():
    """502 上游/降级语义保持原 detail。"""
    detail = "Hub 不可达: ConnectError"
    path = f"/__o2_test_http502_{uuid.uuid4().hex}"

    def boom():
        raise HTTPException(502, detail)

    _mount(path, boom)
    response = _client().get(path)

    assert response.status_code == 502
    assert response.json() == {"detail": detail}
