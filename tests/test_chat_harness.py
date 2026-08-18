"""3.0 群聊固定门（改 8790 群聊必跑）：会话隔离、未分组不扫 Hub、pytest 不注活 pane。

构造器与探针全部来自 tests/chat_harness.py；本文件只写门面断言。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import chat_harness
from agent_cockpit import chat_ledger
from agent_cockpit import herdr_client
from agent_cockpit import runtime_paths
from agent_cockpit import server


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    yield chat_harness.ledger_env(tmp_path, monkeypatch)
    runtime_paths.reset_cache()


def _client() -> TestClient:
    return TestClient(server.app)


def _headers() -> dict[str, str]:
    return {"authorization": "Bearer secret"}


def _stub_runtime(monkeypatch) -> None:
    """无真实 herdr 环境：快照为空、身份增强透传，保证 leader/收割路径确定。"""
    monkeypatch.setattr(server, "_herdr_runtime_snapshot", lambda: {"sessions": [], "panes": []})
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)


def _stub_hub(monkeypatch, rows: list[dict]) -> None:
    """Hub 可用、项目解析直通、消息列表返回伪造行。"""
    monkeypatch.setattr(server.db, "status", lambda: {"available": True, "reason": None})
    monkeypatch.setattr(
        server.next_profile, "require_project",
        lambda path: str(Path(path).expanduser().resolve()),
    )
    monkeypatch.setattr(
        server.db, "project_by_canonical_key",
        lambda key: {"id": 7, "human_key": key},
    )
    monkeypatch.setattr(
        server.db, "messages_for_canonical_project",
        lambda _pid, _limit: rows,
    )


def test_gate1_session_isolation_two_threads(ledger, monkeypatch):
    """门 1：同项目两个群，A 的视图不得出现 B 的账本/Hub 任何内容。"""
    client = _client()
    proj = ledger["root"] / "shared-proj"
    proj.mkdir()
    workspace = chat_ledger.create_workspace(str(proj), "shared")
    thread_a = chat_ledger.create_thread(workspace["id"], "alpha-1")
    thread_b = chat_ledger.create_thread(workspace["id"], "beta-1")

    clock = chat_harness.fake_clock()
    chat_ledger.append_message(
        "alpha-1", kind="me", sender="human", text="alpha 本地一", to=["AlphaFox"],
        ts=clock.next_ms(),
    )
    chat_ledger.append_message(
        "alpha-1", kind="agent", sender="AlphaFox", text="alpha 本地二", to=["human"],
        ts=clock.next_ms(),
    )
    chat_ledger.append_message(
        "beta-1", kind="me", sender="human", text="beta 本地一", to=["BetaWolf"],
        ts=clock.next_ms(),
    )

    hub_rows = [
        # 正例 A：thread 名命中 allowed_threads
        chat_harness.hub_message(
            1, sender_name="human", text="alpha hub 一",
            created_ts=clock.next_s(), thread_id="alpha-1", recipients=("AlphaFox",),
        ),
        # 正例 A：thread_id 命中 allowed_threads，created_ts 走 ISO 字符串分支
        chat_harness.hub_message(
            2, sender_name="AlphaFox", sender_program="kimi", text="alpha hub 二",
            created_ts=clock.next_iso(), thread_id=thread_a["id"], recipients=("human",),
        ),
        # 正例 B：thread 名 + thread_id 两种形态
        chat_harness.hub_message(
            3, sender_name="human", text="beta hub 一",
            created_ts=clock.next_s(), thread_id="beta-1", recipients=("BetaWolf",),
        ),
        chat_harness.hub_message(
            4, sender_name="BetaWolf", sender_program="codex", text="beta hub 旧式无 thread",
            created_ts=clock.next_s(), thread_id="", recipients=("human",),
        ),
        chat_harness.hub_message(
            5, sender_name="BetaWolf", sender_program="codex", text="beta hub 二",
            created_ts=clock.next_s(), thread_id=thread_b["id"], recipients=("human",),
        ),
        # 反例：对 A 全部必须过滤（对 B 的串群探针由 B 视图断言兜底）
        *chat_harness.leak_probe_hub_rows(
            clock, other_thread="beta-1", known_sender="AlphaFox",
        ),
    ]
    _stub_runtime(monkeypatch)
    _stub_hub(monkeypatch, hub_rows)

    view_a = client.get("/api/chat/sessions/alpha-1/mail", headers=_headers())
    assert view_a.status_code == 200
    body_a = view_a.json()
    assert body_a["ungrouped"] is False
    # 时钟注入保证 ts 全序：本地两条在前，Hub 两条按注入顺序接上
    assert [row["text"] for row in body_a["messages"]] == [
        "alpha 本地一", "alpha 本地二", "alpha hub 一", "alpha hub 二",
    ]

    view_b = client.get("/api/chat/sessions/beta-1/mail", headers=_headers())
    assert view_b.status_code == 200
    texts_b = [row["text"] for row in view_b.json()["messages"]]
    # B 的合法视图：本地 + 本群 thread 两种形态；空 thread 即使花名命中也 fail-closed。
    assert texts_b == [
        "beta 本地一", "beta hub 一", "beta hub 二", "probe:别群 thread 串群",
    ]
    # 交叉断言：A 的内容一律不得漏进 B
    for leaked in ("alpha 本地一", "alpha 本地二", "alpha hub 一", "alpha hub 二"):
        assert leaked not in texts_b


def test_gate2_ungrouped_session_never_scans_hub(ledger, monkeypatch):
    """门 2：未入账 session 查时间线，Hub 两个 db 入口零调用。"""
    client = _client()
    clock = chat_harness.fake_clock()
    chat_ledger.append_message(
        "rogue-1", kind="me", sender="human", text="rogue 本地一", to=["RogueFox"],
        ts=clock.next_ms(),
    )
    chat_ledger.append_message(
        "rogue-1", kind="agent", sender="RogueFox", text="rogue 本地二", to=["human"],
        ts=clock.next_ms(),
    )
    monkeypatch.setattr(server.db, "status", lambda: {"available": True, "reason": None})
    calls: list[tuple] = []

    def probe_project(key):
        calls.append(("project_by_canonical_key", key))
        raise AssertionError("未分组不得查 Hub 项目")

    def probe_messages(*args, **kwargs):
        calls.append(("messages_for_canonical_project", args))
        raise AssertionError("未分组不得扫 Hub 消息")

    monkeypatch.setattr(server.db, "project_by_canonical_key", probe_project)
    monkeypatch.setattr(server.db, "messages_for_canonical_project", probe_messages)
    _stub_runtime(monkeypatch)
    monkeypatch.setattr(server.herdr_client, "list_sessions", lambda: [])
    monkeypatch.setattr(server.herdr_client, "recorded_session_workdirs", lambda _n: [])

    response = client.get("/api/chat/sessions/rogue-1/mail", headers=_headers())
    assert response.status_code == 200
    body = response.json()
    assert [row["text"] for row in body["messages"]] == ["rogue 本地一", "rogue 本地二"]
    assert body["ungrouped"] is True
    assert body["project"] is None
    assert calls == []


@pytest.mark.parametrize(
    "text",
    chat_harness.pytest_identity_notice_variants(),
    ids=["pytest-of-full-path", "tmp-pytest-prefix", "pytest-of-word-outside-tmp"],
)
def test_gate3_pytest_identity_notice_never_reaches_live_pane(monkeypatch, text):
    """门 3 反例：pytest 临时路径身份告知必须被 pane_send 丢弃，零 CLI 调用。"""
    chat_harness.restore_real_pane_send(monkeypatch)
    sent: list[list[str]] = []
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_run", lambda args, timeout=0: sent.append(args) or "",
    )
    result = herdr_client.pane_send("live-1", "w1:p1", text)
    assert result == {"available": True, "skipped": "throwaway_identity"}
    assert sent == []


@pytest.mark.parametrize(
    "text",
    [
        chat_harness.identity_notice_text("BrownDesert", "/home/fyc/github/agent-cockpit"),
        "普通提示：请继续处理手头任务",
    ],
    ids=["real-project-identity-notice", "plain-prompt"],
)
def test_gate3_legit_prompt_goes_through(monkeypatch, text):
    """门 3 正例：真实项目身份告知与非告知文本走正常 agent prompt 路径。"""
    chat_harness.restore_real_pane_send(monkeypatch)
    sent: list[list[str]] = []
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_run", lambda args, timeout=0: sent.append(args) or "",
    )
    result = herdr_client.pane_send("live-1", "w1:p1", text)
    assert result == {"available": True, "sent": text, "mode": "prompt"}
    assert sent == [["--session", "live-1", "agent", "prompt", "w1:p1", text]]
