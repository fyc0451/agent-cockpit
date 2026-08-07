"""server.py — Agent Cockpit FastAPI 入口。

与 herdr / Agent Mail hub 同机部署,直读本地 SQLite + 调本机 hub MCP + herdr socket。
Mac/手机浏览器通过内网访问(默认 :8790)。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, UploadFile, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

import db
import httpx
import coordination
import hub_client
import herdr_client
import tasks
import team_inbox_router
import uploads
import files
import mail_projects
import team_sessions
import terminal
import web_push
import settings
from pydantic import BaseModel, Field


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _poller_task
    _poller_task = asyncio.create_task(_poll_live_state())
    try:
        yield
    finally:
        _poller_task.cancel()
        with suppress(asyncio.CancelledError):
            await _poller_task
        _poller_task = None
        await asyncio.to_thread(_release_all_zoom_leases)


app = FastAPI(title="Agent Cockpit", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"
COCKPIT_TOKEN = os.environ.get("COCKPIT_TOKEN", "")
AUTH_COOKIE = "cockpit_session"
TEAM_AUTH_COOKIE = "cockpit_team_human_session"
PUBLIC_PATHS = {
    "/", "/health", "/api/auth/status", "/api/auth/login",
    "/api/agent/team-reply",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
logger = logging.getLogger("agent-cockpit")
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PANE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$")
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
ROOT_DIR = Path(__file__).resolve().parent
AGENT_MAIL_TOOLS_DIR = ROOT_DIR / "agent-mail-tools"
AGENT_MAIL_INIT_SCRIPT = AGENT_MAIL_TOOLS_DIR / "am-init-project"
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
MAIL_COORDINATION_GUIDE = (
    "协作通信约定:长任务每完成一个里程碑检查一次未读消息；多封消息按时间顺序处理；"
    "收到停止/转向时，在完成当前原子操作并保存状态后立即停手汇报；"
    "收到消息后按 mail-recv 输出先 claim、处理完成再单条 complete/ack；"
    "普通打断保存 checkpoint 后恢复原任务，停止/转向不恢复。"
)
LOCAL_ONLY_AUTH_DETAIL = (
    "未设置 COCKPIT_TOKEN 时仅允许本机回环访问；局域网访问请配置 COCKPIT_TOKEN"
)


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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


def _validate_pane_id(pane_id: str) -> None:
    if not PANE_ID_RE.fullmatch(pane_id):
        raise HTTPException(400, "pane id 格式无效")


def _identity_record(cwd: str, agent_type: str) -> dict[str, Any] | None:
    """只按已确定的 Agent Mail human key 查真实身份。"""
    try:
        return db.identity_by_cwd(cwd, agent_type)
    except Exception:
        return None


def _identity_name(cwd: str, agent_type: str) -> str | None:
    ident = _identity_record(cwd, agent_type)
    return ident["name"] if ident else None


def _enrich_board_identities(snapshot: dict[str, Any]) -> dict[str, Any]:
    """按 session 的 canonical Agent Mail 项目给看板 pane 补真实花名。"""
    session_dirs = {
        str(item.get("session") or ""): str(item.get("directory") or "")
        for item in snapshot.get("sessions", [])
        if item.get("session") and item.get("directory")
    }
    projects: dict[str, str | None] = {}
    identities: dict[tuple[str, str], str | None] = {}
    for pane in snapshot.get("panes", []):
        session = str(pane.get("session") or "")
        agent = str(pane.get("agent") or "")
        session_dir = session_dirs.get(session)
        if not agent or not session_dir:
            continue
        if session not in projects:
            try:
                projects[session] = mail_projects.get(session, session_dir)
            except (OSError, ValueError):
                projects[session] = None
        project = projects[session]
        if not project:
            continue
        key = (project, agent)
        if key not in identities:
            identities[key] = _identity_name(project, agent)
        if identities[key]:
            pane["mail_name"] = identities[key]
    return snapshot


def _board_snapshot() -> dict[str, Any]:
    return _enrich_board_identities(herdr_client.snapshot())


def _identity_hint(
    name: str, project: str, agent_type: str, *, roster: str = "", registered: bool = False,
    coordination_context: dict[str, Any] | None = None,
) -> str:
    send = shlex.quote(str(MAIL_SEND_SCRIPT))
    recv = shlex.quote(str(MAIL_RECV_SCRIPT))
    project_arg = shlex.quote(project)
    mail_agent = MAIL_AGENT_NAMES.get(agent_type, agent_type)
    prefix = (
        "[agent-mail 身份告知] 你的邮箱身份已注册:"
        if registered else "[agent-mail 身份告知] "
    )
    hint = (
        f"{prefix}花名={name},项目={project}。"
        f"发消息: {send} --agent {mail_agent} --instance main --project {project_arg} "
        "--to <花名> --subject \"...\" --body \"...\";"
        f"收消息: {recv} --agent {mail_agent} --instance main --project {project_arg} --unread。"
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
    path = request.url.path
    protected = path.startswith("/api/") or path in {"/docs", "/redoc", "/openapi.json"}
    if not protected or path in PUBLIC_PATHS:
        return await call_next(request)
    if not _request_authenticated(request):
        status = 401 if COCKPIT_TOKEN else 403
        detail = "未认证" if COCKPIT_TOKEN else LOCAL_ONLY_AUTH_DETAIL
        return JSONResponse(
            {"detail": detail}, status_code=status,
            headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
        )
    cookie_auth = _valid_cookie(request.cookies.get(AUTH_COOKIE))
    if request.method not in SAFE_METHODS:
        # CSRF 防护:无 Token 模式认证只看 client IP,恶意站点可诱导浏览器
        # 向 localhost 发跨源写请求;对存在的 Origin 一律要求同源。无 Origin
        # 的 CLI/curl 不受影响,保留原有行为。
        origin = request.headers.get("origin")
        if not COCKPIT_TOKEN:
            if origin and not _same_origin(origin, request.headers.get("host")):
                return JSONResponse({"detail": "Origin 校验失败"}, status_code=403)
        elif cookie_auth and not _valid_bearer(
            request.headers.get("authorization")
        ):
            if not _same_origin(origin, request.headers.get("host")):
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
    model: str | None = None


class PaneSendReq(BaseModel):
    text: str
    mode: str = "send"  # send | prompt


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
    name: str | None = None  # 工作区内唯一的本地实例名；为空时保留旧版复用语义
    layout: str = "tab"
    workspace: str = "shared"  # shared(兼容旧调用) | isolated(新建/复用 worktree)
    args: str = Field(default="", max_length=herdr_client.MAX_AGENT_ARGS_LENGTH)


class WorkspaceParticipantReq(BaseModel):
    """协作工作区里的一个 Agent。"""
    id: str = ""
    name: str = ""  # 当前 Herdr 工作区内唯一的本地实例名
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
def api_project(slug: str):
    """项目详情:agent 列表 + 消息流。"""
    mail_status = _agent_mail_status()
    if not mail_status["available"]:
        raise HTTPException(503, mail_status["reason"])
    proj = db.project_by_slug(slug)
    if not proj:
        raise HTTPException(404, f"项目不存在: {slug}")
    messages = [
        coordination.enrich_message(proj["human_key"], message)
        for message in db.recent_messages(proj["id"])
    ]
    return {
        "project": proj,
        "agents": db.list_agents(proj["id"]),
        "messages": messages,
        "agent_mail": mail_status,
    }


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


def _notify_coordination_message(
    project_key: str, recipient: str, message_id: int, subject: str,
    meta: dict[str, Any], *, hard: bool,
) -> dict[str, Any]:
    context = coordination.active_context(project_key, recipient)
    if not context or context.get("run_id") != meta.get("run_id"):
        return {"notified": False, "reason": "recipient_not_in_unique_active_run"}
    intent = str(meta.get("intent") or "info")
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
    )
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
    try:
        meta, warnings = coordination.prepare_metadata(
            project_key=proj["human_key"], sender=sender["name"],
            recipients=req.to, intent=req.intent, importance=req.importance,
            authority="user", supersedes=req.supersedes,
            expires_in=req.expires_in,
        )
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


# ── 设置路由 ────────────────────────────────────────────────────

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
    body = await request.json()
    try:
        return settings.update(body)
    except ValueError as e:
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
    """返回当前 Human 的收件箱路由状态；不含 registry/身份/凭据。"""
    _, human = _team_human_context(request)
    hub = hub_client.public_team_config()["team_hub"]
    return team_inbox_router.route_status(
        hub=hub, human_id=int(human["id"])
    )


def _team_inbox_reply_command(
    binding: dict[str, Any], item: dict[str, Any],
) -> str | None:
    """为远程 Human 消息生成只含本机可信字段的幂等回复命令。"""
    subject = str(item.get("subject") or "")
    if re.fullmatch(r"回复 Team 消息 #[A-Za-z0-9_.:-]+", subject):
        return None
    handle = item.get("sender_handle")
    remote_id = item.get("id")
    if (
        not isinstance(handle, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", handle)
        or isinstance(remote_id, bool)
        or not isinstance(remote_id, (int, str))
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", str(remote_id))
    ):
        return None
    lead = binding.get("lead") if isinstance(binding.get("lead"), dict) else {}
    raw_mail_project = binding.get("mail_project")
    if not isinstance(raw_mail_project, str) or not Path(raw_mail_project).is_absolute():
        return None
    try:
        mail_project = str(Path(raw_mail_project).resolve())
    except (OSError, ValueError):
        return None
    mail_name = lead.get("mail_name")
    lead_agent = MAIL_AGENT_NAMES.get(
        str(lead.get("agent") or ""), str(lead.get("agent") or ""),
    )
    identities = [
        identity for identity in _registry_scan()
        if identity.get("project_key") == mail_project
        and identity.get("name") == mail_name
        and MAIL_AGENT_NAMES.get(
            str(identity.get("agent") or ""), str(identity.get("agent") or ""),
        ) == lead_agent
    ]
    if len(identities) != 1:
        return None
    agent = identities[0].get("agent")
    instance = identities[0].get("instance")
    safe_identity = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
    if (
        not isinstance(agent, str)
        or not re.fullmatch(safe_identity, agent)
        or not isinstance(instance, str)
        or not re.fullmatch(safe_identity, instance)
    ):
        return None
    reply_key = "team-inbox-" + hashlib.sha256(
        f"{binding.get('hub')}\0{binding.get('human_id')}\0{remote_id}".encode()
    ).hexdigest()[:32]
    mail_send = Path(__file__).resolve().parent / "agent-mail-tools" / "mail-send"
    args = [
        str(mail_send),
        "--agent", agent,
        "--instance", instance,
        "--project", mail_project,
        "--to", f"@{handle}",
        "--subject", f"回复 Team 消息 #{remote_id}",
        "--body", "__REPLY_BODY__",
        "--idempotency-key", reply_key,
    ]
    return " ".join(shlex.quote(value) for value in args)


@app.post("/api/team-auth/inbox-route/route")
def api_team_inbox_route_run(request: Request):
    """触发一次远程 Human Inbox → 本机已绑定 Session lead 的安全路由。"""
    authorization, human = _team_human_context(request)
    hub = hub_client.public_team_config()["team_hub"]
    try:
        return team_inbox_router.route_inbox(
            authorization,
            hub=hub,
            human_id=int(human["id"]),
            fetch_inbox=lambda auth: hub_client.human_api(
                "GET", "/hub/api/inbox?limit=100", auth
            ),
            reply_command_for=_team_inbox_reply_command,
        )
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    except hub_client.HumanAPIError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


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
        if not layout.get("horizontal_split"):
            return {
                "available": True, "acquired": False, "owned": False,
                "reason": "not_horizontal", "session": session,
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
    # 先从 snapshot 拿这个 pane 的 cwd 和 agent 类型
    snap = herdr_client.snapshot()
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
    ident = _identity_record(project, agent_type)
    if not ident:
        return {
            "found": False,
            "needs_registration": True,
            "reason": "该通信项目下没有此 agent 的有效身份（未注册或已 retired）",
            "project": project,
        }
    return {
        "found": True,
        "name": ident["name"],
        "program": ident["program"],
        "model": ident.get("model", ""),
        "project_key": project,
        "session": session,
        "cwd": cwd,
        "mail_hint": _collaborator_hint(ident["name"], project),
    }


@app.post("/api/herdr/pane/{session}/{pane_id}/send")
def api_herdr_pane_send(session: str, pane_id: str, req: PaneSendReq):
    """往 pane 发指令(send-keys 或 prompt)。"""
    _validate_session_name(session)
    _validate_pane_id(pane_id)
    if req.mode not in VALID_PANE_SEND_MODES:
        raise HTTPException(400, f"不支持的发送模式: {req.mode}")
    return herdr_client.pane_send(session, pane_id, req.text, req.mode)


def _started_agent_mail_identity(
    session: str, pane_id: str, agent_type: str,
) -> dict[str, Any]:
    """为新增的唯一类型 Agent 注册 canonical 身份并发送身份告知。"""
    base: dict[str, Any] = {
        "registered": False, "registered_now": False, "notified": False,
    }
    mail_agent = MAIL_AGENT_NAMES.get(agent_type, agent_type)
    try:
        panes = [
            pane for pane in herdr_client.snapshot().get("panes", [])
            if pane.get("session") == session
            and MAIL_AGENT_NAMES.get(
                str(pane.get("agent") or ""), str(pane.get("agent") or "")
            ) == mail_agent
        ]
    except Exception as exc:
        return {**base, "warning": f"无法确认同类型 Agent 数量，已跳过身份绑定: {exc}"}
    if len(panes) > 1:
        return {
            **base, "skipped": "ambiguous_same_type",
            "warning": "同类型多实例共用邮箱身份会产生通知歧义，已跳过自动绑定",
        }
    try:
        state = _mail_project_state(session)
    except Exception as exc:
        return {**base, "warning": f"读取 Agent Mail 通信项目失败: {exc}"}
    if not state.get("bound") or not state.get("project"):
        return {**base, "warning": "该 session 尚未绑定 Agent Mail 通信项目"}
    project = str(state["project"])
    status = {**base, "project": project}
    name = _identity_name(project, agent_type)
    if not name:
        if not AGENT_MAIL_INIT_SCRIPT.is_file():
            return {**status, "warning": "Agent Mail 注册工具未安装"}
        try:
            registered = subprocess.run(
                [
                    str(AGENT_MAIL_INIT_SCRIPT), "--project", project,
                    "--only", mail_agent,
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
        name = _identity_name(project, agent_type)
        if not name:
            return {**status, "warning": "Agent Mail 注册完成但未查到有效身份"}
        status["registered_now"] = True
    status.update({"registered": True, "name": name})
    try:
        notified = herdr_client.pane_send(
            session, pane_id,
            _identity_hint(name, project, agent_type, registered=True), "prompt",
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
    name = req.name.strip() if req.name else None
    if name and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", name):
        raise HTTPException(400, "实例名称只能包含字母、数字、_、-，最长 32 位")
    try:
        normalized_args = herdr_client.normalize_agent_args(req.args)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    project_dir = Path(req.workdir).expanduser().resolve()
    workspace: dict[str, Any] = {
        "strategy": "shared", "workdir": str(project_dir),
    }
    git_root = None
    if req.workspace == "isolated":
        if not name:
            raise HTTPException(400, "创建独立 worktree 时必须填写实例名称")
        git_root, source_dir = _worktree_source(project_dir)
        if not git_root or not source_dir:
            raise HTTPException(400, "该目录不是 Git 仓库，不能创建独立 worktree")
        participant = WorkspaceParticipantReq(
            id=name, name=name, agent=req.agent, role="developer",
            task="", workspace="isolated",
        )
        try:
            workspace = _ensure_worktree(
                git_root, source_dir, req.session, participant, 0,
                detached=False, slug=name,
            )
        except ValueError as exc:
            raise HTTPException(400, f"创建 {name} worktree 失败: {exc}") from exc

    try:
        result = herdr_client.start_agent(
            req.session, workspace["workdir"], req.agent, req.model,
            layout=req.layout, label=name, args=normalized_args,
        )
    except Exception as exc:
        cleanup_errors = (
            _rollback_worktrees(git_root, [workspace])
            if git_root and workspace.get("created") else []
        )
        detail = f"启动 {name or req.agent} 失败: {exc}"
        if cleanup_errors:
            detail += "；回滚异常: " + "；".join(cleanup_errors)
        raise HTTPException(500, detail) from exc
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
            req.session, result["pane_id"], req.agent
        )
        try:
            result["coordination"] = coordination.add_participant(
                session=req.session,
                participant_id=name or result["pane_id"],
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
    result = herdr_client.delete_session(name)
    if result.get("deleted"):
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
        herdr_client.detach_pane(session, pane_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"detached": pane_id}


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
    snap = herdr_client.snapshot()
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
    agent_counts: dict[str, int] = {}
    for i, p in enumerate(participants):
        pid = p.id or f"agent-{i + 1}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", pid):
            raise HTTPException(400, f"无效的 participant id: {pid}")
        ids.append(pid)
        agent_counts[p.agent] = agent_counts.get(p.agent, 0) + 1
        name = p.name.strip() or f"{p.agent}-{agent_counts[p.agent]}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", name):
            raise HTTPException(400, f"无效的实例名称: {name}")
        names.append(name)
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "participant id 不能重复")
    if len({name.casefold() for name in names}) != len(names):
        raise HTTPException(400, "实例名称不能重复")
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
    for index, (participant, pid, name, normalized_args) in enumerate(
        zip(participants, ids, names, normalized_agent_args)
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
                    detached=strategy == "review", slug=name,
                    legacy_slug=f"{index + 1}-{participant.agent}",
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
    if not Path(req.workdir).expanduser().resolve().is_dir():
        raise HTTPException(400, "工作目录不存在")
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
    canonical_project = _canonical_mail_project(Path(req.workdir))
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
        )
        results.append({
            "agent": agent_type, "name": plan["name"], "plan": plan, "start": r,
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
            time.sleep(2)  # 等新 Agent 启动
    # 1.5 清理建 session 时 TUI 自带的空白 shell pane(至少一个 agent 在跑才清,避免空 session)
    closed_panes = []
    if base_pane_ids and started:
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
            "mail_name": (
                _identity_name(canonical_project, plan["agent"])
                if sum(1 for p in plans if p["agent"] == plan["agent"]) == 1
                else None
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
            should_brief = result["plan"]["name"] in started_instances
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
    # 3. 注册身份(am-init-project)
    reg_ok = False
    mail_status: dict[str, Any]
    if not mail_binding_ok:
        mail_status = {"available": False, "reason": "通信项目绑定未保存，已跳过身份注册"}
    elif not AGENT_MAIL_INIT_SCRIPT.is_file():
        mail_status = {"available": False, "reason": "Agent Mail 未安装，已跳过身份注册"}
    else:
        try:
            r = subprocess.run(
                [str(AGENT_MAIL_INIT_SCRIPT), "--project", canonical_project],
                cwd=canonical_project,
                capture_output=True, text=True, timeout=60,
            )
            reg_ok = r.returncode == 0
            registration_error = (r.stderr or r.stdout)[-300:]
            mail_status = {
                "available": reg_ok,
                "reason": None if reg_ok else (registration_error or "am-init-project 失败"),
            }
        except Exception as exc:
            mail_status = {"available": False, "reason": str(exc)}
    # 4. Agent Mail 注册成功后才查询身份并通知 pane。
    notified = []
    if reg_ok:
        time.sleep(2)
        duplicate_agent_types = {
            plan["agent"] for plan in plans
            if sum(1 for item in plans if item["agent"] == plan["agent"]) > 1
        }
        if duplicate_agent_types:
            warnings.append(
                "同类型多实例仅使用本地实例名，已跳过自动 Agent Mail 身份绑定: "
                + "、".join(sorted(duplicate_agent_types))
            )
        identities = {
            plan["agent"]: _identity_name(canonical_project, plan["agent"])
            for plan in plans if plan["agent"] not in duplicate_agent_types
        }
        roster = "；".join(
            f"{agent}={identity}"
            for agent, identity in identities.items() if identity
        )
        for result in results:
            plan = result["plan"]
            atype = plan["agent"]
            pid = result["start"].get("pane_id")
            if (
                result.get("error") or plan["name"] not in started_instances
                or atype in duplicate_agent_types or not pid
            ):
                continue
            my_name = _identity_name(canonical_project, atype)
            if not my_name:
                warnings.append(f"{atype} 身份未注册或已 retired，未发送身份告知")
                continue
            context = None
            if coordination_run:
                coordination.bind_identity(
                    coordination_run["run_id"], plan["id"], my_name, pid
                )
                context = coordination.run_context(
                    coordination_run["run_id"], my_name
                )
            hint = _identity_hint(
                my_name, canonical_project, atype, roster=roster,
                coordination_context=context,
            )
            herdr_client.pane_send(req.session, pid, hint, "prompt")
            notified.append(f"{atype}→{my_name}")
    return {
        "ok": not failed, "session": req.session, "workdir": req.workdir,
        "session_created": session_created, "session_started": session_started,
        "started": started, "started_instances": started_instances,
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
    snap = herdr_client.snapshot()
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
    my_name = _identity_name(project, agent_type)
    if not my_name:
        return {
            "ok": False,
            "needs_registration": True,
            "project": project,
            "error": "该通信项目下没有此 agent 的有效身份（未注册或已 retired）",
        }
    hint = _identity_hint(my_name, project, agent_type)
    result = herdr_client.pane_send(session, pane_id, hint, "prompt")
    return {"ok": "error" not in result, "pane_id": pane_id, "agent": agent_type,
            "name": my_name, "project": project, "result": result}


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
    snap = herdr_client.snapshot()
    sess = next((s for s in snap.get("sessions", []) if s.get("session") == name), None)
    if not sess:
        raise HTTPException(404, f"session 不存在: {name}")
    if not AGENT_MAIL_INIT_SCRIPT.is_file():
        return {"ok": False, "unavailable": True, "error": "Agent Mail 未安装"}
    try:
        r = subprocess.run(
            [str(AGENT_MAIL_INIT_SCRIPT), "--project", project], cwd=project,
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            detail = (r.stderr or r.stdout)[-300:]
            return {"ok": False, "project": project, "error": detail or "am-init-project 失败"}
        # 注册成功后,通知各 agent pane 它们的身份
        notified = []
        missing_identities = []
        for p in sess.get("panes", []):
            agent_type = p.get("agent")
            pane_id = p.get("pane_id")
            if not agent_type or not pane_id:
                continue
            my_name = _identity_name(project, agent_type)
            if not my_name:
                missing_identities.append(agent_type)
                continue
            hint = _identity_hint(my_name, project, agent_type, registered=True)
            herdr_client.pane_send(name, pane_id, hint, "prompt")
            notified.append(f"{agent_type}({pane_id})→{my_name}")
        return {
            "ok": True, "project": project,
            "notified": notified,
            "missing_identities": missing_identities,
            "output": r.stdout[-300:] if r.stdout else "",
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


@app.websocket("/api/term/{term_id}")
async def api_term_ws(websocket: WebSocket, term_id: str):
    """终端 WebSocket 双向桥接:浏览器↔PTY。"""
    if not _websocket_authenticated(websocket) or not _same_origin(
        websocket.headers.get("origin"), websocket.headers.get("host")
    ):
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
                    except json.JSONDecodeError:
                        pass
                try:
                    await asyncio.to_thread(terminal.write_term, term_id, text)
                except (TimeoutError, OSError) as e:
                    logger.warning("terminal input write failed %s: %s", term_id, e)
                    await websocket.send_text(f"\r\n[输入未完整写入: {e}]\r\n")
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("terminal websocket failed: %s", term_id)
    finally:
        if pump_task:
            pump_task.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect):
                await pump_task
        _release_term_websocket(term_id, connection)
        with suppress(Exception):
            await websocket.close()


# ── SSE 实时推送(看板状态变化) ────────────────────────────────

_live_state: dict[str, Any] = {
    "revision": 0,
    "unread": None,
    "snapshot": None,
    "attention": None,
}
_poller_task: asyncio.Task | None = None


async def _poll_live_state() -> None:
    global _live_state
    last_sig = ""
    attention_ids: set[str] | None = None
    while True:
        try:
            await asyncio.to_thread(_expire_zoom_leases)
            snap = await asyncio.to_thread(_board_snapshot)
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
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("live state poll failed")
        await asyncio.sleep(4)


@app.get("/api/events")
async def api_events(request: Request):
    """把共享轮询缓存中的变化推送给浏览器。"""
    last_revision = -1

    async def event_gen():
        nonlocal last_revision
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
            await asyncio.sleep(1)

    return EventSourceResponse(event_gen())


# ── 静态前端 ────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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
    return FileResponse(
        STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
    )


@app.get("/health")
def health():
    mail_status = _agent_mail_status()
    push_status = web_push.public_config()
    return {
        "status": "ok",
        "ts": time.time(),
        "db": mail_status["available"],
        "herdr": herdr_client.is_available(),
        "hub": mail_status["write_available"],
        "push": push_status["available"],
    }


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("COCKPIT_HOST", "127.0.0.1")
    port = int(os.environ.get("COCKPIT_PORT", "8790"))
    _validate_bind(host)
    uvicorn.run(app, host=host, port=port, log_level="info")
