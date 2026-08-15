"""Independent Git Checkout. Never writes RepoLocation or mutates source."""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class CheckoutError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise CheckoutError(code)


@dataclass(frozen=True)
class SourceExact:
    head: str
    tree: str
    clean: bool


@dataclass(frozen=True)
class CheckoutExact:
    path: str
    head: str
    tree: str
    ref_kind: str


class GitCheckoutProvider:
    def inspect_source(self, source_path: Path) -> SourceExact:
        source = _absolute(source_path)
        if not _is_git(source):
            _fail("source_not_git")
        if _porcelain(source):
            _fail("source_dirty")
        return SourceExact(_head(source), _tree(source), True)

    def create_checkout(
        self, *, source_path: Path, checkout_path: Path, expected_head: str,
    ) -> CheckoutExact:
        source = _absolute(source_path)
        dest = _absolute(checkout_path)
        if dest == source:
            _fail("checkout_conflict")
        if dest.exists():
            _fail("checkout_conflict")
        before = self.inspect_source(source)
        if before.head != expected_head:
            _fail("checkout_conflict")
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            _run(source, "worktree", "add", "--detach", str(dest), expected_head)
        except CheckoutError:
            _discard_unregistered(source, dest)
            raise
        try:
            created = self.verify_checkout(
                checkout_path=dest, source_path=source,
                expected_head=before.head, expected_tree=before.tree,
            )
            after = self.inspect_source(source)
            if (after.head, after.tree) != (before.head, before.tree):
                _fail("checkout_conflict")
            return created
        except CheckoutError:
            _discard_unregistered(source, dest)
            raise

    def verify_checkout(
        self, *, checkout_path: Path, source_path: Path, expected_head: str,
        expected_tree: str,
    ) -> CheckoutExact:
        source = _absolute(source_path)
        dest = _absolute(checkout_path)
        if dest == source or not dest.exists():
            _fail("checkout_conflict")
        if not _is_git(dest):
            _fail("source_not_git")
        if _head(dest) != expected_head or _tree(dest) != expected_tree:
            _fail("checkout_conflict")
        if _porcelain(dest):
            _fail("source_dirty")
        return CheckoutExact(str(dest), expected_head, expected_tree, "detached")


def _absolute(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        _fail("store_unsafe")
    return path


def _run(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, check=False, capture_output=True,
            text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        _fail("store_write_failed")
    if result.returncode != 0:
        _fail("checkout_conflict")
    return (result.stdout or "").strip()


def _is_git(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path, check=False, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _head(path: Path) -> str:
    return _run(path, "rev-parse", "HEAD")


def _tree(path: Path) -> str:
    return _run(path, "rev-parse", "HEAD^{tree}")


def _porcelain(path: Path) -> str:
    return _run(path, "status", "--porcelain")


def _discard_unregistered(source: Path, dest: Path) -> None:
    if dest.exists() and dest != source:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(dest)],
            cwd=source, check=False, capture_output=True, text=True, timeout=15,
        )
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=source, check=False, capture_output=True, text=True, timeout=15,
        )
