"""全局测试设施:把 settings 存储隔离到临时目录,避免真实 ~/.dashboard-data
配置影响 terminal/uploads 等模块的 live 读取语义。"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    import settings
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(settings, "_cache_mtime", -1.0)
