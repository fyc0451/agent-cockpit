from __future__ import annotations

import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import stat
import tomllib
from pathlib import Path

import pytest

from agent_cockpit import (
    db, files, herdr_client, next_profile, project_discovery_service,
)
import server


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "next_dev.py"
EPHEMERAL_SCRIPT = ROOT / "scripts" / "next_ephemeral_server.py"


def test_fresh_install_docs_order_next_before_legacy() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    next_doc = (ROOT / "docs" / "NEXT-DEVELOPMENT.md").read_text(
        encoding="utf-8"
    )
    commands = (
        'cd "$HOME/github/agent-cockpit-next"',
        "python3 -m venv .venv",
        "npm ci --prefix web",
        "npm run --prefix web build",
        "scripts/next_dev.py check",
        "scripts/next_dev.py start",
        "http://127.0.0.1:18790",
    )

    for document in (readme, next_doc):
        positions = [document.index(marker) for marker in commands]
        assert positions == sorted(positions)
        for label in (
            "选择代码目录",
            "检查并继续",
            "确认添加",
            "继续创建工作空间",
            "创建并打开",
            "开始任务",
            "继续输入下一条任务",
        ):
            assert label in document

    assert readme.index("Cockpit Next 2.0") < readme.index("Legacy 0.3.x")
    assert "~/.config/agent-cockpit-next/cockpit.token" in readme
    assert "不要使用下文旧版的 `COCKPIT_TOKEN`" in readme
    next_section = readme.split("## Legacy 0.3.x", 1)[0]
    legacy_section = readme.split("## Legacy 0.3.x", 1)[1]
    assert "openssl rand -hex 32" in next_section
    assert "COCKPIT_HOST=0.0.0.0" in next_section
    assert "<本机局域网IP>" in next_section
    assert "不要对 Next 执行 `install.sh`" in next_section
    assert "pip install -r requirements.txt" in next_section
    assert "openssl rand -hex 32" not in legacy_section
    assert "`COCKPIT_TOKEN`" in legacy_section
    assert "install.sh" in legacy_section
    for document in (readme, next_doc):
        assert "origin/next" in document
        assert "reviewed" in document
        assert document.index("scripts/next_dev.py start") < document.index(
            "git clone --branch next"
        )
        assert document.index("openssl rand -hex 32") < document.index(
            "git clone --branch next"
        )


def test_user_guide_declares_legacy_and_points_to_next() -> None:
    guide = (ROOT / "docs" / "USER-GUIDE.md").read_text(encoding="utf-8")
    head = guide[:800]
    assert "Legacy 0.3.x" in head
    assert "8790" in head
    assert "Cockpit Next 2.0" in head
    assert "README.md" in head
    assert "NEXT-DEVELOPMENT.md" in head


def _assert_text_error(captured: pytest.CaptureFixture[str], code: str) -> None:
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert lines[0] == code
    assert len(lines) >= 2
    assert lines[1].strip()


def module():
    spec = importlib.util.spec_from_file_location("next_dev", SCRIPT)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def ephemeral_module():
    spec = importlib.util.spec_from_file_location(
        "next_ephemeral_server", EPHEMERAL_SCRIPT,
    )
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_example_is_complete_and_valid_for_declared_home(tmp_path: Path) -> None:
    gate = module()
    values = gate.load_env(ROOT / ".env.next.example", home=tmp_path)
    repo = Path(values["COCKPIT_NEXT_WORKTREE"])
    repo.mkdir(parents=True)
    (repo / ".agent-memory-project").write_text(
        "agent-cockpit-next\n", encoding="ascii",
    )
    assert gate.validate(values, repo=repo, home=tmp_path, check_git=False) == values
    assert values["COCKPIT_PROJECT_ROOT"] == str(tmp_path / "github")


def test_project_root_configuration_reuses_custom_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "code"
    root.mkdir()
    monkeypatch.setenv("COCKPIT_NEXT_PROFILE", "1")
    monkeypatch.setenv("COCKPIT_PROJECT_ROOT", str(root))

    groups = files.allowed_root_groups()

    assert groups["custom"] == [str(root)]
    assert project_discovery_service.FilesRootReader().local_roots() == (root,)
    assert groups["system"]


@pytest.mark.parametrize("profile", (None, "ephemeral"))
def test_project_root_configuration_does_not_change_non_fixed_allowlist(
    profile: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "code"
    root.mkdir()
    if profile is None:
        monkeypatch.delenv("COCKPIT_NEXT_PROFILE", raising=False)
    else:
        monkeypatch.setenv("COCKPIT_NEXT_PROFILE", profile)
    monkeypatch.setenv("COCKPIT_PROJECT_ROOT", str(root))

    assert str(root) not in files.allowed_root_groups()["custom"]


def test_project_root_is_independent_of_cockpit_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = module()
    home = tmp_path / "home"
    checkout = home / "github" / "agent-cockpit-next"
    project_root = tmp_path / "mounted-projects"
    checkout.mkdir(parents=True)
    project_root.mkdir()
    (checkout / ".agent-memory-project").write_text(
        "agent-cockpit-next\n", encoding="ascii",
    )
    values = gate.expected(home)
    values["COCKPIT_PROJECT_ROOT"] = str(project_root)

    assert gate.validate(
        values, repo=checkout, home=home, check_git=False,
    ) == values
    monkeypatch.setattr(
        next_profile.Path, "home", classmethod(lambda _cls: home),
    )
    next_profile.validate_server_environment(checkout, values)


def test_project_root_configuration_fails_closed_for_invalid_values(
    tmp_path: Path,
) -> None:
    gate = module()
    repo = tmp_path / "code" / "agent-cockpit-next"
    repo.mkdir(parents=True)
    (repo / ".agent-memory-project").write_text(
        "agent-cockpit-next\n", encoding="ascii",
    )

    cases = (
        (str(tmp_path), "project_root_unsafe"),
        (str(tmp_path / "missing"), "project_root_missing"),
        (str(tmp_path / "plain.txt"), "project_root_invalid"),
        ("relative/code", "project_root_invalid"),
    )
    (tmp_path / "plain.txt").write_text("not a directory\n", encoding="ascii")
    for value, code in cases:
        values = gate.expected(tmp_path)
        values["COCKPIT_PROJECT_ROOT"] = value
        with pytest.raises(gate.IsolationError, match=code):
            gate.validate(values, repo=repo, home=tmp_path, check_git=False)


@pytest.mark.parametrize(
    ("key", "bad", "code"),
    [
        ("COCKPIT_PORT", "8790", "production_port"),
        ("COCKPIT_SYSTEMD_UNIT", "agent-cockpit.service", "production_unit"),
        ("AGENT_MAIL_PROJECT", "/home/fyc/github/agent-cockpit", "value_mismatch:AGENT_MAIL_PROJECT"),
        ("HERDR_SESSION", "github-agent-cockpit", "value_mismatch:HERDR_SESSION"),
        ("COCKPIT_UPGRADE_V2_ENABLED", "1", "value_mismatch:COCKPIT_UPGRADE_V2_ENABLED"),
        ("COCKPIT_B0_MODE", "on", "value_mismatch:COCKPIT_B0_MODE"),
        ("COCKPIT_HERDR_STATE_MODE", "on", "value_mismatch:COCKPIT_HERDR_STATE_MODE"),
    ],
)
def test_production_or_active_values_fail_closed(
    key: str, bad: str, code: str,
) -> None:
    gate = module()
    values = gate.expected()
    values[key] = bad
    with pytest.raises(gate.IsolationError, match=code):
        gate.validate(values, repo=ROOT, check_git=False)


def test_missing_and_unknown_env_keys_are_rejected() -> None:
    gate = module()
    values = gate.expected()
    del values["COCKPIT_DATA_DIR"]
    with pytest.raises(gate.IsolationError, match="env_keys_mismatch"):
        gate.validate(values, repo=ROOT, check_git=False)

    values = gate.expected()
    del values["COCKPIT_PROJECT_ROOT"]
    with pytest.raises(
        gate.IsolationError,
        match="next_profile_missing:COCKPIT_PROJECT_ROOT",
    ):
        gate.validate(values, repo=ROOT, check_git=False)


def test_production_and_overlapping_runtime_roots_are_rejected(tmp_path: Path) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    values["COCKPIT_DATA_DIR"] = str(tmp_path / "dashboard-data")
    with pytest.raises(gate.IsolationError, match="production_path"):
        gate.validate(values, repo=ROOT, home=tmp_path, check_git=False)

    values = gate.expected(tmp_path)
    values["COCKPIT_UPLOADS_DIR"] = str(
        Path(values["COCKPIT_DATA_DIR"]) / "uploads"
    )
    with pytest.raises(gate.IsolationError, match="runtime_roots_overlap"):
        gate.validate(values, repo=ROOT, home=tmp_path, check_git=False)
    values = gate.expected()
    values["COCKPIT_TOKEN"] = "secret"
    with pytest.raises(gate.IsolationError, match="env_keys_mismatch"):
        gate.validate(values, repo=ROOT, check_git=False)


def test_loader_rejects_duplicates_shell_expansion_and_non_ascii(tmp_path: Path) -> None:
    gate = module()
    cases = (
        "A=1\nA=2\n",
        "A=$(id)\n",
        "A=\u5bc6\u94a5\n",
    )
    for index, body in enumerate(cases):
        path = tmp_path / f"bad-{index}.env"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(gate.IsolationError):
            gate.load_env(path, home=tmp_path)


def test_runtime_roots_are_private_and_distinct(tmp_path: Path) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    gate.ensure_runtime_roots(values)
    roots = [Path(values[key]) for key in (
        "COCKPIT_DATA_DIR", "COCKPIT_CONFIG_DIR",
        "COCKPIT_STATE_DIR", "COCKPIT_UPLOADS_DIR",
    )]
    assert len(set(roots)) == 4
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in roots)


def test_private_herdr_config_generation_upgrade_and_idempotency(
    tmp_path: Path,
) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    gate.ensure_runtime_roots(values)
    config = Path(values["HERDR_CONFIG_PATH"])
    config.parent.mkdir(mode=0o700)
    config.write_text(
        """onboarding = false

[ui]
agent_panel_sort = "spaces"

[ui.toast]
delivery = "terminal"

[theme]
name = "catppuccin"
auto_switch = false
""",
        encoding="ascii",
    )
    config.chmod(0o600)

    assert next_profile.ensure_private_herdr_config(values) == config
    parsed = tomllib.loads(config.read_text(encoding="ascii"))
    assert parsed == {
        "onboarding": False,
        "ui": {
            "agent_panel_sort": "spaces",
            "toast": {"delivery": "terminal"},
        },
        "theme": {"name": "catppuccin", "auto_switch": False},
        "terminal": {"default_shell": "/bin/sh", "shell_mode": "non_login"},
    }
    info = config.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_nlink == 1
    assert stat.S_IMODE(config.parent.lstat().st_mode) == 0o700

    before = (config.read_bytes(), info.st_ino, info.st_mtime_ns)
    assert next_profile.ensure_private_herdr_config(values) == config
    after = config.lstat()
    assert (config.read_bytes(), after.st_ino, after.st_mtime_ns) == before


def test_private_herdr_config_rejects_unsafe_file_and_parent(
    tmp_path: Path,
) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    gate.ensure_runtime_roots(values)
    config = Path(values["HERDR_CONFIG_PATH"])
    config.parent.mkdir(mode=0o700)
    config.write_text("unchanged\n", encoding="ascii")
    config.chmod(0o644)

    with pytest.raises(next_profile.NextProfileError, match="next_herdr_config_unsafe"):
        next_profile.ensure_private_herdr_config(values)
    assert config.read_text(encoding="ascii") == "unchanged\n"

    config.unlink()
    config.parent.chmod(0o755)
    with pytest.raises(next_profile.NextProfileError, match="next_herdr_config_unsafe"):
        next_profile.ensure_private_herdr_config(values)
    assert not config.exists()


def test_private_herdr_config_random_name_failure_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    gate.ensure_runtime_roots(values)
    config = Path(values["HERDR_CONFIG_PATH"])
    config.parent.mkdir(mode=0o700)
    config.write_text("unchanged\n", encoding="ascii")
    config.chmod(0o600)
    monkeypatch.setattr(
        next_profile.os,
        "urandom",
        lambda _size: (_ for _ in ()).throw(OSError("random unavailable")),
    )

    with pytest.raises(
        next_profile.NextProfileError,
        match="next_herdr_config_write_failed",
    ):
        next_profile.ensure_private_herdr_config(values)
    assert config.read_text(encoding="ascii") == "unchanged\n"
    assert list(config.parent.glob(".config.toml.tmp-*")) == []


@pytest.mark.parametrize("ready_restart", (False, True))
def test_ephemeral_config_failure_invalidates_ready_evidence_first(
    ready_restart: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launcher = ephemeral_module()
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    token = "a" * 32
    old_catalog: bytes | None = None
    if ready_restart:
        assert launcher._initialize_layout(root) is True
        environment = launcher._environment(
            root, 12345, token, source_sha="a" * 40,
        )
        lock = launcher.InstanceLock(environment).acquire()
        lock.release()
        config = root / "herdr" / "config.toml"
        config.write_text("onboarding = false\n", encoding="ascii")
        config.chmod(0o600)
        next_profile.activate_ephemeral_runtime_root(root)
        next_profile.finalize_ephemeral_runtime_root(environment)
        old_catalog = (root / next_profile.EPHEMERAL_CATALOG).read_bytes()

    observed_states: list[str] = []

    def fail_config(_environment: dict[str, str]) -> None:
        marker = json.loads(
            (root / next_profile.EPHEMERAL_MARKER).read_text(encoding="ascii")
        )
        observed_states.append(marker["state"])
        raise next_profile.NextProfileError("next_herdr_config_write_failed")

    monkeypatch.setattr(launcher.os, "setsid", lambda: None)
    monkeypatch.setattr(launcher.secrets, "token_hex", lambda _size: token)
    monkeypatch.setattr(
        launcher.next_profile, "ensure_private_herdr_config", fail_config,
    )

    assert launcher.main([
        "--runtime-root", str(root), "--source-sha", "a" * 40,
    ]) == 2
    assert capsys.readouterr().err == "next_herdr_config_write_failed\n"
    marker = json.loads(
        (root / next_profile.EPHEMERAL_MARKER).read_text(encoding="ascii")
    )
    assert observed_states == ["running"]
    assert marker["state"] == "running"
    assert marker["catalog_sha256"] is None
    if old_catalog is None:
        assert not (root / next_profile.EPHEMERAL_CATALOG).exists()
    else:
        assert (root / next_profile.EPHEMERAL_CATALOG).read_bytes() == old_catalog
    assert not next_profile.ephemeral_herdr_config_home(root).exists()


def test_runtime_symlink_is_rejected(tmp_path: Path) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    target = tmp_path / "real"
    target.mkdir()
    data = Path(values["COCKPIT_DATA_DIR"])
    data.parent.mkdir(parents=True)
    data.symlink_to(target, target_is_directory=True)
    with pytest.raises(gate.IsolationError, match="runtime_symlink"):
        gate.ensure_runtime_roots(values)


def test_optional_next_token_file_is_absent_or_loaded(tmp_path: Path) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    assert gate.load_cockpit_token(values) is None

    path = gate.token_file_path(values)
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("a" * 64 + "\n", encoding="ascii")
    path.chmod(0o600)
    assert gate.load_cockpit_token(values) == "a" * 64


@pytest.mark.parametrize(
    "host", ("::1", "localhost", "192.168.1.5", "0.0.0.0 "),
)
def test_fixed_next_host_is_locked_to_loopback_or_all_ipv4(
    host: str, tmp_path: Path,
) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    values["COCKPIT_HOST"] = host
    repo = Path(values["COCKPIT_NEXT_WORKTREE"])
    repo.mkdir(parents=True)
    (repo / ".agent-memory-project").write_text(
        "agent-cockpit-next\n", encoding="ascii",
    )
    with pytest.raises(gate.IsolationError, match="value_mismatch:COCKPIT_HOST"):
        gate.validate(values, repo=repo, home=tmp_path, check_git=False)


def test_host_source_value_is_not_whitespace_normalized(tmp_path: Path) -> None:
    gate = module()
    env_file = tmp_path / "next.env"
    env_file.write_text("COCKPIT_HOST=0.0.0.0 \n", encoding="ascii")
    with pytest.raises(gate.IsolationError, match="env_invalid"):
        gate.load_env(env_file, home=tmp_path)


def test_lan_host_requires_token_but_loopback_remains_optional(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    gate = module()
    loopback = gate.expected()
    gate.validate_host_token(loopback, None)

    lan = {**loopback, "COCKPIT_HOST": "0.0.0.0"}
    with pytest.raises(gate.IsolationError, match="lan_host_token_required"):
        gate.validate_host_token(lan, None)
    gate.validate_host_token(lan, "a" * 32)

    monkeypatch.setattr(gate, "load_env", lambda *_args, **_kwargs: lan)
    monkeypatch.setattr(gate, "validate", lambda *_args, **_kwargs: lan)
    monkeypatch.setattr(gate, "load_cockpit_token", lambda _values: None)
    for command in ("check", "start"):
        assert gate.main([command]) == 1
        captured = capsys.readouterr()
        _assert_text_error(captured, "lan_host_token_required")
        assert "cockpit.token" in captured.err
        assert "t" * 64 not in captured.err
        assert gate.main([command, "--json"]) == 1
        json_captured = capsys.readouterr()
        assert json.loads(json_captured.out) == {
            "error": "lan_host_token_required", "ok": False,
        }
        assert json_captured.err == ""


def test_fixed_server_lan_host_requires_matching_private_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = module()
    home = tmp_path / "home"
    checkout = home / "github" / "agent-cockpit-next"
    checkout.mkdir(parents=True)
    (checkout / ".agent-memory-project").write_text(
        "agent-cockpit-next\n", encoding="ascii",
    )
    values = gate.expected(home)
    values["COCKPIT_HOST"] = "0.0.0.0"
    Path(values["COCKPIT_PROJECT_ROOT"]).mkdir(exist_ok=True)
    monkeypatch.setattr(
        next_profile.Path, "home", classmethod(lambda _cls: home),
    )

    for invalid_host in ("::1", "localhost", "192.168.1.5", "0.0.0.0 "):
        values["COCKPIT_HOST"] = invalid_host
        with pytest.raises(
            next_profile.NextProfileError,
            match="next_profile_invalid:COCKPIT_HOST",
        ):
            next_profile.validate_server_environment(checkout, values)

    values["COCKPIT_HOST"] = "0.0.0.0"
    with pytest.raises(
        next_profile.NextProfileError,
        match="next_profile_invalid:LAN_HOST_TOKEN_REQUIRED",
    ):
        next_profile.validate_server_environment(checkout, values)

    token_path = gate.token_file_path(values)
    token_path.parent.mkdir(parents=True, mode=0o700)
    token_path.write_text("a" * 64 + "\n", encoding="ascii")
    token_path.chmod(0o600)
    with pytest.raises(
        next_profile.NextProfileError,
        match="next_profile_invalid:LAN_HOST_TOKEN_MISMATCH",
    ):
        next_profile.validate_server_environment(checkout, values)

    values["COCKPIT_TOKEN"] = "a" * 64
    next_profile.validate_server_environment(checkout, values)


@pytest.mark.parametrize(
    ("source_sha", "code"),
    [
        (None, "source_sha_missing"),
        ("", "source_sha_missing"),
        ("a" * 39, "source_sha_malformed"),
        ("A" * 40, "source_sha_malformed"),
        ("g" * 40, "source_sha_malformed"),
    ],
)
def test_ephemeral_source_sha_fails_closed_before_runtime_setup(
    source_sha: str | None,
    code: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launcher = ephemeral_module()
    args = ["--runtime-root", str(tmp_path)]
    if source_sha is not None:
        args.extend(["--source-sha", source_sha])

    assert launcher.main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{code}\n"
    assert list(tmp_path.iterdir()) == []


def test_ephemeral_environment_remains_loopback_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COCKPIT_SOURCE_SHA", "b" * 40)
    tmp_path.chmod(0o700)
    assert next_profile.initialize_empty_ephemeral_runtime_root(tmp_path)
    environment = ephemeral_module()._environment(
        tmp_path, 12345, "a" * 32, source_sha="a" * 40,
    )
    assert environment["COCKPIT_HOST"] == "127.0.0.1"
    assert environment["COCKPIT_SOURCE_SHA"] == "a" * 40
    assert "COCKPIT_TOKEN" not in environment
    assert environment["XDG_CONFIG_HOME"] == str(
        next_profile.ephemeral_herdr_config_home(tmp_path)
    )
    assert len(os.fsencode(
        Path(environment["XDG_CONFIG_HOME"])
        / "herdr" / "sessions"
        / next_profile.ephemeral_session_for_root(tmp_path)
        / "herdr-client.sock"
    )) <= 107


def test_next_token_file_rejects_unsafe_metadata_and_content(tmp_path: Path) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    path = gate.token_file_path(values)
    path.parent.mkdir(parents=True, mode=0o700)

    path.write_text("a" * 64 + "\n", encoding="ascii")
    path.chmod(0o644)
    with pytest.raises(gate.IsolationError, match="token_file_unsafe"):
        gate.load_cockpit_token(values)

    path.chmod(0o600)
    path.write_text("short\n", encoding="ascii")
    with pytest.raises(gate.IsolationError, match="token_file_unsafe"):
        gate.load_cockpit_token(values)

    path.write_text("a" * 31 + "!\n", encoding="ascii")
    with pytest.raises(gate.IsolationError, match="token_file_invalid"):
        gate.load_cockpit_token(values)

    path.write_text("b" * 64 + "\n", encoding="ascii")
    hardlink = tmp_path / "hardlink.token"
    os.link(path, hardlink)
    with pytest.raises(gate.IsolationError, match="token_file_unsafe"):
        gate.load_cockpit_token(values)
    hardlink.unlink()

    path.unlink()
    target = tmp_path / "target.token"
    target.write_text("b" * 64 + "\n", encoding="ascii")
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(gate.IsolationError, match="token_file_unsafe"):
        gate.load_cockpit_token(values)


def test_start_environment_discards_inherited_production_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = module()
    monkeypatch.setenv("COCKPIT_SCHEMA_EVIDENCE_PATH", "/production/evidence.json")
    monkeypatch.setenv("COCKPIT_TOKEN", "production-secret")
    monkeypatch.setenv("AGENT_COCKPIT_RELEASE_STATE_DIR", "/production/release")
    monkeypatch.setenv("AGENT_MAIL_PROJECT", "/production/project")
    monkeypatch.setenv("HERDR_SESSION", "production-session")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/production/herdr.sock")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setenv("PYTHONPATH", "/production/python")
    monkeypatch.setenv("PYTHONHOME", "/production/home")
    monkeypatch.setenv("LD_PRELOAD", "/production/preload.so")
    monkeypatch.setenv("XDG_DATA_HOME", "/production/data")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/production/config")
    monkeypatch.setenv("XDG_STATE_HOME", "/production/state")
    monkeypatch.setenv("HERDR_CONFIG_PATH", "/production/herdr.toml")
    monkeypatch.setenv("TEAM_HUB_URL", "https://production.example")
    monkeypatch.setenv("HUMAN_AUTH_URL", "https://production.example/auth")
    clean = gate.sanitized_environment(gate.expected())
    assert "COCKPIT_SCHEMA_EVIDENCE_PATH" not in clean
    assert "COCKPIT_TOKEN" not in clean
    assert clean["AGENT_COCKPIT_RELEASE_STATE_DIR"].endswith(
        "/.local/state/agent-cockpit-next/release-lane"
    )
    assert clean["AGENT_MAIL_PROJECT"].endswith("/github/agent-cockpit-next")
    assert clean["HERDR_SESSION"] == "github-agent-cockpit-next"
    assert "HERDR_SOCKET_PATH" not in clean
    assert "HERDR_PANE_ID" not in clean
    assert "PYTHONPATH" not in clean
    assert "PYTHONHOME" not in clean
    assert "LD_PRELOAD" not in clean
    assert clean["XDG_DATA_HOME"].endswith("agent-cockpit-next-data")
    assert clean["XDG_CONFIG_HOME"].endswith("agent-cockpit-next")
    assert clean["XDG_STATE_HOME"].endswith("agent-cockpit-next")
    assert clean["HERDR_CONFIG_PATH"].endswith(
        "agent-cockpit-next/herdr/config.toml"
    )
    assert clean["TEAM_HUB_URL"] == "http://127.0.0.1:8765"
    assert clean["HUMAN_AUTH_URL"] == "http://127.0.0.1:8766"


def test_systemd_check_fails_closed_on_command_error(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = module()

    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(gate.subprocess, "run", lambda *args, **kwargs: Result())
    assert gate._unit_not_installed() is False


def test_port_probe_allows_immediate_restart_after_reusable_listener_closes() -> None:
    gate = module()
    host = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        with socket.create_connection((host, port)) as client:
            accepted, _ = listener.accept()
            listener.close()
            with accepted:
                accepted.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""

    assert gate._port_available(host, port) is True


def test_port_probe_rejects_active_listener_with_reuseaddr() -> None:
    gate = module()
    host = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, 0))
        listener.listen(1)

        assert gate._port_available(host, listener.getsockname()[1]) is False


def test_web_build_readiness_requires_index_and_assets(tmp_path: Path) -> None:
    gate = module()
    dist = tmp_path / "web" / "dist"

    with pytest.raises(gate.IsolationError, match="next_web_build_unavailable"):
        gate._validate_web_build(tmp_path)

    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    with pytest.raises(gate.IsolationError, match="next_web_build_unavailable"):
        gate._validate_web_build(tmp_path)

    assets = dist / "assets"
    assets.mkdir()
    with pytest.raises(gate.IsolationError, match="next_web_build_unavailable"):
        gate._validate_web_build(tmp_path)

    (assets / "index.js").write_text("export {}\n", encoding="utf-8")
    gate._validate_web_build(tmp_path)

    (dist / "index.html").unlink()
    with pytest.raises(gate.IsolationError, match="next_web_build_unavailable"):
        gate._validate_web_build(tmp_path)


@pytest.mark.parametrize("command", ("check", "start"))
def test_check_and_start_fail_closed_without_web_build(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    monkeypatch.setattr(gate, "load_env", lambda *_args, **_kwargs: values)
    monkeypatch.setattr(gate, "validate", lambda *_args, **_kwargs: values)
    monkeypatch.setattr(gate, "load_cockpit_token", lambda _values: None)
    monkeypatch.setattr(
        gate,
        "_validate_web_build",
        lambda _repo: (_ for _ in ()).throw(
            gate.IsolationError("next_web_build_unavailable")
        ),
    )

    assert gate.main([command]) == 1
    captured = capsys.readouterr()
    _assert_text_error(captured, "next_web_build_unavailable")
    assert gate.main([command, "--json"]) == 1
    json_captured = capsys.readouterr()
    assert json.loads(json_captured.out) == {
        "error": "next_web_build_unavailable", "ok": False,
    }
    assert json_captured.err == ""


def test_git_check_fails_closed_on_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    repo = Path(values["COCKPIT_NEXT_WORKTREE"])
    repo.mkdir(parents=True)
    (repo / ".agent-memory-project").write_text(
        "agent-cockpit-next\n", encoding="ascii",
    )
    monkeypatch.setattr(
        gate.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(gate.IsolationError, match="git_unavailable"):
        gate.validate(values, repo=repo, home=tmp_path)


def test_start_execs_next_venv_with_sanitized_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = module()
    captured: dict[str, object] = {}
    values = gate.expected(tmp_path)
    values["COCKPIT_HOST"] = "0.0.0.0"

    class StubLock:
        fd = 42

        def __init__(self, received: dict[str, str]) -> None:
            assert received == values

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr(gate, "validate", lambda *_args, **_kwargs: values)
    monkeypatch.setattr(gate, "InstanceLock", StubLock)
    monkeypatch.setattr(gate, "_unit_not_installed", lambda: True)
    monkeypatch.setattr(gate, "_port_available", lambda host, port: True)
    monkeypatch.setattr(gate, "ensure_runtime_roots", lambda values: None)
    monkeypatch.setattr(
        gate.next_profile,
        "ensure_private_herdr_config",
        lambda received: captured.update(herdr_config=received),
    )
    monkeypatch.setattr(gate, "load_cockpit_token", lambda values: "t" * 64)
    monkeypatch.setattr(gate, "_validate_web_build", lambda _repo: None)
    monkeypatch.setattr(gate, "_prepare_exec_fds", lambda fd: None)
    monkeypatch.setattr(gate.Path, "is_file", lambda _self: True)
    monkeypatch.setattr(gate.os, "chdir", lambda path: captured.update(cwd=path))
    monkeypatch.setattr(
        gate.os,
        "execve",
        lambda executable, argv, env: captured.update(
            executable=executable, argv=argv, env=env
        ),
    )
    monkeypatch.setenv("PYTHONPATH", "/production/python")
    monkeypatch.setenv("COCKPIT_NEXT_LOCK_FD", "999")

    env_file = tmp_path / "next.env"
    env_file.write_text("ignored=1\n", encoding="ascii")
    assert gate.main(["start", "--env-file", str(env_file)]) == 0
    assert captured["cwd"] == ROOT
    assert captured["executable"] == str(ROOT / ".venv" / "bin" / "python")
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["COCKPIT_HOST"] == "0.0.0.0"
    assert environment["COCKPIT_PORT"] == "18790"
    assert environment["COCKPIT_PROJECT_ROOT"] == str(tmp_path / "github")
    assert environment["VIRTUAL_ENV"] == str(ROOT / ".venv")
    assert environment["COCKPIT_NEXT_LOCK_FD"] == "42"
    assert environment["COCKPIT_TOKEN"] == "t" * 64
    assert "PYTHONPATH" not in environment
    assert captured["herdr_config"] == values


def test_check_does_not_write_private_herdr_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    monkeypatch.setattr(gate, "load_env", lambda *_args, **_kwargs: values)
    monkeypatch.setattr(gate, "validate", lambda *_args, **_kwargs: values)
    monkeypatch.setattr(gate, "load_cockpit_token", lambda _values: None)
    monkeypatch.setattr(gate, "_validate_web_build", lambda _repo: None)
    monkeypatch.setattr(
        gate.next_profile,
        "ensure_private_herdr_config",
        lambda _values: pytest.fail("check must remain read-only"),
        raising=False,
    )

    assert gate.main(["check"]) == 0


def test_check_text_guidance_lists_url_token_path_and_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    gate = module()
    values = gate.expected(tmp_path)
    secret = "t" * 64
    monkeypatch.setattr(gate, "load_env", lambda *_args, **_kwargs: values)
    monkeypatch.setattr(gate, "validate", lambda *_args, **_kwargs: values)
    monkeypatch.setattr(gate, "load_cockpit_token", lambda _values: secret)
    monkeypatch.setattr(gate, "_validate_web_build", lambda _repo: None)

    assert gate.main(["check"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines()[0] == "OK"
    assert "http://127.0.0.1:18790" in captured.out
    token_path = str(gate.token_file_path(values))
    assert token_path in captured.out
    assert "选择代码目录" in captured.out
    assert secret not in captured.out
    assert "COCKPIT_TOKEN=" not in captured.out

    assert gate.main(["check", "--json"]) == 0
    json_captured = capsys.readouterr()
    assert json.loads(json_captured.out) == {
        "ok": True, "profile": "agent-cockpit-next",
    }
    assert json_captured.err == ""
    assert token_path not in json_captured.out
    assert secret not in json_captured.out


def test_start_text_guidance_prints_before_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    gate = module()
    values = {**gate.expected(tmp_path), "COCKPIT_HOST": "0.0.0.0"}
    captured: dict[str, object] = {}

    class StubLock:
        fd = 42

        def __init__(self, _values):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(gate, "load_env", lambda *_args, **_kwargs: values)
    monkeypatch.setattr(gate, "validate", lambda *_args, **_kwargs: values)
    monkeypatch.setattr(gate, "InstanceLock", StubLock)
    monkeypatch.setattr(gate, "_unit_not_installed", lambda: True)
    monkeypatch.setattr(gate, "_port_available", lambda host, port: True)
    monkeypatch.setattr(gate, "ensure_runtime_roots", lambda values: None)
    monkeypatch.setattr(
        gate.next_profile, "ensure_private_herdr_config", lambda _values: None,
    )
    monkeypatch.setattr(gate, "load_cockpit_token", lambda values: "t" * 64)
    monkeypatch.setattr(gate, "_validate_web_build", lambda _repo: None)
    monkeypatch.setattr(gate, "_prepare_exec_fds", lambda fd: None)
    monkeypatch.setattr(gate.Path, "is_file", lambda _self: True)
    monkeypatch.setattr(gate.os, "chdir", lambda path: None)
    monkeypatch.setattr(
        gate.os, "execve",
        lambda executable, argv, env: captured.update(ran=True),
    )

    assert gate.main(["start"]) == 0
    assert captured.get("ran") is True
    text = capsys.readouterr()
    assert text.err == ""
    assert "0.0.0.0:18790" in text.out
    assert "<本机局域网IP>" in text.out
    assert str(gate.token_file_path(values)) in text.out
    assert "选择代码目录" in text.out
    assert "t" * 64 not in text.out


@pytest.mark.parametrize(
    "code",
    (
        "wrong_worktree",
        "env_keys_mismatch",
        "next_port_in_use",
        "git_baseline_mismatch",
        "value_mismatch:COCKPIT_HOST",
    ),
)
def test_common_error_codes_have_executable_chinese_hint(code: str) -> None:
    gate = module()
    hint = gate.error_hint(code)
    assert hint
    assert any("\u4e00" <= char <= "\u9fff" for char in hint)
    assert "cockpit.token" in gate.error_hint("lan_host_token_required")


@pytest.mark.parametrize(
    ("module_name", "expressions"),
    [
        (
            "agent_mail_commands.mail_send",
            ("MAIL_PROJECTS_PATH", "LAUNCH_DESCRIPTORS_PATH", "TYPING_STATE_PATH"),
        ),
        (
            "agent_mail_commands.mail_identity_inject",
            ("MAIL_PROJECTS_PATH", "DESCRIPTORS_PATH"),
        ),
    ],
)
def test_mail_helpers_follow_next_runtime_roots(
    module_name: str, expressions: tuple[str, ...], tmp_path: Path,
) -> None:
    data = tmp_path / "next-data"
    state = tmp_path / "next-state"
    descriptors = data / "descriptors.json"
    env = {
        **os.environ,
        "COCKPIT_DATA_DIR": str(data),
        "COCKPIT_STATE_DIR": str(state),
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH": str(descriptors),
    }
    code = (
        f"import {module_name} as target; "
        f"print(*({','.join('str(target.' + name + ')' for name in expressions)},), sep='\\n')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        check=True, capture_output=True, text=True,
    )
    paths = result.stdout.splitlines()
    assert paths[0] == str(data / "mail-projects.json")
    assert paths[1] == str(descriptors)
    if len(paths) == 3:
        assert paths[2] == str(state / "typing.json")


@pytest.mark.parametrize(
    "module_name",
    (
        "agent_mail_commands.mail_send",
        "agent_mail_commands.mail_identity_inject",
    ),
)
def test_mail_helpers_fail_closed_with_incomplete_next_profile(
    module_name: str,
) -> None:
    env = {**os.environ, "COCKPIT_NEXT_PROFILE": "1"}
    for name in next_profile.FULL_ENV_NAMES:
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "next_profile_missing" in result.stderr


def _next_environment() -> dict[str, str]:
    return module().expected()


def _install_next_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _next_environment().items():
        monkeypatch.setenv(name, value)


def test_next_source_entry_requires_exact_profile() -> None:
    env = dict(os.environ)
    env.pop("COCKPIT_NEXT_PROFILE", None)
    result = subprocess.run(
        [sys.executable, "server.py"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert "next_profile_required" in result.stderr

    env = {
        **env,
        **_next_environment(),
        "COCKPIT_NEXT_WORKTREE": str(ROOT),
        "COCKPIT_PORT": "8790",
    }
    result = subprocess.run(
        [sys.executable, "server.py"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert "next_profile_invalid:COCKPIT_PORT" in result.stderr

    valid_env = {**env, **_next_environment()}
    valid_env.pop("COCKPIT_NEXT_LOCK_FD", None)
    entry = (
        "from agent_cockpit import next_profile; "
        "next_profile.validate_server_environment=lambda *_args, **_kwargs: None; "
        "import runpy; runpy.run_path('server.py', run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-c", entry], cwd=ROOT, env=valid_env,
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert result.stderr == "lock_fd_invalid\n"

    for forged_fd in ("not-a-fd", "02", "2", "999999"):
        valid_env["COCKPIT_NEXT_LOCK_FD"] = forged_fd
        result = subprocess.run(
            [sys.executable, "-c", entry], cwd=ROOT, env=valid_env,
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 2
        assert result.stderr == "lock_fd_invalid\n"


def test_next_herdr_scope_filters_and_rejects_foreign_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_next_environment(monkeypatch)
    expected_root = Path(os.environ["XDG_CONFIG_HOME"]) / "herdr" / "sessions"
    payload = {
        "sessions": [
            {
                "name": "github-agent-cockpit", "running": True,
                "session_dir": "/home/fyc/.config/herdr/sessions/github-agent-cockpit",
                "socket_path": "/home/fyc/.config/herdr/sessions/github-agent-cockpit/herdr.sock",
            },
            {
                "name": "github-agent-cockpit-next", "running": True,
                "session_dir": str(expected_root / "github-agent-cockpit-next"),
                "socket_path": str(
                    expected_root / "github-agent-cockpit-next" / "herdr.sock"
                ),
            },
            {
                "name": "github-agent-cockpit-next", "running": True,
                "session_dir": "/tmp/forged-next",
                "socket_path": "/tmp/forged-next/herdr.sock",
            },
        ]
    }
    monkeypatch.setattr(herdr_client, "is_available", lambda: True)
    monkeypatch.setattr(
        herdr_client, "_run", lambda *_args, **_kwargs: __import__("json").dumps(payload),
    )
    assert [row["name"] for row in herdr_client.list_sessions()] == [
        "github-agent-cockpit-next"
    ]

    monkeypatch.undo()
    _install_next_environment(monkeypatch)
    called = False

    def forbidden_subprocess(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("foreign session must not reach subprocess")

    monkeypatch.setattr(herdr_client.subprocess, "run", forbidden_subprocess)
    with pytest.raises(RuntimeError, match="next_session_forbidden"):
        herdr_client._run(["--session", "github-agent-cockpit", "pane", "read"])
    assert herdr_client.stop_session("github-agent-cockpit")["error"] == (
        "next_session_forbidden"
    )
    assert not called


def test_next_agent_mail_queries_only_exact_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_next_environment(monkeypatch)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT,
            created_at REAL, archived_at REAL
        );
        CREATE TABLE agents (
            id INTEGER PRIMARY KEY, project_id INTEGER, name TEXT, program TEXT,
            model TEXT, task_description TEXT, inception_ts REAL,
            last_active_ts REAL, contact_policy TEXT, retired_at REAL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, project_id INTEGER, thread_id TEXT,
            topic TEXT, subject TEXT, body_md TEXT, importance TEXT,
            ack_required INTEGER, created_ts REAL, reply_to INTEGER,
            sender_id INTEGER
        );
        CREATE TABLE message_recipients (
            message_id INTEGER, agent_id INTEGER, kind TEXT,
            read_ts REAL, ack_ts REAL
        );
        """
    )
    allowed = os.environ["AGENT_MAIL_PROJECT"]
    con.execute("INSERT INTO projects VALUES (1, 'next', ?, 1, NULL)", (allowed,))
    con.execute(
        "INSERT INTO projects VALUES (2, 'production', '/home/fyc/github/agent-cockpit', 2, NULL)"
    )
    con.executescript(
        """
        INSERT INTO agents VALUES
          (1, 1, 'next-agent', 'codex', '', '', 1, 1, 'open', NULL),
          (2, 2, 'production-agent', 'codex', '', '', 2, 2, 'open', NULL);
        INSERT INTO messages VALUES
          (1, 1, 'n', '', 'next', '', 'normal', 0, 1, NULL, 1),
          (2, 2, 'p', '', 'production', '', 'normal', 0, 2, NULL, 2);
        INSERT INTO message_recipients VALUES
          (1, 1, 'to', NULL, NULL),
          (2, 2, 'to', NULL, NULL);
        """
    )
    monkeypatch.setattr(db, "_conn", con)
    try:
        result = db.overview()
        assert [row["slug"] for row in result["projects"]] == ["next"]
        assert result["total_unread"] == 1
        assert db.project_by_slug("production") is None
        assert db.project_by_id(2) is None
        assert db.list_agents(2) == []
        assert db.recent_messages(2) == []
        assert db.agent_by_name(2, "production-agent") is None
        assert db.identity_by_cwd(
            "/home/fyc/github/agent-cockpit", "codex"
        ) is None
    finally:
        con.close()


def test_next_linked_worktree_keeps_exact_mail_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_next_environment(monkeypatch)
    assert server._canonical_mail_project(ROOT) == os.environ["AGENT_MAIL_PROJECT"]
    with pytest.raises(Exception) as exc:
        server._canonical_mail_project(Path("/home/fyc/github/agent-cockpit"))
    assert getattr(exc.value, "status_code", None) == 403


@pytest.mark.parametrize(
    ("module_name", "argv"),
    (
        ("agent_mail_commands.am_register", ["--agent", "codex"]),
        (
            "agent_mail_commands.am_retire",
            ["--agent", "codex", "--instance", "i-aaaaaaaaaaaaaaaaaaaaaaaaaa"],
        ),
        ("agent_mail_commands.am_init_project", []),
    ),
)
def test_next_project_helpers_reject_foreign_project_before_work(
    module_name: str, argv: list[str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_next_environment(monkeypatch)
    target = __import__(module_name, fromlist=["main"])
    with pytest.raises(SystemExit) as exc:
        target.main([*argv, "--project", "/home/fyc/github/agent-cockpit"])
    assert "next_project_forbidden" in str(exc.value) + capsys.readouterr().err


def test_next_task_report_rejects_foreign_session_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_next_environment(monkeypatch)
    from agent_mail_commands import task_report

    called = False

    def forbidden_write(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("foreign session must not reach coordination storage")

    monkeypatch.setattr(task_report.coordination, "submit_task_report", forbidden_write)
    with pytest.raises(SystemExit) as exc:
        task_report.main([
            "--session", "github-agent-cockpit", "--pane", "w1:p1",
            "--request-id", "request", "--progress", "10", "--summary", "working",
        ])
    assert exc.value.code == 2
    assert not called


def test_mail_identity_inject_uses_config_scoped_session_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    config = tmp_path / "next-config" / "herdr" / "config.toml"
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(config))
    from agent_mail_commands import mail_identity_inject

    assert mail_identity_inject._expected_session_dir("next", "") == (
        config.parent / "sessions" / "next"
    )


def test_next_identity_inject_rejects_foreign_session_and_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_next_environment(monkeypatch)
    from agent_mail_commands import mail_identity_inject

    production_socket = (
        "/home/fyc/.config/herdr/sessions/github-agent-cockpit/herdr.sock"
    )
    with pytest.raises(next_profile.NextProfileError, match="next_session_forbidden"):
        mail_identity_inject._expected_session_dir(
            "github-agent-cockpit", production_socket,
        )
    with pytest.raises(
        next_profile.NextProfileError, match="next_herdr_socket_forbidden",
    ):
        mail_identity_inject._expected_session_dir(
            next_profile.SESSION, production_socket,
        )


def test_next_mail_send_rejects_foreign_herdr_without_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_next_environment(monkeypatch)
    from agent_mail_commands import mail_send

    called = False

    def forbidden_subprocess(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("foreign Herdr target must not reach subprocess")

    monkeypatch.setattr(mail_send.subprocess, "run", forbidden_subprocess)
    with pytest.raises(next_profile.NextProfileError, match="next_session_forbidden"):
        mail_send.resolve_explicit_target(
            "github-agent-cockpit", "w1:p1", "codex",
        )
    mail_send._deliver_notify_note(
        {}, "github-agent-cockpit", "w1:p1", str(ROOT), "explicit",
        1, "foreign", "codex", "main", str(ROOT), "info",
    )
    assert not called


def test_next_mail_send_filters_forged_session_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_next_environment(monkeypatch)
    from agent_mail_commands import mail_send

    expected = Path(os.environ["HERDR_CONFIG_PATH"]).parent / "sessions"
    payload = {
        "sessions": [
            {
                "name": "github-agent-cockpit", "running": True,
                "session_dir": "/home/fyc/.config/herdr/sessions/github-agent-cockpit",
                "socket_path": "/home/fyc/.config/herdr/sessions/github-agent-cockpit/herdr.sock",
            },
            {
                "name": next_profile.SESSION, "running": True,
                "session_dir": str(expected / next_profile.SESSION),
                "socket_path": str(expected / next_profile.SESSION / "herdr.sock"),
            },
            {
                "name": next_profile.SESSION, "running": True,
                "session_dir": "/tmp/forged-next",
                "socket_path": "/tmp/forged-next/herdr.sock",
            },
        ],
    }
    monkeypatch.setattr(
        mail_send.subprocess, "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, __import__("json").dumps(payload), "",
        ),
    )
    assert [row["name"] for row in mail_send._session_rows({})] == [
        next_profile.SESSION
    ]
