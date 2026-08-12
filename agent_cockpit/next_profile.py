"""Fail-closed runtime scope for the isolated Cockpit Next source tree."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


PROFILE_ENV = "COCKPIT_NEXT_PROFILE"
PROJECT_MARKER = "agent-cockpit-next"
SESSION = "github-agent-cockpit-next"
HOST = "127.0.0.1"
PORT = "18790"
FULL_ENV_NAMES = (
    "COCKPIT_NEXT_WORKTREE",
    "COCKPIT_HOST",
    "COCKPIT_PORT",
    "COCKPIT_DATA_DIR",
    "COCKPIT_CONFIG_DIR",
    "COCKPIT_STATE_DIR",
    "COCKPIT_UPLOADS_DIR",
    "COCKPIT_COORDINATION_DB",
    "COCKPIT_LAUNCH_DESCRIPTORS_PATH",
    "AGENT_COCKPIT_RELEASE_STATE_DIR",
    "COCKPIT_SYSTEMD_UNIT",
    "COCKPIT_UPGRADE_V2_ENABLED",
    "COCKPIT_B0_MODE",
    "COCKPIT_HERDR_STATE_MODE",
    "COCKPIT_EDITION",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "HERDR_CONFIG_PATH",
    "AGENT_MAIL_DB_PATH",
    "AGENT_MAIL_PROJECT",
    "HERDR_SESSION",
    "TEAM_HUB_URL",
    "HUMAN_AUTH_URL",
)


class NextProfileError(RuntimeError):
    pass


def enabled(environment: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environment is None else environment
    return env.get(PROFILE_ENV) == "1"


def is_next_source(root: Path) -> bool:
    try:
        return (root / ".agent-memory-project").read_text(
            encoding="ascii"
        ) == f"{PROJECT_MARKER}\n"
    except (OSError, UnicodeError):
        return False


def _required(name: str, environment: Mapping[str, str] | None = None) -> str:
    env = os.environ if environment is None else environment
    value = env.get(name)
    if not isinstance(value, str) or not value:
        raise NextProfileError(f"next_profile_missing:{name}")
    return value


def project(environment: Mapping[str, str] | None = None) -> str | None:
    if not enabled(environment):
        return None
    raw = _required("AGENT_MAIL_PROJECT", environment)
    path = Path(raw).expanduser()
    wanted = Path.home().resolve() / "github" / "agent-cockpit-next"
    if (
        not path.is_absolute()
        or path.resolve(strict=False) != path
        or path != wanted
    ):
        raise NextProfileError("next_profile_invalid:AGENT_MAIL_PROJECT")
    return str(path)


def require_project(
    value: str, environment: Mapping[str, str] | None = None,
) -> str:
    scoped = project(environment)
    resolved = str(Path(value).expanduser().resolve(strict=False))
    if scoped is not None and resolved != scoped:
        raise NextProfileError("next_project_forbidden")
    return resolved


def session(environment: Mapping[str, str] | None = None) -> str | None:
    if not enabled(environment):
        return None
    value = _required("HERDR_SESSION", environment)
    if value != SESSION:
        raise NextProfileError("next_profile_invalid:HERDR_SESSION")
    return value


def require_session(
    value: str, environment: Mapping[str, str] | None = None,
) -> str:
    scoped = session(environment)
    if scoped is not None and value != scoped:
        raise NextProfileError("next_session_forbidden")
    return value


def require_helper_environment(
    names: tuple[str, ...], environment: Mapping[str, str] | None = None,
) -> None:
    if not enabled(environment):
        return
    env = os.environ if environment is None else environment
    for name in (*FULL_ENV_NAMES, *names):
        _required(name, environment)
    validate_server_environment(
        Path(_required("COCKPIT_NEXT_WORKTREE", env)), env,
    )


def validate_server_environment(
    root: Path, environment: Mapping[str, str] | None = None,
) -> None:
    env = os.environ if environment is None else environment
    source_is_next = is_next_source(root)
    if source_is_next and not enabled(env):
        raise NextProfileError("next_profile_required")
    if not enabled(env):
        return
    if not source_is_next:
        raise NextProfileError("next_profile_wrong_source")

    home = Path.home().resolve()
    worktree = Path(_required("COCKPIT_NEXT_WORKTREE", env))
    expected = {
        "COCKPIT_NEXT_WORKTREE": str(root.resolve()),
        "COCKPIT_HOST": HOST,
        "COCKPIT_PORT": PORT,
        "COCKPIT_DATA_DIR": str(home / ".local/share/agent-cockpit-next-data"),
        "COCKPIT_CONFIG_DIR": str(home / ".config/agent-cockpit-next"),
        "COCKPIT_STATE_DIR": str(home / ".local/state/agent-cockpit-next"),
        "COCKPIT_UPLOADS_DIR": str(home / ".local/share/agent-cockpit-next-uploads"),
        "COCKPIT_COORDINATION_DB": str(
            home / ".local/share/agent-cockpit-next-data/coordination.sqlite3"
        ),
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH": str(
            home / ".local/share/agent-cockpit-next-data/launch-descriptors.json"
        ),
        "AGENT_COCKPIT_RELEASE_STATE_DIR": str(
            home / ".local/state/agent-cockpit-next/release-lane"
        ),
        "COCKPIT_SYSTEMD_UNIT": "agent-cockpit-next.service",
        "COCKPIT_UPGRADE_V2_ENABLED": "0",
        "COCKPIT_B0_MODE": "off",
        "COCKPIT_HERDR_STATE_MODE": "off",
        "COCKPIT_EDITION": "source",
        "XDG_DATA_HOME": str(home / ".local/share/agent-cockpit-next-data"),
        "XDG_CONFIG_HOME": str(home / ".config/agent-cockpit-next"),
        "XDG_STATE_HOME": str(home / ".local/state/agent-cockpit-next"),
        "HERDR_CONFIG_PATH": str(home / ".config/agent-cockpit-next/herdr/config.toml"),
        "AGENT_MAIL_DB_PATH": str(home / "mcp_agent_mail/storage.sqlite3"),
        "AGENT_MAIL_PROJECT": str(root.resolve()),
        "HERDR_SESSION": SESSION,
        "TEAM_HUB_URL": "http://127.0.0.1:8765",
        "HUMAN_AUTH_URL": "http://127.0.0.1:8766",
    }
    if not worktree.is_absolute() or worktree.resolve(strict=False) != root.resolve():
        raise NextProfileError("next_profile_invalid:COCKPIT_NEXT_WORKTREE")
    for name, wanted in expected.items():
        if env.get(name) != wanted:
            raise NextProfileError(f"next_profile_invalid:{name}")
    project(env)
    session(env)
