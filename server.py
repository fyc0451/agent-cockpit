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
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, UploadFile, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

import db
import hub_client
import herdr_client
import tasks
import uploads
import files
import terminal
import web_push
from pydantic import BaseModel


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


app = FastAPI(title="Agent Cockpit", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"
COCKPIT_TOKEN = os.environ.get("COCKPIT_TOKEN", "")
AUTH_COOKIE = "cockpit_session"
PUBLIC_PATHS = {"/", "/health", "/api/auth/status", "/api/auth/login"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
logger = logging.getLogger("agent-cockpit")
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PANE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$")
VALID_AGENTS = {"codex", "kimi", "claude", "qoder", "qodercli", "qodercn", "grok", "opencode"}
VALID_LAYOUTS = {"right", "horizontal", "down", "vertical", "tab"}
VALID_PANE_SEND_MODES = {"send", "prompt", "keys"}
AGENT_MAIL_INIT_SCRIPT = Path.home() / "agent-mail-tools" / "am-init-project"


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
        raise RuntimeError("非本机监听必须设置 COCKPIT_TOKEN")
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


def _identity_name(cwd: str, agent_type: str) -> str:
    try:
        ident = db.identity_by_cwd(cwd, agent_type)
    except Exception:
        ident = None
    return ident["name"] if ident else f"{agent_type}-main"


def _agent_mail_status() -> dict[str, Any]:
    read_status = db.status()
    if not read_status["available"]:
        return {
            **read_status,
            "read_available": False,
            "write_available": False,
            "write_reason": read_status["reason"],
        }
    write_status = hub_client.status()
    return {
        "available": True,
        "reason": None,
        "read_available": True,
        "write_available": write_status["available"],
        "write_reason": write_status["reason"],
    }


@app.middleware("http")
async def protect_api(request: Request, call_next):
    path = request.url.path
    protected = path.startswith("/api/") or path in {"/docs", "/redoc", "/openapi.json"}
    if not protected or path in PUBLIC_PATHS:
        return await call_next(request)
    if not _request_authenticated(request):
        status = 401 if COCKPIT_TOKEN else 403
        detail = "未认证" if COCKPIT_TOKEN else "未设置 COCKPIT_TOKEN 时仅允许本机访问"
        return JSONResponse(
            {"detail": detail}, status_code=status,
            headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
        )
    cookie_auth = _valid_cookie(request.cookies.get(AUTH_COOKIE))
    if request.method not in SAFE_METHODS and cookie_auth and not _valid_bearer(
        request.headers.get("authorization")
    ):
        if not _same_origin(request.headers.get("origin"), request.headers.get("host")):
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


class AckReq(BaseModel):
    project_id: int
    agent_name: str
    message_id: int


class StartTaskReq(BaseModel):
    workdir: str
    prompt: str
    images: list[str] | None = None  # 已上传的文件 path 列表
    model: str | None = None


class PaneSendReq(BaseModel):
    text: str
    mode: str = "send"  # send | prompt


class WriteFileReq(BaseModel):
    path: str
    content: str
    create: bool = False


class StartAgentReq(BaseModel):
    session: str
    workdir: str
    agent: str = "codex"  # codex | kimi | qodercli
    model: str | None = None


class SetupWorkspaceReq(BaseModel):
    """一键工作区初始化:split pane + 启动 agent + 注册身份 + 通知。"""
    session: str
    workdir: str
    agents: list[str] = ["codex"]  # 要开的 agent 列表,如 ["codex","kimi"]
    layout: str = "right"  # right(水平/左右) | down(垂直/上下) | tab(多页/不分割)


class LoginReq(BaseModel):
    token: str


class PushKeysReq(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionReq(BaseModel):
    endpoint: str
    expirationTime: int | None = None
    keys: PushKeysReq


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
            raise HTTPException(403, "未设置 COCKPIT_TOKEN 时仅允许本机访问")
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
            mail_status = {
                "available": False,
                "reason": f"Agent Mail 查询失败: {exc}",
                "read_available": False,
                "write_available": False,
                "write_reason": f"Agent Mail 查询失败: {exc}",
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
    return {
        "project": proj,
        "agents": db.list_agents(proj["id"]),
        "messages": db.recent_messages(proj["id"]),
        "agent_mail": mail_status,
    }


def _build_attention(snapshot: dict[str, Any]) -> dict[str, Any]:
    """聚合所有需要用户介入的对象，不让可选能力拖垮核心看板。"""
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
    mail_rows: list[dict[str, Any]] = []
    if mail_status["available"]:
        try:
            mail_rows = db.unread_messages(100)
        except Exception as exc:
            logger.exception("attention Agent Mail query failed")
            mail_status = {
                "available": False,
                "reason": f"Agent Mail 查询失败: {exc}",
                "read_available": False,
                "write_available": False,
                "write_reason": f"Agent Mail 查询失败: {exc}",
            }
    for message in mail_rows:
        message_id = str(message.get("id") or "")
        slug = str(message.get("project_slug") or "")
        sender = str(message.get("sender_name") or "未知发件人")
        items.append({
            "id": f"mail:{message_id}",
            "kind": "mail_unread",
            "priority": 80,
            "title": str(message.get("subject") or "未读 Agent Mail"),
            "detail": f"{sender} · {slug}",
            "created_ts": message.get("created_ts"),
            "target": {
                "view": "msgs",
                "project_slug": slug,
                "message_id": message_id,
            },
            "url": (
                f"/#/attention/mail/{quote(slug, safe='')}/"
                f"{quote(message_id, safe='')}"
            ),
        })
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
        "items": items,
        "count": len(items),
        "mail_unread": len(mail_rows),
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
    return _build_attention(herdr_client.snapshot())


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
    try:
        result = hub_client.send_message(
            project_key=proj["human_key"],
            sender_name=sender["name"],
            sender_token=sender["registration_token"],
            to=req.to,
            subject=req.subject,
            body_md=req.body,
            thread_id=req.thread_id,
            importance=req.importance,
            ack_required=req.ack_required,
        )
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(500, f"发送失败: {e}")


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
    try:
        result = hub_client.acknowledge_message(
            project_key=proj["human_key"],
            agent_name=agent["name"],
            registration_token=agent["registration_token"],
            message_id=req.message_id,
        )
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


# ── 文件浏览/编辑路由 ──────────────────────────────────────────

@app.get("/api/files/roots")
def api_file_roots():
    """返回允许浏览的根目录列表。"""
    return {"roots": files.allowed_roots()}


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


@app.post("/api/tasks/{task_id}/apply")
def api_task_apply(task_id: str, action: str = "apply"):
    try:
        return tasks.task_apply(task_id, action)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── herdr 路由(多 session 聚合,Orca 式看板数据源) ────────────

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
    return herdr_client.snapshot()


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

    用 pane 的 cwd + agent 类型,反查 agent-mail 身份(花名/项目)。
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
    try:
        ident = db.identity_by_cwd(cwd, agent_type)
    except Exception:
        ident = None
    if not ident:
        return {
            "found": False,
            "reason": "Agent Mail 不可用或该 pane 尚未注册身份",
        }
    return {
        "found": True,
        "name": ident["name"],
        "program": ident["program"],
        "model": ident.get("model", ""),
        "project_key": ident["human_key"],
        "session": session,
        "cwd": cwd,
        "mail_hint": (
            f'协作者 {ident["name"]} 已接入 agent-mail。'
            f'发消息: mail-send --agent <你的类型> --instance main --project "{ident["human_key"]}" '
            f'--to {ident["name"]} --subject "..." --body "..." '
            f'(mail-send 已在 PATH 中,需在项目目录 {ident["human_key"]} 下执行)'
        ),
    }


@app.post("/api/herdr/pane/{session}/{pane_id}/send")
def api_herdr_pane_send(session: str, pane_id: str, req: PaneSendReq):
    """往 pane 发指令(send-keys 或 prompt)。"""
    _validate_session_name(session)
    _validate_pane_id(pane_id)
    if req.mode not in VALID_PANE_SEND_MODES:
        raise HTTPException(400, f"不支持的发送模式: {req.mode}")
    return herdr_client.pane_send(session, pane_id, req.text, req.mode)


@app.post("/api/herdr/start")
def api_herdr_start(req: StartAgentReq):
    """在 session 里启动一个 agent pane。"""
    _validate_session_name(req.session)
    if req.agent not in VALID_AGENTS:
        raise HTTPException(400, f"不支持的 agent: {req.agent}")
    return herdr_client.start_agent(req.session, req.workdir, req.agent, req.model)


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
    return herdr_client.stop_session(name)


@app.delete("/api/herdr/session/{name}")
def api_herdr_session_delete(name: str):
    """删除已停止的 herdr session。"""
    _validate_session_name(name)
    return herdr_client.delete_session(name)


@app.post("/api/herdr/setup-workspace")
def api_setup_workspace(req: SetupWorkspaceReq):
    """一键工作区初始化:自动建 session → split pane + 启动 → 注册身份 → 通知。

    如果 session 不存在,通过 PTY 自动创建(herdr 需要 TTY 才能 attach/创建)。
    """
    import subprocess
    import time
    _validate_session_name(req.session)
    if not req.agents or any(agent not in VALID_AGENTS for agent in req.agents):
        raise HTTPException(400, "agents 包含不支持的类型")
    if req.layout not in VALID_LAYOUTS:
        raise HTTPException(400, f"不支持的布局: {req.layout}")
    if not Path(req.workdir).expanduser().resolve().is_dir():
        raise HTTPException(400, "工作目录不存在")
    # 0. 检查 session 是否存在,不存在则自动创建
    sessions = herdr_client.list_sessions()
    existing = {s["name"] for s in sessions}
    session_created = False
    if req.session not in existing:
        # 用 PTY 终端创建 session(herdr --session 需要 TTY)
        try:
            t = terminal.create_term(req.workdir)
            time.sleep(0.5)
            # 在 PTY 里跑 herdr --session <name>(创建 + detach)
            terminal.write_term(t["id"], f"herdr --session {shlex.quote(req.session)}\r")
            time.sleep(4)  # 等 herdr server 完全启动并创建 session
            # detach:发 Ctrl-b d(herdr detach 序列),让 client 脱离但 session server 继续跑
            terminal.write_term(t["id"], "\x02d")  # Ctrl-b + d
            time.sleep(2)  # 等 detach 完成,session server 稳定
            # 注意:不 kill PTY!herdr client detach 后 PTY 回到 shell,
            # session server 是独立进程会继续跑。kill PTY 可能连带杀 server。
            session_created = True
        except Exception as e:
            return {"ok": False, "error": f"创建 session 失败: {e}"}
    results = []
    # 1. 为每个 agent 开 pane + 启动
    for agent_type in req.agents:
        r = herdr_client.start_agent(req.session, req.workdir, agent_type, layout=req.layout)
        results.append({"agent": agent_type, "start": r})
        time.sleep(2)  # 等 agent 启动
    # 2. 注册身份(am-init-project)
    reg_ok = False
    mail_status: dict[str, Any]
    if not AGENT_MAIL_INIT_SCRIPT.is_file():
        mail_status = {"available": False, "reason": "Agent Mail 未安装，已跳过身份注册"}
    else:
        try:
            r = subprocess.run(
                [str(AGENT_MAIL_INIT_SCRIPT)], cwd=req.workdir,
                capture_output=True, text=True, timeout=60,
            )
            reg_ok = r.returncode == 0
            mail_status = {
                "available": reg_ok,
                "reason": None if reg_ok else (r.stderr[-300:] or "am-init-project 失败"),
            }
        except Exception as exc:
            mail_status = {"available": False, "reason": str(exc)}
    # 3. Agent Mail 注册成功后才查询身份并通知 pane。
    notified = []
    sess = None
    if reg_ok:
        time.sleep(2)
        snap = herdr_client.snapshot()
        sess = next(
            (s for s in snap.get("sessions", []) if s.get("session") == req.session),
            None,
        )
    if sess:
        for p in sess.get("panes", []):
            atype = p.get("agent")
            pid = p.get("pane_id")
            if not atype or not pid:
                continue
            my_name = _identity_name(req.workdir, atype)
            hint = (
                "[agent-mail 身份告知] 花名={name},项目={proj}。"
                "发消息: mail-send --agent {ag} --instance main --project \"{proj}\" "
                "--to <花名> --subject \"...\" --body \"...\";"
                "收消息: mail-recv --agent {ag} --instance main --project \"{proj}\" --unread。"
            ).format(name=my_name, proj=req.workdir, ag=atype)
            herdr_client.pane_send(req.session, pid, hint, "prompt")
            notified.append(f"{atype}→{my_name}")
    start_ok = all(
        result["start"].get("available", True) is not False
        and "error" not in result["start"]
        for result in results
    )
    return {
        "ok": start_ok, "session": req.session, "workdir": req.workdir,
        "session_created": session_created,
        "started": [r["agent"] for r in results], "registered": reg_ok,
        "notified": notified, "agent_mail": mail_status,
    }


@app.post("/api/herdr/pane/{session}/{pane_id}/tell-identity")
def api_herdr_pane_tell_identity(session: str, pane_id: str):
    """手动给单个 pane 发身份告知(适用于手动在 herdr 里开的新 pane)。"""
    _validate_session_name(session)
    _validate_pane_id(pane_id)
    snap = herdr_client.snapshot()
    p = next((x for s in snap.get("sessions", [])
              if s.get("session") == session
              for x in s.get("panes", []) if x.get("pane_id") == pane_id), None)
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
    my_name = _identity_name(cwd, agent_type)
    hint = (
        "[agent-mail 身份告知] 花名={name},项目={proj}。"
        "发消息: mail-send --agent {ag} --instance main --project \"{proj}\" "
        "--to <花名> --subject \"...\" --body \"...\";"
        "收消息: mail-recv --agent {ag} --instance main --project \"{proj}\" --unread。"
    ).format(name=my_name, proj=cwd, ag=agent_type)
    result = herdr_client.pane_send(session, pane_id, hint, "prompt")
    return {"ok": "error" not in result, "pane_id": pane_id, "agent": agent_type,
            "name": my_name, "result": result}


@app.post("/api/herdr/session/{name}/init-mail")
def api_herdr_session_init_mail(name: str):
    """给 session 的项目目录注册全套 agent-mail 身份,并通知各 agent pane 它们的身份。

    流程:取 session cwd → am-init-project 注册 → 遍历 agent pane,
    给每个发一条身份告知 prompt(让它知道自己的花名 + 怎么收发消息)。
    """
    import subprocess
    _validate_session_name(name)
    snap = herdr_client.snapshot()
    sess = next((s for s in snap.get("sessions", []) if s.get("session") == name), None)
    if not sess:
        raise HTTPException(404, f"session 不存在: {name}")
    cwd = ""
    for p in sess.get("panes", []):
        cwd = p.get("cwd") or ""
        if cwd:
            break
    if not cwd:
        raise HTTPException(400, "该 session 无 cwd,无法注册")
    if not AGENT_MAIL_INIT_SCRIPT.is_file():
        return {"ok": False, "unavailable": True, "error": "Agent Mail 未安装"}
    try:
        r = subprocess.run(
            [str(AGENT_MAIL_INIT_SCRIPT)], cwd=cwd,
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return {"ok": False, "project": cwd, "error": r.stderr[-300:] or "am-init-project 失败"}
        # 注册成功后,通知各 agent pane 它们的身份
        notified = []
        for p in sess.get("panes", []):
            agent_type = p.get("agent")
            pane_id = p.get("pane_id")
            if not agent_type or not pane_id:
                continue
            my_name = _identity_name(cwd, agent_type)
            hint = (
                "[agent-mail 身份告知] 你的邮箱身份已注册:花名={name},项目={proj}。"
                "发消息: mail-send --agent {ag} --instance main --project \"{proj}\" "
                "--to <对方花名> --subject \"...\" --body \"...\";"
                "收消息: mail-recv --agent {ag} --instance main --project \"{proj}\" --unread。"
                "发信后对方 pane 会自动收到通知。"
            ).format(name=my_name, proj=cwd, ag=agent_type)
            herdr_client.pane_send(name, pane_id, hint, "prompt")
            notified.append(f"{agent_type}({pane_id})→{my_name}")
        return {
            "ok": True, "project": cwd,
            "notified": notified,
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
):
    """创建一个新终端会话(PTY bash)。"""
    try:
        return terminal.create_term(cwd, cols, rows, label)
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
    terminal.kill_term(term_id)
    return {"ok": True}


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
    terms_now = {t["id"] for t in terminal.list_terms()}
    if term_id not in terms_now:
        await websocket.send_text("\r\n[终端会话不存在,已关闭]\r\n")
        await websocket.close()
        return
    pump_task = None
    try:
        # 输出转发任务:PTY → WebSocket
        async def pump_out():
            while True:
                data = await asyncio.to_thread(terminal.read_output, term_id, 0.15)
                if data:
                    await websocket.send_bytes(data)
                elif not terminal.is_alive(term_id):
                    tail = await asyncio.to_thread(terminal.drain_output, term_id, 0.05)
                    if tail:
                        await websocket.send_bytes(tail)
                    await websocket.send_text("\r\n[进程已退出]\r\n")
                    break
                await asyncio.sleep(0.02)
        pump_task = asyncio.create_task(pump_out())
        # 主循环:接收浏览器输入
        while True:
            msg = await websocket.receive()
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
            with suppress(asyncio.CancelledError):
                await pump_task
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
            snap = await asyncio.to_thread(herdr_client.snapshot)
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
                    "panes": [
                        (p.get("session"), p.get("pane_id"), p.get("agent"),
                         p.get("agent_status"), p.get("revision"))
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
    return FileResponse(STATIC_DIR / "index.html")


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
