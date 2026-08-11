"""Agent Mail helper commands 的共享函数。"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from artifact_root import resolve_artifact_root


CLIENT_ENV = Path.home() / ".agent-mail" / "client.env"
REGISTRY_DIR = Path.home() / ".agent-mail" / "registry"


def helper_command(name: str) -> str:
    source = resolve_artifact_root() / "agent-mail-tools" / name
    if source.is_file():
        return str(source)
    return shutil.which(name) or name


def slugify(project_key: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", project_key).strip("-").lower() or "root"


def load_client_config() -> tuple[str, str]:
    hub, token = "http://127.0.0.1:8765", ""
    if CLIENT_ENV.is_file():
        for line in CLIENT_ENV.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                if key.strip() == "hub":
                    if value.strip():
                        hub = value.strip()
                elif key.strip() == "token":
                    token = value.strip()
    return hub.rstrip("/"), token


def normalize_hub_url(value: str) -> str:
    """校验并规范化 Hub 基础地址；凭据必须继续放在 token 配置中。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Hub 地址不能为空")
    hub = value.strip()
    if any(char.isspace() for char in hub):
        raise ValueError("Hub 地址不能包含空白字符")
    try:
        parsed = urlsplit(hub)
        port = parsed.port  # 触发非法端口校验
    except ValueError as exc:
        raise ValueError(f"Hub 地址无效: {exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Hub 地址只允许 http 或 https")
    if not parsed.hostname:
        raise ValueError("Hub 地址必须包含主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Hub 地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("Hub 地址不能包含 query 或 fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Hub 端口必须在 1-65535 之间")
    return hub.rstrip("/")


def save_client_hub(value: str) -> str:
    """原子更新 client.env 的 hub 行，保留 token 与未来扩展配置。"""
    hub = normalize_hub_url(value)
    try:
        original = CLIENT_ENV.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""

    lines = original.splitlines(keepends=True)
    found = False
    updated: list[str] = []
    for line in lines:
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        if "=" in body and not body.strip().startswith("#"):
            key, _, _ = body.partition("=")
            if key.strip() == "hub":
                updated.append(f"hub={hub}{ending or chr(10)}")
                found = True
                continue
        updated.append(line)
    content = "".join(updated)
    if not found:
        if content and not content.endswith(("\n", "\r")):
            content += "\n"
        content += f"hub={hub}\n"

    CLIENT_ENV.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".client.env.", suffix=".tmp", dir=str(CLIENT_ENV.parent)
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, CLIENT_ENV)
        os.chmod(CLIENT_ENV, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return hub


def load_identity(agent: str, instance: str, project: str) -> tuple[dict, str, str]:
    project_key = str(Path(project).resolve())
    registry_file = REGISTRY_DIR / slugify(project_key) / f"{agent}--{instance}.json"
    if not registry_file.is_file():
        raise SystemExit(
            f"身份未注册: {registry_file}\n"
            f"先运行: am-register --agent {agent} --instance {instance} --project {project_key}"
        )
    try:
        identity = json.loads(registry_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"身份文件损坏或不可读: {registry_file}: {exc}") from exc
    if not isinstance(identity, dict):
        raise SystemExit(f"身份文件格式错误: {registry_file}")
    if identity.get("project_key") != project_key:
        raise SystemExit(
            f"身份项目不匹配: registry={identity.get('project_key')!r}, request={project_key!r}"
        )
    hub, token = load_client_config()
    if not token:
        raise SystemExit(f"缺少 token，请配置 {CLIENT_ENV}")
    return identity, hub, token


def mcp_call(hub: str, token: str, method: str, params: dict) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(
        f"{hub}/api/",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError) as exc:
        raise SystemExit(f"MCP 请求失败({method}): {exc}") from exc

    payloads: list[str] = []
    current: list[str] = []
    is_sse = False
    for line in raw.splitlines():
        if line.startswith("data:"):
            is_sse = True
            current.append(line[5:].lstrip())
        elif not line and current:
            payloads.append("\n".join(current))
            current = []
    if current:
        payloads.append("\n".join(current))
    if not is_sse:
        payloads = [raw]

    for payload in payloads:
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise SystemExit(f"MCP 响应解析失败({method})")


def mcp_tool(hub: str, token: str, name: str, args: dict) -> dict | list:
    clean_args = {key: value for key, value in args.items() if value is not None}
    result = mcp_call(
        hub, token, "tools/call", {"name": name, "arguments": clean_args}
    )
    if not isinstance(result, dict):
        raise SystemExit(f"tool {name} 返回格式错误")
    if "error" in result:
        raise SystemExit(f"MCP error: {result['error']}")
    result_body = result.get("result")
    content = result_body.get("content", []) if isinstance(result_body, dict) else []
    text = "".join(
        item.get("text", "") for item in content if isinstance(item, dict)
    )
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError:
        raise SystemExit(f"tool {name} failed: {text[:300]}")
