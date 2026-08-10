"""release_identity.py — 冻结 machine-verifiable release identity (Wiki13 J1A R3)。

version/source_sha/edition/instance_id/pid 在进程首次请求时计算并缓存。
请求路径不执行 git，不泄露 path/token/exception text。非法 edition 或
畸形 build metadata → fail-closed（ReleaseIdentityError），/health/live 返回 503 + stable reason。

R3 修复：
- 引入 ReleaseIdentityError(reason 枚举 allowlist)，不拼接任意 ValueError 正文；
  未知 ValueError/Exception 一律 unexpected，marker/path 不出现在响应。
- os.register_at_fork 只在 API 存在时注册（无 fork 平台 import 成功）；
  creator_pid 检测继续作为通用 fallback。
"""
from __future__ import annotations

import os
import re
import uuid
from typing import Any

from version import read_current_version

_VALID_EDITIONS = frozenset({"server", "desktop", "source"})
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# R3: 受控 reason allowlist——/health/live 只暴露这些固定 reason
_ALLOWED_REASONS = frozenset({
    "invalid_edition", "missing_source_sha", "malformed_source_sha",
    "version_unavailable", "unexpected",
})
_instance_id: str = uuid.uuid4().hex
_cached: dict[str, Any] | None = None
_creator_pid: int = os.getpid()


class ReleaseIdentityError(ValueError):
    """release identity 计算失败；reason 必须在 _ALLOWED_REASONS 中。"""

    def __init__(self, reason: str) -> None:
        if reason not in _ALLOWED_REASONS:
            reason = "unexpected"
        super().__init__(reason)
        self.reason = reason


def _compute_identity() -> dict[str, Any]:
    edition = os.environ.get("COCKPIT_EDITION", "source")
    if edition not in _VALID_EDITIONS:
        raise ReleaseIdentityError("invalid_edition")
    source_sha = os.environ.get("COCKPIT_SOURCE_SHA", "")
    if edition in ("server", "desktop"):
        if not source_sha:
            raise ReleaseIdentityError("missing_source_sha")
        if not _GIT_SHA_RE.fullmatch(source_sha):
            raise ReleaseIdentityError("malformed_source_sha")
    else:  # source edition
        if source_sha:
            if not _GIT_SHA_RE.fullmatch(source_sha):
                raise ReleaseIdentityError("malformed_source_sha")
        else:
            source_sha = "unknown"
    try:
        version = read_current_version()
    except Exception:
        raise ReleaseIdentityError("version_unavailable")
    return {
        "version": version,
        "source_sha": source_sha,
        "edition": edition,
        "instance_id": _instance_id,
        "pid": os.getpid(),
    }


def get_release_identity() -> dict[str, Any]:
    """返回冻结的 release identity；首次调用计算并缓存（同进程稳定）。
    fork 后 child 首次调用自动检测 creator_pid 不符 → 重置 instance_id + 重算。
    R3: 未知 ValueError/Exception 包装为 ReleaseIdentityError("unexpected")。"""
    global _cached, _instance_id, _creator_pid
    try:
        if _cached is not None and os.getpid() == _creator_pid:
            return dict(_cached)
        if _cached is not None and os.getpid() != _creator_pid:
            _instance_id = uuid.uuid4().hex
            _creator_pid = os.getpid()
            _cached = None
        if _cached is None:
            _cached = _compute_identity()
            _creator_pid = os.getpid()
        return dict(_cached)
    except ReleaseIdentityError:
        raise
    except (ValueError, Exception) as exc:
        raise ReleaseIdentityError("unexpected") from exc


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


# R3: 只在 API 存在时注册 fork hook（无 fork 平台 import 成功）
if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        after_in_child=lambda: (
            globals().__setitem__("_instance_id", uuid.uuid4().hex),
            globals().__setitem__("_cached", None),
            globals().__setitem__("_creator_pid", os.getpid()),
        )
    )
