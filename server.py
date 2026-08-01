"""server.py — Agent Cockpit FastAPI 入口。

与 herdr / Agent Mail hub 同机部署,直读本地 SQLite + 调本机 hub MCP + herdr socket。
Mac/手机浏览器通过内网访问(默认 :8790)。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, UploadFile, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

import db
import hub_client
import herdr_client
import tasks
import uploads
import files
from pydantic import BaseModel

app = FastAPI(title="Agent Mail Dashboard")
STATIC_DIR = Path(__file__).parent / "static"


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


# ── 数据/通信路由 ───────────────────────────────────────────────

@app.get("/api/overview")
def api_overview():
    """全局总览:项目列表 + 未读 + 统计。"""
    try:
        return db.overview()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/projects/{slug}")
def api_project(slug: str):
    """项目详情:agent 列表 + 消息流。"""
    proj = db.project_by_slug(slug)
    if not proj:
        raise HTTPException(404, f"项目不存在: {slug}")
    return {
        "project": proj,
        "agents": db.list_agents(proj["id"]),
        "messages": db.recent_messages(proj["id"]),
    }


@app.post("/api/send")
def api_send(req: SendMessageReq):
    """以某 agent 身份发消息(走 hub MCP 保证一致性)。"""
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
    data = await file.read()
    try:
        return uploads.save_upload(file.filename or "upload.bin", data)
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


@app.get("/api/files/read")
def api_files_read(path: str):
    """读文件内容。"""
    try:
        return files.read_file(path)
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
def api_herdr_pane(session: str, pane_id: str, lines: int = 80, is_agent: bool = False):
    """读 pane 终端输出(live)。agent pane 用 agent read。"""
    return herdr_client.pane_read(session, pane_id, lines, is_agent)


@app.post("/api/herdr/pane/{session}/{pane_id}/send")
def api_herdr_pane_send(session: str, pane_id: str, req: PaneSendReq):
    """往 pane 发指令(send-keys 或 prompt)。"""
    return herdr_client.pane_send(session, pane_id, req.text, req.mode)


@app.post("/api/herdr/start")
def api_herdr_start(req: StartAgentReq):
    """在 session 里启动一个 agent pane。"""
    return herdr_client.start_agent(req.session, req.workdir, req.agent, req.model)


# ── SSE 实时推送(看板状态变化) ────────────────────────────────

@app.get("/api/events")
async def api_events(request: Request):
    """轮询 SQLite + herdr 看板,有变化推 SSE。"""
    last_unread = -1
    last_herdr_sig = ""

    async def event_gen():
        nonlocal last_unread, last_herdr_sig
        while True:
            if await request.is_disconnected():
                break
            try:
                events = []
                unread = db.global_unread_count()
                if unread != last_unread:
                    events.append({"event": "unread", "data": json.dumps({"count": unread})})
                    last_unread = unread
                # herdr 看板签名:pane+status 的指纹,变了就推
                if herdr_client.is_available():
                    snap = herdr_client.snapshot()
                    sig = json.dumps([
                        (p.get("session"), p.get("pane_id"), p.get("agent"),
                         p.get("agent_status"), p.get("revision"))
                        for p in snap.get("panes", [])
                    ], ensure_ascii=False)
                    if sig != last_herdr_sig:
                        events.append({"event": "board", "data": json.dumps(snap, ensure_ascii=False)})
                        last_herdr_sig = sig
                for e in events:
                    yield e
            except Exception:
                pass
            await asyncio.sleep(4)

    return EventSourceResponse(event_gen())


# ── 静态前端 ────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "ts": time.time(),
        "db": db.DB_PATH.is_file(),
        "herdr": herdr_client.is_available(),
        "hub": True,
    }


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("COCKPIT_HOST", "0.0.0.0")
    port = int(os.environ.get("COCKPIT_PORT", "8790"))
    uvicorn.run(app, host=host, port=port, log_level="info")
