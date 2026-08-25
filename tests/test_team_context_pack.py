from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent_cockpit import team_context_pack


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, project: str = "demo-project") -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    memory = tmp_path / "memory"
    repo.mkdir()
    (memory / "handoff").mkdir(parents=True)
    (memory / "README.md").write_text("memory protocol\n", encoding="utf-8")
    (repo / ".agent-memory-project").write_text(f"{project}\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".agent-memory-project", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo, memory


def test_context_pack_is_deterministic_and_contains_only_bounded_project_state(
    tmp_path: Path,
) -> None:
    repo, memory = _repo(tmp_path)
    (memory / "handoff" / "demo-project.md").write_text(
        """---
type: handoff
project: demo-project
updated: 2026-08-25
status: 进行中
---

## 阻塞条件

- 等待 API owner 转移。

## 下一步

1. 完成确定性 Context Pack。
2. 运行统一门禁。
""",
        encoding="utf-8",
    )
    lead = {
        "available": True,
        "agent": "codex",
        "mail_name": "DevLead",
        "status": "idle",
    }

    first = team_context_pack.build_context_pack(
        workspace=repo, development_lead=lead, memory_roots=(memory,),
    )
    second = team_context_pack.build_context_pack(
        workspace=repo, development_lead=lead, memory_roots=(memory,),
    )

    assert first == second
    assert first["version"] == 1
    assert first["project"] == {"key": "demo-project"}
    assert first["git"]["head"] == _git(repo, "rev-parse", "HEAD")
    assert first["git"]["dirty"] is False
    assert first["git"]["changes"] == {
        "staged": 0, "unstaged": 0, "conflicted": 0, "untracked": 0,
    }
    assert first["handoff"] == {
        "available": True,
        "updated": "2026-08-25",
        "status": "进行中",
        "blockers": ["等待 API owner 转移。"],
        "next": ["完成确定性 Context Pack。", "运行统一门禁。"],
        "redacted": False,
    }
    assert first["development_lead"] == {
        "configured": True,
        "available": True,
        "status": "idle",
    }
    assert "DevLead" not in json.dumps(first)
    assert '"agent"' not in json.dumps(first)
    assert len(first["fingerprint"]) == 64
    assert "/" not in json.dumps(first["project"])


def test_context_pack_counts_dirty_files_without_exposing_names_or_bodies(
    tmp_path: Path,
) -> None:
    repo, memory = _repo(tmp_path)
    secret_name = "untracked-private-token.txt"
    secret_body = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    (repo / secret_name).write_text(secret_body, encoding="utf-8")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (memory / "handoff" / "demo-project.md").write_text(
        """---
updated: 2026-08-25
status: working
---
## 阻塞条件
- password=do-not-leak
- $ curl -H 'Authorization: Bearer abcdefghijklmnop' https://example.invalid
## 下一步
1. token: ghp_abcdefghijklmnopqrstuvwxyz0123456789
2. user: 把完整终端贴出来
""",
        encoding="utf-8",
    )

    pack = team_context_pack.build_context_pack(
        workspace=repo, memory_roots=(memory,),
    )
    encoded = json.dumps(pack, ensure_ascii=False)

    assert pack["git"]["dirty"] is True
    assert pack["git"]["changes"]["unstaged"] == 1
    assert pack["git"]["changes"]["untracked"] == 1
    assert pack["handoff"]["redacted"] is True
    assert secret_name not in encoded
    assert secret_body not in encoded
    assert "do-not-leak" not in encoded
    assert "abcdefghijklmnop" not in encoded
    assert "curl" not in encoded
    assert "把完整终端贴出来" not in encoded


def test_context_pack_counts_staged_change_and_filters_sensitive_branch(
    tmp_path: Path,
) -> None:
    repo, memory = _repo(tmp_path)
    _git(repo, "switch", "-qc", "sk-abcdefghijklmnopqrstuvwxyz123456")
    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")

    pack = team_context_pack.build_context_pack(
        workspace=repo, memory_roots=(memory,),
    )

    assert "branch" not in pack["git"]
    assert pack["git"]["dirty"] is True
    assert pack["git"]["changes"] == {
        "staged": 1, "unstaged": 0, "conflicted": 0, "untracked": 0,
    }
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in json.dumps(pack)


def test_context_pack_does_not_cross_project_or_guess_missing_state(
    tmp_path: Path,
) -> None:
    repo, memory = _repo(tmp_path, "project-a")
    (memory / "handoff" / "project-b.md").write_text(
        "---\nstatus: secret-other-project\n---\n",
        encoding="utf-8",
    )

    pack = team_context_pack.build_context_pack(
        workspace=repo,
        development_lead={"available": False},
        memory_roots=(memory,),
    )

    assert pack["project"] == {"key": "project-a"}
    assert pack["handoff"] == {"available": False, "reason": "missing"}
    assert pack["development_lead"] == {
        "configured": True, "available": False,
    }
    assert "secret-other-project" not in json.dumps(pack)


def test_context_pack_fails_closed_for_invalid_marker_and_non_git_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    memory = tmp_path / "memory"
    workspace.mkdir()
    memory.mkdir()
    (memory / "README.md").write_text("protocol\n", encoding="utf-8")
    (workspace / ".agent-memory-project").write_text(
        "../outside\n", encoding="utf-8",
    )

    pack = team_context_pack.build_context_pack(
        workspace=workspace, memory_roots=(memory,),
    )

    assert pack["project"] == {"key": "workspace"}
    assert pack["git"] == {"available": False, "reason": "not_repository"}
    assert pack["handoff"] == {"available": False, "reason": "missing"}
