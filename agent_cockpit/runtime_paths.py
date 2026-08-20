"""runtime_paths — 唯一的运行时路径解析器(Wiki13 J1B1,R2 修订)。

设计合同(#1927 J1B 审计 + #1933 R2 修订):
- 纯解析:本模块不做 mkdir/写文件/DDL;任何创建只发生在消费方明确的
  写路径(save/upload/connect/ensure_dirs)。
- 四个根:data(~/dashboard-data)、config(~/.config/agent-cockpit)、
  state(~/.local/state/agent-cockpit)、uploads(~/dashboard-uploads),
  与 J0 默认路径逐字节兼容;env 覆盖仅 COCKPIT_DATA_DIR/
  COCKPIT_CONFIG_DIR/COCKPIT_STATE_DIR/COCKPIT_UPLOADS_DIR 与既有的
  COCKPIT_COORDINATION_DB。
- 原子 fail-closed(R2-A):非空显式 override 一旦非法(相对路径/NUL/
  越界 symlink/bundle/过宽根/根重叠/store 碰撞)直接抛
  PathResolutionError,绝不回落默认、绝不静默换库;仅 unset/全空回默认。
  错误信息只含 reason 与 env 名,不回显完整敏感路径。
- 全根集合校验(R2-B):最终四根无论 default/custom 整体校验——拒绝
  /、HOME、install root 及其祖先/内部,任意双向嵌套/相等。
- COCKPIT_COORDINATION_DB(R2-C):同样过 bundle+过宽根+全命名 store
  碰撞门;不得等于 tasks/push 等 store 路径或任一根,外置路径可用但
  必须安全。
- inspect()(R2-D):纯读检查根与 store 的真实写语义——现存文件 store
  要求父目录可写可执行(原子替换/WAL/key 创建);敏感 store 按声明
  mode 拒绝 0644 等;wrong owner、group/world 不安全、类型错位均
  not-ready;首装 creatable 仍纯读。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from .artifact_root import resolve_artifact_root


class PathResolutionError(RuntimeError):
    """路径校验失败;reason 机器可读。detail 只含 env 名,不含敏感路径。"""

    def __init__(self, reason: str, env_name: str):
        super().__init__(f"{reason}: {env_name}")
        self.reason = reason
        self.env_name = env_name


# 应用 bundle/generation 根:持久路径禁止落在其中或其祖先。
INSTALL_ROOT = resolve_artifact_root()
_HOME_ROOT = Path.home().resolve()

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

# store 名 → (根, 相对路径, 类型 file|dir, writer, 声明 mode 或 None)
# 声明 mode:敏感 store 必须精确 0600,多余位(如 0644)判 not-ready。
STORES: dict[str, tuple[str, str, str, str, int | None]] = {
    "settings": ("data", "settings.json", "file", "server", 0o600),
    "tasks": ("data", "tasks.sqlite3", "file", "server", None),
    "project_registry": (
        "data", "project-registry.sqlite3", "file", "server", 0o600,
    ),
    "workspace_work": (
        "data", "workspace-work.sqlite3", "file", "server", 0o600,
    ),
    "workspace_execution": (
        "data", "workspace-execution.sqlite3", "file", "server", 0o600,
    ),
    "runtime_provider": (
        "data", "runtime-provider.sqlite3", "file", "server", 0o600,
    ),
    "event_journal": (
        "data", "event-journal.sqlite3", "file", "server", 0o600,
    ),
    "operation_journal": (
        "data", "operation-journal.sqlite3", "file", "server", 0o600,
    ),
    "project_memory": (
        "data", "project-memory.sqlite3", "file", "server", 0o600,
    ),
    "terminal_ticket": (
        "data", "terminal-ticket.sqlite3", "file", "server", 0o600,
    ),
    "worktrees": ("data", "worktrees", "dir", "server", None),
    "coordination": ("data", "coordination.sqlite3", "file", "server+tools", None),
    "leader_binding": ("data", "leader-binding.sqlite3", "file", "server", None),
    "push": ("data", "push.sqlite3", "file", "server", None),
    "delivery_outbox": ("data", "delivery-outbox.sqlite3", "file", "server", 0o600),
    "vapid": ("data", "vapid-private.pem", "file", "server", 0o600),
    "mail_projects": ("data", "mail-projects.json", "file", "server", 0o600),
    "chat_workspaces": ("data", "chat-workspaces.json", "file", "server", 0o600),
    "chat_threads": ("data", "chat-threads.json", "file", "server", 0o600),
    "chat_messages": ("data", "chat-messages.json", "file", "server", 0o600),
    "team_messages": ("data", "team-messages.json", "file", "server", 0o600),
    "team_sessions": ("data", "team-sessions.json", "file", "server", 0o600),
    "inbox_route": ("data", "team-inbox-route.json", "file", "server", 0o600),
    "upgrade": ("data", "upgrade", "dir", "server", None),
    "typing": ("state", "typing.json", "file", "server", None),
    "file_roots": ("config", "file-roots.json", "file", "server", 0o600),
}

_lock = threading.Lock()
_resolved_roots: dict[str, Path] = {}


def canonicalize(raw: str, *, env_name: str) -> Path:
    """唯一 canonical 点:expanduser→绝对路径校验→resolve。任何一步失败
    抛 PathResolutionError(机器可读 reason,detail 只含 env 名)。"""
    if not isinstance(raw, str) or "\x00" in raw:
        raise PathResolutionError("nul_or_invalid", env_name)
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise PathResolutionError("relative_path", env_name)
    p = Path(expanded)
    resolved = p.resolve(strict=False)
    if resolved != Path(os.path.normpath(expanded)):
        # 越界 symlink:词法路径与真实路径不一致(含链接重定向)
        raise PathResolutionError("symlink_escape", env_name)
    return resolved


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _path_has_symlink(p: Path) -> bool:
    """任一组件是链接即 True(#1940:逐组件检查,不只 final)。"""
    cur = Path(p.anchor)
    for part in p.parts[1:]:
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


def _check_broad(p: Path, env_name: str) -> None:
    """过宽根/危险位置拒绝(R2-B):/、HOME、install root 及其祖先/内部。"""
    if p == Path("/") or p == _HOME_ROOT:
        raise PathResolutionError("broad_root", env_name)
    if p == INSTALL_ROOT or _is_within(p, INSTALL_ROOT):
        raise PathResolutionError("bundle_path", env_name)
    if _is_within(INSTALL_ROOT, p):
        # install root 的祖先:bundle 整体落在持久根内,升级即数据混写
        raise PathResolutionError("broad_root", env_name)


def _resolve_roots_locked() -> dict[str, Path]:
    """一次性解析并整体校验四根;任何非法直接抛错(不部分缓存)。"""
    resolved: dict[str, Path] = {}
    for name in _ROOTS:
        env_name = _ROOT_ENV[name]
        default_lex = _ROOT_DEFAULTS[name]
        default = default_lex.resolve(strict=False)
        raw = os.environ.get(env_name, "").strip()
        if raw:
            p = canonicalize(raw, env_name=env_name)  # 非法即抛
            _check_broad(p, env_name)
            lex = Path(os.path.expanduser(raw))
        else:
            p = default
            _check_broad(p, env_name)  # default 同样过宽根门
            lex = default_lex
        if _path_has_symlink(lex):
            # 词法路径任一组件是链接即拒绝:不被 resolve 静默重锚到外部
            raise PathResolutionError("symlink_escape", env_name)
        resolved[name] = p
    for i, a in enumerate(_ROOTS):
        for b in _ROOTS[i + 1:]:
            pa, pb = resolved[a], resolved[b]
            if pa == pb or _is_within(pa, pb) or _is_within(pb, pa):
                raise PathResolutionError(
                    "root_nested", f"{_ROOT_ENV[a]}|{_ROOT_ENV[b]}",
                )
    return resolved


def _roots() -> dict[str, Path]:
    with _lock:
        if not _resolved_roots:
            _resolved_roots.update(_resolve_roots_locked())
        return dict(_resolved_roots)


def reset_cache() -> None:
    """测试/env 变更后重建解析缓存。"""
    with _lock:
        _resolved_roots.clear()


def data_root() -> Path:
    return _roots()["data"]


def config_root() -> Path:
    return _roots()["config"]


def state_root() -> Path:
    return _roots()["state"]


def uploads_root() -> Path:
    return _roots()["uploads"]


def store(name: str) -> Path:
    """命名 store 的 canonical 路径。coordination 的 COCKPIT_COORDINATION_DB
    覆盖过与根同级的 fail-closed 门(R2-C):bundle/过宽根/全命名 store
    碰撞;不得等于任一 store 路径或根。"""
    if name not in STORES:
        raise KeyError(f"未知 store: {name}")
    if name == "coordination":
        raw = os.environ.get(ENV_COORDINATION_DB, "").strip()
        if raw:
            p = canonicalize(raw, env_name=ENV_COORDINATION_DB)  # 非法即抛
            _check_broad(p, ENV_COORDINATION_DB)
            if _path_has_symlink(p):
                raise PathResolutionError("symlink_escape", ENV_COORDINATION_DB)
            roots = _roots()
            if p in roots.values():
                raise PathResolutionError("store_collision", ENV_COORDINATION_DB)
            for other in STORES:
                if other == "coordination":
                    continue
                root_name, rel, kind = STORES[other][0], STORES[other][1], STORES[other][2]
                sp = roots[root_name] / rel
                # R3-A:等于任一 store 路径,或落在目录型 store 内部,均为碰撞
                if p == sp or (kind == "dir" and _is_within(p, sp)):
                    raise PathResolutionError(
                        "store_collision", ENV_COORDINATION_DB,
                    )
            return p
    root_name, rel = STORES[name][0], STORES[name][1]
    return _roots()[root_name] / rel


def diagnostics() -> list[dict[str, str]]:
    """R2 起 override 非法直接抛错,不再静默回落,故无累积诊断;保留 API。"""
    return []


def _nearest_existing_ancestor(p: Path) -> Path:
    cur = p.parent if not p.exists() else p
    for ancestor in (cur, *cur.parents):
        if ancestor.exists():
            return ancestor
    return Path("/").resolve()


def _store_escape_reason(p: Path, root: Path) -> str | None:
    """R3-B+#1940 纯读:app-owned store 不得经 final/intermediate symlink
    逃出 canonical assigned root。路径中任何一层存在链接(realpath 与词
    法路径不一致)即 fail-closed——无论目标在 root 内外;无链接时对 root
    内路径逐级复核组件。外部 store(coordination 覆盖)无链接即安全。
    返回机器可读 reason 或 None。"""
    real = Path(os.path.realpath(p))
    if real != p:
        return "symlink_escape"
    try:
        rel_parts = p.relative_to(root).parts
    except ValueError:
        return None  # root 外且无链接:外置 store,安全
    cur = root
    for part in rel_parts:
        cur = cur / part
        if cur.is_symlink():
            return "symlink_escape"
    return None


def validate_store(name: str) -> Path:
    """写前守卫(R3-B):writer/DDL 在 mkdir/connect/写文件前必须调用;
    store 路径存在 symlink 逃逸即抛 PathResolutionError(只含 reason 与
    store 名,不泄露真实路径)。"""
    if name not in STORES:
        raise KeyError(f"未知 store: {name}")
    p = store(name)
    root = _roots()[STORES[name][0]]
    reason = _store_escape_reason(p, root)
    if reason:
        raise PathResolutionError(reason, f"store:{name}")
    return p


def _dir_safe(st: os.stat_result, uid: int) -> str | None:
    """目录写语义检查:owner + group/world 不安全。返回 reason 或 None。"""
    if st.st_uid != uid and uid != 0:
        return "wrong_owner"
    if st.st_mode & 0o022:
        return "insecure_mode"
    return None


def _path_health(p: Path, kind: str, declared_mode: int | None, root: Path) -> tuple[bool, str]:
    """纯读判定(R2-D+R3-B):先查 symlink 逃逸(fail-closed);现存路径
    检查真实写语义——原子替换/WAL/key 创建要求父目录可写可执行;敏感
    store 按声明 mode 拒绝多余位;缺席路径看最近现存父目录是否可按
    当前 uid 安全创建。"""
    uid = os.getuid()
    if _store_escape_reason(p, root):
        return False, "symlink_escape"
    if p.exists():
        try:
            st = p.stat()
        except OSError:
            return False, "stat_failed"
        if kind == "file" and p.is_dir():
            return False, "type_mismatch_dir_not_file"
        if kind == "dir" and p.is_file():
            return False, "type_mismatch_file_not_dir"
        if st.st_uid != uid and uid != 0:
            return False, "wrong_owner"
        if st.st_mode & 0o022:
            return False, "insecure_mode"
        if declared_mode is not None and (st.st_mode & 0o777) & ~declared_mode:
            return False, "insecure_mode"
        need = os.R_OK | os.W_OK | (os.X_OK if kind == "dir" else 0)
        if not os.access(p, need):
            return False, "not_readable_writable"
        if kind == "file":
            # 原子替换/WAL/key 创建都要求父目录可写可执行
            parent = p.parent
            try:
                pst = parent.stat()
            except OSError:
                return False, "parent_stat_failed"
            reason = _dir_safe(pst, uid)
            if reason:
                return False, f"parent_{reason}"
            if not os.access(parent, os.W_OK | os.X_OK):
                return False, "parent_not_writable"
        return True, "ok"
    ancestor = _nearest_existing_ancestor(p)
    try:
        st = ancestor.stat()
    except OSError:
        return False, "parent_stat_failed"
    if not ancestor.is_dir():
        return False, "parent_not_dir"
    reason = _dir_safe(st, uid)
    if reason:
        return False, f"parent_{reason}"
    if not os.access(ancestor, os.W_OK | os.X_OK):
        return False, "parent_not_writable"
    return True, "creatable"


def inspect() -> dict[str, Any]:
    """纯读诊断快照(供后续 /health/ready):只 stat/access,不创建。"""
    roots = _roots()
    stores: list[dict[str, Any]] = []
    for name, (root_name, rel, kind, writer, declared_mode) in STORES.items():
        p = store(name)
        ready, reason = _path_health(p, kind, declared_mode, roots[root_name])
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
