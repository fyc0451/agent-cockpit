"""SessionRegistry 单元：有限时效、有界容量、吊销、随机唯一、畸形输入安全。"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_cockpit.auth_session import SessionRegistry


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_issue_returns_unique_opaque_tokens():
    clock = FakeClock()
    reg = SessionRegistry(clock=clock)
    tokens = [reg.issue()[0] for _ in range(64)]
    assert len(set(tokens)) == 64
    assert all(t.isascii() for t in tokens)


def test_validate_then_ttl_expiry():
    clock = FakeClock()
    reg = SessionRegistry(ttl_seconds=60, clock=clock)
    token, expiry = reg.issue()
    assert expiry == clock.now + 60
    assert reg.validate(token) is True
    clock.advance(59)
    assert reg.validate(token) is True
    clock.advance(2)
    assert reg.validate(token) is False
    assert len(reg) == 0  # 惰性清理


def test_revoke_is_per_session():
    clock = FakeClock()
    reg = SessionRegistry(clock=clock)
    a, _ = reg.issue()
    b, _ = reg.issue()
    assert reg.revoke(a) is True
    assert reg.validate(a) is False
    assert reg.validate(b) is True
    assert reg.revoke(a) is False  # 重复吊销幂等
    assert reg.revoke(None) is False


def test_capacity_bound_evicts_soonest_expiry():
    clock = FakeClock()
    reg = SessionRegistry(ttl_seconds=100, capacity=3, clock=clock)
    first, _ = reg.issue()
    clock.advance(10)
    reg.issue()
    reg.issue()
    assert len(reg) == 3
    reg.issue()  # 满员 -> 逐出最快过期者（first）
    assert len(reg) == 3
    assert reg.validate(first) is False


def test_malformed_inputs_never_validate_or_revoke():
    reg = SessionRegistry()
    for bad in ("", None, "üñí", "a" * 10_000, "\x00\x01", "{}"):
        assert reg.validate(bad) is False
        assert reg.revoke(bad) is False


def test_restart_semantics_via_fresh_registry():
    """进程内注册表：新实例（等价重启）不认旧会话 token。"""
    clock = FakeClock()
    old = SessionRegistry(clock=clock)
    token, _ = old.issue()
    new = SessionRegistry(clock=clock)
    assert old.validate(token) is True
    assert new.validate(token) is False  # 跨实例/重启隔离


def test_constructor_rejects_invalid_config():
    for kwargs in ({"ttl_seconds": 0}, {"ttl_seconds": -1}, {"capacity": 0}):
        try:
            SessionRegistry(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {kwargs}")
