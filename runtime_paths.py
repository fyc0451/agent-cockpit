"""runtime_paths — 唯一的运行时路径解析器(Wiki13 J1B1)。

设计合同(与 #1927 J1B 审计一致):
- 纯解析:本模块不做 mkdir/写文件/DDL;任何创建只发生在消费方明确的
  写路径(save/upload/connect/ensure_dirs)。
- 四个根:data(~/dashboard-data)、config(~/.config/agent-cockpit)、
  state(~/.local/state/agent-cockpit)、uploads(~/dashboard-uploads),
  与 J0 默认路径逐字节兼容;env 覆盖仅 COCKPIT_DATA_DIR/
  COCKPIT_CONFIG_DIR/COCKPIT_STATE_DIR/COCKPIT_UPLOADS_DIR 与既有的
  COCKPIT_COORDINATION_DB。
- fail-closed:env 覆盖为相对路径/含 NUL/越界 symlink/落在应用 bundle
  内/与其他根重叠时拒绝该覆盖并回落默认,诊断记入 diagnostics() 与
  inspect();绝不按进程 CWD 静默解析相对路径。
- inspect() 是纯读健康判定材料(供后续 /health/ready):现存路径
  owner/mode/可读写错误→不 ready;首装缺席→仅当最近现存父目录可按
  当前 uid/mode 安全创建才判 ready。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any


class PathResolutionError(RuntimeError):
    """路径校验失败;reason 为机器可读分类。"""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# 应用 bundle 根(本文件所在目录):持久路径禁止落在其中。
INSTALL_ROOT = Path(__file__).resolve().parent

ENV_DATA_DIR = "COCKPIT_DATA_DIR"
ENV_CONFIG_DIR = "COCKPIT_CONFIG_DIR"
ENV_STATE_DIR = "COCKPIT_STATE_DIR"
ENV_UPLOADS_DIR = "COCKPIT_UPLOADS_DIR"
ENV_COORDINATION_DB = "COCKPIT_COORDINATION_DB"

_ROOTS = ("data", "config", "state", "uploads")
_ROOT_ENV = {
    "data": ENV_DATA_DIR,
    "config": ENV_CONFIG_DIR,
    "state": ENV_STATE_DIR,
    "uploads": ENV_UPLOADS_DIR,
}
_ROOT_DEFAULTS: dict[str, Path] = {
    "data": Path.home() / "dashboard-data",
    "config": Path.home() / ".config" / "agent-cockpit",
    "state": Path.home() / ".local" / "state" / "agent-cockpit",
    "uploads": Path.home() / "dashboard-uploads",
}

# store 名 → (根, 相对路径, 类型 file|dir, writer)
STORES: dict[str, tuple[str, str, str, str]] = {
    "settings": ("data", "settings.json", "file", "server"),
    "tasks": ("data", "tasks.sqlite3", "file", "server"),
    "worktrees": ("data", "worktrees", "dir", "server"),
    "coordination": ("data", "coordination.sqlite3", "file", "server+tools"),
    "push": ("data", "push.sqlite3", "file", "server"),
    "vapid": ("data", "vapid-private.pem", "file", "server"),
    "mail_projects": ("data", "mail-projects.json", "file", "server"),
    "team_sessions": ("data", "team-sessions.json", "file", "server"),
    "inbox_route": ("data", "team-inbox-route.json", "file", "server"),
    "upgrade": ("data", "upgrade", "dir", "server"),
    "typing": ("state", "typing.json", "file", "server"),
    "file_roots": ("config", "file-roots.json", "file", "server"),
}

_lock = threading.Lock()
_diagnostics: list[dict[str, str]] = []
_resolved_roots: dict[str, Path] = {}


def _record(reason: str, detail: str) -> None:
    _diagnostics.append({"reason": reason, "detail": detail})


def canonicalize(raw: str, *, env_name: str) -> Path:
    """唯一 canonical 点:expanduser→绝对路径校验→resolve。任何一步失败
    抛 PathResolutionError(机器可读 reason)。"""
    if not isinstance(raw, str) or "\x00" in raw:
        raise PathResolutionError("nul_or_invalid", f"{env_name} 含非法字符")
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise PathResolutionError(
            "relative_path", f"{env_name}={raw!r} 是相对路径,拒绝按 CWD 解析",
        )
    p = Path(expanded)
    resolved = p.resolve(strict=False)
    if resolved != Path(os.path.normpath(expanded)):
        # 越界 symlink:词法路径与真实路径不一致(含链接重定向)
        raise PathResolutionError(
            "symlink_escape", f"{env_name}={raw!r} 经 symlink 重定向",
        )
    return resolved


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_root(name: str) -> Path:
    default = _ROOT_DEFAULTS[name].resolve(strict=False)
    env_name = _ROOT_ENV[name]
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        p = canonicalize(raw, env_name=env_name)
    except PathResolutionError as exc:
        _record(exc.reason, f"{env_name}: {exc.detail};回落默认 {default}")
        return default
    if _is_within(p, INSTALL_ROOT) or p == INSTALL_ROOT:
        _record(
            "bundle_path",
            f"{env_name}={raw!r} 落在应用 bundle 内,拒绝;回落默认 {default}",
        )
        return default
    for other in _ROOTS:
        if other == name or other not in _resolved_roots:
            continue
        op = _resolved_roots[other]
        if p == op or _is_within(p, op) or _is_within(op, p):
            _record(
                "store_overlap",
                f"{env_name}={raw!r} 与根 {other}({op}) 重叠,拒绝;回落默认 {default}",
            )
            return default
    return p


def _roots() -> dict[str, Path]:
    with _lock:
        if not _resolved_roots:
            for name in _ROOTS:  # 固定顺序:data<config<state<uploads
                _resolved_roots[name] = _resolve_root(name)
        return dict(_resolved_roots)


def reset_cache() -> None:
    """测试/env 变更后重建解析缓存。"""
    with _lock:
        _resolved_roots.clear()
        _diagnostics.clear()


def data_root() -> Path:
    return _roots()["data"]


def config_root() -> Path:
    return _roots()["config"]


def state_root() -> Path:
    return _roots()["state"]


def uploads_root() -> Path:
    return _roots()["uploads"]


def store(name: str) -> Path:
    """命名 store 的 canonical 路径。coordination 保留既有
    COCKPIT_COORDINATION_DB 覆盖(同样过 fail-closed 校验)。"""
    if name not in STORES:
        raise KeyError(f"未知 store: {name}")
    if name == "coordination":
        raw = os.environ.get(ENV_COORDINATION_DB, "").strip()
        if raw:
            try:
                return canonicalize(raw, env_name=ENV_COORDINATION_DB)
            except PathResolutionError as exc:
                _record(
                    exc.reason,
                    f"{ENV_COORDINATION_DB}: {exc.detail};回落默认",
                )
    root_name, rel, _, _ = STORES[name]
    return _roots()[root_name] / rel


def diagnostics() -> list[dict[str, str]]:
    with _lock:
        return list(_diagnostics)


def _nearest_existing_ancestor(p: Path) -> Path:
    cur = p.parent if not p.exists() else p
    for ancestor in (cur, *cur.parents):
        if ancestor.exists():
            return ancestor
    return Path("/").resolve()


def _path_health(p: Path, kind: str) -> tuple[bool, str]:
    """纯读判定:现存路径 owner/mode/可读写;缺席路径看最近现存父目录
    是否可按当前 uid 安全创建。"""
    uid = os.getuid()
    if p.exists():
        try:
            st = p.stat()
        except OSError as exc:
            return False, f"stat_failed:{exc}"
        if kind == "file" and p.is_dir():
            return False, "type_mismatch_dir_not_file"
        if kind == "dir" and p.is_file():
            return False, "type_mismatch_file_not_dir"
        if st.st_uid != uid and uid != 0:
            return False, "wrong_owner"
        if st.st_mode & 0o002:
            return False, "world_writable"
        if not os.access(p, os.R_OK | os.W_OK):
            return False, "not_readable_writable"
        return True, "ok"
    ancestor = _nearest_existing_ancestor(p)
    try:
        st = ancestor.stat()
    except OSError as exc:
        return False, f"parent_stat_failed:{exc}"
    if not ancestor.is_dir():
        return False, "parent_not_dir"
    if st.st_uid != uid and uid != 0:
        return False, "parent_wrong_owner"
    if st.st_mode & 0o002:
        return False, "parent_world_writable"
    if not os.access(ancestor, os.W_OK | os.X_OK):
        return False, "parent_not_writable"
    return True, "creatable"


def inspect() -> dict[str, Any]:
    """纯读诊断快照(供后续 /health/ready):只 stat/access,不创建。"""
    roots = _roots()
    stores: list[dict[str, Any]] = []
    for name, (root_name, rel, kind, writer) in STORES.items():
        p = store(name)
        ready, reason = _path_health(p, kind)
        stores.append({
            "name": name, "path": str(p), "root": root_name, "rel": rel,
            "kind": kind, "writer": writer, "exists": p.exists(),
            "ready": ready, "reason": reason,
        })
    return {
        "roots": {name: str(p) for name, p in roots.items()},
        "stores": stores,
        "diagnostics": diagnostics(),
        "ready": all(s["ready"] for s in stores),
    }
