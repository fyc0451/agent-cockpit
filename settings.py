"""settings.py — dashboard 用户配置(存储 ~/dashboard-data/settings.json)。

配置项:
  language       UI 语言(zh/en/ja),前端 localStorage 可覆盖
  dir_agents     每个目录的默认 agent({目录: agent})
  enabled_agents 启用的 agent 类型(启动入口只列这些)
  upload_max_mb  上传单文件上限(MB)
  team_hub_url   Team Hub 业务 API 地址
  human_auth_url Human issuer 地址
  term           终端参数(max_terms/idle_ttl/write_timeout)

(可访问目录由 files.py 的 custom roots 机制管理,见 /api/files/roots,不在此重复。)

读取即时生效(各接入点每次调用时读);写入原子(mkstemp+fsync+replace)。
"""
from __future__ import annotations

import ipaddress
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import runtime_paths

DATA_DIR = runtime_paths.data_root()
SETTINGS_PATH = runtime_paths.store("settings")

# 已知 agent 类型(与 herdr_client._find_agent_bin / _agent_cmd 对齐)
KNOWN_AGENTS = ["codex", "kimi", "claude", "qodercli", "grok", "opencode"]
LANGUAGES = ["zh", "en", "ja"]

DEFAULTS: dict[str, Any] = {
    "language": "zh",
    "dir_agents": {},
    "enabled_agents": list(KNOWN_AGENTS),
    "upload_max_mb": 100,
    "team_hub_url": "",
    "human_auth_url": "",
    "term": {"max_terms": 16, "idle_ttl": 1800, "write_timeout": 2.0},
}

_lock = threading.Lock()
# 读取缓存:文件 mtime 没变就直接用,避免每次 files._resolve 都读盘
_cache: dict[str, Any] | None = None
_cache_mtime: float = -1.0

_PRIVATE_HTTP_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_PRIVATE_HTTP_V6 = ipaddress.ip_network("fc00::/7")


def _deepcopy_defaults() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULTS))


def normalize_service_url(value: Any, label: str, *, allow_empty: bool = False) -> str:
    """规范化服务端点；HTTP 仅允许本机或显式的 RFC1918/ULA 私网地址。"""
    if not isinstance(value, str):
        raise ValueError(f"{label} 地址必须是字符串")
    endpoint = value.strip().rstrip("/")
    if not endpoint:
        if allow_empty:
            return ""
        raise ValueError(f"{label} 地址不能为空")
    if any(char.isspace() for char in endpoint):
        raise ValueError(f"{label} 地址不能包含空白字符")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} 地址无效: {exc}") from exc
    host = parsed.hostname or ""
    try:
        address = ipaddress.ip_address(host)
        private_http = address.is_loopback or (
            address.version == 4
            and any(address in network for network in _PRIVATE_HTTP_V4)
        ) or (
            address.version == 6 and address in _PRIVATE_HTTP_V6
        )
    except ValueError:
        private_http = host.lower() == "localhost"
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
        or (parsed.scheme == "http" and not private_http)
    ):
        raise ValueError(f"{label} 地址必须是本机/私网 HTTP 或 HTTPS")
    return endpoint


def _read_merged() -> dict[str, Any]:
    """读文件并合并默认值。不持锁:调用方需自行保证并发安全(get 在锁外
    拼装后统一写缓存,update 在整个 RMW 持锁)。"""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (ValueError, OSError):
        data = {}
    merged = _deepcopy_defaults()
    for k, v in data.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k].update(v)
        elif k in merged:
            merged[k] = v
    return merged


def get() -> dict[str, Any]:
    """读配置(缺文件/损坏返回默认值;带 mtime 缓存)。"""
    global _cache, _cache_mtime
    try:
        mtime = SETTINGS_PATH.stat().st_mtime
    except OSError:
        with _lock:
            _cache, _cache_mtime = None, -1.0
        return _deepcopy_defaults()
    with _lock:
        if _cache is not None and mtime == _cache_mtime:
            return json.loads(json.dumps(_cache))
    merged = _read_merged()
    with _lock:
        _cache, _cache_mtime = json.loads(json.dumps(merged)), mtime
    return merged


def _validate(cfg: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化配置,非法抛 ValueError(消息带具体原因)。"""
    lang = cfg.get("language")
    if lang not in LANGUAGES:
        raise ValueError(f"language 必须是 {LANGUAGES} 之一: {lang!r}")

    dir_agents = cfg.get("dir_agents")
    if not isinstance(dir_agents, dict):
        raise ValueError("dir_agents 必须是 {目录: agent} 映射")
    clean_da: dict[str, str] = {}
    for d, a in dir_agents.items():
        p = Path(str(d)).expanduser()
        if not p.is_absolute():
            raise ValueError(f"dir_agents 的目录必须是绝对路径: {d}")
        if a not in KNOWN_AGENTS:
            raise ValueError(f"dir_agents 的 agent 未知: {a!r}(目录 {d})")
        clean_da[str(p)] = a
    cfg["dir_agents"] = clean_da

    agents = cfg.get("enabled_agents")
    if not isinstance(agents, list) or not agents:
        raise ValueError("enabled_agents 必须是非空列表")
    unknown = [a for a in agents if a not in KNOWN_AGENTS]
    if unknown:
        raise ValueError(f"未知 agent 类型: {unknown}(可选: {KNOWN_AGENTS})")
    # 去重保序
    cfg["enabled_agents"] = list(dict.fromkeys(agents))

    try:
        mb = int(cfg.get("upload_max_mb"))
    except (TypeError, ValueError):
        raise ValueError(f"upload_max_mb 必须是整数: {cfg.get('upload_max_mb')!r}")
    cfg["upload_max_mb"] = max(1, min(mb, 2048))

    cfg["team_hub_url"] = normalize_service_url(
        cfg.get("team_hub_url"), "Team Hub", allow_empty=True,
    )
    cfg["human_auth_url"] = normalize_service_url(
        cfg.get("human_auth_url"), "Human issuer", allow_empty=True,
    )

    term = cfg.get("term")
    if not isinstance(term, dict):
        raise ValueError("term 必须是对象")
    t = dict(DEFAULTS["term"])
    t.update(term)
    try:
        t["max_terms"] = max(1, min(int(t["max_terms"]), 64))
        t["idle_ttl"] = max(60.0, min(float(t["idle_ttl"]), 86400.0))
        t["write_timeout"] = max(0.2, min(float(t["write_timeout"]), 30.0))
    except (TypeError, ValueError):
        raise ValueError("term 参数必须是数字")
    cfg["term"] = t
    return cfg


def update(partial: dict[str, Any]) -> dict[str, Any]:
    """合并一层更新并落盘,返回生效配置。非法抛 ValueError。

    并发安全:读-改-写全程持 _lock,避免并发 update 基于同一份旧值互相覆盖
    丢失更新;落盘前用 _validate 规范化后的值(而非原始输入),保证文件内容
    与生效配置一致(如 clamp 后的上限、去重后的 agent 列表)。
    """
    global _cache, _cache_mtime
    if not isinstance(partial, dict):
        raise ValueError("请求体必须是 JSON 对象")
    with _lock:
        cfg = _read_merged()
        # 稀疏存储:文件只保留显式设置过的键(并入已有 raw),默认值不落盘,
        # 保持"未显式设置=用模块默认常量"的 live 读取语义。
        raw = _raw()
        for k, v in partial.items():
            if k not in DEFAULTS:
                raise ValueError(f"未知配置项: {k}")
            if isinstance(DEFAULTS[k], dict):
                if not isinstance(v, dict):
                    raise ValueError(f"配置项 {k} 必须是对象")
                merged = dict(cfg.get(k) or {})
                merged.update(v)
                cfg[k] = merged
            else:
                cfg[k] = v
        cfg = _validate(cfg)
        # 用验证/规范化后的生效值回填落盘内容:clamp、去重、路径规范化等
        # 都体现在 cfg 上,不能把原始输入直接写进文件。
        for k, v in partial.items():
            if k == "term":
                stored = raw.get(k) if isinstance(raw.get(k), dict) else {}
                raw[k] = {**stored, **{name: cfg[k][name] for name in v}}
            else:
                raw[k] = cfg[k]
        runtime_paths.validate_store("settings")  # R3-B:symlink 逃逸 fail-closed
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=str(DATA_DIR))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SETTINGS_PATH)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # 写后同步缓存,使 get() 的 mtime 缓存命中的是本次生效值
        _cache, _cache_mtime = json.loads(json.dumps(cfg)), SETTINGS_PATH.stat().st_mtime
        return cfg


# ── 各模块的 live 读取入口 ────────────────────────────────────
# 语义:只有用户在设置文件里显式写过的值才覆盖;否则返回调用方给的默认
# (即模块常量,保持可 monkeypatch,也不让 DEFAULTS 喧宾夺主)。

def _raw() -> dict[str, Any]:
    """读设置文件原文(不含默认值合并);缺文件/损坏返回 {}。"""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def upload_max_bytes(default_bytes: int) -> int:
    mb = _raw().get("upload_max_mb")
    try:
        return int(mb) * 1024 * 1024 if mb else default_bytes
    except (TypeError, ValueError):
        return default_bytes


def term_setting(key: str, default: float) -> float:
    term = _raw().get("term")
    if isinstance(term, dict) and term.get(key) is not None:
        return term[key]
    return default
