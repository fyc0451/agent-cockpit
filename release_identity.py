"""release_identity.py — 冻结 machine-verifiable release identity (Wiki13 J1A R2)。

version/source_sha/edition/instance_id/pid 在进程首次请求时计算并缓存。
请求路径不执行 git，不泄露 path/token/exception text。非法 edition 或
畸形 build metadata → fail-closed（ValueError），/health/live 返回 503 + stable reason。

R2 修复：
- edition=server/desktop 缺 COCKPIT_SOURCE_SHA → fail-closed（仅 source 允许 unknown）
- source_sha 仅接受 40 位小写 git SHA（拒短 SHA/64 位/大写/空值）
- fork：os.register_at_fork child 重置 instance_id + pid（不依赖调用者 refresh）
- 错误消息使用 stable reason code，不回显 raw env/VERSION/exception
"""
from __future__ import annotations

import os
import re
import uuid
from typing import Any

from version import read_current_version

_VALID_EDITIONS = frozenset({"server", "desktop", "source"})
# R2: 仅接受 40 位小写 git SHA
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_instance_id: str = uuid.uuid4().hex
_cached: dict[str, Any] | None = None
_creator_pid: int = os.getpid()


def _compute_identity() -> dict[str, Any]:
    edition = os.environ.get("COCKPIT_EDITION", "source")
    if edition not in _VALID_EDITIONS:
        raise ValueError("invalid_edition")
    source_sha = os.environ.get("COCKPIT_SOURCE_SHA", "")
    if edition in ("server", "desktop"):
        if not source_sha:
            raise ValueError("missing_source_sha")
        if not _GIT_SHA_RE.fullmatch(source_sha):
            raise ValueError("malformed_source_sha")
    else:  # source edition
        if source_sha:
            if not _GIT_SHA_RE.fullmatch(source_sha):
                raise ValueError("malformed_source_sha")
        else:
            source_sha = "unknown"
    try:
        version = read_current_version()
    except Exception:
        raise ValueError("version_unavailable")
    return {
        "version": version,
        "source_sha": source_sha,
        "edition": edition,
        "instance_id": _instance_id,
        "pid": os.getpid(),
    }


def get_release_identity() -> dict[str, Any]:
    """返回冻结的 release identity；首次调用计算并缓存（同进程稳定）。
    fork 后 child 首次调用自动检测 creator_pid 不符 → 重置 instance_id + 重算。"""
    global _cached, _instance_id, _creator_pid
    if _cached is not None and os.getpid() == _creator_pid:
        return dict(_cached)
    if _cached is not None and os.getpid() != _creator_pid:
        # fork child: 重置 instance_id + 重算
        _instance_id = uuid.uuid4().hex
        _creator_pid = os.getpid()
        _cached = None
    if _cached is None:
        _cached = _compute_identity()
        _creator_pid = os.getpid()
    return dict(_cached)


def refresh_pid() -> None:
    """测试用：手动更新 pid。"""
    global _cached, _creator_pid
    if _cached is not None:
        _cached["pid"] = os.getpid()
    _creator_pid = os.getpid()


def reset_cache() -> None:
    """测试用：清空缓存，下次 get_release_identity 重新计算。"""
    global _cached, _instance_id, _creator_pid
    _cached = None
    _instance_id = uuid.uuid4().hex
    _creator_pid = os.getpid()


# R2: fork child 自动重置 identity（不依赖调用者 refresh）
os.register_at_fork(
    after_in_child=lambda: (
        globals().__setitem__("_instance_id", uuid.uuid4().hex),
        globals().__setitem__("_cached", None),
        globals().__setitem__("_creator_pid", os.getpid()),
    )
)
