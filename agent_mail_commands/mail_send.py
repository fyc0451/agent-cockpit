#!/usr/bin/env python3
"""发送 Agent Mail 消息，并安全通知唯一匹配的 Herdr pane。"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from agent_cockpit.artifact_root import resolve_artifact_root

ARTIFACT_ROOT = resolve_artifact_root()
INSTALL_ROOT = str(ARTIFACT_ROOT)
TOOLS_DIR = os.path.join(INSTALL_ROOT, "agent-mail-tools")
from .common import (
    REGISTRY_DIR, helper_command, load_identity, mcp_call, mcp_tool, slugify,
)
from agent_cockpit import chat_ledger, chat_roster, coordination, next_profile  # noqa: E402


next_profile.require_helper_environment((
    "COCKPIT_DATA_DIR",
    "COCKPIT_STATE_DIR",
    "COCKPIT_LAUNCH_DESCRIPTORS_PATH",
))


HERDR_BIN = shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")
MAIL_RECV_BIN = helper_command("mail-recv")
_COCKPIT_DATA_DIR = os.path.expanduser(
    os.environ.get("COCKPIT_DATA_DIR", "~/dashboard-data")
)
_COCKPIT_STATE_DIR = os.path.expanduser(
    os.environ.get("COCKPIT_STATE_DIR", "~/.local/state/agent-cockpit")
)
MAIL_PROJECTS_PATH = os.path.join(_COCKPIT_DATA_DIR, "mail-projects.json")
LAUNCH_DESCRIPTORS_PATH = os.path.expanduser(
    os.environ.get(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH",
        os.path.join(_COCKPIT_DATA_DIR, "launch-descriptors.json"),
    )
)
# Cockpit 落盘的用户输入状态(session → 墙钟时间):正在输入时不注入
# pane 通知,避免消息追加到未提交草稿后被一起提交。
TYPING_STATE_PATH = os.path.join(_COCKPIT_STATE_DIR, "typing.json")
TYPING_DEFER_WINDOW = 30.0
PROG_TO_AGENT = {
    "codex-cli": "codex", "codex": "codex",
    "kimi-work": "kimi", "kimi": "kimi",
    "qoder-cn": "qodercn", "qoder": "qodercn",
    "qodercli": "qodercn", "qodercn": "qodercn",
    "claude": "claude", "claude-code": "claude",
    "grok": "grok", "opencode": "opencode", "zcode": "zcode",
}
_GENERATION_ID_RE = re.compile(r"^[0-9a-f]{40}-[0-9a-f]{64}$")
_OPAQUE_INSTANCE_RE = re.compile(r"^i-[a-z2-7]{26}$")
_PRODUCT_KINDS = {
    "codex": "codex", "kimi": "kimi", "claude": "claude",
    "qodercli": "qodercli", "qodercn": "qodercli", "grok": "grok",
    "opencode": "opencode", "zcode": "opencode",
}


def _config_root() -> str | None:
    if not getattr(sys, "frozen", False):
        return INSTALL_ROOT
    generations = ARTIFACT_ROOT.parent
    if (
        generations.name == "generations"
        and _GENERATION_ID_RE.fullmatch(ARTIFACT_ROOT.name)
    ):
        return str(generations.parent)
    return None


def _team_reply_url() -> str:
    """解析本机 Cockpit 端口；代理地址始终限制在 loopback。"""
    raw_port = os.environ.get("COCKPIT_PORT")
    config_root = _config_root()
    if raw_port is None and config_root is not None:
        try:
            with open(os.path.join(config_root, ".env"), encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if line.startswith("export "):
                        line = line[7:].lstrip()
                    key, separator, value = line.partition("=")
                    if separator and key.strip() == "COCKPIT_PORT":
                        try:
                            parts = shlex.split(value, comments=True)
                        except ValueError:
                            parts = []
                        raw_port = parts[0] if len(parts) == 1 else ""
                        break
        except OSError:
            pass
    raw_port = "8790" if raw_port is None else raw_port.strip()
    if not raw_port.isdecimal() or not 1 <= int(raw_port) <= 65535:
        raise SystemExit(f"error: COCKPIT_PORT 必须是 1-65535 的整数: {raw_port!r}")
    return f"http://127.0.0.1:{raw_port}/api/agent/team-reply"


def _agent_types_match(left: str, right: str) -> bool:
    """比较 Agent Mail 类型与 Herdr runtime，兼容同一 CLI 的别名。"""
    if not left or not right:
        return False
    return PROG_TO_AGENT.get(left, left) == PROG_TO_AGENT.get(right, right)


def _runtime_kind(agent: str) -> str:
    product = PROG_TO_AGENT.get(agent, agent)
    return _PRODUCT_KINDS.get(product, product)


def _agent_mail_db_path() -> str:
    configured = os.environ.get("AGENT_MAIL_DB_PATH")
    if configured:
        return os.path.expanduser(configured)
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    new = os.path.join(data_home, "mcp_agent_mail", "storage.sqlite3")
    legacy = os.path.expanduser("~/mcp_agent_mail/storage.sqlite3")
    return next((path for path in (new, legacy) if os.path.isfile(path)), new)


def _program_by_name(name: str, project_key: str) -> str:
    import sqlite3

    database = _agent_mail_db_path()
    if not os.path.isfile(database):
        return ""
    try:
        con = sqlite3.connect("file:" + database + "?mode=ro", uri=True)
        row = con.execute(
            "SELECT program FROM agents a JOIN projects p ON p.id=a.project_id "
            "WHERE a.name=? AND p.human_key=? AND a.retired_at IS NULL LIMIT 1",
            (name, project_key),
        ).fetchone()
        con.close()
        return row[0] if row else ""
    except Exception:
        return ""


def _notification_identity(name: str, project_key: str) -> tuple[str, str] | None:
    """由本机 registry 解析收件人的真实 CLI agent/instance。"""
    directory = REGISTRY_DIR / slugify(project_key)
    if not directory.is_dir():
        return None
    matches = []
    for path in sorted(directory.glob("*.json")):
        try:
            identity = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(identity, dict) or identity.get("project_key") != project_key:
            continue
        if identity.get("name") != name:
            continue
        if identity.get("retired_at") or identity.get("status") == "retired":
            continue
        agent = identity.get("agent")
        instance = identity.get("instance")
        if (
            isinstance(agent, str) and isinstance(instance, str)
            and agent and instance
            and (
                not _OPAQUE_INSTANCE_RE.fullmatch(instance)
                or identity.get("status") == "active"
            )
        ):
            matches.append((agent, instance))
    return matches[0] if len(matches) == 1 else None


def _registry_identities(project_key: str) -> list[dict]:
    directory = REGISTRY_DIR / slugify(project_key)
    identities: list[dict] = []
    if not directory.is_dir():
        return identities
    for path in sorted(directory.glob("*.json")):
        try:
            identity = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if (
            isinstance(identity, dict)
            and identity.get("project_key") == project_key
            and not identity.get("retired_at")
            and identity.get("status") != "retired"
            and (
                not _OPAQUE_INSTANCE_RE.fullmatch(str(identity.get("instance") or ""))
                or identity.get("status") == "active"
            )
        ):
            identities.append(identity)
    return identities


def bound_mail_thread(thread: str, herdr_session: str) -> str:
    """群聊 thread 必须跟当前 herdr session 同工作区，禁止 SCC 写 cockpit。"""
    here = (herdr_session or "").strip()
    dest = (thread or "").strip()
    if not dest and here and chat_ledger.get_thread_by_session(here):
        dest = here
    if dest and here and dest != here:
        other = chat_ledger.get_thread_by_session(dest)
        mine = chat_ledger.get_thread_by_session(here)
        if (
            other is not None
            and mine is not None
            and other.get("workspace_id") != mine.get("workspace_id")
        ):
            raise SystemExit(
                f"error: 当前 session 是 {here}，不能写 --thread {dest}。"
                f"本群请用 --thread {here}"
            )
    return dest


def _leftover_mail_name(name: str, agent: str) -> bool:
    lowered = str(name or "").strip().lower()
    kind = str(agent or "").strip().lower()
    if not lowered or not kind:
        return False
    return lowered == kind or lowered == f"{kind}-main" or lowered.startswith(f"{kind}-")


def _unique_flower_for_leftover(recipient: str, identities: list[dict]) -> str:
    """kimi-main / kimi 在同时有花名时改写为唯一非 leftover 花名。"""
    key = str(recipient or "").strip()
    if not key:
        return ""
    agents = {
        str(item.get("agent") or "").strip().lower()
        for item in identities
        if item.get("agent")
    }
    kind = ""
    lowered = key.lower()
    if lowered in agents:
        kind = lowered
    elif lowered.endswith("-main") and lowered[:-5] in agents:
        kind = lowered[:-5]
    elif any(lowered.startswith(f"{item}-") for item in agents):
        kind = next(item for item in agents if lowered.startswith(f"{item}-"))
    if not kind:
        for item in identities:
            if str(item.get("name") or "") == key and _leftover_mail_name(key, str(item.get("agent") or "")):
                kind = str(item.get("agent") or "").strip().lower()
                break
    if not kind:
        return ""
    flowers = [
        str(item.get("name") or "")
        for item in identities
        if str(item.get("agent") or "").strip().lower() == kind
        and item.get("name")
        and not _leftover_mail_name(str(item.get("name")), kind)
    ]
    return flowers[0] if len(flowers) == 1 else ""


def _resolve_registry_recipients(
    recipients: list[str], project_key: str, session: str = "",
) -> list[str]:
    """把 agent 类型/类型-实例别名改写为本项目唯一注册的 registry 花名。

    Hub 收件人只接受注册花名；直接写 agent 类型（如 qodercn）会被 Hub
    的收件人启发式校验拒绝。本函数在发送前做本地防呆：
    - 已是注册花名：原样保留；
    - 当前群 session 的 leader / 程序-main：改写为本群 Leader 花名；
    - 类型或 类型-实例 别名且本项目 registry 唯一命中：改写为花名；
    - 别名命中多个身份：报错并列出候选花名；
    - 无命中：原样透传，交给 Hub 校验。
    边界：只读本机 registry；在其他机器注册、本机无条目的身份无法解析，
    会原样透传给 Hub。
    """
    identities = _registry_identities(project_key)
    registered = {
        str(identity.get("name"))
        for identity in identities
        if identity.get("name")
    }
    record = chat_roster.get_session_leader(session) if session else {}
    resolved: list[str] = []
    for recipient in recipients:
        session_name = chat_roster.resolve_session_alias(recipient, record)
        if session_name:
            if session_name != recipient:
                print(
                    f"note: 收件人 '{recipient}' 改写为本群 Leader '{session_name}'",
                    file=sys.stderr,
                )
            resolved.append(session_name)
            continue
        flower = _unique_flower_for_leftover(recipient, identities)
        if flower:
            if flower != recipient:
                print(
                    f"note: 收件人 '{recipient}' 改写为注册花名 '{flower}'",
                    file=sys.stderr,
                )
            resolved.append(flower)
            continue
        if recipient in registered:
            resolved.append(recipient)
            continue
        if recipient.lower() == "leader":
            raise SystemExit(
                "error: 本群还没有登记 Leader。请改用花名，不要写 grok-main"
            )
        if not identities:
            resolved.append(recipient)
            continue
        matches = [
            identity for identity in identities
            if isinstance(identity.get("agent"), str) and identity["agent"]
            and (
                recipient == identity["agent"]
                or (
                    isinstance(identity.get("instance"), str)
                    and identity["instance"]
                    and recipient == f"{identity['agent']}-{identity['instance']}"
                )
            )
        ]
        if len(matches) > 1:
            flower = _unique_flower_for_leftover(recipient, matches)
            if flower:
                print(
                    f"note: 收件人 '{recipient}' 改写为注册花名 '{flower}'",
                    file=sys.stderr,
                )
                resolved.append(flower)
                continue
            options = ", ".join(
                str(identity.get("name")) for identity in matches
            )
            raise SystemExit(
                f"error: 收件人 '{recipient}' 对应多个注册身份（{options}），"
                "请改用精确花名"
            )
        name = str((matches[0].get("name") or "") if matches else "")
        if matches and name and name != recipient:
            print(
                f"note: 收件人 '{recipient}' 改写为注册花名 '{name}'",
                file=sys.stderr,
            )
            resolved.append(name)
        else:
            resolved.append(recipient)
    return resolved


def _team_reply(payload: dict) -> dict:
    """经本机 Cockpit 代理远端 Team Hub；同幂等键最多重试一次。"""
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error = ""
    for attempt in range(2):
        request = urllib.request.Request(
            _team_reply_url(),
            data=encoded,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                raw = response.read(1024 * 1024).decode("utf-8")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("响应格式无效")
            return result
        except urllib.error.HTTPError as exc:
            try:
                error_data = json.loads(exc.read(16 * 1024).decode("utf-8"))
            except (UnicodeError, ValueError):
                error_data = None
            detail = error_data.get("detail") if isinstance(error_data, dict) else None
            last_error = str(detail or f"HTTP {exc.code}")
            if exc.code not in {502, 503, 504} or attempt:
                break
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, ValueError) as exc:
            last_error = exc.reason if isinstance(exc, urllib.error.URLError) else str(exc)
            if attempt:
                break
    raise SystemExit(f"团队回复失败: {last_error or '本机 Cockpit 不可用'}")


def _load_bindings() -> dict:
    try:
        with open(MAIL_PROJECTS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return sessions if isinstance(sessions, dict) else {}


def _session_rows(env: dict) -> list[dict]:
    def scoped(rows: list[dict]) -> list[dict]:
        session = next_profile.session()
        if session is None:
            return rows
        config = Path(os.environ["HERDR_CONFIG_PATH"]).expanduser().resolve()
        expected = config.parent / "sessions" / session
        return [
            row for row in rows
            if row.get("name") == session
            and Path(str(row.get("directory") or "")).expanduser().resolve(
                strict=False
            ) == expected
            and Path(str(row.get("socket") or "")).expanduser().resolve(
                strict=False
            ) == expected / "herdr.sock"
        ]

    try:
        result = subprocess.run(
            [HERDR_BIN, "session", "list", "--json"],
            capture_output=True, text=True, timeout=5, env=env,
        )
        data = json.loads(result.stdout)
        return scoped([
            {
                "name": str(row.get("name", "")),
                "running": bool(row.get("running")),
                "directory": str(row.get("session_dir", "")),
                "socket": str(row.get("socket_path", "")),
            }
            for row in data.get("sessions", []) if isinstance(row, dict) and row.get("name")
        ])
    except Exception:
        pass
    try:
        result = subprocess.run(
            [HERDR_BIN, "session", "list"],
            capture_output=True, text=True, timeout=5, env=env,
        )
    except Exception:
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] != "name":
            rows.append({
                "name": parts[0],
                "running": parts[1] == "running",
                "directory": " ".join(parts[2:-1]),
                "socket": parts[-1],
            })
    return scoped(rows)


def _session_mail_project(bindings: dict, session: dict) -> str | None:
    entry = bindings.get(session["name"])
    if not isinstance(entry, dict):
        return None
    session_dir = session.get("directory") or ""
    if not session_dir:
        return None
    if (
        os.path.realpath(os.path.expanduser(str(entry.get("session_dir", ""))))
        != os.path.realpath(os.path.expanduser(session_dir))
    ):
        return None
    project = entry.get("project")
    return project if isinstance(project, str) and project else None


def _session_bound_to(bindings: dict, session: dict, project_key: str) -> bool:
    project = _session_mail_project(bindings, session)
    return bool(project) and (
        os.path.realpath(os.path.expanduser(project))
        == os.path.realpath(os.path.expanduser(project_key))
    )


def _git_common_dir(path: str) -> str | None:
    """返回目录所属 git 仓库的公共 git dir（worktree 归一到主仓库），失败返回 None。"""
    if not path:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return None
    out = (result.stdout or "").strip()
    if result.returncode != 0 or not out:
        return None
    if not os.path.isabs(out):
        out = os.path.join(path, out)
    try:
        return os.path.realpath(out)
    except OSError:
        return None


def _identity_bound_panes(project_key: str, mail_name: str) -> dict[str, set]:
    """coordination 强绑定（项目内）: mail_name → {session 内绑定的 pane_id}。"""
    try:
        return coordination.panes_by_mail_name(project_key, mail_name)
    except Exception:
        return {}


def _select_notify_targets(
    candidates: list[tuple], project_key: str, mail_name: str = "",
) -> list[tuple]:
    """分级选路；同级唯一才投，歧义/无相关候选一律跳过（不再全局同类型兜底）。

    优先级：1) coordination 强绑定(mail_name→session/pane)；2) session 显式绑定
    项目；3) pane cwd 位于项目根或其子目录；4) cwd 与项目根同 git common-dir
    （覆盖 worktree）。cwd 不可访问时仅 1/2 级可用；返回
    [(session, pane_id, cwd, is_exact)]，is_exact 仅当 cwd 恰为项目根。
    """
    eligible = []
    for candidate in candidates:
        bound = bool(candidate[3]) if len(candidate) > 3 else False
        has_binding = bool(candidate[4]) if len(candidate) > 4 else False
        if has_binding and not bound:
            continue
        eligible.append(candidate)
    if not eligible:
        return []
    proj_real = os.path.realpath(os.path.expanduser(project_key or ""))
    proj_git = _git_common_dir(proj_real)
    identity_panes = _identity_bound_panes(proj_real, mail_name)
    tiers: dict[int, list] = {1: [], 2: [], 3: [], 4: []}
    for candidate in eligible:
        session, pane_id, cwd = candidate[:3]
        bound = bool(candidate[3]) if len(candidate) > 3 else False
        if pane_id in (identity_panes.get(session) or set()):
            tiers[1].append(candidate)
            continue
        if bound:
            tiers[2].append(candidate)
            continue
        if not cwd or not os.path.isdir(cwd):
            continue
        cwd_real = os.path.realpath(os.path.expanduser(cwd))
        if cwd_real == proj_real or cwd_real.startswith(proj_real + os.sep):
            tiers[3].append(candidate)
            continue
        if proj_git and _git_common_dir(cwd_real) == proj_git:
            tiers[4].append(candidate)
    # 首个非空层即定夺：唯一则投；同级多候选为歧义，立即返回空，不降级。
    for tier in (1, 2, 3, 4):
        picks = tiers[tier]
        if not picks:
            continue
        if len(picks) == 1:
            session, pane_id, cwd = picks[0][:3]
            exact = bool(cwd) and (
                os.path.realpath(os.path.expanduser(cwd)) == proj_real
            )
            return [(session, pane_id, cwd, exact)]
        return []
    return []


def _notify_text(
    msg_id: int, subject: str, agent_type: str, instance: str, project_key: str,
    cwd: str, is_exact: bool, intent: str = "info",
) -> str:
    label = "打断请求" if intent in coordination.INTERRUPT_INTENTS else "新消息"
    head = (
        f"[agent-mail {label}] 你收到 #{msg_id} 「{(subject or '')[:50]}」"
        f"，intent={intent}"
    )
    command = (
        f"{shlex.quote(MAIL_RECV_BIN)} --agent {shlex.quote(agent_type)} "
        f"--instance {shlex.quote(instance)} "
        f"--project {shlex.quote(project_key)} --unread --message {msg_id}"
    )
    tail = ""
    if intent in coordination.INTERRUPT_INTENTS:
        tail = (
            "。在当前原子操作安全停手后运行上述命令；基础 checkpoint 会自动保存，"
            "处理完成后按工具输出完成单消息确认并恢复。"
        )
    if is_exact:
        return f"{head},用 {command} 查看并处理{tail}"
    return (
        f"{head}。注意:当前 pane cwd={cwd} 与消息项目 {project_key} 不同,"
        f"请用 {command} 查看并处理{tail}"
    )


def _recipient_typing(session: str, pane_id: str) -> bool:
    """用户正在目标 pane 里输入(未提交草稿)时返回 True。

    typing.json 由 Cockpit 在输入发生时按 pane 落盘:
    新格式 {session: {"panes": {pane_id: ts}, "unknown": ts?}};
    旧 float 格式与 unknown 记录按未知 pane 保守避让。
    """
    try:
        with open(TYPING_STATE_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return False
    entry = data.get(session)

    def fresh(ts) -> bool:
        try:
            return 0 < time.time() - float(ts) < TYPING_DEFER_WINDOW
        except (TypeError, ValueError):
            return False

    if isinstance(entry, (int, float)):
        return fresh(entry)  # 旧格式: pane 未知,保守避让
    if not isinstance(entry, dict):
        return False
    panes = entry.get("panes")
    if isinstance(panes, dict) and fresh(panes.get(pane_id)):
        return True
    return fresh(entry.get("unknown"))


def _herdr_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(HERDR_BIN) + ":" + env.get("PATH", "")
    return env


def _session_panes(session_name: str, env: dict) -> list[dict]:
    """读取某 session 的 pane 快照（SSE data: 行优先）。"""
    next_profile.require_session(session_name)
    result = subprocess.run(
        [HERDR_BIN, "--session", session_name, "api", "snapshot"],
        capture_output=True, text=True, timeout=5, env=env,
    )
    raw = result.stdout
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    try:
        snapshot = json.loads(raw).get("result", {}).get("snapshot", {})
    except (ValueError, json.JSONDecodeError):
        return []
    panes = snapshot.get("panes", []) if isinstance(snapshot, dict) else []
    agents = snapshot.get("agents", []) if isinstance(snapshot, dict) else []
    by_pane: dict[str, list[dict]] = {}
    for agent in agents:
        if isinstance(agent, dict) and agent.get("pane_id"):
            by_pane.setdefault(str(agent["pane_id"]), []).append(agent)
    enriched = []
    for pane in panes:
        if not isinstance(pane, dict):
            continue
        item = dict(pane)
        live = by_pane.get(str(item.get("pane_id") or ""), [])
        if len(live) == 1:
            item["_runtime_name"] = live[0].get("name")
            item["_runtime_kind"] = live[0].get("agent")
        enriched.append(item)
    return enriched


def _managed_notify_target(
    candidates: list[tuple], project_key: str, mail_name: str,
    agent_type: str, instance: str,
) -> list[tuple]:
    """Resolve an opaque mailbox only through one exact active live descriptor."""
    try:
        data = json.loads(Path(LAUNCH_DESCRIPTORS_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    descriptors = data.get("descriptors")
    if data.get("schema") != 2 or not isinstance(descriptors, dict):
        return []
    matches = [
        (key, record) for key, record in descriptors.items()
        if isinstance(record, dict) and record.get("instance_id") == instance
    ]
    if len(matches) != 1:
        return []
    key, record = matches[0]
    kind = _PRODUCT_KINDS.get(agent_type)
    if (
        key != f"instance|{instance}"
        or record.get("state") != "active"
        or record.get("name") != instance
        or record.get("agent") != agent_type
        or record.get("kind") != kind
        or record.get("mail_agent") != agent_type
        or record.get("mail_instance") != instance
        or record.get("mail_name") != mail_name
        or not isinstance(record.get("mail_project"), str)
        or os.path.realpath(os.path.expanduser(record["mail_project"]))
        != os.path.realpath(os.path.expanduser(project_key))
        or not isinstance(record.get("workdir"), str)
    ):
        return []
    exact = []
    for candidate in candidates:
        session, pane_id, cwd = candidate[:3]
        runtime_name = candidate[5] if len(candidate) > 5 else None
        runtime_kind = candidate[6] if len(candidate) > 6 else None
        if (
            session == record.get("session")
            and pane_id == record.get("pane_id")
            and runtime_name == instance
            and runtime_kind == kind
            and bool(cwd)
            and os.path.realpath(os.path.expanduser(cwd))
            == os.path.realpath(os.path.expanduser(record["workdir"]))
        ):
            exact.append(candidate)
    if len(exact) != 1:
        return []
    session, pane_id, cwd = exact[0][:3]
    is_project_root = (
        os.path.realpath(os.path.expanduser(cwd))
        == os.path.realpath(os.path.expanduser(project_key))
    )
    return [(session, pane_id, cwd, is_project_root)]


def resolve_explicit_target(
    session_name: str, pane_id: str, agent_type: str, instance: str = "",
) -> tuple[str, str, str, str]:
    """校验显式通知目标，返回目标与解析时的 Agent 状态。"""
    if not session_name or not pane_id:
        raise ValueError("显式目标需要同时提供 session 与 pane")
    next_profile.require_session(session_name)
    env = _herdr_env()
    running = {
        str(row.get("name")) for row in _session_rows(env) if row.get("running")
    }
    if session_name not in running:
        raise ValueError(f"session 不存在或未运行: {session_name}")
    pane = next(
        (p for p in _session_panes(session_name, env)
         if str(p.get("pane_id") or "") == pane_id),
        None,
    )
    if pane is None:
        raise ValueError(f"pane 不存在: {session_name}/{pane_id}")
    pane_agent = str(pane.get("agent") or "")
    managed = bool(_OPAQUE_INSTANCE_RE.fullmatch(instance))
    matches_type = (
        _runtime_kind(agent_type) == _runtime_kind(pane_agent)
        if managed else _agent_types_match(agent_type, pane_agent)
    )
    if not matches_type:
        raise ValueError(
            f"pane {session_name}/{pane_id} 的 agent 类型为 "
            f"{pane_agent or '空'}，与收件人类型 {agent_type} 不兼容"
        )
    if _OPAQUE_INSTANCE_RE.fullmatch(instance) and (
        pane.get("_runtime_name") != instance
        or pane.get("_runtime_kind") != _PRODUCT_KINDS.get(agent_type)
    ):
        raise ValueError("pane live runtime 与 opaque identity 不匹配")
    cwd = str(pane.get("cwd") or pane.get("foreground_cwd") or "")
    return session_name, pane_id, cwd, str(pane.get("agent_status") or "")


def _notify_pane(
    agent_type: str, instance: str, msg_id: int, subject: str,
    project_key: str = "", intent: str = "info",
    mail_name: str = "", explicit: tuple | None = None,
) -> None:
    if not os.path.isfile(HERDR_BIN) or not project_key:
        return
    try:
        next_profile.require_project(project_key)
    except next_profile.NextProfileError as exc:
        print(f"warning: 跳过越界 Herdr 通知: {exc}", file=sys.stderr)
        return
    env = _herdr_env()
    if explicit is not None:
        session, pane_id, cwd, *extra = explicit
        _deliver_notify_note(
            env, session, pane_id, cwd, "explicit",
            msg_id, subject, agent_type, instance, project_key, intent,
            agent_status=str(extra[0]) if extra else "",
        )
        return
    bindings = _load_bindings()
    sessions = [row for row in _session_rows(env) if row.get("running")]
    candidates = []
    agent_statuses = {}
    for session in sessions:
        try:
            panes = _session_panes(session["name"], env)
        except Exception:
            continue
        bound_project = _session_mail_project(bindings, session)
        bound = bool(bound_project) and os.path.realpath(
            os.path.expanduser(bound_project)
        ) == os.path.realpath(os.path.expanduser(project_key))
        for pane in panes:
            pane_agent = str(pane.get("agent") or "")
            matches_type = (
                _runtime_kind(agent_type) == _runtime_kind(pane_agent)
                if _OPAQUE_INSTANCE_RE.fullmatch(instance)
                else _agent_types_match(agent_type, pane_agent)
            )
            if not matches_type or not pane.get("pane_id"):
                continue
            cwd = pane.get("cwd") or pane.get("foreground_cwd") or ""
            agent_statuses[(session["name"], pane["pane_id"])] = str(
                pane.get("agent_status") or ""
            )
            candidates.append((
                session["name"], pane["pane_id"], cwd, bound, bool(bound_project),
                pane.get("_runtime_name"), pane.get("_runtime_kind"),
            ))
    targets = (
        _managed_notify_target(
            candidates, project_key, mail_name, agent_type, instance,
        )
        if _OPAQUE_INSTANCE_RE.fullmatch(instance)
        else _select_notify_targets(candidates, project_key, mail_name=mail_name)
    )
    if not targets:
        if candidates:
            detail = "; ".join(
                f"{s}/{p}(cwd={c or '无'})" for s, p, c, *_ in candidates[:8]
            )
            print(
                f"warning: 无法唯一确定 {agent_type} 的通知目标，已跳过实时通知"
                "（消息保留未读）。候选: " + detail +
                "。多实例场景可用 --session/--pane 显式指定。",
                file=sys.stderr,
            )
        return
    for session, pane_id, cwd, is_exact in targets:
        _deliver_notify_note(
            env, session, pane_id, cwd, "auto" if not is_exact else "exact",
            msg_id, subject, agent_type, instance, project_key, intent,
            agent_status=agent_statuses.get((session, pane_id), ""),
        )


def _deliver_notify_note(
    env: dict, session: str, pane_id: str, cwd: str, source: str,
    msg_id: int, subject: str, agent_type: str, instance: str,
    project_key: str, intent: str, agent_status: str = "",
) -> None:
    try:
        next_profile.require_session(session)
        next_profile.require_project(project_key)
    except next_profile.NextProfileError as exc:
        print(f"warning: 跳过越界 Herdr 通知: {exc}", file=sys.stderr)
        return
    # Herdr prompt 会被 CLI 当成新的用户输入；Agent 正在生成时直接 prompt
    # 会取消当前 turn。此时只保留持久未读消息，等对方空闲或里程碑自查。
    if agent_status == "working":
        print(
            f"notice: {session}/{pane_id} Agent 正在工作，跳过实时通知"
            "（消息保留未读，对方里程碑自查可见）",
            file=sys.stderr,
        )
        return
    if _recipient_typing(session, pane_id):
        print(
            f"notice: {session}/{pane_id} 用户正在输入，跳过实时通知"
            "（消息保留未读，对方里程碑自查可见）",
            file=sys.stderr,
        )
        return
    is_exact = source == "exact" or (
        bool(cwd)
        and os.path.realpath(os.path.expanduser(cwd))
        == os.path.realpath(os.path.expanduser(project_key))
    )
    note = _notify_text(
        msg_id, subject, agent_type, instance, project_key, cwd, is_exact, intent
    )
    try:
        notified = subprocess.run(
            [HERDR_BIN, "--session", session, "agent", "prompt", pane_id, note],
            capture_output=True, text=True, timeout=8, env=env,
        )
        if notified.returncode != 0:
            print(
                f"warning: Herdr 通知失败({session}/{pane_id}): "
                f"{notified.stderr.strip()[:200]}",
                file=sys.stderr,
            )
    except Exception as exc:
        print(
            f"warning: Herdr 通知异常({session}/{pane_id}): {exc}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--instance", default="default")
    parser.add_argument("--project", default=os.getcwd())
    parser.add_argument("--to", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--thread", default="")
    parser.add_argument("--importance", default="normal", choices=["low", "normal", "high", "urgent"])
    parser.add_argument("--intent", default="info", choices=sorted(coordination.INTENTS))
    parser.add_argument("--supersedes", default="", help="被本消息替代的 message_id，逗号分隔")
    parser.add_argument("--expires-in", type=float, default=None, help="仅 info/review 可用，单位秒")
    parser.add_argument("--idempotency-key", default="", help="团队回复重试时复用的幂等键")
    parser.add_argument("--ack", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--session", default="", help="显式通知目标 session（与 --pane 成对）")
    parser.add_argument("--pane", default="", help="显式通知目标 pane id（与 --session 成对）")
    parser.add_argument("--target", default="", help="显式通知目标，格式 session/pane（等价 --session+--pane）")
    args = parser.parse_args(argv)

    explicit_target = None
    if args.target or args.session or args.pane:
        if args.no_notify:
            raise SystemExit(
                "error: --session/--pane/--target 与 --no-notify 冲突")
        if args.target and (args.session or args.pane):
            raise SystemExit("error: --target 与 --session/--pane 不能同时使用")
        if args.target:
            parts = args.target.split("/", 1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise SystemExit("error: --target 格式应为 session/pane")
            target_session, target_pane = parts
        else:
            if not args.session or not args.pane:
                raise SystemExit("error: 显式目标需要同时提供 --session 与 --pane")
            target_session, target_pane = args.session, args.pane
        explicit_target = (target_session, target_pane)

    body = args.body
    if not body and not sys.stdin.isatty():
        body = sys.stdin.read()
    if not body.strip():
        raise SystemExit("error: 正文为空（--body 或 stdin 管道）")
    identity, hub, token = load_identity(args.agent, args.instance, args.project)
    raw_recipients = [item.strip() for item in args.to.split(",") if item.strip()]
    human_recipients: list[str] = []
    agent_recipients: list[str] = []
    for recipient in raw_recipients:
        if recipient.startswith("@"):
            handle = recipient[1:]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", handle):
                raise SystemExit(f"error: 团队成员花名无效: {recipient}")
            if handle.lower() not in {item.lower() for item in human_recipients}:
                human_recipients.append(handle)
        else:
            agent_recipients.append(recipient)
    args.thread = bound_mail_thread(
        args.thread, os.environ.get("HERDR_SESSION") or "",
    )
    session_hint = (args.thread or os.environ.get("HERDR_SESSION") or "").strip()
    agent_recipients = _resolve_registry_recipients(
        agent_recipients, identity["project_key"], session=session_hint,
    )
    recipients = [*agent_recipients, *(f"@{item}" for item in human_recipients)]
    if not recipients:
        raise SystemExit("error: 收件人为空")
    if explicit_target is not None and not agent_recipients:
        raise SystemExit(
            "error: 显式 session/pane 目标只用于本地 Agent 通知，"
            "收件人至少需包含一个 Agent"
        )
    # 显式目标在发送前一次性解析并缓存；发送之后只使用缓存结果，
    # 避免 pane 在两次 resolve 之间消失导致持久发送被误报失败。
    explicit_resolved: dict[str, tuple] = {}
    if explicit_target is not None:
        for recipient in agent_recipients:
            cli_identity = _notification_identity(recipient, identity["project_key"])
            if not cli_identity:
                raise SystemExit(
                    f"error: 已指定显式通知目标，但收件人 {recipient} 的本地"
                    "身份无法解析，不能静默忽略显式目标（消息未发送）")
            try:
                explicit_resolved[recipient] = resolve_explicit_target(
                    explicit_target[0], explicit_target[1],
                    cli_identity[0], cli_identity[1],
                )
            except (ValueError, RuntimeError) as exc:
                raise SystemExit(
                    f"error: 显式通知目标不可用: {exc}（不回退自动选路，消息未发送）"
                ) from exc
    if args.idempotency_key and not 1 <= len(args.idempotency_key.strip()) <= 128:
        raise SystemExit("error: --idempotency-key 长度必须为 1-128")
    try:
        supersedes = [int(item) for item in args.supersedes.split(",") if item.strip()]
    except ValueError:
        raise SystemExit("error: --supersedes 仅接受逗号分隔的整数 message_id")
    try:
        meta, warnings = coordination.prepare_metadata(
            project_key=identity["project_key"], sender=identity["name"],
            recipients=recipients, intent=args.intent, importance=args.importance,
            supersedes=supersedes, expires_in=args.expires_in,
        )
    except ValueError as exc:
        raise SystemExit(f"error: {exc}")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    human_body = body
    body = coordination.add_metadata(body, meta)
    result = {"deliveries": []}
    if agent_recipients:
        mcp_call(hub, token, "initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mail-send", "version": "1.0"},
        })
        result = mcp_tool(hub, token, "send_message", {
            "project_key": identity["project_key"],
            "sender_name": identity["name"],
            "sender_token": identity["registration_token"],
            "to": agent_recipients,
            "subject": args.subject,
            "body_md": body,
            "thread_id": args.thread or None,
            "importance": args.importance,
            "ack_required": args.ack,
            "auto_contact_if_blocked": True,
        })
    if human_recipients:
        reply_key = args.idempotency_key.strip() or uuid.uuid4().hex
        team_result = _team_reply({
            "mail_project": identity["project_key"],
            "sender_name": identity["name"],
            "registration_token": identity["registration_token"],
            "mention_handles": human_recipients,
            "subject": args.subject,
            "body_md": human_body,
            "importance": args.importance,
            "idempotency_key": reply_key,
        })
        outcomes = team_result.get("deliveries")
        if not isinstance(outcomes, list) or not outcomes:
            raise SystemExit(
                f"团队回复状态无效（重试键: {reply_key}）"
            )
        failed = False
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                failed = True
                continue
            status = str(outcome.get("status") or "")
            handle = str(outcome.get("name") or "")
            if status in {"delivered", "delivered_human_inbox", "already_delivered"}:
                print(f"sent: @{handle} -> Team [{status}]")
            else:
                failed = True
                print(
                    f"warning: @{handle} 未送达({outcome.get('reason') or status})",
                    file=sys.stderr,
                )
        if failed:
            raise SystemExit(f"团队回复未全部送达（重试键: {reply_key}）")
    deliveries = result.get("deliveries") or []
    for delivery in deliveries:
        payload = delivery.get("payload", {})
        message_id = payload.get("id")
        print(
            f"sent: #{message_id} -> {','.join(agent_recipients)}  "
            f"[{payload.get('thread_id') or '-'}]"
        )
        if message_id is not None:
            try:
                coordination.register_message(
                    project_key=identity["project_key"], message_id=int(message_id),
                    sender=identity["name"], meta=meta,
                )
            except Exception as exc:
                print(
                    f"warning: 消息 #{message_id} 已发送，但本地消费元数据登记失败: {exc}",
                    file=sys.stderr,
                )
                continue
        if args.no_notify:
            continue
        project_key = identity["project_key"]
        for recipient in payload.get("to") or []:
            program = _program_by_name(recipient, project_key)
            expected_agent = PROG_TO_AGENT.get(program)
            cli_identity = _notification_identity(recipient, project_key)
            if not cli_identity:
                if program:
                    print(
                        f"warning: 找不到 {recipient} 的本机 agent/instance，已跳过通知",
                        file=sys.stderr,
                    )
                continue
            agent_type, instance = cli_identity
            if expected_agent and expected_agent != agent_type:
                print(
                    f"warning: {recipient} 的 registry agent={agent_type} "
                    f"与 program={program} 不匹配，已跳过通知",
                    file=sys.stderr,
                )
                continue
            # 显式目标只用发送前的缓存解析结果；pane 若此刻已消失，
            # _notify_pane 内仅警告"消息已发送、实时通知失败"，不再重新 resolve。
            cached_explicit = explicit_resolved.get(recipient)
            if explicit_target is not None and cached_explicit is None:
                print(
                    f"warning: 消息 #{message_id} 已发送，但 {recipient} 无"
                    "发送前缓存的显式目标，跳过实时通知",
                    file=sys.stderr,
                )
                continue
            try:
                _notify_pane(
                    agent_type, instance, message_id, args.subject, project_key,
                    str(meta.get("intent") or "info"),
                    mail_name=recipient, explicit=cached_explicit,
                )
            except Exception as exc:
                print(
                    f"warning: 消息 #{message_id} 已发送，但通知 {recipient} 失败: {exc}",
                    file=sys.stderr,
                )
    if agent_recipients and not deliveries:
        print(json.dumps(result, ensure_ascii=False)[:300])


if __name__ == "__main__":
    main()
