"""Low-cardinality, process-local runtime counters."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


_CONNECTION_KINDS = ("sse", "terminal_websocket")
# Fixed hard cap for concurrent SSE streams (no env/config).
MAX_SSE_CONNECTIONS = 64
_PROCESS_STARTED = time.monotonic()
_lock = threading.Lock()
_connection_counts = {kind: 0 for kind in _CONNECTION_KINDS}


@dataclass
class _ConnectionLease:
    kind: str
    closed: bool = False

    def close(self) -> None:
        with _lock:
            if self.closed:
                return
            self.closed = True
            _connection_counts[self.kind] -= 1


def try_open_connection(kind: str) -> _ConnectionLease | None:
    """Atomically check SSE limit and reserve a slot under one lock.

    Returns None only when kind is ``sse`` and the fixed cap is full.
    Terminal websocket leases are unlimited (same as before).
    """
    if kind not in _CONNECTION_KINDS:
        raise ValueError("invalid connection kind")
    with _lock:
        if kind == "sse" and _connection_counts["sse"] >= MAX_SSE_CONNECTIONS:
            return None
        _connection_counts[kind] += 1
    return _ConnectionLease(kind)


def open_connection(kind: str) -> _ConnectionLease:
    """Reserve a connection slot; raises ValueError for unknown kinds.

    For SSE, raises RuntimeError if the fixed concurrent cap is exhausted.
    Prefer ``try_open_connection`` at the HTTP boundary to map full → 429.
    """
    lease = try_open_connection(kind)
    if lease is None:
        raise RuntimeError("sse_connection_limit")
    return lease


@contextmanager
def track_connection(kind: str) -> Iterator[None]:
    lease = open_connection(kind)
    try:
        yield
    finally:
        lease.close()


def process_stats() -> dict[str, int]:
    elapsed = max(0.0, time.monotonic() - _PROCESS_STARTED)
    return {"uptime_seconds": int(elapsed)}


def connection_stats() -> dict[str, int]:
    with _lock:
        return {kind: _connection_counts[kind] for kind in _CONNECTION_KINDS}


__all__ = [
    "MAX_SSE_CONNECTIONS",
    "connection_stats",
    "open_connection",
    "process_stats",
    "track_connection",
    "try_open_connection",
]
