"""持续工作 harness：有下一步才叫醒空闲 Leader，不叫醒 working，有冷却。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent_cockpit import persist_work
from agent_cockpit import server


HANDOFF = """## 下一步

1. 瀑布流对谁说。
2. Codex 花名。
"""


def test_parse_next_steps_reads_numbered_section():
    assert persist_work.parse_next_steps(HANDOFF) == ["瀑布流对谁说。", "Codex 花名。"]
    assert persist_work.parse_next_steps("## 当前状态\n- 无") == []


def test_wake_prompt_never_hardcodes_human_and_leader_does_not_mail_self():
    leader = persist_work.wake_prompt(
        "cockpit-1", "继续收口。",
        wake_mail_name="TopazOwl", leader_mail_name="TopazOwl",
    )
    assert "HumanOverseer" not in leader
    assert "不要给自己发 Agent Mail" in leader
    assert "mail-send" not in leader

    contributor = persist_work.wake_prompt(
        "cockpit-1", "继续收口。",
        wake_mail_name="SilverPine", leader_mail_name="TopazOwl",
    )
    assert "HumanOverseer" not in contributor
    assert "mail-send --to leader --thread cockpit-1" in contributor


def test_plan_wakes_only_idle_leader_in_bound_session():
    panes = [
        {"session": "cockpit", "pane_id": "w1:p1", "agent": "grok",
         "mail_name": "BrownDesert", "agent_status": "idle"},
        {"session": "cockpit", "pane_id": "w1:p5", "agent": "kimi",
         "mail_name": "FoggyBasin", "agent_status": "idle"},
        {"session": "default", "pane_id": "w1:p1", "agent": "codex",
         "mail_name": "X", "agent_status": "idle"},
    ]
    wakes = persist_work.plan_wakes(
        panes=panes,
        bound_sessions={"cockpit"},
        leaders={"cockpit": "BrownDesert"},
        next_item="瀑布流对谁说。",
        now=1_000.0,
        last_wake={},
        min_gap=90.0,
    )
    assert [(w.session, w.pane_id, w.mail_name) for w in wakes] == [
        ("cockpit", "w1:p1", "BrownDesert"),
    ]


def test_plan_wakes_skips_working_and_respects_cooldown():
    pane = {
        "session": "cockpit", "pane_id": "w1:p1", "agent": "grok",
        "mail_name": "BrownDesert", "agent_status": "working",
    }
    assert persist_work.plan_wakes(
        panes=[pane], bound_sessions={"cockpit"},
        leaders={"cockpit": "BrownDesert"}, next_item="x",
        now=100.0, last_wake={}, min_gap=90.0,
    ) == []
    pane["agent_status"] = "idle"
    assert persist_work.plan_wakes(
        panes=[pane], bound_sessions={"cockpit"},
        leaders={"cockpit": "BrownDesert"}, next_item="x",
        now=100.0, last_wake={"cockpit|w1:p1": 50.0}, min_gap=90.0,
    ) == []
    assert persist_work.DEFAULT_MIN_GAP == 90.0


def test_plan_wakes_named_members_not_only_leader():
    panes = [
        {"session": "cockpit", "pane_id": "w1:p1", "agent": "grok",
         "mail_name": "BrownDesert", "agent_status": "idle"},
        {"session": "cockpit", "pane_id": "w1:p2", "agent": "codex",
         "mail_name": "codex-luna-agent-cockpit", "agent_status": "idle"},
        {"session": "cockpit", "pane_id": "w1:p5", "agent": "kimi",
         "mail_name": "FoggyBasin", "agent_status": "idle"},
    ]
    wakes = persist_work.plan_wakes(
        panes=panes,
        bound_sessions={"cockpit"},
        leaders={"cockpit": "BrownDesert"},
        next_items=[
            "FoggyBasin 真机确认 H5 键盘。",
            "Codex 扫 3.0 群聊回归。",
        ],
        now=1_000.0,
        last_wake={},
        min_gap=90.0,
    )
    assert [(w.pane_id, w.mail_name) for w in wakes] == [
        ("w1:p5", "FoggyBasin"),
        ("w1:p2", "codex-luna-agent-cockpit"),
    ]


def test_waiting_for_boss_is_watch_and_does_not_wake_alone():
    assert persist_work.is_watch_item("等 Boss 拍板：发版，或指定下一刀")
    assert persist_work.is_watch_item("等待 Boss 单独确认是否允许 push 20e1a2c")
    assert persist_work.is_watch_item("Boss 明确授权后再 push")
    panes = [{
        "session": "cockpit", "pane_id": "w1:p1", "agent": "grok",
        "mail_name": "BrownDesert", "agent_status": "idle",
    }]
    assert persist_work.plan_wakes(
        panes=panes,
        bound_sessions={"cockpit"},
        leaders={"cockpit": "BrownDesert"},
        next_items=[
            "BrownDesert 盯交付，空闲就分下一刀，不把下一步写空。",
            "等 Boss 拍板：发版，或指定下一刀（滚动截图 / Hub 身份 / 看现场流）。",
        ],
        now=1_000.0,
        last_wake={},
        min_gap=90.0,
    ) == []


def test_plan_wakes_skips_when_only_watch_items():
    panes = [{
        "session": "cockpit", "pane_id": "w1:p1", "agent": "grok",
        "mail_name": "BrownDesert", "agent_status": "idle",
    }]
    assert persist_work.plan_wakes(
        panes=panes,
        bound_sessions={"cockpit"},
        leaders={"cockpit": "BrownDesert"},
        next_items=["BrownDesert 盯交付，空闲就分下一刀，不把下一步写空。"],
        now=1_000.0,
        last_wake={},
        min_gap=90.0,
    ) == []


def test_plan_wakes_skips_leader_watch_when_named_assignee_working():
    panes = [
        {"session": "cockpit", "pane_id": "w1:p1", "agent": "grok",
         "mail_name": "BrownDesert", "agent_status": "idle"},
        {"session": "cockpit", "pane_id": "w1:p5", "agent": "kimi",
         "mail_name": "FoggyBasin", "agent_status": "working"},
    ]
    wakes = persist_work.plan_wakes(
        panes=panes,
        bound_sessions={"cockpit"},
        leaders={"cockpit": "BrownDesert"},
        next_items=[
            "FoggyBasin 给弹窗输入条加展开收起。",
            "BrownDesert 盯交付，空闲就分下一刀，不把下一步写空。",
        ],
        now=1_000.0,
        last_wake={},
        min_gap=90.0,
    )
    assert wakes == []
    panes[1]["agent_status"] = "idle"
    wakes = persist_work.plan_wakes(
        panes=panes,
        bound_sessions={"cockpit"},
        leaders={"cockpit": "BrownDesert"},
        next_items=[
            "FoggyBasin 给弹窗输入条加展开收起。",
            "BrownDesert 盯交付，空闲就分下一刀，不把下一步写空。",
        ],
        now=1_000.0,
        last_wake={},
        min_gap=90.0,
    )
    assert [(w.pane_id, w.mail_name) for w in wakes] == [
        ("w1:p5", "FoggyBasin"),
        ("w1:p1", "BrownDesert"),
    ]


def test_tick_sends_once_and_persists(tmp_path: Path):
    sent: list[tuple] = []
    state = tmp_path / "persist-work.json"
    panes = [{
        "session": "cockpit", "pane_id": "w1:p1", "agent": "grok",
        "mail_name": "BrownDesert", "agent_status": "idle",
    }]
    wakes = persist_work.tick(
        now=10.0,
        panes=panes,
        bound_sessions={"cockpit"},
        leaders={"cockpit": "BrownDesert"},
        handoff_text=HANDOFF,
        send=lambda *a: sent.append(a) or {"available": True},
        state_path=state,
        min_gap=5.0,
    )
    assert len(wakes) == 1
    assert sent[0][0] == "cockpit"
    assert "瀑布流对谁说" in sent[0][2]
    assert "不要给自己发 Agent Mail" in sent[0][2]
    assert "HumanOverseer" not in sent[0][2]
    again = persist_work.tick(
        now=12.0,
        panes=panes,
        bound_sessions={"cockpit"},
        leaders={"cockpit": "BrownDesert"},
        handoff_text=HANDOFF,
        send=lambda *a: sent.append(a) or {"available": True},
        state_path=state,
        min_gap=5.0,
    )
    assert again == []
    assert len(sent) == 1


def test_bound_sessions_for_project_ignores_other_workspaces(tmp_path: Path):
    ours = tmp_path / "ours"
    theirs = tmp_path / "theirs"
    ours.mkdir()
    theirs.mkdir()
    (ours / ".agent-memory-project").write_text("agent-cockpit-next\n", encoding="utf-8")
    (theirs / ".agent-memory-project").write_text("pitapat-video-platform\n", encoding="utf-8")
    sessions = persist_work.bound_sessions_for_project(
        workspaces=[
            {"id": "ws_aaa111111111", "path": str(ours)},
            {"id": "ws_bbb222222222", "path": str(theirs)},
        ],
        threads=[
            {"workspace_id": "ws_aaa111111111", "herdr_session": "cockpit"},
            {"workspace_id": "ws_bbb222222222", "herdr_session": "pitapat-video-platform-1"},
        ],
        project="agent-cockpit-next",
    )
    assert sessions == {"cockpit"}


def test_tick_persist_work_wakes_bound_idle_leader(tmp_path: Path, monkeypatch):
    sent: list[tuple] = []
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".agent-memory-project").write_text("agent-cockpit-next\n", encoding="utf-8")
    handoff = tmp_path / "handoff.md"
    handoff.write_text("## 下一步\n\n1. 把 persist-work 接上。\n", encoding="utf-8")
    monkeypatch.setattr(server.persist_work, "DEFAULT_HANDOFF", handoff)
    monkeypatch.setenv("COCKPIT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        server, "_herdr_runtime_snapshot",
        lambda: {"panes": [
            {
                "session": "cockpit", "pane_id": "w1:p1", "agent": "grok",
                "mail_name": "BrownDesert", "agent_status": "idle",
            },
            {
                "session": "pitapat-video-platform-1", "pane_id": "w1:p2",
                "agent": "codex", "mail_name": "DarkGlacier", "agent_status": "idle",
            },
        ]},
    )
    monkeypatch.setattr(server, "_enrich_board_identities", lambda snap: snap)
    monkeypatch.setattr(
        server.chat_ledger, "list_workspaces",
        lambda: [
            {"id": "ws_aaa111111111", "path": str(repo)},
            {"id": "ws_bbb222222222", "path": str(tmp_path / "other")},
        ],
    )
    monkeypatch.setattr(
        server.chat_ledger, "list_threads",
        lambda: [
            {"workspace_id": "ws_aaa111111111", "herdr_session": "cockpit"},
            {"workspace_id": "ws_bbb222222222", "herdr_session": "pitapat-video-platform-1"},
        ],
    )
    monkeypatch.setattr(
        server.chat_roster, "get_session_leader",
        lambda _name: {"leader_mail_name": "BrownDesert"},
    )
    monkeypatch.setattr(
        server.herdr_client, "pane_send",
        lambda *a, **k: sent.append(a) or {"available": True},
    )
    server._tick_persist_work()
    assert len(sent) == 1
    assert sent[0][:2] == ("cockpit", "w1:p1")
    assert sent[0][3] == "prompt"
    assert "persist-work" in sent[0][2]


def test_lifespan_starts_and_cancels_persist_work(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_persist() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def hang() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(server, "_persist_work_loop", fake_persist)
    monkeypatch.setattr(server, "_poll_live_state", hang)
    monkeypatch.setattr(server, "_poll_message_state", hang)
    monkeypatch.setattr(server, "_worktree_cleanup_loop", hang)
    monkeypatch.setattr(server, "_identity_retirement_loop", hang)
    monkeypatch.setattr(server, "_release_all_zoom_leases", lambda: None)

    async def exercise() -> None:
        async with server.lifespan(server.app):
            await asyncio.wait_for(started.wait(), timeout=2)
            assert server._persist_work_task is not None
            assert not server._persist_work_task.done()
        await asyncio.wait_for(cancelled.wait(), timeout=2)
        assert server._persist_work_task is None

    asyncio.run(exercise())
