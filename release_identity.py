"""release_identity.py — 冻结 machine-verifiable release identity (Wiki13 J1A)。

version/source_sha/edition/instance_id/pid 在进程首次请求时计算并缓存。
请求路径不执行 git，不泄露 path/token。非法 edition 或畸形 build metadata
→ fail-closed（ValueError），/health/live 返回 503 + error。

edition 来源：COCKPIT_EDITION 环境变量（server|desktop|source），缺省 source。
source_sha 来源：COCKPIT_SOURCE_SHA 环境变量（build 时注入），缺省 "unknown"。
instance_id：进程级 UUID（import 时生成，fork 后可用 refresh_pid 更新）。
pid：os.getpid()（首次计算时冻结，refresh_pid 后更新）。
"""
from __future__ import annotations

import os
import re
import uuid
from typing import Any

from version import read_current_version

_VALID_EDITIONS = frozenset({"server", "desktop", "source"})
# 接受 7-8 位短 sha、40 位 git sha、64 位 sha256
_SHA_RE = re.compile(r"^[0-9a-f]{7,8}$|^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_instance_id: str = uuid.uuid4().hex
_cached: dict[str, Any] | None = None


def _compute_identity() -> dict[str, Any]:
    edition = os.environ.get("COCKPIT_EDITION", "source")
    if edition not in _VALID_EDITIONS:
        raise ValueError(
            f"非法 edition: {edition!r}（允许 {sorted(_VALID_EDITIONS)}）"
        )
    source_sha = os.environ.get("COCKPIT_SOURCE_SHA", "")
    if source_sha and not _SHA_RE.fullmatch(source_sha.lower()):
        raise ValueError(f"source_sha 畸形: {source_sha!r}")
    if not source_sha:
        source_sha = "unknown"  # source 模式 fallback
    return {
        "version": read_current_version(),
        "source_sha": source_sha,
        "edition": edition,
        "instance_id": _instance_id,
        "pid": os.getpid(),
    }


def get_release_identity() -> dict[str, Any]:
    """返回冻结的 release identity；首次调用计算并缓存（同进程稳定）。
    返回拷贝，调用方不得修改内部缓存。"""
    global _cached
    if _cached is None:
        _cached = _compute_identity()
    return dict(_cached)


def refresh_pid() -> None:
    """fork 后更新 pid（测试用）。"""
    global _cached
    if _cached is not None:
        _cached["pid"] = os.getpid()


def reset_cache() -> None:
    """测试用：清空缓存，下次 get_release_identity 重新计算。"""
    global _cached
    _cached = None
