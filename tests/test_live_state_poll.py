"""tests/test_live_state_poll.py — poll 指标与自适应间隔的生产路径测试。

全部直接调用 server._record_poll_metrics / _poll_delay / _parse_poll_interval,
不手工复制指标逻辑(避免假绿)。覆盖:成功重置、连续失败 delay 递增封顶、
无 session idle、失败优先于 idle、samples 截断、p50/p95/failure_rate、
环境值校验(拒 <=0/NaN/inf)。
"""
from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture(autouse=True)
def _reset_metrics(monkeypatch):
    """每个测试前重置 _POLL_METRICS,避免相互污染。"""
    fresh = {
        "count": 0, "failures": 0, "consecutive_failures": 0,
        "last_duration": 0.0, "last_session_count": 0, "samples": [],
        "duration_p50": 0.0, "duration_p95": 0.0, "failure_rate": 0.0,
    }
    monkeypatch.setattr(server, "_POLL_METRICS", fresh)


# ── _parse_poll_interval:环境值校验 ──────────────────────────

def test_parse_poll_interval_default():
    assert server._parse_poll_interval(None) == 2.0
    assert server._parse_poll_interval("") == 2.0


def test_parse_poll_interval_valid():
    assert server._parse_poll_interval("2") == 2.0
    assert server._parse_poll_interval("1.5") == 1.5
    assert server._parse_poll_interval("0.1") == 0.1


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "nan", "inf", "-inf", "1e400"])
def test_parse_poll_interval_rejects_invalid(bad):
    """<=0/NaN/inf/溢出 都回退默认 2.0,绝不 busy loop。"""
    assert server._parse_poll_interval(bad) == 2.0


# ── _record_poll_metrics:基础记录 ────────────────────────────

def test_record_metrics_success_resets_consecutive_failures():
    server._POLL_METRICS["consecutive_failures"] = 3
    server._POLL_METRICS["failures"] = 5
    server._record_poll_metrics(0.1, 2, success=True)
    m = server._POLL_METRICS
    assert m["count"] == 1
    assert m["consecutive_failures"] == 0  # 成功重置
    assert m["failures"] == 5  # 累计 failures 不因成功减少
    assert m["last_duration"] == 0.1
    assert m["last_session_count"] == 2
    assert len(m["samples"]) == 1


def test_record_metrics_failure_increments():
    server._record_poll_metrics(0.5, 1, success=False)
    server._record_poll_metrics(0.6, 1, success=False)
    m = server._POLL_METRICS
    assert m["failures"] == 2
    assert m["consecutive_failures"] == 2
    assert m["failure_rate"] == 1.0  # 2/2


def test_record_metrics_failure_rate_mixed():
    server._record_poll_metrics(0.1, 1, success=True)
    server._record_poll_metrics(0.2, 1, success=False)
    server._record_poll_metrics(0.3, 1, success=True)
    assert server._POLL_METRICS["failure_rate"] == pytest.approx(1 / 3)


# ── samples 截断 ─────────────────────────────────────────────

def test_record_metrics_samples_capped():
    for i in range(server._POLL_METRICS_SAMPLES + 20):
        server._record_poll_metrics(float(i), 1, success=True)
    assert len(server._POLL_METRICS["samples"]) == server._POLL_METRICS_SAMPLES


# ── p50/p95 计算 ─────────────────────────────────────────────

def test_record_metrics_p50_p95():
    # 注入已知样本:0..9,p50=4(or 5),p95=9
    for i in range(10):
        server._record_poll_metrics(float(i), 1, success=True)
    m = server._POLL_METRICS
    sorted_s = sorted(range(10))
    assert m["duration_p50"] == sorted_s[5]  # n//2 = 5
    p95_idx = min(9, int(10 * 0.95))  # = 9
    assert m["duration_p95"] == sorted_s[p95_idx]


# ── _poll_delay:自适应间隔(失败优先于 idle)──────────────────

def test_poll_delay_normal():
    assert server._poll_delay(3) == server.POLL_INTERVAL


def test_poll_delay_idle_no_sessions():
    """无 session 且无失败 → idle 间隔。"""
    assert server._poll_delay(0) == server.POLL_IDLE_INTERVAL


def test_poll_delay_failure_takes_priority_over_idle():
    """失败优先于 idle:即使 session_count==0,失败时也退避,不固定 idle 10s。"""
    server._POLL_METRICS["consecutive_failures"] = 1
    delay = server._poll_delay(0)
    expected = min(server.POLL_BACKOFF_MAX, server.POLL_INTERVAL * server.POLL_BACKOFF)
    assert delay == expected
    assert delay != server.POLL_IDLE_INTERVAL


def test_poll_delay_backoff_increases_with_consecutive_failures():
    """连续失败时 delay 逐次递增。"""
    server._POLL_METRICS["consecutive_failures"] = 1
    d1 = server._poll_delay(1)
    server._POLL_METRICS["consecutive_failures"] = 2
    d2 = server._poll_delay(1)
    server._POLL_METRICS["consecutive_failures"] = 3
    d3 = server._poll_delay(1)
    assert d1 < d2 < d3


def test_poll_delay_backoff_capped():
    """退避封顶 POLL_BACKOFF_MAX,不会无限增长。"""
    server._POLL_METRICS["consecutive_failures"] = 100
    assert server._poll_delay(1) == server.POLL_BACKOFF_MAX


def test_poll_delay_resets_after_success():
    """成功后(consecutive_failures=0)回到正常/idle。"""
    server._POLL_METRICS["consecutive_failures"] = 5
    assert server._poll_delay(1) > server.POLL_INTERVAL  # 退避中
    server._record_poll_metrics(0.1, 1, success=True)  # 成功 → 重置
    assert server._poll_delay(1) == server.POLL_INTERVAL
    assert server._poll_delay(0) == server.POLL_IDLE_INTERVAL


# ── /health poll 出口 ───────────────────────────────────────

def test_health_poll_exposes_metrics_without_samples():
    """/health.poll 返回约定字段且不含 raw samples。"""
    server._record_poll_metrics(0.15, 3, success=True)
    server._record_poll_metrics(0.25, 3, success=False)
    poll = server.health_poll()
    # 约定字段齐全
    for key in ("count", "failures", "failure_rate", "consecutive_failures",
                "last_duration", "duration_p50", "duration_p95",
                "last_session_count", "interval"):
        assert key in poll, f"poll 缺字段 {key}"
    # 不暴露 raw samples
    assert "samples" not in poll
    # 值正确
    assert poll["count"] == 2
    assert poll["failures"] == 1
    assert poll["last_session_count"] == 3
    assert poll["interval"] == server.POLL_INTERVAL


def test_health_poll_is_copy_not_global_alias():
    """/health.poll 返回 copy,修改它不影响全局 _POLL_METRICS。"""
    poll = server.health_poll()
    poll["count"] = 999
    assert server._POLL_METRICS["count"] != 999


def test_health_does_not_expose_poll_activity():
    assert "poll" not in server.health()


def test_poll_delay_large_failure_count_stays_capped():
    server._POLL_METRICS["consecutive_failures"] = 1800
    assert server._poll_delay(1) == server.POLL_BACKOFF_MAX


def test_health_poll_requires_authentication(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret")
    client = TestClient(server.app)
    assert client.get("/health.poll").status_code == 401
    assert client.get(
        "/health.poll", headers={"Authorization": "Bearer secret"},
    ).status_code == 200
