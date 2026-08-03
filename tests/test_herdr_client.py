from unittest.mock import call

import herdr_client


def test_start_agent_fallback_selects_highest_numeric_pane(monkeypatch):
    """fallback 应把 w1:p10 视为比 w1:p9 更新，而不是按字符串排序。"""
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_agent_cmd", lambda *args: "codex")
    snapshots = iter([
        {"panes": []},
        {"panes": [
            {"pane_id": "w1:p10", "agent": None},
            {"pane_id": "w1:p9", "agent": None},
        ]},
    ])
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: next(snapshots))
    calls = []

    def fake_run(args, timeout=10):
        calls.append(call(args, timeout=timeout))
        return ""

    monkeypatch.setattr(herdr_client, "_run", fake_run)

    result = herdr_client.start_agent("demo", "/tmp/project")

    assert result["pane_id"] == "w1:p10"
    assert call(
        ["--session", "demo", "pane", "run", "w1:p10", "codex"],
        timeout=8,
    ) in calls


def test_start_agent_reports_missing_executable_before_split(monkeypatch):
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(herdr_client, "_snapshot_session", lambda session: {"panes": []})
    monkeypatch.setattr(
        herdr_client,
        "_find_agent_bin",
        lambda agent: "/definitely/missing/qoder",
    )
    monkeypatch.setattr(
        herdr_client,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应创建 pane")),
    )

    result = herdr_client.start_agent("demo", "/tmp/project", "qoder")

    assert result == {"available": True, "error": "qoder 未安装或不在 PATH"}
