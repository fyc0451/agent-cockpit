"""uploads.py — 文件/图片上传落盘到 ~/dashboard-uploads/。

供手机/电脑浏览器上传截图、文件,落盘后返回路径,
供 codex exec -i 使用,或作为邮件附件。
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from pathlib import Path
from typing import Any

from . import runtime_paths

UPLOAD_DIR = runtime_paths.uploads_root()
MAX_SIZE = 100 * 1024 * 1024  # 100MB(默认值;设置页 upload_max_mb 可覆盖)


def _max_size() -> int:
    """实际上限:设置页 upload_max_mb 优先,常量兜底。"""
    try:
        from . import settings
        return settings.upload_max_bytes(MAX_SIZE)
    except Exception:
        return MAX_SIZE


class UploadTooLarge(ValueError):
    pass


def _safe_name(filename: str) -> str:
    """清洗文件名(去路径分隔/不可打印字符)。不限扩展名,所有格式放开。"""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c if c.isprintable() and c not in "/\\" else "_" for c in name).strip()
    if not name:
        raise ValueError("文件名为空")
    return name


async def save_upload_file(filename: str, source: Any) -> dict:
    """分块保存上传文件,返回 {id, path, filename, size}。"""
    safe_name = _safe_name(filename)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    dest = UPLOAD_DIR / f"{stamp}-{secrets.token_hex(4)}-{safe_name}"
    size = 0
    limit = _max_size()
    try:
        with dest.open("xb") as out:
            while chunk := await source.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise UploadTooLarge(f"文件过大: {size} bytes > {limit}")
                await asyncio.to_thread(out.write, chunk)
            await asyncio.to_thread(out.flush)
            await asyncio.to_thread(os.fsync, out.fileno())
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return {
        "id": dest.stem,
        "path": str(dest),
        "filename": safe_name,
        "size": size,
    }


def list_uploads(limit: int = 50) -> list[dict]:
    """列出最近的上传文件。"""
    if not UPLOAD_DIR.is_dir():
        return []
    files = sorted(UPLOAD_DIR.iterdir(), key=lambda p: p.name, reverse=True)[:limit]
    result = []
    for f in files:
        try:
            if not f.is_file():
                continue
            size = f.stat().st_size
        except OSError:
            # 上传列表与清理/用户删除可并发；消失的条目直接跳过。
            continue
        result.append({
            "id": f.stem, "path": str(f), "filename": f.name, "size": size,
        })
    return result
