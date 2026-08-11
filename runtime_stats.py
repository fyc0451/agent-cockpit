"""Low-cardinality, process-local runtime counters."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


_CONNECTION_KINDS = ("sse", "terminal_websocket")
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


def open_connection(kind: str) -> _ConnectionLease:
    if kind not in _CONNECTION_KINDS:
        raise ValueError("invalid connection kind")
    with _lock:
        _connection_counts[kind] += 1
    return _ConnectionLease(kind)


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
    "connection_stats",
    "open_connection",
    "process_stats",
    "track_connection",
]
