"""git 变更卡片：collect_git_card 采集 + 账本 git 字段校验。"""
from __future__ import annotations

import subprocess

import pytest

from agent_cockpit import chat_ledger
from agent_cockpit import git_card
from agent_cockpit import runtime_paths


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("COCKPIT_DATA_DIR", str(data))
    runtime_paths.reset_cache()
    yield tmp_path
    runtime_paths.reset_cache()


def _git(repo, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    return repo


def test_collect_git_card_clean_repo_returns_none(git_repo):
    assert git_card.collect_git_card(str(git_repo)) is None


def test_collect_git_card_with_tracked_changes(git_repo):
    (git_repo / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    (git_repo / "b.txt").write_text("new\n", encoding="utf-8")
    card = git_card.collect_git_card(str(git_repo))
    assert card is not None
    assert card["files"] == 2
    assert "a.txt" in card["stat"]


def test_collect_git_card_untracked_only_uses_porcelain(git_repo):
    (git_repo / "c.txt").write_text("new\n", encoding="utf-8")
    card = git_card.collect_git_card(str(git_repo))
    assert card is not None
    assert card["files"] == 1
    assert "c.txt" in card["stat"]


def test_collect_git_card_non_git_dir_returns_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "x.txt").write_text("x\n", encoding="utf-8")
    assert git_card.collect_git_card(str(plain)) is None


def test_collect_git_card_bad_input_returns_none():
    assert git_card.collect_git_card(None) is None
    assert git_card.collect_git_card("") is None
    assert git_card.collect_git_card("/nonexistent/path/nope") is None


def test_collect_git_card_truncates_long_stat(git_repo, monkeypatch):
    def fake_run(_cwd, *args):
        if "status" in args:
            return " M a.txt\n"
        if args[:2] == ("rev-parse", "--is-inside-work-tree"):
            return "true\n"
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return "main\n"
        return "x" * 9999
    monkeypatch.setattr(git_card, "_run_git", fake_run)
    card = git_card.collect_git_card(str(git_repo))
    assert card is not None
    assert len(card["stat"]) == 4000


def test_collect_workspace_git_reports_branch_and_clean(git_repo):
    summary = git_card.collect_workspace_git(str(git_repo))
    assert summary["repo"] is True
    assert summary["files"] == 0
    assert summary["stat"] == ""
    assert summary["branch"]
    assert summary["branch"] in summary["branches"]
    assert "diff" not in summary


def test_collect_workspace_git_dirty_includes_optional_diff(git_repo):
    (git_repo / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    summary = git_card.collect_workspace_git(str(git_repo), include_diff=True)
    assert summary["repo"] is True
    assert summary["files"] == 1
    assert "a.txt" in summary["stat"]
    assert "hello" in summary["diff"] or "+world" in summary["diff"]


def test_collect_workspace_git_non_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    summary = git_card.collect_workspace_git(str(plain), include_diff=True)
    assert summary == {
        "repo": False, "branch": "", "branches": [], "files": 0, "stat": "", "diff": "",
    }


def test_ledger_message_git_roundtrip(isolated_ledger):
    row = chat_ledger.append_message(
        "chat-1", kind="agent", sender="BrownDesert", text="改完了",
        to=["human"], git={"files": 3, "stat": " a.txt | 2 +-"},
    )
    assert row["git"] == {"files": 3, "stat": " a.txt | 2 +-"}
    listed = chat_ledger.list_messages("chat-1")
    assert listed[0]["git"] == {"files": 3, "stat": " a.txt | 2 +-"}


def test_ledger_message_without_git_omits_field(isolated_ledger):
    row = chat_ledger.append_message(
        "chat-1", kind="agent", sender="BrownDesert", text="改完了", to=["human"],
    )
    assert "git" not in row


@pytest.mark.parametrize("card", [
    {"files": -1, "stat": "x"},
    {"files": 1, "stat": "x" * 4097},
    {"files": "3", "stat": "x"},
    {"files": 1},
    "not-a-dict",
])
def test_ledger_message_rejects_invalid_git(isolated_ledger, card):
    with pytest.raises(ValueError):
        chat_ledger.append_message(
            "chat-1", kind="agent", sender="BrownDesert", text="改完了",
            to=["human"], git=card,
        )


def test_set_message_git_updates_and_clears(isolated_ledger):
    row = chat_ledger.append_message(
        "chat-1", kind="agent", sender="BrownDesert", text="改完了", to=["human"],
    )
    updated = chat_ledger.set_message_git(row["id"], {"files": 2, "stat": "s"})
    assert updated["git"] == {"files": 2, "stat": "s"}
    cleared = chat_ledger.set_message_git(row["id"], None)
    assert "git" not in cleared
    assert chat_ledger.set_message_git("msg_000000000000", {"files": 1, "stat": "s"}) is None
    with pytest.raises(ValueError):
        chat_ledger.set_message_git(row["id"], {"files": -1, "stat": "s"})
