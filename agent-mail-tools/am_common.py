"""am-register / mail-send / mail-recv 的共享函数。"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path


CLIENT_ENV = Path.home() / ".agent-mail" / "client.env"
REGISTRY_DIR = Path.home() / ".agent-mail" / "registry"


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


def load_identity(agent: str, instance: str, project: str) -> tuple[dict, str, str]:
    project_key = str(Path(project).resolve())
    registry_file = REGISTRY_DIR / slugify(project_key) / f"{agent}--{instance}.json"
    if not registry_file.is_file():
        raise SystemExit(
            f"身份未注册: {registry_file}\n"
            f"先运行: am-register --agent {agent} --instance {instance} --project {project_key}"
        )
    identity = json.loads(registry_file.read_text())
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
        f"{hub}/mcp/",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    return json.loads(raw)


def mcp_tool(hub: str, token: str, name: str, args: dict) -> dict | list:
    clean_args = {key: value for key, value in args.items() if value is not None}
    result = mcp_call(
        hub, token, "tools/call", {"name": name, "arguments": clean_args}
    )
    if "error" in result:
        raise SystemExit(f"MCP error: {result['error']}")
    text = "".join(
        item.get("text", "") for item in result.get("result", {}).get("content", [])
    )
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError:
        raise SystemExit(f"tool {name} failed: {text[:300]}")
