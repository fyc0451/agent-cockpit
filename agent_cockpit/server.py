"""server.py — Agent Cockpit FastAPI 入口。

与 herdr / Agent Mail hub 同机部署,直读本地 SQLite + 调本机 hub MCP + herdr socket。
Mac/手机浏览器通过内网访问(默认 :8790)。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import itertools
import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import Body, FastAPI, UploadFile, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from . import db
import httpx
from . import coordination
from . import hub_client
from . import herdr_client
from . import release_identity
from . import runtime_stats
from . import herdr_state
from . import leader_binding
from . import b0_wiring
from . import tasks
from . import team_inbox_router
from . import uploads
from . import files
from . import instance_lock
from . import mail_projects
from . import next_profile
from . import team_sessions
from . import terminal
from . import version
from . import upgrade_core
from . import upgrade_service
from . import web_push
from . import settings
from . import project_discovery
from . import project_discovery_service
from . import project_registry_api
from . import project_registry_store
from . import project_workbench_adapter
from . import runtime_paths
from .artifact_root import resolve_artifact_root
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator


ROOT_DIR = resolve_artifact_root()
_next_instance_lock_owner: instance_lock.InstanceLock | None = None
if next_profile.enabled():
    try:
        _next_instance_lock_owner = instance_lock.require_registered_owner()
    except instance_lock.LockError as exc:
        raise RuntimeError("next_instance_lock_required") from exc


H0_STATE_MODE_ENV = "COCKPIT_HERDR_STATE_MODE"
H0_STATE_CANARY_SESSIONS_ENV = "COCKPIT_HERDR_STATE_CANARY_SESSIONS"
_H0_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _parse_h0_state_mode(value: str | None) -> str:
    mode = (value or "").strip().lower() or "off"
    if mode not in {"off", "canary", "on"}:
        raise RuntimeError(f"invalid {H0_STATE_MODE_ENV}")
    return mode


def _parse_h0_canary_sessions(value: str | None) -> frozenset[str]:
    raw = (value or "").strip()
    if not raw:
        return frozenset()
    sessions: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if not name or not _H0_SESSION_NAME_RE.fullmatch(name):
            raise RuntimeError(f"invalid {H0_STATE_CANARY_SESSIONS_ENV}")
        if name in sessions:
            raise RuntimeError(f"duplicate {H0_STATE_CANARY_SESSIONS_ENV}")
        sessions.append(name)
    return frozenset(sessions)


def _validate_h0_state_config(mode: str, canary_sessions: frozenset[str]) -> None:
    if mode == "canary" and not canary_sessions:
        raise RuntimeError(f"{H0_STATE_CANARY_SESSIONS_ENV} required for canary")


H0_STATE_MODE = _parse_h0_state_mode(os.environ.get(H0_STATE_MODE_ENV))
H0_STATE_CANARY_SESSIONS = _parse_h0_canary_sessions(
    os.environ.get(H0_STATE_CANARY_SESSIONS_ENV)
)
_validate_h0_state_config(H0_STATE_MODE, H0_STATE_CANARY_SESSIONS)

B0_MODE_ENV = "COCKPIT_B0_MODE"
B0_CANARY_SCOPES_ENV = "COCKPIT_B0_CANARY_SCOPES"


def _parse_b0_mode(value: str | None) -> str:
    mode = (value or "").strip().lower() or "off"
    if mode not in {"off", "shadow", "canary", "on"}:
        raise RuntimeError(f"invalid {B0_MODE_ENV}")
    return mode


def _parse_b0_canary_scopes(value: str | None) -> frozenset[tuple[str, str]]:
    raw = (value or "").strip()
    if not raw:
        return frozenset()
    scopes: list[tuple[str, str]] = []
    for item in raw.split(","):
        entry = item.strip()
        scope_kind, separator, scope_id = entry.partition("/")
        if (
            not entry
            or not separator
            or scope_kind not in leader_binding.SCOPE_KINDS
            or not scope_id
            or len(scope_id) > 256
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in scope_id)
        ):
            raise RuntimeError(f"invalid {B0_CANARY_SCOPES_ENV}")
        scope = (scope_kind, scope_id)
        if scope in scopes:
            raise RuntimeError(f"duplicate {B0_CANARY_SCOPES_ENV}")
        scopes.append(scope)
    return frozenset(scopes)


def _validate_b0_config(
    mode: str, canary_scopes: frozenset[tuple[str, str]],
) -> None:
    if mode == "canary" and not canary_scopes:
        raise RuntimeError(f"{B0_CANARY_SCOPES_ENV} required for canary")


B0_MODE = _parse_b0_mode(os.environ.get(B0_MODE_ENV))
B0_CANARY_SCOPES = _parse_b0_canary_scopes(
    os.environ.get(B0_CANARY_SCOPES_ENV)
)
_validate_b0_config(B0_MODE, B0_CANARY_SCOPES)
B0_ISSUER = os.environ.get("B0_ISSUER", "local").strip() or "local"


def _h0_state_enabled() -> bool:
    return H0_STATE_MODE in {"canary", "on"}


def _h0_state_session_enabled(name: str) -> bool:
    return H0_STATE_MODE == "on" or (
        H0_STATE_MODE == "canary" and name in H0_STATE_CANARY_SESSIONS
    )


def _b0_runtime_active() -> bool:
    return B0_MODE in {"canary", "on"}


def _b0_scope_enabled(scope_kind: str, scope_id: str) -> bool:
    return B0_MODE == "on" or (
        B0_MODE == "canary" and (scope_kind, scope_id) in B0_CANARY_SCOPES
    )


def _require_next_instance_lock() -> None:
    if not next_profile.enabled():
        return
    owner = _next_instance_lock_owner
    if not isinstance(owner, instance_lock.InstanceLock) or owner.fd is None:
        raise RuntimeError("next_instance_lock_required")
    try:
        owner.read_metadata(current_owner=True)
    except instance_lock.LockError as exc:
        raise RuntimeError("next_instance_lock_invalid") from exc


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _poller_task, _message_poller_task, _worktree_cleanup_task
    global _identity_retirement_task
    _require_next_instance_lock()
    project_registry_api_service().prepare()
    state_enabled = _h0_state_enabled()
    b0_runtime_active = _b0_runtime_active()
    if state_enabled and not _open_state_clients():
        # R7 门禁:未决 retiring/survivors 未回收,fail-closed 拒绝启动
        raise RuntimeError("herdr state clients not fully reaped; refusing start")
    if state_enabled:
        await asyncio.to_thread(_reconcile_state_client)
    if b0_runtime_active:
        b0_wiring.install_claim_gate(
            issuer=B0_ISSUER,
            scope_filter=_b0_scope_enabled,
            enforce_all=B0_MODE == "on",
        )
        await asyncio.to_thread(_b0_rebuild_on_start)
    else:
        b0_wiring.uninstall_claim_gate()
    _poller_task = asyncio.create_task(_poll_live_state())
    _message_poller_task = asyncio.create_task(_poll_message_state())
    _worktree_cleanup_task = asyncio.create_task(_worktree_cleanup_loop())
    _identity_retirement_task = asyncio.create_task(_identity_retirement_loop())
    # T1:pending 重启恢复。单任务失败只记日志；列库失败可重试一次，不拖垮启动。
    try:
        recovery = await asyncio.to_thread(tasks.recover_pending_tasks)
        if recovery.get("retryable"):
            logger.warning(
                "pending task recovery list failed; retrying once: %s", recovery
            )
            recovery = await asyncio.to_thread(tasks.recover_pending_tasks)
        if recovery.get("failed") or recovery.get("error"):
            logger.warning("pending task recovery incomplete: %s", recovery)
        elif not recovery.get("skipped"):
            logger.info(
                "pending task recovery: recovered=%s failed=%s total=%s",
                recovery.get("recovered"),
                recovery.get("failed"),
                recovery.get("total"),
            )
    except Exception:
        logger.exception("pending task recovery crashed; continuing startup")
    try:
        yield
    finally:
        background_tasks = (
            _poller_task, _message_poller_task, _worktree_cleanup_task,
            _identity_retirement_task,
        )
        for task in background_tasks:
            if task is not None:
                task.cancel()
        for task in background_tasks:
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("background task failed during shutdown")
        if b0_runtime_active:
            b0_wiring.uninstall_claim_gate()
        try:
            await asyncio.to_thread(_release_all_zoom_leases)
        finally:
            survivors = (
                await asyncio.to_thread(_stop_state_client)
                if state_enabled else []
            )
            if state_enabled and survivors:
                # deadline 耗尽仍有存活 state 线程:诊断已序列化,真实引用
                # 保留在 _state_survivors;不得仅记日志后正常完成
                logger.error("herdr state clients survived stop: %s", survivors)
                raise RuntimeError(
                    f"herdr state clients survived stop: {len(survivors)}"
                )
            _poller_task = None
            _message_poller_task = None
            _worktree_cleanup_task = None
            _identity_retirement_task = None


app = FastAPI(title="Agent Cockpit", lifespan=lifespan)
STATIC_DIR = ROOT_DIR / "static"
NEXT_WEB_DIR = ROOT_DIR / "web" / "dist"
COCKPIT_TOKEN = os.environ.get("COCKPIT_TOKEN", "")
AUTH_COOKIE = "cockpit_session"
TEAM_AUTH_COOKIE = "cockpit_team_human_session"
PUBLIC_PATHS = {
    "/", "/health", "/health/live", "/health/ready", "/api/auth/status", "/api/auth/login",
    "/api/agent/team-reply",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
UNTRUSTED_PROXY_HEADERS = frozenset({
    b"forwarded",
    b"x-forwarded-for",
    b"x-forwarded-host",
    b"x-forwarded-port",
    b"x-forwarded-proto",
    b"x-real-ip",
})
logger = logging.getLogger("agent-cockpit")
# 对外 500 统一文案：不回显异常类型/正文/路径/token 等内部细节（O2）。
INTERNAL_ERROR_DETAIL = "服务器内部错误，请稍后重试"
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PANE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$")


_project_registry_store: project_registry_store.ProjectRegistryStore | None = None


class _RegistryDiscoveryReader:
    def match_discovery(
        self, *, node_id: str, canonical_path: str, repository_fingerprint: str | None,
    ) -> tuple[project_discovery.RegistryMatch | None, tuple[project_discovery.RegistryMatch, ...]]:
        exact, possible = _project_registry().match_discovery(
            node_id=node_id,
            canonical_path=canonical_path,
            repository_fingerprint=repository_fingerprint,
        )
        def convert(value):
            if value is None:
                return None
            return project_discovery.RegistryMatch(
                value.project_id, value.slug, value.display_name,
            )
        return convert(exact), tuple(convert(value) for value in possible)


def _project_registry() -> project_registry_store.ProjectRegistryStore:
    global _project_registry_store
    if _project_registry_store is None:
        _project_registry_store = project_registry_store.initialize(
            runtime_paths.store("project_registry")
        )
    return _project_registry_store


def _project_workbench_registry() -> project_registry_store.ProjectRegistryStore:
    if _project_registry_store is not None:
        return _project_registry_store
    return project_registry_store.open_existing(
        runtime_paths.store("project_registry")
    )


def _project_discovery_service() -> project_discovery_service.LocalProjectDiscoveryService:
    return project_discovery_service.LocalProjectDiscoveryService(
        registry_match_reader=_RegistryDiscoveryReader(),
    )


def project_registry_api_service() -> project_registry_api.ApiService:
    return project_registry_api.ApiService(
        registry_provider=_project_registry,
        discovery_provider=_project_discovery_service,
    )


project_registry_api.install(app, project_registry_api_service())


def _scoped_registry_request(request: Request) -> bool:
    return project_registry_api.is_scoped_registry_path(str(request.scope.get("path") or request.url.path))


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    """规范化 HTTPException 响应；仅 500 强制通用文案，4xx/502/503 等保持原 detail。"""
    if _scoped_registry_request(request):
        return project_registry_api.bridge_http_exception(request, exc.status_code, exc.detail)
    if exc.status_code == 500:
        return JSONResponse(
            status_code=500,
            content={"detail": INTERNAL_ERROR_DETAIL},
            headers=exc.headers,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def _request_validation_exception_handler(request: Request, exc: RequestValidationError):
    if _scoped_registry_request(request):
        return project_registry_api.bridge_validation_error(request)
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """未捕获异常：记 stack + method/path，响应仅通用 500（不记 body/鉴权/query）。"""
    logger.exception(
        "unhandled exception method=%s path=%s",
        request.method,
        request.url.path,
    )
    if _scoped_registry_request(request):
        return project_registry_api.bridge_unhandled(request)
    return JSONResponse(
        status_code=500,
        content={"detail": INTERNAL_ERROR_DETAIL},
    )


@app.exception_handler(files.CustomRootsError)
async def _custom_roots_exception_handler(request: Request, exc: files.CustomRootsError):
    """污染的本地授权 store 属服务不可用，返回稳定脱敏 reason。"""
    return JSONResponse(status_code=503, content={"detail": str(exc)})
TEAM_API_ROUTES = (
    (re.compile(r"^humans/me$"), {"GET", "PUT"}),
    (re.compile(r"^inbox$"), {"GET"}),
    (re.compile(r"^inbox/mark-read$"), {"POST"}),
    (re.compile(r"^projects$"), {"GET", "POST"}),
    (re.compile(r"^projects/[A-Za-z0-9_-]+/join-requests$"), {"POST"}),
    (re.compile(r"^projects/[A-Za-z0-9_-]+/membership$"), {"GET", "PATCH"}),
    (re.compile(r"^projects/[A-Za-z0-9_-]+/chat/messages$"), {"GET"}),
    (re.compile(r"^projects/[A-Za-z0-9_-]+/support-requests$"), {"POST"}),
    (re.compile(r"^projects/[A-Za-z0-9_-]+/members$"), {"GET"}),
    (re.compile(r"^projects/[A-Za-z0-9_-]+/members/[0-9]+$"), {"PATCH"}),
    (re.compile(r"^projects/[A-Za-z0-9_-]+/agents$"), {"GET"}),
    (re.compile(r"^projects/[A-Za-z0-9_-]+/agents/[0-9]+$"), {"PATCH"}),
    (re.compile(r"^agents$"), {"GET"}),
    (re.compile(r"^projects/[A-Za-z0-9_-]+/agent-bindings$"), {"GET", "POST"}),
    (re.compile(r"^projects/[A-Za-z0-9_-]+/agent-bindings/[0-9]+$"), {"DELETE"}),
)
VALID_AGENTS = {"codex", "kimi", "claude", "qoder", "qodercli", "qodercn", "grok", "opencode"}
VALID_LAYOUTS = {"right", "horizontal", "down", "vertical", "tab"}
VALID_COLLAB_MODES = {"quick", "develop_review", "parallel", "custom"}
VALID_WORKSPACE_ROLES = {"lead", "developer", "reviewer", "researcher"}
VALID_WORKSPACE_STRATEGIES = {"auto", "shared", "isolated"}
VALID_PANE_SEND_MODES = {"send", "prompt", "keys"}
MAIL_AGENT_NAMES = {
    "codex-cli": "codex",
    "kimi-work": "kimi",
    "claude-code": "claude",
    "qoder": "qodercn",
    "qodercli": "qodercn",
    "qodercn": "qodercn",
    "qoder-cn": "qodercn",
}
SESSION_START_TIMEOUT = 20.0
SESSION_BOOTSTRAP_OUTPUT_LIMIT = 16 * 1024
SESSION_BOOTSTRAP_PANE_COLS = 100
SESSION_BOOTSTRAP_PANE_ROWS = 30
TERM_READ_WAIT = 0.02
TERM_READ_BURST = 256 * 1024


def _parse_poll_interval(raw: str | None) -> float:
    """解析 COCKPIT_POLL_INTERVAL:拒 <=0/NaN/inf,非法值回退默认。"""
    default = 2.0
    if not raw:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    # math.isfinite 拒 NaN/inf;<=0 会 busy loop
    import math
    if not math.isfinite(val) or val <= 0:
        return default
    return val


# live-state 轮询间隔:默认 2s(并行 snapshot 后单轮总耗时≈最慢单项,可承受更短间隔)。
# 无 session 时回到空闲间隔省 fork;连续失败退避避免风暴(分支优先于 idle:即使
# 无 session,失败时也要退避,否则异常时永远固定 idle 10s 不做连续退避)。
POLL_INTERVAL = _parse_poll_interval(os.environ.get("COCKPIT_POLL_INTERVAL"))
POLL_IDLE_INTERVAL = 10.0
POLL_BACKOFF = 1.5
POLL_BACKOFF_MAX = 20.0
# 轮询指标:保留最近 N 次耗时,计算 p50/p95/failure_rate,供诊断(不改 SSE schema)。
_POLL_METRICS_SAMPLES = 60
_POLL_METRICS: dict[str, Any] = {
    "count": 0, "failures": 0, "consecutive_failures": 0,
    "last_duration": 0.0, "last_session_count": 0, "samples": [],
    "duration_p50": 0.0, "duration_p95": 0.0, "failure_rate": 0.0,
}
_message_state: dict[str, Any] = {
    "revision": 0, "source_version": None, "signatures": None, "changes": [],
}


def _record_poll_metrics(duration: float, session_count: int, success: bool) -> None:
    """记录单次 poll 指标并更新聚合(p50/p95/failure_rate)。生产路径,测试直接调。"""
    m = _POLL_METRICS
    m["count"] += 1
    m["last_duration"] = duration
    m["last_session_count"] = session_count
    samples = m["samples"]
    samples.append(duration)
    if len(samples) > _POLL_METRICS_SAMPLES:
        del samples[: len(samples) - _POLL_METRICS_SAMPLES]
    if success:
        m["consecutive_failures"] = 0
    else:
        m["failures"] += 1
        m["consecutive_failures"] += 1
    # 聚合:基于 samples 窗口的 p50/p95;failure_rate 基于累计 count。
    sorted_samples = sorted(samples)
    n = len(sorted_samples)
    m["duration_p50"] = sorted_samples[n // 2] if n else 0.0
    p95_idx = min(n - 1, int(n * 0.95))
    m["duration_p95"] = sorted_samples[p95_idx] if n else 0.0
    m["failure_rate"] = m["failures"] / m["count"] if m["count"] else 0.0


def _poll_delay(session_count: int) -> float:
    """计算下一轮 poll 的 sleep 间隔。失败分支优先于 idle。

    生产路径,测试直接调:覆盖成功(idle/正常)、连续失败 delay 递增封顶。
    """
    cf = _POLL_METRICS["consecutive_failures"]
    if cf > 0:
        delay = POLL_INTERVAL
        for _ in range(cf):
            delay *= POLL_BACKOFF
            if delay >= POLL_BACKOFF_MAX:
                return POLL_BACKOFF_MAX
        return delay
    if session_count == 0:
        return POLL_IDLE_INTERVAL
    return POLL_INTERVAL


def _refresh_message_state() -> None:
    """在外部 SQLite 提交后更新项目级消息 revision，不读取消息正文。"""
    global _message_state
    version = (db.data_version(), coordination.message_state_revision())
    if version == _message_state["source_version"]:
        return
    signatures = db.message_project_signatures()
    slugs = db.project_slugs_by_human_key()
    for project_key, receipt_signature in coordination.message_project_signatures().items():
        slug = slugs.get(project_key)
        if slug:
            signatures[slug] = (signatures.get(slug, ()), receipt_signature)
    previous = _message_state["signatures"]
    changed: list[str] = []
    revision = int(_message_state["revision"])
    if previous is not None:
        changed = sorted(
            slug for slug in set(previous) | set(signatures)
            if previous.get(slug) != signatures.get(slug)
        )
        if changed:
            revision += 1
    changes = list(_message_state["changes"])
    if changed:
        changes.append({"revision": revision, "projects": changed})
        changes = changes[-60:]
    _message_state = {
        "revision": revision,
        "source_version": version,
        "signatures": signatures,
        "changes": changes,
    }
AGENT_MAIL_TOOLS_DIR = ROOT_DIR / "agent-mail-tools"
AGENT_MAIL_INIT_SCRIPT = AGENT_MAIL_TOOLS_DIR / "am-init-project"
AM_RETIRE_SCRIPT = AGENT_MAIL_TOOLS_DIR / "am-retire"
MAIL_SEND_SCRIPT = AGENT_MAIL_TOOLS_DIR / "mail-send"
MAIL_RECV_SCRIPT = AGENT_MAIL_TOOLS_DIR / "mail-recv"
TASK_REPORT_SCRIPT = AGENT_MAIL_TOOLS_DIR / "task-report"
_SETUP_WORKSPACE_LOCKS: dict[str, threading.Lock] = {}
_SETUP_WORKSPACE_LOCKS_GUARD = threading.Lock()
_TEAM_SESSION_BIND_LOCK = threading.Lock()
ZOOM_LEASE_TTL = 30.0
ZOOM_LEASE_RETRY = 5.0
_ZOOM_LEASES: dict[str, dict[str, Any]] = {}
_ZOOM_LEASES_LOCK = threading.RLock()
TERM_WS_TAKEN_OVER_CODE = 4001
TERM_WS_INVALID_CODE = 4004
_TERM_WS_CONNECTIONS: dict[str, dict[str, Any]] = {}
_TERM_INPUT_NOTE_TASKS: dict[str, asyncio.Task[None]] = {}
_TERM_INPUT_NOTE_PENDING: set[str] = set()
_IDENTITY_RETIRE_LOCKS: dict[str, threading.Lock] = {}
_IDENTITY_RETIRE_LOCKS_GUARD = threading.Lock()
IDENTITY_RETIRE_RETRY_INTERVAL_S = 60.0
MAIL_COORDINATION_GUIDE = (
    "协作通信约定:长任务每完成一个里程碑检查一次未读消息；多封消息按时间顺序处理；"
    "收到停止/转向时，在完成当前原子操作并保存状态后立即停手汇报；"
    "收到消息后按 mail-recv 输出先 claim、处理完成再单条 complete/ack；"
    "普通打断保存 checkpoint 后恢复原任务，停止/转向不恢复。"
)
LOCAL_ONLY_AUTH_DETAIL = (
    "未设置 COCKPIT_TOKEN 时仅允许本机回环访问；局域网访问请配置 COCKPIT_TOKEN"
)

# --- B0 wiring（A2/B1 定稿合同；消息 I/O 仅在 _poll_message_state 链路） ---
_b0_coordinator: b0_wiring.B0Coordinator | None = None
_b0_init_lock = threading.Lock()
_b0_shadow_state: dict[str, Any] = {
    "available": True, "degraded": False, "reason": None,
    "scopes": 0, "identities": 0, "pulled": 0,
}
_b0_shadow_lock = threading.Lock()
_b0_runtime_state: dict[str, Any] = {
    "available": False, "degraded": True, "reason": "starting",
    "scopes": {}, "last_reasons": {},
}
_b0_runtime_state_lock = threading.Lock()


def _b0_binding_target(scope: str) -> tuple[str, str] | None:
    """scope → active binding 的 (session, pane_id)（W1 adapter 路由）。"""
    try:
        issuer, scope_kind, scope_id = b0_wiring.split_scope_key(scope)
        if issuer != B0_ISSUER or not _b0_scope_enabled(scope_kind, scope_id):
            return None
        row = leader_binding.get_active_binding(issuer, scope_kind, scope_id)
    except Exception:
        return None
    if not row:
        return None
    session = str(row.get("session") or "")
    pane_id = str(row.get("pane_id") or "")
    if not session or not pane_id:
        return None
    return session, pane_id


def _b0_get_coordinator() -> b0_wiring.B0Coordinator | None:
    """惰性创建 B0 协调器（DB/环境异常时返回 None，调用方降级跳过）。"""
    global _b0_coordinator
    if not _b0_runtime_active():
        return None
    if _b0_coordinator is not None:
        return _b0_coordinator
    with _b0_init_lock:
        if _b0_coordinator is None:
            try:
                adapter = b0_wiring.HerdrPromptAdapter(_b0_binding_target)
                _b0_coordinator = b0_wiring.B0Coordinator(
                    adapter, B0_ISSUER, scope_filter=_b0_scope_enabled,
                )
            except Exception:
                logger.exception("b0 coordinator init failed")
                return None
    return _b0_coordinator


def _b0_control_transport(event: dict[str, Any]) -> bool:
    """F6/F7：以事件 scope 的 active binding 身份向各 active run 参与者
    发送可 claim、携 binding version 的 Hub control message。"""
    scope_kind = str(event.get("scope_kind") or "")
    scope_id = str(event.get("scope_id") or "")
    if (
        str(event.get("issuer") or "") != B0_ISSUER
        or not _b0_scope_enabled(scope_kind, scope_id)
    ):
        return False
    try:
        row = leader_binding.get_active_binding(
            B0_ISSUER, scope_kind, scope_id,
        )
    except Exception:
        return False
    if not row or not row.get("registry_selector"):
        return False
    try:
        identity = b0_wiring.resolve_selector(str(row["registry_selector"]))
    except b0_wiring.CredentialUnavailable:
        return False
    return b0_wiring.send_control_message_to_participants(event, identity)


def _b0_poll_tick() -> None:
    """消息 poller 链路的一次 B0 tick：sync + fanout + dual-pull/ingest。"""
    global _b0_shadow_state, _b0_runtime_state
    if B0_MODE == "off":
        return
    if B0_MODE == "shadow":
        state = b0_wiring.shadow_probe(B0_ISSUER)
        with _b0_shadow_lock:
            _b0_shadow_state = dict(state)
        return
    coord = _b0_get_coordinator()
    if coord is None:
        return
    try:
        coord.sync_bindings()
        coord.fanout_control_events(transport=_b0_control_transport)
        coord.poll_once(unread_only=True)
        state = coord.state()
        degraded = any(
            bool(scope.get("degraded"))
            for scope in state.get("scopes", {}).values()
            if isinstance(scope, dict)
        )
        with _b0_runtime_state_lock:
            _b0_runtime_state = {
                "available": not degraded,
                "degraded": degraded,
                "reason": (
                    b0_wiring.REASON_CREDENTIAL_UNAVAILABLE if degraded else None
                ),
                "scopes": state.get("scopes", {}),
                "last_reasons": state.get("last_reasons", {}),
            }
    except Exception:
        logger.exception("b0 poll tick failed")
        with _b0_runtime_state_lock:
            _b0_runtime_state = {
                "available": False, "degraded": True,
                "reason": "runtime_error", "scopes": {},
                "last_reasons": {},
            }


def _b0_apply_live_status(snap: dict[str, Any]) -> None:
    """live poller 唯一允许的 B0 入口：仅注入 agent_status，禁消息 I/O。
    附带 G6 闭环：同名 agent 以新 pane 重启时更新 binding 路由载荷。"""
    if not _b0_runtime_active():
        return
    coord = _b0_get_coordinator()
    if coord is None or not isinstance(snap, dict):
        return
    try:
        rows = coord.active_bindings()
    except Exception:
        return
    panes = [p for p in snap.get("panes", []) if isinstance(p, dict)]
    status_by_pane = {p.get("pane_id"): p.get("agent_status") for p in panes}
    panes_by_mail: dict[Any, list[dict[str, Any]]] = {}
    for p in panes:
        if p.get("mail_name"):
            panes_by_mail.setdefault(p.get("mail_name"), []).append(p)
    for row in rows:
        pane_id = str(row.get("pane_id") or "")
        # G6：路由 pane 消失但同 mail_name 有新 pane → 改绑路由载荷；
        # 多候选歧义时禁止自动改绑（确定性门）
        if pane_id not in status_by_pane:
            candidates = panes_by_mail.get(row.get("mail_name")) or []
            candidates = [
                c for c in candidates if c.get("pane_id") != pane_id
            ]
            if len(candidates) > 1:
                logger.warning(
                    "b0 G6 ambiguous panes for mail_name=%s count=%d; "
                    "refusing auto-rebind",
                    row.get("mail_name"), len(candidates),
                )
            elif len(candidates) == 1:
                cand = candidates[0]
                try:
                    leader_binding.bind_leader(
                        row["issuer"], row["scope_kind"], row["scope_id"],
                        mail_name=str(row["mail_name"]),
                        agent_name=row.get("agent_name"),
                        agent_kind=row.get("agent_kind"),
                        session=str(cand.get("session") or row.get("session") or ""),
                        pane_id=str(cand["pane_id"]),
                        registry_selector=row.get("registry_selector"),
                        expected_version=int(row["binding_version"]),
                    )
                    pane_id = str(cand["pane_id"])
                except Exception:
                    logger.exception("b0 G6 pane rebind failed")
        status = status_by_pane.get(pane_id)
        if not status:
            continue
        scope = b0_wiring.scope_key(
            row["issuer"], row["scope_kind"], row["scope_id"],
        )
        try:
            coord.set_target_status(scope, str(status))
        except Exception:
            logger.exception("b0 set_target_status failed")


def _b0_rebuild_on_start() -> None:
    """restart rebuild（ADR 故障5）：同步 binding + 从 Hub unread 重建 pending。"""
    if not _b0_runtime_active():
        return
    global _b0_runtime_state
    coord = _b0_get_coordinator()
    if coord is None:
        return
    try:
        coord.rebuild()
        state = coord.state()
        degraded = any(
            bool(scope.get("degraded"))
            for scope in state.get("scopes", {}).values()
            if isinstance(scope, dict)
        )
        with _b0_runtime_state_lock:
            _b0_runtime_state = {
                "available": not degraded,
                "degraded": degraded,
                "reason": (
                    b0_wiring.REASON_CREDENTIAL_UNAVAILABLE if degraded else None
                ),
                "scopes": state.get("scopes", {}),
                "last_reasons": state.get("last_reasons", {}),
            }
    except Exception:
        logger.exception("b0 rebuild failed")
        with _b0_runtime_state_lock:
            _b0_runtime_state = {
                "available": False, "degraded": True,
                "reason": "restart_rebuild_failed", "scopes": {},
                "last_reasons": {},
            }


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _loopback_authority(value: str, scheme: str) -> tuple[str, int] | None:
    if (
        not value
        or len(value) > 255
        or value != value.strip()
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        or "@" in value
    ):
        return None
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", value)
        if not match:
            return None
        host, port_text = match.groups()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return None
        if address.version != 6 or "%" in host or not address.is_loopback:
            return None
        normalized_host = address.compressed
    else:
        if value.count(":") > 1:
            return None
        host, separator, port_text = value.partition(":")
        if not separator:
            port_text = None
        if not host:
            return None
        if host.lower() == "localhost":
            normalized_host = "localhost"
        else:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                return None
            if address.version != 4 or not address.is_loopback:
                return None
            normalized_host = address.compressed
    if port_text is None:
        port = 443 if scheme == "https" else 80
    elif (
        not port_text
        or not port_text.isascii()
        or not port_text.isdecimal()
        or port_text != str(int(port_text))
    ):
        return None
    else:
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
    return normalized_host, port


def _origin_authority(value: str) -> tuple[str, str, int] | None:
    if (
        not value
        or len(value) > 2048
        or value != value.strip()
        or "?" in value
        or "#" in value
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if (
            scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        authority = _loopback_authority(parsed.netloc, scheme)
    except ValueError:
        return None
    if authority is None:
        return None
    return scheme, *authority


def _no_token_scope_trusted(
    scope: dict[str, Any], *, require_origin: bool,
) -> bool:
    scheme = {
        "http": "http",
        "https": "https",
        "ws": "http",
        "wss": "https",
    }.get(scope.get("scheme"))
    raw_headers = scope.get("headers")
    if scheme is None or not isinstance(raw_headers, (list, tuple)):
        return False
    host_values: list[bytes] = []
    origin_values: list[bytes] = []
    for header in raw_headers:
        if (
            not isinstance(header, (list, tuple))
            or len(header) != 2
            or not isinstance(header[0], bytes)
            or not isinstance(header[1], bytes)
        ):
            return False
        name, value = header
        name = name.lower()
        if name in UNTRUSTED_PROXY_HEADERS or name.startswith(b"x-forwarded-"):
            return False
        if name == b"host":
            host_values.append(value)
        elif name == b"origin":
            origin_values.append(value)
    if len(host_values) != 1 or len(origin_values) > 1:
        return False
    if require_origin and len(origin_values) != 1:
        return False
    try:
        host_value = host_values[0].decode("ascii")
        origin_value = origin_values[0].decode("ascii") if origin_values else None
    except UnicodeDecodeError:
        return False
    host = _loopback_authority(host_value, scheme)
    if host is None:
        return False
    if origin_value is None:
        return True
    return _origin_authority(origin_value) == (scheme, *host)


def _session_value() -> str:
    if not COCKPIT_TOKEN:
        return ""
    return hmac.new(
        COCKPIT_TOKEN.encode("utf-8"), b"agent-cockpit-session", hashlib.sha256
    ).hexdigest()


def _valid_bearer(value: str | None) -> bool:
    if not COCKPIT_TOKEN or not value or not value.startswith("Bearer "):
        return False
    return hmac.compare_digest(value[7:], COCKPIT_TOKEN)


def _valid_cookie(value: str | None) -> bool:
    expected = _session_value()
    return bool(expected and value and hmac.compare_digest(value, expected))


def _request_authenticated(request: Request) -> bool:
    if not COCKPIT_TOKEN:
        return _is_loopback(request.client.host if request.client else None)
    return _valid_bearer(request.headers.get("authorization")) or _valid_cookie(
        request.cookies.get(AUTH_COOKIE)
    )


def _websocket_authenticated(websocket: WebSocket) -> bool:
    if not COCKPIT_TOKEN:
        return _is_loopback(websocket.client.host if websocket.client else None)
    return _valid_cookie(websocket.cookies.get(AUTH_COOKIE))


def _same_origin(origin: str | None, host: str | None) -> bool:
    if not origin or not host:
        return False
    try:
        return urlsplit(origin).netloc.lower() == host.lower()
    except ValueError:
        return False


def _validate_bind(host: str) -> None:
    if not _is_loopback(host) and not COCKPIT_TOKEN:
        raise RuntimeError("非回环监听（含局域网 IP）必须设置 COCKPIT_TOKEN")
    if not _is_loopback(host):
        logger.warning(
            "非回环监听必须使用 HTTPS 或 Tailscale Serve；"
            "明文 HTTP 会暴露可重放的登录 cookie"
        )


def _validate_session_name(name: str) -> None:
    if not SESSION_NAME_RE.fullmatch(name):
        raise HTTPException(400, "session 名仅允许字母、数字、下划线和连字符")
    try:
        next_profile.require_session(name)
    except next_profile.NextProfileError as exc:
        raise HTTPException(404, "session 不存在") from exc


def _validate_pane_id(pane_id: str) -> None:
    if not PANE_ID_RE.fullmatch(pane_id):
        raise HTTPException(400, "pane id 格式无效")


def _registry_identity_for_instance(
    cwd: str, agent_type: str, instance_id: str,
) -> dict[str, Any] | None:
    """从本机 registry 精确读取一个 Cockpit managed instance。"""
    try:
        opaque_id = herdr_client.validate_agent_instance_id(instance_id)
        project = str(Path(cwd).expanduser().resolve())
        mail_agent = MAIL_AGENT_NAMES.get(agent_type, agent_type)
        project_dir = re.sub(
            r"[^A-Za-z0-9]+", "-", project,
        ).strip("-").lower() or "root"
        filename = f"{mail_agent}--{opaque_id}.json"
        root = _REGISTRY_ROOT.resolve()
        identity = _read_registry_entry(root / project_dir / filename, root)
    except (OSError, ValueError):
        return None
    if not identity or (
        identity.get("project_key") != project
        or identity.get("agent") != mail_agent
        or identity.get("instance") != opaque_id
        or identity.get("status") not in (None, "active")
        or identity.get("retired_at")
    ):
        return None
    return identity


def _identity_record(
    cwd: str, agent_type: str, instance_id: str | None = None,
) -> dict[str, Any] | None:
    """只按已确定的 Agent Mail human key 查真实身份。"""
    try:
        cwd = next_profile.require_project(cwd)
        if instance_id is not None:
            identity = _registry_identity_for_instance(cwd, agent_type, instance_id)
            if not identity:
                return None
            return db.identity_by_cwd(cwd, agent_type, identity["name"])
        return db.identity_by_cwd(cwd, agent_type)
    except Exception:
        return None


def _identity_name(
    cwd: str, agent_type: str, instance_id: str | None = None,
) -> str | None:
    ident = _identity_record(cwd, agent_type, instance_id)
    return ident["name"] if ident else None


def _enrich_board_identities(snapshot: dict[str, Any]) -> dict[str, Any]:
    """按 session 的 canonical Agent Mail 项目给看板 pane 补真实花名。"""
    session_dirs = {
        str(item.get("session") or ""): str(item.get("directory") or "")
        for item in snapshot.get("sessions", [])
        if item.get("session") and item.get("directory")
    }
    projects: dict[str, str | None] = {}
    identities: dict[tuple[str, str, str], str | None] = {}
    descriptors: dict[tuple[str, str], dict[str, Any] | None] = {}
    legacy_counts: dict[tuple[str, str], int] = {}
    for pane in snapshot.get("panes", []):
        session = str(pane.get("session") or "")
        pane_id = str(pane.get("pane_id") or "")
        agent = str(pane.get("agent") or "")
        descriptor = herdr_client.get_launch_descriptor(session, pane_id)
        descriptors[(session, pane_id)] = descriptor
        if not descriptor and session and agent:
            key = (session, agent)
            legacy_counts[key] = legacy_counts.get(key, 0) + 1
    for pane in snapshot.get("panes", []):
        session = str(pane.get("session") or "")
        agent = str(pane.get("agent") or "")
        pane_id = str(pane.get("pane_id") or "")
        session_dir = session_dirs.get(session)
        if not agent or not session_dir:
            continue
        descriptor = descriptors.get((session, pane_id))
        instance_id = ""
        mail_agent = agent
        if descriptor and descriptor.get("instance_id"):
            instance_id = str(descriptor["instance_id"])
            mail_agent = str(descriptor.get("agent") or "")
            if not mail_agent:
                continue
            pane["instance_id"] = instance_id
            pane["display_name"] = descriptor.get("display_name") or agent
            pane["runtime_name"] = descriptor.get("name")
        elif legacy_counts.get((session, agent), 0) != 1:
            continue
        if session not in projects:
            try:
                projects[session] = mail_projects.get(session, session_dir)
            except (OSError, ValueError):
                projects[session] = None
        project = projects[session]
        if not project:
            continue
        key = (project, mail_agent, instance_id)
        if key not in identities:
            identities[key] = (
                _identity_name(project, mail_agent, instance_id)
                if instance_id else _identity_name(project, mail_agent)
            )
        if identities[key]:
            pane["mail_name"] = identities[key]
    return snapshot


def _board_snapshot() -> dict[str, Any]:
    return _enrich_board_identities(_herdr_runtime_snapshot())


def _identity_hint(
    name: str, project: str, agent_type: str, *, roster: str = "", registered: bool = False,
    coordination_context: dict[str, Any] | None = None,
    instance_id: str | None = None,
) -> str:
    send = shlex.quote(str(MAIL_SEND_SCRIPT))
    recv = shlex.quote(str(MAIL_RECV_SCRIPT))
    project_arg = shlex.quote(project)
    mail_agent = MAIL_AGENT_NAMES.get(agent_type, agent_type)
    mail_instance = instance_id or "main"
    if instance_id is not None:
        herdr_client.validate_agent_instance_id(instance_id)
    instance_arg = shlex.quote(mail_instance)
    prefix = (
        "[agent-mail 身份告知] 你的邮箱身份已注册:"
        if registered else "[agent-mail 身份告知] "
    )
    hint = (
        f"{prefix}花名={name},项目={project}。"
        f"发消息: {send} --agent {mail_agent} --instance {instance_arg} --project {project_arg} "
        "--to <花名> --subject \"...\" --body \"...\";"
        f"收消息: {recv} --agent {mail_agent} --instance {instance_arg} --project {project_arg} --unread。"
    )
    if roster:
        hint += f"协作者身份: {roster}。"
    if coordination_context:
        hint += (
            f"当前协作 run={coordination_context['run_id']},"
            f"task={coordination_context['participant_id']},"
            f"revision={coordination_context['task_revision']}。"
            "工作指令不按固定时间过期；仅 run/task/revision 失效或被 supersede 时作废。"
        )
    return f"{hint}{MAIL_COORDINATION_GUIDE}"


def _collaborator_hint(name: str, project: str) -> str:
    send = shlex.quote(str(MAIL_SEND_SCRIPT))
    project_arg = shlex.quote(project)
    return (
        f"协作者 {name} 已接入 agent-mail。"
        f"发消息: {send} --agent <你的类型> --instance main --project {project_arg} "
        f"--to {name} --subject \"...\" --body \"...\""
    )


def _agent_mail_status() -> dict[str, Any]:
    read_status = db.status()
    write_status = hub_client.status()
    return {
        **read_status,
        "read_available": read_status["available"],
        "write_available": write_status["available"],
        "write_reason": write_status["reason"],
    }


def _agent_mail_requirement() -> dict[str, Any] | None:
    """新工作区和新 Agent 必须连接可写的 Agent Mail Hub。"""
    status = _agent_mail_status()
    if status.get("write_available") is True:
        return None
    reason = (
        status.get("write_reason")
        or status.get("reason")
        or "Agent Mail Hub 不可写"
    )
    return {
        "ok": False,
        "code": "agent_mail_required",
        "error": f"Agent Mail 是创建工作区和添加 Agent 的必需能力：{reason}",
        "agent_mail": status,
    }


@app.middleware("http")
async def protect_api(request: Request, call_next):
    if not COCKPIT_TOKEN and (
        not _request_authenticated(request)
        or not _no_token_scope_trusted(request.scope, require_origin=False)
    ):
        path = str(request.scope.get("path") or "")
        if project_registry_api.is_scoped_registry_path(path):
            return project_registry_api.bridge_http_exception(request, 403, LOCAL_ONLY_AUTH_DETAIL)
        return JSONResponse({"detail": LOCAL_ONLY_AUTH_DETAIL}, status_code=403)
    path = str(request.scope.get("path") or "")
    protected = path.startswith("/api/") or path in {
        "/docs", "/redoc", "/openapi.json", "/health.poll",
    }
    # 窄豁免：仅持有效一次性 grant（头携带、scope 匹配、未用未过期）的
    # rebind 请求可绕过全局门；grant 在端点内原子消费
    parts = path.split("/")
    if (
        len(parts) == 6 and parts[1] == "api" and parts[2] == "binding"
        and parts[5] == "rebind"
        and _b0_scope_enabled(parts[3], parts[4])
        and _b0_valid_grant_for(request, parts[3], parts[4])
    ):
        return await call_next(request)
    if not protected or path in PUBLIC_PATHS:
        return await call_next(request)
    if not _request_authenticated(request):
        status = 401 if COCKPIT_TOKEN else 403
        detail = "未认证" if COCKPIT_TOKEN else LOCAL_ONLY_AUTH_DETAIL
        if project_registry_api.is_scoped_registry_path(path):
            return project_registry_api.bridge_http_exception(request, status, detail)
        return JSONResponse(
            {"detail": detail}, status_code=status,
            headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
        )
    cookie_auth = _valid_cookie(request.cookies.get(AUTH_COOKIE))
    if request.method not in SAFE_METHODS:
        # Token cookie 写请求继续执行既有同源 CSRF 校验。无 Token 模式
        # 已在全入口门校验 Host 和可选 Origin；缺 Origin 仅为本机 CLI 保留。
        origin = request.headers.get("origin")
        if COCKPIT_TOKEN and cookie_auth and not _valid_bearer(
            request.headers.get("authorization")
        ):
            if not _same_origin(origin, request.headers.get("host")):
                if project_registry_api.is_scoped_registry_path(path):
                    return project_registry_api.bridge_http_exception(request, 403, "Origin 校验失败")
                return JSONResponse({"detail": "Origin 校验失败"}, status_code=403)
    return await call_next(request)


# ── 请求模型 ────────────────────────────────────────────────────

class SendMessageReq(BaseModel):
    project_id: int
    sender_name: str  # agent 花名,如 zcode-main
    to: list[str]
    subject: str
    body: str = ""
    thread_id: str | None = None
    importance: str = "normal"
    ack_required: bool = False
    intent: str = "info"
    supersedes: list[int] = Field(default_factory=list)
    expires_in: float | None = None
    hard: bool = False
    run_independent: bool = False
    binding_scope_kind: str | None = None
    binding_scope_id: str | None = None


class HumanLoginReq(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class HumanRegistrationReq(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)
    invite_code: str = Field(min_length=1, max_length=256)


class HumanInvitationReq(BaseModel):
    expires_in: int = Field(default=24 * 60 * 60, ge=300, le=7 * 24 * 60 * 60)


class HumanUserStatusReq(BaseModel):
    status: str = Field(min_length=1, max_length=16)


class AckReq(BaseModel):
    project_id: int
    agent_name: str
    message_id: int


class MessageCleanupReq(BaseModel):
    project_id: int
    older_than_days: int = 30


class StartTaskReq(BaseModel):
    workdir: str
    prompt: str
    images: list[str] | None = None  # 已上传的文件 path 列表
    model: str | None = Field(default=None, pattern=tasks.MODEL_PATTERN)


class PaneSendReq(BaseModel):
    text: str
    mode: str = "send"  # send | prompt


class RebindReq(BaseModel):
    """B1 控制面改绑请求（ADR §2a 修订）：expected_version 必填 CAS。"""

    mail_name: str
    expected_version: int
    agent_name: str | None = None
    agent_kind: str | None = None
    session: str | None = None
    pane_id: str | None = None
    registry_selector: str | None = None
    caller_mail_name: str | None = None
    grant_token: str | None = None


class RebindGrantReq(BaseModel):
    mail_name: str
    ttl_seconds: int = Field(default=300, ge=10, le=3600)


class ZoomLeaseReq(BaseModel):
    term_id: str
    action: str = "acquire"  # acquire | renew | release


class PaneSplitLayoutReq(BaseModel):
    mode: str = "horizontal"  # horizontal | vertical | grid4


class TabUntileReq(BaseModel):
    tab_id: str


class PaneComposeReq(BaseModel):
    pane_ids: list[str]
    orientation: str = "horizontal"  # horizontal | vertical


class WriteFileReq(BaseModel):
    path: str
    content: str
    create: bool = False


class FileRootReq(BaseModel):
    path: str


class StartAgentReq(BaseModel):
    session: str
    workdir: str
    agent: str = "codex"  # codex | kimi | qodercli
    model: str | None = None
    name: str | None = None  # 用户可见显示名；可重复，不参与实例寻址
    layout: str = "tab"
    workspace: str = "shared"  # shared(兼容旧调用) | isolated(新建/复用 worktree)
    args: str = Field(default="", max_length=herdr_client.MAX_AGENT_ARGS_LENGTH)


class WorkspaceParticipantReq(BaseModel):
    """协作工作区里的一个 Agent。"""
    id: str = ""
    name: str = ""  # 用户可见显示名；可重复，不参与实例寻址
    agent: str
    role: str = "developer"
    task: str = ""
    workspace: str = "auto"
    review_target: str | None = None
    args: str = Field(default="", max_length=herdr_client.MAX_AGENT_ARGS_LENGTH)


class SetupWorkspaceReq(BaseModel):
    """一键工作区初始化:split pane + 启动 agent + 注册身份 + 通知。"""
    session: str
    workdir: str
    agents: list[str] = Field(default_factory=lambda: ["codex"])
    layout: str = "tab"  # tab(多页/不分割) | right(水平/左右) | down(垂直/上下)
    mode: str = "quick"
    participants: list[WorkspaceParticipantReq] | None = None


class InspectWorkspaceReq(BaseModel):
    workdir: str


class MailProjectReq(BaseModel):
    project: str | None = None
    replace: bool = False


class LoginReq(BaseModel):
    token: str


class PushKeysReq(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionReq(BaseModel):
    endpoint: str
    expirationTime: int | None = None
    keys: PushKeysReq


class AgentMailConfigReq(BaseModel):
    hub: str
    team_hub: str | None = None
    human_auth: str | None = None


class TeamSessionBindReq(BaseModel):
    session: str = Field(..., min_length=1, max_length=64)
    replace: bool = False


class _AssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssignmentCreateReq(_AssignmentRequest):
    assignment: str
    assignee: str
    expected_reply: str | None = None
    deadline: float | None = None

    @field_validator("deadline", mode="before")
    @classmethod
    def _strict_deadline(cls, value: Any) -> Any:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise ValueError("deadline 必须是有限 epoch 或 null")
        return value


class AssignmentPatchReq(_AssignmentRequest):
    status: str
    expected_version: StrictInt


class AssignmentCloseReq(_AssignmentRequest):
    expected_version: StrictInt


# ── 认证 ─────────────────────────────────────────────────────────

@app.get("/api/auth/status")
def api_auth_status(request: Request):
    return {
        "required": bool(COCKPIT_TOKEN),
        "authenticated": _request_authenticated(request),
        "local_only": not bool(COCKPIT_TOKEN),
    }


@app.post("/api/auth/login")
def api_auth_login(req: LoginReq, request: Request):
    if not COCKPIT_TOKEN:
        if not _is_loopback(request.client.host if request.client else None):
            raise HTTPException(403, LOCAL_ONLY_AUTH_DETAIL)
        return {"ok": True, "required": False}
    if not hmac.compare_digest(req.token, COCKPIT_TOKEN):
        raise HTTPException(401, "令牌错误")
    response = JSONResponse({"ok": True, "required": True})
    response.set_cookie(
        AUTH_COOKIE, _session_value(), httponly=True,
        secure=request.url.scheme == "https", samesite="strict", path="/",
    )
    return response


@app.post("/api/auth/logout")
def api_auth_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(AUTH_COOKIE, path="/")
    return response


# ── 数据/通信路由 ───────────────────────────────────────────────

@app.get("/api/overview")
def api_overview():
    """全局总览:项目列表 + 未读 + 统计。"""
    mail_status = _agent_mail_status()
    if mail_status["available"]:
        try:
            result = db.overview()
            result["agent_mail"] = mail_status
            return result
        except Exception as exc:
            write_available = mail_status["write_available"]
            write_reason = mail_status["write_reason"]
            mail_status = {
                "available": False,
                "reason": f"Agent Mail 查询失败: {exc}",
                "read_available": False,
                "write_available": write_available,
                "write_reason": write_reason,
            }
    return {
        "projects": [],
        "total_unread": 0,
        "total_projects": 0,
        "total_agents": 0,
        "agent_mail": mail_status,
    }


@app.get("/api/projects/{slug}")
def api_project(slug: str, limit: int = 50):
    """项目详情:agent 列表 + 消息流。

    limit: 最近消息条数，默认 50，上限 500（消息页全列表筛选用）。
    """
    mail_status = _agent_mail_status()
    if not mail_status["available"]:
        raise HTTPException(503, mail_status["reason"])
    proj = db.project_by_slug(slug)
    if not proj:
        raise HTTPException(404, f"项目不存在: {slug}")
    try:
        msg_limit = int(limit)
    except (TypeError, ValueError):
        msg_limit = 50
    msg_limit = max(1, min(msg_limit, 500))
    messages = [
        coordination.enrich_message(proj["human_key"], message)
        for message in db.recent_messages(proj["id"], limit=msg_limit)
    ]
    return {
        "project": proj,
        "agents": db.list_agents(proj["id"]),
        "messages": messages,
        "agent_mail": mail_status,
        "message_limit": msg_limit,
    }


@app.get("/api/projects/{slug}/workbench")
def api_project_workbench(slug: str):
    """聚合 Registry 证明的 legacy Project、Assignment 与 live session。"""
    try:
        return project_workbench_adapter.read_workbench(
            slug,
            mail_status_provider=_agent_mail_status,
            legacy_project_provider=db.project_by_slug,
            registry_provider=_project_workbench_registry,
            assignments_provider=coordination.list_assignments,
            runtime_snapshot_provider=_herdr_runtime_snapshot,
            live_binding_provider=mail_projects.get,
            observed_at=time.time(),
        )
    except project_workbench_adapter.WorkbenchReadError as exc:
        raise HTTPException(exc.status_code, exc.detail) from None


def _assignment_project_key(slug: str) -> str:
    project = db.project_by_slug(slug)
    if not project:
        raise HTTPException(404, f"项目不存在: {slug}")
    return str(Path(project["human_key"]).expanduser().resolve())


def _assignment_request(model: type[_AssignmentRequest], body: Any) -> Any:
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(400, "assignment 请求参数无效") from exc


def _project_assignment(project_key: str, assignment_id: str) -> dict[str, Any]:
    row = coordination.get_assignment(assignment_id)
    if row is None or row["project_key"] != project_key:
        raise HTTPException(404, f"任务不存在: {assignment_id}")
    return row


def _raise_assignment_error(
    exc: ValueError, assignment_id: str | None = None,
) -> None:
    message = str(exc)
    if "任务不存在" in message:
        raise HTTPException(404, message) from exc
    if "expected_version 冲突" in message or "任务已关闭" in message:
        detail: dict[str, Any] = {"message": message}
        current = coordination.get_assignment(assignment_id or "")
        if current is not None:
            detail.update(
                current_version=current["version"],
                current_status=current["status"],
            )
        raise HTTPException(409, detail) from exc
    raise HTTPException(400, message) from exc


@app.get("/api/coordination/projects/{slug}/assignments")
def api_coordination_assignments(
    slug: str,
    statuses: list[str] | None = Query(default=None),
    assignee: str | None = None,
):
    project_key = _assignment_project_key(slug)
    if statuses is not None and any(
        status not in coordination.ASSIGNMENT_STATUSES for status in statuses
    ):
        raise HTTPException(400, "statuses 包含非法 status")
    if assignee is not None:
        assignee = assignee.strip()
        if not assignee or len(assignee) > coordination.ASSIGNEE_TEXT_LIMIT:
            raise HTTPException(400, "assignee 必须非空且不超过 128 字符")
    try:
        return coordination.list_assignments(
            project_key, statuses=statuses, assignee=assignee,
        )
    except ValueError as exc:
        _raise_assignment_error(exc)


@app.post("/api/coordination/projects/{slug}/assignments")
def api_coordination_assignment_create(
    slug: str, body: Any = Body(...),
):
    project_key = _assignment_project_key(slug)
    req = _assignment_request(AssignmentCreateReq, body)
    try:
        return coordination.create_assignment(project_key=project_key, **req.model_dump())
    except ValueError as exc:
        _raise_assignment_error(exc)


@app.get("/api/coordination/projects/{slug}/assignments/{assignment_id}")
def api_coordination_assignment(slug: str, assignment_id: str):
    return _project_assignment(_assignment_project_key(slug), assignment_id)


@app.patch("/api/coordination/projects/{slug}/assignments/{assignment_id}")
def api_coordination_assignment_patch(
    slug: str, assignment_id: str, body: Any = Body(...),
):
    project_key = _assignment_project_key(slug)
    _project_assignment(project_key, assignment_id)
    req = _assignment_request(AssignmentPatchReq, body)
    if req.expected_version < 1:
        raise HTTPException(400, "expected_version 必须是正整数")
    try:
        return coordination.transition_assignment(
            assignment_id,
            to_status=req.status,
            expected_version=req.expected_version,
        )
    except ValueError as exc:
        _raise_assignment_error(exc, assignment_id)


@app.post("/api/coordination/projects/{slug}/assignments/{assignment_id}/close")
def api_coordination_assignment_close(
    slug: str, assignment_id: str, body: Any = Body(...),
):
    project_key = _assignment_project_key(slug)
    _project_assignment(project_key, assignment_id)
    req = _assignment_request(AssignmentCloseReq, body)
    if req.expected_version < 1:
        raise HTTPException(400, "expected_version 必须是正整数")
    try:
        return coordination.close_assignment(
            assignment_id, expected_version=req.expected_version,
        )
    except ValueError as exc:
        _raise_assignment_error(exc, assignment_id)


SESSION_AGENT_STATUSES = {"working", "blocked", "done", "idle", "unknown"}
SESSION_STATUS_PRIORITY = {"blocked": 4, "working": 3, "idle": 2, "done": 1, "empty": 0}


def _build_session_progress(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """按运行中的 Herdr session 聚合实时 agent 状态与协作任务。"""
    observed_ts = time.time()
    source_sessions = snapshot.get("sessions") or []
    sessions: dict[str, dict[str, Any]] = {
        str(item.get("session")): {
            "session": str(item.get("session")),
            "directory": str(item.get("directory") or ""),
            "panes": list(item.get("panes") or []),
        }
        for item in source_sessions
        if item.get("session")
    }
    # 兼容测试、旧调用方及 Herdr 返回不完整 session 列表的情况。
    for pane in snapshot.get("panes", []):
        session = str(pane.get("session") or "")
        if not session:
            continue
        row = sessions.setdefault(
            session, {"session": session, "directory": "", "panes": []}
        )
        if pane not in row["panes"]:
            row["panes"].append(pane)

    result: list[dict[str, Any]] = []
    for session, source in sessions.items():
        try:
            run = coordination.run_by_session(session)
        except Exception:
            logger.exception("session progress coordination query failed: %s", session)
            run = None
        try:
            reports = coordination.task_reports(session)
        except Exception:
            logger.exception("session task reports query failed: %s", session)
            reports = {}
        participants = list((run or {}).get("participants") or [])
        by_pane = {
            str(item.get("pane_id")): item
            for item in participants if item.get("pane_id")
        }
        by_agent: dict[str, list[dict[str, Any]]] = {}
        for item in participants:
            by_agent.setdefault(str(item.get("agent_type") or ""), []).append(item)
        used_participants: set[str] = set()
        agents: list[dict[str, Any]] = []
        for pane in source["panes"]:
            agent_type = str(pane.get("agent") or "")
            if not agent_type:
                continue
            pane_id = str(pane.get("pane_id") or "")
            participant = by_pane.get(pane_id)
            candidates = by_agent.get(agent_type, [])
            if participant is None and len(candidates) == 1:
                candidate_id = str(candidates[0].get("participant_id") or "")
                if candidate_id not in used_participants:
                    participant = candidates[0]
            if participant:
                used_participants.add(str(participant.get("participant_id") or ""))
            status = str(pane.get("agent_status") or "unknown")
            if status not in SESSION_AGENT_STATUSES:
                status = "unknown"
            agents.append({
                "agent": agent_type,
                "mail_name": (
                    (participant or {}).get("mail_name") or pane.get("mail_name")
                ),
                "participant_id": (participant or {}).get("participant_id"),
                "role": (participant or {}).get("role"),
                "task": (participant or {}).get("task_text"),
                "status": status,
                "pane_id": pane_id,
                "cwd": str(pane.get("cwd") or ""),
                "coordination_state": (participant or {}).get("state"),
                "task_revision": (participant or {}).get("task_revision"),
                "report": reports.get(pane_id),
            })

        summary = {
            status: sum(1 for agent in agents if agent["status"] == status)
            for status in ("working", "blocked", "done", "idle", "unknown")
        }
        total = len(agents)
        if summary["blocked"]:
            status = "blocked"
        elif summary["working"]:
            status = "working"
        elif summary["idle"] or summary["unknown"]:
            status = "idle"
        elif total:
            status = "done"
        else:
            status = "empty"
        result.append({
            "session": session,
            "generation": str((run or {}).get("run_id") or ""),
            "directory": source["directory"],
            "status": status,
            "progress": round(summary["done"] * 100 / total) if total else 0,
            "summary": summary,
            "agents": agents,
            "updated_ts": observed_ts,
        })

    return sorted(
        result,
        key=lambda item: (
            -SESSION_STATUS_PRIORITY[item["status"]], str(item["session"]).lower()
        ),
    )


def _build_attention(snapshot: dict[str, Any]) -> dict[str, Any]:
    """聚合 session 任务进度与需要用户介入的对象。"""
    sessions = _build_session_progress(snapshot)
    items: list[dict[str, Any]] = []
    for pane in snapshot.get("panes", []):
        if pane.get("agent") and pane.get("agent_status") == "blocked":
            session = str(pane.get("session") or "")
            pane_id = str(pane.get("pane_id") or "")
            items.append({
                "id": f"pane:{session}:{pane_id}",
                "kind": "pane_blocked",
                "priority": 100,
                "title": f"{pane['agent']} 等待你",
                "detail": " · ".join(
                    part for part in (session, pane.get("cwd_name") or "") if part
                ),
                "created_ts": None,
                "target": {
                    "view": "herdrflow",
                    "session": session,
                    "pane_id": pane_id,
                },
                "url": (
                    f"/#/attention/pane/{quote(session, safe='')}/"
                    f"{quote(pane_id, safe='')}"
                ),
            })

    try:
        task_rows = tasks.list_tasks(100)
    except Exception:
        logger.exception("attention task query failed")
        task_rows = []
    for task in task_rows:
        status = task.get("status")
        if not task.get("run_workdir") or status not in ("done", "failed"):
            continue
        task_id = str(task.get("id") or "")
        kind = "task_failed" if status == "failed" else "task_review"
        source_name = Path(str(task.get("workdir") or "")).name or "项目"
        items.append({
            "id": f"task:{task_id}:{status}",
            "kind": kind,
            "priority": 90 if status == "failed" else 70,
            "title": "后台任务失败" if status == "failed" else "代码改动待审",
            "detail": f"{source_name} · {'执行失败' if status == 'failed' else '隔离改动等待审查'}",
            "created_ts": task.get("created_ts"),
            "target": {"view": "attention", "task_id": task_id},
            "url": f"/#/attention/task/{quote(task_id, safe='')}",
        })

    mail_status = _agent_mail_status()
    mail_unread = 0
    if mail_status["available"]:
        try:
            mail_unread = db.global_unread_count()
        except Exception as exc:
            logger.exception("attention Agent Mail query failed")
            write_available = mail_status["write_available"]
            write_reason = mail_status["write_reason"]
            mail_status = {
                "available": False,
                "reason": f"Agent Mail 查询失败: {exc}",
                "read_available": False,
                "write_available": write_available,
                "write_reason": write_reason,
            }
    items.sort(
        key=lambda item: (
            item["priority"],
            float(item["created_ts"])
            if isinstance(item.get("created_ts"), (int, float))
            else 0,
        ),
        reverse=True,
    )
    return {
        "sessions": sessions,
        "items": items,
        "count": len(items),
        "mail_unread": mail_unread,
        "capabilities": {"agent_mail": mail_status},
    }


def _attention_changes(
    previous: set[str] | None, items: list[dict[str, Any]]
) -> tuple[set[str], list[dict[str, Any]]]:
    current = {str(item["id"]) for item in items}
    if previous is None:
        return current, []
    return current, [item for item in items if item["id"] not in previous]


@app.get("/api/attention")
def api_attention():
    return _build_attention(_board_snapshot())


def _task_report_prompt(
    session: str, pane_id: str, request_id: str,
) -> str:
    command = " ".join([
        shlex.quote(str(TASK_REPORT_SCRIPT)),
        "--session", shlex.quote(session),
        "--pane", shlex.quote(pane_id),
        "--request-id", shlex.quote(request_id),
    ])
    return (
        "[Agent Cockpit 非打断状态上报] 这不是停止或转向请求。"
        "请完成当前原子操作，在下一个安全检查点上报一次当前进度，然后继续原任务。"
        "执行下面的命令，并把示例内容替换为真实信息；progress 使用 0-100 整数，"
        "没有阻塞时 blocker 传空字符串：\n"
        f"{command} --progress 50 --summary \"已完成的里程碑\" "
        "--next \"下一步\" --blocker \"\""
    )


@app.post("/api/attention/refresh-reports")
def api_attention_refresh_reports():
    """按用户请求让当前 Agent 在安全检查点提交结构化进度。"""
    sessions = _build_session_progress(_board_snapshot())
    requested: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for session_row in sessions:
        session = str(session_row.get("session") or "")
        if not SESSION_NAME_RE.fullmatch(session):
            continue
        for agent in session_row.get("agents") or []:
            pane_id = str(agent.get("pane_id") or "")
            target = (session, pane_id)
            if not PANE_ID_RE.fullmatch(pane_id) or target in seen:
                continue
            seen.add(target)
            report = coordination.request_task_report(
                session=session,
                pane_id=pane_id,
                agent_type=str(agent.get("agent") or "unknown"),
                mail_name=agent.get("mail_name"),
            )
            request_id = str(report["request_id"])
            sent = herdr_client.pane_send(
                session, pane_id,
                _task_report_prompt(session, pane_id, request_id),
                "prompt",
            )
            error = sent.get("error")
            if sent.get("available", True) is False:
                error = error or "Herdr 不可用"
            row = {
                "session": session,
                "pane_id": pane_id,
                "agent": agent.get("agent"),
                "mail_name": agent.get("mail_name"),
            }
            if error:
                coordination.fail_task_report_request(
                    session, pane_id, request_id, str(error)
                )
                failed.append({**row, "error": str(error)})
            else:
                requested.append(row)
    return {
        "ok": not failed,
        "requested": len(requested),
        "failed": len(failed),
        "targets": requested,
        "failures": failed,
    }


@app.get("/api/push/config")
def api_push_config():
    return web_push.public_config()


@app.post("/api/push/subscriptions")
def api_push_subscribe(req: PushSubscriptionReq):
    try:
        return web_push.save_subscription(req.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/push/subscriptions")
def api_push_unsubscribe(endpoint: str):
    return {"ok": web_push.delete_subscription(endpoint)}


def _delivery_payloads(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    deliveries = result.get("deliveries") or []
    return [
        delivery.get("payload", {})
        for delivery in deliveries if isinstance(delivery, dict)
        and isinstance(delivery.get("payload"), dict)
    ]


def _b0_enabled_bindings() -> list[dict[str, Any]]:
    if not _b0_runtime_active():
        return []
    try:
        rows = leader_binding.list_bindings(issuer=B0_ISSUER, state="active")
    except Exception:
        logger.exception("b0 active bindings unavailable")
        raise HTTPException(503, "B0 binding 存储暂不可用")
    return [
        row for row in rows
        if _b0_scope_enabled(
            str(row.get("scope_kind") or ""),
            str(row.get("scope_id") or ""),
        )
    ]


def _b0_select_sender_binding(
    sender: str, scope_kind: str | None, scope_id: str | None,
) -> tuple[dict[str, Any] | None, int]:
    """按显式 scope 或唯一 canonical sender 选择 B0 binding。"""
    if not _b0_runtime_active():
        return None, 0
    if bool(scope_kind) != bool(scope_id):
        raise HTTPException(400, "binding_scope_kind 与 binding_scope_id 必须同时提供")
    rows = _b0_enabled_bindings()
    if scope_kind and scope_id:
        if scope_kind not in leader_binding.SCOPE_KINDS:
            raise HTTPException(400, "binding_scope_kind 无效")
        if not _b0_scope_enabled(scope_kind, scope_id):
            raise HTTPException(409, "B0 scope 未启用")
        selected = [
            row for row in rows
            if row.get("scope_kind") == scope_kind and row.get("scope_id") == scope_id
        ]
        if len(selected) != 1 or not hmac.compare_digest(
            sender, str(selected[0].get("mail_name") or "") if selected else "",
        ):
            raise HTTPException(403, "发送者不是该 scope 的 active canonical Leader")
        return selected[0], len(rows)
    matches = [
        row for row in rows
        if hmac.compare_digest(sender, str(row.get("mail_name") or ""))
    ]
    if len(matches) > 1:
        raise HTTPException(409, "canonical Leader 匹配多个 scope，必须显式选择")
    return (matches[0] if matches else None), len(rows)


def _b0_validate_control_metadata(
    meta: dict[str, Any], sender: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not _b0_runtime_active():
        return None, None
    issuer = str(meta.get("binding_issuer") or "")
    scope_kind = str(meta.get("binding_scope_kind") or "")
    scope_id = str(meta.get("binding_scope_id") or "")
    if not (issuer or scope_kind or scope_id):
        return None, None
    if (
        issuer != B0_ISSUER
        or scope_kind not in leader_binding.SCOPE_KINDS
        or not scope_id
        or not _b0_scope_enabled(scope_kind, scope_id)
    ):
        return None, b0_wiring.REASON_STALE_BINDING_VERSION
    try:
        row = leader_binding.get_active_binding(B0_ISSUER, scope_kind, scope_id)
        version = int(meta.get("binding_version"))
    except Exception:
        logger.exception("b0 control metadata validation failed")
        return None, b0_wiring.REASON_STALE_BINDING_VERSION
    if (
        not row
        or version != int(row.get("binding_version") or -1)
        or not sender
        or not hmac.compare_digest(sender, str(row.get("mail_name") or ""))
    ):
        return None, b0_wiring.REASON_STALE_BINDING_VERSION
    return row, None


def _notify_coordination_message(
    project_key: str, recipient: str, message_id: int, subject: str,
    meta: dict[str, Any], *, hard: bool, sender: str | None = None,
) -> dict[str, Any]:
    intent = str(meta.get("intent") or "info")
    # W6 canonical 授权门：控制动作必须先于任何中断（含 hard C-c）校验发送者
    canonical_note = ""
    if intent in coordination.NO_RESUME_INTENTS:
        binding, reason = _b0_validate_control_metadata(meta, sender)
        if reason:
            return {"notified": False, "reason": reason}
        if binding:
            canonical_note = (
                f" [canonical binding v{binding['binding_version']} "
                f"from={binding['mail_name']} "
                f"scope={binding['scope_kind']}/{binding['scope_id']}；"
                "与本地已知版本冲突时拒绝并报警]"
            )
    context = coordination.active_context(project_key, recipient)
    if not context or context.get("run_id") != meta.get("run_id"):
        return {"notified": False, "reason": "recipient_not_in_unique_active_run"}
    checkpoint = None
    if intent in coordination.INTERRUPT_INTENTS:
        checkpoint = coordination.request_pause(
            project_key=project_key, recipient=recipient, message_id=message_id,
            cwd=str(context["workdir"]), hard=hard,
        )
    session = str(context["session"])
    pane_id = str(context.get("pane_id") or "")
    if not pane_id:
        return {"notified": False, "reason": "pane_missing", "checkpoint": checkpoint}
    if hard:
        stopped = herdr_client.pane_send(session, pane_id, "C-c", "keys")
        if stopped.get("available", True) is False or stopped.get("error"):
            return {
                "notified": False, "reason": "hard_interrupt_failed",
                "detail": stopped, "checkpoint": checkpoint,
            }
    agent_type = MAIL_AGENT_NAMES.get(
        str(context["agent_type"]), str(context["agent_type"])
    )
    command = (
        f"{shlex.quote(str(MAIL_RECV_SCRIPT))} --agent {shlex.quote(agent_type)} "
        f"--instance main --project {shlex.quote(project_key)} --unread "
        f"--message {message_id}"
    )
    note = (
        f"[Agent Cockpit {'硬中断' if hard else '协作式打断'}] 消息 #{message_id} "
        f"intent={intent}「{subject[:60]}」。"
        f"基础 checkpoint 已保存；运行 {command} 领取。"
        "处理完成后按工具输出单条 complete；普通打断会自动校验 revision 并恢复，"
        "stop/redirect 不恢复旧任务。"
    ) + canonical_note
    sent = herdr_client.pane_send(session, pane_id, note, "prompt")
    return {
        "notified": not sent.get("error") and sent.get("available", True),
        "detail": sent, "checkpoint": checkpoint,
    }


@app.post("/api/send")
def api_send(req: SendMessageReq):
    """以某 agent 身份发消息(走 hub MCP 保证一致性)。"""
    mail_status = _agent_mail_status()
    if not mail_status["write_available"]:
        raise HTTPException(503, mail_status["write_reason"])
    proj = db.project_by_id(req.project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    sender = db.agent_by_name(req.project_id, req.sender_name)
    if not sender:
        raise HTTPException(404, f"发送身份不存在: {req.sender_name}")
    if req.hard and req.intent not in coordination.NO_RESUME_INTENTS:
        raise HTTPException(400, "硬中断仅允许 stop/redirect")
    selected_binding, enabled_binding_count = _b0_select_sender_binding(
        str(sender["name"]), req.binding_scope_kind, req.binding_scope_id,
    )
    if (
        req.intent in coordination.NO_RESUME_INTENTS
        and B0_MODE == "on"
        and enabled_binding_count
        and selected_binding is None
    ):
        raise HTTPException(403, "控制消息发送者不是 active canonical Leader")
    # cross_run_fail_fast（冻结合同）：发送端在 Hub 写入前失败，零半成功
    if selected_binding is not None:
        sender_ctx = coordination.active_context(proj["human_key"], sender["name"])
        try:
            b0_wiring.cross_run_fail_fast(
                proj["human_key"], sender["name"], list(req.to),
                sender_run_id=(sender_ctx or {}).get("run_id"),
                run_independent=req.run_independent,
            )
        except b0_wiring.CrossRunFailFast as exc:
            raise HTTPException(409, str(exc))
    try:
        meta, warnings = coordination.prepare_metadata(
            project_key=proj["human_key"], sender=sender["name"],
            recipients=req.to, intent=req.intent, importance=req.importance,
            authority="user", supersedes=req.supersedes,
            expires_in=req.expires_in,
        )
        # W6：控制消息持久化 canonical binding version（claim 服务端门依据）
        if (
            req.intent in coordination.NO_RESUME_INTENTS
            and selected_binding is not None
        ):
            meta.update({
                "binding_issuer": B0_ISSUER,
                "binding_scope_kind": selected_binding["scope_kind"],
                "binding_scope_id": selected_binding["scope_id"],
                "binding_version": int(selected_binding["binding_version"]),
            })
        body = coordination.add_metadata(req.body, meta)
        result = hub_client.send_message(
            project_key=proj["human_key"],
            sender_name=sender["name"],
            sender_token=sender["registration_token"],
            to=req.to,
            subject=req.subject,
            body_md=body,
            thread_id=req.thread_id,
            importance=req.importance,
            ack_required=req.ack_required,
        )
        notifications = []
        payloads = _delivery_payloads(result)
        if payloads and not hub_client.allows_local_actions():
            warnings.append(
                "共享 Hub 响应仅作为只读数据处理；未触发本地终端通知或协调状态变更"
            )
            payloads = []
        for payload in payloads:
            message_id = payload.get("id")
            if message_id is None:
                continue
            try:
                coordination.register_message(
                    project_key=proj["human_key"], message_id=int(message_id),
                    sender=sender["name"], meta=meta, trusted_user=True,
                )
            except Exception as exc:
                warnings.append(
                    f"消息 #{message_id} 已发送，但本地消费元数据登记失败: {exc}"
                )
                continue
            for recipient in payload.get("to") or req.to:
                try:
                    notification = _notify_coordination_message(
                        proj["human_key"], str(recipient), int(message_id),
                        req.subject, meta, hard=req.hard,
                        sender=sender["name"],
                    )
                except Exception as exc:
                    notification = {
                        "notified": False, "reason": "notify_failed",
                        "error": str(exc),
                    }
                    warnings.append(
                        f"消息 #{message_id} 已发送，但通知 {recipient} 失败: {exc}"
                    )
                notifications.append({
                    "recipient": recipient, **notification,
                })
        return {
            "ok": True, "result": result, "coordination": {
                "meta": meta, "warnings": warnings,
                "notifications": notifications,
            },
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"发送失败: {e}")


@app.post("/api/messages/cleanup")
def api_messages_cleanup(req: MessageCleanupReq):
    """删除某项目 N 天前的消息(经 Hub 删除接口,含 git 归档与级联)。"""
    if req.older_than_days < 1 or req.older_than_days > 3650:
        raise HTTPException(400, "older_than_days 须在 1-3650 之间")
    mail_status = _agent_mail_status()
    if not mail_status["read_available"]:
        raise HTTPException(503, mail_status["reason"])
    if not mail_status["write_available"]:
        raise HTTPException(503, mail_status["write_reason"])
    if not hub_client.allows_local_actions():
        raise HTTPException(409, "消息清理仅支持与本地数据库配套的本机 Agent Mail Hub")
    if not db.project_by_id(req.project_id):
        raise HTTPException(404, "项目不存在")
    rows = db._rows(
        "SELECT id FROM messages WHERE project_id = ? "
        "AND created_ts < datetime('now', ?)",
        (req.project_id, f"-{req.older_than_days} days"),
    )
    ids = [int(r["id"]) for r in rows]
    if not ids:
        return {"deleted": 0}
    deleted = 0
    # Hub 删除接口单批上限 500
    for i in range(0, len(ids), 500):
        batch = ids[i:i + 500]
        try:
            resp = httpx.post(
                f"{hub_client.HUB}/mail/api/delete-messages",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {hub_client.TOKEN}",
                },
                json={"message_ids": batch},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Hub 不可达: {exc.__class__.__name__}")
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"Hub 删除失败: {resp.text[:200]}")
        deleted += int(resp.json().get("deleted_count", 0))
    return {"deleted": deleted}


@app.post("/api/ack")
def api_ack(req: AckReq):
    """标记消息已读。"""
    mail_status = _agent_mail_status()
    if not mail_status["write_available"]:
        raise HTTPException(503, mail_status["write_reason"])
    proj = db.project_by_id(req.project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    agent = db.agent_by_name(req.project_id, req.agent_name)
    if not agent:
        raise HTTPException(404, f"agent 不存在: {req.agent_name}")
    coordination.dismiss_message(
        proj["human_key"], agent["name"], req.message_id
    )
    try:
        result = hub_client.acknowledge_message(
            project_key=proj["human_key"],
            agent_name=agent["name"],
            registration_token=agent["registration_token"],
            message_id=req.message_id,
        )
        coordination.mark_acked(proj["human_key"], agent["name"], req.message_id)
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(500, f"ack 失败: {e}")


# ── B1 控制面：Leader 改绑（ADR §2a 修订；与 team-auth session-bindings 正交） ──

_B0_GRANT_TTL_DEFAULT = 300.0
_B0_REBIND_GRANTS: dict[str, dict[str, Any]] = {}
_B0_REBIND_GRANTS_LOCK = threading.Lock()


def _b0_issue_grant(scope_kind: str, scope_id: str, mail_name: str, ttl: float) -> dict[str, Any]:
    token = secrets.token_hex(32)
    expires = time.time() + ttl
    with _B0_REBIND_GRANTS_LOCK:
        # 清理过期/已用 grant，防内存累积
        stale = [
            k for k, v in _B0_REBIND_GRANTS.items()
            if v["expires_ts"] < time.time() or v["used"]
        ]
        for k in stale:
            _B0_REBIND_GRANTS.pop(k, None)
        _B0_REBIND_GRANTS[token] = {
            "scope_kind": scope_kind, "scope_id": scope_id,
            "mail_name": mail_name, "expires_ts": expires, "used": False,
        }
    return {"grant_token": token, "expires_ts": expires, "single_use": True}


def _b0_consume_grant(
    token: str | None, scope_kind: str, scope_id: str, mail_name: str | None,
) -> bool:
    """原子消费一次性 grant：必须匹配 scope+mail_name、未过期、未使用。"""
    if not token or not mail_name:
        return False
    with _B0_REBIND_GRANTS_LOCK:
        grant = _B0_REBIND_GRANTS.get(token)
        if grant is None or grant["used"]:
            return False
        if grant["expires_ts"] < time.time():
            _B0_REBIND_GRANTS.pop(token, None)
            return False
        if (
            grant["scope_kind"] != scope_kind
            or grant["scope_id"] != scope_id
            or not hmac.compare_digest(grant["mail_name"], mail_name)
        ):
            return False
        grant["used"] = True
        return True


def _b0_valid_grant_for(request: Request, scope_kind: str, scope_id: str) -> bool:
    """中间件窄豁免前置：请求头必须携带与该 scope 匹配的有效未用 grant
    （消费发生在端点内；此处只判有效性）。"""
    token = (request.headers.get("x-b0-grant-token") or "").strip()
    if not token:
        return False
    with _B0_REBIND_GRANTS_LOCK:
        grant = _B0_REBIND_GRANTS.get(token)
        if grant is None or grant["used"] or grant["expires_ts"] < time.time():
            return False
        return grant["scope_kind"] == scope_kind and grant["scope_id"] == scope_id


def _b0_user_request(request: Request) -> bool:
    """用户判定：复用全局门的 _valid_bearer/_valid_cookie/loopback 语义；
    cookie 写请求附加同源 CSRF 校验。bogus cookie/非回环无 token 一律拒绝。"""
    if COCKPIT_TOKEN:
        if _valid_bearer(request.headers.get("authorization")):
            return True
        if _valid_cookie(request.cookies.get(AUTH_COOKIE)):
            origin = request.headers.get("origin")
            if origin and not _same_origin(origin, request.headers.get("host")):
                return False
            return True
        return False
    return _is_loopback(request.client.host if request.client else None)


@app.post("/api/binding/{scope_kind}/{scope_id}/rebind-grant")
def api_binding_rebind_grant(
    scope_kind: str, scope_id: str, req: RebindGrantReq, request: Request,
):
    if scope_kind not in leader_binding.SCOPE_KINDS:
        raise HTTPException(400, f"非法 scope_kind（允许 {leader_binding.SCOPE_KINDS}）")
    if not _b0_scope_enabled(scope_kind, scope_id):
        raise HTTPException(409, "B0 scope 未启用")
    if not _b0_user_request(request):
        raise HTTPException(403, "签发 grant 仅限 COCKPIT_TOKEN 用户")
    return _b0_issue_grant(scope_kind, scope_id, req.mail_name, float(req.ttl_seconds))


@app.post("/api/binding/{scope_kind}/{scope_id}/rebind")
def api_binding_rebind(scope_kind: str, scope_id: str, req: RebindReq, request: Request):
    if scope_kind not in leader_binding.SCOPE_KINDS:
        raise HTTPException(400, f"非法 scope_kind（允许 {leader_binding.SCOPE_KINDS}）")
    if not _b0_scope_enabled(scope_kind, scope_id):
        raise HTTPException(409, "B0 scope 未启用")
    try:
        active = leader_binding.get_active_binding(B0_ISSUER, scope_kind, scope_id)
    except leader_binding.BindingError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        logger.exception("rebind: read active binding failed")
        raise HTTPException(503, "binding 存储暂不可用")
    # 鉴权：用户（_valid_bearer/_valid_cookie/loopback，含 CSRF）放行；
    # 否则 active Leader 必须持一次性/有时效 grant（禁止长期 digest）
    if _b0_user_request(request):
        actor = "user"
    else:
        if (
            active is not None
            and req.caller_mail_name
            and hmac.compare_digest(
                str(req.caller_mail_name), str(active.get("mail_name") or ""),
            )
            and _b0_consume_grant(
                req.grant_token, scope_kind, scope_id, req.caller_mail_name,
            )
        ):
            actor = "active_leader"
        else:
            raise HTTPException(
                403, "改绑仅限 COCKPIT_TOKEN 用户或持一次性 grant 的 active Leader",
            )
    try:
        row = b0_wiring.perform_rebind(
            scope_kind, scope_id, issuer=B0_ISSUER,
            mail_name=req.mail_name, expected_version=req.expected_version,
            agent_name=req.agent_name, agent_kind=req.agent_kind,
            session=req.session, pane_id=req.pane_id,
            registry_selector=req.registry_selector,
        )
    except leader_binding.StaleVersionError as exc:
        # CAS 失败：零变更，保持旧 binding
        raise HTTPException(409, str(exc))
    except leader_binding.BindingError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        logger.exception("rebind failed")
        raise HTTPException(500, "改绑失败（零变更）")
    coord = _b0_get_coordinator()
    if coord is not None:
        try:
            coord.sync_bindings()
        except Exception:
            logger.exception("rebind: coordinator sync failed")
    return {
        "rebound": True,
        "actor": actor,
        "issuer": B0_ISSUER,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "mail_name": row.get("mail_name"),
        "binding_version": row.get("binding_version"),
        "route_epoch": row.get("route_epoch"),
        "previous_mail_name": row.get("previous_mail_name"),
    }


# ── 上传路由 ────────────────────────────────────────────────────

@app.post("/api/upload")
async def api_upload(file: UploadFile):
    """上传文件/图片,落盘返回路径(供 codex -i 或附件用)。"""
    try:
        return await uploads.save_upload_file(file.filename or "upload.bin", file)
    except uploads.UploadTooLarge as e:
        raise HTTPException(413, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/uploads")
def api_uploads():
    return uploads.list_uploads()


# ── 版本 / Release ──────────────────────────────────────────────

@app.get("/api/version")
def api_version(refresh: bool = Query(False)):
    """当前版本 + GitHub latest Release 状态（需 Cockpit 认证，非 PUBLIC）。

    refresh=true 绕过 6h 缓存；网络/解析失败时仍 200 且 status=unavailable。
    """
    return version.get_version_info(refresh=refresh)


@app.get("/api/upgrade/status")
def api_upgrade_status():
    """Pure-read V2 status when explicitly enabled; otherwise retain V1 retirement."""
    if not upgrade_service.is_enabled():
        return upgrade_core.retired_status()
    try:
        return upgrade_service.get_status()
    except Exception:
        raise HTTPException(503, {"error_code": "status_unavailable"})


_UPGRADE_ERROR_STATUS = {
    "upgrade_busy": 409,
    "already_current": 409,
    "request_invalid": 400,
    "controller_unavailable": 503,
    "trust_unavailable": 503,
    "release_unavailable": 503,
    "platform_unsupported": 503,
}


@app.post("/api/upgrade")
def api_upgrade_start():
    """Prepare and detach the signed V2 controller when explicitly enabled."""
    if not upgrade_service.is_enabled():
        return upgrade_core.retired_start_response()
    try:
        receipt = upgrade_service.start_latest()
    except upgrade_service.UpgradeServiceError as exc:
        status_code = _UPGRADE_ERROR_STATUS.get(exc.code, 500)
        code = exc.code if status_code != 500 else "upgrade_failed"
        raise HTTPException(status_code, {"error_code": code})
    return JSONResponse(status_code=202, content=receipt)


# ── 设置路由 ────────────────────────────────────────────────────

# ── 主题同步到 Herdr（Web 主题为单一真相源）────────────────────
# Web 切主题时把 Herdr 自身 UI 主题写进 config.toml 并热重载。
# light 必须用浅色内置名：solarized 是暗色，solarized-light 才是浅色。
HERDR_THEME_MAP = {"light": "solarized-light", "dark": "catppuccin"}
_THEME_REQUEST_LOCK = threading.Lock()
_THEME_EFFECT_LOCK = threading.Lock()
_THEME_GENERATION = 0
_THEME_CONFIG_DIRTY = False


def _theme_generation_is_current(generation: int) -> bool:
    with _THEME_REQUEST_LOCK:
        return generation == _THEME_GENERATION


class ThemeHerdrReq(BaseModel):
    mode: str


@app.post("/api/theme/herdr")
def api_theme_herdr(req: ThemeHerdrReq):
    """同步 Web 主题到 Herdr / agent。

    顺序（后台）：
      1) 条件 reload-config（仅 config 变更）
      2) Mode 2031 一次 → OpenCode 等跟宿主 light/dark mode（/themes 只是选择器）
      3) agent 原生 slash：Grok `/theme light|dark` 等

    前端只改 palette；禁止前端再 notify 双发 Mode 2031。
    """
    if req.mode not in HERDR_THEME_MAP:
        raise HTTPException(400, "mode 必须是 light 或 dark")
    mode = req.mode
    theme_name = HERDR_THEME_MAP[mode]
    global _THEME_CONFIG_DIRTY, _THEME_GENERATION
    with _THEME_REQUEST_LOCK:
        _THEME_GENERATION += 1
        generation = _THEME_GENERATION
        try:
            write = herdr_client.set_theme_for_web_mode(mode, name_override=theme_name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except OSError as exc:
            raise HTTPException(500, f"写入 herdr 配置失败: {exc}")
        herdr_client.set_web_theme_mode(mode)
        config_changed = bool(write.get("changed")) if isinstance(write, dict) else True
        _THEME_CONFIG_DIRTY = _THEME_CONFIG_DIRTY or config_changed
        reload_pending = _THEME_CONFIG_DIRTY

    reload_state: dict[str, Any] = {
        "ok": True, "scheduled": False, "skipped": not reload_pending,
    }
    agents_state: dict[str, Any] = {"scheduled": True}

    def _notify_mode_2031() -> list[str]:
        notified: list[str] = []
        try:
            for item in terminal.list_terms():
                tid = str(item.get("id") or "")
                if not tid or not item.get("alive") or not item.get("label"):
                    continue
                if terminal.set_color_scheme(tid, mode, notify=True):
                    notified.append(tid)
        except Exception:
            logger.exception("theme/herdr notify labeled terms failed")
        return notified

    def _bg_theme_side_effects() -> None:
        # 请求可并发到达：副作用串行化，并在每个阶段执行 latest-wins 检查。
        # 旧请求即使先启动，也不能在新请求之后反向覆盖 Agent 主题。
        with _THEME_EFFECT_LOCK:
            if not _theme_generation_is_current(generation):
                return
            global _THEME_CONFIG_DIRTY
            with _THEME_REQUEST_LOCK:
                if generation != _THEME_GENERATION:
                    return
                reload_required = _THEME_CONFIG_DIRTY
                _THEME_CONFIG_DIRTY = False
            if reload_required:
                try:
                    result = herdr_client.reload_config()
                    if not result.get("ok", False):
                        raise RuntimeError(str(result.get("errors") or "reload failed"))
                except Exception:
                    logger.exception("theme/herdr reload_config failed")
                    with _THEME_REQUEST_LOCK:
                        _THEME_CONFIG_DIRTY = True
            if not _theme_generation_is_current(generation):
                return
            try:
                _notify_mode_2031()
            except Exception:
                logger.exception("theme/herdr Mode 2031 failed")
            if not _theme_generation_is_current(generation):
                return
            try:
                agents = herdr_client.apply_agent_web_themes(mode)
                if not agents.get("ok", False):
                    logger.warning("theme/herdr agent sync incomplete: %s", agents)
            except Exception:
                logger.exception("theme/herdr apply_agent_web_themes failed")

    try:
        if reload_pending:
            reload_state["scheduled"] = True
        threading.Thread(
            target=_bg_theme_side_effects, name="herdr-theme-side", daemon=True,
        ).start()
    except Exception:
        logger.exception("theme/herdr schedule side effects failed")
        _bg_theme_side_effects()

    return {
        "ok": True,
        "theme": theme_name,
        "generation": generation,
        "config_changed": config_changed,
        "reload": reload_state,
        "notified_terms": [],
        "agents": agents_state,
    }


@app.get("/api/settings")
def api_settings_get():
    """读用户配置(附 known agent 类型与语言枚举,供设置页渲染)。"""
    return {
        **settings.get(),
        "known_agents": settings.KNOWN_AGENTS,
        "languages": settings.LANGUAGES,
    }


@app.put("/api/settings")
async def api_settings_put(request: Request):
    """更新用户配置(一层合并)。校验失败 400。"""
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("设置请求必须是 JSON 对象")
        return settings.update(body)
    except (ValueError, UnicodeError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/agent-mail/config")
def api_agent_mail_config_get():
    """返回可公开给设置页的 Hub 配置；token 永不进入响应。"""
    config = hub_client.reload_config()
    return {
        "hub": config["hub"],
        **hub_client.public_team_config(),
        "status": hub_client.status(),
    }


@app.put("/api/agent-mail/config")
def api_agent_mail_config_put(req: AgentMailConfigReq):
    """保存本地/团队 Hub 地址并立即生效；任何凭据都不经该接口返回。"""
    try:
        if (req.team_hub is None) != (req.human_auth is None):
            raise ValueError("Team Hub 与 Human issuer 地址必须同时填写")
        team_config = None
        if req.team_hub is not None and req.human_auth is not None:
            team_config = hub_client.normalize_team_config(
                req.team_hub, req.human_auth,
            )
        hub_client.save_client_hub(req.hub)
        if team_config:
            settings.update(team_config)
        config = hub_client.reload_config()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "hub": config["hub"],
        **hub_client.public_team_config(),
        "status": hub_client.status(),
    }


@app.api_route(
    "/api/team/{route:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def api_team_proxy(route: str, request: Request):
    """白名单代理 Hub Human API；返回数据不得触发任何本地执行。"""
    method = request.method.upper()
    normalized = route.strip("/")
    if not any(
        pattern.fullmatch(normalized) and method in methods
        for pattern, methods in TEAM_API_ROUTES
    ):
        raise HTTPException(404, "不支持的 Hub Human API 路由")
    token = request.cookies.get(TEAM_AUTH_COOKIE, "")
    if not token or len(token) > 8192:
        raise HTTPException(401, "需要有效的 Hub Human JWT")
    authorization = f"Bearer {token}"
    payload = None
    if method != "GET":
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(400, "请求体必须是 JSON 对象") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "请求体必须是 JSON 对象")
    try:
        # Re-check the issuer on every Human API request so a disabled account
        # loses Cockpit access immediately instead of waiting for JWT expiry.
        hub_client.human_profile(authorization)
        return hub_client.human_api(
            method,
            f"/hub/api/{normalized}",
            authorization,
            payload,
        )
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    except hub_client.HumanAuthError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except hub_client.HumanAPIError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@app.post("/api/team-auth/login")
def api_team_auth_login(req: HumanLoginReq, request: Request):
    """窄代理独立 issuer；Cockpit 不持有签名密钥或用户密码。"""
    try:
        data = hub_client.human_login(req.username, req.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except hub_client.HumanAuthError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    token = data.get("access_token")
    profile = data.get("profile")
    expires_in = data.get("expires_in")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 8192
        or not isinstance(profile, dict)
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or not 1 <= expires_in <= 7 * 24 * 60 * 60
    ):
        raise HTTPException(502, "Human issuer 返回了无效响应")
    response = JSONResponse({"authenticated": True, "profile": profile})
    response.set_cookie(
        TEAM_AUTH_COOKIE,
        token,
        max_age=expires_in,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/api",
    )
    return response


def _team_human_authorization(request: Request) -> str:
    token = request.cookies.get(TEAM_AUTH_COOKIE, "")
    if not token or len(token) > 8192:
        raise HTTPException(401, "团队账号未登录或登录已过期")
    return f"Bearer {token}"


def _raise_human_auth_error(exc: hub_client.HumanAuthError) -> None:
    raise HTTPException(exc.status_code, exc.detail) from exc


_REGISTRY_ROOT = Path.home() / ".agent-mail" / "registry"
_REGISTRY_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REGISTRY_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}--[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.json$")
_REGISTRY_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}--[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.json$"
)
_REGISTRY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _registry_identity_id(project_dir_name: str, filename: str) -> str:
    return f"{project_dir_name}/{filename}"


def _registry_scan() -> list[dict[str, Any]]:
    """安全扫描 registry，返回每条（安全摘要 + identity_id + 完整 identity）。

    内部函数：调用方自行决定暴露哪些字段。跳过权限过宽、坏 JSON、
    symlink/越界或 owner 不匹配的文件。
    """
    root = _REGISTRY_ROOT
    if not root.is_dir():
        return []
    try:
        resolved_root = root.resolve()
    except OSError:
        return []
    result: list[dict[str, Any]] = []
    for project_dir in sorted(p for p in resolved_root.iterdir() if p.is_dir()):
        if not _REGISTRY_DIR_RE.fullmatch(project_dir.name) or project_dir.is_symlink():
            continue
        for entry in sorted(project_dir.iterdir()):
            if not _REGISTRY_FILE_RE.fullmatch(entry.name):
                continue
            identity = _read_registry_entry(entry, resolved_root)
            if identity is None:
                continue
            identity["identity_id"] = _registry_identity_id(project_dir.name, entry.name)
            result.append(identity)
    return result


def _registry_identities() -> list[dict[str, Any]]:
    """安全读取 ~/.agent-mail/registry 下的身份摘要。

    只返回 name/identity_id/project_slug/hub/program/model 等安全字段，绝不
    包含 registration_token；跳过权限过宽、坏 JSON、symlink/越界或 owner
    不匹配的文件。Hub 与当前 Team Hub 不匹配时返回 eligible=false +
    reason=hub_mismatch，而不是静默过滤，让页面可以明确提示用户。
    """
    team_hub = hub_client.public_team_config()["team_hub"]
    result: list[dict[str, Any]] = []
    for identity in _registry_scan():
        summary = {
            "identity_id": identity["identity_id"],
            "name": identity["name"],
            "project_slug": identity.get("project_slug"),
            "program": identity.get("program"),
            "model": identity.get("model"),
            "hub": identity["hub"],
        }
        if identity.get("hub") == team_hub:
            summary["eligible"] = True
        else:
            summary["eligible"] = False
            summary["reason"] = "hub_mismatch"
        result.append(summary)
    return result


def _read_registry_entry(entry: Path, resolved_root: Path) -> dict[str, Any] | None:
    """读取单个 registry 文件并校验安全性；返回完整 identity（含 token）。"""
    try:
        if entry.is_symlink():
            return None
        resolved = entry.resolve(strict=False)
        resolved.relative_to(resolved_root)
        if not resolved.is_file():
            return None
        stat = resolved.stat()
        mode = stat.st_mode & 0o777
        if mode != 0o600:
            return None
        if hasattr(os, "geteuid") and stat.st_uid != os.geteuid():
            return None
        data = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        scoped_project = next_profile.project()
        if scoped_project is not None and data.get("project_key") != scoped_project:
            return None
        hub = data.get("hub")
        if not isinstance(hub, str) or not hub:
            return None
        norm_hub = settings.normalize_service_url(hub, "Hub")
        name = data.get("name")
        if not isinstance(name, str) or not _REGISTRY_NAME_RE.fullmatch(name):
            return None
        project_slug = data.get("project_slug")
        if not isinstance(project_slug, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", project_slug
        ):
            return None
        token = data.get("registration_token")
        if not isinstance(token, str) or not token:
            return None
        data["name"] = name
        data["project_slug"] = project_slug
        data["hub"] = norm_hub
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _registry_identity(identity_id: str) -> dict[str, Any] | None:
    """按 identity_id（project-dir/filename 白名单）返回完整 identity。"""
    if not _REGISTRY_ID_RE.fullmatch(identity_id):
        return None
    project_dir_name, filename = identity_id.split("/", 1)
    root = _REGISTRY_ROOT
    if not root.is_dir():
        return None
    try:
        resolved_root = root.resolve()
    except OSError:
        return None
    project_dir = resolved_root / project_dir_name
    if (
        not project_dir.is_dir()
        or project_dir.is_symlink()
        or not _REGISTRY_DIR_RE.fullmatch(project_dir_name)
    ):
        return None
    entry = project_dir / filename
    if not _REGISTRY_FILE_RE.fullmatch(entry.name):
        return None
    identity = _read_registry_entry(entry, resolved_root)
    if identity is None:
        return None
    identity["identity_id"] = identity_id
    return identity


@app.get("/api/team-auth/local-identities")
def api_team_local_identities(request: Request):
    """返回本机 registry 中与当前 Team Hub 匹配的身份安全摘要。"""
    try:
        hub_client.human_profile(_team_human_authorization(request))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except hub_client.HumanAuthError as exc:
        _raise_human_auth_error(exc)
    return {"identities": _registry_identities()}


def _team_session_candidates() -> list[dict[str, Any]]:
    """只把运行中 Session 的唯一负责人作为 Team 绑定候选。"""
    identities = _registry_scan()
    result: list[dict[str, Any]] = []
    for session in _build_session_progress(_board_snapshot()):
        leads = [agent for agent in session["agents"] if agent.get("role") == "lead"]
        item: dict[str, Any] = {
            "session": session["session"],
            "generation": session["generation"],
            "directory": session["directory"],
            "status": session["status"],
            "agent_count": len(session["agents"]),
            "lead": None,
            "ready": False,
            "reason": None,
        }
        if len(leads) != 1:
            item["reason"] = "负责人未配置" if not leads else "存在多个负责人"
            result.append(item)
            continue
        if not item["generation"]:
            item["reason"] = "Session 尚未建立协作运行"
            result.append(item)
            continue
        lead = {
            key: leads[0].get(key)
            for key in ("pane_id", "agent", "mail_name", "participant_id", "status")
        }
        item["lead"] = lead
        if not lead.get("mail_name"):
            item["reason"] = "负责人尚未注册 Agent Mail 身份"
            result.append(item)
            continue
        try:
            state = _mail_project_state(str(session["session"]))
        except Exception:
            logger.exception("Team Session 读取通信项目失败: %s", session["session"])
            item["reason"] = "负责人通信项目不可用"
            result.append(item)
            continue
        project = state.get("project") if state.get("bound") else None
        if not project:
            item["reason"] = "Session 尚未选择 Agent Mail 通信项目"
            result.append(item)
            continue
        lead_agent = MAIL_AGENT_NAMES.get(str(lead.get("agent") or ""), str(lead.get("agent") or ""))
        matches = [
            identity for identity in identities
            if identity.get("project_key") == project
            and identity.get("name") == lead.get("mail_name")
            and MAIL_AGENT_NAMES.get(
                str(identity.get("agent") or ""), str(identity.get("agent") or "")
            ) == lead_agent
        ]
        if len(matches) != 1:
            item["reason"] = "负责人本机身份缺失或不唯一"
            result.append(item)
            continue
        item["ready"] = True
        item["mail_project"] = project
        result.append(item)
    return result


def _team_client_session_id(session: str, generation: str) -> str:
    """给 Team Hub 的不透明 Session 代际标识，不暴露本机名称或路径。"""
    return hashlib.sha256(f"{session}\0{generation}".encode()).hexdigest()


def _team_session_lead_payload(row: dict[str, Any]) -> dict[str, Any]:
    lead = row.get("lead") if isinstance(row.get("lead"), dict) else {}
    client_id = str(row.get("client_session_id") or "")
    if not client_id:
        client_id = _team_client_session_id(
            str(row.get("session") or ""),
            str(row.get("session_generation") or row.get("generation") or ""),
        )
    payload: dict[str, Any] = {
        "client_session_id": client_id,
        "lead_label": str(lead.get("mail_name") or lead.get("agent") or "Session lead")[:128],
    }
    if row.get("rotate_reply_token") is True:
        payload["rotate_reply_token"] = True
    return payload


def _team_remote_session_lead(
    authorization: str, project_slug: str, row: dict[str, Any],
) -> dict[str, Any]:
    try:
        result = hub_client.human_api(
            "PUT",
            f"/hub/api/projects/{quote(project_slug, safe='')}/session-lead",
            authorization,
            _team_session_lead_payload(row),
        )
    except hub_client.HumanAPIError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    if not isinstance(result, dict):
        raise HTTPException(502, "Team Hub 返回了无效 Session 负责人")
    return result


def _team_remote_session_unbind(
    authorization: str, project_slug: str, client_session_id: str,
) -> None:
    try:
        hub_client.human_api(
            "DELETE",
            f"/hub/api/projects/{quote(project_slug, safe='')}/session-lead",
            authorization,
            {"client_session_id": client_session_id},
        )
    except hub_client.HumanAPIError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def _team_remote_reply_token(remote: dict[str, Any]) -> str | None:
    token = remote.get("reply_token")
    if token is None:
        return None
    if not isinstance(token, str) or not token or len(token) > 128:
        raise HTTPException(502, "Team Hub 返回了无效回复凭据")
    return token


def _team_restore_session_routes(
    authorization: str,
    hub: str,
    human_id: int,
    rows: list[dict[str, Any]],
) -> None:
    """补偿恢复已解绑路由，并立即替换本机已失效 capability。"""
    for row in rows:
        try:
            remote = _team_remote_session_lead(
                authorization,
                str(row["project_slug"]),
                {**row, "rotate_reply_token": True},
            )
            reply_token = _team_remote_reply_token(remote)
            if reply_token is None:
                raise HTTPException(502, "Team Hub 未签发回复凭据")
            team_sessions.update_reply_token(
                hub=hub,
                human_id=human_id,
                project_slug=str(row["project_slug"]),
                client_session_id=str(row["client_session_id"]),
                reply_token=reply_token,
            )
        except Exception:
            logger.exception("Team Session 改绑失败后的旧路由恢复失败")


def _team_human_context(request: Request) -> tuple[str, dict[str, Any]]:
    authorization = _team_human_authorization(request)
    try:
        human = hub_client.human_api("GET", "/hub/api/humans/me", authorization)
    except hub_client.HumanAPIError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    if (
        not isinstance(human, dict)
        or isinstance(human.get("id"), bool)
        or not isinstance(human.get("id"), int)
    ):
        raise HTTPException(502, "Team Hub 返回了无效 Human 身份")
    return authorization, human


def _team_project_slug(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise HTTPException(400, "project_slug 无效")
    return value


def _public_team_session_candidate(row: dict[str, Any]) -> dict[str, Any]:
    lead = row.get("lead") if isinstance(row.get("lead"), dict) else None
    return {
        "session": row.get("session"),
        "status": row.get("status"),
        "agent_count": row.get("agent_count"),
        "lead": ({
            "agent": lead.get("agent"),
            "mail_name": lead.get("mail_name"),
            "status": lead.get("status"),
        } if lead else None),
        "ready": bool(row.get("ready")),
        "reason": row.get("reason"),
    }


def _public_team_session_binding(
    row: dict[str, Any], candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate = next((
        item for item in candidates
        if item.get("session") == row.get("session")
        and item.get("generation") == row.get("session_generation")
    ), None)
    same_name = next((
        item for item in candidates if item.get("session") == row.get("session")
    ), None)
    lead = row.get("lead") if isinstance(row.get("lead"), dict) else {}
    active = candidate is not None
    has_reply_capability = bool(
        isinstance(row.get("reply_token"), str) and row.get("reply_token")
    )
    reason = None
    if candidate is not None and not candidate.get("ready"):
        reason = candidate.get("reason")
    elif candidate is not None and not has_reply_capability:
        reason = "负责人通信凭据需要重新同步"
    elif candidate is None and same_name is not None:
        reason = "Session 已重建，需要重新绑定"
    elif candidate is None:
        reason = "Session 已停止"
    return {
        "project_slug": row.get("project_slug"),
        "session": row.get("session"),
        "lead": {
            "agent": lead.get("agent"),
            "mail_name": lead.get("mail_name"),
        },
        "agent_id": row.get("agent_id"),
        "active": active,
        "ready": bool(
            active and candidate and candidate.get("ready")
            and has_reply_capability
        ),
        "reason": reason,
        "updated_ts": row.get("updated_ts"),
    }


def _team_session_bindings_for(hub: str, human_id: int) -> list[dict[str, Any]]:
    try:
        return team_sessions.list_bindings(hub, human_id)
    except OSError as exc:
        logger.exception("本机 Session 绑定状态读取失败")
        raise HTTPException(500, "本机 Session 绑定状态不可用") from exc


@app.get("/api/team-auth/session-bindings")
def api_team_session_bindings(request: Request):
    """返回当前 Human 的本机 Session 候选和已绑定 TeamProject。"""
    _, human = _team_human_context(request)
    hub = hub_client.public_team_config()["team_hub"]
    candidates = _team_session_candidates()
    bindings = _team_session_bindings_for(hub, int(human["id"]))
    return {
        "sessions": [_public_team_session_candidate(row) for row in candidates],
        "bindings": [
            _public_team_session_binding(row, candidates) for row in bindings
        ],
    }


@app.put("/api/team-auth/session-bindings/{project_slug}")
def api_team_session_bind(
    project_slug: str, req: TeamSessionBindReq, request: Request,
):
    """选择 Session 后在 Team Hub 创建受管出口并保存本机负责人路由。"""
    slug = _team_project_slug(project_slug)
    _validate_session_name(req.session)
    authorization, human = _team_human_context(request)
    hub = hub_client.public_team_config()["team_hub"]
    with _TEAM_SESSION_BIND_LOCK:
        try:
            membership = hub_client.human_api(
                "GET",
                f"/hub/api/projects/{quote(slug, safe='')}/membership",
                authorization,
            )
        except hub_client.HumanAPIError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        if not isinstance(membership, dict) or membership.get("status") != "active":
            raise HTTPException(403, "只有项目 active 成员可以绑定本机 Session")
        candidate = next(
            (
                row for row in _team_session_candidates()
                if row["session"] == req.session
            ),
            None,
        )
        if candidate is None:
            raise HTTPException(404, "本机 Session 不存在或已停止")
        if not candidate.get("ready") or not candidate.get("mail_project"):
            raise HTTPException(
                409, str(candidate.get("reason") or "Session 负责人不可用")
            )
        bindings = _team_session_bindings_for(hub, int(human["id"]))
        current = next((
            row for row in bindings
            if row.get("project_slug") == slug
            and row.get("session") == req.session
            and row.get("session_generation") == str(candidate["generation"])
        ), None)
        try:
            conflicts = team_sessions.conflicts_for(
                hub=hub,
                human_id=int(human["id"]),
                project_slug=slug,
                session=req.session,
                session_generation=str(candidate["generation"]),
            )
        except OSError as exc:
            logger.exception("本机 Session 绑定状态读取失败")
            raise HTTPException(500, "本机 Session 绑定状态不可用") from exc
        if conflicts and not req.replace:
            raise HTTPException(409, "Session 或团队项目已有绑定，改绑需要显式确认")

        client_session_id = _team_client_session_id(
            req.session, str(candidate["generation"]),
        )
        deactivated: list[dict[str, Any]] = []
        try:
            for conflict in conflicts:
                _team_remote_session_unbind(
                    authorization,
                    str(conflict["project_slug"]),
                    str(conflict["client_session_id"]),
                )
                deactivated.append(conflict)
            remote = _team_remote_session_lead(
                authorization,
                slug,
                {
                    **candidate,
                    "client_session_id": client_session_id,
                    "rotate_reply_token": not bool(
                        isinstance((current or {}).get("reply_token"), str)
                        and (current or {}).get("reply_token")
                    ),
                },
            )
        except Exception:
            _team_restore_session_routes(
                authorization, hub, int(human["id"]), deactivated,
            )
            raise
        agent = remote.get("agent") if isinstance(remote.get("agent"), dict) else None
        agent_id = agent.get("id") if isinstance(agent, dict) else None
        reply_token = _team_remote_reply_token(remote)
        current_reply_token = (current or {}).get("reply_token")
        if (
            isinstance(agent_id, bool)
            or not isinstance(agent_id, int)
            or agent_id <= 0
            or (
                reply_token is None
                and not (
                    isinstance(current_reply_token, str)
                    and bool(current_reply_token)
                )
            )
        ):
            try:
                _team_remote_session_unbind(authorization, slug, client_session_id)
            except Exception:
                logger.exception("Team Hub 无效响应后的新路由清理失败")
            _team_restore_session_routes(
                authorization, hub, int(human["id"]), deactivated,
            )
            raise HTTPException(502, "Team Hub 返回了无效负责人 Agent")
        try:
            binding = team_sessions.bind(
                hub=hub,
                human_id=int(human["id"]),
                project_slug=slug,
                session=req.session,
                session_generation=str(candidate["generation"]),
                session_dir=str(candidate["directory"]),
                mail_project=str(candidate["mail_project"]),
                lead={key: str((candidate["lead"] or {}).get(key) or "") for key in (
                    "pane_id", "agent", "mail_name", "participant_id"
                )},
                client_session_id=client_session_id,
                agent_id=agent_id,
                reply_token=reply_token,
                replace=req.replace,
            )
        except (ValueError, OSError) as exc:
            try:
                _team_remote_session_unbind(authorization, slug, client_session_id)
            except Exception:
                logger.exception("本地 Session 绑定写入失败后的 Hub 补偿失败")
            _team_restore_session_routes(
                authorization, hub, int(human["id"]), deactivated,
            )
            if isinstance(exc, ValueError):
                raise HTTPException(409, str(exc)) from exc
            raise HTTPException(500, "本机 Session 绑定保存失败") from exc
    return {
        "ok": True,
        "binding": _public_team_session_binding(binding, [candidate]),
    }


@app.delete("/api/team-auth/session-bindings/{project_slug}")
def api_team_session_unbind(project_slug: str, request: Request):
    """解除本机 Session 路由；不停止 Session、不删除 Agent 或历史消息。"""
    slug = _team_project_slug(project_slug)
    authorization, human = _team_human_context(request)
    hub = hub_client.public_team_config()["team_hub"]
    with _TEAM_SESSION_BIND_LOCK:
        current = next((
            row for row in _team_session_bindings_for(hub, int(human["id"]))
            if row.get("project_slug") == slug
        ), None)
        if current is None:
            return {"ok": True, "removed": False}
        _team_remote_session_unbind(
            authorization, slug, str(current.get("client_session_id") or ""),
        )
        try:
            removed = team_sessions.unbind_project(hub, int(human["id"]), slug)
        except OSError as exc:
            try:
                remote = _team_remote_session_lead(
                    authorization, slug,
                    {**current, "rotate_reply_token": True},
                )
                reply_token = _team_remote_reply_token(remote)
                if reply_token is None:
                    raise HTTPException(502, "Team Hub 未签发回复凭据")
                team_sessions.update_reply_token(
                    hub=hub,
                    human_id=int(human["id"]),
                    project_slug=slug,
                    client_session_id=str(current["client_session_id"]),
                    reply_token=reply_token,
                )
            except Exception:
                logger.exception("本地 Session 解绑失败后的 Hub 补偿失败")
            raise HTTPException(500, "本机 Session 解绑保存失败") from exc
    return {"ok": True, "removed": bool(removed)}


def _team_agent_reply_forbidden() -> HTTPException:
    return HTTPException(403, "Invalid reply credentials")


def _team_agent_reply_binding(
    mail_project: str,
    sender_name: str,
    registration_token: str,
) -> dict[str, Any]:
    """用本机 registry token 证明调用者就是 active Session 的唯一 lead。"""
    try:
        project = str(Path(mail_project).expanduser().resolve())
    except (OSError, ValueError):
        raise _team_agent_reply_forbidden()
    identities = [
        identity for identity in _registry_scan()
        if identity.get("project_key") == project
        and identity.get("name") == sender_name
        and isinstance(identity.get("registration_token"), str)
    ]
    if len(identities) != 1:
        raise _team_agent_reply_forbidden()
    try:
        valid_identity = hmac.compare_digest(
            str(identities[0]["registration_token"]), registration_token,
        )
    except TypeError:
        valid_identity = False
    if not valid_identity:
        raise _team_agent_reply_forbidden()
    try:
        bindings = team_sessions.reply_bindings_for_lead(project, sender_name)
    except (OSError, ValueError):
        raise _team_agent_reply_forbidden()
    if len(bindings) != 1:
        raise _team_agent_reply_forbidden()
    binding = bindings[0]
    if binding.get("hub") != hub_client.public_team_config()["team_hub"]:
        raise _team_agent_reply_forbidden()
    candidates = [
        candidate for candidate in _team_session_candidates()
        if candidate.get("session") == binding.get("session")
        and candidate.get("generation") == binding.get("session_generation")
        and candidate.get("mail_project") == project
        and candidate.get("ready") is True
        and isinstance(candidate.get("lead"), dict)
        and candidate["lead"].get("mail_name") == sender_name
    ]
    if len(candidates) != 1:
        raise _team_agent_reply_forbidden()
    return binding


@app.post("/api/agent/team-reply")
async def api_team_agent_reply(request: Request):
    """Agent 本机验证后，以 Session lead capability 代理远端 Human 回复。"""
    if not _is_loopback(request.client.host if request.client else None):
        raise _team_agent_reply_forbidden()
    try:
        body = await request.json()
    except (UnicodeError, ValueError):
        raise HTTPException(400, "团队回复请求无效")
    if not isinstance(body, dict):
        raise HTTPException(400, "团队回复请求无效")
    allowed = {
        "mail_project", "sender_name", "registration_token", "mention_handles",
        "subject", "body_md", "importance", "idempotency_key",
    }
    if set(body) - allowed:
        raise HTTPException(400, "团队回复请求包含未支持字段")
    mail_project = body.get("mail_project")
    sender_name = body.get("sender_name")
    registration_token = body.get("registration_token")
    if (
        not isinstance(mail_project, str)
        or not mail_project
        or len(mail_project) > 4096
        or not Path(mail_project).is_absolute()
        or not isinstance(sender_name, str)
        or not _REGISTRY_NAME_RE.fullmatch(sender_name)
        or not isinstance(registration_token, str)
        or not registration_token
        or len(registration_token) > 4096
    ):
        raise _team_agent_reply_forbidden()
    binding = _team_agent_reply_binding(
        mail_project, sender_name, registration_token,
    )

    handles = body.get("mention_handles")
    subject = body.get("subject")
    body_md = body.get("body_md")
    importance = body.get("importance", "normal")
    idempotency_key = body.get("idempotency_key")
    if not isinstance(handles, list) or not 1 <= len(handles) <= 50:
        raise HTTPException(400, "至少需要一个团队成员")
    clean_handles: list[str] = []
    for value in handles:
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value)
        ):
            raise HTTPException(400, "团队成员花名无效")
        if value.lower() not in {item.lower() for item in clean_handles}:
            clean_handles.append(value)
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or len(subject.strip()) > 512
        or not isinstance(body_md, str)
        or not body_md.strip()
        or len(body_md) > 50_000
        or importance not in {"low", "normal", "high", "urgent"}
        or not isinstance(idempotency_key, str)
        or not 1 <= len(idempotency_key.strip()) <= 128
    ):
        raise HTTPException(400, "团队回复内容无效")
    remote_payload = {
        "client_session_id": str(binding["client_session_id"]),
        "reply_token": str(binding["reply_token"]),
        "subject": subject.strip(),
        "body_md": body_md,
        "importance": importance,
        "mention_handles": clean_handles,
        "idempotency_key": idempotency_key.strip(),
    }
    try:
        result = hub_client.session_lead_reply(
            str(binding["project_slug"]), remote_payload,
        )
    except hub_client.HumanAPIError as exc:
        if exc.status_code == 403:
            raise _team_agent_reply_forbidden() from exc
        if exc.status_code == 404:
            raise HTTPException(404, "团队成员不存在或已退出项目") from exc
        if exc.status_code == 409:
            raise HTTPException(409, "Session 负责人当前不可用") from exc
        raise HTTPException(502, "团队回复暂时失败") from exc
    deliveries = result.get("deliveries")
    safe_deliveries = []
    if isinstance(deliveries, list):
        for item in deliveries:
            if not isinstance(item, dict):
                continue
            safe_deliveries.append({
                key: item.get(key)
                for key in (
                    "name", "status", "reason", "receipt_message_id",
                    "target_project_key",
                )
            })
    return {
        "status": result.get("status"),
        "message_id": result.get("message_id"),
        "deliveries": safe_deliveries,
    }


class LocalIdentityClaimReq(BaseModel):
    identity_id: str = Field(..., min_length=1, max_length=256)
    project_slug: str = Field(..., min_length=1, max_length=128)


@app.post("/api/team-auth/local-identities/claim")
def api_team_local_identity_claim(req: LocalIdentityClaimReq, request: Request):
    """Cockpit 服务端读取 registration_token 调 Hub claim；前端永不接触 token。"""
    authorization = _team_human_authorization(request)
    try:
        hub_client.human_profile(authorization)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except hub_client.HumanAuthError as exc:
        _raise_human_auth_error(exc)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", req.project_slug):
        raise HTTPException(400, "project_slug 无效")
    identity = _registry_identity(req.identity_id)
    if identity is None:
        raise HTTPException(404, "本地未找到匹配的注册身份")
    if identity.get("hub") != hub_client.public_team_config()["team_hub"]:
        raise HTTPException(409, "该身份属于另一个 Hub，请先把 Agent Mail Hub 配置到当前 Team Hub 并重新注册身份")
    try:
        result = hub_client.claim_agent(
            authorization=authorization,
            project_slug=req.project_slug,
            source_project_slug=identity["project_slug"],
            agent_name=identity["name"],
            registration_token=identity["registration_token"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except hub_client.HumanAPIError as exc:
        # 固定安全映射：上游 detail 可能意外包含 token，绝不原样回显。
        raise HTTPException(exc.status_code, "Hub 认领失败，请稍后重试或检查 Hub 配置") from exc
    agent = result.get("agent") if isinstance(result, dict) else None
    if not isinstance(agent, dict):
        raise HTTPException(502, "Hub 认领返回了无效结果")
    # 显式字段白名单：绝不回显 Hub 原始 dict（可能含 token）
    safe_agent = {
        key: agent[key]
        for key in ("id", "name", "program", "model")
        if key in agent
    }
    return {"ok": True, "agent": safe_agent}


@app.get("/api/team-auth/status")
def api_team_auth_status(request: Request):
    try:
        return {
            "authenticated": True,
            **hub_client.human_profile(_team_human_authorization(request)),
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except hub_client.HumanAuthError as exc:
        _raise_human_auth_error(exc)


@app.post("/api/team-auth/logout")
def api_team_auth_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(TEAM_AUTH_COOKIE, path="/api")
    return response


@app.get("/api/team-auth/inbox-route/status")
def api_team_inbox_route_status(request: Request):
    """返回已禁用的远程 Inbox → Pane 兼容状态。"""
    _, human = _team_human_context(request)
    hub = hub_client.public_team_config()["team_hub"]
    return team_inbox_router.route_status(
        hub=hub, human_id=int(human["id"])
    )


@app.post("/api/team-auth/inbox-route/route")
def api_team_inbox_route_run(request: Request):
    """兼容端点：明确报告远程 Inbox → Pane 能力已禁用。"""
    authorization, human = _team_human_context(request)
    hub = hub_client.public_team_config()["team_hub"]
    return team_inbox_router.route_inbox(
        authorization,
        hub=hub,
        human_id=int(human["id"]),
    )


@app.post("/api/team-auth/register", status_code=201)
def api_team_auth_register(req: HumanRegistrationReq):
    try:
        return hub_client.human_register(
            req.username, req.display_name, req.password, req.invite_code
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except hub_client.HumanAuthError as exc:
        _raise_human_auth_error(exc)


@app.post("/api/team-auth/invitations", status_code=201)
def api_team_auth_create_invitation(req: HumanInvitationReq, request: Request):
    try:
        return hub_client.human_create_invitation(
            _team_human_authorization(request), req.expires_in
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except hub_client.HumanAuthError as exc:
        _raise_human_auth_error(exc)


@app.get("/api/team-auth/users")
def api_team_auth_users(request: Request):
    try:
        return hub_client.human_list_users(_team_human_authorization(request))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except hub_client.HumanAuthError as exc:
        _raise_human_auth_error(exc)


@app.patch("/api/team-auth/users/{username}")
def api_team_auth_update_user(
    username: str, req: HumanUserStatusReq, request: Request
):
    try:
        return hub_client.human_set_user_status(
            _team_human_authorization(request), username, req.status
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except hub_client.HumanAuthError as exc:
        _raise_human_auth_error(exc)


@app.get("/api/env-check")
def api_env_check():
    """环境自检:herdr / 各 agent 可执行文件 / Agent Mail 是否就绪(设置页展示)。"""
    herdr_ok = herdr_client.is_available()
    agents = {}
    for name in settings.KNOWN_AGENTS:
        path = herdr_client._find_agent_bin(name)
        # _find_agent_bin 找不到时兜底返回裸名,用"仍是裸名"判断未安装
        installed = path != name and Path(path).is_file()
        agents[name] = {"installed": installed, "path": path if installed else ""}
    return {
        "herdr": {"installed": herdr_ok, "path": herdr_client.HERDR_BIN if herdr_ok else ""},
        "agents": agents,
        "agent_mail": _agent_mail_status(),
    }


# ── 文件浏览/编辑路由 ──────────────────────────────────────────

def _visible_root_groups(groups: dict) -> dict:
    """展示用根目录:隐藏 /tmp 临时残留和 cockpit 内部 worktree(访问权限不变)。"""
    def hidden(p: str) -> bool:
        normalized = p.rstrip("/") or "/"
        return (
            any(
                normalized == root or normalized.startswith(root + "/")
                for root in ("/tmp", "/var/tmp", "/private/tmp")
            )
            or "-cockpit-worktrees/" in normalized + "/"
        )
    return {k: [p for p in paths if not hidden(p)] for k, paths in groups.items()}


@app.get("/api/files/roots")
def api_file_roots():
    """返回允许浏览的根目录列表。"""
    groups = _visible_root_groups(files.allowed_root_groups())
    return {"roots": [path for roots in groups.values() for path in roots], "groups": groups}


@app.post("/api/files/roots")
def api_file_root_add(req: FileRootReq):
    """持久化添加一个明确的自定义目录。"""
    try:
        result = files.add_custom_root(req.path)
        groups = _visible_root_groups(files.allowed_root_groups())
        return {**result, "roots": [p for roots in groups.values() for p in roots], "groups": groups}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/files/roots")
def api_file_root_remove(path: str):
    """移除自定义目录；系统目录和已注册项目不受影响。"""
    try:
        result = files.remove_custom_root(path)
        groups = _visible_root_groups(files.allowed_root_groups())
        return {**result, "roots": [p for roots in groups.values() for p in roots], "groups": groups}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/files")
def api_files_list(path: str = ""):
    """列目录或读文件。传目录路径列内容;传文件路径返回文件信息。"""
    try:
        return files.list_dir(path)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/files/search")
def api_files_search(path: str, q: str, limit: int = 100):
    """在指定白名单目录及其子目录中按文件名搜索。"""
    try:
        return files.search_files(path, q, limit)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/files/read")
def api_files_read(path: str):
    """读文件内容。"""
    try:
        return files.read_file(path)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/files/download")
def api_files_download(path: str):
    """下载单个文件。"""
    try:
        target = files.download_path(path)
        return FileResponse(target, filename=target.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/files/raw")
def api_files_raw(path: str):
    """内联预览媒体文件(图片/音视频,限 PREVIEW_EXT 白名单)。"""
    try:
        target = files.preview_path(path)
        return FileResponse(
            target,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/files/download-dir")
def api_files_download_dir(path: str, background: BackgroundTasks):
    """整目录打包 zip 下载(跳过 .git 与符号链接,有数量/大小上限)。"""
    try:
        archive = files.zip_dir(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    background.add_task(archive.unlink, missing_ok=True)
    return FileResponse(
        archive, filename=f"{Path(path).name}.zip", background=background
    )


@app.post("/api/files/write")
def api_files_write(req: WriteFileReq):
    """写文件(覆盖)。create=true 允许新建。"""
    try:
        return files.write_file(req.path, req.content, req.create)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/files")
def api_files_delete(path: str):
    """删除文件或空目录。"""
    try:
        return files.delete_file(path)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── 任务执行路由 ────────────────────────────────────────────────

@app.get("/api/tasks")
def api_tasks():
    return tasks.list_tasks()


@app.get("/api/tasks/stats")
def api_task_stats():
    return tasks.task_stats()


@app.get("/api/runtime/stats")
def api_runtime_stats():
    return {
        "process": runtime_stats.process_stats(),
        "connections": runtime_stats.connection_stats(),
        "terminal_sessions": terminal.session_stats(),
        "task_output_buffers": tasks.output_buffer_stats(),
    }


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str):
    t = tasks.get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@app.post("/api/tasks")
def api_start_task(req: StartTaskReq):
    try:
        return tasks.start_task(
            workdir=req.workdir, prompt=req.prompt,
            images=req.images, model=req.model,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/tasks/{task_id}/diff")
def api_task_diff(task_id: str):
    try:
        return tasks.task_diff(task_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/tasks/{task_id}/cancel")
def api_task_cancel(task_id: str):
    try:
        return tasks.cancel_task(task_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tasks/{task_id}/apply")
def api_task_apply(task_id: str, action: str = "apply"):
    try:
        return tasks.task_apply(task_id, action)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── herdr 路由(多 session 聚合,Orca 式看板数据源) ────────────

def _term_exists(term_id: str) -> bool:
    return any(term.get("id") == term_id for term in terminal.list_terms())


def _zoom_result_ok(result: dict[str, Any], zoomed: bool) -> bool:
    return (
        result.get("available", True) is not False
        and not result.get("error")
        and result.get("zoomed") is zoomed
    )


def _release_zoom_lease_locked(
    session: str, lease: dict[str, Any], now: float,
) -> dict[str, Any]:
    result = herdr_client.pane_zoom(session, lease.get("pane_id"), mode="off")
    if _zoom_result_ok(result, False):
        if _ZOOM_LEASES.get(session) is lease:
            _ZOOM_LEASES.pop(session, None)
        return {
            "available": True, "released": True, "owned": False,
            "changed": bool(result.get("changed")), "session": session,
        }
    lease["expires_at"] = now + ZOOM_LEASE_RETRY
    return {
        "available": result.get("available", True), "released": False,
        "owned": True, "session": session,
        "error": result.get("error") or "Herdr zoom 还原失败，将自动重试",
    }


def _expire_zoom_leases(now: float | None = None) -> list[dict[str, Any]]:
    current = time.monotonic() if now is None else now
    released = []
    with _ZOOM_LEASES_LOCK:
        expired = [
            (session, lease)
            for session, lease in _ZOOM_LEASES.items()
            if lease["expires_at"] <= current
        ]
        for session, lease in expired:
            released.append(_release_zoom_lease_locked(session, lease, current))
    return released


def _acquire_zoom_lease(
    session: str, owner: str, now: float | None = None,
) -> dict[str, Any]:
    current = time.monotonic() if now is None else now
    if not _term_exists(owner):
        return {
            "available": True, "acquired": False, "owned": False,
            "reason": "terminal_missing", "session": session,
        }
    with _ZOOM_LEASES_LOCK:
        _expire_zoom_leases(current)
        lease = _ZOOM_LEASES.get(session)
        if lease:
            if lease["owner"] != owner:
                return {
                    "available": True, "acquired": False, "owned": False,
                    "reason": "leased", "session": session,
                }
            return _renew_zoom_lease(session, owner, current)

        layout = herdr_client.pane_layout(session)
        if layout.get("available", True) is False or layout.get("error"):
            return {
                "available": layout.get("available", True), "acquired": False,
                "owned": False, "reason": "layout_unavailable", "session": session,
                "error": layout.get("error"),
            }
        if layout.get("zoomed"):
            return {
                "available": True, "acquired": False, "owned": False,
                "reason": "already_zoomed", "session": session,
            }
        if len(layout.get("panes") or []) <= 1:
            return {
                "available": True, "acquired": False, "owned": False,
                "reason": "single_pane", "session": session,
            }
        pane_id = layout.get("focused_pane_id")
        if not pane_id:
            return {
                "available": True, "acquired": False, "owned": False,
                "reason": "pane_missing", "session": session,
            }
        zoom = herdr_client.pane_zoom(session, pane_id, mode="on")
        if not _zoom_result_ok(zoom, True):
            return {
                "available": zoom.get("available", True), "acquired": False,
                "owned": False, "reason": "zoom_failed", "session": session,
                "error": zoom.get("error") or "Herdr zoom 未生效",
            }
        if not zoom.get("changed"):
            return {
                "available": True, "acquired": False, "owned": False,
                "reason": "already_zoomed", "session": session,
            }
        _ZOOM_LEASES[session] = {
            "owner": owner, "pane_id": zoom.get("focused_pane_id") or pane_id,
            "tab_id": layout.get("tab_id"), "expires_at": current + ZOOM_LEASE_TTL,
        }
        return {
            "available": True, "acquired": True, "owned": True,
            "changed": bool(zoom.get("changed")), "session": session,
            "pane_id": _ZOOM_LEASES[session]["pane_id"], "ttl": ZOOM_LEASE_TTL,
        }


def _renew_zoom_lease(
    session: str, owner: str, now: float | None = None,
) -> dict[str, Any]:
    current = time.monotonic() if now is None else now
    with _ZOOM_LEASES_LOCK:
        _expire_zoom_leases(current)
        lease = _ZOOM_LEASES.get(session)
        if not lease:
            return {
                "available": True, "acquired": False, "owned": False,
                "reason": "lease_missing", "session": session,
            }
        if lease["owner"] != owner:
            return {
                "available": True, "acquired": False, "owned": False,
                "reason": "not_owner", "session": session,
            }
        if not _term_exists(owner):
            released = _release_zoom_lease_locked(session, lease, current)
            return {**released, "acquired": False, "reason": "terminal_missing"}
        lease["expires_at"] = current + ZOOM_LEASE_TTL
        layout = herdr_client.pane_layout(session, lease.get("pane_id"))
        if layout.get("available", True) is False or layout.get("error"):
            return {
                "available": layout.get("available", True), "acquired": True,
                "owned": True, "session": session, "renewed": True,
                "warning": layout.get("error") or "暂时无法确认 zoom 状态",
                "ttl": ZOOM_LEASE_TTL,
            }
        pane_id = layout.get("focused_pane_id") or lease["pane_id"]
        if not layout.get("zoomed"):
            zoom = herdr_client.pane_zoom(session, pane_id, mode="on")
            if not _zoom_result_ok(zoom, True):
                return {
                    "available": zoom.get("available", True), "acquired": True,
                    "owned": True, "session": session, "renewed": True,
                    "warning": zoom.get("error") or "暂时无法恢复 zoom",
                    "ttl": ZOOM_LEASE_TTL,
                }
            if not zoom.get("changed"):
                _ZOOM_LEASES.pop(session, None)
                return {
                    "available": True, "acquired": False, "owned": False,
                    "reason": "already_zoomed", "session": session,
                }
            pane_id = zoom.get("focused_pane_id") or pane_id
        lease["pane_id"] = pane_id
        return {
            "available": True, "acquired": True, "owned": True,
            "renewed": True, "session": session, "pane_id": pane_id,
            "ttl": ZOOM_LEASE_TTL,
        }


def _release_zoom_lease(
    session: str, owner: str, now: float | None = None,
) -> dict[str, Any]:
    current = time.monotonic() if now is None else now
    with _ZOOM_LEASES_LOCK:
        lease = _ZOOM_LEASES.get(session)
        if not lease:
            return {
                "available": True, "released": True, "owned": False,
                "changed": False, "session": session,
            }
        if lease["owner"] != owner:
            return {
                "available": True, "released": False, "owned": False,
                "reason": "not_owner", "session": session,
            }
        return _release_zoom_lease_locked(session, lease, current)


def _release_zoom_leases_for_owner(owner: str) -> None:
    with _ZOOM_LEASES_LOCK:
        sessions = [
            session for session, lease in _ZOOM_LEASES.items()
            if lease["owner"] == owner
        ]
    for session in sessions:
        _release_zoom_lease(session, owner)


def _release_all_zoom_leases() -> None:
    with _ZOOM_LEASES_LOCK:
        leases = [
            (session, lease["owner"])
            for session, lease in _ZOOM_LEASES.items()
        ]
    for session, owner in leases:
        _release_zoom_lease(session, owner)

@app.get("/api/herdr/status")
def api_herdr_status():
    return {"available": herdr_client.is_available(), "binary": herdr_client.HERDR_BIN}


@app.get("/api/herdr/sessions")
def api_herdr_sessions():
    """列出所有 herdr session。"""
    return {"sessions": herdr_client.list_sessions()}


@app.get("/api/herdr/snapshot")
def api_herdr_snapshot():
    """聚合所有 running session 的 pane → 看板卡片数据源。"""
    return _board_snapshot()


@app.post("/api/herdr/session/{name}/zoom-lease")
def api_herdr_zoom_lease(name: str, req: ZoomLeaseReq):
    """窄屏 attach 的 zoom 租约；只还原 Cockpit 自己开启的 zoom。"""
    _validate_session_name(name)
    if req.action == "acquire":
        return _acquire_zoom_lease(name, req.term_id)
    if req.action == "renew":
        return _renew_zoom_lease(name, req.term_id)
    if req.action == "release":
        return _release_zoom_lease(name, req.term_id)
    raise HTTPException(400, f"不支持的 zoom lease action: {req.action}")


@app.get("/api/herdr/pane/{session}/{pane_id}")
def api_herdr_pane(
    session: str,
    pane_id: str,
    lines: int = Query(80, ge=1, le=1000),
    is_agent: bool = False,
):
    """读 pane 终端输出(live)。agent pane 用 agent read。"""
    _validate_session_name(session)
    _validate_pane_id(pane_id)
    return herdr_client.pane_read(session, pane_id, lines, is_agent)


@app.get("/api/herdr/pane/{session}/{pane_id}/summary")
def api_herdr_pane_summary(
    session: str,
    pane_id: str,
    max_lines: int = Query(30, ge=1, le=200),
):
    """取 agent 会话摘要。"""
    _validate_session_name(session)
    _validate_pane_id(pane_id)
    return herdr_client.pane_summary(session, pane_id, max_lines)


@app.get("/api/herdr/pane/{session}/{pane_id}/identity")
def api_herdr_pane_identity(session: str, pane_id: str):
    """查 herdr agent 对应的 agent-mail 身份(@ 注入协作者信息用)。

    用 session 绑定的 canonical human key + agent 类型反查真实身份。
    """
    _validate_session_name(session)
    _validate_pane_id(pane_id)
    # 先从当前运行时快照拿这个 pane 的 cwd 和 agent 类型。
    snap = _herdr_runtime_snapshot()
    pane = next((p for p in snap.get("panes", [])
                 if p.get("session") == session and p.get("pane_id") == pane_id), None)
    if not pane:
        raise HTTPException(404, "pane 不存在")
    cwd = pane.get("cwd", "")
    agent_type = pane.get("agent") or ""
    if not cwd or not agent_type:
        return {"found": False, "reason": "pane 无 cwd 或 agent 类型"}
    mail_status = db.status()
    if not mail_status["available"]:
        return {"found": False, "unavailable": True, "reason": mail_status["reason"]}
    state = _mail_project_state(session)
    if not state["bound"]:
        return {
            "found": False,
            "needs_project": True,
            "reason": "该 session 尚未选择 Agent Mail 通信项目",
            **state,
        }
    project = state["project"]
    descriptor = herdr_client.get_launch_descriptor(session, pane_id)
    instance_id = (
        str(descriptor.get("instance_id"))
        if descriptor and descriptor.get("instance_id") else None
    )
    mail_agent = (
        str(descriptor.get("agent") or "") if instance_id else str(agent_type)
    )
    if instance_id and not mail_agent:
        return {
            "found": False,
            "needs_registration": True,
            "reason": "managed descriptor 缺少 product agent",
            "project": project,
        }
    if not instance_id:
        legacy_count = sum(
            1 for candidate in snap.get("panes", [])
            if candidate.get("session") == session
            and candidate.get("agent") == agent_type
            and not herdr_client.get_launch_descriptor(
                session, str(candidate.get("pane_id") or ""),
            )
        )
        if legacy_count != 1:
            return {
                "found": False,
                "needs_registration": True,
                "reason": "同 session 存在多个无 descriptor 的同类型 agent",
                "project": project,
            }
    ident = (
        _identity_record(project, mail_agent, instance_id)
        if instance_id else _identity_record(project, mail_agent)
    )
    if not ident:
        return {
            "found": False,
            "needs_registration": True,
            "reason": "该通信项目下没有此 agent 的有效身份（未注册或已 retired）",
            "project": project,
        }
    result = {
        "found": True,
        "name": ident["name"],
        "program": ident["program"],
        "model": ident.get("model", ""),
        "project_key": project,
        "session": session,
        "cwd": cwd,
        "mail_hint": _collaborator_hint(ident["name"], project),
    }
    if instance_id:
        result["instance_id"] = instance_id
        result["display_name"] = descriptor.get("display_name") or agent_type
    return result


@app.post("/api/herdr/pane/{session}/{pane_id}/send")
def api_herdr_pane_send(session: str, pane_id: str, req: PaneSendReq):
    """往 pane 发指令(send-keys 或 prompt)。"""
    _validate_session_name(session)
    _validate_pane_id(pane_id)
    if req.mode not in VALID_PANE_SEND_MODES:
        raise HTTPException(400, f"不支持的发送模式: {req.mode}")
    return herdr_client.pane_send(session, pane_id, req.text, req.mode)


def _started_agent_mail_identity(
    session: str, pane_id: str, agent_type: str, instance_id: str,
    *, notify: bool = True, project_hint: str | None = None,
) -> dict[str, Any]:
    """为新增 managed instance 注册精确身份并发送身份告知。"""
    base: dict[str, Any] = {
        "registered": False, "registered_now": False, "notified": False,
        "instance_id": instance_id,
    }
    mail_agent = MAIL_AGENT_NAMES.get(agent_type, agent_type)
    try:
        herdr_client.validate_agent_instance_id(instance_id)
    except Exception as exc:
        return {**base, "warning": f"读取 Agent Mail 通信项目失败: {exc}"}
    if project_hint:
        try:
            project = next_profile.require_project(project_hint)
        except next_profile.NextProfileError as exc:
            return {**base, "warning": str(exc)}
    else:
        try:
            state = _mail_project_state(session)
        except Exception as exc:
            return {**base, "warning": f"读取 Agent Mail 通信项目失败: {exc}"}
        if not state.get("bound") or not state.get("project"):
            return {**base, "warning": "该 session 尚未绑定 Agent Mail 通信项目"}
        project = str(state["project"])
    status = {**base, "project": project}
    name = _identity_name(project, agent_type, instance_id)
    if not name:
        if not AGENT_MAIL_INIT_SCRIPT.is_file():
            return {**status, "warning": "Agent Mail 注册工具未安装"}
        try:
            registered = subprocess.run(
                [
                    str(AGENT_MAIL_INIT_SCRIPT), "--project", project,
                    "--instance", instance_id, "--only", mail_agent,
                ],
                cwd=project, capture_output=True, text=True, timeout=60,
            )
        except Exception as exc:
            return {**status, "warning": f"Agent Mail 身份注册失败: {exc}"}
        if registered.returncode != 0:
            detail = (registered.stderr or registered.stdout)[-300:].strip()
            return {
                **status,
                "warning": f"Agent Mail 身份注册失败: {detail or 'am-init-project 失败'}",
            }
        name = _identity_name(project, agent_type, instance_id)
        if not name:
            return {**status, "warning": "Agent Mail 注册完成但未查到有效身份"}
        status["registered_now"] = True
    status.update({"registered": True, "name": name})
    try:
        herdr_client.update_launch_descriptor_by_instance(
            instance_id, mail_agent=mail_agent, mail_instance=instance_id,
            mail_name=name, mail_project=project,
        )
    except (OSError, ValueError) as exc:
        status["descriptor_warning"] = str(exc)
    if not notify:
        return status
    try:
        notified = herdr_client.pane_send(
            session, pane_id,
            _identity_hint(
                name, project, agent_type, registered=True,
                instance_id=instance_id,
            ),
            "prompt",
        )
    except Exception as exc:
        return {**status, "warning": f"身份已注册，但告知发送失败: {exc}"}
    if notified.get("available", True) is False or notified.get("error"):
        return {
            **status,
            "warning": "身份已注册，但告知发送失败: "
            + str(notified.get("error") or "Herdr 不可用"),
        }
    status["notified"] = True
    return status


def _retire_agent_instance(
    instance_id: str, *, project_hint: str | None = None,
) -> dict[str, Any]:
    """把一个 pending descriptor 精确退休到 Hub，并保留本地 tombstone。"""
    try:
        opaque_id = herdr_client.validate_agent_instance_id(instance_id)
    except ValueError as exc:
        return {"instance_id": str(instance_id), "retired": False, "error": str(exc)}

    with _IDENTITY_RETIRE_LOCKS_GUARD:
        retire_lock = _IDENTITY_RETIRE_LOCKS.setdefault(opaque_id, threading.Lock())

    with retire_lock:
        descriptor = herdr_client.get_launch_descriptor_by_instance(
            opaque_id, include_retired=True,
        )
        if not descriptor:
            return {
                "instance_id": opaque_id, "retired": False,
                "error": "launch descriptor 不存在，无法证明退休目标",
            }
        if descriptor.get("state") == "retired":
            return {"instance_id": opaque_id, "retired": True}
        if descriptor.get("state") != "retirement_pending":
            return {
                "instance_id": opaque_id, "retired": False,
                "error": "launch descriptor 尚未进入 retirement_pending",
            }

        agent_type = str(descriptor.get("agent") or "").strip()
        mail_agent = str(
            descriptor.get("mail_agent")
            or MAIL_AGENT_NAMES.get(agent_type, agent_type)
            or ""
        ).strip()
        project = str(descriptor.get("mail_project") or project_hint or "").strip()
        if not mail_agent or not project:
            error = "缺少精确的 Mail agent 或 project，保留 pending 等待修复"
            try:
                herdr_client.fail_launch_descriptor_retirement(opaque_id, error)
            except (OSError, ValueError):
                pass
            return {"instance_id": opaque_id, "retired": False, "error": error}

        # 路由在 unbind 前提供 project_hint；先持久化，保证进程重启后仍可重试。
        if not descriptor.get("mail_project") or not descriptor.get("mail_agent"):
            try:
                herdr_client.update_launch_descriptor_by_instance(
                    opaque_id, mail_agent=mail_agent, mail_instance=opaque_id,
                    mail_project=project,
                )
            except (OSError, ValueError) as exc:
                error = f"退休目标保存失败: {exc}"
                try:
                    herdr_client.fail_launch_descriptor_retirement(opaque_id, error)
                except (OSError, ValueError):
                    pass
                return {"instance_id": opaque_id, "retired": False, "error": error}

        if not AM_RETIRE_SCRIPT.is_file():
            error = "Agent Mail 退休工具未安装"
            try:
                herdr_client.fail_launch_descriptor_retirement(opaque_id, error)
            except (OSError, ValueError):
                pass
            return {"instance_id": opaque_id, "retired": False, "error": error}

        try:
            retired = subprocess.run(
                [
                    str(AM_RETIRE_SCRIPT), "--agent", mail_agent,
                    "--instance", opaque_id, "--project", project,
                ],
                cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=60,
            )
            if retired.returncode != 0:
                detail = (retired.stderr or retired.stdout)[-500:].strip()
                raise RuntimeError(detail or f"am-retire 退出码 {retired.returncode}")
            finalized = herdr_client.finalize_launch_descriptor_retirement(opaque_id)
            if not finalized:
                raise RuntimeError("Hub 已退休，但本地 descriptor 不存在")
        except Exception as exc:
            error = str(exc)[:500] or type(exc).__name__
            try:
                herdr_client.fail_launch_descriptor_retirement(opaque_id, error)
            except (OSError, ValueError):
                pass
            return {"instance_id": opaque_id, "retired": False, "error": error}
        return {"instance_id": opaque_id, "retired": True}


def _retire_pending_agent_instances(
    instance_ids: list[str], *, project_hint: str | None = None,
) -> dict[str, Any]:
    requested = list(dict.fromkeys(str(item) for item in instance_ids if item))
    retired: list[str] = []
    pending: list[str] = []
    errors: dict[str, str] = {}
    for instance_id in requested:
        result = _retire_agent_instance(instance_id, project_hint=project_hint)
        if result.get("retired"):
            retired.append(instance_id)
        else:
            pending.append(instance_id)
            errors[instance_id] = str(result.get("error") or "退休失败")
    return {
        "requested": requested, "retired": retired, "pending": pending,
        "errors": errors, "complete": not pending,
    }


def _retry_pending_agent_retirements() -> dict[str, Any]:
    pending = herdr_client.pending_launch_descriptor_retirements()
    return _retire_pending_agent_instances([
        str(item.get("instance_id") or "") for item in pending
    ])


def _attach_identity_retirement(
    result: dict[str, Any], *, project_hint: str | None = None,
) -> dict[str, Any]:
    pending = result.get("retirement_pending")
    if not isinstance(pending, list) or not pending:
        return result
    retirement = _retire_pending_agent_instances(pending, project_hint=project_hint)
    result["identity_retirement"] = retirement
    if not retirement["complete"]:
        result["partial"] = True
    return result


def _start_agent(req: StartAgentReq) -> dict[str, Any]:
    if req.agent not in VALID_AGENTS:
        raise HTTPException(400, f"不支持的 agent: {req.agent}")
    if req.layout not in VALID_LAYOUTS:
        raise HTTPException(400, f"不支持的布局: {req.layout}")
    if req.workspace not in {"shared", "isolated"}:
        raise HTTPException(400, f"不支持的工作目录策略: {req.workspace}")
    mail_requirement = _agent_mail_requirement()
    if mail_requirement:
        raise HTTPException(503, mail_requirement["error"])
    try:
        display_name = herdr_client.validate_display_name(req.name or req.agent)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    instance_id = herdr_client.new_agent_instance_id()
    try:
        normalized_args = herdr_client.normalize_agent_args(req.args)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    project_dir = Path(req.workdir).expanduser().resolve()
    if next_profile.enabled():
        _canonical_mail_project(project_dir)
    workspace: dict[str, Any] = {
        "strategy": "shared", "workdir": str(project_dir),
    }
    git_root = None
    if req.workspace == "isolated":
        git_root, source_dir = _worktree_source(project_dir)
        if not git_root or not source_dir:
            raise HTTPException(400, "该目录不是 Git 仓库，不能创建独立 worktree")
        participant = WorkspaceParticipantReq(
            id=instance_id, name=display_name, agent=req.agent, role="developer",
            task="", workspace="isolated",
        )
        try:
            workspace = _ensure_worktree(
                git_root, source_dir, req.session, participant, 0,
                detached=False, slug=instance_id,
            )
        except ValueError as exc:
            raise HTTPException(400, f"创建 {display_name} worktree 失败: {exc}") from exc

    try:
        result = herdr_client.start_agent(
            req.session, workspace["workdir"], req.agent, req.model,
            layout=req.layout, label=display_name, args=normalized_args,
            instance_id=instance_id,
        )
    except Exception as exc:
        cleanup_errors = (
            _rollback_worktrees(git_root, [workspace])
            if git_root and workspace.get("created") else []
        )
        detail = f"启动 {display_name} 失败: {exc}"
        if cleanup_errors:
            detail += "；回滚异常: " + "；".join(cleanup_errors)
        raise HTTPException(500, detail) from exc
    result.setdefault("instance_id", instance_id)
    result.setdefault("display_name", display_name)
    result["workspace"] = workspace
    launch_failed = bool(result.get("error")) or result.get("available", True) is False
    if launch_failed and git_root and workspace.get("created"):
        cleanup_errors = _rollback_worktrees(git_root, [workspace])
        result["worktree_rolled_back"] = not cleanup_errors
        if cleanup_errors:
            result["rollback_errors"] = cleanup_errors
    if (
        not launch_failed and not result.get("reused")
        and isinstance(result.get("pane_id"), str) and result["pane_id"]
    ):
        result["agent_mail"] = _started_agent_mail_identity(
            req.session, result["pane_id"], req.agent, instance_id,
        )
        try:
            result["coordination"] = coordination.add_participant(
                session=req.session,
                participant_id=instance_id,
                agent=req.agent,
                pane_id=result["pane_id"],
                workdir=workspace["workdir"],
                mail_name=result["agent_mail"].get("name"),
            )
        except Exception as exc:
            result["coordination"] = {
                "joined": False, "reused": False,
                "reason": f"新增 Agent 未同步到协作 run: {exc}",
            }
    return result


@app.post("/api/herdr/start")
def api_herdr_start(req: StartAgentReq):
    """在 session 里串行启动一个 agent pane。"""
    _validate_session_name(req.session)
    with _SETUP_WORKSPACE_LOCKS_GUARD:
        lock = _SETUP_WORKSPACE_LOCKS.setdefault(req.session, threading.Lock())
    if not lock.acquire(blocking=False):
        raise HTTPException(409, f"session {req.session} 正在变更，请勿重复提交")
    try:
        return _start_agent(req)
    finally:
        lock.release()


@app.post("/api/herdr/pane/{session}/{pane_id}/restart")
def api_herdr_pane_restart(session: str, pane_id: str, resume: bool = False):
    """重启 pane 里的 agent(Ctrl+C + 重新启动)。resume=true 尝试恢复历史。"""
    _validate_session_name(session)
    _validate_pane_id(pane_id)
    return herdr_client.restart_pane(session, pane_id, resume=resume)


@app.delete("/api/herdr/pane/{session}/{pane_id}")
def api_herdr_pane_delete(session: str, pane_id: str):
    """显式关闭 pane；managed identity 仅在关闭成功后进入退休流程。"""
    _validate_session_name(session)
    _validate_pane_id(pane_id)
    result = herdr_client.close_pane(session, pane_id)
    if result.get("closed"):
        project_hint = None
        try:
            state = _mail_project_state(session)
            if state.get("bound") and state.get("project"):
                project_hint = str(state["project"])
        except (OSError, ValueError, HTTPException):
            pass
        _attach_identity_retirement(result, project_hint=project_hint)
    return result


@app.post("/api/herdr/session/{name}/stop")
def api_herdr_session_stop(name: str):
    """停止 herdr session。"""
    _validate_session_name(name)
    result = herdr_client.stop_session(name)
    if not result.get("error"):
        coordination.close_session(name, "stopped")
    return result


@app.delete("/api/herdr/session/{name}")
def api_herdr_session_delete(name: str):
    """删除已停止的 herdr session。"""
    _validate_session_name(name)
    project_hint = None
    try:
        state = _mail_project_state(name)
        if state.get("bound") and state.get("project"):
            project_hint = str(state["project"])
    except (OSError, ValueError, HTTPException):
        pass
    result = herdr_client.delete_session(name)
    if result.get("deleted"):
        _attach_identity_retirement(result, project_hint=project_hint)
        coordination.close_session(name, "deleted")
        mail_projects.unbind(name)
    return result


@app.post("/api/herdr/pane/{session}/{pane_id}/layout/split")
def api_herdr_pane_layout_split(session: str, pane_id: str, req: PaneSplitLayoutReq):
    """一键分屏:当前 pane 拆成水平/垂直/2×2 四宫格,新槽位为空 shell。"""
    _validate_session_name(session)
    _validate_pane_id(pane_id)
    if req.mode not in herdr_client.SPLIT_MODES:
        raise HTTPException(400, f"不支持的分屏模式: {req.mode}")
    try:
        created = herdr_client.split_pane_layout(session, pane_id, req.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"mode": req.mode, "created": created}


@app.post("/api/herdr/pane/{session}/{pane_id}/layout/detach")
def api_herdr_pane_layout_detach(session: str, pane_id: str):
    """拆出当前 pane:移到自己的独立 tab。"""
    _validate_session_name(session)
    _validate_pane_id(pane_id)
    try:
        moved_pane_id = herdr_client.detach_pane(session, pane_id) or pane_id
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"detached": moved_pane_id}


@app.post("/api/herdr/session/{name}/layout/untile")
def api_herdr_session_layout_untile(name: str, req: TabUntileReq):
    """整组拆开:指定 tab 内除第一个 pane 外,其余逐个移到独立 tab。"""
    _validate_session_name(name)
    if not PANE_ID_RE.fullmatch(req.tab_id):
        raise HTTPException(400, "非法 tab id")
    try:
        moved = herdr_client.untile_tab(name, req.tab_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"tab_id": req.tab_id, "moved": moved}


@app.post("/api/herdr/session/{name}/layout/compose")
def api_herdr_session_layout_compose(name: str, req: PaneComposeReq):
    """组合分屏:把选中的 2-4 个 pane 组成一个分屏(第一个为基准)。"""
    _validate_session_name(name)
    if req.orientation not in herdr_client.COMPOSE_ORIENTATIONS:
        raise HTTPException(400, f"不支持的组合方向: {req.orientation}")
    if not 2 <= len(req.pane_ids) <= herdr_client.COMPOSE_MAX_PANES:
        raise HTTPException(
            400, f"组合分屏仅支持 2-{herdr_client.COMPOSE_MAX_PANES} 个 pane")
    for pid in req.pane_ids:
        if not PANE_ID_RE.fullmatch(pid):
            raise HTTPException(400, f"非法 pane id: {pid}")
    try:
        base = herdr_client.compose_panes(name, list(req.pane_ids), req.orientation)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"base": base, "composed": len(req.pane_ids)}


@app.get("/api/coordination/session/{name}")
def api_coordination_session(name: str):
    _validate_session_name(name)
    run = coordination.run_by_session(name)
    if not run:
        raise HTTPException(404, "该 session 尚无协作 run")
    return run


def _git(workdir: Path, *args: str) -> str:
    """在指定仓库执行受控 git 子命令。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Git 不可用: {exc}") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()[-400:]
        raise ValueError(message or f"git {' '.join(args)} 失败")
    return result.stdout.strip()


def _git_root(workdir: Path) -> Path | None:
    try:
        return Path(_git(workdir, "rev-parse", "--show-toplevel")).resolve()
    except ValueError:
        return None


def _git_common_dir(workdir: Path) -> Path | None:
    if not workdir.is_dir():
        return None
    try:
        raw = _git(
            workdir, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        return Path(raw).resolve()
    except ValueError:
        try:
            raw = _git(workdir, "rev-parse", "--git-common-dir")
            path = Path(raw)
            return (path if path.is_absolute() else workdir / path).resolve()
        except ValueError:
            return None


def _worktree_source(project_dir: Path) -> tuple[Path | None, Path | None]:
    """把任意 checkout 中的目录映射回主仓库，避免在 managed worktree 里再嵌套。"""
    checkout_root = _git_root(project_dir)
    if not checkout_root:
        return None, None
    common_dir = _git_common_dir(project_dir)
    if common_dir and common_dir.name == ".git":
        primary_root = common_dir.parent.resolve()
        try:
            relative = project_dir.relative_to(checkout_root)
        except ValueError:
            relative = None
        if relative is not None:
            primary_project_dir = (primary_root / relative).resolve()
            if primary_project_dir.is_dir():
                return primary_root, primary_project_dir
    return checkout_root, project_dir


def _canonical_mail_project(workdir: Path) -> str:
    """同一 clone 的 linked worktree 统一映射到主 worktree root。"""
    resolved = workdir.expanduser().resolve()
    scoped = next_profile.project()
    if scoped is not None:
        scope_path = Path(scoped)
        try:
            resolved.relative_to(scope_path)
            return scoped
        except ValueError:
            resolved_root = _git_root(resolved)
            resolved_common = _git_common_dir(resolved)
            scoped_common = _git_common_dir(scope_path)
            primary_root = (
                resolved_common.parent.resolve()
                if resolved_common is not None and resolved_common.name == ".git"
                else None
            )
            if (
                resolved_root is not None
                and resolved_common is not None
                and scoped_common is not None
                and resolved_common == scoped_common
                and resolved_root != primary_root
            ):
                return scoped
            raise HTTPException(403, "Agent Mail 项目超出 Next 隔离范围")
    common_dir = _git_common_dir(resolved)
    if common_dir is None:
        return str(resolved)
    if common_dir.name == ".git" and common_dir.parent.is_dir():
        return str(common_dir.parent.resolve())
    try:
        listed = _git(resolved, "worktree", "list", "--porcelain")
    except ValueError:
        root = _git_root(resolved)
        return str(root or resolved)
    first = next(
        (
            Path(line.removeprefix("worktree ")).resolve()
            for line in listed.splitlines()
            if line.startswith("worktree ")
        ),
        None,
    )
    return str(first or _git_root(resolved) or resolved)


def _session_record(name: str) -> dict[str, Any]:
    record = next(
        (item for item in herdr_client.list_sessions() if item.get("name") == name),
        None,
    )
    if not record:
        raise HTTPException(404, f"session 不存在: {name}")
    session_dir = record.get("directory") or ""
    if not session_dir:
        raise HTTPException(409, f"session {name} 缺少 session_dir，无法安全绑定通信项目")
    return record


def _same_mail_project_family(project: str, session_path: str) -> bool:
    candidate = Path(project).expanduser()
    source = Path(session_path).expanduser()
    if not candidate.is_absolute() or not source.is_absolute():
        return False
    candidate = candidate.resolve()
    source = source.resolve()
    candidate_common = _git_common_dir(candidate)
    source_common = _git_common_dir(source)
    if candidate_common is not None or source_common is not None:
        return (
            candidate_common is not None
            and source_common is not None
            and candidate_common == source_common
        )
    return candidate == source


def _session_pane_paths(name: str) -> list[str]:
    snap = _herdr_runtime_snapshot()
    sess = next(
        (item for item in snap.get("sessions", []) if item.get("session") == name),
        None,
    )
    panes = sess.get("panes", []) if sess else [
        pane for pane in snap.get("panes", []) if pane.get("session") == name
    ]
    return [pane["cwd"] for pane in panes if pane.get("cwd")]


def _mail_project_candidates(
    name: str, session_dir: str, pane_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    status = db.status()
    if not status["available"]:
        return []
    paths = [session_dir, *(pane_paths if pane_paths is not None else _session_pane_paths(name))]
    try:
        projects = db.list_projects()
    except Exception:
        return []
    candidates = []
    seen = set()
    for project in projects:
        human_key = project.get("human_key") or ""
        if not human_key or human_key in seen:
            continue
        if any(_same_mail_project_family(human_key, path) for path in paths):
            candidates.append(project)
            seen.add(human_key)
    return candidates


def _mail_project_suggestion(pane_paths: list[str]) -> str:
    """只有所有带 cwd 的 pane 指向同一项目时才给出预填建议。"""
    projects = {
        _canonical_mail_project(Path(path))
        for path in pane_paths
        if Path(path).expanduser().is_dir()
    }
    return next(iter(projects)) if len(projects) == 1 else ""


def _mail_project_state(name: str) -> dict[str, Any]:
    record = _session_record(name)
    session_dir = str(Path(record["directory"]).expanduser().resolve())
    project = mail_projects.get(name, session_dir)
    binding_invalidated = False
    if project and db.status()["available"]:
        try:
            active_projects = {item["human_key"] for item in db.list_projects()}
        except Exception:
            active_projects = None
        if active_projects is not None and (
            project not in active_projects or not Path(project).is_dir()
        ):
            mail_projects.unbind(name, session_dir)
            project = None
            binding_invalidated = True
    if project:
        return {
            "session": name,
            "session_dir": session_dir,
            "bound": True,
            "project": project,
            "candidates": [],
            "needs_selection": False,
            "suggested_project": project,
            "migrated": False,
            "binding_invalidated": False,
        }
    pane_paths = _session_pane_paths(name)
    candidates = _mail_project_candidates(name, session_dir, pane_paths)
    if len(candidates) == 1:
        migrated = True
        try:
            project = mail_projects.bind(
                name, session_dir, candidates[0]["human_key"]
            )
        except ValueError:
            project = mail_projects.get(name, session_dir)
            if not project:
                raise
            migrated = False
        return {
            "session": name,
            "session_dir": session_dir,
            "bound": True,
            "project": project,
            "candidates": candidates,
            "needs_selection": False,
            "suggested_project": project,
            "migrated": migrated,
            "binding_invalidated": binding_invalidated,
        }
    return {
        "session": name,
        "session_dir": session_dir,
        "bound": False,
        "project": None,
        "candidates": candidates,
        "needs_selection": True,
        "suggested_project": _mail_project_suggestion(pane_paths) if not candidates else "",
        "migrated": False,
        "binding_invalidated": binding_invalidated,
    }


def _bind_mail_project(
    name: str, project: str, *, replace: bool = False,
) -> tuple[str, str]:
    record = _session_record(name)
    session_dir = str(Path(record["directory"]).expanduser().resolve())
    selected = Path(project).expanduser()
    if not selected.is_absolute():
        raise HTTPException(400, "Agent Mail 项目必须是绝对路径")
    selected = selected.resolve()
    if not selected.is_dir():
        raise HTTPException(400, f"Agent Mail 项目目录不存在: {selected}")
    candidates = _mail_project_candidates(name, session_dir)
    candidate = next(
        (
            item["human_key"] for item in candidates
            if Path(item["human_key"]).expanduser().resolve() == selected
        ),
        None,
    )
    project_key = candidate or _canonical_mail_project(selected)
    try:
        project_key = mail_projects.bind(
            name, session_dir, project_key, replace=replace
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return project_key, session_dir


def _ensure_worktree(
    git_root: Path, project_dir: Path, session: str, participant: WorkspaceParticipantReq,
    index: int, detached: bool, slug: str | None = None,
    legacy_slug: str | None = None,
) -> dict[str, Any]:
    """创建或复用 Cockpit 管理的 worktree，返回实际工作目录。"""
    slug = slug or f"{index + 1}-{participant.agent}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", slug):
        raise ValueError(f"无效的 worktree 名称: {slug}")
    base = git_root.parent / f".{git_root.name}-cockpit-worktrees" / session
    _git(git_root, "worktree", "prune")
    listed = _git(git_root, "worktree", "list", "--porcelain")
    registered = {
        Path(line.removeprefix("worktree ")).resolve()
        for line in listed.splitlines() if line.startswith("worktree ")
    }
    target = (base / slug).resolve()
    named_branch = f"agent-cockpit/{session}/{slug}"
    named_branch_exists = not detached and subprocess.run(
        [
            "git", "-C", str(git_root), "show-ref", "--verify", "--quiet",
            f"refs/heads/{named_branch}",
        ],
        timeout=10,
    ).returncode == 0
    if legacy_slug and legacy_slug != slug:
        legacy_target = (base / legacy_slug).resolve()
        legacy_branch = f"agent-cockpit/{session}/{legacy_slug}"
        legacy_branch_exists = not detached and subprocess.run(
            [
                "git", "-C", str(git_root), "show-ref", "--verify", "--quiet",
                f"refs/heads/{legacy_branch}",
            ],
            timeout=10,
        ).returncode == 0
        if (
            target not in registered and not target.exists()
            and not named_branch_exists
            and (legacy_target in registered or legacy_branch_exists)
        ):
            slug = legacy_slug
            target = legacy_target
    reused = target in registered
    branch = None if detached else f"agent-cockpit/{session}/{slug}"
    branch_exists = bool(branch) and subprocess.run(
        ["git", "-C", str(git_root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        timeout=10,
    ).returncode == 0
    created = False
    branch_created = False
    if not reused:
        if target.exists():
            raise ValueError(f"worktree 目标已存在但未被 Git 登记: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if detached:
            _git(git_root, "worktree", "add", "--detach", str(target), "HEAD")
            created = True
        else:
            if branch_exists:
                _git(git_root, "worktree", "add", str(target), branch)
                created = True
            else:
                _git(git_root, "worktree", "add", "-b", branch, str(target), "HEAD")
                created = True
                branch_created = True
    relative = project_dir.relative_to(git_root)
    actual_dir = target / relative
    if not actual_dir.is_dir():
        if created:
            with suppress(ValueError):
                _git(git_root, "worktree", "remove", "--force", str(target))
            if branch_created and branch:
                with suppress(ValueError):
                    _git(git_root, "branch", "-D", branch)
        raise ValueError(f"worktree 中找不到工作目录: {actual_dir}")
    dirty = bool(_git(target, "status", "--porcelain"))
    return {
        "strategy": "review" if detached else "isolated",
        "workdir": str(actual_dir),
        "worktree": str(target),
        "branch": branch,
        "reused": reused,
        "resumed": bool(branch_exists),
        "dirty": dirty,
        "created": created,
        "branch_created": branch_created,
    }


def _rollback_worktrees(git_root: Path, workspaces: list[dict[str, Any]]) -> list[str]:
    """回滚本次新建的 worktree；恢复的旧分支永不删除。"""
    errors = []
    for workspace in reversed(workspaces):
        try:
            _git(git_root, "worktree", "remove", "--force", workspace["worktree"])
        except ValueError as exc:
            errors.append(f"移除 {workspace['worktree']} 失败: {exc}")
        branch = workspace.get("branch")
        if workspace.get("branch_created") and branch:
            try:
                _git(git_root, "branch", "-D", branch)
            except ValueError as exc:
                errors.append(f"删除分支 {branch} 失败: {exc}")
    return errors


def _prepare_workspace(req: SetupWorkspaceReq) -> tuple[list[dict[str, Any]], list[str]]:
    """验证协作定义，并把自动策略解析成每个 Agent 的实际工作目录。"""
    if req.mode not in VALID_COLLAB_MODES:
        raise HTTPException(400, f"不支持的协作方式: {req.mode}")
    source = req.participants
    legacy = source is None
    participants = source if source is not None else [
        WorkspaceParticipantReq(id=f"agent-{i + 1}", agent=agent)
        for i, agent in enumerate(req.agents)
    ]
    if not participants:
        raise HTTPException(400, "至少选择一个 agent")
    if any(p.agent not in VALID_AGENTS for p in participants):
        raise HTTPException(400, "agents 包含不支持的类型")
    if any(p.role not in VALID_WORKSPACE_ROLES for p in participants):
        raise HTTPException(400, "participants 包含不支持的角色")
    if any(p.workspace not in VALID_WORKSPACE_STRATEGIES for p in participants):
        raise HTTPException(400, "participants 包含不支持的工作目录策略")
    # task 为选填:留空时 _workspace_briefing 会省略"你的任务"行
    normalized_agent_args = []
    for participant in participants:
        try:
            normalized_agent_args.append(
                herdr_client.normalize_agent_args(participant.args)
            )
        except ValueError as exc:
            raise HTTPException(
                400, f"{participant.name or participant.agent} 的{exc}"
            ) from exc

    ids = []
    names = []
    instance_ids = []
    agent_counts: dict[str, int] = {}
    for i, p in enumerate(participants):
        pid = p.id or f"agent-{i + 1}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", pid):
            raise HTTPException(400, f"无效的 participant id: {pid}")
        ids.append(pid)
        agent_counts[p.agent] = agent_counts.get(p.agent, 0) + 1
        name = p.name.strip() or f"{p.agent}-{agent_counts[p.agent]}"
        try:
            name = herdr_client.validate_display_name(name)
        except ValueError as exc:
            raise HTTPException(400, f"无效的显示名称: {name}（{exc}）") from exc
        names.append(name)
        instance_ids.append(herdr_client.new_agent_instance_id())
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "participant id 不能重复")
    roles_by_id = {pid: p.role for p, pid in zip(participants, ids)}
    for p, pid in zip(participants, ids):
        if p.review_target and (p.review_target not in ids or p.review_target == pid):
            raise HTTPException(400, f"无效的复核对象: {p.review_target}")
        if p.review_target and roles_by_id[p.review_target] not in {"lead", "developer"}:
            raise HTTPException(400, "Reviewer 的复核对象必须是写入者")

    writers = [p for p in participants if p.role in {"lead", "developer"}]
    if req.mode == "develop_review" and (
        not writers or not any(p.role == "reviewer" for p in participants)
    ):
        raise HTTPException(400, "开发 + 复核模式至少需要一名开发者和一名 Reviewer")
    if req.mode == "parallel" and len(writers) < 2:
        raise HTTPException(400, "并行开发模式至少需要两名写入者")
    if len(writers) > 1 and any(p.workspace == "shared" for p in writers):
        raise HTTPException(400, "多个并行写入者不能共享工作目录")

    project_dir = Path(req.workdir).expanduser().resolve()
    git_root = None if legacy else _git_root(project_dir)
    warnings: list[str] = []
    needs_worktrees = req.mode in {"develop_review", "parallel"} or len(writers) > 1 or any(
        p.workspace == "isolated" or (p.role == "reviewer" and p.workspace == "auto")
        for p in participants
    )
    if not git_root:
        if not legacy and (len(writers) > 1 or any(p.workspace == "isolated" for p in participants)):
            raise HTTPException(400, "该目录不是 Git 仓库，暂不支持并行或强制独立工作目录")
        if not legacy and needs_worktrees:
            warnings.append("当前不是 Git 仓库，所有 Agent 使用原工作目录")
    elif needs_worktrees and _git(git_root, "status", "--porcelain"):
        warnings.append("项目有未提交改动；独立 worktree 从当前 HEAD 创建，不包含这些改动")

    plans = []
    created_workspaces: list[dict[str, Any]] = []
    for index, (participant, pid, name, instance_id, normalized_args) in enumerate(
        zip(participants, ids, names, instance_ids, normalized_agent_args)
    ):
        strategy = participant.workspace
        if strategy == "auto":
            if participant.role == "reviewer":
                strategy = "review"
            elif participant.role in {"lead", "developer"} and (
                req.mode in {"develop_review", "parallel"} or len(writers) > 1
            ):
                strategy = "isolated"
            else:
                strategy = "shared"
        elif participant.role == "reviewer" and strategy == "isolated":
            strategy = "review"

        workspace = {"strategy": "shared", "workdir": str(project_dir)}
        if git_root and strategy in {"isolated", "review"}:
            try:
                workspace = _ensure_worktree(
                    git_root, project_dir, req.session, participant, index,
                    detached=strategy == "review", slug=instance_id,
                )
            except ValueError as exc:
                cleanup_errors = _rollback_worktrees(git_root, created_workspaces)
                detail = f"创建 {participant.agent} worktree 失败: {exc}"
                if cleanup_errors:
                    detail += "；回滚异常: " + "；".join(cleanup_errors)
                raise HTTPException(400, detail) from exc
            if workspace["created"]:
                created_workspaces.append(workspace)
            if workspace["resumed"]:
                warnings.append(
                    f"{participant.agent} 从已有分支恢复；如需全新任务请使用新 session 名"
                )
            if workspace["reused"] and workspace["dirty"]:
                warnings.append(
                    f"{participant.agent} 的已有 worktree 有未提交改动，已原样保留"
                )
        plans.append({
            "id": pid,
            "name": name,
            "instance_id": instance_id,
            "agent": participant.agent,
            "role": participant.role,
            "task": participant.task.strip(),
            "args": normalized_args,
            "review_target": participant.review_target,
            **workspace,
        })
    return plans, warnings


def _workspace_briefing(
    req: SetupWorkspaceReq, plan: dict[str, Any], plans: list[dict[str, Any]],
    coordination_context: dict[str, Any] | None = None,
) -> str:
    role_labels = {"lead": "负责人/开发", "developer": "开发", "reviewer": "Reviewer", "researcher": "调研"}
    coworkers = "、".join(
        f"{p['name']}[{p['agent']}]({role_labels[p['role']]})"
        for p in plans if p["id"] != plan["id"]
    )
    lines = [
        "[Agent Cockpit 工作区任务]",
        f"你的本地实例: {plan['name']} ({plan['agent']})",
        f"你的角色: {role_labels[plan['role']]}",
    ]
    if plan["task"].strip():
        lines.append(f"你的任务: {plan['task']}")
    lines.append(f"工作目录策略: {plan['strategy']} ({plan['workdir']})")
    if coworkers:
        lines.append(f"协作者: {coworkers}")
    if plan["role"] == "reviewer":
        target_plan = next((p for p in plans if p["id"] == plan.get("review_target")), None)
        target = target_plan["name"] if target_plan else "开发者"
        if plan["strategy"] == "review":
            lines.append(
                f"复核对象: {target}。等待对方给出 commit SHA，在当前 detached worktree "
                "切换到该 SHA 后复核；默认只提意见，不直接改被审分支。"
            )
        else:
            lines.append(
                f"复核对象: {target}。请在共享工作目录只读复核；不要 checkout、"
                "切换分支或修改文件。"
            )
    elif plan["role"] == "lead":
        lines.append("你负责推进目标、协调协作者并汇总最终结果。")
    if coordination_context:
        lines.append(
            f"协作运行: run={coordination_context['run_id']}, "
            f"task={plan['id']}, revision=1。"
        )
    lines.append(MAIL_COORDINATION_GUIDE)
    return "\n".join(lines)


def _start_pty_drainer(term_id: str, output: bytearray):
    """持续排空隐藏 PTY，避免登录 shell/TUI 输出反压阻塞输入命令。"""
    stop = threading.Event()

    def drain():
        while not stop.is_set():
            data = terminal.read_output(term_id, 0.1)
            if not data:
                stop.wait(0.02)
                continue
            output.extend(data)
            overflow = len(output) - SESSION_BOOTSTRAP_OUTPUT_LIMIT
            if overflow > 0:
                del output[:overflow]

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    return stop, thread


def _stop_pty_drainer(stop: threading.Event | None, thread: threading.Thread | None):
    if stop is None or thread is None:
        return
    stop.set()
    thread.join(timeout=0.5)


def _pty_output_tail(output: bytearray) -> str:
    """把有限 PTY 尾输出转换为可展示诊断，去掉 ANSI/控制字符。"""
    text = output.decode("utf-8", "replace")
    text = re.sub(
        r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])",
        "",
        text,
    )
    text = "".join(ch if ch in "\n\t" or ord(ch) >= 32 else " " for ch in text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[-1000:]


def _workspace_bootstrap_dims(layout: str, agent_count: int) -> tuple[int, int]:
    """为隐藏 Herdr client 预留 agent 启动尺寸，避免分屏后 TUI 过窄。"""
    cols = SESSION_BOOTSTRAP_PANE_COLS
    rows = SESSION_BOOTSTRAP_PANE_ROWS
    # session 初建的 shell pane 会一直保留到所有 agent 启动完成，因此分屏布局
    # 必须把它也计入；tab 布局的 pane 不共享同一行/列，无需额外放大。
    pane_count = max(1, agent_count) + 1
    if layout in {"right", "horizontal"}:
        cols = min(terminal.MAX_COLS, cols * pane_count)
    elif layout in {"down", "vertical"}:
        rows = min(terminal.MAX_ROWS, rows * pane_count)
    return cols, rows


@app.post("/api/herdr/inspect-workspace")
def api_inspect_workspace(req: InspectWorkspaceReq):
    """探测创建页需要的 Git 能力，不修改工作目录。"""
    workdir = Path(req.workdir).expanduser().resolve()
    if not workdir.is_dir():
        raise HTTPException(400, "工作目录不存在")
    git_root = _git_root(workdir)
    try:
        dirty = bool(_git(git_root, "status", "--porcelain")) if git_root else False
    except ValueError as exc:
        raise HTTPException(400, f"检查 Git 工作目录失败: {exc}") from exc
    return {
        "workdir": str(workdir),
        "is_git": git_root is not None,
        "git_root": str(git_root) if git_root else None,
        "dirty": dirty,
    }


def _setup_workspace(req: SetupWorkspaceReq):
    """一键工作区初始化:自动建 session → split pane + 启动 → 注册身份 → 通知。

    如果 session 不存在,通过 PTY 自动创建(herdr 需要 TTY 才能 attach/创建)。
    """
    if req.layout not in VALID_LAYOUTS:
        raise HTTPException(400, f"不支持的布局: {req.layout}")
    resolved_workdir = Path(req.workdir).expanduser().resolve()
    if not resolved_workdir.is_dir():
        raise HTTPException(400, "工作目录不存在")
    canonical_project = _canonical_mail_project(resolved_workdir)
    if not herdr_client.is_available():
        return {
            "ok": False,
            "error": f"herdr 未安装或不可执行: {herdr_client.HERDR_BIN}",
            "session": req.session,
            "session_started": False,
            "started": [],
        }
    mail_requirement = _agent_mail_requirement()
    if mail_requirement:
        return {
            **mail_requirement,
            "session": req.session,
            "session_started": False,
            "started": [],
        }
    # 0. 检查 session 是否存在,不存在则自动创建
    sessions = herdr_client.list_sessions()
    states = {s["name"]: s.get("status") for s in sessions}
    active_session_record = next(
        (s for s in sessions if s.get("name") == req.session), None
    )
    session_created = req.session not in states
    session_started = states.get(req.session) == "running"
    initial_session_dir = (
        (active_session_record or {}).get("directory")
        or str(Path(req.workdir).expanduser().resolve())
    )
    existing_project = mail_projects.get(req.session, initial_session_dir)
    if existing_project and existing_project != canonical_project:
        raise HTTPException(
            409,
            f"session {req.session} 已绑定通信项目 {existing_project}；"
            "如需更换，请在会话页显式重新绑定",
        )
    if not session_started and herdr_client.onboarding_required():
        return {
            "ok": False,
            "code": "herdr_onboarding_required",
            "error": (
                "Herdr 首次配置尚未完成。请打开终端完成配置向导，"
                "再按 Ctrl-b d 脱离并重新启动工作区"
            ),
            "herdr_command": (
                f"{shlex.quote(herdr_client.HERDR_BIN)} "
                f"--session {shlex.quote(req.session)}"
            ),
            "session": req.session,
            "session_created": session_created,
            "session_started": False,
            "started": [],
        }
    plans, warnings = _prepare_workspace(req)
    for plan in plans:
        if not plan.get("instance_id"):
            plan["instance_id"] = herdr_client.new_agent_instance_id()
    base_pane_ids: list[str] = []
    if not session_started:
        # 用 PTY 终端创建 session(herdr --session 需要 TTY)
        t = None
        drain_stop = None
        drain_thread = None
        pty_output = bytearray()
        try:
            bootstrap_cols, bootstrap_rows = _workspace_bootstrap_dims(
                req.layout, len(plans),
            )
            t = terminal.create_term(
                req.workdir, cols=bootstrap_cols, rows=bootstrap_rows,
                command=[herdr_client.HERDR_BIN, "--session", req.session],
            )
            drain_stop, drain_thread = _start_pty_drainer(t["id"], pty_output)
            # 直接在 PTY 中 exec Herdr，不能先开用户登录 shell 再输入命令：
            # oh-my-zsh 更新询问等启动提示会吞掉命令开头，造成确定性失败。
            deadline = time.monotonic() + SESSION_START_TIMEOUT
            while time.monotonic() < deadline:
                current_sessions = herdr_client.list_sessions()
                running_record = next(
                    (
                        s for s in current_sessions
                        if s["name"] == req.session and s.get("status") == "running"
                    ),
                    None,
                )
                if running_record:
                    session_started = True
                    active_session_record = running_record
                    break
                time.sleep(0.25)
            if not session_started:
                latest = next(
                    (s for s in herdr_client.list_sessions() if s["name"] == req.session),
                    None,
                )
                _stop_pty_drainer(drain_stop, drain_thread)
                terminal.kill_term(t["id"])
                return {
                    "ok": False,
                    "error": (
                        f"启动 session 超时({SESSION_START_TIMEOUT:g}秒): {req.session}；"
                        f"herdr 状态: {(latest or {}).get('status', '未出现在 session 列表')}。"
                        "请在设置 → 环境自检确认 herdr，或运行 ./doctor.sh"
                    ),
                    "session": req.session,
                    "session_created": session_created,
                    "session_started": False,
                    "started": [],
                    "terminal_output": _pty_output_tail(pty_output),
                }
            # detach:发 Ctrl-b d(herdr detach 序列),让 client 脱离但 session server 继续跑
            terminal.write_term(t["id"], "\x02d")  # Ctrl-b + d
            time.sleep(0.5)  # 等 Herdr client 退出,session server 稳定
            _stop_pty_drainer(drain_stop, drain_thread)
            # 记录 TUI 建 session 自带的初始 shell pane,agent 启动后清理
            # H0.5 保留 CLI:session start mutation 后的即时验证读取
            base_pane_ids = [
                p.get("pane_id")
                for p in herdr_client.snapshot().get("panes", [])
                if p.get("session") == req.session and p.get("pane_id")
            ]
            # 不主动 kill PTY；detach 后 Herdr client 会自行退出，死 PTY 由 sweep 回收。
        except Exception as e:
            _stop_pty_drainer(drain_stop, drain_thread)
            if t is not None and not session_started:
                terminal.kill_term(t["id"])
            return {
                "ok": False,
                "error": f"启动 session 失败: {e}",
                "session": req.session,
                "session_created": session_created,
                "session_started": False,
                "started": [],
                "terminal_output": _pty_output_tail(pty_output),
            }
    session_dir = (
        (active_session_record or {}).get("directory")
        or str(Path(req.workdir).expanduser().resolve())
    )
    mail_binding_ok = True
    try:
        mail_projects.bind(req.session, session_dir, canonical_project)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        mail_binding_ok = False
        warnings.append(f"通信项目绑定保存失败({mail_projects.STATE_PATH}): {exc}")
    results = []
    started = []
    started_instances = []
    started_instance_ids = []
    reused = []
    reused_instances = []
    failed = []
    failed_instances = []
    # 1. 为每个 agent 开 pane + 启动
    for plan in plans:
        agent_type = plan["agent"]
        r = herdr_client.start_agent(
            req.session, plan["workdir"], agent_type, layout=req.layout,
            label=plan["name"], args=plan.get("args", ""),
            instance_id=plan["instance_id"],
        )
        results.append({
            "agent": agent_type, "name": plan["name"],
            "instance_id": plan["instance_id"], "plan": plan, "start": r,
        })
        error = r.get("error")
        if req.participants is not None and r.get("reused"):
            existing_cwd = r.get("cwd")
            same_workdir = bool(existing_cwd) and (
                Path(existing_cwd).expanduser().resolve()
                == Path(plan["workdir"]).expanduser().resolve()
            )
            if same_workdir:
                reused.append(agent_type)
                reused_instances.append(plan["name"])
            else:
                error = f"session 中已存在 {agent_type}，无法应用新的工作目录"
        if r.get("available", True) is False:
            error = error or "Herdr 不可用"
        results[-1]["error"] = error
        if error:
            failed.append({"agent": agent_type, "error": error})
            failed_instances.append({"name": plan["name"], "error": error})
        elif not r.get("reused"):
            started.append(agent_type)
            started_instances.append(plan["name"])
            started_instance_ids.append(plan["instance_id"])
            time.sleep(2)  # 等新 Agent 启动
    # 1.5 清理建 session 时 TUI 自带的空白 shell pane(至少一个 agent 在跑才清,避免空 session)
    closed_panes = []
    if base_pane_ids and started:
        # H0.5 保留 CLI:close pane mutation 前的即时存在性验证
        current = {
            p.get("pane_id"): p
            for p in herdr_client.snapshot().get("panes", [])
            if p.get("session") == req.session
        }
        for pid in base_pane_ids:
            pane = current.get(pid)
            if pane and not pane.get("agent"):
                r = herdr_client.close_pane(req.session, pid)
                if r.get("available", True) and not r.get("error"):
                    closed_panes.append(pid)
    # 1.75 为本轮协作建立稳定 run/task/revision；相同配置幂等复用。
    coordination_run = None
    run_participants = []
    for result in results:
        pane_id = result["start"].get("pane_id")
        if result.get("error") or not pane_id:
            continue
        plan = result["plan"]
        run_participants.append({
            "id": plan["id"], "agent": plan["agent"], "role": plan["role"],
            "task": plan["task"], "workdir": plan["workdir"],
            "pane_id": pane_id,
            "local_name": plan["name"],
            "instance_id": plan["instance_id"],
            "mail_name": (
                _identity_name(
                    canonical_project, plan["agent"], plan["instance_id"],
                )
            ),
        })
    if run_participants:
        try:
            coordination_run = coordination.start_run(
                project_key=canonical_project, session=req.session,
                session_dir=session_dir, participants=run_participants,
            )
        except Exception as exc:
            warnings.append(f"可靠消息 run 建立失败: {exc}")
    # 2. 新版协作工作区向每个成功启动的 Agent 注入角色和任务。
    briefed = []
    briefed_instances = []
    if req.participants is not None:
        for result in results:
            pane_id = result["start"].get("pane_id")
            should_brief = result["plan"]["instance_id"] in started_instance_ids
            if result.get("error") or not should_brief or not pane_id:
                continue
            plan = result["plan"]
            briefing = _workspace_briefing(
                req, plan, plans, coordination_run
            )
            sent = herdr_client.pane_send(req.session, pane_id, briefing, "prompt")
            if sent.get("available", True) is False or sent.get("error"):
                warnings.append(f"{plan['agent']} 的任务说明发送失败")
            else:
                briefed.append(plan["agent"])
                briefed_instances.append(plan["name"])
    # 3. 每个 managed instance 独立注册；同类型实例不再共享 main 身份。
    registration_results: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if not mail_binding_ok:
        mail_status: dict[str, Any] = {
            "available": False, "reason": "通信项目绑定未保存，已跳过身份注册",
        }
    elif not AGENT_MAIL_INIT_SCRIPT.is_file():
        mail_status = {"available": False, "reason": "Agent Mail 未安装，已跳过身份注册"}
    else:
        for result in results:
            plan = result["plan"]
            pane_id = result["start"].get("pane_id")
            if (
                result.get("error") or not pane_id
                or plan["instance_id"] not in started_instance_ids
            ):
                continue
            identity = _started_agent_mail_identity(
                req.session, pane_id, plan["agent"], plan["instance_id"],
                notify=False, project_hint=canonical_project,
            )
            result["agent_mail"] = identity
            registration_results.append((result, identity))
            if identity.get("warning"):
                warnings.append(f"{plan['name']} Agent Mail: {identity['warning']}")
        failed_registration = [
            identity.get("warning") or "身份未注册"
            for _, identity in registration_results if not identity.get("registered")
        ]
        reg_ok = bool(registration_results) and not failed_registration
        mail_status = {
            "available": reg_ok,
            "reason": "；".join(str(item) for item in failed_registration) or None,
        }

    # 4. 精确身份注册完成后建立 coordination 绑定并通知各自 pane。
    notified = []
    roster = "；".join(
        f"{result['plan']['name']}[{result['plan']['instance_id'][-6:]}]={identity['name']}"
        for result, identity in registration_results if identity.get("registered")
    )
    for result, identity in registration_results:
        if not identity.get("registered"):
            continue
        plan = result["plan"]
        pane_id = result["start"]["pane_id"]
        my_name = str(identity["name"])
        context = None
        if coordination_run:
            try:
                coordination.bind_identity(
                    coordination_run["run_id"], plan["id"], my_name, pane_id
                )
                context = coordination.run_context(
                    coordination_run["run_id"], my_name
                )
            except Exception:
                logger.exception(
                    "coordination identity context failed: %s/%s",
                    req.session, plan["id"],
                )
                warnings.append(f"{plan['name']} 可靠消息身份上下文建立失败")
        hint = _identity_hint(
            my_name, canonical_project, plan["agent"], roster=roster,
            coordination_context=context, instance_id=plan["instance_id"],
        )
        sent = herdr_client.pane_send(req.session, pane_id, hint, "prompt")
        if sent.get("available", True) is False or sent.get("error"):
            warnings.append(f"{plan['name']} 身份告知发送失败")
            continue
        identity["notified"] = True
        notified.append(f"{plan['name']}→{my_name}")
    reg_ok = mail_status.get("available") is True
    return {
        "ok": not failed, "session": req.session, "workdir": req.workdir,
        "session_created": session_created, "session_started": session_started,
        "started": started, "started_instances": started_instances,
        "started_instance_ids": started_instance_ids,
        "reused": reused, "reused_instances": reused_instances,
        "failed": failed, "failed_instances": failed_instances, "results": results,
        "idempotent": bool(reused and not started and not failed), "registered": reg_ok,
        "notified": notified, "briefed": briefed,
        "briefed_instances": briefed_instances, "agent_mail": mail_status,
        "mode": req.mode, "workspaces": plans, "warnings": warnings,
        "closed_panes": closed_panes, "mail_project": canonical_project,
        "coordination": coordination_run,
    }


@app.post("/api/herdr/setup-workspace")
def api_setup_workspace(req: SetupWorkspaceReq):
    """串行处理同名 session 的创建请求，不阻塞其他 session。"""
    _validate_session_name(req.session)
    with _SETUP_WORKSPACE_LOCKS_GUARD:
        lock = _SETUP_WORKSPACE_LOCKS.setdefault(req.session, threading.Lock())
    if not lock.acquire(blocking=False):
        raise HTTPException(409, f"session {req.session} 正在创建，请勿重复提交")
    try:
        return _setup_workspace(req)
    finally:
        lock.release()


@app.post("/api/herdr/pane/{session}/{pane_id}/tell-identity")
def api_herdr_pane_tell_identity(session: str, pane_id: str):
    """手动给单个 pane 发身份告知(适用于手动在 herdr 里开的新 pane)。"""
    _validate_session_name(session)
    _validate_pane_id(pane_id)
    snap = _herdr_runtime_snapshot()
    p = next(
        (
            x for s in snap.get("sessions", []) if s.get("session") == session
            for x in s.get("panes", []) if x.get("pane_id") == pane_id
        ),
        None,
    ) or next(
        (
            x for x in snap.get("panes", [])
            if x.get("session") == session and x.get("pane_id") == pane_id
        ),
        None,
    )
    if not p:
        raise HTTPException(404, "pane 不存在")
    cwd = p.get("cwd") or ""
    agent_type = p.get("agent") or ""
    if not agent_type:
        raise HTTPException(400, "该 pane 不是 agent(herdr 未检测到),请等 agent 启动后再试")
    if not cwd:
        raise HTTPException(400, "该 pane 无 cwd")
    mail_status = db.status()
    if not mail_status["available"]:
        return {"ok": False, "unavailable": True, "error": mail_status["reason"]}
    state = _mail_project_state(session)
    if not state["bound"]:
        return {
            "ok": False,
            "code": "mail_project_required",
            "error": "请先为该 session 选择通信项目",
            **state,
        }
    project = state["project"]
    descriptor = herdr_client.get_launch_descriptor(session, pane_id)
    instance_id = (
        str(descriptor.get("instance_id"))
        if descriptor and descriptor.get("instance_id") else None
    )
    mail_agent = (
        str(descriptor.get("agent") or "") if instance_id else str(agent_type)
    )
    if instance_id and not mail_agent:
        return {
            "ok": False,
            "needs_registration": True,
            "project": project,
            "error": "managed descriptor 缺少 product agent",
        }
    my_name = (
        _identity_name(project, mail_agent, instance_id)
        if instance_id else _identity_name(project, mail_agent)
    )
    if not my_name:
        return {
            "ok": False,
            "needs_registration": True,
            "project": project,
            "error": "该通信项目下没有此 agent 的有效身份（未注册或已 retired）",
        }
    hint = _identity_hint(
        my_name, project, mail_agent, instance_id=instance_id,
    )
    result = herdr_client.pane_send(session, pane_id, hint, "prompt")
    response = {
        "ok": "error" not in result, "pane_id": pane_id, "agent": agent_type,
        "name": my_name, "project": project, "result": result,
    }
    if instance_id:
        response["instance_id"] = instance_id
        response["display_name"] = descriptor.get("display_name") or agent_type
    return response


@app.get("/api/herdr/session/{name}/mail-project")
def api_herdr_session_mail_project(name: str):
    """查询 session 的 canonical Agent Mail 项目和旧数据候选。"""
    _validate_session_name(name)
    return _mail_project_state(name)


@app.post("/api/herdr/session/{name}/init-mail")
def api_herdr_session_init_mail(name: str, req: MailProjectReq | None = None):
    """用 session 已绑定的 human key 注册身份并通知 agent pane。"""
    _validate_session_name(name)
    if req and req.project:
        project, _ = _bind_mail_project(name, req.project, replace=req.replace)
    else:
        state = _mail_project_state(name)
        if not state["bound"]:
            return {
                "ok": False,
                "code": "mail_project_required",
                "error": "请选择该 session 使用的 Agent Mail 通信项目",
                **state,
            }
        project = state["project"]
    snap = _herdr_runtime_snapshot()
    sess = next((s for s in snap.get("sessions", []) if s.get("session") == name), None)
    if not sess:
        raise HTTPException(404, f"session 不存在: {name}")
    if not AGENT_MAIL_INIT_SCRIPT.is_file():
        return {"ok": False, "unavailable": True, "error": "Agent Mail 未安装"}
    try:
        panes = [
            p for p in sess.get("panes", [])
            if p.get("agent") and p.get("pane_id")
        ]
        managed: dict[str, dict[str, Any]] = {}
        legacy = []
        for pane in panes:
            pane_id = str(pane["pane_id"])
            descriptor = herdr_client.get_launch_descriptor(name, pane_id)
            if descriptor and descriptor.get("instance_id"):
                managed[pane_id] = descriptor
            else:
                legacy.append(pane)

        output = ""
        if legacy:
            r = subprocess.run(
                [str(AGENT_MAIL_INIT_SCRIPT), "--project", project], cwd=project,
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                detail = (r.stderr or r.stdout)[-300:]
                return {
                    "ok": False, "project": project,
                    "error": detail or "am-init-project 失败",
                }
            output = r.stdout[-300:] if r.stdout else ""

        notified = []
        missing_identities = []
        identity_status = []
        for p in panes:
            agent_type = p.get("agent")
            pane_id = str(p["pane_id"])
            descriptor = managed.get(pane_id)
            if descriptor:
                instance_id = str(descriptor["instance_id"])
                mail_agent = str(descriptor.get("agent") or "")
                if not mail_agent:
                    status = {
                        "registered": False, "notified": False,
                        "instance_id": instance_id,
                        "warning": "managed descriptor 缺少 product agent",
                    }
                    identity_status.append(status)
                    missing_identities.append(f"{agent_type}({instance_id})")
                    continue
                status = _started_agent_mail_identity(
                    name, pane_id, mail_agent, instance_id, notify=True,
                    project_hint=project,
                )
                identity_status.append(status)
                my_name = status.get("name")
                if status.get("registered") and status.get("notified") and my_name:
                    notified.append(f"{agent_type}({pane_id})→{my_name}")
                else:
                    missing_identities.append(f"{agent_type}({instance_id})")
                continue
            my_name = _identity_name(project, agent_type)
            if not my_name:
                missing_identities.append(agent_type)
                continue
            hint = _identity_hint(my_name, project, agent_type, registered=True)
            herdr_client.pane_send(name, pane_id, hint, "prompt")
            notified.append(f"{agent_type}({pane_id})→{my_name}")
        managed_failures = [
            status for status in identity_status
            if not status.get("registered") or not status.get("notified")
        ]
        return {
            "ok": not managed_failures, "project": project,
            "notified": notified,
            "missing_identities": missing_identities,
            "identity_status": identity_status,
            "partial": bool(notified and managed_failures),
            "output": output,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Web 终端(PTY,完整交互:斜杠/Esc/vim) ─────────────────────

@app.post("/api/term")
def api_term_create(
    cwd: str | None = None,
    cols: int = 80,
    rows: int = 24,
    label: str | None = None,
    replace_existing: bool | None = None,
):
    """创建一个新终端会话(PTY bash)。"""
    try:
        should_replace = (
            label is not None if replace_existing is None else replace_existing
        )
        create = (
            terminal.replace_labeled_term
            if should_replace
            else terminal.create_term
        )
        return create(cwd, cols, rows, label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(429, str(e))
    except Exception as e:
        raise HTTPException(500, f"创建终端失败: {e}")


@app.get("/api/term")
def api_term_list():
    """列出所有终端会话。"""
    return {"terms": terminal.list_terms()}


@app.delete("/api/term/{term_id}")
def api_term_kill(term_id: str):
    """关闭终端。"""
    _release_zoom_leases_for_owner(term_id)
    terminal.kill_term(term_id)
    return {"ok": True}


async def _claim_term_websocket(
    term_id: str, websocket: WebSocket,
) -> dict[str, Any]:
    """最新页面独占 PTY 输出；旧连接立即停泵且不得重新抢占。"""
    connection: dict[str, Any] = {"websocket": websocket, "pump_task": None}
    previous = _TERM_WS_CONNECTIONS.get(term_id)
    _TERM_WS_CONNECTIONS[term_id] = connection
    if previous:
        previous_pump = previous.get("pump_task")
        if previous_pump:
            previous_pump.cancel()
            with suppress(asyncio.CancelledError):
                await previous_pump
        previous_websocket = previous.get("websocket")
        if previous_websocket:
            with suppress(Exception):
                await previous_websocket.close(
                    code=TERM_WS_TAKEN_OVER_CODE,
                    reason="terminal opened by a newer page",
                )
    return connection


def _term_websocket_is_current(term_id: str, connection: dict[str, Any]) -> bool:
    return _TERM_WS_CONNECTIONS.get(term_id) is connection


def _release_term_websocket(term_id: str, connection: dict[str, Any]) -> None:
    if _term_websocket_is_current(term_id, connection):
        _TERM_WS_CONNECTIONS.pop(term_id, None)


async def _drain_term_input_notes(term_id: str) -> None:
    """合并连续按键的后台记录，避免 note 任务挤满默认线程池。"""
    try:
        while term_id in _TERM_INPUT_NOTE_PENDING:
            _TERM_INPUT_NOTE_PENDING.discard(term_id)
            try:
                await asyncio.to_thread(terminal.note_user_input, term_id)
            except Exception:
                logger.exception("terminal input note failed: %s", term_id)
    finally:
        if _TERM_INPUT_NOTE_TASKS.get(term_id) is asyncio.current_task():
            _TERM_INPUT_NOTE_TASKS.pop(term_id, None)


def _schedule_term_input_note(term_id: str) -> None:
    _TERM_INPUT_NOTE_PENDING.add(term_id)
    task = _TERM_INPUT_NOTE_TASKS.get(term_id)
    if task is None or task.done():
        _TERM_INPUT_NOTE_TASKS[term_id] = asyncio.create_task(
            _drain_term_input_notes(term_id)
        )


@app.websocket("/api/term/{term_id}")
async def api_term_ws(websocket: WebSocket, term_id: str):
    """终端 WebSocket 双向桥接:浏览器↔PTY。"""
    trusted = _websocket_authenticated(websocket)
    if not COCKPIT_TOKEN:
        trusted = trusted and _no_token_scope_trusted(
            websocket.scope, require_origin=True,
        )
    else:
        trusted = trusted and _same_origin(
            websocket.headers.get("origin"), websocket.headers.get("host")
        )
    if not trusted:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    # 会话存在性校验
    terms_now = {
        t["id"] for t in terminal.list_terms() if t.get("alive", True)
    }
    if term_id not in terms_now:
        superseded = terminal.was_superseded(term_id)
        await websocket.send_text(
            "\r\n[终端已由更新的页面接管]\r\n"
            if superseded
            else "\r\n[终端会话不存在,已关闭]\r\n"
        )
        await websocket.close(
            code=TERM_WS_TAKEN_OVER_CODE if superseded else TERM_WS_INVALID_CODE,
            reason=(
                "terminal replaced by a newer page"
                if superseded
                else "terminal session no longer exists"
            ),
        )
        return
    connection = await _claim_term_websocket(term_id, websocket)
    connection_lease = runtime_stats.open_connection("terminal_websocket")
    pump_task = None
    try:
        if not _term_websocket_is_current(term_id, connection):
            return
        if websocket.query_params.get("replay") == "1":
            history = await asyncio.to_thread(terminal.output_history, term_id)
            if not _term_websocket_is_current(term_id, connection):
                return
            if history:
                await websocket.send_bytes(history)
            await websocket.send_json({"type": "replay_complete"})
        # 输出转发任务:PTY → WebSocket
        async def pump_out():
            while _term_websocket_is_current(term_id, connection):
                # asyncio 取消不会停止已启动的 to_thread；接管时必须等旧读完成，
                # 否则旧线程可能在新页面重放之后抢走一块 PTY 输出。
                read_task = asyncio.create_task(asyncio.to_thread(
                    terminal.read_available, term_id, TERM_READ_WAIT, TERM_READ_BURST,
                ))
                try:
                    data = await asyncio.shield(read_task)
                except asyncio.CancelledError:
                    with suppress(asyncio.CancelledError):
                        await read_task
                    raise
                try:
                    if data:
                        await websocket.send_bytes(data)
                    elif not terminal.is_alive(term_id):
                        if terminal.was_superseded(term_id):
                            await websocket.close(
                                code=TERM_WS_TAKEN_OVER_CODE,
                                reason="terminal replaced by a newer page",
                            )
                            break
                        tail = await asyncio.to_thread(
                            terminal.drain_output, term_id, 0.05
                        )
                        if tail:
                            await websocket.send_bytes(tail)
                        await websocket.send_text("\r\n[进程已退出]\r\n")
                        await websocket.close(
                            code=TERM_WS_INVALID_CODE,
                            reason="terminal process exited",
                        )
                        break
                except WebSocketDisconnect:
                    break
        pump_task = asyncio.create_task(pump_out())
        connection["pump_task"] = pump_task
        # 主循环:接收浏览器输入
        while _term_websocket_is_current(term_id, connection):
            msg = await websocket.receive()
            if not _term_websocket_is_current(term_id, connection):
                break
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            if text:
                if text.startswith("{"):
                    try:
                        ctrl = json.loads(text)
                        if isinstance(ctrl, dict) and ctrl.get("type") == "resize":
                            terminal.resize_term(term_id, ctrl.get("cols", 80), ctrl.get("rows", 24))
                            continue
                        if isinstance(ctrl, dict) and ctrl.get("type") == "theme":
                            mode = ctrl.get("mode")
                            notify = ctrl.get("notify") is True
                            await asyncio.to_thread(
                                terminal.set_color_scheme,
                                term_id,
                                mode,
                                notify=notify,
                            )
                            continue
                    except json.JSONDecodeError:
                        pass
                try:
                    # 先写 PTY(低延迟路径):用户输入的回显只取决于数据多快到
                    # PTY 又被 pump_out 发回来。note_user_input 内部会 fork herdr
                    # 子进程解析落点 pane(server.py 注释原称约 2ms,实测 fork+exec
                    # 在 Linux 上 5-15ms、macOS 更慢),把它放到 write 之后且不 await,
                    # 避免每个按键的回显都等一次 fork——这是终端输入卡顿的主因。
                    # note_user_input 只产生副作用(记录避让时间戳),返回值不用于
                    # 决定本次写入；连续按键合并为单任务 + 一次尾随记录，避免
                    # 大量 to_thread 任务反过来挤占 write_term 使用的默认线程池。
                    await asyncio.to_thread(terminal.write_term, term_id, text)
                    _schedule_term_input_note(term_id)
                except (TimeoutError, OSError) as e:
                    logger.warning("terminal input write failed %s: %s", term_id, e)
                    await websocket.send_text(f"\r\n[输入未完整写入: {e}]\r\n")
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("terminal websocket failed: %s", term_id)
    finally:
        try:
            if pump_task:
                pump_task.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await pump_task
            _release_term_websocket(term_id, connection)
            with suppress(Exception):
                await websocket.close()
        finally:
            connection_lease.close()


# ── SSE 实时推送(看板状态变化) ────────────────────────────────

# ── H0.5 socket 状态客户端(看板/Attention/SSE 可选数据源) ─────
# off 全量沿用 CLI；canary 仅 allowlist session 读 H0.4 线程安全缓存，
# 其他 session 仍走 CLI；on 全量读缓存。启用缓存后不再周期性 fork 对应
# session 的 herdr api snapshot；CLI 仍保留给 mutation、能力门和 session
# 发现(list_sessions)。socket/server 中断时
# 继续服务旧缓存并显式 degraded,恢复后由 H0.4 客户端自动 resync;
# 无可靠缓存时显式 unavailable,禁止静默回退旧轮询 fork。
# 每 session 一个独立客户端:增删/socket 变化只影响该 session,旧健康
# session 缓存持续可读,无整体空窗;发现失败 fail-closed,保留旧客户端
# 与缓存,绝不清空。

_STATE_CLIENT_LOCK = threading.RLock()
_state_clients: dict[str, herdr_state.HerdrStateClient] = {}
# 发现层元数据:name -> {"socket","directory"};仅 running session。
_state_sessions_meta: dict[str, dict[str, str]] = {}
_state_discovery_ok = False
_state_discovery_reason = "state client not running"
# H0.5 R3:lifespan running 闸门 + epoch。publish 前在同一把锁内校验
# running+epoch+old/meta(CAS);lifespan stop 关闸门并递增 epoch,同进程新
# lifespan 重新 open 后旧 epoch 仍失效。
_state_running = False
_state_epoch = 0
# socket 换入超时未就绪的持久降级:name -> {"reason"};旧客户端缓存继续可读,
# 但 snapshot/SSE 显式 degraded,retry 成功换入后清除。
_state_swap_pending: dict[str, dict[str, str]] = {}
# H0.5 R5:已登记未发布的候选 owner record(不可变:epoch/name/token/client
# identity)。登记同名活跃候选时跳过复用;摘除仅当 current is owner(身份
# 比较),旧 epoch/旧 worker 绝不能移除新候选。start/stop 线性化由
# HerdrStateClient._lifecycle_lock 保证(stop 先取得则 start 拒绝生线程)。
@dataclass(frozen=True)
class _InflightOwner:
    epoch: int
    name: str
    token: int
    client: "herdr_state.HerdrStateClient"


_state_inflight: dict[str, _InflightOwner] = {}
_inflight_token = itertools.count(1)
# H0.5 R6:retiring ownership。任何"已摘下但尚未真正停止"的 client 都必须
# 在同一锁临界区 identity-safe 转移进 _state_retiring(token->owner 真引用),
# 不得只存在于 reconcile 局部变量;request_stop+join 真正成功后才摘除。
_state_retiring: dict[int, _InflightOwner] = {}
# deadline 耗尽仍存活的 client:保留真实对象与 ownership,可重复
# request_stop/join(reaper/再次 stop 重试);诊断序列化与内部引用分离。
_state_survivors: dict[int, _InflightOwner] = {}
# H0.5 R7:reap/生命周期串行锁。global stop 阶段二、reaper、reconcile 的
# to_reap 回收、open 的有界 reap 全部在此锁下串行(single-owner);锁序
# 恒为 REAP→CLIENT,禁止反序。survivor 转移靠锁内身份 CAS,并发成功摘除
# 的 owner 不得复活/重复报告。
_STATE_REAP_LOCK = threading.Lock()
# H0.5 R10/R11:shutdown intent ticket(独立 TICKET 锁保护)。R11:登记改经
# 专用 _STATE_TICKET_LOCK 严格短临界区,不依赖可能长持有的 CLIENT 锁;
# open/reconcile 的读取与 stop 完成的消费全部经同一锁同步,CAS 场景
# check+commit 在 TICKET 临界区内与登记线性化。stop 在尝试 REAP 之前即
# 登记 ticket;timed acquire 耗尽返回 deferred 时 ticket 持久——open 必须
# False,reconcile 注册/发布/换入 CAS 必须拒绝,不能只靠调用者看到返回值。
# stop 完成时仅清除 <= 自己 ticket 的 intent(覆盖更早的 deferred 请求;
# 较早完成绝不清除较晚 ticket)。TICKET 是叶子锁:持 TICKET 时绝不再取
# REAP/CLIENT,锁序恒 REAP→CLIENT→TICKET。
_STATE_TICKET_LOCK = threading.Lock()
_state_stop_tickets: set[int] = set()
_state_stop_ticket_seq = itertools.count(1)


def _register_stop_ticket() -> int:
    """TICKET 锁短临界区登记 shutdown intent,返回单调 ticket 号。"""
    with _STATE_TICKET_LOCK:
        ticket = next(_state_stop_ticket_seq)
        _state_stop_tickets.add(ticket)
        return ticket


def _stop_tickets_pending() -> bool:
    """TICKET 锁下读取是否有未消费 shutdown intent(门禁用)。"""
    with _STATE_TICKET_LOCK:
        return bool(_state_stop_tickets)


def _consume_stop_tickets(ticket: int, timeout: float = 5.0) -> bool:
    """stop 完成:仅清除 <= 本 ticket 的 intent(覆盖更早 deferred);
    更晚 ticket 保留——较早完成绝不清除较晚 ticket。
    R13:bounded TICKET acquire(timeout);拿不到→False(ticket 保留)。"""
    if not _STATE_TICKET_LOCK.acquire(timeout=timeout):
        return False
    try:
        for pending in [t for t in _state_stop_tickets if t <= ticket]:
            _state_stop_tickets.discard(pending)
    finally:
        _STATE_TICKET_LOCK.release()
    return True
# socket 变化时等新客户端完成 bootstrap 的有界就绪窗口(秒)。
STATE_SWAP_READY_TIMEOUT_S = 2.0
# stop 对全部唯一 client(published+inflight)共享的总 join 预算(秒),
# 不随 session 数叠加。
STATE_STOP_JOIN_TIMEOUT_S = 5.0


def _state_discovery_interval() -> float:
    try:
        return max(1.0, float(os.environ.get("COCKPIT_STATE_DISCOVERY_INTERVAL", "10")))
    except ValueError:
        return 10.0


def _retire_client_locked(
    name: str, epoch: int, client: herdr_state.HerdrStateClient
) -> _InflightOwner:
    """锁内调用:把已摘下的 client 所有权 identity-safe 转移进
    _state_retiring(真实引用受管,不停成功不摘)。"""
    owner = _InflightOwner(
        epoch=epoch, name=name, token=next(_inflight_token), client=client,
    )
    _state_retiring[owner.token] = owner
    return owner


def _reap_owner(owner: _InflightOwner, absolute_deadline: float) -> bool:
    """_STATE_REAP_LOCK 下调用:锁外 stop+join(剩余窗口);真正成功才经
    锁内身份 CAS 摘除。phase2 CLIENT reacquire 用 remaining 有界获取
    (#1883);拿不到→False(deferred,owner 保留不清 ticket/不丢 owner),
    绝不阻塞。survivor 转移由调用方(stop 路径)做,reaper 不转移。"""
    remaining = max(0.0, absolute_deadline - time.monotonic())
    try:
        ok = owner.client.stop(join_timeout=remaining)
    except Exception:
        logger.exception("herdr state client stop failed: %s", owner.name)
        ok = False
    if not ok:
        return False
    remaining = max(0.0, absolute_deadline - time.monotonic())
    if not _STATE_CLIENT_LOCK.acquire(timeout=remaining):
        return False  # deferred:拿不到 CLIENT,owner 保留在 retiring/survivors
    try:
        if _state_retiring.get(owner.token) is owner:
            del _state_retiring[owner.token]
        if _state_survivors.get(owner.token) is owner:
            del _state_survivors[owner.token]
    finally:
        _STATE_CLIENT_LOCK.release()
    return True


def _pending_owners_locked() -> list[_InflightOwner]:
    """_STATE_CLIENT_LOCK 下调用:全部未决 ownership(retiring+survivors)。"""
    return list(_state_retiring.values()) + [
        o for o in _state_survivors.values() if o.token not in _state_retiring
    ]


def _reap_owners(owners: list[_InflightOwner], budget: float) -> None:
    """REAP 锁下按共享预算回收一批 owner(stop/reaper/reconcile 共用,
    保证 single-owner 串行)。"""
    if not owners:
        return
    with _STATE_REAP_LOCK:
        deadline = time.monotonic() + budget
        for owner in owners:
            _reap_owner(owner, deadline)


def _reap_retired_clients() -> None:
    """reaper:reconcile 每轮重试回收 retiring/survivors(重复
    request_stop+join,共享小 deadline 不叠加;REAP 锁串行)。"""
    with _STATE_CLIENT_LOCK:
        owners = _pending_owners_locked()
    _reap_owners(owners, budget=1.0)


def _open_state_clients() -> bool:
    """lifespan 启动(R7 门禁):先在有界窗口内 reap 未决 retiring/survivors,
    锁内确认全空才开新 running epoch;否则 fail-closed 返回 False——旧同名
    线程未退绝不发布新同名 client,回收后才允许 restart。"""
    global _state_running, _state_epoch
    deadline = time.monotonic() + STATE_STOP_JOIN_TIMEOUT_S
    with _STATE_REAP_LOCK:
        while True:
            with _STATE_CLIENT_LOCK:
                owners = _pending_owners_locked()
            if not owners or time.monotonic() >= deadline:
                break
            for owner in owners:
                _reap_owner(
                    owner, deadline
                )
            time.sleep(0.05)
        with _STATE_CLIENT_LOCK:
            # R11:check+commit 与 ticket 登记经 TICKET 锁线性化——登记
            # 先于本临界区完成则 open 拒绝;反之 open 先提交,登记其后。
            with _STATE_TICKET_LOCK:
                if _state_retiring or _state_survivors or _state_stop_tickets:
                    return False  # fail-closed:未决 ownership/shutdown intent 拒绝开新 epoch
                _state_running = True
                _state_epoch += 1
                return True


def _stop_state_client() -> list[dict[str, Any]]:
    """lifespan 关闭(R8:全事务线性化)。整个事务(关闸/增 epoch/摘
    ownership、广播/join、survivor 转移)在 _STATE_REAP_LOCK 下与 open
    全事务互斥——open 设 running 与 stop 关闸绝不交错,锁内顺序决定最终
    running,stop 返回后不会被较早开始的 open 恢复。阶段一(同一 CLIENT
    临界区):关 running 闸门、递增 epoch、把 published+inflight 全部
    identity-safe 转移进 _state_retiring(与既有 retiring 合并),清空
    clients/inflight/meta/swap-pending/discovery。阶段二:先向快照内所有
    唯一 client(published+inflight+retiring+既往 survivors 重试)广播
    request_stop,再按共享总 deadline 逐个 join;真正退出才摘除。deadline
    耗尽仍存活者移入 _state_survivors 保留真实引用与 ownership(可重复
    request_stop/join/reaper),返回诊断列表(序列化与内部引用分离);
    空列表=全部干净退出。"""
    global _state_clients, _state_sessions_meta, _state_running, _state_epoch
    global _state_discovery_ok, _state_discovery_reason, _state_swap_pending
    global _state_inflight
    # R15:ticket 登记是持久 shutdown intent 的线性化前奏，也是唯一预算
    # 例外。登记成功后立即建立 absolute deadline；之后 REAP/CLIENT、join、
    # phase2 bookkeeping 与完成消费 TICKET 全部只使用同一 remaining。
    ticket = _register_stop_ticket()
    absolute_deadline = time.monotonic() + STATE_STOP_JOIN_TIMEOUT_S
    if not _STATE_REAP_LOCK.acquire(
        timeout=max(0.0, absolute_deadline - time.monotonic())
    ):
        return [{
            "deferred": True,
            "reason": "reap_lock_timeout",
            "budget_s": STATE_STOP_JOIN_TIMEOUT_S,
            "ticket": ticket,
        }]
    try:
        if not _STATE_CLIENT_LOCK.acquire(
            timeout=max(0.0, absolute_deadline - time.monotonic())
        ):
            return [{
                "deferred": True,
                "reason": "client_lock_timeout",
                "budget_s": STATE_STOP_JOIN_TIMEOUT_S,
                "ticket": ticket,
            }]
        try:
            _state_running = False
            _state_epoch += 1
            epoch = _state_epoch
            for name, client in _state_clients.items():
                _retire_client_locked(name, epoch, client)
            for owner in _state_inflight.values():
                _state_retiring[owner.token] = owner
            _state_clients = {}
            _state_inflight = {}
            _state_sessions_meta = {}
            _state_swap_pending = {}
            _state_discovery_ok = False
            _state_discovery_reason = "state client not running"
            targets: dict[int, _InflightOwner] = {}
            for owner in list(_state_retiring.values()) + list(
                _state_survivors.values()
            ):
                targets[id(owner.client)] = owner
        finally:
            _STATE_CLIENT_LOCK.release()
        owners = list(targets.values())
        # 阶段二:先广播 cancel,再按入口绝对 deadline 的剩余窗口逐个 join
        for owner in owners:
            try:
                owner.client.request_stop()
            except Exception:
                logger.exception("herdr state client request_stop failed")
        survived: list[_InflightOwner] = []
        for owner in owners:
            if not _reap_owner(owner, absolute_deadline):
                # R12:phase2 survivor 转移用 bounded CLIENT reacquire(remaining);
                # 拿不到→deferred(owner 保留在 retiring,不清 ticket/不丢 owner)
                remaining = max(0.0, absolute_deadline - time.monotonic())
                if _STATE_CLIENT_LOCK.acquire(timeout=remaining):
                    try:
                        if _state_retiring.get(owner.token) is owner:
                            del _state_retiring[owner.token]
                            _state_survivors[owner.token] = owner
                            survived.append(owner)
                        elif _state_survivors.get(owner.token) is owner:
                            survived.append(owner)
                    finally:
                        _STATE_CLIENT_LOCK.release()
                # 拿不到 CLIENT → deferred:owner 留在 retiring,不进 survived
        # R15:retiring/survivors 只能在 CLIENT 锁下读取。拿不到锁或仍有
        # retiring 都表示本轮未完成：保留 ticket，返回明确 deferred。
        remaining = max(0.0, absolute_deadline - time.monotonic())
        if not _STATE_CLIENT_LOCK.acquire(timeout=remaining):
            return [{
                "deferred": True,
                "reason": "phase2_state_lock_timeout",
                "ticket": ticket,
                "budget_s": STATE_STOP_JOIN_TIMEOUT_S,
            }]
        try:
            pending_retiring = len(_state_retiring)
            pending_survivors = len(_state_survivors)
        finally:
            _STATE_CLIENT_LOCK.release()
        if pending_retiring:
            return [{
                "deferred": True,
                "reason": "phase2_incomplete",
                "ticket": ticket,
                "budget_s": STATE_STOP_JOIN_TIMEOUT_S,
                "pending_retiring": pending_retiring,
                "pending_survivors": pending_survivors,
            }]
        # 全部完成(reaped 或 survivor):bounded TICKET 消费 <= 本 ticket
        remaining = max(0.0, absolute_deadline - time.monotonic())
        if not _consume_stop_tickets(ticket, timeout=remaining):
            # TICKET 拿不到→deferred:不清 ticket,返回诊断
            return [{
                "deferred": True,
                "reason": "ticket_consume_timeout",
                "ticket": ticket,
                "budget_s": STATE_STOP_JOIN_TIMEOUT_S,
                "pending_retiring": pending_retiring,
                "pending_survivors": pending_survivors,
            }]
    finally:
        _STATE_REAP_LOCK.release()
    diagnostics: list[dict[str, Any]] = []
    for owner in survived:
        alive = [
            t.name for t in threading.enumerate()
            if t.name.startswith(f"cockpit-state-{owner.name}") and t.is_alive()
        ]
        sessions = getattr(owner.client, "_sessions", None) or getattr(
            owner.client, "sessions", {}
        )
        diagnostics.append({
            "client": repr(owner.client),
            "sessions": sorted(sessions),
            "name": owner.name,
            "epoch": owner.epoch,
            "token": owner.token,
            "alive_state_threads": alive,
        })
    return diagnostics


def _discover_running_sessions() -> dict[str, dict[str, str]] | None:
    """CLI session 发现(单 fork)。返回 None 表示发现不可用/失败(fail-closed,
    调用方必须保留旧客户端与缓存);空 dict 表示成功发现零 session。"""
    if not herdr_client.is_available():
        return None
    try:
        discovered = herdr_client.list_sessions()
    except Exception:
        logger.exception("herdr state session discovery failed")
        return None
    # list_sessions 对 JSON/兼容表格两条命令都失败时吞错返回 [] 并置失败
    # 标志(threading.local,与本调用同线程可读);空列表+标志=失败而非空态。
    if getattr(herdr_client._LIST_SESSIONS_FAILED, "value", False):
        return None
    running = {
        str(item["name"]): {
            "socket": str(item.get("socket") or ""),
            "directory": str(item.get("directory") or ""),
        }
        for item in discovered
        if item.get("status") == "running" and item.get("socket")
    }
    if H0_STATE_MODE == "canary":
        return {
            name: item for name, item in running.items()
            if _h0_state_session_enabled(name)
        }
    return running


def _build_session_client(name: str, socket_path: str) -> herdr_state.HerdrStateClient:
    """仅构建候选客户端,不启动线程。线程启动必须在 inflight 登记后的
    同一临界区内进行(见 _reconcile_state_client)。"""
    return herdr_state.HerdrStateClient({name: socket_path})


def _client_ready(client: herdr_state.HerdrStateClient, name: str) -> bool:
    try:
        store = client.snapshot_cached()
    except Exception:
        return False
    if not store.get("available"):
        return False
    state = client.state().get("sessions", {}).get(name, {}).get("state")
    return state == "subscribed"


def _candidate_cancelled(name: str, owner: _InflightOwner) -> bool:
    """候选是否已被取消:stop 关闸/epoch 变迁/inflight 易主(身份比较,
    旧 worker 不会误认新 epoch 同名候选)。ready 等待每轮调用。"""
    with _STATE_CLIENT_LOCK:
        return (
            not _state_running
            or _state_epoch != owner.epoch
            or _state_inflight.get(name) is not owner
        )


def _start_candidate(
    name: str, epoch: int, client: herdr_state.HerdrStateClient,
    owner: _InflightOwner,
) -> bool:
    """锁外启动候选;False/异常/partial-start 统一 identity-safe 收尾:
    仍是 owner 则 CLIENT 临界区 del inflight→retiring,经统一 REAP 入口
    回收;已被 global stop 摘取则不双停。异常记录且不炸掉整个 reconcile。
    返回是否已成功启动(调用方继续 publish CAS)。"""
    try:
        started = client.start()
    except Exception:
        logger.exception("herdr state candidate start failed: %s", name)
        started = False
    if started:
        return True
    iter_reap: list[_InflightOwner] = []
    with _STATE_CLIENT_LOCK:
        if _state_inflight.get(name) is owner:
            del _state_inflight[name]
            iter_reap.append(_retire_client_locked(name, epoch, client))
    _reap_owners(iter_reap, STATE_STOP_JOIN_TIMEOUT_S)
    return False


def _reconcile_state_client() -> None:
    """按发现结果增量对齐 per-session 客户端。

    - 发现失败:fail-closed,只标记 discovery degraded,不动旧客户端/缓存。
    - 新增 session:锁内登记不可变 owner record(同名活跃候选跳过复用),
      锁外 start(stop 先取得所有权则 start 拒绝不生线程),再 owner+epoch
      CAS 原子 inflight→published;bootstrap 前该 session 显式 degraded。
    - 删除 session:只停止并摘除该 session 的客户端。
    - socket 路径变化(restart):同样 owner 登记+锁外 start,锁外有界等待
      就绪(每轮观察取消);ready 后 owner+epoch+old-client CAS 通过才原子
      换入并停旧;超时弃新留旧,记录 _state_swap_pending 持久 degraded,
      下轮发现自动重试,成功后清降级。
    - ownership:摘除仅当 current is owner(身份比较);stop 摘走的候选
      reconcile 绝不再 stop/publish,旧 epoch worker 摘不到新同名候选。
    """
    global _state_discovery_ok, _state_discovery_reason, _state_sessions_meta
    running = _discover_running_sessions()
    if running is None:
        with _STATE_CLIENT_LOCK:
            if _state_running:
                _state_discovery_ok = False
                _state_discovery_reason = "session discovery unavailable"
        return
    _reap_retired_clients()  # reaper:重试回收 retiring/survivors
    to_reap: list[_InflightOwner] = []
    added: list[tuple[str, str]] = []
    changed: list[tuple[str, str, herdr_state.HerdrStateClient | None]] = []
    with _STATE_CLIENT_LOCK:
        if not _state_running:
            return  # lifespan 已关闭:拒绝一切 publish
        epoch = _state_epoch
        _state_discovery_ok = True
        _state_discovery_reason = ""
        if running == _state_sessions_meta:
            return
        removed = [n for n in _state_sessions_meta if n not in running]
        added_names = [n for n in running if n not in _state_sessions_meta]
        changed_names = [
            n for n in running
            if n in _state_sessions_meta and running[n] != _state_sessions_meta[n]
        ]
        for name in removed:
            client = _state_clients.pop(name, None)
            if client is not None:
                # 同临界区 identity-safe 转移所有权,不停成功不摘
                to_reap.append(_retire_client_locked(name, epoch, client))
            _state_swap_pending.pop(name, None)
        # removed 立即从 meta 摘除;added/changed 的 meta 仅发布成功后推进
        _state_sessions_meta = {
            n: m for n, m in _state_sessions_meta.items() if n in running
        }
        for name in added_names:
            added.append((name, running[name]["socket"]))
        for name in changed_names:
            changed.append((name, running[name]["socket"], _state_clients.get(name)))
    # removed 退休者立即回收(REAP 锁串行),避免门禁阻塞本轮新增/换入
    _reap_owners(to_reap, STATE_STOP_JOIN_TIMEOUT_S)
    for name, socket_path in added:
        client = _build_session_client(name, socket_path)
        with _STATE_CLIENT_LOCK:
            if not (
                _state_running
                and _state_epoch == epoch
                and name not in _state_clients
                and name not in _state_sessions_meta
            ):
                continue  # 闸门已关:候选从未启动线程,零副作用直接丢弃
            if name in _state_inflight:
                continue  # 同名活跃候选已在途:跳过复用,不起双 worker
            if _state_retiring or _state_survivors or _stop_tickets_pending():
                continue  # R7/R10/R11 门禁:未决 ownership/shutdown intent 未清,拒绝新候选(下轮重试)
            owner = _InflightOwner(
                epoch=epoch, name=name,
                token=next(_inflight_token), client=client,
            )
            _state_inflight[name] = owner
        # 锁外启动:False/异常/partial 统一收尾;stop 先取得所有权则不生线程
        if not _start_candidate(name, epoch, client, owner):
            continue
        # R8:added 统一 identity-safe 收尾——publish CAS 任一条件失败但仍是
        # owner 时,同一 CLIENT 临界区 del inflight→转 retiring,锁外经统一
        # REAP 入口回收;不留僵尸 inflight/活线程,同名下轮不再饥饿。
        iter_reap = []
        with _STATE_CLIENT_LOCK:
            # R11:check+commit 与 ticket 登记经 TICKET 锁线性化
            with _STATE_TICKET_LOCK:
                if (
                    _state_inflight.get(name) is owner  # 身份比较
                    and _state_running
                    and _state_epoch == epoch
                    and name not in _state_clients
                    and name not in _state_sessions_meta
                    and not _state_retiring
                    and not _state_survivors  # R7:发布前确认无未决 ownership
                    and not _state_stop_tickets  # R10:shutdown intent 未清不发布
                ):
                    # 仍持有 ownership:原子 inflight→published
                    del _state_inflight[name]
                    _state_clients[name] = client
                    _state_sessions_meta[name] = running[name]
                elif _state_inflight.get(name) is owner:
                    del _state_inflight[name]
                    iter_reap.append(_retire_client_locked(name, epoch, client))
            # 否则 stop 已摘取并拥有该候选,不得 publish/再 stop
        _reap_owners(iter_reap, STATE_STOP_JOIN_TIMEOUT_S)
    for name, socket_path, old_client in changed:
        new_client = _build_session_client(name, socket_path)
        iter_reap: list[_InflightOwner] = []
        owner: _InflightOwner | None = None
        with _STATE_CLIENT_LOCK:
            # R11:check+commit 与 ticket 登记经 TICKET 锁线性化
            with _STATE_TICKET_LOCK:
                owned = (
                    _state_running
                    and _state_epoch == epoch
                    and _state_clients.get(name) is old_client
                    and name not in _state_inflight  # 同名候选在途:跳过复用
                    and not _state_retiring
                    and not _state_survivors  # R7 门禁:未决 ownership 未清不起换入
                    and not _state_stop_tickets  # R10:shutdown intent 未清不起候选
                )
                if owned:
                    owner = _InflightOwner(
                        epoch=epoch, name=name,
                        token=next(_inflight_token), client=new_client,
                    )
                    _state_inflight[name] = owner
        if owner is None:
            continue  # 闸门已关或已有同名候选:未启动线程,直接丢弃
        # 锁外启动:False/异常/partial 统一收尾;stop 先取得所有权则不生线程
        if not _start_candidate(name, epoch, new_client, owner):
            continue
        # 锁外有界等待就绪:每轮观察取消,stop 后立即退出
        ready = False
        cancelled = False
        deadline = time.monotonic() + STATE_SWAP_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if _candidate_cancelled(name, owner):
                cancelled = True
                break
            if _client_ready(new_client, name):
                ready = True
                break
            time.sleep(0.05)
        swapped = False
        with _STATE_CLIENT_LOCK:
            # R11:check+commit 与 ticket 登记经 TICKET 锁线性化
            with _STATE_TICKET_LOCK:
                still_owned = _state_inflight.get(name) is owner
                if still_owned:
                    del _state_inflight[name]  # 仅 owner 本人可摘除
                if (
                    still_owned
                    and not cancelled
                    and ready
                    and _state_running
                    and _state_epoch == epoch
                    and _state_clients.get(name) is old_client
                    and not _state_retiring
                    and not _state_survivors  # R7:换入前确认无未决 ownership
                    and not _state_stop_tickets  # R10:shutdown intent 未清不换入
                ):
                    # 原子换入:新客户端握手含全量 resync,清降级,meta 推进;
                    # 旧 client 同一临界区转移进 retiring(不停成功不摘)
                    _state_clients[name] = new_client
                    _state_sessions_meta[name] = running[name]
                    _state_swap_pending.pop(name, None)
                    swapped = True
                    if old_client is not None:
                        iter_reap.append(_retire_client_locked(name, epoch, old_client))
                elif still_owned:
                    # 我们摘取→我们负责 stop:同一临界区转移进 retiring
                    iter_reap.append(_retire_client_locked(name, epoch, new_client))
                    if (
                        not cancelled
                        and not ready
                        and _state_running
                        and _state_epoch == epoch
                        and _state_clients.get(name) is old_client
                    ):
                        # 新 socket 不就绪:弃新留旧,旧缓存可读但持久 degraded
                        _state_swap_pending[name] = {
                            "reason": f"state socket swap not ready: {socket_path}",
                        }
            # still_owned=False:stop 已摘取并拥有候选,不得再 stop/publish
        # 立即回收本轮退休者(REAP 锁串行),避免门禁阻塞后续 session
        _reap_owners(iter_reap, STATE_STOP_JOIN_TIMEOUT_S)


def _state_client_snapshot() -> dict[str, Any]:
    """聚合各 per-session 客户端缓存,合并 directory,显式 degraded/unavailable。"""
    with _STATE_CLIENT_LOCK:
        clients = dict(_state_clients)
        meta = {name: dict(item) for name, item in _state_sessions_meta.items()}
        swap_pending = {
            name: dict(item) for name, item in _state_swap_pending.items()
        }
        discovery_ok = _state_discovery_ok
        discovery_reason = _state_discovery_reason
    if not clients:
        # 无 running session(发现正常)是合法空态;否则显式 unavailable。
        if discovery_ok and not meta:
            return {
                "available": True, "degraded": False,
                "sessions": [], "panes": [], "agents": [],
                "total_panes": 0, "agent_panes": 0,
            }
        return {
            "available": False, "degraded": True,
            "reason": discovery_reason or "state client not running",
            "sessions": [], "panes": [], "agents": [],
            "total_panes": 0, "agent_panes": 0,
        }
    degraded = not discovery_ok
    sessions: list[dict[str, Any]] = []
    panes: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    any_bootstrapped = False
    for name, client in clients.items():
        try:
            store = client.snapshot_cached()
            lifecycle = client.state().get("sessions", {})
        except Exception:
            logger.exception("herdr state client read failed: %s", name)
            store, lifecycle = {"available": False, "sessions": []}, {}
        state = lifecycle.get(name, {}).get("state")
        entry = next(
            (s for s in store.get("sessions", []) if s.get("session") == name),
            None,
        )
        if entry is None:
            entry = {
                "session": name, "status": "running", "panes": [],
                "agents": [], "focused_pane_id": None, "layouts": [],
            }
        entry["directory"] = meta.get(name, {}).get("directory", "")
        entry["state_status"] = state
        if state != "subscribed":
            degraded = True
        if name in swap_pending:
            # socket 换入超时:旧缓存仍可读,但显式 degraded 且原因可见
            degraded = True
            entry["state_status"] = "swap_pending"
            entry["state_reason"] = swap_pending[name].get("reason", "")
        if store.get("available"):
            any_bootstrapped = True
        sessions.append(entry)
        panes.extend(entry.get("panes", []))
        agents.extend(entry.get("agents", []))
    # 已发现但尚未建客户端的 session(异常窗口)显式 degraded。
    for name in meta:
        if name not in clients:
            degraded = True
            sessions.append({
                "session": name, "status": "running", "panes": [],
                "agents": [], "focused_pane_id": None, "layouts": [],
                "directory": meta[name].get("directory", ""),
                "state_status": "starting",
            })
    available = any_bootstrapped
    result: dict[str, Any] = {
        "available": available,
        "degraded": degraded or not available,
        "sessions": sessions,
        "panes": panes,
        "agents": agents,
        "total_panes": len(panes),
        "agent_panes": sum(1 for p in panes if p.get("agent")),
    }
    if not available:
        result["reason"] = "no bootstrapped session cache"
    elif not discovery_ok and discovery_reason:
        result["reason"] = discovery_reason
    elif swap_pending:
        result["reason"] = "state socket swap pending: " + ",".join(
            sorted(swap_pending)
        )
    return result


def _merge_h0_canary_snapshot(
    legacy: dict[str, Any], cached: dict[str, Any],
) -> dict[str, Any]:
    """allowlist session 用 socket cache，其余 session 保持旧 CLI 快照。"""
    scope = H0_STATE_CANARY_SESSIONS
    cached_by_name = {
        str(item.get("session")): item
        for item in cached.get("sessions", [])
        if isinstance(item, dict) and item.get("session") in scope
    }
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    has_unscoped = False
    has_scoped = False
    missing_scoped = False
    for legacy_entry in legacy.get("sessions", []):
        if not isinstance(legacy_entry, dict) or not legacy_entry.get("session"):
            continue
        name = str(legacy_entry["session"])
        seen.add(name)
        if name not in scope:
            has_unscoped = True
            sessions.append(legacy_entry)
            continue
        has_scoped = True
        cached_entry = cached_by_name.get(name)
        if cached_entry is not None:
            sessions.append(cached_entry)
            continue
        missing_scoped = True
        sessions.append({
            "session": name,
            "status": legacy_entry.get("status", "running"),
            "directory": legacy_entry.get("directory", ""),
            "panes": [],
            "agents": [],
            "focused_pane_id": None,
            "layouts": [],
            "state_status": "unavailable",
            "state_reason": cached.get("reason", "canary state cache unavailable"),
        })
    for name, cached_entry in cached_by_name.items():
        if name not in seen:
            has_scoped = True
            sessions.append(cached_entry)

    panes = [pane for session in sessions for pane in session.get("panes", [])]
    agents = [agent for session in sessions for agent in session.get("agents", [])]
    if not sessions:
        available = bool(legacy.get("available")) and bool(cached.get("available"))
    else:
        available = (
            (has_unscoped and bool(legacy.get("available")))
            or (has_scoped and not missing_scoped and bool(cached.get("available")))
        )
    degraded = (
        bool(legacy.get("degraded"))
        or not bool(legacy.get("available"))
        or bool(cached.get("degraded"))
        or missing_scoped
        or not available
    )
    reasons: list[str] = []
    if (bool(cached.get("degraded")) or missing_scoped) and cached.get("reason"):
        reasons.append(f"canary cache: {cached['reason']}")
    if not bool(legacy.get("available")) and legacy.get("reason"):
        reasons.append(f"legacy snapshot: {legacy['reason']}")
    result: dict[str, Any] = {
        "available": available,
        "degraded": degraded,
        "sessions": sessions,
        "panes": panes,
        "agents": agents,
        "total_panes": len(panes),
        "agent_panes": sum(1 for pane in panes if pane.get("agent")),
    }
    if reasons:
        result["reason"] = "; ".join(reasons)
    return result


def _herdr_runtime_snapshot() -> dict[str, Any]:
    """off 用 CLI，canary 按 session 混合，on 全量使用 socket cache。"""
    if H0_STATE_MODE == "on":
        return _state_client_snapshot()
    if H0_STATE_MODE == "canary":
        return _merge_h0_canary_snapshot(
            herdr_client.snapshot(
                exclude_sessions=H0_STATE_CANARY_SESSIONS
            ),
            _state_client_snapshot(),
        )
    return herdr_client.snapshot()


_live_state: dict[str, Any] = {
    "revision": 0,
    "unread": None,
    "snapshot": None,
    "attention": None,
}
_poller_task: asyncio.Task | None = None
_message_poller_task: asyncio.Task | None = None
_worktree_cleanup_task: asyncio.Task | None = None
_identity_retirement_task: asyncio.Task | None = None
# 过期 task worktree 后台清理：启动后立即跑一轮，之后每 6 小时一轮。
WORKTREE_CLEANUP_INTERVAL_S = 6 * 3600
WORKTREE_CLEANUP_MAX_AGE_HOURS = 48.0


async def _wait_worktree_cleanup_interval() -> None:
    """等待下一轮 worktree 清理；测试可 monkeypatch 为 Event 驱动，避免真实 sleep。"""
    await asyncio.sleep(WORKTREE_CLEANUP_INTERVAL_S)


async def _worktree_cleanup_loop() -> None:
    """单协程串行清理过期 task worktree，不与自身重叠。

    使用 asyncio.to_thread 调用 tasks.cleanup_worktrees，避免阻塞事件循环；
    单次清理异常只记日志并进入下一轮等待。
    """
    while True:
        try:
            await asyncio.to_thread(
                tasks.cleanup_worktrees, WORKTREE_CLEANUP_MAX_AGE_HOURS
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worktree cleanup failed")
        try:
            await _wait_worktree_cleanup_interval()
        except asyncio.CancelledError:
            raise


async def _wait_identity_retirement_interval() -> None:
    await asyncio.sleep(IDENTITY_RETIRE_RETRY_INTERVAL_S)


async def _identity_retirement_loop() -> None:
    """周期重试 Hub 退休；先等待一轮，避免测试/短生命周期启动触碰外部状态。"""
    while True:
        await _wait_identity_retirement_interval()
        try:
            result = await asyncio.to_thread(_retry_pending_agent_retirements)
            if result.get("pending"):
                logger.warning("identity retirement retry incomplete: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("identity retirement retry failed")


async def _poll_live_state() -> None:
    global _live_state
    last_sig = ""
    attention_ids: set[str] | None = None
    last_discovery = 0.0
    while True:
        poll_start = time.monotonic()
        success = False
        session_count = 0
        try:
            if (
                _h0_state_enabled()
                and time.monotonic() - last_discovery >= _state_discovery_interval()
            ):
                await asyncio.to_thread(_reconcile_state_client)
                last_discovery = time.monotonic()
            await asyncio.to_thread(_expire_zoom_leases)
            snap = await asyncio.to_thread(_board_snapshot)
            await asyncio.to_thread(_b0_apply_live_status, snap)
            session_count = len(snap.get("sessions", [])) if snap.get("available") else 0
            await asyncio.to_thread(coordination.maintain_live_claims, snap)
            attention = await asyncio.to_thread(_build_attention, snap)
            attention_ids, new_items = _attention_changes(
                attention_ids, attention["items"]
            )
            if new_items:
                await asyncio.to_thread(web_push.notify, new_items)
            unread = attention["mail_unread"]
            sig = json.dumps(
                {
                    "unread": unread,
                    "attention": attention["items"],
                    "capabilities": attention["capabilities"],
                    # H0.5:可用性/降级与生命周期变化也必须触发 revision+1 推送
                    "available": snap.get("available"),
                    "degraded": snap.get("degraded"),
                    "reason": snap.get("reason"),
                    "state_status": {
                        s.get("session"): s.get("state_status")
                        for s in snap.get("sessions", [])
                        if isinstance(s, dict)
                    },
                    "session_tasks": [
                        (
                            session.get("session"), session.get("status"),
                            session.get("progress"),
                            [
                                (
                                    agent.get("pane_id"), agent.get("mail_name"),
                                    agent.get("role"), agent.get("task"),
                                    agent.get("status"),
                                    agent.get("coordination_state"),
                                    agent.get("task_revision"),
                                    (
                                        (agent.get("report") or {}).get("request_id"),
                                        (agent.get("report") or {}).get("reported_ts"),
                                        (agent.get("report") or {}).get("progress"),
                                        (agent.get("report") or {}).get("summary"),
                                        (agent.get("report") or {}).get("next_step"),
                                        (agent.get("report") or {}).get("blocker"),
                                        (agent.get("report") or {}).get("pending"),
                                        (agent.get("report") or {}).get("request_error"),
                                    ),
                                )
                                for agent in session.get("agents", [])
                            ],
                        )
                        for session in attention.get("sessions", [])
                    ],
                    "panes": [
                        (p.get("session"), p.get("pane_id"), p.get("agent"),
                         p.get("agent_status"), p.get("revision"), p.get("mail_name"))
                        for p in snap.get("panes", [])
                    ],
                },
                ensure_ascii=False,
            )
            if sig != last_sig:
                _live_state = {
                    "revision": _live_state["revision"] + 1,
                    "unread": unread,
                    "snapshot": snap,
                    "attention": attention,
                }
                last_sig = sig
            success = bool(snap.get("available", False)) and not any(
                session.get("error") for session in snap.get("sessions", [])
                if isinstance(session, dict)
            ) and not snap.get("degraded", False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("live state poll failed")
        # 指标 + 自适应间隔:统一走 helper(生产路径单一,测试直接调 helper)。
        # delay 失败分支优先于 idle:_poll_delay 内部先看 consecutive_failures。
        duration = time.monotonic() - poll_start
        _record_poll_metrics(duration, session_count, success)
        await asyncio.sleep(_poll_delay(session_count))


async def _poll_message_state() -> None:
    """独立1秒消息revision检测；不受Herdr空闲/失败退避影响。"""
    while True:
        if not db.DB_PATH.is_file():
            await asyncio.sleep(5)
            continue
        try:
            await asyncio.to_thread(_refresh_message_state)
            await asyncio.to_thread(_b0_poll_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("message revision poll failed")
        await asyncio.sleep(1)


class _SseCappedEventSourceResponse(EventSourceResponse):
    """EventSourceResponse whose SSE lease is owned by the full ASGI call.

    Reservation happens inside ``__call__`` (not in the endpoint body) so a
    response object that is never started cannot leak a slot. The outer
    ``finally`` always releases exactly once, including cancel/disconnect and
    failures before the body iterator is entered.
    """

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        lease = runtime_stats.try_open_connection("sse")
        if lease is None:
            body = b'{"detail":"too many concurrent event streams"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"retry-after", b"5"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        try:
            await super().__call__(scope, receive, send)
        finally:
            lease.close()


@app.get("/api/events")
async def api_events(request: Request):
    """把共享轮询缓存中的变化推送给浏览器。"""
    # Auth already enforced by middleware. SSE slot is reserved only when the
    # ASGI response actually starts (see _SseCappedEventSourceResponse).
    last_revision = -1
    last_message_revision = -1

    async def event_stream():
        nonlocal last_revision, last_message_revision
        while not await request.is_disconnected():
            state = _live_state
            if state["revision"] != last_revision and state["unread"] is not None:
                yield {"event": "unread", "data": json.dumps({"count": state["unread"]})}
                if state["snapshot"] is not None:
                    yield {
                        "event": "board",
                        "data": json.dumps(state["snapshot"], ensure_ascii=False),
                    }
                if state["attention"] is not None:
                    yield {
                        "event": "attention",
                        "data": json.dumps(state["attention"], ensure_ascii=False),
                    }
                last_revision = state["revision"]
            message_state = _message_state
            if (
                message_state["signatures"] is not None
                and message_state["revision"] != last_message_revision
            ):
                projects: list[str]
                if last_message_revision == -1:
                    projects = sorted(message_state["signatures"] or {})
                else:
                    changes = [
                        change for change in message_state["changes"]
                        if change["revision"] > last_message_revision
                    ]
                    oldest = message_state["changes"][0]["revision"] if message_state["changes"] else message_state["revision"]
                    if last_message_revision >= 0 and last_message_revision < oldest - 1:
                        projects = sorted(message_state["signatures"] or {})
                    else:
                        projects = sorted({
                            project for change in changes for project in change["projects"]
                        })
                yield {
                    "event": "messages",
                    "data": json.dumps({
                        "revision": message_state["revision"],
                        "projects": projects,
                    }),
                }
                last_message_revision = message_state["revision"]
            await asyncio.sleep(1)

    return _SseCappedEventSourceResponse(event_stream())


# ── 静态前端 ────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _next_web_file(relative: str) -> Path | None:
    """Resolve one regular build file without following symlink components."""
    parts = relative.split("/")
    if any(
        not part or part in {".", ".."} or "\\" in part or "\x00" in part
        for part in parts
    ):
        return None
    target = NEXT_WEB_DIR.joinpath(*parts)
    try:
        relative_to_root = target.relative_to(ROOT_DIR)
        current = ROOT_DIR
        if current.is_symlink():
            return None
        for part in relative_to_root.parts:
            current /= part
            if current.is_symlink():
                return None
        if not current.is_file():
            return None
    except (OSError, ValueError):
        return None
    return current


@app.get("/assets/{asset_path:path}")
def next_web_asset(asset_path: str):
    if not next_profile.enabled():
        raise HTTPException(404, "Not Found")
    target = _next_web_file(f"assets/{asset_path}")
    if target is None:
        raise HTTPException(404, "Not Found")
    return FileResponse(
        target,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.api_route("/assets", methods=["GET", "HEAD"])
def next_web_assets_root():
    """Keep the asset mount root invalid instead of redirecting it."""
    raise HTTPException(404, "Not Found")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/manifest.webmanifest")
def web_manifest():
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/")
def index():
    # no-cache:手机端浏览器常启发式缓存首页,新功能(如上传类型放开)必须能及时下发
    if next_profile.enabled():
        next_index = _next_web_file("index.html")
        if next_index is None:
            raise HTTPException(503, "next_web_build_unavailable")
        return FileResponse(next_index, headers={"Cache-Control": "no-cache"})
    return FileResponse(
        STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
    )


@app.get("/health")
def health():
    """外部依赖聚合(Wiki13 J1)：失败仍 HTTP 200 + status=degraded，
    不得触发 bundle rollback。不探测 app-owned store schema。"""
    mail_status = _agent_mail_status()
    push_status = web_push.public_config()
    external = {
        "db": bool(mail_status.get("available")),
        "herdr": bool(herdr_client.is_available()),
        "hub": bool(mail_status.get("write_available")),
        "push": bool(push_status.get("available")),
    }
    if B0_MODE == "off":
        b0_state = {
            "active": False, "available": True,
            "degraded": False, "reason": None,
        }
    elif B0_MODE == "shadow":
        with _b0_shadow_lock:
            b0_state = {"active": False, "shadow": True, **_b0_shadow_state}
    else:
        with _b0_runtime_state_lock:
            b0_state = {"active": True, **_b0_runtime_state}
    ok = all(external.values()) and not bool(b0_state.get("degraded"))
    return {
        "status": "ok" if ok else "degraded",
        "ts": time.time(),
        "herdr_state_mode": H0_STATE_MODE,
        "herdr_state_canary_sessions": (
            sorted(H0_STATE_CANARY_SESSIONS)
            if H0_STATE_MODE == "canary" else []
        ),
        "b0_mode": B0_MODE,
        "b0_canary_scopes": (
            sorted(f"{kind}/{scope_id}" for kind, scope_id in B0_CANARY_SCOPES)
            if B0_MODE == "canary" else []
        ),
        "b0": b0_state,
        **external,
        "external": external,
    }


@app.get("/health/live")
def health_live():
    """纯读无副作用(Wiki13 J1A)：只证明目标进程响应并返回 release identity。
    不探测 Herdr/Hub/Push/Mail，不触发 DDL/reconcile。
    R3: ReleaseIdentityError 暴露 allowlisted reason；未知异常→unexpected，
    任意 raw text 不得出现在 503 响应中。"""
    try:
        identity = release_identity.get_release_identity()
    except release_identity.ReleaseIdentityError as exc:
        # R3: 只暴露 allowlisted reason，不拼接任意 ValueError 正文
        raise HTTPException(503, f"release_identity_error: {exc.reason}")
    except Exception:
        raise HTTPException(503, "release_identity_error: unexpected")
    return {"status": "live", "identity": identity}


@app.get("/health/ready")
def health_ready():
    """公共纯读 readiness(Wiki13 J1)：release identity + paths/config/manifest
    + app-owned store 严格 fingerprint。零写入；响应脱敏(无路径/异常正文)。
    不兼容 → HTTP 503；兼容(含 missing_creatable) → 200。"""
    try:
        identity = release_identity.get_release_identity()
    except release_identity.ReleaseIdentityError as exc:
        raise HTTPException(
            503,
            {
                "status": "not_ready",
                "compat_family": "0.3.x",
                "reason": f"identity_error:{exc.reason}",
                "stores": [],
            },
        )
    except Exception:
        raise HTTPException(
            503,
            {
                "status": "not_ready",
                "compat_family": "0.3.x",
                "reason": "identity_error:unexpected",
                "stores": [],
            },
        )
    from . import store_schema

    body = store_schema.evaluate_ready(identity)
    # Strip any accidental path-like fields; identity already sanitized.
    if body.get("ready"):
        return body
    raise HTTPException(503, body)



@app.get("/health.poll")
def health_poll():
    """返回受认证的 poll 聚合诊断，不暴露 raw samples。"""
    m = _POLL_METRICS
    return {
        "count": m["count"],
        "failures": m["failures"],
        "failure_rate": m["failure_rate"],
        "consecutive_failures": m["consecutive_failures"],
        "last_duration": m["last_duration"],
        "duration_p50": m["duration_p50"],
        "duration_p95": m["duration_p95"],
        "last_session_count": m["last_session_count"],
        "interval": POLL_INTERVAL,
    }


def main() -> int:
    import uvicorn

    from .log_config import LogConfigError, configure_logging

    try:
        next_profile.validate_server_environment(ROOT_DIR)
    except next_profile.NextProfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # Fixed install root = generation/package directory (not process cwd).
    _install_dir = ROOT_DIR
    try:
        level_name = configure_logging(install_dir=_install_dir)
    except LogConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    host = os.environ.get("COCKPIT_HOST", "127.0.0.1")
    port = int(os.environ.get("COCKPIT_PORT", "8790"))
    _validate_bind(host)
    # log_config=None avoids a second uvicorn config; access_log=False blocks
    # full_path/query leakage from uvicorn.access (see O3 R1 F1).
    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=bool(COCKPIT_TOKEN),
        log_level=level_name.lower(),
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
