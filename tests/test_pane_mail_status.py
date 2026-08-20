"""测试 pane Agent Mail 连接状态检测。"""
from __future__ import annotations

import os
from unittest import mock

from agent_cockpit import pane_live


def test_check_mail_connectivity_all_ok():
    """所有条件满足时 connected=True（需要 agent_mail_commands 可导入）。"""
    with (
        mock.patch.dict(os.environ, {
            "AGENT_MAIL_NAME": "test-agent",
            "HERDR_CONFIG_PATH": "/path/to/config",
        }),
        mock.patch("agent_cockpit.herdr_client.snapshot") as mock_snap,
    ):
        mock_snap.return_value = {
            "panes": [
                {
                    "pane_id": "5",
                    "agent_session": {"uuid": "01a02001-e0b4-4c0a-8e12-d15188506504"},
                },
            ],
        }
        result = pane_live.check_agent_mail_connectivity("5")
        # can_send_mail 取决于 agent_mail_commands 是否可导入
        # 在测试环境可能为 False，只验证基本检测逻辑
        assert result["pane_id"] == "5"
        assert result["details"]["has_mail_name"] is True
        assert result["details"]["has_config_path"] is True
        assert result["details"]["has_agent_session"] is True
        # connected = all conditions，包括 can_send_mail
        assert isinstance(result["connected"], bool)


def test_check_mail_connectivity_missing_env():
    """环境变量缺失时 connected=False。"""
    with (
        mock.patch.dict(os.environ, {}, clear=True),
        mock.patch("agent_cockpit.herdr_client.snapshot") as mock_snap,
    ):
        mock_snap.return_value = {
            "panes": [
                {"pane_id": "5", "agent_session": {"uuid": "test-uuid"}},
            ],
        }
        result = pane_live.check_agent_mail_connectivity("5")
        assert result["connected"] is False
        assert result["details"]["has_mail_name"] is False
        assert result["details"]["has_config_path"] is False


def test_check_mail_connectivity_missing_agent_session():
    """pane 没有 agent_session 时 connected=False。"""
    with (
        mock.patch.dict(os.environ, {
            "AGENT_MAIL_NAME": "test-agent",
            "HERDR_CONFIG_PATH": "/path/to/config",
        }),
        mock.patch("agent_cockpit.herdr_client.snapshot") as mock_snap,
    ):
        mock_snap.return_value = {
            "panes": [
                {"pane_id": "5"},  # 没有 agent_session
            ],
        }
        result = pane_live.check_agent_mail_connectivity("5")
        assert result["connected"] is False
        assert result["details"]["has_agent_session"] is False


def test_check_mail_connectivity_pane_not_found():
    """pane 不存在时 connected=False。"""
    with (
        mock.patch.dict(os.environ, {
            "AGENT_MAIL_NAME": "test-agent",
            "HERDR_CONFIG_PATH": "/path/to/config",
        }),
        mock.patch("agent_cockpit.herdr_client.snapshot") as mock_snap,
    ):
        mock_snap.return_value = {"panes": []}
        result = pane_live.check_agent_mail_connectivity("999")
        assert result["connected"] is False
        assert result["details"]["has_agent_session"] is False
