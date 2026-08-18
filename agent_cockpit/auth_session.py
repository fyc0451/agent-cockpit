"""Server-side login session registry.

P1 修复：logout 必须吊销服务端会话（重放旧 cookie 一律 401），支持同一用户
多浏览器并发会话（logout 只吊销当前会话），会话有限时效 + 有界内存。

设计约束：
- 会话 token 为每次登录随机生成（secrets.token_urlsafe(32)，仅 ASCII），
  不再从 COCKPIT_TOKEN 派生单一静态值。
- 默认只在进程内存。传入 path 后原子写入 0600 文件，重启仍认未过期会话。
- 有界性：TTL 有限（默认 12h）+ 容量上限（默认 128），登录时惰性清理过期项
  并在满员时逐出最快过期者；无任何后台线程。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

DEFAULT_SESSION_TTL_SECONDS = 12 * 3600
MAX_SESSIONS = 128


class SessionRegistry:
    """Opaque token -> expiry 的有界注册表；所有方法线程安全。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
        capacity: int = MAX_SESSIONS,
        clock=time.time,
        path: Path | str | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._ttl = float(ttl_seconds)
        self._capacity = int(capacity)
        self._clock = clock
        self._path = Path(path) if path else None
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()
        self._load()

    def issue(self) -> tuple[str, float]:
        """签发一个新会话，返回 (opaque_token, expiry_epoch)。"""
        now = self._clock()
        with self._lock:
            self._purge(now)
            while len(self._sessions) >= self._capacity:
                victim = min(self._sessions, key=self._sessions.get)
                del self._sessions[victim]
            token = secrets.token_urlsafe(32)
            expiry = now + self._ttl
            self._sessions[token] = expiry
            self._save()
            return token, expiry

    def validate(self, token: str | None) -> bool:
        """校验会话 token；过期即删除（惰性清理）。任何畸形输入仅返回 False。"""
        if not token or not isinstance(token, str):
            return False
        now = self._clock()
        with self._lock:
            expiry = self._sessions.get(token)
            if expiry is None:
                return False
            if expiry <= now:
                del self._sessions[token]
                self._save()
                return False
            return True

    def revoke(self, token: str | None) -> bool:
        """吊销单个会话；返回是否确实吊销。"""
        if not token or not isinstance(token, str):
            return False
        with self._lock:
            removed = self._sessions.pop(token, None) is not None
            if removed:
                self._save()
            return removed

    def _purge(self, now: float) -> None:
        # 调用方必须已持锁。
        expired = [token for token, expiry in self._sessions.items() if expiry <= now]
        for token in expired:
            del self._sessions[token]

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(sessions, dict):
            return
        now = self._clock()
        loaded: dict[str, float] = {}
        for token, expiry in sessions.items():
            if not isinstance(token, str) or not token.isascii() or not token:
                continue
            try:
                exp = float(expiry)
            except (TypeError, ValueError):
                continue
            if exp > now:
                loaded[token] = exp
        self._sessions = loaded

    def _save(self) -> None:
        # 调用方必须已持锁。
        if self._path is None:
            return
        payload = json.dumps({"sessions": self._sessions}, separators=(",", ":"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(tmp, flags, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._path)
        os.chmod(self._path, 0o600)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
