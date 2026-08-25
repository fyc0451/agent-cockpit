"""为只读 Team Agent 生成最小、确定性的项目上下文包。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable


PACK_VERSION = 1
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_STATUS_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,32}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T[^\s]{1,64})?$")
_LEAD_STATUSES = frozenset({"working", "idle", "blocked", "done"})
_LEAD_REASONS = frozenset({
    "not_selected", "target_stopped", "target_ambiguous",
    "target_identity_changed", "unavailable",
})
_MEMORY_ROOTS = (
    "/mnt/d/Obsidian/agent-memory",
    "/Users/admin/Documents/ObsidianVault/agent-memory",
    r"D:\Obsidian\agent-memory",
)
_SECRET_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+",
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|"
    r"passwd|credential|authorization|cookie|client[_-]?secret)\s*[:=]\s*[^\s,;]+",
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,})\b",
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
    r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@",
))


def _project_key(workspace: Path) -> str | None:
    marker = workspace / ".agent-memory-project"
    if marker.exists():
        try:
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 256:
                values = []
            else:
                values = [
                    line.strip()
                    for line in marker.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
        except (OSError, UnicodeError):
            values = []
        if (
            len(values) == 1
            and _PROJECT_KEY_RE.fullmatch(values[0])
            and not any(pattern.search(values[0]) for pattern in _SECRET_PATTERNS)
        ):
            return values[0]
    return workspace.name if _PROJECT_KEY_RE.fullmatch(workspace.name) else None


def _git_summary(workspace: Path) -> dict[str, Any]:
    changes: dict[str, int] = {
        "staged": 0,
        "unstaged": 0,
        "conflicted": 0,
        "untracked": 0,
    }
    result: dict[str, Any] = {
        "available": True,
        "head": None,
        "dirty": False,
        "changes": changes,
    }
    try:
        completed = subprocess.run(
            [
                "git", "status", "--porcelain=v2", "--branch", "-z",
                "--untracked-files=normal", "--ignore-submodules=dirty",
            ],
            cwd=workspace,
            env={
                key: value for key, value in {
                    "PATH": os.environ.get("PATH"),
                    "HOME": os.environ.get("HOME"),
                    "SYSTEMROOT": os.environ.get("SYSTEMROOT"),
                    "GIT_OPTIONAL_LOCKS": "0",
                    "LC_ALL": "C",
                }.items() if value is not None
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "reason": "not_repository"}
    if completed.returncode != 0:
        return {"available": False, "reason": "not_repository"}

    records = completed.stdout.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if record.startswith(b"# branch.oid "):
            oid = record[len(b"# branch.oid "):].decode("ascii", "ignore")
            result["head"] = oid if re.fullmatch(r"[0-9a-f]{40,64}", oid) else None
            continue
        if record.startswith((b"1 ", b"2 ")):
            fields = record.split(b" ", 2)
            xy = fields[1] if len(fields) > 1 else b".."
            if xy[:1] != b".":
                changes["staged"] += 1
            if xy[1:2] != b".":
                changes["unstaged"] += 1
            if record.startswith(b"2 "):
                index += 1  # rename/copy 的第二个路径字段；内容永不返回
            continue
        if record.startswith(b"u "):
            changes["conflicted"] += 1
        elif record.startswith(b"? "):
            changes["untracked"] += 1
    result["dirty"] = any(changes.values())
    return result


def _memory_root(roots: Iterable[str | Path] | None) -> tuple[Path | None, str | None]:
    override = os.environ.get("AGENT_MEMORY_ROOT", "").strip()
    candidates = (
        list(roots)
        if roots is not None else ([override] if override else _MEMORY_ROOTS)
    )
    found: list[Path] = []
    for raw in candidates:
        try:
            root = Path(raw).expanduser().resolve()
            if (root / "README.md").is_file():
                found.append(root)
        except (OSError, ValueError):
            continue
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0], None
    return None, "memory_root_ambiguous" if unique else "memory_root_missing"


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return values
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip().strip("'\"")
    return {}


def _safe_item(value: str) -> str:
    compact = " ".join(value.replace("\x00", " ").split())[:512]
    unsafe_context = (
        re.match(
            r"^(?:\$|❯|➜|PS>|[A-Za-z]:\\>|user:|assistant:|system:|"
            r"developer:|boss:|用户[：:]|助手[：:]|系统[：:]|开发者[：:])",
            compact,
            re.I,
        )
        or re.search(
            r"(?:完整)?(?:终端|对话|聊天记录|消息正文|terminal|conversation)",
            compact,
            re.I,
        )
    )
    if unsafe_context or any(pattern.search(compact) for pattern in _SECRET_PATTERNS):
        return ""
    return compact


def _section_items(
    text: str, heading: str, *, numbered: bool,
) -> tuple[list[str], bool]:
    items: list[str] = []
    redacted = False
    in_section = False
    pattern = re.compile(r"^\s*\d+\.\s+(.+)$" if numbered else r"^\s*-\s+(.+)$")
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == heading
            continue
        if not in_section:
            continue
        match = pattern.match(line)
        if match:
            item = _safe_item(match.group(1))
            if item:
                items.append(item)
            else:
                redacted = True
        if len(items) == 8:
            break
    return items, redacted


def _handoff_summary(
    project_key: str | None,
    roots: Iterable[str | Path] | None,
) -> dict[str, Any]:
    if project_key is None:
        return {"available": False, "reason": "missing"}
    root, _reason = _memory_root(roots)
    if root is None:
        return {"available": False, "reason": "missing"}
    path = root / "handoff" / f"{project_key}.md"
    try:
        handoff_dir = root / "handoff"
        resolved = path.resolve()
        if (
            handoff_dir.is_symlink()
            or resolved.parent != handoff_dir
            or path.is_symlink()
            or not resolved.is_file()
            or resolved.stat().st_size > 65_536
        ):
            return {"available": False, "reason": "missing"}
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {"available": False, "reason": "missing"}
    meta = _frontmatter(text)
    if meta.get("project") not in {None, project_key}:
        return {"available": False, "reason": "missing"}
    status = meta.get("status")
    updated = meta.get("updated")
    safe_status = _safe_item(status or "")
    valid_status = (
        safe_status
        if safe_status and _SAFE_STATUS_RE.fullmatch(safe_status) else None
    )
    next_steps, next_redacted = _section_items(text, "## 下一步", numbered=True)
    blockers, blocker_redacted = _section_items(
        text, "## 阻塞条件", numbered=False,
    )
    return {
        "available": True,
        "status": valid_status,
        "updated": updated if updated and _DATE_RE.fullmatch(updated) else None,
        "blockers": blockers,
        "next": next_steps,
        "redacted": bool(
            next_redacted
            or blocker_redacted
            or (status and valid_status is None)
        ),
    }


def _lead_summary(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    configured = value is not None
    available = configured and source.get("available") is True
    status = source.get("status")
    reason = source.get("reason")
    result: dict[str, Any] = {"configured": configured, "available": available}
    if available and status in _LEAD_STATUSES:
        result["status"] = status
    elif configured and reason in _LEAD_REASONS:
        result["reason"] = reason
    return result


def build_context_pack(
    workspace: str | Path,
    *,
    development_lead: dict[str, Any] | None = None,
    memory_roots: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    """只从可信 workspace 元数据、Git 和同项目 handoff 构造固定结构。"""
    try:
        root = Path(workspace).expanduser().resolve()
        valid_root = root.is_dir()
    except (OSError, ValueError):
        root = Path("/")
        valid_root = False
    project_key = _project_key(root) if valid_root else None
    pack: dict[str, Any] = {
        "version": PACK_VERSION,
        "project": {"key": project_key},
        "git": (
            _git_summary(root)
            if valid_root else {"available": False, "reason": "not_repository"}
        ),
        "handoff": _handoff_summary(project_key, memory_roots),
        "development_lead": _lead_summary(development_lead),
    }
    canonical = json.dumps(
        pack, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    pack["fingerprint"] = hashlib.sha256(canonical).hexdigest()
    return pack
