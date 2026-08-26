"""测试 pane Agent Mail 连接状态检测。"""
from __future__ import annotations

from unittest import mock

from agent_cockpit import pane_live


def _snap(panes: list[dict]) -> dict:
    return {"available": True, "panes": panes}


def test_check_mail_connectivity_all_ok():
    """花名和活着的 agent 都在时 connected=True。"""
    with (
        mock.patch("agent_cockpit.herdr_client.snapshot") as mock_snap,
        mock.patch("agent_cockpit.chat_roster.get_pane_mail_name", return_value="BrownDesert"),
        mock.patch("agent_cockpit.pane_live._mail_send_module_available", return_value=True),
    ):
        mock_snap.return_value = _snap([
            {"session": "cockpit", "pane_id": "w1:p1", "agent": "grok"},
        ])
        result = pane_live.check_agent_mail_connectivity("cockpit", "w1:p1")
        assert result["pane_id"] == "w1:p1"
        assert result["details"]["has_mail_name"] is True
        assert result["details"]["has_config_path"] is True
        assert result["details"]["has_agent_session"] is True
        assert result["details"]["can_send_mail"] is True
        assert result["connected"] is True


def test_check_mail_connectivity_ignores_cockpit_process_env():
    """8790 进程没有 AGENT_MAIL_NAME 时，不得把整群标成未连接。"""
    with (
        mock.patch("agent_cockpit.herdr_client.snapshot") as mock_snap,
        mock.patch("agent_cockpit.chat_roster.get_pane_mail_name", return_value="GrayFalcon"),
        mock.patch("agent_cockpit.pane_live._mail_send_module_available", return_value=True),
    ):
        mock_snap.return_value = _snap([
            {"session": "cockpit", "pane_id": "w1:p6", "agent": "codex"},
        ])
        result = pane_live.check_agent_mail_connectivity("cockpit", "w1:p6")
        assert result["details"]["has_mail_name"] is True
        assert result["details"]["has_agent_session"] is True
        assert result["details"]["can_send_mail"] is True
        assert result["connected"] is True


def test_check_mail_connectivity_missing_mail_helper():
    """helper 未安装时 connected=False，且不导入执行 helper。"""
    with (
        mock.patch("agent_cockpit.herdr_client.snapshot") as mock_snap,
        mock.patch("agent_cockpit.chat_roster.get_pane_mail_name", return_value="GrayFalcon"),
        mock.patch("agent_cockpit.pane_live._mail_send_module_available", return_value=False),
    ):
        mock_snap.return_value = _snap([
            {"session": "cockpit", "pane_id": "w1:p6", "agent": "codex"},
        ])
        result = pane_live.check_agent_mail_connectivity("cockpit", "w1:p6")
        assert result["details"]["can_send_mail"] is False
        assert result["connected"] is False


def test_check_mail_connectivity_missing_mail_name():
    """花名不在花名册、快照也没有时 connected=False。"""
    with (
        mock.patch("agent_cockpit.herdr_client.snapshot") as mock_snap,
        mock.patch("agent_cockpit.chat_roster.get_pane_mail_name", return_value=""),
    ):
        mock_snap.return_value = _snap([
            {"session": "cockpit", "pane_id": "w1:p1", "agent": "grok"},
        ])
        result = pane_live.check_agent_mail_connectivity("cockpit", "w1:p1")
        assert result["connected"] is False
        assert result["details"]["has_mail_name"] is False


def test_check_mail_connectivity_missing_live_agent():
    """pane 还在但里面没有 agent 时 connected=False。"""
    with (
        mock.patch("agent_cockpit.herdr_client.snapshot") as mock_snap,
        mock.patch("agent_cockpit.chat_roster.get_pane_mail_name", return_value="BrownDesert"),
    ):
        mock_snap.return_value = _snap([
            {"session": "cockpit", "pane_id": "w1:p1"},
        ])
        result = pane_live.check_agent_mail_connectivity("cockpit", "w1:p1")
        assert result["connected"] is False
        assert result["details"]["has_agent_session"] is False


def test_check_mail_connectivity_pane_not_found():
    """pane 不在快照里时 connected=False。"""
    with (
        mock.patch("agent_cockpit.herdr_client.snapshot") as mock_snap,
        mock.patch("agent_cockpit.chat_roster.get_pane_mail_name", return_value="BrownDesert"),
    ):
        mock_snap.return_value = _snap([])
        result = pane_live.check_agent_mail_connectivity("cockpit", "w1:p1")
        assert result["connected"] is False
        assert result["details"]["has_agent_session"] is False
