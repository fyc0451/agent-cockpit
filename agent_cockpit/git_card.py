"""工作区 git 摘要：当前分支 + 未提交文件数 + stat / 可选 diff。

给文件页用，描述整个工作区相对当前分支的脏状态，不是某条结论的变更。
采集失败返回空摘要，不抛。
"""
from __future__ import annotations

import subprocess
from typing import Any

_STAT_LIMIT = 4000
_DIFF_LIMIT = 32_000


def _run_git(cwd: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def collect_workspace_git(cwd: str | None, *, include_diff: bool = False) -> dict[str, Any]:
    """工作区级摘要。repo=False 表示不是 git 仓库或读失败。"""
    empty: dict[str, Any] = {
        "repo": False,
        "branch": "",
        "branches": [],
        "files": 0,
        "stat": "",
    }
    if include_diff:
        empty["diff"] = ""
    if not isinstance(cwd, str) or not cwd.strip():
        return empty
    root = cwd.strip()
    inside = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if not inside or inside.strip() != "true":
        return empty
    branch = (_run_git(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    if branch == "HEAD":
        short = (_run_git(root, "rev-parse", "--short", "HEAD") or "").strip()
        branch = f"detached {short}".strip()
    listed = _run_git(root, "branch", "--format=%(refname:short)") or ""
    branches = [line.strip() for line in listed.splitlines() if line.strip()]
    if branch and branch not in branches and not branch.startswith("detached "):
        branches.insert(0, branch)
    porcelain = _run_git(root, "status", "--porcelain") or ""
    files = len([line for line in porcelain.splitlines() if line.strip()])
    stat = (_run_git(root, "diff", "--stat", "HEAD") or "").strip()
    if not stat and porcelain.strip():
        # 全是未跟踪文件时 diff --stat HEAD 为空，退回 porcelain。
        stat = porcelain.strip()
    if len(stat) > _STAT_LIMIT:
        stat = stat[:_STAT_LIMIT]
    out: dict[str, Any] = {
        "repo": True,
        "branch": branch,
        "branches": branches,
        "files": files,
        "stat": stat,
    }
    if include_diff:
        patch = (_run_git(root, "diff", "HEAD") or "").strip()
        if not patch and porcelain.strip():
            patch = porcelain.strip()
        if len(patch) > _DIFF_LIMIT:
            patch = patch[:_DIFF_LIMIT] + "\n…（已截断）"
        out["diff"] = patch
    return out


def collect_git_card(cwd: str | None) -> dict[str, Any] | None:
    """旧卡片形状：有未提交改动时返回 {files, stat}。"""
    summary = collect_workspace_git(cwd)
    if not summary["repo"] or summary["files"] <= 0:
        return None
    return {"files": summary["files"], "stat": summary["stat"]}
