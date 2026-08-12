"""运行时版本与 GitHub Release 检查（U1a）。

版本只读仓库根 VERSION 文件，不依赖部署目录的 Git 元数据。
latest 查询官方非 draft/non-prerelease Release；失败时 HTTP 层仍 200，
status=unavailable，不向前端透传 body/异常/token。
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .artifact_root import resolve_artifact_root
from .github_release_auth import load_github_release_token


VERSION_PATH = resolve_artifact_root() / "VERSION"
GITHUB_LATEST_API = (
    "https://api.github.com/repos/fyc0451/agent-cockpit/releases/latest"
)
RELEASE_URL_PREFIX = "https://github.com/fyc0451/agent-cockpit/releases/"
# 仅接受 x.y.z / vx.y.z 三段数字 SemVer
_SEMVER_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
CACHE_TTL_S = 6 * 3600
HTTP_TIMEOUT_S = 5.0

# 缓存：单次进程内；refresh 绕过；并发刷新单飞（generation 合并等待中的请求）
_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()
_cached_latest: dict[str, Any] | None = None  # 解析后的 latest 或 None
_cached_checked_at: float | None = None
_cached_fetch_ok: bool = False  # True=拿到合法 latest；False=unavailable 结果
_fetch_generation: int = 0


def _utc_iso(ts: float | None = None) -> str:
    when = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    # 稳定 UTC ISO8601，带 Z 后缀
    return when.isoformat().replace("+00:00", "Z")


def read_current_version(path: Path | None = None) -> str:
    """读取 VERSION 文件（严格单行非空）。"""
    target = path if path is not None else VERSION_PATH
    text = target.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("VERSION 文件为空")
    # 运行时 current 也必须是合法三段 SemVer（无强制 v 前缀）
    parsed = parse_semver(text)
    if parsed is None:
        raise ValueError(f"VERSION 不是合法 SemVer: {text!r}")
    return format_semver(parsed)


def parse_semver(value: str) -> tuple[int, int, int] | None:
    """解析 x.y.z 或 vx.y.z；非法返回 None。"""
    if not isinstance(value, str):
        return None
    match = _SEMVER_RE.fullmatch(value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def format_semver(parts: tuple[int, int, int]) -> str:
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


def compare_semver(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    """返回 -1/0/1（a<b / a==b / a>b）。"""
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def is_allowed_release_url(url: str) -> bool:
    """只接受官方 releases 路径下的 https 链接。"""
    if not isinstance(url, str) or not url.startswith(RELEASE_URL_PREFIX):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        return False
    if ".." in parsed.path or "\\" in parsed.path:
        return False
    return parsed.path.startswith("/fyc0451/agent-cockpit/releases/")


def _parse_release_payload(data: Any) -> dict[str, Any] | None:
    """从 GitHub Release JSON 提取 latest；非法则 None。"""
    if not isinstance(data, dict):
        return None
    if data.get("draft") is True or data.get("prerelease") is True:
        return None
    tag = data.get("tag_name")
    release_tag = str(tag) if tag is not None else ""
    if release_tag.startswith("agent-cockpit-v"):
        release_tag = release_tag.removeprefix("agent-cockpit-v")
        parts = parse_semver(release_tag)
        if parts is None or release_tag != format_semver(parts):
            return None
    else:
        parts = parse_semver(release_tag)
    if parts is None:
        return None
    html_url = data.get("html_url")
    if not isinstance(html_url, str) or not is_allowed_release_url(html_url):
        return None
    name = data.get("name")
    if name is None or name == "":
        name = str(tag)
    elif not isinstance(name, str):
        return None
    published_at = data.get("published_at")
    if published_at is not None and not isinstance(published_at, str):
        return None
    return {
        "version": format_semver(parts),
        "name": name,
        "url": html_url,
        "published_at": published_at,
    }


def _http_get_latest() -> dict[str, Any] | None:
    """发起一次外部请求；任何失败返回 None（调用方标 unavailable）。"""
    try:
        token = load_github_release_token()
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-cockpit-version-check",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            response = client.get(
                GITHUB_LATEST_API,
                headers=headers,
            )
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            return None
        return _parse_release_payload(payload)
    except Exception:
        # 超时、限流网络错误、TLS 等一律降级；不向外抛
        return None


def _cache_fresh(now: float) -> bool:
    return (
        _cached_checked_at is not None
        and (now - _cached_checked_at) < CACHE_TTL_S
    )


def get_latest_release(*, refresh: bool = False) -> tuple[dict[str, Any] | None, float]:
    """返回 (latest_or_None, checked_at_epoch)。

    默认 6h 缓存；refresh=True 绕过。并发刷新时只有一个线程打外网：
    等待 refresh 锁期间若 generation 已前进且缓存仍新鲜，直接复用。
    """
    global _cached_latest, _cached_checked_at, _cached_fetch_ok, _fetch_generation
    now = time.time()
    with _cache_lock:
        if not refresh and _cache_fresh(now):
            return _cached_latest, float(_cached_checked_at)
        wait_gen = _fetch_generation

    with _refresh_lock:
        now = time.time()
        with _cache_lock:
            # 并发单飞：等待期间已有线程完成一次抓取
            if _fetch_generation > wait_gen and _cache_fresh(now):
                return _cached_latest, float(_cached_checked_at)
            if not refresh and _cache_fresh(now):
                return _cached_latest, float(_cached_checked_at)
        latest = _http_get_latest()
        checked = time.time()
        with _cache_lock:
            _cached_latest = latest
            _cached_checked_at = checked
            _cached_fetch_ok = latest is not None
            _fetch_generation += 1
            return _cached_latest, float(_cached_checked_at)


def clear_cache() -> None:
    """测试用：清空 latest 缓存。"""
    global _cached_latest, _cached_checked_at, _cached_fetch_ok, _fetch_generation
    with _cache_lock:
        _cached_latest = None
        _cached_checked_at = None
        _cached_fetch_ok = False
        _fetch_generation = 0


def get_version_info(
    *,
    refresh: bool = False,
    version_path: Path | None = None,
) -> dict[str, Any]:
    """组装 /api/version 响应体。"""
    current_version = read_current_version(version_path)
    current_parts = parse_semver(current_version)
    assert current_parts is not None

    latest, checked_at = get_latest_release(refresh=refresh)
    if latest is None:
        status = "unavailable"
        latest_out: dict[str, Any] | None = None
    else:
        latest_parts = parse_semver(latest["version"])
        assert latest_parts is not None
        if compare_semver(latest_parts, current_parts) > 0:
            status = "update_available"
        else:
            status = "up_to_date"
        latest_out = {
            "version": latest["version"],
            "name": latest["name"],
            "url": latest["url"],
            "published_at": latest.get("published_at"),
        }

    return {
        "current": {"version": current_version},
        "latest": latest_out,
        "status": status,
        "checked_at": _utc_iso(checked_at),
    }
