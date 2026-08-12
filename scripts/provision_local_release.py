#!/usr/bin/env python3
"""Provision local private-Release credentials without printing secrets."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_cockpit.github_release_auth import (  # noqa: E402
    MAX_TOKEN_BYTES,
    load_github_release_token,
)


CONFIG_DIR = Path.home() / ".config" / "agent-cockpit"
PRIVATE_KEY = CONFIG_DIR / "server-release-ed25519.key"
PUBLIC_KEY = CONFIG_DIR / "server-release-ed25519.pub"
TOKEN_FILE = CONFIG_DIR / "github-release.token"


def _require_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
    ):
        raise SystemExit("release_config_directory_unsafe")


def _atomic_write(path: Path, payload: bytes, *, replace: bool) -> None:
    if os.path.lexists(path) and not replace:
        if _read_private_file(path, size=len(payload)) == payload:
            return
        raise SystemExit(f"{path.name}_already_exists")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_private_file(path: Path, *, size: int) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SystemExit(f"{path.name}_unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_size != size
    ):
        raise SystemExit(f"{path.name}_unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SystemExit(f"{path.name}_unavailable") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise SystemExit(f"{path.name}_unsafe")
        payload = os.read(fd, size + 1)
        if len(payload) != size or os.read(fd, 1):
            raise SystemExit(f"{path.name}_unsafe")
        return payload
    finally:
        os.close(fd)


def _load_or_create_key() -> tuple[bytes, bytes]:
    if os.path.lexists(PRIVATE_KEY):
        private_bytes = _read_private_file(PRIVATE_KEY, size=32)
        key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    else:
        key = Ed25519PrivateKey.generate()
        private_bytes = key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        _atomic_write(PRIVATE_KEY, private_bytes, replace=False)
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    _atomic_write(PUBLIC_KEY, public_bytes, replace=False)
    _read_private_file(PRIVATE_KEY, size=32)
    _read_private_file(PUBLIC_KEY, size=32)
    return private_bytes, public_bytes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-token-stdin", action="store_true")
    parser.add_argument("--replace-github-token", action="store_true")
    args = parser.parse_args()
    if args.replace_github_token and not args.github_token_stdin:
        parser.error("--replace-github-token requires --github-token-stdin")

    _require_private_directory(CONFIG_DIR)
    if args.github_token_stdin:
        token = sys.stdin.buffer.read(MAX_TOKEN_BYTES + 2)
        if token.endswith(b"\n"):
            token = token[:-1]
        if not token or len(token) > MAX_TOKEN_BYTES or b"\n" in token or b"\r" in token:
            raise SystemExit("github_token_invalid")
        _atomic_write(
            TOKEN_FILE, token + b"\n", replace=args.replace_github_token,
        )
    if load_github_release_token(home=Path.home()) is None:
        raise SystemExit("github_token_missing")
    _private, public = _load_or_create_key()
    print(f"LOCAL_RELEASE_READY public_key_sha256={hashlib.sha256(public).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
