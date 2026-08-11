#!/usr/bin/env python3
"""Resolve a managed pane to one exact active Agent Mail identity."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .common import REGISTRY_DIR, helper_command, slugify


MAIL_PROJECTS_PATH = Path.home() / "dashboard-data" / "mail-projects.json"
DESCRIPTORS_PATH = Path.home() / "dashboard-data" / "launch-descriptors.json"
HERDR_BIN = shutil.which("herdr") or str(Path.home() / ".local" / "bin" / "herdr")
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PANE_ID_RE = re.compile(r"^[A-Za-z0-9_]+:[A-Za-z0-9_]+$")
INSTANCE_ID_RE = re.compile(r"^i-[a-z2-7]{26}$")
PRODUCT_KINDS = {
    "codex": "codex", "kimi": "kimi", "claude": "claude",
    "qodercli": "qodercli", "qodercn": "qodercli", "grok": "grok",
    "opencode": "opencode", "zcode": "opencode",
}


@dataclass(frozen=True)
class ManagedIdentity:
    project: str
    agent: str
    instance_id: str
    identity: dict


def _safe_open_chain(path: Path) -> int | None:
    home = Path.home()
    try:
        components = path.relative_to(home).parts
        if not components or any(component in ("", ".", "..") for component in components):
            return None
        current = os.open(home, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(current)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            os.close(current)
            return None
        for index, component in enumerate(components):
            try:
                opened = os.open(
                    component, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current,
                )
            except OSError:
                os.close(current)
                return None
            info = os.fstat(opened)
            last = index == len(components) - 1
            valid = (
                info.st_uid == os.getuid()
                and (
                    stat.S_ISREG(info.st_mode)
                    and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_nlink == 1
                    if last else
                    stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022
                )
            )
            if not valid:
                os.close(opened)
                os.close(current)
                return None
            os.close(current)
            current = opened
        return current
    except OSError:
        os.close(current)
        return None


def _safe_read_json(path: Path) -> dict | None:
    fd = _safe_open_chain(path)
    if fd is None:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _bound_project(session: str, socket_path: str) -> str | None:
    state = _safe_read_json(MAIL_PROJECTS_PATH)
    if not state or not SESSION_RE.fullmatch(session):
        return None
    entry = state.get("sessions", {}).get(session)
    if not isinstance(entry, dict):
        return None
    try:
        session_dir = Path(str(entry["session_dir"])).expanduser()
        project = Path(str(entry["project"])).expanduser()
        expected = (
            Path(socket_path).expanduser().resolve().parent
            if socket_path else
            (Path.home() / ".config" / "herdr" / "sessions" / session).resolve()
        )
        if (
            not session_dir.is_absolute()
            or str(session_dir) != str(session_dir.resolve())
            or session_dir != expected
            or not project.is_absolute()
            or str(project) != str(project.resolve())
            or not project.is_dir()
        ):
            return None
    except (KeyError, OSError, RuntimeError, ValueError):
        return None
    return str(project)


def _canonical_project(
    cwd: str, session: str, state_path: Path, socket_path: str = "",
) -> str:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        entry = data.get("sessions", {}).get(session, {})
        bound_dir = Path(str(entry.get("session_dir") or "")).expanduser().resolve()
        expected = (
            Path(socket_path).expanduser().resolve().parent
            if socket_path else
            (Path.home() / ".config" / "herdr" / "sessions" / session).resolve()
        )
        project = Path(str(entry.get("project") or "")).expanduser().resolve()
        if SESSION_RE.fullmatch(session) and bound_dir == expected and project.is_dir():
            return str(project)
    except (OSError, TypeError, ValueError):
        pass
    return str(Path(cwd).expanduser().resolve())


def _snapshot(session: str) -> dict | None:
    try:
        result = subprocess.run(
            [HERDR_BIN, "--session", session, "api", "snapshot"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    try:
        value = json.loads(raw).get("result", {}).get("snapshot")
    except (AttributeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _live_identity_matches(
    session: str, pane_id: str, workdir: str, kind: str, instance_id: str,
) -> bool:
    snapshot = _snapshot(session)
    if not snapshot:
        return False
    panes = [
        pane for pane in snapshot.get("panes", [])
        if isinstance(pane, dict) and pane.get("pane_id") == pane_id
    ]
    if len(panes) != 1:
        return False
    pane = panes[0]
    try:
        pane_workdir = Path(str(pane.get("cwd") or pane.get("foreground_cwd") or ""))
        if not pane_workdir.is_absolute() or pane_workdir.resolve() != Path(workdir):
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    agents = [
        agent for agent in snapshot.get("agents", [])
        if isinstance(agent, dict) and agent.get("pane_id") == pane_id
    ]
    return (
        len(agents) == 1
        and agents[0].get("name") == instance_id
        and PRODUCT_KINDS.get(str(agents[0].get("agent") or "")) == kind
    )


def resolve_managed_identity() -> ManagedIdentity | None:
    session = os.environ.get("HERDR_SESSION", "")
    pane_id = os.environ.get("HERDR_PANE_ID", "")
    if not SESSION_RE.fullmatch(session) or not PANE_ID_RE.fullmatch(pane_id):
        return None
    project = _bound_project(session, os.environ.get("HERDR_SOCKET_PATH", ""))
    if project is None:
        return None
    data = _safe_read_json(DESCRIPTORS_PATH)
    if not data or data.get("schema") != 2 or not isinstance(data.get("descriptors"), dict):
        return None
    matches = [
        (key, record) for key, record in data["descriptors"].items()
        if isinstance(record, dict)
        and record.get("session") == session
        and record.get("pane_id") == pane_id
    ]
    if len(matches) != 1:
        return None
    key, record = matches[0]
    agent = record.get("agent")
    kind = record.get("kind")
    instance_id = record.get("instance_id")
    workdir = record.get("workdir")
    if (
        not isinstance(agent, str)
        or PRODUCT_KINDS.get(agent) != kind
        or not isinstance(instance_id, str)
        or not INSTANCE_ID_RE.fullmatch(instance_id)
        or key != f"instance|{instance_id}"
        or record.get("name") != instance_id
        or record.get("state") != "active"
        or not isinstance(workdir, str)
    ):
        return None
    try:
        canonical_workdir = Path(workdir)
        if (
            not canonical_workdir.is_absolute()
            or str(canonical_workdir) != str(canonical_workdir.resolve())
            or canonical_workdir != Path.cwd().resolve()
        ):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    if not _live_identity_matches(session, pane_id, workdir, kind, instance_id):
        return None
    identity = _safe_read_json(
        REGISTRY_DIR / slugify(project) / f"{agent}--{instance_id}.json"
    )
    if not identity or (
        identity.get("project_key") != project
        or identity.get("agent") != agent
        or identity.get("instance") != instance_id
        or not isinstance(identity.get("name"), str)
        or not identity["name"]
        or identity.get("status") != "active"
        or identity.get("retired_at")
    ):
        return None
    return ManagedIdentity(project, agent, instance_id, identity)


def _context(resolved: ManagedIdentity) -> str:
    send = helper_command("mail-send")
    recv = helper_command("mail-recv")
    return (
        f"[agent-mail 身份] 你是 {resolved.identity['name']},项目 {resolved.project}。"
        f" 发消息: {send} --agent {resolved.agent} --instance {resolved.instance_id}"
        f" --project \"{resolved.project}\" --to <花名> --subject \"...\" --body \"...\";"
        f" 收消息: {recv} --agent {resolved.agent} --instance {resolved.instance_id}"
        f" --project \"{resolved.project}\" --unread。"
    )


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-h", "--help"}:
        print("usage: mail-identity-inject [--print-identity]")
        return
    if args not in ([], ["--print-identity"]):
        return
    resolved = resolve_managed_identity()
    if resolved is None:
        return
    if args == ["--print-identity"]:
        print(f"{resolved.agent}\t{resolved.instance_id}")
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": _context(resolved),
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
