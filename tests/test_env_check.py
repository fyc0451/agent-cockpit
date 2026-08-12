"""GET /api/env-check 环境自检接口测试(设置页首次引导用)。"""
from fastapi.testclient import TestClient

from agent_cockpit import herdr_client
import server
from agent_cockpit import settings


def _client(monkeypatch, herdr_ok=True):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "", raising=False)
    monkeypatch.setattr(herdr_client, "HERDR_BIN", "/fake/herdr" if herdr_ok else "herdr")
    monkeypatch.setattr(herdr_client, "is_available", lambda: herdr_ok)
    monkeypatch.setattr(
        server, "_agent_mail_status", lambda: {"available": False, "reason": "未安装"}
    )
    return TestClient(server.app, client=("127.0.0.1", 50000))


def test_env_check_reports_component_status(monkeypatch, tmp_path):
    # 只有一个 agent "已安装"(落在真实路径),其余 _find_agent_bin 兜底裸名
    kimi = tmp_path / "kimi"
    kimi.write_text("#!/bin/sh\n")

    def fake_find(name: str) -> str:
        return str(kimi) if name == "kimi" else name

    monkeypatch.setattr(herdr_client, "_find_agent_bin", fake_find)
    client = _client(monkeypatch)

    r = client.get("/api/env-check")
    assert r.status_code == 200
    data = r.json()

    assert data["herdr"] == {"installed": True, "path": "/fake/herdr"}
    assert set(data["agents"]) == set(settings.KNOWN_AGENTS)
    assert data["agents"]["kimi"] == {"installed": True, "path": str(kimi)}
    assert data["agents"]["codex"] == {"installed": False, "path": ""}
    assert data["agent_mail"]["available"] is False


def test_env_check_herdr_missing(monkeypatch):
    monkeypatch.setattr(herdr_client, "_find_agent_bin", lambda name: name)
    client = _client(monkeypatch, herdr_ok=False)

    data = client.get("/api/env-check").json()
    assert data["herdr"] == {"installed": False, "path": ""}
    assert all(not a["installed"] for a in data["agents"].values())


def test_agent_mail_requirement_covers_missing_and_read_only_hub(monkeypatch):
    monkeypatch.setattr(
        server,
        "_agent_mail_status",
        lambda: {"available": False, "reason": "数据库不存在"},
    )
    missing = server._agent_mail_requirement()
    assert missing["code"] == "agent_mail_required"
    assert "数据库不存在" in missing["error"]

    monkeypatch.setattr(
        server,
        "_agent_mail_status",
        lambda: {
            "available": True,
            "write_available": False,
            "write_reason": "Hub 不可连接",
        },
    )
    read_only = server._agent_mail_requirement()
    assert "Hub 不可连接" in read_only["error"]

    monkeypatch.setattr(
        server,
        "_agent_mail_status",
        lambda: {"available": True, "write_available": True},
    )
    assert server._agent_mail_requirement() is None


def test_remote_hub_allows_agent_creation_without_local_database(monkeypatch):
    monkeypatch.setattr(
        server.db,
        "status",
        lambda: {"available": False, "reason": "数据库不存在"},
    )
    monkeypatch.setattr(
        server.hub_client,
        "status",
        lambda: {"available": True, "reason": None},
    )

    assert server._agent_mail_status() == {
        "available": False,
        "reason": "数据库不存在",
        "read_available": False,
        "write_available": True,
        "write_reason": None,
    }
    assert server._agent_mail_requirement() is None
