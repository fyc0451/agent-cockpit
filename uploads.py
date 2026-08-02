"""uploads.py — 文件/图片上传落盘到 ~/dashboard-uploads/。

供手机/电脑浏览器上传截图、文件,落盘后返回路径,
供 codex exec -i 使用,或作为邮件附件。
"""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Any

UPLOAD_DIR = Path.home() / "dashboard-uploads"
MAX_SIZE = 20 * 1024 * 1024  # 20MB
ALLOWED_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".pdf", ".txt", ".md", ".json", ".csv", ".log",
    ".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp",
    ".zip", ".tar", ".gz",
}


class UploadTooLarge(ValueError):
    pass


def _safe_name(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c if c.isprintable() and c not in "/\\" else "_" for c in name).strip()
    ext = Path(name).suffix.lower()
    if not name or ext not in ALLOWED_EXT:
        raise ValueError(f"不支持的文件类型: {ext or '(无扩展名)'}")
    return name


async def save_upload_file(filename: str, source: Any) -> dict:
    """分块保存上传文件,返回 {id, path, filename, size}。"""
    safe_name = _safe_name(filename)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    dest = UPLOAD_DIR / f"{stamp}-{secrets.token_hex(4)}-{safe_name}"
    size = 0
    try:
        with dest.open("xb") as out:
            while chunk := await source.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_SIZE:
                    raise UploadTooLarge(f"文件过大: {size} bytes > {MAX_SIZE}")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except Exception:
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
    return [
        {"id": f.stem, "path": str(f), "filename": f.name, "size": f.stat().st_size}
        for f in files if f.is_file()
    ]
