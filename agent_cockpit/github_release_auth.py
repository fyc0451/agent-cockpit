"""Load the optional credential used for private GitHub Releases."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path


TOKEN_FILE_ENV = "COCKPIT_GITHUB_TOKEN_FILE"
TOKEN_FILE_NAME = "github-release.token"
MAX_TOKEN_BYTES = 512
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{20,512}\Z")


class GitHubReleaseAuthError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def token_file_path(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    source = os.environ if environ is None else environ
    configured = source.get(TOKEN_FILE_ENV)
    if configured is not None:
        path = Path(configured)
        if not configured or not path.is_absolute():
            raise GitHubReleaseAuthError("github_token_path_invalid")
        return path
    root = Path.home() if home is None else home
    return root / ".config" / "agent-cockpit" / TOKEN_FILE_NAME


def load_github_release_token(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> str | None:
    path = token_file_path(environ=environ, home=home)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GitHubReleaseAuthError("github_token_unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or before.st_size < 20
        or before.st_size > MAX_TOKEN_BYTES + 1
    ):
        raise GitHubReleaseAuthError("github_token_unsafe")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise GitHubReleaseAuthError("github_token_unavailable") from exc
    try:
        opened = os.fstat(fd)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_mode != before.st_mode
            or opened.st_uid != before.st_uid
            or opened.st_nlink != before.st_nlink
            or opened.st_size != before.st_size
        ):
            raise GitHubReleaseAuthError("github_token_unsafe")
        raw = os.read(fd, MAX_TOKEN_BYTES + 2)
        if os.read(fd, 1):
            raise GitHubReleaseAuthError("github_token_unsafe")
        after = os.fstat(fd)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise GitHubReleaseAuthError("github_token_unsafe")
    finally:
        os.close(fd)

    if raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        token = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GitHubReleaseAuthError("github_token_invalid") from exc
    if _TOKEN_RE.fullmatch(token) is None:
        raise GitHubReleaseAuthError("github_token_invalid")
    return token


__all__ = [
    "GitHubReleaseAuthError",
    "MAX_TOKEN_BYTES",
    "TOKEN_FILE_ENV",
    "TOKEN_FILE_NAME",
    "load_github_release_token",
    "token_file_path",
]
