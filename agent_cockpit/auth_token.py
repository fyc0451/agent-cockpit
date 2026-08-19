"""Strict private token-file loading shared by Next launch validation."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Mapping


TOKEN_FILE_NAME = "cockpit.token"
MIN_TOKEN_BYTES = 4
MAX_TOKEN_BYTES = 64
# 给人记的短密码：4–64 位字母数字或 _-
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{4,64}\Z")


class TokenFileError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def token_file_path(values: Mapping[str, str]) -> Path:
    return Path(values["COCKPIT_CONFIG_DIR"]) / TOKEN_FILE_NAME


def _token_file_signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def load_cockpit_token(values: Mapping[str, str]) -> str | None:
    path = token_file_path(values)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TokenFileError("token_file_unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
        or before.st_size < MIN_TOKEN_BYTES
        or before.st_size > MAX_TOKEN_BYTES + 1
    ):
        raise TokenFileError("token_file_unsafe")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise TokenFileError("token_file_unavailable") from exc
    try:
        opened = os.fstat(fd)
        if _token_file_signature(opened) != _token_file_signature(before):
            raise TokenFileError("token_file_unsafe")
        chunks: list[bytes] = []
        remaining = MAX_TOKEN_BYTES + 2
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise TokenFileError("token_file_unsafe")
        after = os.fstat(fd)
        if _token_file_signature(after) != _token_file_signature(opened):
            raise TokenFileError("token_file_unsafe")
        try:
            current = path.lstat()
        except OSError as exc:
            raise TokenFileError("token_file_unsafe") from exc
        if _token_file_signature(current) != _token_file_signature(after):
            raise TokenFileError("token_file_unsafe")
    except OSError as exc:
        raise TokenFileError("token_file_unavailable") from exc
    finally:
        os.close(fd)

    raw = b"".join(chunks)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        token = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TokenFileError("token_file_invalid") from exc
    if TOKEN_RE.fullmatch(token) is None:
        raise TokenFileError("token_file_invalid")
    return token
