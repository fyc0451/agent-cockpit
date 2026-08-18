"""3.0 群聊账本对抗测试 harness：隔离环境、时钟注入、泄漏探针原语。

只提供构造器与探针，不做断言；断言留给各测试按门面语义书写。
配套用法见 tests/test_chat_harness.py。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_cockpit import herdr_client
from agent_cockpit import runtime_paths

# conftest 的 autouse fixture 会把 herdr_client.pane_send 换成空 stub；
# 模块在收集阶段导入（fixture 尚未运行），此处拿到的是真身，供门 3 还原。
REAL_PANE_SEND = herdr_client.pane_send


def ledger_env(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """隔离账本环境：四个 COCKPIT_*_DIR 全部落到 tmp_path 下。

    与 test_chat_ledger.py 的 isolated_ledger fixture 同语义，
    这里做成参数化 builder，便于非 fixture 场景复用。
    """
    paths = {
        "data": tmp_path / "data",
        "config": tmp_path / "config",
        "state": tmp_path / "state",
        "uploads": tmp_path / "uploads",
    }
    for path in paths.values():
        path.mkdir()
    monkeypatch.setenv("COCKPIT_DATA_DIR", str(paths["data"]))
    monkeypatch.setenv("COCKPIT_CONFIG_DIR", str(paths["config"]))
    monkeypatch.setenv("COCKPIT_STATE_DIR", str(paths["state"]))
    monkeypatch.setenv("COCKPIT_UPLOADS_DIR", str(paths["uploads"]))
    monkeypatch.delenv("COCKPIT_COORDINATION_DB", raising=False)
    runtime_paths.reset_cache()
    paths["root"] = tmp_path
    return paths


class FakeClock:
    """确定性时钟：固定起点、步进毫秒，供 ts= 注入与 Hub created_ts 构造。"""

    def __init__(self, start_ms: int = 1_800_000_000_000, step_ms: int = 1_000):
        self._now = start_ms
        self._step = step_ms

    def peek_ms(self) -> int:
        return self._now

    def next_ms(self) -> int:
        stamp = self._now
        self._now += self._step
        return stamp

    def next_s(self) -> int:
        """整秒版本，匹配 Hub created_ts 的 int 秒格式。"""
        return self.next_ms() // 1000

    def next_iso(self) -> str:
        """ISO 字符串版本，覆盖 _mail_ts_ms 的字符串解析分支。"""
        stamp = self.next_ms()
        return datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()


def fake_clock(start_ms: int = 1_800_000_000_000, step_ms: int = 1_000) -> FakeClock:
    return FakeClock(start_ms=start_ms, step_ms=step_ms)


def hub_message(
    mid: int,
    *,
    sender_name: str,
    text: str,
    created_ts: int | str,
    sender_program: str = "",
    thread_id: str = "",
    recipients: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """伪造一行 Hub 消息，结构对齐 db.messages_for_canonical_project 返回。"""
    return {
        "id": mid,
        "sender_name": sender_name,
        "sender_program": sender_program,
        "body_md": text,
        "subject": text,
        "created_ts": created_ts,
        "thread_id": thread_id,
        "recipients": [{"name": name} for name in recipients],
    }


def leak_probe_hub_rows(
    clock: FakeClock,
    *,
    other_thread: str,
    known_sender: str,
    start_id: int = 900,
) -> list[dict[str, Any]]:
    """泄漏探针：每一行按 _hub_message_in_chat 语义都必须被过滤掉。

    覆盖：别群 thread、不存在的 thread_id、空 thread_id + 陌生 sender、
    空 thread_id + 本群 sender 但时间早于 thread 创建（since_ms 截断）。
    """
    return [
        hub_message(
            start_id, sender_name="human", text="probe:别群 thread 串群",
            created_ts=clock.next_s(), thread_id=other_thread,
            recipients=("human",),
        ),
        hub_message(
            start_id + 1, sender_name=known_sender, text="probe:幽灵 thread_id",
            created_ts=clock.next_s(), thread_id="th_000000000000",
            recipients=("human",),
        ),
        hub_message(
            start_id + 2, sender_name="StrangerX", text="probe:陌生 sender 空 thread",
            created_ts=clock.next_s(), thread_id="",
            recipients=("NobodyHere",),
        ),
        hub_message(
            start_id + 3, sender_name=known_sender, text="probe:过期空 thread 旧信",
            created_ts=1_700_000_000, thread_id="",
            recipients=("human",),
        ),
    ]


def identity_notice_text(name: str, project_path: str) -> str:
    """构造 [agent-mail 身份告知] 文本，项目路径由调用方注入。"""
    return f"[agent-mail 身份告知] 花名={name},项目={project_path}"


def pytest_identity_notice_variants(name: str = "FuchsiaPond") -> list[str]:
    """pytest 临时路径身份告知变体：全部必须被 pane_send 丢弃。"""
    return [
        identity_notice_text(name, f"/tmp/pytest-of-fyc/pytest-2828/test_x0/{name.lower()}"),
        identity_notice_text(name, "/tmp/pytest-123/abc"),
        identity_notice_text(name, "/home/fyc/pytest-of-data/legacy-repo"),
    ]


def restore_real_pane_send(monkeypatch) -> None:
    """撤销 conftest 对 pane_send 的全局 stub，让测试打到真身的过滤分支。"""
    monkeypatch.setattr(herdr_client, "pane_send", REAL_PANE_SEND)
