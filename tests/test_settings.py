"""settings.py 配置存储与各模块接入测试。"""
import json
import os
import time

import pytest

import settings
import terminal
import uploads


@pytest.fixture(autouse=True)
def tmp_settings(tmp_path, monkeypatch):
    """设置文件隔离到 tmp_path,并清掉 mtime 缓存。"""
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(settings, "_cache_mtime", -1.0)
    return tmp_path


# ── 存储与校验 ──────────────────────────────────────────────────

def test_defaults_when_file_missing():
    cfg = settings.get()
    assert cfg["language"] == "zh"
    assert cfg["enabled_agents"] == settings.KNOWN_AGENTS
    assert cfg["upload_max_mb"] == 100
    assert cfg["term"]["max_terms"] == 16


def test_update_merges_and_persists(tmp_settings):
    out = settings.update({"language": "en", "upload_max_mb": 50})
    assert out["language"] == "en"
    assert out["upload_max_mb"] == 50
    # 落盘后可重读
    on_disk = json.loads((tmp_settings / "settings.json").read_text())
    assert on_disk["language"] == "en"
    assert settings.get()["language"] == "en"


def test_update_rejects_bad_language():
    with pytest.raises(ValueError, match="language"):
        settings.update({"language": "fr"})


def test_update_rejects_unknown_key():
    with pytest.raises(ValueError, match="未知配置项"):
        settings.update({"extra_roots": ["/tmp"]})


def test_update_rejects_unknown_agent():
    with pytest.raises(ValueError, match="未知 agent"):
        settings.update({"enabled_agents": ["codex", "gpt9"]})


def test_update_dir_agents_validates(tmp_path):
    with pytest.raises(ValueError, match="绝对路径"):
        settings.update({"dir_agents": {"relative/dir": "codex"}})
    with pytest.raises(ValueError, match="agent 未知"):
        settings.update({"dir_agents": {str(tmp_path): "gpt9"}})
    out = settings.update({"dir_agents": {str(tmp_path): "kimi"}})
    assert out["dir_agents"] == {str(tmp_path): "kimi"}


def test_update_clamps_numbers():
    out = settings.update({"upload_max_mb": 99999, "term": {"max_terms": 9999}})
    assert out["upload_max_mb"] == 2048
    assert out["term"]["max_terms"] == 64


def test_update_persists_validated_values(tmp_settings):
    """落盘必须是 _validate 规范化后的值,不能写未验证的原始输入。"""
    settings.update({"upload_max_mb": 99999, "term": {"max_terms": 9999}})
    on_disk = json.loads((tmp_settings / "settings.json").read_text())
    assert on_disk["upload_max_mb"] == 2048
    assert on_disk["term"]["max_terms"] == 64
    # 去重后落盘;重读时无需再经历 clamp 也是合法值
    settings.update({"enabled_agents": ["codex", "codex", "kimi"]})
    on_disk = json.loads((tmp_settings / "settings.json").read_text())
    assert on_disk["enabled_agents"] == ["codex", "kimi"]


def test_update_keeps_nested_settings_sparse(tmp_settings):
    settings.update({"term": {"max_terms": 9999}})

    on_disk = json.loads((tmp_settings / "settings.json").read_text())
    assert on_disk == {"term": {"max_terms": 64}}


def test_update_refreshes_global_cache(monkeypatch):
    settings.update({"language": "en"})
    monkeypatch.setattr(
        settings,
        "_read_merged",
        lambda: (_ for _ in ()).throw(AssertionError("写后应命中缓存")),
    )

    assert settings.get()["language"] == "en"


def test_update_serializes_concurrent_read_modify_write(tmp_settings, monkeypatch):
    """并发 update 不得丢失更新:整个 RMW 持锁,后写基于最新落盘值。"""
    import threading

    real_replace = os.replace
    first = True
    entered = threading.Event()
    gate = threading.Event()

    def slow_replace(src, dst):
        nonlocal first
        if first:
            first = False
            entered.set()
            gate.wait()
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", slow_replace)
    results = {}

    def do_lang():
        results["lang"] = settings.update({"language": "en"})

    def do_mb():
        results["mb"] = settings.update({"upload_max_mb": 50})

    ta = threading.Thread(target=do_lang)
    tb = threading.Thread(target=do_mb)
    ta.start()
    assert entered.wait(5)
    tb.start()
    time.sleep(0.3)  # 让 tb 进入 update:若未持锁会读到旧文件,持锁则阻塞
    gate.set()
    tb.join(10)
    ta.join(10)

    on_disk = json.loads((tmp_settings / "settings.json").read_text())
    assert on_disk["language"] == "en"
    assert on_disk["upload_max_mb"] == 50
    assert results["lang"]["language"] == "en"
    assert results["mb"]["upload_max_mb"] == 50


def test_corrupt_file_falls_back_to_defaults(tmp_settings):
    (tmp_settings / "settings.json").write_text("{not json")
    assert settings.get()["language"] == "zh"


# ── live 读取语义:只有显式配置才覆盖调用方默认 ────────────────

def test_live_readers_use_default_without_explicit_config():
    assert settings.upload_max_bytes(12345) == 12345
    assert settings.term_setting("write_timeout", 7.7) == 7.7


def test_live_readers_respect_explicit_config():
    settings.update({"upload_max_mb": 3, "term": {"write_timeout": 5.0}})
    assert settings.upload_max_bytes(12345) == 3 * 1024 * 1024
    assert settings.term_setting("write_timeout", 7.7) == 5.0


# ── 接入点:uploads / terminal 走设置 ─────────────────────────

def test_uploads_max_size_uses_settings():
    assert uploads._max_size() == uploads.MAX_SIZE  # 未配置时用模块常量
    settings.update({"upload_max_mb": 2})
    assert uploads._max_size() == 2 * 1024 * 1024


def test_terminal_cfg_uses_settings():
    assert terminal._term_cfg("max_terms", terminal.MAX_TERMS) == terminal.MAX_TERMS
    settings.update({"term": {"max_terms": 3}})
    assert terminal._term_cfg("max_terms", terminal.MAX_TERMS) == 3


# ── server 路由 ─────────────────────────────────────────────────

def test_settings_routes(monkeypatch):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}

    r = client.get("/api/settings", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["language"] == "zh"
    assert "known_agents" in body and "languages" in body

    r = client.put("/api/settings", headers=headers, json={"language": "ja"})
    assert r.status_code == 200
    assert r.json()["language"] == "ja"

    r = client.put("/api/settings", headers=headers, json={"language": "xx"})
    assert r.status_code == 400

    # 未认证被拒
    assert client.get("/api/settings").status_code == 401
