from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_cockpit import git_checkout_provider as checkout_mod


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "dev@example.com")
    _git(path, "config", "user.name", "dev")
    (path / "README").write_text("hello\n")
    _git(path, "add", "README")
    _git(path, "commit", "-m", "init")
    return path


def test_inspect_rejects_non_git_and_dirty(tmp_path: Path) -> None:
    provider = checkout_mod.GitCheckoutProvider()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(checkout_mod.CheckoutError) as missing:
        provider.inspect_source(empty)
    assert missing.value.code == "source_not_git"
    source = _repo(tmp_path / "src")
    (source / "README").write_text("dirty\n")
    with pytest.raises(checkout_mod.CheckoutError) as dirty:
        provider.inspect_source(source)
    assert dirty.value.code == "source_dirty"


def test_create_checkout_is_independent_and_failure_is_unregistered(tmp_path: Path) -> None:
    provider = checkout_mod.GitCheckoutProvider()
    source = _repo(tmp_path / "src")
    exact = provider.inspect_source(source)
    dest = tmp_path / "worktrees" / "managed-checkouts" / "chk_one"
    created = provider.create_checkout(
        source_path=source, checkout_path=dest, expected_head=exact.head,
    )
    assert Path(created.path) == dest
    assert dest != source
    assert created.head == exact.head
    assert created.tree == exact.tree
    assert created.ref_kind == "detached"
    assert provider.inspect_source(source) == exact
    assert (source / "README").read_text() == "hello\n"
    listed = _git(source, "worktree", "list")
    assert str(dest) in listed
    assert str(source) in listed

    clash = tmp_path / "worktrees" / "managed-checkouts" / "chk_clash"
    clash.mkdir(parents=True)
    with pytest.raises(checkout_mod.CheckoutError) as conflict:
        provider.create_checkout(
            source_path=source, checkout_path=clash, expected_head=exact.head,
        )
    assert conflict.value.code == "checkout_conflict"
    after = _git(source, "worktree", "list")
    assert str(clash) not in after or clash.is_dir() and not (clash / ".git").exists()
    assert provider.inspect_source(source) == exact
