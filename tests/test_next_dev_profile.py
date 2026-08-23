from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agent_cockpit import db, next_profile

ROOT = Path(__file__).resolve().parents[1]


def test_dev_profile_db_scope_includes_all_registered_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db.next_profile, "is_dev", lambda: True)
    monkeypatch.setattr(
        db.next_profile,
        "project",
        lambda: (_ for _ in ()).throw(AssertionError("dev scope must be global")),
    )

    assert db._scope() is None


def test_isolated_profile_db_scope_stays_single_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db.next_profile, "is_dev", lambda: False)
    monkeypatch.setattr(db.next_profile, "project", lambda: "/isolated/project")

    assert db._scope() == "/isolated/project"


def _dev_server():
    spec = importlib.util.spec_from_file_location(
        "agent_cockpit_dev_server", ROOT / "scripts" / "dev_server.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dev_env(home: Path, repo: Path) -> dict[str, str]:
    values = {
        "COCKPIT_NEXT_PROFILE": next_profile.DEV_PROFILE,
        "COCKPIT_PROJECT_ROOT": str(home / "github"),
        "COCKPIT_HOST": "127.0.0.1",
    }
    values.update(next_profile.dev_layout(home, repo))
    return values


def test_dev_fd_inventory_falls_back_to_dev_fd(monkeypatch):
    gate = _dev_server()._load_next_dev()
    listed = []
    changed = []

    def fake_listdir(path):
        listed.append(path)
        if path == "/proc/self/fd":
            raise FileNotFoundError(path)
        return ["0", "1", "2", "42", "43", "not-a-fd"]

    monkeypatch.setattr(gate.os, "listdir", fake_listdir)
    monkeypatch.setattr(
        gate.os, "set_inheritable", lambda fd, value: changed.append((fd, value)),
    )

    gate._prepare_exec_fds(42)

    assert listed == ["/proc/self/fd", "/dev/fd"]
    assert changed == [(43, False)]


def test_dev_fd_inventory_fails_closed_without_supported_directory(monkeypatch):
    gate = _dev_server()._load_next_dev()
    monkeypatch.setattr(
        gate.os, "listdir", lambda path: (_ for _ in ()).throw(FileNotFoundError(path)),
    )

    with pytest.raises(gate.IsolationError, match="fd_inventory_unavailable"):
        gate._prepare_exec_fds(42)


def test_dev_profile_accepts_this_checkout_on_8790(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "code" / "agent-cockpit"
    github = home / "github"
    github.mkdir(parents=True)
    repo.mkdir(parents=True)
    (repo / ".agent-memory-project").write_text("agent-cockpit-next\n", encoding="ascii")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    env = _dev_env(home, repo)
    next_profile.validate_server_environment(repo, env)
    assert next_profile.enabled(env)
    assert next_profile.is_dev(env)
    assert not next_profile.is_ephemeral(env)
    assert next_profile.project(env) == str(repo)
    assert next_profile.session(env) is None
    assert next_profile.require_session("any-session", env) == "any-session"
    child = github / "other-proj"
    child.mkdir()
    assert next_profile.require_project(str(child), env) == str(child.resolve())
    other = home / "pitapat" / "app"
    other.mkdir(parents=True)
    assert next_profile.require_project(str(other), env) == str(other.resolve())
    deleted = home / "github" / "deleted-project"
    assert next_profile.require_retirement_project(str(deleted), env) == str(deleted)
    with pytest.raises(next_profile.NextProfileError, match="next_project_forbidden"):
        next_profile.require_project(str(deleted), env)
    for forbidden in (home / ".ssh" / "old-project", tmp_path / "outside"):
        with pytest.raises(next_profile.NextProfileError, match="next_project_forbidden"):
            next_profile.require_retirement_project(str(forbidden), env)
    with pytest.raises(next_profile.NextProfileError, match="next_project_forbidden"):
        next_profile.require_project(str(tmp_path / "outside"), env)
    env["HERDR_SESSION"] = "cockpit"
    next_profile.validate_server_environment(repo, env)
    assert next_profile.require_session("cockpit", env) == "cockpit"
    monkeypatch.delenv("COCKPIT_PROJECT_ROOT", raising=False)
    launcher = _dev_server()
    lan = launcher.dev_values(repo, home, "0.0.0.0")
    assert lan["COCKPIT_HOST"] == "0.0.0.0"
    assert lan["COCKPIT_PROJECT_ROOT"] == str(repo.parent.resolve())
    with pytest.raises(next_profile.NextProfileError, match="next_profile_invalid:COCKPIT_HOST"):
        launcher.dev_values(repo, home, "192.168.1.5")


def test_dev_values_does_not_require_home_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = home / "agent-cockpit"
    home.mkdir()
    repo.mkdir()
    (repo / ".agent-memory-project").write_text("agent-cockpit-next\n", encoding="ascii")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("COCKPIT_PROJECT_ROOT", raising=False)
    launcher = _dev_server()
    values = launcher.dev_values(repo, home)
    assert values["COCKPIT_PROJECT_ROOT"] == str(repo.resolve())
    assert not (home / "github").exists()
    next_profile.validate_server_environment(repo, values)


def test_dev_values_honors_project_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = home / "src" / "agent-cockpit"
    projects = home / "work"
    home.mkdir()
    repo.mkdir(parents=True)
    projects.mkdir()
    (repo / ".agent-memory-project").write_text("agent-cockpit-next\n", encoding="ascii")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("COCKPIT_PROJECT_ROOT", str(projects))
    launcher = _dev_server()
    values = launcher.dev_values(repo, home)
    assert values["COCKPIT_PROJECT_ROOT"] == str(projects)


def test_dev_profile_rejects_sandbox_home_and_wrong_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "code" / "agent-cockpit"
    (home / "github").mkdir(parents=True)
    repo.mkdir(parents=True)
    (repo / ".agent-memory-project").write_text("agent-cockpit-next\n", encoding="ascii")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    env = _dev_env(home, repo)
    env["HOME"] = str(tmp_path / "sandbox-home")
    (tmp_path / "sandbox-home").mkdir()
    with pytest.raises(next_profile.NextProfileError, match="next_profile_invalid:HOME"):
        next_profile.validate_server_environment(repo, env)
    env.pop("HOME")
    env["COCKPIT_PORT"] = next_profile.PORT
    with pytest.raises(next_profile.NextProfileError, match="next_profile_invalid:COCKPIT_PORT"):
        next_profile.validate_server_environment(repo, env)


def test_fixed_profile_still_rejects_8790() -> None:
    values = {
        "COCKPIT_NEXT_PROFILE": next_profile.FIXED_PROFILE,
        "COCKPIT_NEXT_WORKTREE": str(ROOT),
        "COCKPIT_PROJECT_ROOT": str(Path.home() / "github"),
        "COCKPIT_HOST": "127.0.0.1",
        "COCKPIT_PORT": "8790",
        "COCKPIT_DATA_DIR": str(Path.home() / ".local/share/agent-cockpit-next-data"),
        "COCKPIT_CONFIG_DIR": str(Path.home() / ".config/agent-cockpit-next"),
        "COCKPIT_STATE_DIR": str(Path.home() / ".local/state/agent-cockpit-next"),
        "COCKPIT_UPLOADS_DIR": str(Path.home() / ".local/share/agent-cockpit-next-uploads"),
        "COCKPIT_COORDINATION_DB": str(
            Path.home() / ".local/share/agent-cockpit-next-data/coordination.sqlite3"
        ),
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH": str(
            Path.home() / ".local/share/agent-cockpit-next-data/launch-descriptors.json"
        ),
        "AGENT_COCKPIT_RELEASE_STATE_DIR": str(
            Path.home() / ".local/state/agent-cockpit-next/release-lane"
        ),
        "AGENT_MAIL_PROJECT": str(ROOT),
        "HERDR_SESSION": next_profile.SESSION,
        "COCKPIT_SYSTEMD_UNIT": "agent-cockpit-next.service",
        "COCKPIT_UPGRADE_V2_ENABLED": "0",
        "COCKPIT_B0_MODE": "off",
        "COCKPIT_HERDR_STATE_MODE": "off",
        "COCKPIT_EDITION": "source",
        "XDG_DATA_HOME": str(Path.home() / ".local/share/agent-cockpit-next-data"),
        "XDG_CONFIG_HOME": str(Path.home() / ".config/agent-cockpit-next"),
        "XDG_STATE_HOME": str(Path.home() / ".local/state/agent-cockpit-next"),
        "HERDR_CONFIG_PATH": str(Path.home() / ".config/agent-cockpit-next/herdr/config.toml"),
        "AGENT_MAIL_DB_PATH": str(Path.home() / "mcp_agent_mail/storage.sqlite3"),
        "TEAM_HUB_URL": "http://127.0.0.1:8765",
        "HUMAN_AUTH_URL": "http://127.0.0.1:8766",
    }
    with pytest.raises(next_profile.NextProfileError, match="next_profile_invalid:COCKPIT_PORT"):
        next_profile.validate_server_environment(ROOT, values)
