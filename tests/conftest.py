"""全局测试设施:把 settings 存储隔离到临时目录,避免真实 ~/.dashboard-data
配置影响 terminal/uploads 等模块的 live 读取语义。"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    import coordination
    import files
    import mail_projects
    import settings
    import team_inbox_router
    import team_sessions
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
