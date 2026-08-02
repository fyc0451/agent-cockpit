"""hub_client.py — 通过 MCP JSON-RPC 调用本机 hub 的写操作。

读操作走 db.py 直读 SQLite;写操作(发消息/ack)走这里调 hub,
保证事务一致性和 audit log。hub 在 127.0.0.1:8765。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

# 复用 agent-mail-tools 的 client.env 取 hub/token
_CLIENT_ENV = Path.home() / ".agent-mail" / "client.env"


def _load_config() -> tuple[str, str]:
    hub, token = "http://127.0.0.1:8765", ""
    if _CLIENT_ENV.is_file():
        for line in _CLIENT_ENV.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key == "hub":
                    # client.env 里的 hub 可能是 tailscale 地址,本地优先用 127.0.0.1
                    pass
                elif key == "token":
                    token = value
    return "http://127.0.0.1:8765", token


HUB, TOKEN = _load_config()
_headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_id_counter = [0]


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
