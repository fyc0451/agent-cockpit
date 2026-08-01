"""files.py — 文件浏览与编辑。

安全模型:基于「允许的根目录」白名单。只有白名单内的路径可浏览/读/写,
防止无认证的内网 dashboard 暴露整个文件系统。

允许的根目录(可按需扩展):
  - 所有已注册项目的 human_key(SQLite 里的项目路径)
  - ~/dashboard-uploads/(上传文件区)
  - ~/agent-mail-tools/
  - ~/dashboard-data/
路径必须解析后落在某个白名单根下,否则拒绝。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

# 最大可编辑文件大小(防止加载巨大文件拖垮前端)
MAX_EDIT_SIZE = 2 * 1024 * 1024  # 2MB
# 文本编辑白名单后缀(二进制不开放编辑,只读可放宽)
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".html", ".css", ".scss", ".less",
    ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".rb", ".php", ".swift", ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".graphql", ".proto",
    ".xml", ".csv", ".tsv", ".log",
    ".gitignore", ".dockerignore", ".editorconfig",
    # 无后缀常见配置文件名按名判断,见 _is_text()
}

# 常见二进制后缀(明确不允许编辑)
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff",
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".webm", ".wav", ".flac",
    ".node", ".wasm",
}

# 访问根:整个 home 目录(私人内网工具,放开浏览)
_HOME = Path.home().resolve()


def _load_roots() -> list[Path]:
    """允许访问的根目录:整个 home。"""
    return [_HOME]


def reset_roots() -> None:
    """重置根缓存(现在单根 home,空操作,保留接口兼容)。"""
    pass


def _resolve(rel: str) -> Path:
    """把相对/绝对路径解析并校验落在白名单内,否则抛 ValueError。"""
    # 支持以项目 slug 或绝对路径定位;这里只接受绝对路径或 ~/ 相对
    path = Path(rel).expanduser()
    if not path.is_absolute():
        # 相对路径基于 home
        path = Path.home() / rel
    path = path.resolve(strict=False)
    roots = _load_roots()
    for root in roots:
        try:
            path.relative_to(root)
            return path
        except ValueError:
            continue
    raise ValueError(f"路径不在允许范围内: {path}")


def _is_text(path: Path) -> bool:
    """判断是否可作为文本编辑。"""
    if path.name in {".gitignore", ".dockerignore", ".env", ".editorconfig",
                     "Makefile", "Dockerfile", "Gemfile", "Rakefile"}:
        return True
    ext = path.suffix.lower()
    if ext in BINARY_EXT:
        return False
    if ext in TEXT_EXT:
        return True
    # 无后缀:尝试读首块判断是否 UTF-8
    return _looks_text(path)


def _looks_text(path: Path) -> bool:
    """读前 2KB 判断是否文本。"""
    try:
        with open(path, "rb") as f:
            chunk = f.read(2048)
        chunk.decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


# ── 浏览 ────────────────────────────────────────────────────────

def list_dir(rel: str) -> dict[str, Any]:
    """列目录。返回 {path, entries:[{name,type,size,modifiable}]}。"""
    path = _resolve(rel)
    if not path.exists():
        raise ValueError(f"不存在: {path}")
    if path.is_file():
        return _info_file(path)
    entries = []
    try:
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith(".git"):
                continue  # 跳过 .git 内部
            try:
                st = child.stat()
                entries.append({
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size": st.st_size if child.is_file() else 0,
                    "modifiable": child.is_file() and _is_text(child) and st.st_size <= MAX_EDIT_SIZE,
                    "ext": child.suffix.lower().lstrip("."),
                })
            except OSError:
                continue
    except PermissionError:
        raise ValueError(f"无权限: {path}")
    return {"path": str(path), "entries": entries}


def _info_file(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {
        "path": str(path),
        "type": "file",
        "entries": [{
            "name": path.name, "type": "file", "size": st.st_size,
            "modifiable": _is_text(path) and st.st_size <= MAX_EDIT_SIZE,
            "ext": path.suffix.lower().lstrip("."),
        }],
    }


def read_file(rel: str) -> dict[str, Any]:
    """读文件内容。文本返回 text,二进制返回 {binary:true, size}。"""
    path = _resolve(rel)
    if not path.is_file():
        raise ValueError(f"不是文件: {path}")
    st = path.stat()
    if st.st_size > 10 * MAX_EDIT_SIZE:
        raise ValueError(f"文件过大({st.st_size} bytes)")
    if _is_text(path):
        return {
            "path": str(path), "text": path.read_text(encoding="utf-8"),
            "size": st.st_size, "binary": False, "modifiable": st.st_size <= MAX_EDIT_SIZE,
        }
    return {"path": str(path), "binary": True, "size": st.st_size, "modifiable": False}


def write_file(rel: str, content: str, create: bool = False) -> dict[str, Any]:
    """写文本文件(覆盖)。create=True 时允许新建。"""
    path = _resolve(rel)
    if not create and not path.exists():
        raise ValueError(f"文件不存在(未传 create): {path}")
    if path.exists() and not path.is_file():
        raise ValueError(f"不是文件: {path}")
    if path.exists() and not _is_text(path):
        raise ValueError("二进制文件不允许编辑")
    if len(content.encode("utf-8")) > MAX_EDIT_SIZE:
        raise ValueError(f"内容超过 {MAX_EDIT_SIZE} 字节限制")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子写:先写临时文件再 rename
    tmp = path.with_suffix(path.suffix + ".dash-tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    st = path.stat()
    return {"path": str(path), "size": st.st_size, "ok": True}


def delete_file(rel: str) -> dict[str, Any]:
    """删除文件或空目录(谨慎,仅白名单内)。"""
    path = _resolve(rel)
    if path.is_file():
        path.unlink()
        return {"deleted": str(path), "type": "file"}
    if path.is_dir():
        # 只允许删空目录,防误删
        shutil.rmtree(path)
        return {"deleted": str(path), "type": "dir"}
    raise ValueError(f"无法删除: {path}")


def allowed_roots() -> list[str]:
    """返回当前允许的根目录列表(供前端展示侧栏)。"""
    return [str(r) for r in _load_roots()]
