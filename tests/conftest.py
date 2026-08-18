"""全局测试设施:把 settings 存储隔离到临时目录,避免真实 ~/.dashboard-data
配置影响 terminal/uploads 等模块的 live 读取语义。"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    from agent_cockpit import coordination
    from agent_cockpit import files
    from agent_cockpit import mail_projects
    from agent_cockpit import settings
    from agent_cockpit import team_inbox_router
    from agent_cockpit import team_sessions
    from agent_cockpit import upgrade_service
    monkeypatch.delenv(upgrade_service.ENABLE_ENV, raising=False)
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(settings, "_cache_mtime", -1.0)
    monkeypatch.setattr(mail_projects, "STATE_PATH", tmp_path / "mail-projects.json")
    monkeypatch.setattr(team_sessions, "STATE_PATH", tmp_path / "team-sessions.json")
    monkeypatch.setattr(
        team_inbox_router, "ROUTE_STATE", tmp_path / "team-inbox-route.json",
    )
    monkeypatch.setattr(coordination, "DB_PATH", tmp_path / "coordination.sqlite3")
    monkeypatch.setattr(files, "_custom_roots_file", lambda: tmp_path / "file-roots.json")
    from agent_cockpit import chat_roster
    from agent_cockpit import herdr_client
    monkeypatch.setattr(
        herdr_client, "_kimi_config_home", lambda: tmp_path / ".kimi-code",
    )
    monkeypatch.setattr(
        herdr_client, "pane_send",
        lambda *a, **k: {"available": True, "blocked_by_test": True},
    )
    monkeypatch.setattr(chat_roster, "LEADERS_DIR", tmp_path / "session-leaders")
    monkeypatch.setattr(chat_roster, "PANES_DIR", tmp_path / "session-panes")
