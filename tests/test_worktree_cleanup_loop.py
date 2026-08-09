"""server lifespan 接入 tasks.cleanup_worktrees 的后台调度测试。

不真实 sleep 6h：用 Event / monkeypatch 驱动 _wait_worktree_cleanup_interval。
不改 cleanup_worktrees 删除语义，只验证调度、异常续跑、退出 cancel。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

import server
import tasks


def test_worktree_cleanup_loop_runs_immediately_then_waits(monkeypatch):
    """启动后立刻 to_thread 跑一轮 cleanup，再进入可注入的等待。"""
    calls: list[float] = []
    # 每次 wait 从队列取一个 token；无 token 时阻塞，避免 Event 置位后空转。
    wait_tokens: asyncio.Queue[None] = asyncio.Queue()
    entered_waits = 0
    entered = asyncio.Event()

    def fake_cleanup(max_age_hours: float = 48) -> dict:
        calls.append(float(max_age_hours))
        return {"removed": [], "count": 0, "errors": []}

    async def fake_wait() -> None:
        nonlocal entered_waits
        entered_waits += 1
        entered.set()
        await wait_tokens.get()

    monkeypatch.setattr(tasks, "cleanup_worktrees", fake_cleanup)
    monkeypatch.setattr(server, "_wait_worktree_cleanup_interval", fake_wait)

    async def exercise() -> None:
        task = asyncio.create_task(server._worktree_cleanup_loop())
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert calls == [server.WORKTREE_CLEANUP_MAX_AGE_HOURS]
        assert entered_waits == 1
        # 放行一轮 → 第二次 cleanup → 再次进入 wait
        entered.clear()
        await wait_tokens.put(None)
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert calls == [
            server.WORKTREE_CLEANUP_MAX_AGE_HOURS,
            server.WORKTREE_CLEANUP_MAX_AGE_HOURS,
        ]
        assert entered_waits == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


def test_worktree_cleanup_loop_logs_and_continues_on_error(monkeypatch, caplog):
    """cleanup 抛异常时 logger.exception 后循环继续，不重叠重入。"""
    calls = {"n": 0}
    entered_wait = asyncio.Event()
    in_cleanup = asyncio.Event()
    max_concurrent = {"v": 0}
    active = {"v": 0}

    def flaky_cleanup(max_age_hours: float = 48) -> dict:
        active["v"] += 1
        max_concurrent["v"] = max(max_concurrent["v"], active["v"])
        in_cleanup.set()
        try:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom-cleanup")
            return {"removed": [], "count": 0, "errors": []}
        finally:
            active["v"] -= 1

    async def fake_wait() -> None:
        entered_wait.set()
        await asyncio.Event().wait()  # 阻塞到 cancel

    monkeypatch.setattr(tasks, "cleanup_worktrees", flaky_cleanup)
    monkeypatch.setattr(server, "_wait_worktree_cleanup_interval", fake_wait)

    async def exercise() -> None:
        with caplog.at_level(logging.ERROR, logger="agent-cockpit"):
            task = asyncio.create_task(server._worktree_cleanup_loop())
            await asyncio.wait_for(entered_wait.wait(), timeout=2)
            assert calls["n"] == 1
            assert max_concurrent["v"] == 1
            assert any("worktree cleanup failed" in r.message for r in caplog.records)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())


def test_worktree_cleanup_loop_cancel_during_wait(monkeypatch):
    """等待区间 cancel 后任务结束，不遗留。"""
    calls: list[float] = []
    waiting = asyncio.Event()

    def fake_cleanup(max_age_hours: float = 48) -> dict:
        calls.append(float(max_age_hours))
        return {"removed": [], "count": 0, "errors": []}

    async def fake_wait() -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(tasks, "cleanup_worktrees", fake_cleanup)
    monkeypatch.setattr(server, "_wait_worktree_cleanup_interval", fake_wait)

    async def exercise() -> None:
        task = asyncio.create_task(server._worktree_cleanup_loop())
        await asyncio.wait_for(waiting.wait(), timeout=2)
        assert calls == [server.WORKTREE_CLEANUP_MAX_AGE_HOURS]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done()

    asyncio.run(exercise())


def test_lifespan_starts_and_cancels_worktree_cleanup(monkeypatch):
    """lifespan 启动 cleanup 任务，退出时 cancel+await，不遗留。"""
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()
    poller_started = asyncio.Event()
    message_poller_started = asyncio.Event()

    async def fake_cleanup_loop() -> None:
        cleanup_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_cancelled.set()
            raise

    async def fake_poller() -> None:
        poller_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    async def fake_message_poller() -> None:
        message_poller_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    async def fake_release() -> None:
        return None

    monkeypatch.setattr(server, "_worktree_cleanup_loop", fake_cleanup_loop)
    monkeypatch.setattr(server, "_poll_live_state", fake_poller)
    monkeypatch.setattr(server, "_poll_message_state", fake_message_poller)
    monkeypatch.setattr(
        server, "_release_all_zoom_leases", lambda: None,
    )

    async def exercise() -> None:
        async with server.lifespan(server.app):
            await asyncio.wait_for(cleanup_started.wait(), timeout=2)
            await asyncio.wait_for(poller_started.wait(), timeout=2)
            await asyncio.wait_for(message_poller_started.wait(), timeout=2)
            assert server._worktree_cleanup_task is not None
            assert server._message_poller_task is not None
            assert not server._worktree_cleanup_task.done()
        await asyncio.wait_for(cleanup_cancelled.wait(), timeout=2)
        assert server._worktree_cleanup_task is None
        assert server._poller_task is None
        assert server._message_poller_task is None

    asyncio.run(exercise())


def test_lifespan_cleans_up_when_background_task_already_failed(monkeypatch):
    released = []

    async def failed_poller() -> None:
        raise RuntimeError("poller crashed")

    async def waiting_task() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "_poll_live_state", failed_poller)
    monkeypatch.setattr(server, "_poll_message_state", waiting_task)
    monkeypatch.setattr(server, "_worktree_cleanup_loop", waiting_task)
    monkeypatch.setattr(
        server, "_release_all_zoom_leases", lambda: released.append(True),
    )

    async def exercise() -> None:
        async with server.lifespan(server.app):
            await asyncio.sleep(0)
        assert server._poller_task is None
        assert server._message_poller_task is None
        assert server._worktree_cleanup_task is None

    asyncio.run(exercise())
    assert released == [True]


def test_wait_interval_uses_six_hours(monkeypatch):
    """默认等待间隔为 6 小时（不真实 sleep）。"""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)

    async def exercise() -> None:
        await server._wait_worktree_cleanup_interval()

    asyncio.run(exercise())
    assert slept == [server.WORKTREE_CLEANUP_INTERVAL_S]
    assert server.WORKTREE_CLEANUP_INTERVAL_S == 6 * 3600
    assert server.WORKTREE_CLEANUP_MAX_AGE_HOURS == 48.0
