"""herdr_client.py — herdr CLI subprocess 封装(多 session 聚合)。

herdr 以多个 session 运行,每个 session 有独立 socket。本模块遍历所有 session,
聚合 pane 状态,这是"每个 agent 都可视化"的数据源。

关键修正(对比旧版):不再只查 default socket,而是 herdr session list 枚举所有 session,
逐个 --session <name> 取 snapshot 聚合。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
from concurrent.futures import Future, ThreadPoolExecutor, wait
from functools import lru_cache
from pathlib import Path
from typing import Any

# herdr 二进制:优先用环境变量,其次 PATH 探测,最后试 ~/.local/bin
_HERDR_ENV = os.environ.get("HERDR_BIN")
HERDR_BIN = _HERDR_ENV or shutil.which("herdr") or str(Path.home() / ".local" / "bin" / "herdr")
# herdr 所在的额外 PATH(供子进程找到它)
_HERDR_DIR = str(Path(HERDR_BIN).parent) if HERDR_BIN else ""
PANE_CREATE_TIMEOUT = 3.0
AGENT_START_TIMEOUT = 10.0
QODER_AGENT_START_TIMEOUT = 60.0
QODER_AGENTS = frozenset({"qoder", "qodercli", "qodercn"})
# 其他冷启动慢的 agent 的识别窗口(二进制大/启动自检耗时,如 grok 约 160MB)
SLOW_AGENT_START_TIMEOUTS = {"grok": 60.0}
AGENT_POLL_INTERVAL = 0.2
# agent wait 未显式给 timeout 时的子进程上限：herdr 默认无限等待，Cockpit 不能
# 让子进程永久阻塞，故给一个有限上界；调用方需要更长的真实等待应显式传 timeout_ms。
AGENT_WAIT_DEFAULT_TIMEOUT_S = 60.0
RESTART_SHELL_TIMEOUT_S = 10.0
RESTART_SECOND_INTERRUPT_S = 2.0
MAX_AGENT_ARGS_LENGTH = 2048
SNAPSHOT_SESSION_TIMEOUT_S = 8.0
SNAPSHOT_TOTAL_TIMEOUT_S = 10.0
_SNAPSHOT_DEADLINE = threading.local()
_LIST_SESSIONS_FAILED = threading.local()
_SNAPSHOT_EXECUTOR_LOCK = threading.RLock()
_SNAPSHOT_EXECUTOR: ThreadPoolExecutor | None = None
_SNAPSHOT_FUTURES: set[Future[dict[str, Any]]] = set()
SNAPSHOT_MAX_QUEUED = 64
HERDR_MIN_VERSION = (0, 8, 0)
HERDR_MIN_PROTOCOL = 19
HERDR_MIN_SCHEMA_VERSION = 1
HERDR_REQUIRED_METHODS = frozenset({
    "session.snapshot",
    "agent.list",
    "agent.get",
    "agent.start",
    "agent.read",
    "agent.prompt",
    "agent.wait",
    "agent.send_keys",
    "pane.process_info",
    "events.subscribe",
})
AGENT_KIND_ALIASES = {
    "codex": "codex",
    "claude": "claude",
    "kimi": "kimi",
    "opencode": "opencode",
    "grok": "grok",
    "qoder": "qodercli",
    "qodercli": "qodercli",
    "qodercn": "qodercli",
    "qoderclicn": "qodercli",
}
_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_RESTART_GUARD = threading.Lock()
_RESTARTING_PANES: set[tuple[str, str]] = set()


class HerdrCapabilityError(RuntimeError):
    """安装版 Herdr 不满足 Cockpit 运行时契约。"""


def normalize_agent_kind(agent: str) -> str:
    """把产品侧 agent 别名映射成 Herdr 原生 kind。"""
    kind = AGENT_KIND_ALIASES.get(agent)
    if kind is None:
        raise ValueError(f"不支持的 agent: {agent}")
    return kind


def validate_agent_name(name: str) -> str:
    """验证 Herdr 0.8 的唯一 live agent name 语法。"""
    if not isinstance(name, str) or not _AGENT_NAME_RE.fullmatch(name):
        raise ValueError(
            "实例名称必须以小写字母开头，且只能包含小写字母、数字、_、-，最长 32 位"
        )
    return name


def resolve_unique_agent_name(
    agent: str, requested: str | None, agents: list[dict[str, Any]],
) -> str:
    """根据实时 agent 列表生成或验证 session 内唯一名称。"""
    normalize_agent_kind(agent)
    live_names = {
        str(item.get("name"))
        for item in agents
        if isinstance(item, dict) and item.get("name")
    }
    if requested is not None:
        candidate = validate_agent_name(requested.strip())
        if candidate in live_names:
            raise ValueError(f"实例名称已被占用: {candidate}")
        return candidate
    index = 1
    while True:
        candidate = validate_agent_name(f"{agent}-{index}")
        if candidate not in live_names:
            return candidate
        index += 1


def require_live_pane_id(pane_id: str, panes: list[dict[str, Any]]) -> str:
    """只接受实时快照返回的精确 pane ID，不解析或猜测 opaque handle。"""
    if (
        not isinstance(pane_id, str)
        or not pane_id
        or len(pane_id) > 128
        or pane_id.startswith("-")
        or not pane_id.isascii()
        or any(ord(char) < 32 or ord(char) == 127 for char in pane_id)
    ):
        raise ValueError("pane id 格式无效")
    matches = [
        item for item in panes
        if isinstance(item, dict) and item.get("pane_id") == pane_id
    ]
    if len(matches) != 1:
        raise ValueError(f"pane id 不属于当前实时快照: {pane_id}")
    return pane_id


def _snapshot_future_done(
    pool: ThreadPoolExecutor, future: Future[dict[str, Any]],
) -> None:
    """最后一个 snapshot future 完成后销毁空闲池，避免常驻线程与 PTY fork 并存。"""
    global _SNAPSHOT_EXECUTOR
    shutdown = False
    with _SNAPSHOT_EXECUTOR_LOCK:
        _SNAPSHOT_FUTURES.discard(future)
        if not _SNAPSHOT_FUTURES and _SNAPSHOT_EXECUTOR is pool:
            _SNAPSHOT_EXECUTOR = None
            shutdown = True
    if shutdown:
        pool.shutdown(wait=False, cancel_futures=True)


def _submit_snapshot(
    session: str, deadline: float,
) -> Future[dict[str, Any]] | None:
    """提交到全局有界池；卡死任务最多占 4 线程、排队最多 64 项。"""
    global _SNAPSHOT_EXECUTOR
    with _SNAPSHOT_EXECUTOR_LOCK:
        if len(_SNAPSHOT_FUTURES) >= SNAPSHOT_MAX_QUEUED:
            return None
        if _SNAPSHOT_EXECUTOR is None:
            _SNAPSHOT_EXECUTOR = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="cockpit-snapshot",
            )
        pool = _SNAPSHOT_EXECUTOR
        future = pool.submit(_snapshot_session_safe, session, deadline)
        _SNAPSHOT_FUTURES.add(future)
        future.add_done_callback(
            lambda done, owner=pool: _snapshot_future_done(owner, done)
        )
        return future


def _agent_start_timeout(agent: str) -> float:
    """QoderCLI/grok 等冷启动较慢,其余 Agent 继续使用默认识别窗口。"""
    if agent in QODER_AGENTS:
        return QODER_AGENT_START_TIMEOUT
    return SLOW_AGENT_START_TIMEOUTS.get(agent, AGENT_START_TIMEOUT)


def _find_agent_bin(name: str) -> str:
    """探测 agent 二进制完整路径(shutil.which → 已知安装路径 fallback)。"""
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    # qoderclicn 用 glob 匹配最新版本目录(版本号会变)
    qoder_dir = home / ".qoder-cn" / "bin" / "qoderclicn"
    qoder_bins = sorted(qoder_dir.glob("qoderclicn-*"), reverse=True) if qoder_dir.is_dir() else []
    qoder_path = str(qoder_bins[0]) if qoder_bins else "qoderclicn"
    paths = {
        "codex": [home / ".npm-global" / "bin" / "codex"],
        "kimi": [home / ".kimi-code" / "bin" / "kimi"],
        "claude": [home / ".npm-global" / "bin" / "claude"],
        "qoder": [qoder_path],
        "qodercli": [qoder_path],
        "qodercn": [qoder_path],
        "grok": [home / ".grok" / "downloads" / "grok-linux-x86_64"],
        "opencode": [home / ".opencode" / "bin" / "opencode"],
    }
    for p in paths.get(name, []):
        if p and Path(p).is_file():
            return str(p)
    return name  # 最后兜底用裸名


# agent 类型 → 启动命令构造器
def normalize_agent_args(args: str = "") -> str:
    """把用户输入解析为参数列表后逐项引用，禁止把它升级成 shell 语句。"""
    if not isinstance(args, str):
        raise ValueError("启动参数必须是字符串")
    if len(args) > MAX_AGENT_ARGS_LENGTH:
        raise ValueError(f"启动参数不能超过 {MAX_AGENT_ARGS_LENGTH} 个字符")
    if any(char in args for char in ("\0", "\r", "\n")):
        raise ValueError("启动参数不能包含换行或 NUL 字符")
    try:
        return shlex.join(shlex.split(args, posix=True))
    except ValueError as exc:
        raise ValueError(f"启动参数格式无效: {exc}") from exc


def _agent_cmd(agent: str, workdir: str, args: str = "") -> str:
    """构造 agent 启动命令，二进制、目录和额外参数均经 shlex 安全引用。"""
    wdb = shlex.quote(workdir)
    bins = {
        "codex": lambda b: f"{shlex.quote(b)} -C {wdb}",
        "kimi": lambda b: f"{shlex.quote(b)}",
        "claude": lambda b: f"{shlex.quote(b)}",  # claude 默认用 cwd
        "qoder": lambda b: f"{shlex.quote(b)}",
        "qodercli": lambda b: f"{shlex.quote(b)}",
        "qodercn": lambda b: f"{shlex.quote(b)}",
        "grok": lambda b: f"{shlex.quote(b)}",
        "opencode": lambda b: f"{shlex.quote(b)}",
    }
    bin_path = _find_agent_bin(agent)
    builder = bins.get(agent, bins["codex"])
    cmd = builder(bin_path)
    extra = normalize_agent_args(args)
    return f"{cmd} {extra}" if extra else cmd


def is_available() -> bool:
    return bool(HERDR_BIN) and Path(HERDR_BIN).is_file() and os.access(HERDR_BIN, os.X_OK)


def onboarding_required() -> bool:
    """Herdr 首次配置是否尚未完成；配置损坏交给 Herdr 自身报错。"""
    config_path = Path(
        os.environ.get("HERDR_CONFIG_PATH", "~/.config/herdr/config.toml")
    ).expanduser()
    if not config_path.is_file():
        return True
    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    # Herdr 默认配置约定：缺少 onboarding 也会显示首次配置向导。
    return config.get("onboarding") is not False


def herdr_config_path() -> Path:
    return Path(
        os.environ.get("HERDR_CONFIG_PATH", "~/.config/herdr/config.toml")
    ).expanduser()


def reload_config(timeout: int = 10) -> dict[str, Any]:
    """热重载运行中的 herdr server 配置；返回 ok/reloaded/errors,不抛异常。

    herdr socket 是 per-session 的(~/.config/herdr/sessions/<name>/herdr.sock),
    必须对每个运行中的 session 带 --session 执行 reload-config；
    枚举不到 session 时退回默认 server 命令。
    """
    try:
        sessions = [
            s.get("name") for s in list_sessions()
            if s.get("name") and s.get("status") == "running"
        ]
    except Exception:
        sessions = []
    if not sessions:
        try:
            _run(["server", "reload-config"], timeout=timeout)
            return {"ok": True, "reloaded": [], "errors": []}
        except Exception as exc:
            return {"ok": False, "reloaded": [], "errors": [str(exc)]}
    reloaded: list[str] = []
    errors: list[str] = []
    for name in sessions:
        try:
            _run(["--session", str(name), "server", "reload-config"], timeout=timeout)
            reloaded.append(str(name))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return {"ok": not errors, "reloaded": reloaded, "errors": errors}


_THEME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def set_theme_name(name: str) -> Path:
    """更新 config.toml [theme].name；委托 set_theme_for_web_mode。"""
    light_names = (
        "solarized-light", "catppuccin-latte", "one-light", "gruvbox-light",
        "tokyo-night-day", "kanagawa-lotus", "rose-pine-dawn",
    )
    mode = "light" if "light" in (name or "").lower() or name in light_names else "dark"
    return set_theme_for_web_mode(mode, name_override=name)["path"]


def set_theme_for_web_mode(
    mode: str, name_override: str | None = None,
) -> dict[str, Any]:
    """按 Web light/dark 写 herdr [theme].name，并关闭 auto_switch。

    返回 {"path","name","changed"}。changed=False 时跳过 reload-config，
    避免无配置变更仍全 session 重绘；Mode 2031 / grok slash 仍每次执行。
    """
    if mode not in ("light", "dark"):
        raise ValueError("mode 必须是 light 或 dark")
    dark_name = "catppuccin"
    light_name = "solarized-light"
    if name_override is not None:
        name = name_override
    else:
        name = light_name if mode == "light" else dark_name
    if not _THEME_NAME_RE.fullmatch(name or ""):
        raise ValueError("非法主题名")
    path = herdr_config_path()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.is_file() else []
    out: list[str] = []
    in_theme = False
    seen: set[str] = set()

    def _kv(key: str, value: str, ending: str = "\n") -> str:
        if key == "auto_switch":
            return f"{key} = {value}{ending}"
        return f'{key} = "{value}"{ending}'

    keys_wanted = {
        "name": name,
        "auto_switch": "false",
    }

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_theme:
                for key, val in keys_wanted.items():
                    if key not in seen:
                        out.append(_kv(key, val))
                        seen.add(key)
            in_theme = stripped == "[theme]"
            out.append(line)
            continue
        if in_theme:
            m = re.match(r"^\s*(name|auto_switch|dark_name|light_name)\s*=", line)
            if m:
                key = m.group(1)
                if key in keys_wanted:
                    ending = "\n" if line.endswith("\n") else ""
                    out.append(_kv(key, keys_wanted[key], ending))
                    seen.add(key)
                continue
        out.append(line)
    if in_theme:
        for key, val in keys_wanted.items():
            if key not in seen:
                out.append(_kv(key, val))
                seen.add(key)
    if not seen:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.extend(["\n", "[theme]\n"])
        for key, val in keys_wanted.items():
            out.append(_kv(key, val))

    new_text = "".join(out)
    old_text = "".join(lines)
    if new_text == old_text:
        return {"path": path, "name": name, "changed": False}

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".herdr-config.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"path": path, "name": name, "changed": True}


def _run(args: list[str], timeout: float = 10) -> str:
    """跑 herdr 子命令,注入 PATH,返回 stdout。失败抛 RuntimeError。"""
    extra_path = _HERDR_DIR + (":" + os.environ.get("PATH", "") if os.environ.get("PATH") else "")
    env = {**os.environ, "PATH": extra_path or os.environ.get("PATH", "/usr/bin:/bin")}
    try:
        r = subprocess.run(
            [HERDR_BIN] + args, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except FileNotFoundError:
        raise RuntimeError(f"herdr 未找到: {HERDR_BIN}")
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"herdr {' '.join(args)} 超时(>{timeout}s)") from e
    if r.returncode != 0:
        raise RuntimeError(f"herdr {' '.join(args)} 失败: {r.stderr.strip()[:200]}")
    return r.stdout


def _schema_method_names(value: Any) -> set[str]:
    methods: set[str] = set()
    if isinstance(value, dict):
        method = value.get("method")
        if isinstance(method, dict) and isinstance(method.get("const"), str):
            methods.add(method["const"])
        for child in value.values():
            methods.update(_schema_method_names(child))
    elif isinstance(value, list):
        for child in value:
            methods.update(_schema_method_names(child))
    return methods


def probe_herdr_capabilities() -> dict[str, Any]:
    """读取安装版 CLI 的版本和 schema，拒绝旧能力或静默兼容。"""
    if not is_available():
        raise HerdrCapabilityError("Herdr 未安装；请安装或升级到 0.8.0 以上版本")
    try:
        version_output = _run(["--version"], timeout=5)
    except RuntimeError as exc:
        raise HerdrCapabilityError(f"无法读取 Herdr 版本；请升级或重装 Herdr: {exc}") from exc
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version_output)
    if not match:
        raise HerdrCapabilityError("无法识别 Herdr 版本；请升级或重装 Herdr")
    version_tuple = tuple(int(part) for part in match.groups())
    version = ".".join(match.groups())
    if version_tuple < HERDR_MIN_VERSION:
        raise HerdrCapabilityError(
            f"Herdr {version} 不受支持；请升级到 0.8.0 以上版本"
        )

    try:
        schema_output = _run(["api", "schema", "--json"], timeout=5)
        schema = json.loads(schema_output)
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise HerdrCapabilityError(
            f"Herdr API schema 无效；请升级或重装 Herdr: {exc}"
        ) from exc
    if not isinstance(schema, dict):
        raise HerdrCapabilityError("Herdr API schema 无效；请升级或重装 Herdr")
    protocol = schema.get("protocol")
    schema_version = schema.get("schema_version")
    if not isinstance(protocol, int) or isinstance(protocol, bool):
        raise HerdrCapabilityError("Herdr API schema 缺少 protocol；请升级 Herdr")
    if protocol < HERDR_MIN_PROTOCOL:
        raise HerdrCapabilityError(
            f"Herdr protocol {protocol} 不受支持；请升级到 protocol 19 以上版本"
        )
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version < HERDR_MIN_SCHEMA_VERSION
    ):
        raise HerdrCapabilityError(
            "Herdr API schema_version 不受支持；请升级 Herdr"
        )
    methods = _schema_method_names(schema.get("schemas"))
    missing = HERDR_REQUIRED_METHODS - methods
    if missing:
        raise HerdrCapabilityError(
            "Herdr API 缺少必要方法 " + ", ".join(sorted(missing)) + "；请升级 Herdr"
        )
    return {
        "version": version,
        "protocol": protocol,
        "schema_version": schema_version,
        "methods": sorted(HERDR_REQUIRED_METHODS),
    }


@lru_cache(maxsize=1)
def require_herdr_capabilities() -> dict[str, Any]:
    """每个 Cockpit 进程只探测一次安装版 Herdr 能力。"""
    return probe_herdr_capabilities()


def list_sessions() -> list[dict[str, Any]]:
    """枚举所有 herdr session。返回 [{name, status, directory, socket}]。"""
    if not is_available():
        return []
    _LIST_SESSIONS_FAILED.value = False
    try:
        out = _run(["session", "list", "--json"], timeout=_snapshot_timeout())
        data = json.loads(out)
        if not isinstance(data, dict):
            raise ValueError("session list 不是对象")
        rows = data.get("sessions", [])
        if not isinstance(rows, list):
            raise ValueError("sessions 不是列表")
        return [
            {
                "name": str(row.get("name", "")),
                "status": "running" if row.get("running") else "stopped",
                "directory": str(row.get("session_dir", "")),
                "socket": str(row.get("socket_path", "")),
            }
            for row in rows
            if isinstance(row, dict) and row.get("name")
        ]
    except (RuntimeError, ValueError, json.JSONDecodeError):
        # 兼容尚未支持 --json 的旧版 herdr；新版使用稳定 JSON，避免表格
        # 列宽、路径空格或展示格式变化导致 running session 被误判为缺失。
        try:
            out = _run(["session", "list"], timeout=_snapshot_timeout())
        except RuntimeError:
            _LIST_SESSIONS_FAILED.value = True
            return []
    sessions = []
    for line in out.splitlines():
        # 解析表格行:name status directory socket
        parts = line.split()
        if len(parts) >= 4 and parts[0] not in ("name", ""):
            sessions.append({
                "name": parts[0],
                "status": parts[1],
                "directory": parts[2] if len(parts) > 2 else "",
                "socket": parts[-1],
            })
    return sessions


def _slim_layout(layout: dict[str, Any]) -> dict[str, Any]:
    """精简 PaneLayoutSnapshot:zoom 状态 + pane 几何 + 水平分屏判定。"""
    panes = []
    for p in layout.get("panes") or []:
        rect = p.get("rect") or {}
        panes.append({
            "pane_id": p.get("pane_id"),
            "focused": p.get("focused", False),
            "x": rect.get("x", 0),
            "y": rect.get("y", 0),
            "width": rect.get("width", 0),
            "height": rect.get("height", 0),
        })
    ys = [p["y"] for p in panes]
    return {
        "tab_id": layout.get("tab_id"),
        "workspace_id": layout.get("workspace_id"),
        "zoomed": bool(layout.get("zoomed", False)),
        "focused_pane_id": layout.get("focused_pane_id"),
        "panes": panes,
        # 同一 y 上有 ≥2 个 pane 即左右水平分屏(窄屏需单 pane 聚焦的场景)
        "horizontal_split": len(panes) > 1 and len(set(ys)) < len(ys),
        "area": layout.get("area") or {},
    }


def _snapshot_timeout() -> float:
    deadline = getattr(_SNAPSHOT_DEADLINE, "value", None)
    if deadline is None:
        return SNAPSHOT_SESSION_TIMEOUT_S
    return max(0.001, min(SNAPSHOT_SESSION_TIMEOUT_S, deadline - time.monotonic()))


def _snapshot_session(session: str) -> dict[str, Any]:
    """取单个 session 的 snapshot,返回精简后的 {panes, agents}。"""
    try:
        out = _run(["api", "snapshot", "--session", session], timeout=_snapshot_timeout())
    except RuntimeError as e:
        return {"session": session, "error": str(e), "panes": []}
    # 解析 SSE data: 行
    raw = out
    for line in out.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        # 可能直接是 JSON-RPC 错误
        return {"session": session, "error": "snapshot parse failed", "panes": []}
    if not isinstance(data, dict):
        return {"session": session, "error": "snapshot parse failed", "panes": []}
    if "error" in data:
        return {"session": session, "error": str(data["error"]), "panes": []}
    result = data.get("result")
    if not isinstance(result, dict):
        return {"session": session, "error": "snapshot parse failed", "panes": []}
    snap = result.get("snapshot")
    if not isinstance(snap, dict):
        return {"session": session, "error": "snapshot parse failed", "panes": []}
    panes = snap.get("panes", [])
    if not isinstance(panes, list):
        return {"session": session, "error": "snapshot parse failed", "panes": []}
    slim = []
    for p in panes:
        if not isinstance(p, dict):
            continue
        cwd = p.get("cwd") or p.get("foreground_cwd") or ""
        slim.append({
            "pane_id": p.get("pane_id"),
            "session": session,
            "workspace_id": p.get("workspace_id"),
            "tab_id": p.get("tab_id"),
            "agent": p.get("agent"),  # codex/kimi/qodercli/None
            "agent_status": p.get("agent_status"),  # idle/working/blocked/done/unknown
            "cwd": cwd,
            "cwd_name": cwd.rstrip("/").split("/")[-1] if cwd else "",
            "label": p.get("label"),
            "terminal_title": p.get("terminal_title_stripped") or p.get("terminal_title"),
            "focused": p.get("focused", False),
            "revision": p.get("revision", 0),
        })
    return {
        "session": session,
        "status": "running",
        "panes": slim,
        "agents": snap.get("agents", []) if isinstance(snap.get("agents", []), list) else [],
        "focused_pane_id": snap.get("focused_pane_id"),
        # 各 tab 的 zoom 状态与几何(窄屏 attach 判断单 pane 聚焦用)
        "layouts": [
            _slim_layout(l) for l in snap.get("layouts", []) if isinstance(l, dict)
        ],
    }


def _snapshot_session_safe(session: str, deadline: float) -> dict[str, Any]:
    """_snapshot_session 的安全包装:捕获一切异常,绝不抛出。

    _snapshot_session 内部已 try/except herdr 调用失败,但若其解析逻辑本身有 bug
    抛出非 RuntimeError(如 KeyError/TypeError),会中断整轮 snapshot 的 future 收集。
    这里兜底:任何异常都转成与失败一致的结构(session/error/panes 空),保持顺序。
    """
    if time.monotonic() >= deadline:
        return {"session": session, "error": "snapshot total timeout", "panes": []}
    _SNAPSHOT_DEADLINE.value = deadline
    try:
        result = _snapshot_session(session)
        if not isinstance(result, dict):
            return {"session": session, "error": "snapshot returned non-dict", "panes": []}
        return result
    except Exception as e:
        return {"session": session, "error": f"snapshot crashed: {e}", "panes": []}
    finally:
        try:
            del _SNAPSHOT_DEADLINE.value
        except AttributeError:
            pass


def snapshot(
    *, exclude_sessions: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """聚合所有 running session 的 pane,这是 agent 全景视图。

    exclude_sessions 仅供 scoped canary 的兼容快照使用：在提交 worker 前排除
    已由 socket cache 接管的 session，避免名义切换后仍 fork 对应 CLI snapshot。
    多 session 的 snapshot 调用各 fork 一个 herdr 子进程;串行执行时 N 个 session
    要等 N 倍单次耗时。用有界线程池并行,worker 上限 min(4, N),结果按 list_sessions
    原顺序回填(不用 as_completed)。_snapshot_session_safe 兜底一切异常,单个 session
    失败/崩溃返回 error dict 不影响其他,顺序稳定。所有 worker 共用整轮 deadline,
    后续批次只拿剩余预算,避免 N>4 时总耗时按批次倍增。所有 N>=1 都走
    worker,这样即使内部逻辑不合作,调用方也能按整轮 deadline 返回。
    """
    if not is_available():
        return {"available": False, "sessions": [], "panes": [], "agents": []}
    deadline = time.monotonic() + SNAPSHOT_TOTAL_TIMEOUT_S
    _SNAPSHOT_DEADLINE.value = deadline
    _LIST_SESSIONS_FAILED.value = False
    try:
        sessions = list_sessions()
    finally:
        try:
            del _SNAPSHOT_DEADLINE.value
        except AttributeError:
            pass
    if getattr(_LIST_SESSIONS_FAILED, "value", False):
        return {
            "available": False, "sessions": [], "panes": [], "agents": [],
            "error": "session list failed",
        }
    excluded = exclude_sessions or set()
    running = [
        session for session in sessions
        if session.get("status") == "running" and session.get("name") not in excluded
    ]
    if not running:
        return {
            "available": True, "sessions": [], "panes": [],
            "total_panes": 0, "agent_panes": 0,
        }
    # 按 list_sessions 顺序提交,收集到同序的 future 列表,保证 results 顺序稳定。
    snaps: list[dict[str, Any] | None] = [None] * len(running)
    futures = [_submit_snapshot(s["name"], deadline) for s in running]
    submitted = [future for future in futures if future is not None]
    remaining = max(0.0, deadline - time.monotonic())
    done, pending = wait(submitted, timeout=remaining)
    for i, fut in enumerate(futures):
        if fut is None:
            snaps[i] = {
                "session": running[i]["name"],
                "error": "snapshot worker queue full", "panes": [],
            }
            continue
        if fut not in done:
            snaps[i] = {
                "session": running[i]["name"],
                "error": "snapshot total timeout", "panes": [],
            }
            continue
        try:
            snaps[i] = fut.result()
        except Exception as e:
            snaps[i] = {"session": running[i]["name"], "error": f"future failed: {e}", "panes": []}
    for fut in pending:
        fut.cancel()
    results: list[dict[str, Any]] = []
    all_panes: list[dict[str, Any]] = []
    for s, snap in zip(running, snaps):
        snap["directory"] = s.get("directory", "")
        results.append(snap)
        all_panes.extend(snap.get("panes", []))
    return {
        "available": True,
        "sessions": results,
        "panes": all_panes,  # 扁平化的所有 pane,前端可按 session 分组
        "total_panes": len(all_panes),
        "agent_panes": sum(1 for p in all_panes if p.get("agent")),
    }


def pane_read(session: str, pane_id: str, lines: int = 100, is_agent: bool = False) -> dict[str, Any]:
    """读 pane 终端输出。agent pane 用 `agent read`(能拿到对话),普通终端用 `pane read`。"""
    if not is_available():
        return {"available": False}
    try:
        if is_agent:
            # agent read:位置参数 pane_id,--session 全局前置
            out = _run(
                ["--session", session, "agent", "read", pane_id, "--lines", str(lines)],
                timeout=8,
            )
        else:
            out = _run(
                ["--session", session, "pane", "read", pane_id, "--lines", str(lines)],
                timeout=8,
            )
        return {"available": True, "session": session, "pane_id": pane_id, "output": out}
    except RuntimeError as e:
        if is_agent and "agent_not_idle" in str(e):
            try:
                out = _run(
                    [
                        "--session", session, "agent", "read", pane_id,
                        "--source", "visible", "--lines", str(lines),
                    ],
                    timeout=8,
                )
            except RuntimeError as fallback_error:
                return {
                    "available": True,
                    "error": str(fallback_error),
                    "output": "",
                }
            return {
                "available": True,
                "session": session,
                "pane_id": pane_id,
                "output": out,
                "source": "visible",
                "degraded": True,
                "notice": "Agent 正在运行，仅显示当前画面；空闲后自动恢复完整历史。",
            }
        return {"available": True, "error": str(e), "output": ""}


def pane_summary(session: str, pane_id: str, max_lines: int = 30) -> dict[str, Any]:
    """取 agent 最近会话的摘要(@ 引用会话用)。

    读 agent read 输出,过滤掉 TUI 装饰行(边框/状态栏/空行),
    只保留对话内容(用户消息 ›、agent 回复 •、普通输出行),截取尾部 max_lines 行。
    """
    if not is_available():
        return {"available": False}
    try:
        out = _run(["--session", session, "agent", "read", pane_id], timeout=8)
    except RuntimeError as e:
        return {"available": True, "error": str(e), "summary": ""}
    # 过滤 TUI 噪声:边框字符、纯空行、状态栏、超长装饰线
    noise_prefixes = (
        "─", "═", "│", "╭", "╰", "╮", "╯", "•  └",  # 边框
        "  gpt-", "  context:", "  yolo", "  K3",   # 状态栏
        "Token usage", "Tip:", "Use /",             # 启动提示
    )
    kept = []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        if any(s.startswith(p) for p in noise_prefixes):
            continue
        # 跳过纯装饰长横线
        if set(s) <= {"─", "═", " ", "│"} and len(s) > 20:
            continue
        kept.append(line.rstrip())
    # 取尾部
    summary_lines = kept[-max_lines:] if len(kept) > max_lines else kept
    return {
        "available": True,
        "session": session,
        "pane_id": pane_id,
        "summary": "\n".join(summary_lines),
        "line_count": len(summary_lines),
    }


def _parse_data_json(out: str) -> dict[str, Any] | None:
    """解析 herdr CLI 输出:SSE `data:` 行优先,否则按整体 JSON。失败返回 None。"""
    raw = out
    for line in out.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def pane_layout(session: str, pane_id: str | None = None) -> dict[str, Any]:
    """查询 pane 所在 tab 的布局:zoom 状态与 pane 几何。

    窄屏(手机)attach 水平分屏时做单 pane 聚焦判断的依据:
      zoomed            当前 tab 是否已有 zoom 的 pane(还原/不重复 zoom 用)
      focused_pane_id   当前焦点 pane(zoom 默认目标)
      horizontal_split  同一 y 上有 ≥2 个 pane(左右水平分屏)
    pane_id 省略时查询 UI 焦点 pane 所在 tab。
    """
    if not is_available():
        return {"available": False}
    args = ["--session", session, "pane", "layout"]
    if pane_id:
        args += ["--pane", pane_id]
    try:
        out = _run(args, timeout=8)
    except RuntimeError as e:
        return {"available": True, "error": str(e)}
    data = _parse_data_json(out)
    if not data:
        return {"available": True, "error": "pane layout 输出解析失败"}
    result = data.get("result", data)
    layout = result.get("layout", result)
    if not isinstance(layout.get("panes"), list):
        return {"available": True, "error": "pane layout 输出缺少 panes"}
    return {"available": True, "session": session, **_slim_layout(layout)}


def pane_zoom(
    session: str, pane_id: str | None = None, mode: str = "on",
) -> dict[str, Any]:
    """显式设置 pane zoom(窄屏 attach 聚焦单 pane、退出还原用)。

    只接受显式 on/off(不用 toggle,避免并发/共享状态下语义漂移):
    已是目标态时 herdr 返回 reason=already_zoomed/already_unzoomed 且
    changed=False,幂等。pane_id 省略时作用于 UI 焦点 pane。
    返回含 tab_id 与 horizontal_split(取自带回的 layout),便于调用方
    识别并拒绝"用户已手动 zoom"的场景。失败降级 {available,error},不抛异常。
    """
    if not is_available():
        return {"available": False}
    if mode not in ("on", "off"):
        return {"available": True, "error": f"非法 zoom mode(仅支持 on/off): {mode}"}
    args = ["--session", session, "pane", "zoom"]
    if pane_id:
        args.append(pane_id)
    args.append(f"--{mode}")
    try:
        out = _run(args, timeout=8)
    except RuntimeError as e:
        return {"available": True, "error": str(e)}
    data = _parse_data_json(out)
    if not data:
        return {"available": True, "error": "pane zoom 输出解析失败"}
    result = data.get("result", data)
    zoom = result.get("zoom", result)
    if "zoomed" not in zoom:
        return {"available": True, "error": "pane zoom 输出缺少 zoomed"}
    layout = zoom.get("layout")
    slim = _slim_layout(layout) if isinstance(layout, dict) else {}
    return {
        "available": True,
        "session": session,
        "pane_id": zoom.get("pane_id"),
        "zoomed": bool(zoom.get("zoomed")),
        "changed": bool(zoom.get("zoom_changed", zoom.get("changed", False))),
        "reason": zoom.get("reason"),
        "focused_pane_id": zoom.get("focused_pane_id"),
        "tab_id": slim.get("tab_id"),
        "horizontal_split": slim.get("horizontal_split", False),
    }


def pane_send(session: str, pane_id: str, text: str, mode: str = "prompt") -> dict[str, Any]:
    """往 pane 发送。

    mode:
      prompt    → agent pane 用 `agent prompt`(把文本作为提示提交给 agent)
      send      → 普通终端用 `pane send-text`(发文本)+ Enter(执行)
      keys      → 按键序列用 `pane send-keys`(只接受按键名如 Enter C-c Esc)
      slash     → agent TUI 斜杠命令：send-text + Enter（不走 agent prompt，避免当聊天提交）
    正确语法统一为 `herdr --session <s> <subcmd> <pane_id> ...`(session 全局前置)。
    """
    if not is_available():
        return {"available": False}
    try:
        if mode == "prompt":
            _run(["--session", session, "agent", "prompt", pane_id, text], timeout=10)
        elif mode == "send":
            # 普通命令：原子 pane run 一次性发命令并提交回车，不再拆成
            # send-text + send-keys 两次（拆分会让 agent TUI 把首段当 prompt）。
            _run(["--session", session, "pane", "run", pane_id, text], timeout=8)
        elif mode == "slash":
            # Grok 等自绘 TUI：/theme light 必须当斜杠命令键入，不能 agent prompt。
            cmd = str(text or "").strip()
            if not cmd.startswith("/"):
                cmd = "/" + cmd
            _run(["--session", session, "pane", "send-text", pane_id, cmd], timeout=5)
            _run(["--session", session, "pane", "send-keys", pane_id, "Enter"], timeout=5)
        else:  # keys
            keys = text.split()
            _run(["--session", session, "pane", "send-keys", pane_id] + keys, timeout=5)
        return {"available": True, "sent": text, "mode": mode}
    except RuntimeError as e:
        return {"available": True, "error": str(e)}


def grok_theme_slash(mode: str) -> str:
    """Web light/dark → Grok /theme 目标（见 grok user-guide 06-theming）。

    Grok 接受 light/dark 别名（GrokDay/GrokNight），也接受 /theme grokday 等。
    """
    return "/theme light" if mode == "light" else "/theme dark"


# Web light/dark → OpenCode 内置主题名（用户指定；勿用 Mode 2031 当唯一手段）
OPENCODE_THEME_BY_WEB = {
    "light": "palenight",
    "dark": "aura",
}


def agent_theme_slash(agent: str, mode: str) -> str | None:
    """各 agent 可直接一次发完的主题 slash；需多步 UI 的返回 None 由专用路径处理。

    - grok: `/theme light|dark`
    - opencode: 不走 light/dark 指令，统一 /themes → 主题名（palenight / aura）
    """
    if mode not in ("light", "dark"):
        return None
    kind = str(agent or "").strip().lower()
    if kind == "grok":
        return grok_theme_slash(mode)
    return None


def opencode_theme_name(mode: str) -> str:
    """Web light/dark → OpenCode 主题 key（亮 palenight / 暗 aura）。"""
    if mode not in OPENCODE_THEME_BY_WEB:
        raise ValueError("mode 必须是 light 或 dark")
    return OPENCODE_THEME_BY_WEB[mode]


def set_opencode_tui_theme(theme_name: str) -> Path:
    """写入 ~/.config/opencode/tui.json 的 theme，供新建/重启 OpenCode 继承。"""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", theme_name or ""):
        raise ValueError("非法 OpenCode 主题名")
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    path = root / "opencode" / "tui.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"现有 OpenCode tui.json 不是有效 JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("现有 OpenCode tui.json 必须是 JSON 对象")
        data = raw
    data["$schema"] = data.get("$schema") or "https://opencode.ai/tui.json"
    data["theme"] = theme_name
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(
        prefix=".tui-theme.", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def apply_opencode_theme_to_pane(
    session: str, pane_id: str, theme_name: str,
) -> dict[str, Any]:
    """通过 OpenCode 主题弹层切换主题，不触碰或提交 composer 草稿。"""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", theme_name or ""):
        return {"error": "非法主题名"}

    prefix = ["--session", session, "pane"]
    dialog_open = False
    try:
        # Ctrl+X,T 直接打开独立主题弹层，OpenCode 会保留已有 composer 草稿。
        _run(prefix + ["send-keys", pane_id, "ctrl+x", "t"], timeout=5)
        _run(
            prefix + [
                "wait-output", pane_id,
                "--regex", r"^\s*Themes\s+esc\s*$",
                "--source", "visible", "--lines", "60",
                "--timeout", "2000", "--raw",
            ],
            timeout=3,
        )
        dialog_open = True
        _run(prefix + ["send-keys", pane_id, "ctrl+u"], timeout=5)
        _run(prefix + ["send-text", pane_id, theme_name], timeout=5)
        time.sleep(0.25)
        _run(
            prefix + [
                "wait-output", pane_id,
                "--regex", rf"^\s*(?:●\s+)?{re.escape(theme_name)}\s*$",
                "--source", "visible", "--lines", "60",
                "--timeout", "2000", "--raw",
            ],
            timeout=3,
        )
        _run(prefix + ["send-keys", pane_id, "Enter"], timeout=5)
        for _ in range(10):
            time.sleep(0.1)
            screen = _run(
                prefix + [
                    "read", pane_id, "--source", "visible",
                    "--lines", "60", "--format", "text",
                ],
                timeout=5,
            )
            if not re.search(r"(?m)^\s*Themes\s+esc\s*$", screen):
                dialog_open = False
                break
        if dialog_open:
            raise RuntimeError("OpenCode 主题弹层确认后未关闭")
    except RuntimeError as exc:
        if dialog_open:
            try:
                _run(prefix + ["send-keys", pane_id, "esc"], timeout=5)
            except RuntimeError:
                pass
        return {"error": str(exc)}
    return {
        "available": True, "sent": f"theme-dialog → {theme_name}",
        "mode": "opencode-theme-pick",
    }


def apply_agent_web_themes(mode: str) -> dict[str, Any]:
    """把 Web 明暗推到 live agent pane（按 agent 原生主题手段）。

    - grok: `/theme light|dark`（自绘，light/dark 就是主题别名）
    - opencode: 同一条路 /themes → 主题名（亮 palenight / 暗 aura）+ tui.json
      不是「//theme dark」另一套；OpenCode 的明暗靠换主题包，不是换 mode 指令。
    """
    if mode not in ("light", "dark"):
        raise ValueError("mode 必须是 light 或 dark")
    applied: list[dict[str, str]] = []
    errors: list[str] = []
    skipped: list[dict[str, str]] = []
    opencode_theme = opencode_theme_name(mode)
    tui_path: str | None = None
    try:
        tui_path = str(set_opencode_tui_theme(opencode_theme))
    except Exception as exc:
        errors.append(f"opencode:tui.json: {exc}")

    try:
        snap = snapshot()
    except Exception as exc:
        errors.append(f"snapshot: {exc}")
        return {
            "ok": False, "applied": [], "errors": errors,
            "skipped": [], "mode": mode, "opencode_theme": opencode_theme,
            "tui_json": tui_path,
        }
    for pane in snap.get("panes") or []:
        if not isinstance(pane, dict):
            continue
        agent = str(pane.get("agent") or "").strip().lower()
        if not agent:
            continue
        session = str(pane.get("session") or "")
        pane_id = str(pane.get("pane_id") or "")
        if not session or not pane_id:
            continue

        if agent in ("opencode", "open-code"):
            if tui_path is None:
                skipped.append({
                    "session": session, "pane_id": pane_id,
                    "agent": "opencode", "reason": "tui_config_write_failed",
                })
                continue
            # 亮/暗同一机制：/themes 选中不同主题名
            result = apply_opencode_theme_to_pane(session, pane_id, opencode_theme)
            if result.get("error"):
                errors.append(f"opencode:{session}/{pane_id}: {result['error']}")
            else:
                applied.append({
                    "session": session, "pane_id": pane_id,
                    "agent": "opencode", "command": f"/themes→{opencode_theme}",
                })
            continue

        cmd = agent_theme_slash(agent, mode)
        if not cmd:
            skipped.append({
                "session": session, "pane_id": pane_id, "agent": agent,
                "reason": "no_theme_slash",
            })
            continue
        result = pane_send(session, pane_id, cmd, mode="slash")
        if result.get("error"):
            errors.append(f"{agent}:{session}/{pane_id}: {result['error']}")
        else:
            applied.append({
                "session": session, "pane_id": pane_id,
                "agent": agent, "command": cmd,
            })
    return {
        "ok": not errors,
        "applied": applied,
        "errors": errors,
        "skipped": skipped,
        "mode": mode,
        "opencode_theme": opencode_theme,
        "tui_json": tui_path,
    }


def apply_grok_web_theme(mode: str) -> dict[str, Any]:
    """兼容旧名：只推 grok pane 的 /theme slash。"""
    if mode not in ("light", "dark"):
        raise ValueError("mode 必须是 light 或 dark")
    cmd = grok_theme_slash(mode)
    applied: list[dict[str, str]] = []
    errors: list[str] = []
    try:
        panes = snapshot().get("panes") or []
    except Exception as exc:
        return {
            "ok": False, "applied": [], "errors": [f"snapshot: {exc}"],
            "command": cmd,
        }
    for pane in panes:
        if not isinstance(pane, dict) or str(pane.get("agent") or "").lower() != "grok":
            continue
        session = str(pane.get("session") or "")
        pane_id = str(pane.get("pane_id") or "")
        if not session or not pane_id:
            continue
        result = pane_send(session, pane_id, cmd, mode="slash")
        if result.get("error"):
            errors.append(f"{session}/{pane_id}: {result['error']}")
        else:
            applied.append({"session": session, "pane_id": pane_id})
    return {
        "ok": not errors,
        "applied": applied,
        "errors": errors,
        "command": cmd,
    }


def grok_launch_theme_args(mode: str | None) -> list[str]:
    """新建 grok 时附加的原生 argv：浅色用 --light，暗色不传（默认 GrokNight）。"""
    if mode == "light":
        return ["--light"]
    return []


# Web 主题最近一次同步结果（供 start_agent 注入 grok --light）
_WEB_THEME_MODE: str | None = None


def set_web_theme_mode(mode: str | None) -> None:
    global _WEB_THEME_MODE
    _WEB_THEME_MODE = mode if mode in ("light", "dark") else None


def current_web_theme_mode() -> str | None:
    return _WEB_THEME_MODE


def agent_wait(
    session: str, target: str, until: list[str] | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """等 agent 进入指定状态，使用原生 `agent wait`，不轮询也不键盘模拟。

    target 优先用稳定唯一 agent name（跨 workspace 移动后 pane id 会变），
    也兼容 pane id。until 为空时沿用 herdr 默认（idle/done/blocked）。
    timeout_ms 同时作为 herdr --timeout（毫秒）；省略时不向 herdr 传 --timeout，
    子进程等待由 AGENT_WAIT_DEFAULT_TIMEOUT_S 兜底，避免永久阻塞。
    """
    if not is_available():
        return {"available": False}
    argv = ["--session", session, "agent", "wait", target]
    for status in until or []:
        argv += ["--until", str(status)]
    if timeout_ms is not None:
        argv += ["--timeout", str(int(timeout_ms))]
        subprocess_timeout = timeout_ms / 1000.0 + 5
    else:
        subprocess_timeout = AGENT_WAIT_DEFAULT_TIMEOUT_S
    try:
        _run(argv, timeout=subprocess_timeout)
        return {
            "available": True, "session": session, "target": target, "matched": True,
        }
    except RuntimeError as exc:
        return {
            "available": True, "session": session, "target": target,
            "matched": False, "error": str(exc),
        }


# ── managed launch descriptor ──────────────────────────────────────────
# Herdr AgentInfo/snapshot 不保留原始 `agent start` argv，process-info 也不能当
# 重建契约。Cockpit 在原生启动成功时把权威契约 {name, kind, args} 持久化，供
# restart 按 session+pane/name 精确取回，绝不在 restart 时从进程 argv/label/类型
# 默认值猜测。key 为 session|name（name 是 session 内唯一且跨 workspace 移动稳定）。
_LAUNCH_DESCRIPTOR_LOCK = threading.RLock()


def launch_descriptors_path() -> Path:
    """launch descriptor 持久化路径；可用 COCKPIT_LAUNCH_DESCRIPTORS_PATH 覆盖（测试用）。"""
    configured = os.environ.get("COCKPIT_LAUNCH_DESCRIPTORS_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "dashboard-data" / "launch-descriptors.json"


def _load_launch_descriptors() -> dict[str, Any]:
    path = launch_descriptors_path()
    if not path.is_file():
        return {"schema": 1, "descriptors": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": 1, "descriptors": {}}
    if not isinstance(data, dict) or not isinstance(data.get("descriptors"), dict):
        return {"schema": 1, "descriptors": {}}
    return data


def _save_launch_descriptors(data: dict[str, Any]) -> None:
    path = launch_descriptors_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".launch-desc.", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_launch_descriptor(
    *, session: str, pane_id: str, name: str, kind: str, args: list[str],
    agent: str | None = None, workdir: str | None = None,
) -> dict[str, Any]:
    """原生启动成功后持久化权威 launch 契约；返回写入的规范化记录。

    name 是 resolve_unique_agent_name 给出的 session 内唯一运行时名；kind 是
    canonical Herdr kind；args 是传给 `--` 的原生 argv 列表（保留空格/分号等原样）。
    """
    record = {
        "session": str(session),
        "name": str(name),
        "kind": str(kind),
        "args": [str(a) for a in args] if isinstance(args, (list, tuple)) else [],
        "agent": agent,
        "pane_id": str(pane_id),
        "workdir": workdir,
    }
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
        data["descriptors"][f"{session}|{name}"] = record
        _save_launch_descriptors(data)
    return dict(record)


def get_launch_descriptor(session: str, pane_id: str) -> dict[str, Any] | None:
    """按 session+pane 精确取回 launch 契约；不存在返回 None（restart 不得猜测）。"""
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
    for record in data["descriptors"].values():
        if (
            isinstance(record, dict)
            and record.get("session") == session
            and record.get("pane_id") == pane_id
        ):
            return dict(record)
    return None


def get_launch_descriptor_by_name(session: str, name: str) -> dict[str, Any] | None:
    """按 session+唯一 name 取回 launch 契约（name 跨 workspace 移动稳定）。"""
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
    record = data["descriptors"].get(f"{session}|{name}")
    return dict(record) if isinstance(record, dict) else None


def clear_launch_descriptors(session: str) -> dict[str, Any]:
    """删除某 session 的全部 launch descriptor。

    仅在 `herdr session delete` 成功后调用：Herdr 删除 session 后以同名重建会重新分配
    workspace/pane/name ID，上一代 descriptor 会与新 live identity 假匹配，让 restart
    误用旧 kind/args。返回 cleared 计数；落盘失败时返回 error，由调用方结构化暴露，
    不静默宣告 descriptor 已安全。
    """
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
        descriptors = data["descriptors"]
        doomed = [
            key for key, record in descriptors.items()
            if isinstance(record, dict) and record.get("session") == session
        ]
        if not doomed:
            return {"cleared": 0}
        for key in doomed:
            del descriptors[key]
        try:
            _save_launch_descriptors(data)
        except OSError as exc:
            return {"cleared": 0, "error": str(exc)}
        return {"cleared": len(doomed)}


def clear_launch_descriptor_by_pane(session: str, pane_id: str) -> dict[str, Any]:
    """删除某 session+pane 对应的 launch descriptor。

    pane close 后 Herdr 可能复用该 opaque pane ID；若不清理，新 agent 落到复用 ID 时，
    get_launch_descriptor 会把旧记录误当当前契约。仅在 `herdr pane close` 成功后调用。
    """
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
        descriptors = data["descriptors"]
        doomed = [
            key for key, record in descriptors.items()
            if isinstance(record, dict)
            and record.get("session") == session
            and record.get("pane_id") == pane_id
        ]
        if not doomed:
            return {"cleared": 0}
        for key in doomed:
            del descriptors[key]
        try:
            _save_launch_descriptors(data)
        except OSError as exc:
            return {"cleared": 0, "error": str(exc)}
        return {"cleared": len(doomed)}


def _rename_agent_context(
    session: str, pane: dict[str, Any], agent: str, layout: str,
    label: str | None = None,
) -> None:
    """把 Herdr 实际展示的 workspace/tab/pane 都改成可辨认名称。"""
    pane_id = pane.get("pane_id")
    tab_id = pane.get("tab_id")
    workspace_id = pane.get("workspace_id")
    display_name = label or agent
    commands = []
    if pane_id:
        commands.append([
            "--session", session, "pane", "rename", pane_id, display_name,
        ])
    if tab_id:
        tab_label = display_name if layout == "tab" else session
        commands.append(["--session", session, "tab", "rename", tab_id, tab_label])
    if workspace_id:
        commands.append([
            "--session", session, "workspace", "rename", workspace_id, session,
        ])
    for command in commands:
        try:
            _run(command, timeout=5)
        except RuntimeError:
            pass


def start_agent(
    session: str, workdir: str, agent: str = "codex", model: str | None = None,
    layout: str = "tab", label: str | None = None, args: str = "",
) -> dict[str, Any]:
    """在指定 session 里启动一个 agent pane(新建 tab/pane 跑 agent)。

    全部受支持 agent 统一用 Herdr 原生 `agent start`：先按 layout 创建 pane 并
    解析响应 ID，再以唯一 name + --kind + --pane 启动，readiness 由 --timeout 兜底。
    agent: codex | claude | kimi | opencode | grok | qoder(cli/cn)。
    返回新 pane 信息；启动失败回滚本次 pane，返回结构化 error，不回退键盘模拟。
    """
    if not is_available():
        return {"available": False}
    try:
        require_herdr_capabilities()
        normalize_agent_kind(agent)
        if label is not None:
            validate_agent_name(label)
    except HerdrCapabilityError as exc:
        return {
            "available": True,
            "error_code": "herdr_upgrade_required",
            "error": str(exc),
        }
    except ValueError as exc:
        return {"available": True, "error_code": "invalid_agent_identity", "error": str(exc)}
    normalized_args = normalize_agent_args(args)
    agent_args = shlex.split(normalized_args) if normalized_args else []
    # Grok 自绘 TUI 默认暗色；Web 浅色时启动加 --light（运行中切主题走 /theme slash）
    if agent == "grok":
        for flag in grok_launch_theme_args(current_web_theme_mode()):
            if flag not in agent_args:
                agent_args = [flag, *agent_args]
    # 只复用当前 live snapshot 中 agent 与工作目录都匹配的 pane。仅按 agent
    # 复用会把新任务静默送进另一个项目；不在 snapshot 中的旧 pane 不视为存活。
    snap = _snapshot_session(session)
    try:
        target_dir = Path(workdir).expanduser().resolve()
    except OSError:
        target_dir = Path(workdir).expanduser().absolute()

    def matching_cwd(pane: dict[str, Any]) -> bool:
        cwd = pane.get("cwd")
        if not cwd:
            return False
        try:
            return Path(cwd).expanduser().resolve() == target_dir
        except OSError:
            return Path(cwd).expanduser().absolute() == target_dir

    panes = snap.get("panes", [])
    matching = [
        p for p in panes
        if p.get("agent") == agent and matching_cwd(p)
    ]
    if label:
        existing = next((p for p in matching if p.get("label") == label), None)
        if existing is None:
            collision = next(
                (
                    p for p in panes
                    if str(p.get("label") or "").casefold() == label.casefold()
                ),
                None,
            )
            if collision:
                return {
                    "available": True,
                    "error": f"实例名称已被 pane {collision.get('pane_id')} 使用: {label}",
                }
            # 兼容升级前没有实例标签的单 pane：首次使用新 UI 时认领并改名。
            # 仅自动生成的 `<agent>-1` 可以认领；用户显式填写其他名称时
            # 语义一定是新实例。多个无标签候选也无法安全区分。
            legacy = [p for p in matching if p.get("label") in (None, "", agent)]
            existing = (
                legacy[0]
                if label.casefold() == f"{agent}-1".casefold() and len(legacy) == 1
                else None
            )
    else:
        existing = matching[0] if matching else None
    if existing:
        _rename_agent_context(session, existing, agent, layout, label)
        result = {
            "available": True,
            "pane_id": existing["pane_id"],
            "agent": agent,
            "reused": True,
            "msg": f"{agent} pane 已存在({existing['pane_id']}),跳过",
        }
        existing_cwd = existing.get("cwd") or existing.get("foreground_cwd")
        if existing_cwd:
            result["cwd"] = existing_cwd
        if label:
            result["label"] = label
        # 复用路径与启动路径语义一致：若该 pane 由本路径启动过（有 launch 契约），
        # 暴露其权威 name/kind；legacy pane 无契约则不臆造。
        reused_desc = get_launch_descriptor(session, existing["pane_id"])
        if reused_desc:
            result["name"] = reused_desc["name"]
            result["kind"] = reused_desc["kind"]
        return result
    agent_bin = _find_agent_bin(agent)
    if not (Path(agent_bin).is_file() and os.access(agent_bin, os.X_OK)):
        return {"available": True, "error": f"{agent} 未安装或不在 PATH"}
    # 在任何布局 mutation 前，基于当前 live agents 解析 session 内唯一运行时名：
    # label 缺省时分配 agent-N，避免裸名与已有同 kind live agent 冲突，导致 pane 已
    # 创建却在 agent start 时因 live name 唯一约束失败回滚。
    try:
        runtime_name = resolve_unique_agent_name(agent, label, snap.get("agents", []))
    except ValueError as exc:
        return {"available": True, "error": str(exc)}
    canonical_kind = normalize_agent_kind(agent)
    before_ids = {
        str(p.get("pane_id")) for p in snap.get("panes", []) if p.get("pane_id")
    }
    # OpenCode/Bun 在窄 split 中可能直接 fatal signal 4；即使调用方仍传旧默认
    # right，也自动使用独立 tab。其他 agent 尊重显式布局。
    effective_layout = "tab" if agent == "opencode" else layout
    new_pid = None
    try:
        # 根据 layout 开新 pane:right/down 用 split,tab 用 tab create
        if effective_layout == "tab":
            # 多页:每个 agent 一个新 tab
            create_out = _run(
                ["--session", session, "tab", "create", "--cwd", workdir],
                timeout=5,
            )
        else:
            # 分屏:right(水平/左右)或 down(垂直/上下)
            direction = "right" if effective_layout in ("right", "horizontal") else "down"
            create_out = _run(
                ["--session", session, "pane", "split", "--current",
                 "--direction", direction, "--no-focus", "--cwd", workdir],
                timeout=5,
            )

        reported_pid = None
        for line in create_out.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                data = json.loads(line[5:].strip())
            except (ValueError, json.JSONDecodeError):
                continue
            result = data.get("result", {})
            reported_pid = (
                result.get("pane", {}).get("pane_id")
                or result.get("tab", {}).get("focused_pane_id")
            )
            break

        # 无论 Herdr 是否返回 id，都用前后 snapshot 验证它确实是本次新增 pane，
        # 并保留该 pane 的 tab/workspace id 供启动后改名复用（无需再取一次 snapshot）。
        created_pane: dict[str, Any] | None = None
        deadline = time.monotonic() + PANE_CREATE_TIMEOUT
        while time.monotonic() < deadline:
            after = _snapshot_session(session)
            after_panes = {
                str(p.get("pane_id")): p
                for p in after.get("panes", [])
                if isinstance(p, dict) and p.get("pane_id")
            }
            created_ids = set(after_panes) - before_ids
            if reported_pid and str(reported_pid) in created_ids:
                new_pid = str(reported_pid)
                created_pane = after_panes[new_pid]
                break
            if not reported_pid and len(created_ids) == 1:
                new_pid = created_ids.pop()
                created_pane = after_panes[new_pid]
                break
            if not reported_pid and len(created_ids) > 1:
                raise RuntimeError("创建 pane 时同时出现多个新 pane，无法安全识别")
            time.sleep(AGENT_POLL_INTERVAL)
        if not new_pid:
            raise RuntimeError("split/tab 后找不到本次创建的新 pane")
        start_timeout = _agent_start_timeout(agent)
        # 全部受支持 agent 统一用原生 agent start：Herdr 按 --kind 解析 canonical
        # 可执行文件、在 pane 的交互 shell 内启动，并在 --timeout 内等待 readiness。
        # Cockpit 不再按 agent 类型回退 pane run，也不自造 readiness 轮询；任何启动
        # 失败都保留原 pane/session 并返回结构化错误，不回退键盘模拟。
        start_argv = [
            "--session", session, "agent", "start", runtime_name,
            "--kind", canonical_kind,
            "--pane", new_pid,
            "--timeout", str(int(start_timeout * 1000)),
        ]
        if agent_args:
            start_argv += ["--", *agent_args]
        # 新建 pane 的交互 shell 就绪有延迟，立即 agent start 会报
        # agent_pane_busy(not an available shell)；短窗内重试等待就绪。
        shell_deadline = time.monotonic() + 10
        while True:
            try:
                _run(start_argv, timeout=int(start_timeout) + 5)
                break
            except RuntimeError as exc:
                if (
                    "agent_pane_busy" in str(exc)
                    and time.monotonic() < shell_deadline
                ):
                    time.sleep(0.5)
                    continue
                raise

        # 启动成功：把 workspace/tab/pane 改成可辨认名称(默认是序号,看板/TUI 里
        # 分不清)；失败不影响启动结果。created_pane 来自创建后的 snapshot，含
        # tab_id/workspace_id，无需再取一次 snapshot。展示名用唯一 runtime_name。
        _rename_agent_context(
            session, created_pane or {"pane_id": new_pid}, agent, effective_layout,
            runtime_name,
        )
        # 持久化权威 launch 契约 {name, kind, args}：Herdr 不保留原始 start argv，
        # 故由启动路径落盘，供 restart 按 session+pane/name 精确取回原参数重建，
        # 绝不从进程 argv/label/类型默认值猜测。落盘失败不杀已成功启动的 agent。
        descriptor_error: str | None = None
        try:
            save_launch_descriptor(
                session=session, pane_id=new_pid, name=runtime_name,
                kind=canonical_kind, args=agent_args, agent=agent, workdir=workdir,
            )
        except OSError as exc:
            descriptor_error = str(exc)
        result = {
            "available": True,
            "pane_id": new_pid,
            "agent": agent,
            "name": runtime_name,
            "kind": canonical_kind,
            "layout": effective_layout,
        }
        if label:
            result["label"] = label
        if descriptor_error:
            result["descriptor_error"] = descriptor_error
        return result
    except RuntimeError as e:
        rolled_back = False
        if new_pid:
            try:
                _run(["--session", session, "pane", "close", new_pid], timeout=5)
                rolled_back = True
            except RuntimeError:
                pass
        return {
            "available": True,
            "error": str(e),
            "pane_id": new_pid,
            "rolled_back": rolled_back,
        }


def close_pane(session: str, pane_id: str) -> dict[str, Any]:
    """关闭 pane(清理一键工作区遗留的空白 shell pane 用)。"""
    if not is_available():
        return {"available": False}
    try:
        _run(["--session", session, "pane", "close", pane_id], timeout=5)
    except RuntimeError as e:
        return {"available": True, "error": str(e)}
    # pane 已关闭：清理该 pane 的 launch descriptor。Herdr 可能复用 opaque pane ID，
    # 不清理会让后续复用该 ID 的新 agent 误读旧契约。
    cleanup = clear_launch_descriptor_by_pane(session, pane_id)
    result: dict[str, Any] = {"available": True, "closed": pane_id}
    if cleanup.get("error"):
        result["descriptor_cleanup_error"] = cleanup["error"]
    else:
        result["descriptors_cleared"] = cleanup.get("cleared", 0)
    return result


SPLIT_MODES = ("horizontal", "vertical", "grid4")
COMPOSE_ORIENTATIONS = ("horizontal", "vertical")
COMPOSE_MAX_PANES = 4


def _pane_ids_of(session: str) -> set[str]:
    snap = _snapshot_session(session)
    return {
        str(p.get("pane_id"))
        for p in snap.get("panes", [])
        if p.get("pane_id")
    }


def _require_unzoomed(snap: dict[str, Any], pane_ids: list[str]) -> None:
    """布局变更前拒绝 zoom tab，避免 Herdr 返回成功但 move 实际未执行。"""
    pane_tabs = {
        str(p.get("pane_id")): str(p.get("tab_id") or "")
        for p in snap.get("panes", [])
        if p.get("pane_id")
    }
    zoomed_tabs = {
        str(layout.get("tab_id") or "")
        for layout in snap.get("layouts", [])
        if layout.get("zoomed")
    }
    if any(pane_tabs.get(pid) in zoomed_tabs for pid in pane_ids):
        raise ValueError("pane 所在 tab 正在放大，请先退出单 pane 放大后重试")


def _move_pane(session: str, args: list[str]) -> None:
    """执行 pane move，并检查 Herdr 的 changed 字段而非只看退出码。"""
    out = _run(["--session", session, "pane", "move", *args], timeout=10)
    data = _parse_data_json(out)
    if not data:
        raise RuntimeError("pane move 输出解析失败")
    result = data.get("result", data)
    move = result.get("move_result", result)
    if not isinstance(move, dict):
        raise RuntimeError("pane move 输出缺少结果")
    if not move.get("changed"):
        reason = str(move.get("reason") or "未说明原因")
        if reason == "zoomed_tab":
            raise RuntimeError("pane 所在 tab 正在放大，请先退出单 pane 放大后重试")
        raise RuntimeError(f"pane move 未生效: {reason}")


def _split_pane_once(session: str, pane_id: str, direction: str) -> str:
    """对 pane 分屏一次(right/down),返回新建 pane id。"""
    before = _pane_ids_of(session)
    out = _run(
        ["--session", session, "pane", "split", pane_id,
         "--direction", direction, "--no-focus"],
        timeout=10,
    )
    data = _parse_data_json(out) or {}
    result = data.get("result") or {}
    reported = (
        (result.get("pane") or {}).get("pane_id")
        or (result.get("tab") or {}).get("focused_pane_id")
    )
    reported = str(reported) if reported else ""
    if reported and reported not in before:
        return reported
    deadline = time.monotonic() + PANE_CREATE_TIMEOUT
    while time.monotonic() < deadline:
        created = _pane_ids_of(session) - before
        if len(created) == 1:
            return created.pop()
        time.sleep(AGENT_POLL_INTERVAL)
    raise RuntimeError("split 后无法识别新 pane")


def split_pane_layout(session: str, pane_id: str, mode: str) -> list[str]:
    """把单个 pane 拆成布局。新槽位为空 shell,返回新建 pane id 列表。

    - horizontal:左右两栏;vertical:上下两栏;grid4:2×2 四宫格。
    """
    snap = _snapshot_session(session)
    existing = {
        str(p.get("pane_id")) for p in snap.get("panes", []) if p.get("pane_id")
    }
    if pane_id not in existing:
        raise ValueError(f"未找到 pane: {pane_id}")
    _require_unzoomed(snap, [pane_id])
    if mode == "horizontal":
        return [_split_pane_once(session, pane_id, "right")]
    if mode == "vertical":
        return [_split_pane_once(session, pane_id, "down")]
    if mode == "grid4":
        right = _split_pane_once(session, pane_id, "right")
        bottom_left = _split_pane_once(session, pane_id, "down")
        bottom_right = _split_pane_once(session, right, "down")
        return [right, bottom_left, bottom_right]
    raise ValueError(f"不支持的分屏模式: {mode}")


def detach_pane(session: str, pane_id: str) -> None:
    """把 pane 拆到独立 tab(herdr pane move --new-tab)。"""
    snap = _snapshot_session(session)
    pane = next(
        (p for p in snap.get("panes", []) if str(p.get("pane_id")) == pane_id),
        None,
    )
    if not pane:
        raise ValueError(f"未找到 pane: {pane_id}")
    same_tab = [
        p for p in snap.get("panes", [])
        if str(p.get("tab_id") or "") == str(pane.get("tab_id") or "")
    ]
    if len(same_tab) <= 1:
        raise ValueError("当前 pane 已经是独立 tab")
    _require_unzoomed(snap, [pane_id])
    _move_pane(session, [pane_id, "--new-tab"])


def untile_tab(session: str, tab_id: str) -> list[str]:
    """拆开 tab 内分屏:保留第一个 pane,其余逐个移到独立 tab。"""
    snap = _snapshot_session(session)
    panes = [
        str(p.get("pane_id"))
        for p in snap.get("panes", [])
        if p.get("pane_id") and str(p.get("tab_id") or "") == str(tab_id)
    ]
    if not panes:
        raise ValueError(f"未找到 tab: {tab_id}")
    _require_unzoomed(snap, panes)
    moved: list[str] = []
    for pid in panes[1:]:
        try:
            _move_pane(session, [pid, "--new-tab"])
        except RuntimeError as exc:
            raise RuntimeError(
                f"拆开整组时已移动 {len(moved)} 个 pane，后续操作失败: {exc}"
            ) from exc
        moved.append(pid)
    return moved


def compose_panes(session: str, pane_ids: list[str], orientation: str) -> str:
    """把 2-4 个 pane 组合为一个分屏,第一个为基准。返回基准 pane id。

    horizontal 将所有 pane 排成单行,vertical 将所有 pane 排成单列。
    """
    if orientation not in COMPOSE_ORIENTATIONS:
        raise ValueError(f"不支持的组合方向: {orientation}")
    if not 2 <= len(pane_ids) <= COMPOSE_MAX_PANES:
        raise ValueError(f"组合分屏仅支持 2-{COMPOSE_MAX_PANES} 个 pane")
    if len(set(pane_ids)) != len(pane_ids):
        raise ValueError("不能重复组合同一个 pane")
    snap = _snapshot_session(session)
    existing = {
        str(p.get("pane_id")) for p in snap.get("panes", []) if p.get("pane_id")
    }
    missing = [pid for pid in pane_ids if pid not in existing]
    if missing:
        raise ValueError("未找到 pane: " + ", ".join(missing))
    _require_unzoomed(snap, pane_ids)
    base = pane_ids[0]
    base_tab = next(
        (
            str(p.get("tab_id"))
            for p in snap.get("panes", [])
            if str(p.get("pane_id")) == base and p.get("tab_id")
        ),
        "",
    )
    if not base_tab:
        raise ValueError(f"无法确定基准 pane 所在 tab: {base}")
    direction = "right" if orientation == "horizontal" else "down"

    def _move(pid: str, target: str, direction: str, ratio: str) -> None:
        _move_pane(
            session,
            [pid, "--tab", base_tab, "--target-pane", target, "--split", direction,
             "--ratio", ratio],
        )

    moves = [
        (
            pane_ids[index],
            pane_ids[index - 1],
            direction,
            f"{1 / (len(pane_ids) - index + 1):.6f}".rstrip("0").rstrip("."),
        )
        for index in range(1, len(pane_ids))
    ]
    completed = 0
    try:
        for pid, target, move_direction, ratio in moves:
            _move(pid, target, move_direction, ratio)
            completed += 1
    except RuntimeError as exc:
        raise RuntimeError(
            f"组合分屏时已完成 {completed}/{len(moves)} 步，后续操作失败: {exc}"
        ) from exc
    return base


def _restart_error(code: str, error: str, pane_id: str) -> dict[str, Any]:
    return {
        "available": True, "error_code": code, "error": error,
        "pane_id": pane_id, "preserved": True,
    }


def _pane_at_available_shell(session: str, pane_id: str) -> bool:
    """以 process-info 证明 shell 自身在前台，且没有其他前台进程。"""
    out = _run(
        ["--session", session, "pane", "process-info", "--pane", pane_id],
        timeout=5,
    )
    data = _parse_data_json(out)
    if not data:
        raise RuntimeError("pane process-info 输出解析失败")
    result = data.get("result", data)
    if not isinstance(result, dict):
        raise RuntimeError("pane process-info 输出格式无效")
    info = result.get("process_info", result)
    if not isinstance(info, dict) or info.get("pane_id") != pane_id:
        raise RuntimeError("pane process-info 输出缺少目标 pane")
    shell_pid = info.get("shell_pid")
    foreground_pgid = info.get("foreground_process_group_id")
    processes = info.get("foreground_processes")
    if not isinstance(shell_pid, int) or isinstance(shell_pid, bool):
        return False
    if foreground_pgid != shell_pid or not isinstance(processes, list):
        return False
    return all(
        isinstance(process, dict) and process.get("pid") == shell_pid
        for process in processes
    )


def restart_pane(
    session: str, pane_id: str, agent: str | None = None,
    workdir: str | None = None, resume: bool = False,
) -> dict[str, Any]:
    """原位重启 managed agent，并恢复原唯一 name/kind/native args。"""
    if not is_available():
        return {"available": False}
    key = (session, pane_id)
    with _RESTART_GUARD:
        if key in _RESTARTING_PANES:
            return _restart_error(
                "restart_in_progress", f"pane {pane_id} 正在重启", pane_id,
            )
        _RESTARTING_PANES.add(key)
    try:
        try:
            require_herdr_capabilities()
        except HerdrCapabilityError as exc:
            return _restart_error("herdr_upgrade_required", str(exc), pane_id)

        snap = _snapshot_session(session)
        if snap.get("error"):
            return _restart_error(
                "restart_snapshot_failed", str(snap["error"]), pane_id,
            )
        pane = next(
            (item for item in snap.get("panes", []) if item.get("pane_id") == pane_id),
            None,
        )
        if pane is None:
            return _restart_error(
                "restart_pane_not_found", f"找不到 pane: {pane_id}", pane_id,
            )
        descriptor = get_launch_descriptor(session, pane_id)
        if descriptor is None:
            return _restart_error(
                "restart_identity_missing",
                f"pane {pane_id} 缺少 managed launch descriptor", pane_id,
            )
        name = descriptor.get("name")
        kind = descriptor.get("kind")
        launch_args = descriptor.get("args")
        product_agent = descriptor.get("agent") or pane.get("agent")
        if (
            not isinstance(name, str) or not name
            or not isinstance(kind, str) or not kind
            or not isinstance(launch_args, list)
            or any(not isinstance(value, str) for value in launch_args)
            or not isinstance(product_agent, str) or not product_agent
        ):
            return _restart_error(
                "restart_identity_invalid", "managed launch descriptor 无效", pane_id,
            )
        try:
            if normalize_agent_kind(product_agent) != kind:
                raise ValueError("kind 不匹配")
        except ValueError as exc:
            return _restart_error("restart_identity_invalid", str(exc), pane_id)
        live = [
            item for item in snap.get("agents", [])
            if isinstance(item, dict) and item.get("pane_id") == pane_id
        ]
        try:
            live_kind = (
                normalize_agent_kind(str(live[0].get("agent") or ""))
                if len(live) == 1 else None
            )
        except ValueError:
            live_kind = None
        if len(live) != 1 or live[0].get("name") != name or live_kind != kind:
            return _restart_error(
                "restart_identity_mismatch",
                f"pane {pane_id} 的 live identity 与 launch descriptor 不一致",
                pane_id,
            )
        if agent is not None:
            try:
                requested_kind = normalize_agent_kind(agent)
            except ValueError as exc:
                return _restart_error("restart_identity_invalid", str(exc), pane_id)
            if requested_kind != kind:
                return _restart_error(
                    "restart_identity_mismatch",
                    "请求 agent 与 managed kind 不一致", pane_id,
                )
        if resume and kind != "codex":
            return _restart_error(
                "restart_resume_unsupported", "resume 仅支持 Codex", pane_id,
            )

        try:
            _run(
                ["--session", session, "agent", "send-keys", name, "esc"],
                timeout=3,
            )
            _run(
                ["--session", session, "agent", "send-keys", name, "ctrl+c"],
                timeout=3,
            )
        except RuntimeError as exc:
            return _restart_error("restart_exit_failed", str(exc), pane_id)

        deadline = time.monotonic() + RESTART_SHELL_TIMEOUT_S
        second_interrupt_at = time.monotonic() + RESTART_SECOND_INTERRUPT_S
        second_interrupt_sent = False
        shell_ready = False
        while time.monotonic() < deadline:
            current = _snapshot_session(session)
            if current.get("error"):
                return _restart_error(
                    "restart_shell_probe_failed", str(current["error"]), pane_id,
                )
            current_pane = next(
                (
                    item for item in current.get("panes", [])
                    if item.get("pane_id") == pane_id
                ),
                None,
            )
            if current_pane is None:
                return _restart_error(
                    "restart_pane_lost", f"重启期间 pane {pane_id} 消失", pane_id,
                )
            old_live = any(
                isinstance(item, dict) and item.get("name") == name
                for item in current.get("agents", [])
            )
            if not old_live and not current_pane.get("agent"):
                try:
                    shell_ready = _pane_at_available_shell(session, pane_id)
                except RuntimeError as exc:
                    return _restart_error(
                        "restart_shell_probe_failed", str(exc), pane_id,
                    )
                if shell_ready:
                    break
            elif (
                not second_interrupt_sent
                and time.monotonic() >= second_interrupt_at
            ):
                try:
                    _run(
                        ["--session", session, "agent", "send-keys", name, "ctrl+c"],
                        timeout=3,
                    )
                except RuntimeError:
                    # 可选第二次中断与 agent 正常退出存在竞态；下一轮快照与
                    # process-info 才是是否回到 shell 的权威判定。
                    pass
                second_interrupt_sent = True
            time.sleep(AGENT_POLL_INTERVAL)
        if not shell_ready:
            return _restart_error(
                "restart_shell_not_ready",
                f"pane {pane_id} 未在 {RESTART_SHELL_TIMEOUT_S:g}s 内回到可用 shell",
                pane_id,
            )

        native_args = list(launch_args)
        if resume:
            native_args += ["resume", "--last"]
        start_timeout = _agent_start_timeout(product_agent)
        start_argv = [
            "--session", session, "agent", "start", name,
            "--kind", kind, "--pane", pane_id,
            "--timeout", str(int(start_timeout * 1000)),
        ]
        if native_args:
            start_argv += ["--", *native_args]
        start_deadline = time.monotonic() + RESTART_SHELL_TIMEOUT_S
        while True:
            try:
                _run(start_argv, timeout=int(start_timeout) + 5)
                break
            except RuntimeError as exc:
                if (
                    "agent_pane_busy" in str(exc)
                    and time.monotonic() < start_deadline
                ):
                    time.sleep(AGENT_POLL_INTERVAL)
                    continue
                code = (
                    "restart_shell_not_ready"
                    if "agent_pane_busy" in str(exc)
                    else "restart_start_failed"
                )
                return _restart_error(code, str(exc), pane_id)
        return {
            "available": True, "restarted": True, "preserved": True,
            "pane_id": pane_id, "agent": product_agent, "name": name,
            "kind": kind, "args": native_args, "resume": resume,
        }
    finally:
        with _RESTART_GUARD:
            _RESTARTING_PANES.discard(key)


def stop_session(session: str) -> dict[str, Any]:
    """停止一个 herdr session。"""
    if not is_available():
        return {"available": False}
    try:
        _run(["session", "stop", session], timeout=10)
        return {"available": True, "stopped": session}
    except RuntimeError as e:
        return {"available": True, "error": str(e)}


def delete_session(session: str) -> dict[str, Any]:
    """删除一个已停止的 session。"""
    if not is_available():
        return {"available": False}
    try:
        _run(["session", "delete", session], timeout=10)
    except RuntimeError as e:
        return {"available": True, "error": str(e)}
    # session 已删除：清理该 session 的 launch descriptor，避免同名 session 重建后
    # 把上一代 args 误当当前权威契约（workspace/pane/name ID 会被 Herdr 重新分配）。
    # 清理失败不复活 session，但必须结构化暴露，不静默宣告 descriptor 已安全。
    cleanup = clear_launch_descriptors(session)
    result: dict[str, Any] = {"available": True, "deleted": session}
    if cleanup.get("error"):
        result["descriptor_cleanup_error"] = cleanup["error"]
    else:
        result["descriptors_cleared"] = cleanup.get("cleared", 0)
    return result
