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
import re
import socket
import stat
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from . import next_profile
from . import settings

from agent_mail_commands.common import load_client_config, save_client_hub

HUB, TOKEN = load_client_config()
# Team Human API 可经独立 SSH 隧道/HTTPS 入口访问远端 Hub，避免把本地
# Agent Mail 协作流量一并切走；未配置时保持旧行为并复用 HUB。
TEAM_HUB_URL = os.environ.get("TEAM_HUB_URL", "").strip().rstrip("/")
HUMAN_AUTH_URL = os.environ.get("HUMAN_AUTH_URL", "http://127.0.0.1:8766").rstrip("/")
TEAM_ADMIN_ACCESS_TOKEN_FILE = os.environ.get(
    "TEAM_ADMIN_ACCESS_TOKEN_FILE", ""
).strip()
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


def status() -> dict[str, Any]:
    """轻量检查 Agent Mail Hub 是否可写，不发送 token 或 MCP 请求。"""
    if not TOKEN:
        return {
            "available": False,
            "reason": "Agent Mail Hub token 未配置",
            "hub": HUB,
        }
    parsed = urlsplit(HUB)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return {"available": False, "reason": "Agent Mail Hub 地址无效", "hub": HUB}
    try:
        with socket.create_connection((host, port), timeout=1):
            pass
    except OSError as exc:
        return {
            "available": False,
            "reason": f"Agent Mail Hub 不可连接: {exc}",
            "hub": HUB,
        }
    return {"available": True, "reason": None, "hub": HUB}


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
        resp = client.post(f"{HUB}/api/", json=payload, headers=headers)
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


def _team_hub_url() -> str:
    configured = settings.get().get("team_hub_url")
    return settings.normalize_service_url(
        configured or TEAM_HUB_URL or HUB, "Team Hub",
    )


def _human_auth_url() -> str:
    configured = settings.get().get("human_auth_url")
    return settings.normalize_service_url(
        configured or HUMAN_AUTH_URL or "http://127.0.0.1:8766", "Human issuer",
    )


def normalize_team_config(team_hub: str, human_auth: str) -> dict[str, str]:
    """校验设置页提交的团队端点，供落盘前一次性完成全部输入校验。"""
    return {
        "team_hub_url": settings.normalize_service_url(team_hub, "Team Hub"),
        "human_auth_url": settings.normalize_service_url(human_auth, "Human issuer"),
    }


def public_team_config() -> dict[str, str]:
    """返回可展示的 Team 端点；不包含 token、Cookie 或任何凭据。"""
    return {
        "team_hub": _team_hub_url(),
        "human_auth": _human_auth_url(),
    }


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
    base_url = _team_hub_url()
    try:
        with httpx.Client(timeout=30) as client:
            response = client.request(
                method,
                f"{base_url}{path}",
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


def _session_lead_capability_post(
    project_slug: str,
    path: str,
    payload: dict[str, Any],
    *,
    action: str,
) -> dict[str, Any]:
    """调用固定的 binding-scoped Team Hub capability 路径。"""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", project_slug):
        raise ValueError("project_slug 无效")
    if path not in {
        "status", "inbox/claim", "reply",
    } and not re.fullmatch(r"inbox/[1-9][0-9]*/complete", path):
        raise ValueError("Session lead capability 路径无效")
    if not isinstance(payload, dict):
        raise ValueError(f"{action}请求无效")
    base_url = _team_hub_url()
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{base_url}/hub/api/projects/{quote(project_slug, safe='')}/session-lead/{path}",
                json=payload,
                headers=_headers,
            )
    except httpx.HTTPError as exc:
        raise HumanAPIError(502, f"Hub {action}请求失败") from exc
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.is_error:
        # capability 错误不向本地 Agent 区分 token/binding 细节。
        detail = "Invalid reply credentials" if response.status_code == 403 else None
        raise HumanAPIError(
            response.status_code,
            str(detail or f"Hub {action}失败(HTTP {response.status_code})"),
        )
    if not isinstance(data, dict):
        raise HumanAPIError(502, f"Hub 返回了无效的{action}结果")
    return data


def session_lead_claim(
    project_slug: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """受限领取当前 binding 的一条 Team Inbox 消息。"""
    return _session_lead_capability_post(
        project_slug, "inbox/claim", payload, action="收件领取",
    )


def session_lead_status(
    project_slug: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """上报当前 binding 的最小本地运行状态。"""
    return _session_lead_capability_post(
        project_slug, "status", payload, action="状态心跳",
    )


def session_lead_complete(
    project_slug: str, inbox_item_id: int, payload: dict[str, Any],
) -> dict[str, Any]:
    """用 claim capability 完成一条 Team Inbox 消息。"""
    if isinstance(inbox_item_id, bool) or inbox_item_id < 1:
        raise ValueError("inbox_item_id 无效")
    return _session_lead_capability_post(
        project_slug,
        f"inbox/{inbox_item_id}/complete",
        payload,
        action="收件完成",
    )


def session_lead_reply(
    project_slug: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """回复一条已获消息级授权的 Team Topic 工作。"""
    return _session_lead_capability_post(
        project_slug, "reply", payload, action="回复",
    )


def claim_agent(
    *,
    authorization: str,
    project_slug: str,
    source_project_slug: str,
    agent_name: str,
    registration_token: str,
) -> dict[str, Any]:
    """用本地注册身份的 registration_token 向 Team Hub 认领 Agent。

    转发当前 Human JWT（authorization）；token 只随本次请求转发，不落盘；
    Cockpit 前端永不接触 token。目标项目用安全 project_slug（URL 路径），
    source_project_slug 标识身份所属项目（registry project_slug），
    不发送本机 project_key 路径。
    """
    if (
        not authorization.startswith("Bearer ")
        or not authorization[7:].strip()
        or len(authorization) > 8192
    ):
        raise ValueError("需要有效的 Hub Human JWT")
    for label, value in (
        ("project_slug", project_slug),
        ("source_project_slug", source_project_slug),
    ):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
            raise ValueError(f"{label} 无效")
    if not isinstance(registration_token, str) or not registration_token:
        raise ValueError("registration_token 无效")
    if len(registration_token) > 4096:
        raise ValueError("registration_token 无效")
    if not isinstance(agent_name, str) or not 1 <= len(agent_name) <= 128:
        raise ValueError("身份名无效")
    headers = {
        **_headers,
        "Authorization": authorization,
    }
    base_url = _team_hub_url()
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{base_url}/hub/api/projects/{quote(project_slug, safe='')}/agent-claims",
                json={
                    "source_project_slug": source_project_slug,
                    "agent_name": agent_name,
                    "registration_token": registration_token,
                },
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
    if not isinstance(data, dict):
        raise HumanAPIError(502, "Hub 返回了无效的 JSON")
    return data


def human_login(username: str, password: str) -> dict[str, Any]:
    """向独立 issuer 登录；凭据只随本次请求转发，不保存。"""
    if not 1 <= len(username) <= 64 or not 1 <= len(password) <= 256:
        raise ValueError("用户名或密码长度无效")
    base_url = _human_auth_url()
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(
                f"{base_url}/token",
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


def _human_auth_api(
    method: str,
    path: str,
    *,
    authorization: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_url = _human_auth_url()
    if authorization and (
        not authorization.startswith("Bearer ")
        or not authorization[7:].strip()
        or len(authorization) > 8192
    ):
        raise ValueError("需要有效的 Human JWT")
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    if path.startswith("/admin/") and TEAM_ADMIN_ACCESS_TOKEN_FILE:
        token_path = Path(TEAM_ADMIN_ACCESS_TOKEN_FILE).expanduser()
        try:
            info = token_path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("团队管理员二次密钥文件必须是权限 0600 的普通文件")
            raw = token_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"无法读取团队管理员二次密钥文件: {exc}") from exc
        if not 32 <= len(raw.strip()) <= 512:
            raise ValueError("团队管理员二次密钥长度必须为 32–512 字节")
        try:
            headers["X-Agent-Hub-Admin-Key"] = raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("团队管理员二次密钥必须是 UTF-8 文本") from exc
    try:
        with httpx.Client(timeout=10) as client:
            response = client.request(
                method,
                f"{base_url}{path}",
                json=payload if method != "GET" else None,
                headers=headers,
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
    if not isinstance(data, dict):
        raise HumanAuthError(502, "Human issuer 返回了无效响应")
    return data


def human_profile(authorization: str) -> dict[str, Any]:
    return _human_auth_api("GET", "/me", authorization=authorization)


def human_change_password(authorization: str, new_password: str) -> dict[str, Any]:
    if not 1 <= len(new_password) <= 256:
        raise ValueError("新密码长度无效")
    return _human_auth_api(
        "PATCH",
        "/me/password",
        authorization=authorization,
        payload={"new_password": new_password},
    )


def human_register(
    username: str,
    display_name: str,
    password: str,
    invite_code: str,
) -> dict[str, Any]:
    return _human_auth_api(
        "POST",
        "/register",
        payload={
            "username": username,
            "display_name": display_name,
            "password": password,
            "invite_code": invite_code,
        },
    )


def human_create_invitation(
    authorization: str,
    expires_in: int,
    project_slug: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"expires_in": expires_in}
    if project_slug is not None:
        payload["project_slug"] = project_slug
    return _human_auth_api(
        "POST",
        "/admin/invitations",
        authorization=authorization,
        payload=payload,
    )


def human_list_users(authorization: str) -> dict[str, Any]:
    return _human_auth_api("GET", "/admin/users", authorization=authorization)


def human_set_user_status(
    authorization: str, username: str, status: str
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", username):
        raise ValueError("用户名格式无效")
    return _human_auth_api(
        "PATCH",
        f"/admin/users/{username}",
        authorization=authorization,
        payload={"status": status},
    )


def _ensure_init() -> None:
    global _initialized
    if not _initialized:
        _call("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "dashboard", "version": "1.0"},
        })
        _initialized = True


def _hub_tool_error(result: Any) -> str | None:
    if isinstance(result, str):
        text = result.strip()
        if text.lower().startswith("error") or "not found" in text.lower():
            return text
        return None
    if isinstance(result, dict):
        err = result.get("error")
        if isinstance(err, str) and err.strip():
            return err.strip()
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return None


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
    next_profile.require_project(project_key)
    _ensure_init()
    result = _tool("send_message", {
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
    error = _hub_tool_error(result)
    if error:
        raise RuntimeError(error)
    return result


def ensure_project(human_key: str) -> Any:
    """在 Hub 登记 canonical 项目路径，得到 slug，供 Overseer 发信。"""
    next_profile.require_project(human_key)
    if not TOKEN:
        raise RuntimeError("Agent Mail token 未配置")
    _ensure_init()
    result = _tool("ensure_project", {"human_key": human_key})
    error = _hub_tool_error(result)
    if error:
        raise RuntimeError(error)
    return result


def overseer_send(
    *,
    project: str,
    recipients: list[str],
    subject: str,
    body_md: str,
    thread_id: str | None = None,
) -> Any:
    """Human Overseer 发信：不需要 adjective+noun 的 human agent。"""
    if not TOKEN:
        raise RuntimeError("Agent Mail token 未配置")
    if not project or not recipients or not subject or not body_md:
        raise RuntimeError("Overseer 发信参数不完整")
    payload: dict[str, Any] = {
        "recipients": recipients,
        "subject": subject,
        "body_md": body_md,
    }
    if thread_id:
        payload["thread_id"] = thread_id
    ident = quote(project, safe="")
    headers = {**_headers, "Authorization": f"Bearer {TOKEN}"}
    with httpx.Client(timeout=30) as client:
        resp = client.post(f"{HUB}/mail/{ident}/overseer/send", json=payload, headers=headers)
        if resp.status_code >= 400:
            detail = resp.text.strip()[:300] or f"HTTP {resp.status_code}"
            raise RuntimeError(f"Overseer 发信失败: {detail}")
    if not resp.content:
        return {"ok": True}
    try:
        return resp.json()
    except ValueError:
        return {"ok": True, "raw": resp.text[:200]}


def acknowledge_message(
    *,
    project_key: str,
    agent_name: str,
    registration_token: str,
    message_id: int,
) -> Any:
    next_profile.require_project(project_key)
    _ensure_init()
    return _tool("acknowledge_message", {
        "project_key": project_key,
        "agent_name": agent_name,
        "registration_token": registration_token,
        "message_id": message_id,
    })
