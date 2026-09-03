"""看现场 WebSocket 泵：先推一帧，变化才再推。"""
from __future__ import annotations

import asyncio
import threading

from fastapi.testclient import TestClient

from agent_cockpit import pane_live
from agent_cockpit import server


def test_extract_pane_text_unwraps_json_output():
    assert pane_live.extract_pane_text({
        "output": '{"result":{"text":"hello from pane"}}',
    }) == "hello from pane"
    assert pane_live.extract_pane_text({"output": "", "error": "gone"}) == "gone"


def test_looks_like_tui_screen_keeps_grok_layout():
    screen = (
        "   fyc ~/hr-ready       ⸬ 1 │ 174K / 256K\n"
        "\n"
        "     ▾ Tasks 1\n"
        "     ❯ Boss 在群聊给你发了消     7:18 PM\n"
        "       息。请直接做下面的任\n"
        "  ╭──────────────────────────────────────╮\n"
        "  │ ❯ Build anything                     │\n"
        "  ╰─── grok-4.6 (high) · always-approve ─╯\n"
        "  →:expand  │  Enter:open  │  Ctrl+e:\n"
    )
    assert pane_live.looks_like_tui_screen(screen)
    kept = pane_live.extract_pane_text({"output": screen})
    assert kept == screen.rstrip("\n")
    assert kept.splitlines()[4].startswith("       息。")
    assert pane_live.snapshot_from_read({"output": screen})["layout"] == "tui"


def test_unwrap_terminal_wrap_joins_split_pane_lines():
    text = pane_live.unwrap_terminal_wrap(
        "• 剩余节点也继续稳定下\n"
        "载，已到约 1.5 MB。发\n"
        "布脚本使用固定 commit\n"
        "和失败关闭策略，不会因\n"
        "为网络慢而跳过依赖；因\n"
        "此关机后得到的 GPU 服\n"
        "务与仓库代码能严格对\n"
        "应。\n"
        "\n"
        "• Waiting for background…\n"
        "  ssh badge-dev 'bash…\n",
        short_limit=0,
    )
    assert "剩余节点也继续稳定下载，已到约 1.5 MB。" in text
    assert "不会因为网络慢而跳过依赖" in text
    assert "服务与仓库代码能严格对应。" in text
    assert "\n载，" not in text


def test_extract_live_progress_keeps_codex_status_not_tool_chrome():
    screen = (
        "• Waited for background terminal · ssh badge-dev\n"
        "  docker run --rm\n"
        "• 固定源码包已下载到约 5.7 MB，连接稳定但带宽偏低。"
        "发布过程没有切换 current。\n"
        "• Ran ssh badge-dev 'bash -s'\n"
        "• 下载已到约 7.6 MB，仍是持续增长且无超时。"
        "继续当前幂等部署是成本和风险更低的路径。\n"
        "• Explored\n"
        "  Search source.tar.gz\n"
        "• Waiting for background terminal\n"
    )
    assert pane_live.extract_live_progress(screen, "codex") == (
        "下载已到约 7.6 MB，仍是持续增长且无超时。"
        "继续当前幂等部署是成本和风险更低的路径。"
    )
    assert pane_live.extract_live_progress(screen, "grok") == ""
    wrapped = (
        "• 固定源码包已下载到约\n"
        " 5.7 MB，连接稳定但带\n"
        "宽偏低。\n"
    )
    assert "5.7 MB" in pane_live.extract_live_progress(wrapped, "codex")


def test_extract_live_progress_uses_agent_specific_safe_adapters():
    kimi = (
        "● Used Read (secret.env) · 10 lines\n"
        "● 已完成数据结构核对，正在整理可以直接执行的修复步骤。\n"
    )
    claude = (
        "● Bash(git status)\n"
        "● 已定位状态同步的竞态条件，接下来补充回归测试。\n"
    )
    assert pane_live.extract_live_progress(kimi, "kimi") == (
        "已完成数据结构核对，正在整理可以直接执行的修复步骤。"
    )
    assert pane_live.extract_live_progress(claude, "claude") == (
        "已定位状态同步的竞态条件，接下来补充回归测试。"
    )
    assert pane_live.extract_live_progress("● 正在读取 /home/fyc/.env token=abc", "kimi") == ""
    assert pane_live.extract_live_progress("anything", "unknown-agent") == ""


def test_live_progress_rejects_common_secrets_commands_and_relative_paths():
    unsafe = (
        "正在配置 AKIAIOSFODNN7EXAMPLE 供服务使用。",
        "正在检查 eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.signaturevalue。",
        "正在使用 ghp_abcdefghijklmnopqrstuvwxyz123456 完成验证。",
        "正在运行 npm test 验证界面行为。",
        "正在读取 src/agent_cockpit/server.py 定位问题。",
    )
    for summary in unsafe:
        assert pane_live.sanitize_live_progress(summary) == ""


def test_snapshot_from_envelope_uses_matched_read():
    snap = pane_live.snapshot_from_envelope({
        "event": "pane.output_matched",
        "data": {
            "pane_id": "w1:p2",
            "matched_line": "four",
            "read": {"text": "two\nthree\nfour"},
        },
    })
    assert snap == {
        "type": "snapshot", "output": "two\nthree\nfour", "error": None, "layout": "log",
    }
    assert pane_live.snapshot_from_envelope({"event": "pane.scroll_changed", "data": {}}) is None


def test_pump_sends_first_frame_then_only_changes():
    frames = [
        {"type": "snapshot", "output": "one\ntwo\nthree", "error": None},
        {"type": "snapshot", "output": "one\ntwo\nthree", "error": None},
        {"type": "snapshot", "output": "two\nthree\nfour", "error": None},
    ]
    sent: list[dict] = []
    waits = {"n": 0}

    def reader(_session: str, _pane: str) -> dict:
        return frames[min(waits["n"], len(frames) - 1)]

    def wait() -> dict | None:
        waits["n"] += 1
        return None

    async def send(payload: dict) -> None:
        sent.append(payload)

    asyncio.run(pane_live.pump_pane_live(
        send, "cockpit", "w1:p2",
        reader=reader, wait=wait, closed=lambda: waits["n"] >= 2,
    ))
    assert [row["output"] for row in sent] == ["one\ntwo\nthree", "two\nthree\nfour"]


def test_pump_pushes_event_snapshot_without_reread():
    sent: list[dict] = []
    reads = {"n": 0}
    waits = {"n": 0}

    def reader(_session: str, _pane: str) -> dict:
        reads["n"] += 1
        return {"type": "snapshot", "output": "one", "error": None}

    def wait() -> dict | None:
        waits["n"] += 1
        return {"type": "snapshot", "output": "one\ntwo", "error": None}

    async def send(payload: dict) -> None:
        sent.append(payload)

    asyncio.run(pane_live.pump_pane_live(
        send, "cockpit", "w1:p2",
        reader=reader, wait=wait, closed=lambda: waits["n"] >= 1,
    ))
    assert reads["n"] == 1
    assert [row["output"] for row in sent] == ["one", "one\ntwo"]


class _GateWaiter:
    def __init__(self, *_a, **_k) -> None:
        self.gate = threading.Event()

    def wait(self):
        self.gate.wait(0.5)
        return None

    def close(self) -> None:
        self.gate.set()


def test_pane_live_websocket_pushes_snapshot(monkeypatch):
    monkeypatch.setattr(server, "_websocket_trusted", lambda _ws: True)
    monkeypatch.setattr(
        server.pane_live, "read_snapshot",
        lambda _s, _p: {
            "type": "snapshot", "output": "hello from live", "error": None, "layout": "log",
        },
    )
    monkeypatch.setattr(server.pane_live, "HerdrLiveWaiter", _GateWaiter)
    client = TestClient(server.app)
    with client.websocket_connect("/api/chat/sessions/cockpit/panes/w1:p2/live") as ws:
        assert ws.receive_json() == {
            "type": "snapshot", "output": "hello from live", "error": None, "layout": "log",
        }
