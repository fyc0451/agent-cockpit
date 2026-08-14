"""Server-side login session registry.

P1 修复：logout 必须吊销服务端会话（重放旧 cookie 一律 401），支持同一用户
多浏览器并发会话（logout 只吊销当前会话），会话有限时效 + 有界内存。

设计约束：
- 会话 token 为每次登录随机生成（secrets.token_urlsafe(32)，仅 ASCII），
  不再从 COCKPIT_TOKEN 派生单一静态值。
- 注册表为进程内存：服务重启后全部会话失效（需重新登录），跨实例天然隔离。
- 有界性：TTL 有限（默认 12h）+ 容量上限（默认 128），登录时惰性清理过期项
  并在满员时逐出最快过期者；无任何后台线程。
"""
from __future__ import annotations

import secrets
import threading
import time

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
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._ttl = float(ttl_seconds)
        self._capacity = int(capacity)
        self._clock = clock
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

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
                return False
            return True

    def revoke(self, token: str | None) -> bool:
        """吊销单个会话；返回是否确实吊销。"""
        if not token or not isinstance(token, str):
            return False
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def _purge(self, now: float) -> None:
        # 调用方必须已持锁。
        expired = [token for token, expiry in self._sessions.items() if expiry <= now]
        for token in expired:
            del self._sessions[token]

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
