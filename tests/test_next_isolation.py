from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
import stat
from pathlib import Path

import pytest

from agent_cockpit import db, herdr_client, next_profile
import server


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "next_dev.py"


def module():
    spec = importlib.util.spec_from_file_location("next_dev", SCRIPT)
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
    assert environment["COCKPIT_PORT"] == "18790"
    assert environment["VIRTUAL_ENV"] == str(ROOT / ".venv")
    assert environment["COCKPIT_NEXT_LOCK_FD"] == "42"
    assert "PYTHONPATH" not in environment


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
