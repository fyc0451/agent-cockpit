"""hub_client.py — 通过 MCP JSON-RPC 调用 hub 的写操作。

读操作走 db.py 直读 SQLite;写操作(发消息/ack)走这里调 hub,
保证事务一致性和 audit log。hub 地址取 ~/.agent-mail/client.env 的 hub
字段(统一由 agent-mail-tools/am_common.load_client_config 解析),
未配置时默认 127.0.0.1:8765(个人模式)。
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
from typing import Any
from urllib.parse import urlsplit

import httpx

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-mail-tools")
)
from am_common import load_client_config, save_client_hub as save_client_hub  # noqa: E402

HUB, TOKEN = load_client_config()
HUMAN_AUTH_URL = os.environ.get("HUMAN_AUTH_URL", "http://127.0.0.1:8766").rstrip("/")
_headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_id_counter = [0]


class HumanAPIError(RuntimeError):
    """Hub Human API 返回的可安全透传错误。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class HumanAuthError(RuntimeError):
    """独立 Human issuer 返回的安全错误。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def status() -> dict[str, Any]:
    """轻量检查 Agent Mail Hub 是否可写，不发送 token 或 MCP 请求。"""
    if not TOKEN:
        return {"available": False, "reason": "Agent Mail Hub token 未配置"}
    parsed = urlsplit(HUB)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return {"available": False, "reason": "Agent Mail Hub 地址无效"}
    try:
        with socket.create_connection((host, port), timeout=1):
            pass
    except OSError as exc:
        return {"available": False, "reason": f"Agent Mail Hub 不可连接: {exc}"}
    return {"available": True, "reason": None}


def _next_id() -> int:
    _id_counter[0] += 1
    return _id_counter[0]


def _response_data(raw: str) -> str:
    """从 SSE 响应取第一条事件的完整 data；普通 JSON 原样返回。"""
    parts: list[str] = []
    saw_data = False
    for line in raw.splitlines():
        if not line:
            if parts:
                break
            continue
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            parts.append(value)
            saw_data = True
    return "\n".join(parts) if saw_data else raw


def _call(method: str, params: dict[str, Any]) -> Any:
    """发起一次 JSON-RPC 调用,解析 SSE 响应返回 result。"""
    headers = {**_headers, "Authorization": f"Bearer {TOKEN}"}
    payload = {"jsonrpc": "2.0", "id": _next_id(), "method": method, "params": params}
    with httpx.Client(timeout=30) as client:
        resp = client.post(f"{HUB}/mcp/", json=payload, headers=headers)
        resp.raise_for_status()
    data = json.loads(_response_data(resp.text))
    if "error" in data:
        raise RuntimeError(f"hub MCP error: {data['error']}")
    return data.get("result")


def _tool(name: str, arguments: dict[str, Any]) -> Any:
    """调用一个 MCP 工具,提取 text content 里的 JSON。"""
    result = _call("tools/call", {"name": name, "arguments": arguments})
    content = result.get("content", []) if isinstance(result, dict) else []
    text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return text


# 首次调用前 initialize(模块级,进程启动时做一次即可,但 MCP 是无状态的,每次连接都要 init)
_initialized = False


def reload_config() -> dict[str, Any]:
    """重读 client.env，让后续 Hub 调用无需重启即可使用新地址。"""
    global HUB, TOKEN, _initialized
    HUB, TOKEN = load_client_config()
    _initialized = False
    return {"hub": HUB, "token_configured": bool(TOKEN)}


def allows_local_actions() -> bool:
    """仅本机 Hub 可参与本地终端通知；共享 Hub 响应一律视为只读数据。"""
    host = (urlsplit(HUB).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def human_api(
    method: str,
    path: str,
    authorization: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    """调用固定 Hub Human API；JWT 仅随本次请求转发，不保存到磁盘。"""
    if (
        not authorization.startswith("Bearer ")
        or not authorization[7:].strip()
        or len(authorization) > 8192
    ):
        raise ValueError("需要有效的 Hub Human JWT")
    if not path.startswith("/hub/api/"):
        raise ValueError("Hub Human API 路径无效")
    headers = {
        **_headers,
        "Authorization": authorization,
    }
    try:
        with httpx.Client(timeout=30) as client:
            response = client.request(
                method,
                f"{HUB}{path}",
                json=payload if method != "GET" else None,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise HumanAPIError(502, f"Hub 请求失败: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.is_error:
        detail = data.get("detail") if isinstance(data, dict) else None
        raise HumanAPIError(
            response.status_code,
            str(detail or f"Hub 返回 HTTP {response.status_code}"),
        )
    if not isinstance(data, (dict, list)):
        raise HumanAPIError(502, "Hub 返回了无效的 JSON")
    return data


def human_login(username: str, password: str) -> dict[str, Any]:
    """向独立 issuer 登录；凭据只随本次请求转发，不保存。"""
    if not 1 <= len(username) <= 64 or not 1 <= len(password) <= 256:
        raise ValueError("用户名或密码长度无效")
    parsed = urlsplit(HUMAN_AUTH_URL)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and not _is_loopback_host(parsed.hostname))
    ):
        raise ValueError("Human issuer 地址必须是本机 HTTP 或 HTTPS")
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(
                f"{HUMAN_AUTH_URL}/token",
                json={"username": username, "password": password},
            )
    except httpx.HTTPError as exc:
        raise HumanAuthError(502, f"Human issuer 请求失败: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.is_error:
        detail = data.get("detail") if isinstance(data, dict) else None
        raise HumanAuthError(
            response.status_code,
            str(detail or f"Human issuer 返回 HTTP {response.status_code}"),
        )
    token = data.get("access_token") if isinstance(data, dict) else None
    profile = data.get("profile") if isinstance(data, dict) else None
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 8192
        or not isinstance(profile, dict)
        or not isinstance(profile.get("display_name"), str)
        or not profile["display_name"].strip()
    ):
        raise HumanAuthError(502, "Human issuer 返回了无效响应")
    return data


def _ensure_init() -> None:
    global _initialized
    if not _initialized:
        _call("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "dashboard", "version": "1.0"},
        })
        _initialized = True


def send_message(
    *,
    project_key: str,
    sender_name: str,
    sender_token: str,
    to: list[str],
    subject: str,
    body_md: str = "",
    thread_id: str | None = None,
    importance: str = "normal",
    ack_required: bool = False,
) -> Any:
    _ensure_init()
    return _tool("send_message", {
        "project_key": project_key,
        "sender_name": sender_name,
        "sender_token": sender_token,
        "to": to,
        "subject": subject,
        "body_md": body_md,
        "thread_id": thread_id,
        "importance": importance,
        "ack_required": ack_required,
        "auto_contact_if_blocked": True,
    })


def acknowledge_message(
    *,
    project_key: str,
    agent_name: str,
    registration_token: str,
    message_id: int,
) -> Any:
    _ensure_init()
    return _tool("acknowledge_message", {
        "project_key": project_key,
        "agent_name": agent_name,
        "registration_token": registration_token,
        "message_id": message_id,
    })
