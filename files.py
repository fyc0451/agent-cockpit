"""files.py — 文件浏览与编辑。

安全模型:基于「允许的根目录」显式白名单。只有白名单内的路径可浏览/读/写/删,
防止内网 dashboard 暴露整个文件系统(尤其 ~/.ssh、~/.agent-mail 等敏感目录)。

允许的根目录:
  - 本项目(dashboard)目录
  - agent-mail DB 已注册且实际存在的项目 human_key
  - ~/dashboard-uploads/(上传文件区)
  - ~/dashboard-data/
  - ~/agent-mail-tools/
路径必须解析后落在某个白名单根下,否则拒绝;空路径直接拒绝。
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

# 最大可编辑文件大小(防止加载巨大文件拖垮前端)
MAX_EDIT_SIZE = 2 * 1024 * 1024  # 2MB
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_ENTRIES = 20_000
SEARCH_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
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

# 访问根白名单的构成:本项目目录 + home 下固定子目录 + DB 注册项目
_HOME = Path.home().resolve()
_PROJECT_DIR = Path(__file__).resolve().parent
_HOME_SUBDIRS = ("dashboard-uploads", "dashboard-data", "agent-mail-tools")


def _registered_project_roots() -> list[Path]:
    """agent-mail DB 已注册且实际存在的项目目录。"""
    try:
        import db  # 延迟导入,避免模块级依赖/循环
        projects = db.list_projects()
    except Exception:
        return []
    roots = []
    for row in projects:
        key = row.get("human_key")
        if not key:
            continue
        try:
            p = Path(key).expanduser().resolve()
        except OSError:
            continue
        if p.is_dir():
            roots.append(p)
    return roots


def _load_roots() -> list[Path]:
    """允许访问的根目录(显式白名单,去重后返回)。"""
    roots: list[Path] = [_PROJECT_DIR]
    for name in _HOME_SUBDIRS:
        roots.append((_HOME / name).resolve(strict=False))
    roots.extend(_registered_project_roots())
    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def reset_roots() -> None:
    """重置根缓存(根列表现由 _load_roots 实时计算,空操作,保留接口兼容)。"""
    pass


def _resolve(rel: str) -> Path:
    """把相对/绝对路径解析并校验落在白名单内,否则抛 ValueError。"""
    if not rel or not rel.strip():
        raise ValueError("路径为空")
    # 支持绝对路径或 ~/ 相对;相对路径基于 home
    path = Path(rel).expanduser()
    if not path.is_absolute():
        path = _HOME / rel
    path = path.resolve(strict=False)
    for root in _load_roots():
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


def search_files(rel: str, query: str, limit: int = 100) -> dict[str, Any]:
    """在白名单目录内递归按名称搜索文件和目录。"""
    query = query.strip()
    if not query:
        raise ValueError("搜索关键词为空")
    if len(query) > 128:
        raise ValueError("搜索关键词过长(最多 128 字符)")
    if not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise ValueError(f"搜索结果范围必须为 1-{MAX_SEARCH_RESULTS}")

    root = _resolve(rel)
    if not root.is_dir():
        raise ValueError(f"搜索范围不是目录: {root}")

    needle = query.casefold()
    results: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    stop = False

    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(
            (
                name for name in dirs
                if name not in SEARCH_SKIP_DIRS
                and not (current_path / name).is_symlink()
            ),
            key=str.casefold,
        )
        names.sort(key=str.casefold)

        entries = ([(name, "dir") for name in dirs]
                   + [(name, "file") for name in names])
        for name, kind in entries:
            item = current_path / name
            if item.is_symlink():
                continue
            scanned += 1
            if scanned > MAX_SEARCH_ENTRIES:
                truncated = True
                stop = True
                break
            if needle not in name.casefold():
                continue
            try:
                if kind == "dir":
                    if not item.is_dir():
                        continue
                    size = 0
                    modifiable = False
                else:
                    if not item.is_file():
                        continue
                    st = item.stat()
                    size = st.st_size
                    modifiable = _is_text(item) and size <= MAX_EDIT_SIZE
            except OSError:
                continue
            results.append({
                "name": name,
                "path": str(item),
                "relative": str(item.relative_to(root)),
                "type": kind,
                "size": size,
                "modifiable": modifiable,
                "ext": item.suffix.lower().lstrip("."),
            })
            if len(results) > limit:
                results.pop()
                truncated = True
                stop = True
                break
        if stop:
            break

    return {
        "path": str(root),
        "query": query,
        "results": results,
        "truncated": truncated,
    }


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


def download_path(rel: str) -> Path:
    """返回校验后的下载文件路径。"""
    path = _resolve(rel)
    if not path.is_file():
        raise ValueError(f"不是文件: {path}")
    return path


def write_file(rel: str, content: str, create: bool = False) -> dict[str, Any]:
    """写文本文件(覆盖)。create=True 时允许新建。

    原子写:同目录唯一临时文件 + fsync + os.replace,并保留原文件 mode。
    """
    path = _resolve(rel)
    if not create and not path.exists():
        raise ValueError(f"文件不存在(未传 create): {path}")
    if path.exists() and not path.is_file():
        raise ValueError(f"不是文件: {path}")
    if path.exists() and not _is_text(path):
        raise ValueError("二进制文件不允许编辑")
    data = content.encode("utf-8")
    if len(data) > MAX_EDIT_SIZE:
        raise ValueError(f"内容超过 {MAX_EDIT_SIZE} 字节限制")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".dash-tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    st = path.stat()
    return {"path": str(path), "size": st.st_size, "ok": True}


def _resolve_for_delete(rel: str) -> Path:
    """删除专用解析:只 resolve 父目录,保留最后一段。

    与 _resolve 不同,最后一段不做 dereference——若 entry 是 symlink,
    删的是 link 本身而非目标;父目录中的 symlink 仍全部解析,
    父路径 symlink 逃逸(指向白名单外)照样拒绝。
    """
    if not rel or not rel.strip():
        raise ValueError("路径为空")
    path = Path(rel).expanduser()
    if not path.is_absolute():
        path = _HOME / rel
    parent = path.parent.resolve(strict=False)
    candidate = parent / path.name
    for root in _load_roots():
        if candidate == root:
            # 是允许根本身:放行给 delete_file 的根检查,报专门的错误
            return candidate
        try:
            parent.relative_to(root)
            return candidate
        except ValueError:
            continue
    raise ValueError(f"路径不在允许范围内: {path}")


def delete_file(rel: str) -> dict[str, Any]:
    """删除文件/symlink/空目录(谨慎,仅白名单内)。

    禁止删除任一允许的根目录。symlink 只 unlink link 本身,
    绝不触碰目标(无论目标在白名单内还是外)。
    """
    path = _resolve_for_delete(rel)
    for root in _load_roots():
        if path == root:
            raise ValueError(f"不允许删除根目录: {path}")
    if path.is_symlink() or path.is_file():
        path.unlink()
        return {"deleted": str(path), "type": "file"}
    if path.is_dir():
        # 只允许删空目录,防误删
        try:
            path.rmdir()
        except OSError as e:
            raise ValueError(f"目录非空或不可删除: {path} ({e.strerror or e})")
        return {"deleted": str(path), "type": "dir"}
    raise ValueError(f"无法删除: {path}")


def allowed_roots() -> list[str]:
    """返回当前允许的根目录列表(供前端展示侧栏)。"""
    return [str(r) for r in _load_roots()]
