"""uploads.py — 文件/图片上传落盘到 ~/dashboard-uploads/。

供手机/电脑浏览器上传截图、文件,落盘后返回路径,
供 codex exec -i 使用,或作为邮件附件。
"""
from __future__ import annotations

import time
from pathlib import Path

UPLOAD_DIR = Path.home() / "dashboard-uploads"
MAX_SIZE = 20 * 1024 * 1024  # 20MB
ALLOWED_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".pdf", ".txt", ".md", ".json", ".csv", ".log",
    ".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp",
    ".zip", ".tar", ".gz",
}


def save_upload(filename: str, data: bytes) -> dict:
    """落盘上传文件,返回 {id, path, filename, size}。"""
    if len(data) > MAX_SIZE:
        raise ValueError(f"文件过大: {len(data)} bytes > {MAX_SIZE}")
    ext = Path(filename).suffix.lower()
    if ext and ext not in ALLOWED_EXT:
        raise ValueError(f"不支持的文件类型: {ext}")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 时间戳前缀防重名 + 保留原名
    safe_name = filename.replace("/", "_").replace("\\", "_")
    stamp = int(time.time() * 1000)
    dest = UPLOAD_DIR / f"{stamp}-{safe_name}"
    dest.write_bytes(data)
    return {
        "id": dest.stem,
        "path": str(dest),
        "filename": safe_name,
        "size": len(data),
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
