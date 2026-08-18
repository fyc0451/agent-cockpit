"""files.py — 文件浏览与编辑。

安全模型:基于「允许的根目录」显式白名单。只有白名单内的路径可浏览/读/写/删,
防止内网 dashboard 暴露整个文件系统(尤其 ~/.ssh、~/.agent-mail 等敏感目录)。

允许的根目录:
  - 本项目(dashboard)目录
  - agent-mail DB 已注册且实际存在的项目 human_key
  - 用户通过文件页显式添加的自定义目录
  - runtime_paths 当前生效的 uploads/data 根(默认 ~/dashboard-uploads、
    ~/dashboard-data;env 自定义 profile 时跟随覆盖值)
  - ~/agent-mail-tools/
路径必须解析后落在某个白名单根下,否则拒绝;空路径直接拒绝。
"""
from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
import threading
from enum import Enum
from pathlib import Path
from typing import Any

from . import next_profile, runtime_paths

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

# 允许内联预览的媒体后缀(svg 可携带脚本,不内联,只下载)
PREVIEW_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".avif",
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus",
    ".mp4", ".webm", ".mov", ".m4v", ".mkv",
}

# 目录打包下载的保护上限
MAX_ZIP_FILES = 5000
MAX_ZIP_SIZE = 500 * 1024 * 1024  # 500MB(打包前原始大小)
# 「添加工作区」挑选器一层最多返回这么多子目录（对齐 3080 browse maxEntries）
BROWSE_MAX_ENTRIES = 1000

# 访问根白名单的构成:本项目目录 + home 下固定子目录 + DB 注册项目
_HOME = Path.home().resolve()
_PROJECT_DIR = runtime_paths.INSTALL_ROOT
_ROOTS_LOCK = threading.Lock()


class CustomRootsReason(str, Enum):
    UNREADABLE = "unreadable"
    INVALID_JSON = "invalid_json"
    INVALID_SHAPE = "invalid_shape"
    TOO_MANY_ENTRIES = "too_many_entries"
    INVALID_ENTRY_TYPE = "invalid_entry_type"
    RELATIVE_PATH = "relative_path"
    NONCANONICAL_PATH = "noncanonical_path"
    BROAD_ROOT = "broad_root"
    SENSITIVE_ROOT = "sensitive_root"
    MISSING_PATH = "missing_path"
    NOT_DIRECTORY = "not_directory"


class CustomRootsError(RuntimeError):
    """持久化 custom roots 整体无效；正文仅含稳定 reason，不含路径。"""

    def __init__(self, reason: CustomRootsReason):
        self.reason = reason
        super().__init__(f"custom_roots_invalid:{reason.value}")


class TrustedRootError(RuntimeError):
    """Stable error for Registry-authorized read-only roots."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _custom_roots_file() -> Path:
    return runtime_paths.store("file_roots")


def _system_roots() -> list[Path]:
    # R2-E:白名单只纳入 resolver 当前生效的 data/uploads 根,不再硬编码
    # legacy ~/dashboard-data 与 ~/dashboard-uploads,自定义 profile 时不暴露默认 home 存储。
    roots = [_PROJECT_DIR]
    for p in (
        runtime_paths.uploads_root(),
        runtime_paths.data_root(),
        _HOME / "agent-mail-tools",
    ):
        rp = p.resolve(strict=False)
        if rp not in roots:
            roots.append(rp)
    return roots


def _registered_project_roots() -> list[Path]:
    """agent-mail DB 已注册且实际存在的项目目录。"""
    try:
        from . import db  # 延迟导入,避免模块级依赖/循环
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


def _custom_root_policy(path: Path) -> CustomRootsReason | None:
    broad = {Path("/"), _HOME.resolve(), _HOME.parent.resolve()}
    if path in broad:
        return CustomRootsReason.BROAD_ROOT
    blocked = (
        Path("/etc"), Path("/proc"), Path("/sys"), Path("/dev"), Path("/run"),
        _HOME / ".ssh", _HOME / ".gnupg", _HOME / ".agent-mail",
        _HOME / ".config" / "agent-cockpit",
        runtime_paths.data_root(), runtime_paths.config_root(),
        runtime_paths.state_root(), runtime_paths.uploads_root(),
    )
    for root in blocked:
        try:
            path.relative_to(root.resolve(strict=False))
        except ValueError:
            continue
        return CustomRootsReason.SENSITIVE_ROOT
    return None


def _persisted_custom_root(value: Any) -> Path:
    if not isinstance(value, str):
        raise CustomRootsError(CustomRootsReason.INVALID_ENTRY_TYPE)
    if "\x00" in value:
        raise CustomRootsError(CustomRootsReason.NONCANONICAL_PATH)
    lexical = Path(value)
    if not lexical.is_absolute():
        raise CustomRootsError(CustomRootsReason.RELATIVE_PATH)
    if value != str(lexical):
        raise CustomRootsError(CustomRootsReason.NONCANONICAL_PATH)
    try:
        check = lexical.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise CustomRootsError(CustomRootsReason.NONCANONICAL_PATH) from None
    reason = _custom_root_policy(check)
    if reason:
        raise CustomRootsError(reason)
    try:
        resolved = lexical.resolve(strict=True)
    except FileNotFoundError:
        raise CustomRootsError(CustomRootsReason.MISSING_PATH) from None
    except (OSError, RuntimeError, ValueError):
        raise CustomRootsError(CustomRootsReason.NONCANONICAL_PATH) from None
    if resolved != lexical:
        raise CustomRootsError(CustomRootsReason.NONCANONICAL_PATH)
    if not resolved.is_dir():
        raise CustomRootsError(CustomRootsReason.NOT_DIRECTORY)
    return resolved


def _read_custom_roots(target: Path | None = None) -> list[Path]:
    explicit_target = target is not None
    target = target if explicit_target else _custom_roots_file()
    if not explicit_target:
        try:
            runtime_paths.validate_store("file_roots")
        except runtime_paths.PathResolutionError:
            raise CustomRootsError(CustomRootsReason.UNREADABLE) from None
    if target.is_symlink():
        raise CustomRootsError(CustomRootsReason.UNREADABLE) from None
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError):
        raise CustomRootsError(CustomRootsReason.UNREADABLE) from None
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        raise CustomRootsError(CustomRootsReason.INVALID_JSON) from None
    if not isinstance(raw, list):
        raise CustomRootsError(CustomRootsReason.INVALID_SHAPE)
    if len(raw) > 100:
        raise CustomRootsError(CustomRootsReason.TOO_MANY_ENTRIES)
    roots: list[Path] = []
    for value in raw:
        path = _persisted_custom_root(value)
        if path not in roots:
            roots.append(path)
    return roots


def _configured_project_roots() -> list[Path]:
    if not next_profile.enabled() or next_profile.is_ephemeral():
        return []
    return [next_profile.configured_project_root()]


def _write_custom_roots(roots: list[Path]) -> None:
    target = _custom_roots_file()
    runtime_paths.validate_store("file_roots")  # R3-B:symlink 逃逸 fail-closed
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".file-roots.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump([str(path) for path in roots], handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def allowed_root_groups() -> dict[str, list[str]]:
    """按来源返回白名单根；同一路径只出现在优先级最高的一组。"""
    groups = {
        "system": _system_roots(),
        "projects": _registered_project_roots(),
        "custom": [*_configured_project_roots(), *_read_custom_roots()],
    }
    seen: set[Path] = set()
    result: dict[str, list[str]] = {}
    for name, roots in groups.items():
        values = []
        for root in roots:
            resolved = root.resolve(strict=False)
            if resolved in seen:
                continue
            seen.add(resolved)
            values.append(str(resolved))
        result[name] = values
    return result


def _load_roots() -> list[Path]:
    """允许访问的根目录(显式白名单,去重后返回)。"""
    groups = allowed_root_groups()
    return [Path(path) for paths in groups.values() for path in paths]


def _normalize_custom_root(rel: str, *, must_exist: bool) -> Path:
    if not rel or not rel.strip():
        raise ValueError("目录路径为空")
    path = Path(rel.strip()).expanduser()
    if not path.is_absolute():
        path = _HOME / path
    # 先用非严格解析做名单判断:即使目标不存在(如 macOS 无 /proc),
    # 宽泛/敏感目录也必须报对应的原因,而不是"不存在"
    check = path.resolve(strict=False)
    reason = _custom_root_policy(check)
    if reason is CustomRootsReason.BROAD_ROOT:
        raise ValueError("请选择具体目录，不能添加 /、/home 或整个用户 Home")
    if reason is CustomRootsReason.SENSITIVE_ROOT:
        raise ValueError(f"敏感或系统运行目录不能添加: {check}")
    try:
        path = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"目录不存在或无法解析: {path}") from exc
    if must_exist and not path.is_dir():
        raise ValueError(f"不是目录: {path}")
    return path


def add_custom_root(rel: str) -> dict[str, Any]:
    path = _normalize_custom_root(rel, must_exist=True)
    with _ROOTS_LOCK:
        existing = [*_system_roots(), *_registered_project_roots()]
        if path in (root.resolve(strict=False) for root in existing):
            return {"path": str(path), "added": False}
        custom = _read_custom_roots()
        if path in custom:
            return {"path": str(path), "added": False}
        if len(custom) >= 100:
            raise ValueError("自定义目录已达上限 100 个")
        custom.append(path)
        _write_custom_roots(custom)
    return {"path": str(path), "added": True}


def remove_custom_root(rel: str) -> dict[str, Any]:
    path = _normalize_custom_root(rel, must_exist=False)
    with _ROOTS_LOCK:
        custom = _read_custom_roots()
        if path not in custom:
            raise ValueError(f"不是自定义目录，不能移除: {path}")
        custom.remove(path)
        _write_custom_roots(custom)
    return {"path": str(path), "removed": True}


def reset_roots() -> None:
    """重置根缓存(根列表现由 _load_roots 实时计算,空操作,保留接口兼容)。"""
    pass


def _ancestry_crumbs(path: Path) -> list[dict[str, str]]:
    crumbs: list[dict[str, str]] = []
    current = path
    while True:
        crumbs.append({"name": current.name or str(current), "path": str(current)})
        parent = current.parent
        if parent == current:
            break
        current = parent
    crumbs.reverse()
    return crumbs


def browse_picker_dir(rel: str | None = None) -> dict[str, Any]:
    """列一层目录供添加工作区挑选。不走访问白名单；空路径 = Home。

    只返回目录。可以浏览 / 和 Home（与 3080 一致），敏感目录拒绝。
    真正「添加」仍走 add_custom_root，不能把 / 或整个 Home 加成工作区。
    """
    if not rel or not str(rel).strip():
        path = _HOME
    else:
        raw = str(rel).strip()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = _HOME / raw
        try:
            path = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"目录不存在或无法解析: {path}") from exc
    if _custom_root_policy(path) is CustomRootsReason.SENSITIVE_ROOT:
        raise ValueError(f"敏感或系统运行目录不能浏览: {path}")
    if not path.exists() or not path.is_dir():
        raise ValueError(f"不是目录: {path}")
    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        children = sorted(path.iterdir(), key=lambda item: item.name.lower())
    except PermissionError:
        raise ValueError(f"无权限: {path}") from None
    for child in children:
        if len(entries) >= BROWSE_MAX_ENTRIES:
            truncated = True
            break
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "path": str(child),
            "hidden": child.name.startswith("."),
        })
    return {
        "path": str(path),
        "home": str(_HOME),
        "crumbs": _ancestry_crumbs(path),
        "entries": entries,
        "truncated": truncated,
    }


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


def _trusted_relative_parts(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str):
        raise TrustedRootError("invalid_relative_path")
    if relative:
        if (
            relative.startswith(("/", "~"))
            or "\\" in relative
            or any(ord(char) < 32 or ord(char) == 127 for char in relative)
        ):
            raise TrustedRootError("invalid_relative_path")
        parts = tuple(relative.split("/"))
        if any(not part or part in {".", ".."} or part.startswith("~") for part in parts):
            raise TrustedRootError("invalid_relative_path")
    else:
        parts = ()
    return parts


def _close_trusted_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        # POSIX close errors leave ownership indeterminate; never retry this fd.
        raise TrustedRootError("local_files_unavailable") from None


def _transfer_trusted_fd(parent_fd: int, child_fd: int) -> int:
    try:
        os.close(parent_fd)
    except OSError:
        try:
            os.close(child_fd)
        except OSError:
            pass
        raise TrustedRootError("local_files_unavailable") from None
    return child_fd


def _open_trusted_root(root: str | Path) -> tuple[int, Path]:
    lexical_root = Path(root)
    if not lexical_root.is_absolute() or ".." in lexical_root.parts:
        raise TrustedRootError("local_files_unavailable")
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    if (
        any(not hasattr(os, name) for name in required)
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise TrustedRootError("local_files_unavailable")
    flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        current = os.open("/", flags)
    except OSError:
        raise TrustedRootError("local_files_unavailable") from None
    for part in lexical_root.parts[1:]:
        try:
            child = os.open(part, flags, dir_fd=current)
        except OSError:
            _close_trusted_fd(current)
            raise TrustedRootError("local_files_unavailable") from None
        current = _transfer_trusted_fd(current, child)
    return current, lexical_root


def _relative_open_error(parent_fd: int, name: str, exc: OSError) -> TrustedRootError:
    if exc.errno == errno.ELOOP:
        return TrustedRootError("invalid_relative_path")
    if exc.errno == errno.ENOTDIR:
        try:
            mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
        except OSError:
            return TrustedRootError("file_not_found")
        if stat.S_ISLNK(mode):
            return TrustedRootError("invalid_relative_path")
        return TrustedRootError("file_not_found")
    if exc.errno in {errno.ENOENT, errno.ENXIO}:
        return TrustedRootError("file_not_found")
    return TrustedRootError("local_files_unavailable")


def _open_from_bound_root(
    root_fd: int, parts: tuple[str, ...], *, directory: bool = False,
) -> int:
    try:
        current = os.dup(root_fd)
    except OSError:
        raise TrustedRootError("local_files_unavailable") from None
    try:
        for index, part in enumerate(parts):
            flags = (
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                | getattr(os, "O_CLOEXEC", 0)
            )
            if index < len(parts) - 1 or directory:
                flags |= os.O_DIRECTORY
            try:
                child = os.open(part, flags, dir_fd=current)
            except OSError as exc:
                mapped = _relative_open_error(current, part, exc)
                _close_trusted_fd(current)
                raise mapped from None
            current = _transfer_trusted_fd(current, child)
        return current
    except TrustedRootError:
        raise


def _open_trusted_target(root: str | Path, relative: str) -> tuple[int, Path]:
    parts = _trusted_relative_parts(relative)
    root_fd, lexical_root = _open_trusted_root(root)
    try:
        target_fd = _open_from_bound_root(root_fd, parts)
    except TrustedRootError:
        _close_trusted_fd(root_fd)
        raise
    return (
        _transfer_trusted_fd(root_fd, target_fd),
        lexical_root.joinpath(*parts),
    )


def _fd_is_text(fd: int, name: str) -> bool:
    path = Path(name)
    if path.name in {".gitignore", ".dockerignore", ".env", ".editorconfig",
                     "Makefile", "Dockerfile", "Gemfile", "Rakefile"}:
        return True
    ext = path.suffix.lower()
    if ext in BINARY_EXT:
        return False
    if ext in TEXT_EXT:
        return True
    try:
        os.pread(fd, 2048, 0).decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


def _entry_modifiable(parent_fd: int, name: str, st: os.stat_result) -> bool:
    if st.st_size > MAX_EDIT_SIZE:
        return False
    path = Path(name)
    if path.name in {".gitignore", ".dockerignore", ".env", ".editorconfig",
                     "Makefile", "Dockerfile", "Gemfile", "Rakefile"}:
        return True
    ext = path.suffix.lower()
    if ext in BINARY_EXT:
        return False
    if ext in TEXT_EXT:
        return True
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM}:
            return False
        if exc.errno == errno.ELOOP:
            return False
        raise TrustedRootError("local_files_unavailable") from None
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
            raise TrustedRootError("local_files_unavailable")
        return stat.S_ISREG(opened.st_mode) and _fd_is_text(fd, name)
    finally:
        _close_trusted_fd(fd)


def _bound_directory_entries(
    fd: int, *, skip_git_prefix: bool = False,
) -> list[tuple[str, str, os.stat_result]]:
    try:
        names = os.listdir(fd)
    except OSError:
        raise TrustedRootError("local_files_unavailable") from None
    entries: list[tuple[str, str, os.stat_result]] = []
    for name in names:
        if skip_git_prefix and name.startswith(".git"):
            continue
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError:
            raise TrustedRootError("local_files_unavailable") from None
        if stat.S_ISDIR(st.st_mode):
            entries.append((name, "dir", st))
        elif stat.S_ISREG(st.st_mode):
            entries.append((name, "file", st))
    return sorted(entries, key=lambda item: (item[1] != "dir", item[0].casefold()))


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
    return _list_dir_path(_resolve(rel))


def list_dir_from_trusted_root(root: str | Path, relative: str) -> dict[str, Any]:
    """List from a Registry-authorized root through a bound descriptor."""
    fd, path = _open_trusted_target(root, relative)
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode):
            st = os.fstat(fd)
            return {
                "path": str(path),
                "type": "file",
                "entries": [{
                    "name": path.name,
                    "type": "file",
                    "size": st.st_size,
                    "modifiable": (
                        _fd_is_text(fd, path.name) and st.st_size <= MAX_EDIT_SIZE
                    ),
                    "ext": path.suffix.lower().lstrip("."),
                }],
            }
        if not stat.S_ISDIR(mode):
            raise TrustedRootError("file_not_found")
        entries = [{
            "name": name,
            "type": kind,
            "size": st.st_size if kind == "file" else 0,
            "modifiable": kind == "file" and _entry_modifiable(fd, name, st),
            "ext": Path(name).suffix.lower().lstrip("."),
        } for name, kind, st in _bound_directory_entries(fd, skip_git_prefix=True)]
        return {"path": str(path), "entries": entries}
    except TrustedRootError:
        raise
    except OSError:
        raise TrustedRootError("local_files_unavailable") from None
    finally:
        _close_trusted_fd(fd)


def _list_dir_path(path: Path) -> dict[str, Any]:
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
    return _search_files_path(_resolve(rel), query, limit)


def search_files_from_trusted_root(
    root: str | Path, relative: str, query: str, limit: int = 100,
) -> dict[str, Any]:
    """Search a Registry-authorized tree without resolving descriptor paths."""
    query = query.strip()
    if not query or len(query) > 128 or not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise TrustedRootError("local_files_unavailable")
    parts = _trusted_relative_parts(relative)
    root_fd, root_path = _open_trusted_root(root)
    try:
        start_fd = _open_from_bound_root(root_fd, parts, directory=True)
        _close_trusted_fd(start_fd)
        path = root_path.joinpath(*parts)
        needle = query.casefold()
        results: list[dict[str, Any]] = []
        scanned = 0
        truncated = False
        pending: list[tuple[str, ...]] = [parts]
        while pending:
            current_parts = pending.pop()
            directory_fd = _open_from_bound_root(root_fd, current_parts, directory=True)
            try:
                child_dirs: list[tuple[str, ...]] = []
                for name, kind, st in _bound_directory_entries(directory_fd):
                    if kind == "dir" and name in SEARCH_SKIP_DIRS:
                        continue
                    scanned += 1
                    if scanned > MAX_SEARCH_ENTRIES:
                        truncated = True
                        break
                    item_parts = (*current_parts, name)
                    if needle in name.casefold():
                        relative_parts = item_parts[len(parts):]
                        results.append({
                            "name": name,
                            "path": str(root_path.joinpath(*item_parts)),
                            "relative": "/".join(relative_parts),
                            "type": kind,
                            "size": st.st_size if kind == "file" else 0,
                            "modifiable": (
                                kind == "file"
                                and _entry_modifiable(directory_fd, name, st)
                            ),
                            "ext": Path(name).suffix.lower().lstrip("."),
                        })
                        if len(results) > limit:
                            results.pop()
                            truncated = True
                            break
                    if kind == "dir":
                        child_dirs.append(item_parts)
            finally:
                _close_trusted_fd(directory_fd)
            if truncated:
                break
            pending.extend(reversed(child_dirs))
        return {
            "path": str(path),
            "query": query,
            "results": results,
            "truncated": truncated,
        }
    except TrustedRootError:
        raise
    except OSError:
        raise TrustedRootError("local_files_unavailable") from None
    finally:
        _close_trusted_fd(root_fd)


def _search_files_path(root: Path, query: str, limit: int = 100) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise ValueError("搜索关键词为空")
    if len(query) > 128:
        raise ValueError("搜索关键词过长(最多 128 字符)")
    if not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise ValueError(f"搜索结果范围必须为 1-{MAX_SEARCH_RESULTS}")

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


def confine_to_root(root: Path, rel: str | None) -> Path:
    """把请求路径限制在会话/项目目录内，不走全局白名单。"""
    base = root.resolve(strict=False)
    if not base.is_dir():
        raise ValueError("会话目录不存在")
    raw = (rel or "").strip()
    if not raw or raw.rstrip("/") == str(base).rstrip("/"):
        return base
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / raw
    try:
        target = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"路径无法解析: {path}") from exc
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError("路径不在会话目录内") from exc
    return target


def list_under_root(root: Path, rel: str | None = None) -> dict[str, Any]:
    return _list_dir_path(confine_to_root(root, rel))


def read_under_root(root: Path, rel: str) -> dict[str, Any]:
    return _read_file_path(confine_to_root(root, rel))


def search_under_root(root: Path, query: str, limit: int = 100) -> dict[str, Any]:
    return _search_files_path(confine_to_root(root, None), query, limit)


def read_file(rel: str) -> dict[str, Any]:
    """读文件内容。文本返回 text,二进制返回 {binary:true, size}。"""
    return _read_file_path(_resolve(rel))


def read_file_from_trusted_root(root: str | Path, relative: str) -> dict[str, Any]:
    """Read a Registry-authorized file through its bound descriptor."""
    fd, path = _open_trusted_target(root, relative)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TrustedRootError("file_not_found")
        if st.st_size > 10 * MAX_EDIT_SIZE:
            raise TrustedRootError("local_files_unavailable")
        if not _fd_is_text(fd, path.name):
            return {
                "path": str(path), "binary": True, "size": st.st_size,
                "modifiable": False,
            }
        data = bytearray()
        while len(data) <= 10 * MAX_EDIT_SIZE:
            chunk = os.read(fd, min(64 * 1024, 10 * MAX_EDIT_SIZE + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > 10 * MAX_EDIT_SIZE:
            raise TrustedRootError("local_files_unavailable")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise TrustedRootError("local_files_unavailable") from None
        return {
            "path": str(path), "text": text, "size": st.st_size,
            "binary": False, "modifiable": st.st_size <= MAX_EDIT_SIZE,
        }
    except TrustedRootError:
        raise
    except OSError:
        raise TrustedRootError("local_files_unavailable") from None
    finally:
        _close_trusted_fd(fd)


def _read_file_path(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"不是文件: {path}")
    st = path.stat()
    if st.st_size > 10 * MAX_EDIT_SIZE:
        raise ValueError(f"文件过大({st.st_size} bytes)")
    if _is_text(path):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"文件不是有效的 UTF-8 文本: {path}") from exc
        return {
            "path": str(path), "text": text,
            "size": st.st_size, "binary": False, "modifiable": st.st_size <= MAX_EDIT_SIZE,
        }
    return {"path": str(path), "binary": True, "size": st.st_size, "modifiable": False}


def download_path(rel: str) -> Path:
    """返回校验后的下载文件路径。"""
    path = _resolve(rel)
    if not path.is_file():
        raise ValueError(f"不是文件: {path}")
    return path


def preview_path(rel: str) -> Path:
    """返回校验后的内联预览文件路径(限 PREVIEW_EXT 媒体类型)。"""
    path = download_path(rel)
    if path.suffix.lower() not in PREVIEW_EXT:
        raise ValueError("该文件类型不支持内联预览，请下载查看")
    return path


def zip_dir(rel: str) -> Path:
    """把白名单内目录打包为临时 zip 并返回路径(调用方负责删除)。

    跳过 .git 内部与符号链接;文件数/总大小超限抛 ValueError。
    """
    import zipfile

    path = _resolve(rel)
    if not path.is_dir():
        raise ValueError(f"不是目录: {path}")
    fd, tmp_name = tempfile.mkstemp(prefix=".dir-dl-", suffix=".zip")
    total = 0
    count = 0
    try:
        with os.fdopen(fd, "wb") as raw:
            with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in sorted(path.rglob("*")):
                    rel_parts = item.relative_to(path).parts
                    if ".git" in rel_parts:
                        continue
                    if item.is_symlink() or not item.is_file():
                        continue
                    count += 1
                    if count > MAX_ZIP_FILES:
                        raise ValueError(f"目录文件数超过 {MAX_ZIP_FILES} 上限")
                    total += item.stat().st_size
                    if total > MAX_ZIP_SIZE:
                        raise ValueError("目录总大小超过 500MB 上限")
                    zf.write(item, arcname=str(Path(path.name, *rel_parts)))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return Path(tmp_name)


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
