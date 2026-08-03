"""settings.py — dashboard 用户配置(存储 ~/dashboard-data/settings.json)。

配置项:
  language       UI 语言(zh/en/ja),前端 localStorage 可覆盖
  dir_agents     每个目录的默认 agent({目录: agent})
  enabled_agents 启用的 agent 类型(启动入口只列这些)
  upload_max_mb  上传单文件上限(MB)
  term           终端参数(max_terms/idle_ttl/write_timeout)

(可访问目录由 files.py 的 custom roots 机制管理,见 /api/files/roots,不在此重复。)

读取即时生效(各接入点每次调用时读);写入原子(mkstemp+fsync+replace)。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

DATA_DIR = Path.home() / "dashboard-data"
SETTINGS_PATH = DATA_DIR / "settings.json"

# 已知 agent 类型(与 herdr_client._find_agent_bin / _agent_cmd 对齐)
KNOWN_AGENTS = ["codex", "kimi", "claude", "qodercli", "grok", "opencode"]
LANGUAGES = ["zh", "en", "ja"]

DEFAULTS: dict[str, Any] = {
    "language": "zh",
    "dir_agents": {},
    "enabled_agents": list(KNOWN_AGENTS),
    "upload_max_mb": 100,
    "term": {"max_terms": 16, "idle_ttl": 1800, "write_timeout": 2.0},
}

_lock = threading.Lock()
# 读取缓存:文件 mtime 没变就直接用,避免每次 files._resolve 都读盘
_cache: dict[str, Any] | None = None
_cache_mtime: float = -1.0


def _deepcopy_defaults() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULTS))


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
    """合并一层更新并落盘,返回生效配置。非法抛 ValueError。"""
    if not isinstance(partial, dict):
        raise ValueError("请求体必须是 JSON 对象")
    cfg = get()
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
            raw[k] = merged
        else:
            cfg[k] = v
            raw[k] = v
    cfg = _validate(cfg)
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
