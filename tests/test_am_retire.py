"""Focused tests for agent-mail-tools/am-retire (Hub retire_agent + local tombstone)."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "agent-mail-tools"


def _load_am_retire():
    path = TOOLS / "am-retire"
    if not path.is_file():
        pytest.skip("am-retire not implemented yet")
    loader = importlib.machinery.SourceFileLoader("cockpit_am_retire", str(path))
    spec = importlib.util.spec_from_file_location(
        "cockpit_am_retire", str(path), loader=loader
    )
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _seed_registry(module, tmp_path: Path, *, instance: str = "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"):
    project = tmp_path / "proj"
    project.mkdir()
    project_key = str(project.resolve())
    registry_dir = tmp_path / module.slugify(project_key)
    registry_dir.mkdir()
    registry_file = registry_dir / f"codex--{instance}.json"
    identity = {
        "project_key": project_key,
        "project_slug": module.slugify(project_key),
        "agent": "codex",
        "instance": instance,
        "name": "FlowerCodex",
        "registration_token": "registration-token",
        "program": "codex",
        "model": "unknown",
        "hub": "http://hub",
    }
    registry_file.write_text(json.dumps(identity), encoding="utf-8")
    registry_file.chmod(0o600)
    return project_key, registry_file, identity


def test_retire_rejects_unsafe_instance_component():
    module = _load_am_retire()
    for bad in ("../evil", "a/b", "a\\b", "a b", ".", "-x", ""):
        with pytest.raises(SystemExit, match="仅允许"):
            module._validate_component(bad, "instance")


def test_retire_requires_exact_registry_path(tmp_path, monkeypatch):
    """必须按 agent+opaque instance 精确命中 registry，不模糊匹配。"""
    module = _load_am_retire()
    module.REGISTRY_DIR = tmp_path
    project = tmp_path / "proj"
    project.mkdir()
    project_key = str(project.resolve())
    (tmp_path / module.slugify(project_key)).mkdir()
    # 存在 main，但请求 i-bbbbbbbbbbbbbbbbbbbbbbbbbb → 未注册
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "tok"))
    monkeypatch.setattr(module.sys, "argv", [
        "am-retire",
        "--agent", "codex",
        "--instance", "i-bbbbbbbbbbbbbbbbbbbbbbbbbb",
        "--project", project_key,
    ])
    with pytest.raises(SystemExit, match="身份未注册|尚未注册"):
        module.main()


def test_retire_rejects_safe_but_non_opaque_instance(monkeypatch):
    module = _load_am_retire()
    monkeypatch.setattr(module.sys, "argv", [
        "am-retire", "--agent", "codex", "--instance", "main",
        "--project", "/tmp/project",
    ])
    with pytest.raises(SystemExit, match="opaque id"):
        module.main()


def test_retire_hub_success_writes_tombstone_keeps_file(tmp_path, monkeypatch, capsys):
    module = _load_am_retire()
    module.REGISTRY_DIR = tmp_path
    project_key, registry_file, identity = _seed_registry(module, tmp_path)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "tok"))
    monkeypatch.setattr(module, "mcp_call", lambda *_a, **_k: {})
    calls = []

    def tool(_hub, _token, name, args):
        calls.append((name, args))
        if name == "retire_agent":
            assert args["project_key"] == project_key
            assert args["agent_name"] == "FlowerCodex"
            assert args["registration_token"] == "registration-token"
            return {
                "status": "retired",
                "agent_name": "FlowerCodex",
                "project_key": project_key,
            }
        if name == "whois":
            return {
                "name": "FlowerCodex",
                "retired_at": "2026-08-11T12:00:00+00:00",
            }
        raise AssertionError(name)

    monkeypatch.setattr(module, "mcp_tool", tool)
    monkeypatch.setattr(module.sys, "argv", [
        "am-retire", "--agent", "codex", "--instance", "i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--project", project_key,
    ])
    module.main()

    assert [n for n, _ in calls][0] == "retire_agent"
    assert "retire_agent" in [n for n, _ in calls]
    assert registry_file.is_file()
    data = json.loads(registry_file.read_text(encoding="utf-8"))
    assert data["name"] == "FlowerCodex"
    assert data["instance"] == "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert data["registration_token"] == "registration-token"
    assert data.get("status") == "retired"
    assert data.get("retired_at")
    assert (registry_file.stat().st_mode & 0o777) == 0o600
    out = capsys.readouterr().out
    assert "退休成功" in out or "retired" in out.lower()
    assert "FlowerCodex" in out
    # 不得打印 token
    assert "registration-token" not in out


def test_retire_idempotent_when_already_retired_locally(tmp_path, monkeypatch, capsys):
    module = _load_am_retire()
    module.REGISTRY_DIR = tmp_path
    project_key, registry_file, identity = _seed_registry(module, tmp_path)
    tomb = dict(identity)
    tomb["status"] = "retired"
    tomb["retired_at"] = "2026-08-11T00:00:00+00:00"
    registry_file.write_text(json.dumps(tomb), encoding="utf-8")
    registry_file.chmod(0o600)

    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "tok"))
    monkeypatch.setattr(module, "mcp_call", lambda *_a, **_k: {})
    calls = []

    def tool(_hub, _token, name, args):
        calls.append(name)
        if name == "whois":
            return {
                "name": "FlowerCodex",
                "retired_at": "2026-08-11T00:00:00+00:00",
            }
        if name == "retire_agent":
            return {
                "status": "retired",
                "agent_name": "FlowerCodex",
                "project_key": project_key,
            }
        raise AssertionError(name)

    monkeypatch.setattr(module, "mcp_tool", tool)
    monkeypatch.setattr(module.sys, "argv", [
        "am-retire", "--agent", "codex", "--instance", "i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--project", project_key,
    ])
    module.main()
    # 幂等：允许 whois 短路或再次 retire_agent，但必须成功且文件仍在
    assert registry_file.is_file()
    data = json.loads(registry_file.read_text(encoding="utf-8"))
    assert data.get("status") == "retired" or data.get("retired_at")
    assert "registration-token" not in capsys.readouterr().out


def test_retire_hub_failure_does_not_tombstone(tmp_path, monkeypatch):
    module = _load_am_retire()
    module.REGISTRY_DIR = tmp_path
    project_key, registry_file, identity = _seed_registry(module, tmp_path)
    before = registry_file.read_text(encoding="utf-8")
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "tok"))
    monkeypatch.setattr(module, "mcp_call", lambda *_a, **_k: {})

    def tool(_hub, _token, name, _args):
        if name == "retire_agent":
            raise SystemExit("tool retire_agent failed: boom")
        raise AssertionError(name)

    monkeypatch.setattr(module, "mcp_tool", tool)
    monkeypatch.setattr(module.sys, "argv", [
        "am-retire", "--agent", "codex", "--instance", "i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--project", project_key,
    ])
    with pytest.raises(SystemExit):
        module.main()
    after = json.loads(registry_file.read_text(encoding="utf-8"))
    assert after == identity
    assert "retired" not in after
    assert registry_file.read_text(encoding="utf-8") == before


def test_retire_hub_success_local_write_fail_no_success_claim(
    tmp_path, monkeypatch, capsys,
):
    module = _load_am_retire()
    module.REGISTRY_DIR = tmp_path
    project_key, registry_file, identity = _seed_registry(module, tmp_path)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "tok"))
    monkeypatch.setattr(module, "mcp_call", lambda *_a, **_k: {})
    monkeypatch.setattr(
        module, "mcp_tool",
        lambda *_a, **_k: {
            "status": "retired",
            "agent_name": "FlowerCodex",
            "project_key": project_key,
        },
    )

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(module, "_atomic_write_identity", boom)
    monkeypatch.setattr(module.sys, "argv", [
        "am-retire", "--agent", "codex", "--instance", "i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--project", project_key,
    ])
    with pytest.raises(SystemExit, match="Hub|本地|tombstone|落盘"):
        module.main()
    # 原文件未变成谎报成功的“无 token 删除”
    assert registry_file.is_file()
    data = json.loads(registry_file.read_text(encoding="utf-8"))
    assert data["registration_token"] == "registration-token"


def test_retire_rejects_symlink_registry(tmp_path, monkeypatch):
    module = _load_am_retire()
    module.REGISTRY_DIR = tmp_path
    project = tmp_path / "proj"
    project.mkdir()
    project_key = str(project.resolve())
    reg_dir = tmp_path / module.slugify(project_key)
    reg_dir.mkdir()
    real = tmp_path / "outside.json"
    real.write_text("{}")
    real.chmod(0o600)
    link = reg_dir / "codex--i-aaaaaaaaaaaaaaaaaaaaaaaaaa.json"
    link.symlink_to(real)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "tok"))
    monkeypatch.setattr(module.sys, "argv", [
        "am-retire", "--agent", "codex", "--instance", "i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--project", project_key,
    ])
    with pytest.raises(SystemExit, match="symlink|拒绝"):
        module.main()


def test_retire_rejects_project_mismatch(tmp_path, monkeypatch):
    module = _load_am_retire()
    module.REGISTRY_DIR = tmp_path
    project_key, registry_file, identity = _seed_registry(module, tmp_path)
    bad = dict(identity)
    bad["project_key"] = "/other/project"
    registry_file.write_text(json.dumps(bad), encoding="utf-8")
    registry_file.chmod(0o600)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "tok"))
    monkeypatch.setattr(module.sys, "argv", [
        "am-retire", "--agent", "codex", "--instance", "i-aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--project", project_key,
    ])
    with pytest.raises(SystemExit, match="项目不匹配"):
        module.main()


@pytest.mark.parametrize(("field", "value"), [("agent", None), ("instance", None)])
def test_retire_requires_exact_registry_identity_fields(
    field, value, tmp_path, monkeypatch,
):
    module = _load_am_retire()
    module.REGISTRY_DIR = tmp_path
    project_key, registry_file, identity = _seed_registry(module, tmp_path)
    identity[field] = value
    registry_file.write_text(json.dumps(identity), encoding="utf-8")
    registry_file.chmod(0o600)
    monkeypatch.setattr(module, "load_client_config", lambda: ("http://hub", "tok"))
    monkeypatch.setattr(module.sys, "argv", [
        "am-retire", "--agent", "codex",
        "--instance", "i-aaaaaaaaaaaaaaaaaaaaaaaaaa", "--project", project_key,
    ])
    with pytest.raises(SystemExit, match=f"{field} 字段"):
        module.main()


def test_retire_requires_exact_registry_mode(tmp_path):
    module = _load_am_retire()
    path = tmp_path / "identity.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o640)
    with pytest.raises(SystemExit, match="0600"):
        module._secure_read_identity(path)
