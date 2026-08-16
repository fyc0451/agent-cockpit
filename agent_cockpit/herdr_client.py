"""herdr_client.py — herdr CLI subprocess 封装(多 session 聚合)。

herdr 以多个 session 运行,每个 session 有独立 socket。本模块遍历所有 session,
聚合 pane 状态,这是"每个 agent 都可视化"的数据源。

关键修正(对比旧版):不再只查 default socket,而是 herdr session list 枚举所有 session,
逐个 --session <name> 取 snapshot 聚合。
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor, wait
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, NamedTuple

from . import next_profile

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
SESSION_BOOTSTRAP_TIMEOUT_S = 10.0
SESSION_BOOTSTRAP_POLL_S = 0.1
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
    "zcode": "opencode",
    "grok": "grok",
    "qoder": "qodercli",
    "qodercli": "qodercli",
    "qodercn": "qodercli",
    "qoderclicn": "qodercli",
}
_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_AGENT_INSTANCE_ID_RE = re.compile(r"^i-[a-z2-7]{26}$")
MAX_DISPLAY_NAME_LENGTH = 64
_ATTACH_ONLY_PRODUCTS = frozenset({"zcode"})
_RESTART_GUARD = threading.Lock()
_RESTARTING_PANES: set[tuple[str, str]] = set()
_SESSION_BOOTSTRAP_LOCK = threading.RLock()
_SESSION_BOOTSTRAP_PROCESSES: dict[str, subprocess.Popen[bytes]] = {}
_WORKSPACE_BOOTSTRAP_LOCK = threading.RLock()
_WORKSPACE_BOOTSTRAP_LABEL = "Cockpit Next"


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


def validate_display_name(name: str) -> str:
    """校验用户可见名称；它不参与任何运行时或邮箱身份选择。"""
    if not isinstance(name, str):
        raise ValueError("显示名称不能为空")
    value = name.strip()
    if not value:
        raise ValueError("显示名称不能为空")
    if len(value) > MAX_DISPLAY_NAME_LENGTH:
        raise ValueError(f"显示名称最长 {MAX_DISPLAY_NAME_LENGTH} 个字符")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("显示名称不能包含控制字符")
    return value


def validate_agent_instance_id(instance_id: str) -> str:
    """验证 Cockpit 生成的 128-bit opaque 实例 ID。"""
    if not isinstance(instance_id, str) or not _AGENT_INSTANCE_ID_RE.fullmatch(instance_id):
        raise ValueError("agent instance id 格式无效")
    return instance_id


def new_agent_instance_id() -> str:
    """生成兼容 Herdr live agent name 语法的不可复用实例 ID。"""
    encoded = base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")
    return f"i-{encoded.lower()}"


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
    if name in _ATTACH_ONLY_PRODUCTS:
        return ""
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


def _herdr_sessions_root() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    if not isinstance(configured, str) or not configured:
        raise ValueError("XDG_CONFIG_HOME required for scoped Herdr session")
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be absolute")
    return root.resolve(strict=False) / "herdr" / "sessions"


def _scoped_session_rows(
    sessions: list[dict[str, Any]], scoped: str,
) -> list[dict[str, Any]]:
    try:
        session_root = _herdr_sessions_root() / scoped
        return [
            row for row in sessions
            if row["name"] == scoped
            and Path(row["directory"]).resolve(strict=False) == session_root
            and Path(row["socket"]).resolve(strict=False)
            == session_root / "herdr.sock"
        ]
    except (OSError, ValueError):
        _LIST_SESSIONS_FAILED.value = True
        return []


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
        if next_profile.enabled():
            return {
                "ok": False, "reloaded": [],
                "errors": ["next_session_not_running"],
            }
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
    if "--session" in args:
        index = args.index("--session")
        if index + 1 >= len(args):
            raise RuntimeError("herdr --session 缺少值")
        try:
            next_profile.require_session(args[index + 1])
        except next_profile.NextProfileError as exc:
            raise RuntimeError(str(exc)) from exc
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
        sessions = [
            {
                "name": str(row.get("name", "")),
                "status": "running" if row.get("running") else "stopped",
                "directory": str(row.get("session_dir", "")),
                "socket": str(row.get("socket_path", "")),
            }
            for row in rows
            if isinstance(row, dict) and row.get("name")
        ]
        scoped = next_profile.session()
        if scoped is None:
            return sessions
        return _scoped_session_rows(sessions, scoped)
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
    scoped = next_profile.session()
    if scoped is None:
        return sessions
    return _scoped_session_rows(sessions, scoped)


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
        "tabs": [
            {
                "tab_id": item.get("tab_id"),
                "workspace_id": item.get("workspace_id"),
                "label": item.get("label"),
                "pane_count": item.get("pane_count"),
            }
            for item in snap.get("tabs", [])
            if isinstance(item, dict)
        ] if isinstance(snap.get("tabs", []), list) else [],
        "workspaces": [
            {
                "workspace_id": item.get("workspace_id"),
                "active_tab_id": item.get("active_tab_id"),
                "focused": item.get("focused", False),
                "pane_count": item.get("pane_count"),
                "tab_count": item.get("tab_count"),
            }
            if isinstance(item, dict) else None
            for item in snap.get("workspaces", [])
        ] if isinstance(snap.get("workspaces", []), list) else None,
        "focused_pane_id": snap.get("focused_pane_id"),
        "focused_workspace_id": snap.get("focused_workspace_id"),
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
            # Grok 等自绘 TUI：/theme grokday 必须当斜杠命令键入，不能 agent prompt。
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

    Grok 4.5 会拒绝 dark 别名；canonical 主题 key 在当前版本稳定可用。
    """
    if mode not in ("light", "dark"):
        raise ValueError("mode 必须是 light 或 dark")
    return "/theme grokday" if mode == "light" else "/theme groknight"


# Web light/dark → OpenCode 内置主题名（用户指定；勿用 Mode 2031 当唯一手段）
OPENCODE_THEME_BY_WEB = {
    "light": "palenight",
    "dark": "aura",
}


def agent_theme_slash(agent: str, mode: str) -> str | None:
    """各 agent 可直接一次发完的主题 slash；需多步 UI 的返回 None 由专用路径处理。

    - grok: `/theme grokday|groknight`
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


def _opencode_visible(prefix: list[str], pane_id: str) -> str:
    return _run(
        prefix + [
            "read", pane_id, "--source", "visible",
            "--lines", "60", "--format", "text",
        ],
        timeout=5,
    )


def _wait_opencode_visible(
    prefix: list[str], pane_id: str, predicate: Callable[[str], bool],
    error: str, timeout: float = 5.0,
) -> str:
    deadline = time.monotonic() + timeout
    while True:
        screen = _opencode_visible(prefix, pane_id)
        if predicate(screen):
            return screen
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(error)
        time.sleep(min(0.1, remaining))


class _OpenCodePopupRegion(NamedTuple):
    title: str
    header_line: int
    header_column: int
    query: str
    rows: tuple[str, ...]


_OPENCODE_POPUP_WIDTH = 54
_OPENCODE_POPUP_TITLE_OFFSET = 2


def _opencode_popup_regions(
    screen: str, title: str,
) -> tuple[_OpenCodePopupRegion, ...]:
    lines = screen.splitlines()
    header_pattern = re.compile(rf"{re.escape(title)}\s{{2,}}esc")
    header_crop_pattern = re.compile(
        rf"{re.escape(title)}\s{{2,}}esc\s*$",
    )
    regions: list[_OpenCodePopupRegion] = []
    for index, line in enumerate(lines):
        for match in header_pattern.finditer(line):
            header_column = match.start()
            crop_right = header_column + (
                _OPENCODE_POPUP_WIDTH - _OPENCODE_POPUP_TITLE_OFFSET
            )
            if not header_crop_pattern.fullmatch(line[header_column:crop_right]):
                continue
            # Real OpenCode popups start with: header, spacer, Search/filter,
            # spacer. Both sides of the fixed-width crop may retain background.
            def content_at(row: str) -> str:
                return row[header_column:crop_right]

            layout = [content_at(row) for row in lines[index + 1:index + 4]]
            if (
                len(layout) != 3
                or layout[0].strip()
                or not layout[1].strip()
                or layout[2].strip()
            ):
                continue
            query = layout[1]
            if query != query.lstrip():
                continue

            rows = [query.strip()]
            blank_run = 0
            for row in lines[index + 4:index + 25]:
                content = content_at(row)
                if header_crop_pattern.fullmatch(content):
                    break
                if not content.strip():
                    blank_run += 1
                    if blank_run > 2:
                        break
                    continue
                if content != content.lstrip():
                    break
                blank_run = 0
                label = content.strip()
                rows.append(label)
            regions.append(
                _OpenCodePopupRegion(
                    title, index, header_column, query.strip(), tuple(rows),
                ),
            )
    return tuple(regions)


def _opencode_popup_region_at(
    screen: str, expected: _OpenCodePopupRegion,
) -> _OpenCodePopupRegion | None:
    for region in _opencode_popup_regions(screen, expected.title):
        if (
            region.header_line == expected.header_line
            and region.header_column == expected.header_column
        ):
            return region
    return None


def _opencode_popup_header_at(
    screen: str, expected: _OpenCodePopupRegion,
) -> bool:
    lines = screen.splitlines()
    if expected.header_line >= len(lines):
        return False
    crop_right = expected.header_column + (
        _OPENCODE_POPUP_WIDTH - _OPENCODE_POPUP_TITLE_OFFSET
    )
    pattern = re.compile(rf"{re.escape(expected.title)}\s{{2,}}esc\s*$")
    return pattern.fullmatch(
        lines[expected.header_line][expected.header_column:crop_right],
    ) is not None


def _opencode_popup_label(row: str) -> str:
    return re.sub(r"^●\s+", "", row.strip())


def _opencode_popup_has_label(region: _OpenCodePopupRegion, label: str) -> bool:
    return any(_opencode_popup_label(row) == label for row in region.rows)


def _opencode_popup_has_first_body_label(
    region: _OpenCodePopupRegion, query: str, label: str,
) -> bool:
    return (
        region.query == query
        and len(region.rows) > 1
        and _opencode_popup_label(region.rows[1]) == label
    )


def _opencode_popup_opening_signature(
    region: _OpenCodePopupRegion,
) -> tuple[int, int, str, tuple[str, ...]]:
    # Rows after the first body item may be background below the borderless popup.
    return (
        region.header_line, region.header_column,
        region.query, region.rows[1:2],
    )


def _opencode_new_popup_region(
    screen: str,
    title: str,
    before: tuple[_OpenCodePopupRegion, ...],
) -> _OpenCodePopupRegion | None:
    old_signatures = {
        _opencode_popup_opening_signature(region) for region in before
    }
    return next(
        (region for region in _opencode_popup_regions(screen, title)
         if _opencode_popup_opening_signature(region) not in old_signatures),
        None,
    )


def apply_opencode_theme_to_pane(
    session: str, pane_id: str, theme_name: str,
) -> dict[str, Any]:
    """通过 OpenCode 主题弹层切换主题，不触碰或提交 composer 草稿。"""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", theme_name or ""):
        return {"error": "非法主题名"}

    prefix = ["--session", session, "pane"]
    dialog_may_be_open = False
    try:
        before = _opencode_visible(prefix, pane_id)
        before_regions = _opencode_popup_regions(before, "Themes")
        # Ctrl+X,T 直接打开独立主题弹层，OpenCode 会保留已有 composer 草稿。
        _run(prefix + ["send-keys", pane_id, "ctrl+x", "t"], timeout=5)
        dialog_may_be_open = True
        opened = _wait_opencode_visible(
            prefix, pane_id,
            lambda screen: _opencode_new_popup_region(
                screen, "Themes", before_regions,
            ) is not None,
            "OpenCode 主题弹层未打开",
        )
        popup = _opencode_new_popup_region(
            opened, "Themes", before_regions,
        )
        if popup is None:
            raise RuntimeError("OpenCode 主题弹层未打开")
        _run(prefix + ["send-keys", pane_id, "ctrl+u"], timeout=5)
        _run(prefix + ["send-text", pane_id, theme_name], timeout=5)
        _wait_opencode_visible(
            prefix, pane_id,
            lambda screen: (
                (region := _opencode_popup_region_at(screen, popup)) is not None
                and _opencode_popup_has_first_body_label(
                    region, theme_name, theme_name,
                )
            ),
            f"OpenCode 主题候选未出现: {theme_name}",
        )
        _run(prefix + ["send-keys", pane_id, "Enter"], timeout=5)
        _wait_opencode_visible(
            prefix, pane_id,
            lambda screen: not _opencode_popup_header_at(screen, popup),
            "OpenCode 主题弹层确认后未关闭", timeout=1.0,
        )
        dialog_may_be_open = False
    except Exception as exc:
        if dialog_may_be_open:
            try:
                _run(prefix + ["send-keys", pane_id, "esc"], timeout=5)
            except Exception:
                pass
        return {"error": str(exc)}
    return {
        "available": True, "sent": f"theme-dialog → {theme_name}",
        "mode": "opencode-theme-pick",
    }


def apply_opencode_mode_to_pane(
    session: str, pane_id: str, mode: str,
) -> dict[str, Any]:
    """通过 OpenCode 命令弹层把主题模式显式设为 light 或 dark。"""
    if mode not in ("light", "dark"):
        return {"error": "mode 必须是 light 或 dark"}

    prefix = ["--session", session, "pane"]
    dialog_may_be_open = False
    try:
        before = _opencode_visible(prefix, pane_id)
        before_regions = _opencode_popup_regions(before, "Commands")
        _run(prefix + ["send-keys", pane_id, "ctrl+p"], timeout=5)
        dialog_may_be_open = True
        opened = _wait_opencode_visible(
            prefix, pane_id,
            lambda screen: _opencode_new_popup_region(
                screen, "Commands", before_regions,
            ) is not None,
            "OpenCode 命令弹层未打开",
        )
        popup = _opencode_new_popup_region(
            opened, "Commands", before_regions,
        )
        if popup is None:
            raise RuntimeError("OpenCode 命令弹层未打开")
        _run(prefix + ["send-keys", pane_id, "ctrl+u"], timeout=5)
        _run(prefix + ["send-text", pane_id, "Switch to"], timeout=5)

        def _target_mode(screen: str) -> str | None:
            region = _opencode_popup_region_at(screen, popup)
            if region is None or region.query != "Switch to":
                return None
            for row in region.rows:
                match = re.fullmatch(r"Switch to (light|dark) mode", row)
                if match is not None:
                    return match.group(1)
            return None

        output = _wait_opencode_visible(
            prefix, pane_id, lambda screen: _target_mode(screen) is not None,
            "无法识别 OpenCode 当前主题模式",
        )
        target_mode = _target_mode(output)
        if target_mode is None:
            raise RuntimeError("无法识别 OpenCode 当前主题模式")
        changed = target_mode == mode
        _run(
            prefix + ["send-keys", pane_id, "Enter" if changed else "esc"],
            timeout=5,
        )
        _wait_opencode_visible(
            prefix, pane_id,
            lambda screen: not _opencode_popup_header_at(screen, popup),
            "OpenCode 命令弹层未关闭", timeout=1.0,
        )
        dialog_may_be_open = False
    except Exception as exc:
        if dialog_may_be_open:
            try:
                _run(prefix + ["send-keys", pane_id, "esc"], timeout=5)
            except Exception:
                pass
        return {"error": str(exc)}
    return {
        "available": True, "sent": f"theme-mode → {mode}",
        "mode": "opencode-theme-mode", "changed": changed,
    }


def apply_agent_web_themes(mode: str) -> dict[str, Any]:
    """把 Web 明暗推到 live agent pane（按 agent 原生主题手段）。

    - grok: `/theme grokday|groknight`（Grok 4.5 的真实主题 key）
    - opencode: 主题弹层选主题名（亮 palenight / 暗 aura），再通过命令弹层显式
      设置 light/dark mode；主题名与明暗模式是两个独立状态。
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
                continue
            mode_result = apply_opencode_mode_to_pane(session, pane_id, mode)
            if mode_result.get("error"):
                errors.append(f"opencode:{session}/{pane_id}: {mode_result['error']}")
                continue
            applied.append({
                "session": session, "pane_id": pane_id,
                "agent": "opencode",
                "command": f"theme-dialog→{opencode_theme}; mode→{mode}",
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
    instance_id: str | None = None, display_name: str | None = None,
    project_id: str | None = None, workspace_id: str | None = None,
) -> dict[str, Any]:
    """原生启动成功后持久化权威 launch 契约；返回写入的规范化记录。

    name 是 resolve_unique_agent_name 给出的 session 内唯一运行时名；kind 是
    canonical Herdr kind；args 是传给 `--` 的原生 argv 列表（保留空格/分号等原样）。
    """
    if instance_id is None and (project_id is not None or workspace_id is not None):
        raise ValueError("workspace authority 仅适用于 managed descriptor")
    record: dict[str, Any] = {
        "session": str(session),
        "name": str(name),
        "kind": str(kind),
        "args": [str(a) for a in args] if isinstance(args, (list, tuple)) else [],
        "agent": agent,
        "pane_id": str(pane_id),
        "workdir": workdir,
    }
    key = f"{session}|{name}"
    if instance_id is not None:
        opaque_id = validate_agent_instance_id(instance_id)
        if name != opaque_id:
            raise ValueError("managed runtime name 必须等于 agent instance id")
        if (project_id is None) != (workspace_id is None):
            raise ValueError("project/workspace authority 必须成对提供")
        if project_id is not None and (
            not re.fullmatch(r"prj_[0-9a-f]{32}", project_id)
            or not re.fullmatch(r"ws_[0-9a-f]{32}", workspace_id or "")
        ):
            raise ValueError("project/workspace authority 格式无效")
        record.update({
            "instance_id": opaque_id,
            "display_name": validate_display_name(display_name or agent or name),
            "state": "active",
        })
        if project_id is not None:
            record.update({
                "project_id": project_id,
                "workspace_id": workspace_id,
            })
        key = f"instance|{opaque_id}"
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
        if instance_id is not None:
            data["schema"] = 2
        data["descriptors"][key] = record
        _save_launch_descriptors(data)
    return dict(record)


def _launch_descriptor_is_active(record: dict[str, Any]) -> bool:
    return record.get("state", "active") == "active"


def _workspace_launch_label(instance_id: str) -> str:
    return "cockpit-launch-" + validate_agent_instance_id(instance_id)


_WORKSPACE_CODEX_READONLY_PUBLIC_ARGS = ["--sandbox", "read-only"]


def _workspace_codex_trust_args(canonical_path: str) -> list[str]:
    """Build one invocation-only Codex project trust override."""
    if (
        not isinstance(canonical_path, str)
        or not canonical_path
        or not Path(canonical_path).is_absolute()
        or "\x00" in canonical_path
        or any(0xD800 <= ord(char) <= 0xDFFF for char in canonical_path)
    ):
        raise ValueError("workspace canonical path invalid")
    try:
        quoted_path = json.dumps(canonical_path, ensure_ascii=False)
        override = (
            f"projects={{{quoted_path}={{trust_level=\"trusted\"}}}}"
        )
        override.encode("utf-8", errors="strict")
        decoded = tomllib.loads(override)
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("workspace trust override invalid") from exc
    if decoded != {
        "projects": {canonical_path: {"trust_level": "trusted"}},
    }:
        raise ValueError("workspace trust override invalid")
    return ["-c", override]


def _workspace_descriptor_internal_args(record: dict[str, Any]) -> list[str]:
    has_authority = "project_id" in record or "workspace_id" in record
    if not has_authority:
        return []
    instance_id = record.get("instance_id")
    workdir = record.get("workdir")
    kind = record.get("kind")
    if (
        not isinstance(instance_id, str)
        or _AGENT_INSTANCE_ID_RE.fullmatch(instance_id) is None
        or record.get("name") != instance_id
        or not isinstance(record.get("project_id"), str)
        or re.fullmatch(r"prj_[0-9a-f]{32}", record["project_id"]) is None
        or not isinstance(record.get("workspace_id"), str)
        or re.fullmatch(r"ws_[0-9a-f]{32}", record["workspace_id"]) is None
        or not isinstance(workdir, str)
        or not Path(workdir).is_absolute()
        or record.get("args") not in ([], _WORKSPACE_CODEX_READONLY_PUBLIC_ARGS)
        or (
            record.get("args") == _WORKSPACE_CODEX_READONLY_PUBLIC_ARGS
            and kind != "codex"
        )
        or record.get("agent") != kind
        or record.get("state") != "active"
    ):
        raise ValueError("workspace launch descriptor invalid")
    return _workspace_codex_trust_args(workdir) if kind == "codex" else []


class _WorkspaceBootstrapResult(NamedTuple):
    workspace_id: str
    root_pane_id: str | None


def _valid_workspace_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value.isascii()
        and not value.startswith("-")
        and not any(
            char.isspace() or ord(char) < 32 or ord(char) == 127
            for char in value
        )
    )


def _workspace_from_snapshot(snapshot_value: dict[str, Any]) -> str | None:
    if snapshot_value.get("error") is not None:
        raise ValueError("workspace snapshot unavailable")
    workspaces = snapshot_value.get("workspaces")
    if not isinstance(workspaces, list):
        raise ValueError("workspace snapshot invalid")
    parsed: list[tuple[str, bool]] = []
    for item in workspaces:
        if not isinstance(item, dict):
            raise ValueError("workspace snapshot invalid")
        workspace_id = item.get("workspace_id")
        focused = item.get("focused", False)
        if not _valid_workspace_id(workspace_id) or type(focused) is not bool:
            raise ValueError("workspace snapshot invalid")
        parsed.append((workspace_id, focused))
    if len({workspace_id for workspace_id, _focused in parsed}) != len(parsed):
        raise ValueError("workspace snapshot invalid")
    focused_workspace_id = snapshot_value.get("focused_workspace_id")
    if focused_workspace_id is not None:
        if (
            not isinstance(focused_workspace_id, str)
            or focused_workspace_id not in {item[0] for item in parsed}
        ):
            raise ValueError("workspace snapshot invalid")
        marked = [workspace_id for workspace_id, focused in parsed if focused]
        if marked and marked != [focused_workspace_id]:
            raise ValueError("workspace snapshot invalid")
        return focused_workspace_id
    marked = [workspace_id for workspace_id, focused in parsed if focused]
    if len(marked) == 1:
        return marked[0]
    if len(marked) > 1:
        raise ValueError("workspace snapshot invalid")
    if len(parsed) == 1:
        return parsed[0][0]
    if parsed:
        raise ValueError("workspace snapshot unavailable")
    return None


def _workspace_managed_bootstrap(
    session: str, workdir: str, snapshot_value: dict[str, Any],
) -> _WorkspaceBootstrapResult:
    """Ensure the headless session has a workspace before reserving authority."""
    with _WORKSPACE_BOOTSTRAP_LOCK:
        workspace_id = _workspace_from_snapshot(snapshot_value)
        if workspace_id is not None:
            return _WorkspaceBootstrapResult(workspace_id, None)

        # A second snapshot under the process-wide lock closes the concurrent
        # first-launch race without changing the existing-workspace fast path.
        current = _snapshot_session(session)
        workspace_id = _workspace_from_snapshot(current)
        if workspace_id is not None:
            return _WorkspaceBootstrapResult(workspace_id, None)

        out = _run(
            [
                "--session", session, "workspace", "create", "--cwd", workdir,
                "--label", _WORKSPACE_BOOTSTRAP_LABEL, "--no-focus",
            ],
            timeout=5,
        )
        data = _parse_data_json(out)
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict) or result.get("type") != "workspace_created":
            raise ValueError("workspace create result invalid")
        workspace = result.get("workspace")
        tab = result.get("tab")
        root_pane = result.get("root_pane")
        if not all(isinstance(item, dict) for item in (workspace, tab, root_pane)):
            raise ValueError("workspace create result invalid")
        assert isinstance(workspace, dict)
        assert isinstance(tab, dict)
        assert isinstance(root_pane, dict)
        workspace_id = workspace.get("workspace_id")
        active_tab_id = workspace.get("active_tab_id")
        tab_id = tab.get("tab_id")
        pane_id = root_pane.get("pane_id")
        root_cwd = root_pane.get("cwd")
        if not all(
            isinstance(value, str) and value
            for value in (workspace_id, active_tab_id, tab_id, pane_id, root_cwd)
        ):
            raise ValueError("workspace create result invalid")
        if not _valid_workspace_id(workspace_id):
            raise ValueError("workspace create result invalid")
        if (
            active_tab_id != tab_id
            or tab.get("workspace_id") != workspace_id
            or root_pane.get("tab_id") != tab_id
            or (
                root_pane.get("workspace_id") is not None
                and root_pane.get("workspace_id") != workspace_id
            )
        ):
            raise ValueError("workspace create result invalid")
        try:
            actual_cwd = Path(root_cwd).expanduser().resolve()
            expected_cwd = Path(workdir).expanduser().resolve()
        except OSError as exc:
            raise ValueError("workspace create result invalid") from exc
        if actual_cwd != expected_cwd:
            raise ValueError("workspace create result invalid")
        return _WorkspaceBootstrapResult(workspace_id, pane_id)


def _load_launch_descriptors_strict() -> dict[str, Any]:
    path = launch_descriptors_path()
    if not path.is_file():
        return {"schema": 2, "descriptors": {}}
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_launch_object,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("launch descriptor store 损坏") from exc
    if (
        not isinstance(data, dict)
        or data.get("schema") not in {1, 2}
        or not isinstance(data.get("descriptors"), dict)
    ):
        raise ValueError("launch descriptor store 损坏")
    return data


def reserve_workspace_launch_descriptor(
    *, session: str, name: str, kind: str, agent: str, workdir: str,
    instance_id: str, display_name: str, project_id: str, workspace_id: str,
    args: list[str] | None = None,
) -> dict[str, Any]:
    """Persist exact Workspace authority before any managed launch mutation."""
    opaque_id = validate_agent_instance_id(instance_id)
    if name != opaque_id:
        raise ValueError("managed runtime name 必须等于 agent instance id")
    public_args = list(args or [])
    if (
        not re.fullmatch(r"prj_[0-9a-f]{32}", project_id)
        or not re.fullmatch(r"ws_[0-9a-f]{32}", workspace_id)
        or not isinstance(workdir, str)
        or not workdir
        or public_args not in ([], _WORKSPACE_CODEX_READONLY_PUBLIC_ARGS)
        or (public_args and kind != "codex")
    ):
        raise ValueError("project/workspace authority 格式无效")
    record = {
        "session": session,
        "name": opaque_id,
        "kind": kind,
        "args": public_args,
        "agent": agent,
        "pane_id": "",
        "workdir": workdir,
        "instance_id": opaque_id,
        "display_name": validate_display_name(display_name),
        "launch_label": _workspace_launch_label(opaque_id),
        "state": "pending",
        "project_id": project_id,
        "workspace_id": workspace_id,
    }
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors_strict()
        key = f"instance|{opaque_id}"
        if key in data["descriptors"]:
            raise ValueError("agent instance id 已存在，不能复用")
        data["schema"] = 2
        data["descriptors"][key] = record
        _save_launch_descriptors(data)
    return dict(record)


def bind_pending_workspace_launch_descriptor(
    instance_id: str, pane_id: str,
) -> dict[str, Any]:
    opaque_id = validate_agent_instance_id(instance_id)
    if not isinstance(pane_id, str) or not pane_id:
        raise ValueError("pane id 格式无效")
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors_strict()
        record = data["descriptors"].get(f"instance|{opaque_id}")
        if not isinstance(record, dict) or record.get("state") != "pending":
            raise ValueError("pending launch descriptor 不存在")
        if record.get("pane_id") not in {"", pane_id}:
            raise ValueError("pending launch descriptor pane 冲突")
        record["pane_id"] = pane_id
        _save_launch_descriptors(data)
        return dict(record)


def activate_pending_workspace_launch_descriptor(
    instance_id: str, pane_id: str,
) -> dict[str, Any]:
    opaque_id = validate_agent_instance_id(instance_id)
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors_strict()
        record = data["descriptors"].get(f"instance|{opaque_id}")
        if (
            not isinstance(record, dict)
            or record.get("state") != "pending"
            or record.get("pane_id") != pane_id
        ):
            raise ValueError("pending launch descriptor authority 不匹配")
        record["state"] = "active"
        _save_launch_descriptors(data)
        return dict(record)


def discard_pending_workspace_launch_descriptor(instance_id: str) -> bool:
    opaque_id = validate_agent_instance_id(instance_id)
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors_strict()
        key = f"instance|{opaque_id}"
        record = data["descriptors"].get(key)
        if not isinstance(record, dict) or record.get("state") != "pending":
            return False
        del data["descriptors"][key]
        _save_launch_descriptors(data)
        return True


def get_launch_descriptor(session: str, pane_id: str) -> dict[str, Any] | None:
    """按 session+pane 精确取回 launch 契约；不存在返回 None（restart 不得猜测）。"""
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
    for record in data["descriptors"].values():
        if (
            isinstance(record, dict)
            and _launch_descriptor_is_active(record)
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
    if isinstance(record, dict) and _launch_descriptor_is_active(record):
        return dict(record)
    for candidate in data["descriptors"].values():
        if (
            isinstance(candidate, dict)
            and _launch_descriptor_is_active(candidate)
            and candidate.get("session") == session
            and candidate.get("name") == name
        ):
            return dict(candidate)
    return None


def get_launch_descriptor_by_instance(
    instance_id: str, *, include_retired: bool = False,
) -> dict[str, Any] | None:
    """按 opaque instance ID 精确取回 descriptor；默认只返回 active。"""
    opaque_id = validate_agent_instance_id(instance_id)
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
    record = data["descriptors"].get(f"instance|{opaque_id}")
    if not isinstance(record, dict):
        return None
    if not include_retired and not _launch_descriptor_is_active(record):
        return None
    return dict(record)


def list_workspace_launch_descriptors(
    session: str, project_id: str, workspace_id: str,
) -> tuple[dict[str, Any], ...]:
    """List active managed descriptors bound to one exact Registry authority."""
    if (
        not isinstance(session, str)
        or not session
        or not isinstance(project_id, str)
        or not re.fullmatch(r"prj_[0-9a-f]{32}", project_id)
        or not isinstance(workspace_id, str)
        or not re.fullmatch(r"ws_[0-9a-f]{32}", workspace_id)
    ):
        raise ValueError("workspace launch descriptor authority 格式无效")
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors_strict()
    values: list[dict[str, Any]] = []
    for key, record in data["descriptors"].items():
        if not isinstance(record, dict):
            continue
        has_authority = (
            "project_id" in record or "workspace_id" in record
        )
        if not has_authority:
            continue
        instance_id = record.get("instance_id")
        valid = (
            isinstance(instance_id, str)
            and _AGENT_INSTANCE_ID_RE.fullmatch(instance_id) is not None
            and key == f"instance|{instance_id}"
            and record.get("name") == instance_id
            and isinstance(record.get("project_id"), str)
            and re.fullmatch(
                r"prj_[0-9a-f]{32}", record["project_id"],
            ) is not None
            and isinstance(record.get("workspace_id"), str)
            and re.fullmatch(
                r"ws_[0-9a-f]{32}", record["workspace_id"],
            ) is not None
            and isinstance(record.get("session"), str)
            and bool(record.get("session"))
            and isinstance(record.get("workdir"), str)
            and bool(record.get("workdir"))
            and isinstance(record.get("kind"), str)
            and record.get("agent") == record.get("kind")
            and record.get("args") == []
            and record.get("state") in {
                "active", "pending", "retired", "retirement_pending",
            }
            and (
                record.get("state") != "pending"
                or record.get("launch_label") == _workspace_launch_label(instance_id)
            )
        )
        if not valid:
            raise ValueError("workspace launch descriptor 损坏")
        if (
            not _launch_descriptor_is_active(record)
            or record.get("session") != session
            or record.get("project_id") != project_id
            or record.get("workspace_id") != workspace_id
        ):
            continue
        values.append(dict(record))
    return tuple(sorted(values, key=lambda item: item["instance_id"]))


def _strict_launch_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate launch descriptor key")
        value[key] = item
    return value


def recover_workspace_launch_descriptors(
    session: str, project_id: str, workspace_id: str,
) -> None:
    """Promote exact live pending agents or verify cleanup before discarding."""
    if (
        not re.fullmatch(r"prj_[0-9a-f]{32}", project_id)
        or not re.fullmatch(r"ws_[0-9a-f]{32}", workspace_id)
    ):
        raise ValueError("project/workspace authority 格式无效")
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors_strict()
        pending = [
            dict(record) for record in data["descriptors"].values()
            if isinstance(record, dict)
            and record.get("state") == "pending"
            and record.get("session") == session
            and record.get("project_id") == project_id
            and record.get("workspace_id") == workspace_id
        ]
    if not pending:
        return
    snapshot_value = session_snapshot(session)
    if snapshot_value.get("error") is not None:
        raise RuntimeError("pending descriptor snapshot unavailable")
    panes = snapshot_value.get("panes")
    agents = snapshot_value.get("agents")
    if not isinstance(panes, list) or not isinstance(agents, list):
        raise RuntimeError("pending descriptor snapshot invalid")
    for record in pending:
        instance_id = record.get("instance_id")
        pane_id = record.get("pane_id")
        kind = record.get("kind")
        workdir = record.get("workdir")
        launch_label = record.get("launch_label")
        if launch_label != _workspace_launch_label(str(instance_id)):
            raise RuntimeError("pending descriptor launch label invalid")
        if not pane_id:
            tabs = snapshot_value.get("tabs")
            if not isinstance(tabs, list):
                raise RuntimeError("pending descriptor tab snapshot invalid")
            matching_tabs = [
                item for item in tabs
                if isinstance(item, dict) and item.get("label") == launch_label
            ]
            if not matching_tabs:
                if any(
                    isinstance(item, dict) and item.get("name") == instance_id
                    for item in agents
                ) or any(
                    isinstance(item, dict) and item.get("label") == launch_label
                    for item in panes
                ):
                    raise RuntimeError("pending descriptor launch identity conflict")
                discard_pending_workspace_launch_descriptor(str(instance_id))
                continue
            if len(matching_tabs) != 1:
                raise RuntimeError("pending descriptor launch label ambiguous")
            matched_tab = matching_tabs[0]
            tab_id = matched_tab.get("tab_id")
            if (
                not isinstance(tab_id, str)
                or not tab_id
                or isinstance(matched_tab.get("pane_count"), bool)
                or matched_tab.get("pane_count") != 1
            ):
                raise RuntimeError("pending descriptor launch tab invalid")
            matching_panes = [
                item for item in panes
                if isinstance(item, dict) and item.get("tab_id") == tab_id
            ]
            if len(matching_panes) != 1:
                raise RuntimeError("pending descriptor launch pane ambiguous")
            matched_pane = matching_panes[0]
            recovered_pane_id = matched_pane.get("pane_id")
            if (
                not isinstance(recovered_pane_id, str)
                or not recovered_pane_id
                or matched_pane.get("cwd") != workdir
                or matched_pane.get("agent") not in {None, ""}
                or any(
                    isinstance(item, dict)
                    and (
                        item.get("name") == instance_id
                        or item.get("pane_id") == recovered_pane_id
                    )
                    for item in agents
                )
            ):
                raise RuntimeError("pending descriptor launch authority mismatch")
            bind_pending_workspace_launch_descriptor(
                str(instance_id), recovered_pane_id,
            )
            pane_id = recovered_pane_id
        live_agents = [
            item for item in agents
            if isinstance(item, dict) and item.get("name") == instance_id
        ]
        live_panes = [
            item for item in panes
            if isinstance(item, dict) and item.get("pane_id") == pane_id
        ] if pane_id else []
        if len(live_agents) == 1 and len(live_panes) == 1:
            live_agent = live_agents[0]
            live_pane = live_panes[0]
            if (
                live_agent.get("pane_id") != pane_id
                or live_agent.get("kind", live_agent.get("agent")) != kind
                or live_pane.get("agent") != kind
                or live_pane.get("cwd") != workdir
            ):
                raise RuntimeError("pending descriptor live authority mismatch")
            activate_pending_workspace_launch_descriptor(instance_id, pane_id)
            continue
        if live_agents:
            raise RuntimeError("pending descriptor live identity ambiguous")
        if pane_id and live_panes:
            if not _close_created_pane_verified(session, pane_id, str(instance_id)):
                raise RuntimeError("pending descriptor cleanup incomplete")
        discard_pending_workspace_launch_descriptor(instance_id)


def session_snapshot(session: str) -> dict[str, Any]:
    """Return one Herdr session snapshot for stable instance reconciliation."""
    next_profile.require_session(session)
    return _snapshot_session(session)


def ensure_session(session: str) -> dict[str, Any]:
    """Ensure the fixed Next Herdr session exists before a managed launch."""
    next_profile.require_session(session)
    if not is_available():
        return {"available": False}
    with _SESSION_BOOTSTRAP_LOCK:
        deadline = time.monotonic() + SESSION_BOOTSTRAP_TIMEOUT_S
        owned = _SESSION_BOOTSTRAP_PROCESSES.get(session)
        if owned is not None and owned.poll() is not None:
            _SESSION_BOOTSTRAP_PROCESSES.pop(session, None)
            owned = None
        if _session_bootstrap_ready(session, deadline):
            return {"available": True, "session": session, "created": False}
        if getattr(_LIST_SESSIONS_FAILED, "value", False):
            return {"available": False, "error": "session bootstrap failed"}

        created = False
        if owned is None:
            if time.monotonic() >= deadline:
                return {"available": False, "error": "session bootstrap failed"}
            extra_path = _HERDR_DIR + (
                ":" + os.environ.get("PATH", "")
                if os.environ.get("PATH") else ""
            )
            environment = {
                **os.environ,
                "PATH": extra_path or os.environ.get("PATH", "/usr/bin:/bin"),
            }
            try:
                owned = subprocess.Popen(
                    [HERDR_BIN, "--session", session, "server"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                    env=environment,
                )
            except OSError:
                return {"available": False, "error": "session bootstrap failed"}
            _SESSION_BOOTSTRAP_PROCESSES[session] = owned
            created = True

        while time.monotonic() < deadline:
            if owned.poll() is not None:
                break
            if _session_bootstrap_ready(session, deadline):
                return {
                    "available": True, "session": session, "created": created,
                }
            time.sleep(min(
                SESSION_BOOTSTRAP_POLL_S,
                max(0.0, deadline - time.monotonic()),
            ))

        if created:
            if _terminate_bootstrap_process(owned):
                _SESSION_BOOTSTRAP_PROCESSES.pop(session, None)
            else:
                return {
                    "available": False,
                    "error_code": "session_cleanup_incomplete",
                    "error": "session bootstrap cleanup incomplete",
                }
        return {"available": False, "error": "session bootstrap failed"}


def _session_bootstrap_ready(session: str, deadline: float) -> bool:
    _SNAPSHOT_DEADLINE.value = deadline
    try:
        sessions = list_sessions()
    finally:
        try:
            del _SNAPSHOT_DEADLINE.value
        except AttributeError:
            pass
    if getattr(_LIST_SESSIONS_FAILED, "value", False):
        return False
    try:
        expected_root = _herdr_sessions_root() / session
    except (OSError, ValueError):
        _LIST_SESSIONS_FAILED.value = True
        return False
    matches = [
        item for item in sessions
        if item.get("name") == session and item.get("status") == "running"
    ]
    if len(matches) != 1:
        return False
    row = matches[0]
    try:
        directory = Path(str(row.get("directory") or "")).resolve(strict=False)
        socket_path = Path(str(row.get("socket") or "")).resolve(strict=False)
    except OSError:
        return False
    if directory != expected_root or socket_path != expected_root / "herdr.sock":
        return False
    _SNAPSHOT_DEADLINE.value = deadline
    try:
        snapshot_value = _snapshot_session(session)
    finally:
        try:
            del _SNAPSHOT_DEADLINE.value
        except AttributeError:
            pass
    return (
        snapshot_value.get("session") == session
        and snapshot_value.get("error") is None
        and isinstance(snapshot_value.get("panes"), list)
        and isinstance(snapshot_value.get("agents"), list)
    )


def _terminate_bootstrap_process(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None:
        return True
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None


def update_launch_descriptor_by_instance(
    instance_id: str, **changes: Any,
) -> dict[str, Any] | None:
    """更新 managed descriptor 的运行位置或 Mail 映射。"""
    opaque_id = validate_agent_instance_id(instance_id)
    allowed = {
        "pane_id", "session", "display_name", "mail_agent", "mail_instance",
        "mail_name", "mail_project", "retirement_error",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError("不支持的 launch descriptor 字段: " + ", ".join(sorted(unknown)))
    if "display_name" in changes:
        changes["display_name"] = validate_display_name(changes["display_name"])
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
        record = data["descriptors"].get(f"instance|{opaque_id}")
        if not isinstance(record, dict):
            return None
        record.update(changes)
        _save_launch_descriptors(data)
        return dict(record)


def pending_launch_descriptor_retirements() -> list[dict[str, Any]]:
    """返回待同步到 Agent Mail Hub 的退休 tombstone。"""
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
    return [
        dict(record) for record in data["descriptors"].values()
        if isinstance(record, dict) and record.get("state") == "retirement_pending"
    ]


def _mark_launch_descriptors_retirement_pending(
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    """把匹配的 managed descriptor 转成 pending；legacy 记录直接清理。"""
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
        descriptors = data["descriptors"]
        instance_ids: list[str] = []
        legacy_keys: list[str] = []
        changed = False
        for key, record in descriptors.items():
            if not isinstance(record, dict) or not predicate(record):
                continue
            instance_id = record.get("instance_id")
            if isinstance(instance_id, str) and _AGENT_INSTANCE_ID_RE.fullmatch(instance_id):
                if record.get("state", "active") == "retired":
                    continue
                record["state"] = "retirement_pending"
                record["retirement_pending_at"] = time.time()
                record.pop("retirement_error", None)
                changed = True
                instance_ids.append(instance_id)
            else:
                legacy_keys.append(key)
        for key in legacy_keys:
            del descriptors[key]
            changed = True
        if changed:
            try:
                _save_launch_descriptors(data)
            except OSError as exc:
                return {"cleared": 0, "instance_ids": [], "error": str(exc)}
        return {
            "cleared": len(instance_ids) + len(legacy_keys),
            "instance_ids": instance_ids,
        }


def mark_launch_descriptor_retirement_pending(
    session: str, pane_id: str,
) -> dict[str, Any]:
    return _mark_launch_descriptors_retirement_pending(
        lambda record: record.get("session") == session and record.get("pane_id") == pane_id
    )


def mark_launch_descriptors_retirement_pending(session: str) -> dict[str, Any]:
    return _mark_launch_descriptors_retirement_pending(
        lambda record: record.get("session") == session
    )


def finalize_launch_descriptor_retirement(instance_id: str) -> dict[str, Any] | None:
    """Hub retire 成功后保留不可复用的 retired tombstone。"""
    opaque_id = validate_agent_instance_id(instance_id)
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
        record = data["descriptors"].get(f"instance|{opaque_id}")
        if not isinstance(record, dict):
            return None
        record["state"] = "retired"
        record["retired_at"] = time.time()
        record.pop("retirement_error", None)
        _save_launch_descriptors(data)
        return dict(record)


def fail_launch_descriptor_retirement(
    instance_id: str, error: str,
) -> dict[str, Any] | None:
    """记录可重试退休失败，不把身份误标成已退休。"""
    opaque_id = validate_agent_instance_id(instance_id)
    with _LAUNCH_DESCRIPTOR_LOCK:
        data = _load_launch_descriptors()
        record = data["descriptors"].get(f"instance|{opaque_id}")
        if not isinstance(record, dict):
            return None
        if record.get("state") == "retired":
            return dict(record)
        record["state"] = "retirement_pending"
        record["retirement_error"] = str(error)[:500]
        record["retirement_attempts"] = int(record.get("retirement_attempts") or 0) + 1
        _save_launch_descriptors(data)
        return dict(record)


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


def _close_created_pane_verified(
    session: str, pane_id: str, instance_id: str,
) -> bool:
    try:
        _run(["--session", session, "pane", "close", pane_id], timeout=5)
    except RuntimeError:
        pass
    snapshot_value = _snapshot_session(session)
    if snapshot_value.get("error") is not None:
        return False
    panes = snapshot_value.get("panes")
    agents = snapshot_value.get("agents")
    if not isinstance(panes, list) or not isinstance(agents, list):
        return False
    return not any(
        isinstance(item, dict) and item.get("pane_id") == pane_id
        for item in panes
    ) and not any(
        isinstance(item, dict) and item.get("name") == instance_id
        for item in agents
    )


def start_agent(
    session: str, workdir: str, agent: str = "codex", model: str | None = None,
    layout: str = "tab", label: str | None = None, args: str = "",
    instance_id: str | None = None,
    project_id: str | None = None, workspace_id: str | None = None,
) -> dict[str, Any]:
    """在指定 session 里启动一个 agent pane(新建 tab/pane 跑 agent)。

    全部受支持 agent 统一用 Herdr 原生 `agent start`：先按 layout 创建 pane 并
    解析响应 ID，再以唯一 name + --kind + --pane 启动，readiness 由 --timeout 兜底。
    agent: codex | claude | kimi | opencode | grok | qoder(cli/cn)。
    返回新 pane 信息；启动失败回滚本次 pane，返回结构化 error，不回退键盘模拟。
    """
    if agent in _ATTACH_ONLY_PRODUCTS:
        return {
            "available": True,
            "error_code": "attach_only_agent",
            "error": f"{agent} 仅支持绑定已有 pane，不能由 Cockpit 启动",
        }
    if not is_available():
        return {"available": False}
    managed = instance_id is not None
    workspace_managed = managed and project_id is not None
    display_name: str | None = None
    try:
        require_herdr_capabilities()
        normalize_agent_kind(agent)
        if not managed and (project_id is not None or workspace_id is not None):
            raise ValueError("workspace authority 仅适用于 managed agent")
        if managed:
            instance_id = validate_agent_instance_id(instance_id)
            display_name = validate_display_name(label or agent)
            if (project_id is None) != (workspace_id is None):
                raise ValueError("project/workspace authority 必须成对提供")
            if project_id is not None and (
                not re.fullmatch(r"prj_[0-9a-f]{32}", project_id)
                or not re.fullmatch(r"ws_[0-9a-f]{32}", workspace_id or "")
            ):
                raise ValueError("project/workspace authority 格式无效")
        elif label is not None:
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
    if agent == "grok" and not workspace_managed:
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
    if managed:
        existing = None
    elif label:
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
        if managed:
            assert instance_id is not None
            live_names = {
                str(item.get("name")) for item in snap.get("agents", [])
                if isinstance(item, dict) and item.get("name")
            }
            if instance_id in live_names:
                raise ValueError(f"agent instance id 已被 live agent 使用: {instance_id}")
            if get_launch_descriptor_by_instance(instance_id, include_retired=True):
                raise ValueError(f"agent instance id 已存在，不能复用: {instance_id}")
            runtime_name = instance_id
        else:
            runtime_name = resolve_unique_agent_name(agent, label, snap.get("agents", []))
    except ValueError as exc:
        return {"available": True, "error": str(exc)}
    canonical_kind = normalize_agent_kind(agent)
    pending_reserved = False
    workspace_target: str | None = None
    bootstrap_root_pane_id: str | None = None
    internal_agent_args: list[str] = []
    if workspace_managed:
        assert instance_id is not None
        assert project_id is not None and workspace_id is not None
        if agent_args and (
            canonical_kind != "codex"
            or agent_args != _WORKSPACE_CODEX_READONLY_PUBLIC_ARGS
        ):
            return {
                "available": True,
                "error_code": "workspace_agent_args_forbidden",
                "error": "workspace managed agent args must be empty",
            }
        if layout != "tab":
            return {
                "available": True,
                "error_code": "workspace_agent_layout_forbidden",
                "error": "workspace managed agent layout must be tab",
            }
        if canonical_kind == "codex":
            try:
                internal_agent_args = _workspace_codex_trust_args(workdir)
            except ValueError:
                return {
                    "available": True,
                    "error_code": "workspace_agent_trust_unavailable",
                    "error": "workspace agent trust unavailable",
                }
        launch_label = _workspace_launch_label(instance_id)
        if any(
            isinstance(item, dict) and item.get("label") == launch_label
            for item in snap.get("tabs", [])
        ):
            return {
                "available": True,
                "error_code": "descriptor_prepare_failed",
                "error": "workspace launch authority unavailable",
            }
        try:
            bootstrap = _workspace_managed_bootstrap(session, workdir, snap)
            workspace_target = bootstrap.workspace_id
            bootstrap_root_pane_id = bootstrap.root_pane_id
        except (OSError, RuntimeError, ValueError):
            return {
                "available": True,
                "error_code": "workspace_bootstrap_failed",
                "error": "workspace bootstrap unavailable",
            }
        try:
            reserve_workspace_launch_descriptor(
                session=session, name=instance_id, kind=canonical_kind,
                agent=agent, workdir=workdir, instance_id=instance_id,
                display_name=display_name or agent, project_id=project_id,
                workspace_id=workspace_id, args=list(agent_args),
            )
            pending_reserved = True
        except (OSError, ValueError):
            return {
                "available": True,
                "error_code": "descriptor_prepare_failed",
                "error": "workspace launch authority unavailable",
            }
    before_ids = {
        str(p.get("pane_id")) for p in snap.get("panes", []) if p.get("pane_id")
    }
    if bootstrap_root_pane_id is not None:
        before_ids.add(bootstrap_root_pane_id)
    # OpenCode/Bun 在窄 split 中可能直接 fatal signal 4；即使调用方仍传旧默认
    # right，也自动使用独立 tab。其他 agent 尊重显式布局。
    effective_layout = "tab" if agent == "opencode" else layout
    new_pid = None
    try:
        # 根据 layout 开新 pane:right/down 用 split,tab 用 tab create
        if effective_layout == "tab":
            # 多页:每个 agent 一个新 tab
            create_out = _run(
                [
                    "--session", session, "tab", "create",
                    *(
                        ["--workspace", workspace_target]
                        if workspace_target is not None else []
                    ),
                    "--cwd", workdir,
                    *(["--label", launch_label] if workspace_managed else []),
                ],
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
        create_data = _parse_data_json(create_out)
        create_result = (
            create_data.get("result") if isinstance(create_data, dict) else None
        )
        if isinstance(create_result, dict):
            result_pane = create_result.get("pane")
            result_root_pane = create_result.get("root_pane")
            result_tab = create_result.get("tab")
            reported_pid = (
                result_pane.get("pane_id")
                if isinstance(result_pane, dict) else None
            ) or (
                result_root_pane.get("pane_id")
                if isinstance(result_root_pane, dict) else None
            ) or (
                result_tab.get("focused_pane_id")
                if isinstance(result_tab, dict) else None
            )

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
        if workspace_managed:
            assert instance_id is not None
            try:
                bind_pending_workspace_launch_descriptor(instance_id, new_pid)
            except (OSError, ValueError):
                cleaned = _close_created_pane_verified(
                    session, new_pid, instance_id,
                )
                if cleaned:
                    try:
                        discard_pending_workspace_launch_descriptor(instance_id)
                    except (OSError, ValueError):
                        pass
                return {
                    "available": True,
                    "error_code": (
                        "descriptor_bind_failed"
                        if cleaned else "descriptor_cleanup_incomplete"
                    ),
                    "error": "workspace launch authority unavailable",
                    "pane_id": new_pid,
                    "rolled_back": cleaned,
                }
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
        native_agent_args = [*agent_args, *internal_agent_args]
        if native_agent_args:
            start_argv += ["--", *native_agent_args]
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
            display_name or runtime_name,
        )
        # 持久化权威 launch 契约 {name, kind, args}：Herdr 不保留原始 start argv，
        # 故由启动路径落盘，供 restart 按 session+pane/name 精确取回原参数重建，
        # 绝不从进程 argv/label/类型默认值猜测。落盘失败不杀已成功启动的 agent。
        descriptor_error = False
        if workspace_managed:
            assert instance_id is not None
            try:
                activate_pending_workspace_launch_descriptor(instance_id, new_pid)
            except (OSError, ValueError):
                descriptor_error = True
                cleaned = _close_created_pane_verified(
                    session, new_pid, instance_id,
                )
                if cleaned:
                    try:
                        discard_pending_workspace_launch_descriptor(instance_id)
                    except (OSError, ValueError):
                        pass
                if not cleaned:
                    return {
                        "available": True,
                        "error_code": "descriptor_cleanup_incomplete",
                        "error": "workspace launch cleanup incomplete",
                        "pane_id": new_pid,
                        "instance_id": instance_id,
                        "rolled_back": False,
                    }
        else:
            try:
                save_launch_descriptor(
                    session=session, pane_id=new_pid, name=runtime_name,
                    kind=canonical_kind, args=agent_args, agent=agent,
                    workdir=workdir, instance_id=instance_id,
                    display_name=display_name,
                )
            except OSError:
                descriptor_error = True
        result = {
            "available": True,
            "pane_id": new_pid,
            "agent": agent,
            "name": runtime_name,
            "kind": canonical_kind,
            "layout": effective_layout,
        }
        if managed:
            result["instance_id"] = instance_id
            result["display_name"] = display_name
        if label:
            result["label"] = display_name or label
        if descriptor_error:
            result["descriptor_error"] = "launch descriptor unavailable"
            result["rolled_back"] = workspace_managed
        return result
    except RuntimeError as e:
        rolled_back = False
        if new_pid:
            if workspace_managed:
                rolled_back = _close_created_pane_verified(
                    session, new_pid, str(instance_id),
                )
            else:
                try:
                    _run(
                        ["--session", session, "pane", "close", new_pid],
                        timeout=5,
                    )
                    rolled_back = True
                except RuntimeError:
                    pass
        if workspace_managed and pending_reserved and instance_id is not None:
            if rolled_back or new_pid is None:
                try:
                    discard_pending_workspace_launch_descriptor(instance_id)
                except (OSError, ValueError):
                    pass
            elif new_pid is not None:
                return {
                    "available": True,
                    "error_code": "descriptor_cleanup_incomplete",
                    "error": "workspace launch cleanup incomplete",
                    "pane_id": new_pid,
                    "instance_id": instance_id,
                    "rolled_back": False,
                }
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
    cleanup = mark_launch_descriptor_retirement_pending(session, pane_id)
    result: dict[str, Any] = {"available": True, "closed": pane_id}
    if cleanup.get("error"):
        result["descriptor_cleanup_error"] = cleanup["error"]
    else:
        result["descriptors_cleared"] = cleanup.get("cleared", 0)
        if cleanup.get("instance_ids"):
            result["retirement_pending"] = cleanup["instance_ids"]
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


def _move_pane(session: str, args: list[str]) -> dict[str, Any]:
    """执行 pane move，返回包含新 pane ID 的权威 move_result。"""
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
    return move


def _pane_identity_label(
    session: str, pane: dict[str, Any],
) -> tuple[dict[str, Any] | None, str, str]:
    """返回 descriptor、显示标签和 agent kind；显示字段不参与任何寻址。"""
    pane_id = str(pane.get("pane_id") or "")
    descriptor = get_launch_descriptor(session, pane_id) if pane_id else None
    candidates = (
        descriptor.get("display_name") if descriptor else None,
        pane.get("label"),
        pane.get("agent"),
        pane.get("runtime_name") or pane.get("agent_name") or pane.get("name"),
    )
    label = next(
        (str(value).strip() for value in candidates if str(value or "").strip()),
        pane_id or "agent",
    )
    agent = str(
        (descriptor or {}).get("agent") or pane.get("agent") or "agent"
    ).strip() or "agent"
    return descriptor, label, agent


def _restore_moved_pane_identity(
    session: str, pane: dict[str, Any], move: dict[str, Any], *, layout: str,
) -> str:
    """move 后迁移 descriptor，并恢复 pane/tab 的可读标签。"""
    old_pane_id = str(pane.get("pane_id") or "")
    descriptor, label, agent = _pane_identity_label(session, pane)
    moved_pane = move.get("pane") if isinstance(move.get("pane"), dict) else {}
    new_pane_id = str(moved_pane.get("pane_id") or old_pane_id)
    if not new_pane_id:
        raise RuntimeError("pane move 成功但未返回可识别的 pane id")
    descriptor_error: str | None = None
    if descriptor and new_pane_id != old_pane_id:
        instance_id = descriptor.get("instance_id")
        if instance_id:
            try:
                update_launch_descriptor_by_instance(instance_id, pane_id=new_pane_id)
            except (OSError, ValueError) as exc:
                descriptor_error = str(exc)
    renamed_pane = dict(pane)
    renamed_pane.update(moved_pane)
    renamed_pane["pane_id"] = new_pane_id
    _rename_agent_context(session, renamed_pane, agent, layout, label)
    if descriptor_error:
        raise RuntimeError(
            f"pane 已移动到 {new_pane_id}，但 descriptor 迁移失败: {descriptor_error}"
        )
    return new_pane_id


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


def detach_pane(session: str, pane_id: str) -> str:
    """把 pane 拆到独立 tab，并返回 move 后的真实 pane id。"""
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
    move = _move_pane(session, [pane_id, "--new-tab"])
    return _restore_moved_pane_identity(session, pane, move, layout="tab")


def untile_tab(session: str, tab_id: str) -> list[str]:
    """拆开 tab 内分屏:保留第一个 pane,其余逐个移到独立 tab。"""
    snap = _snapshot_session(session)
    panes = [
        p
        for p in snap.get("panes", [])
        if p.get("pane_id") and str(p.get("tab_id") or "") == str(tab_id)
    ]
    if not panes:
        raise ValueError(f"未找到 tab: {tab_id}")
    pane_ids = [str(p.get("pane_id")) for p in panes]
    _require_unzoomed(snap, pane_ids)
    moved: list[str] = []
    for pane in panes[1:]:
        pid = str(pane.get("pane_id"))
        try:
            move = _move_pane(session, [pid, "--new-tab"])
            moved_id = _restore_moved_pane_identity(
                session, pane, move, layout="tab",
            )
        except RuntimeError as exc:
            raise RuntimeError(
                f"拆开整组时已移动 {len(moved)} 个 pane，后续操作失败: {exc}"
            ) from exc
        moved.append(moved_id)
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

    def _move(pid: str, target: str, direction: str, ratio: str) -> dict[str, Any]:
        return _move_pane(
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
    pane_by_id = {
        str(p.get("pane_id")): p for p in snap.get("panes", []) if p.get("pane_id")
    }
    current_ids = {pid: pid for pid in pane_ids}
    try:
        for original_pid, original_target, move_direction, ratio in moves:
            pid = current_ids[original_pid]
            target = current_ids[original_target]
            pane = pane_by_id[original_pid]
            move = _move(pid, target, move_direction, ratio)
            current_ids[original_pid] = _restore_moved_pane_identity(
                session, pane, move, layout=direction,
            )
            completed += 1
    except RuntimeError as exc:
        raise RuntimeError(
            f"组合分屏时已完成 {completed}/{len(moves)} 步，后续操作失败: {exc}"
        ) from exc
    # 基准 pane 没有 move，也恢复一次名称，防止原 tab 名覆盖了 pane 识别信息。
    _restore_moved_pane_identity(
        session, pane_by_id[base], {"changed": True}, layout=direction,
    )
    return current_ids[base]


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
        if product_agent in _ATTACH_ONLY_PRODUCTS:
            return _restart_error(
                "attach_only_agent", f"{product_agent} 仅支持外部运行时重启", pane_id,
            )
        try:
            if normalize_agent_kind(product_agent) != kind:
                raise ValueError("kind 不匹配")
            internal_agent_args = _workspace_descriptor_internal_args(descriptor)
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
            if kind == "grok":
                _run(
                    ["--session", session, "pane", "send-keys", pane_id, "ctrl+u"],
                    timeout=3,
                )
                _run(
                    ["--session", session, "pane", "send-text", pane_id, "/quit"],
                    timeout=3,
                )
                _run(
                    ["--session", session, "pane", "send-keys", pane_id, "Enter"],
                    timeout=3,
                )
            else:
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
                kind != "grok"
                and not second_interrupt_sent
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

        public_args = list(launch_args)
        if resume:
            public_args += ["resume", "--last"]
        native_args = [*internal_agent_args, *public_args]
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
        result = {
            "available": True, "restarted": True, "preserved": True,
            "pane_id": pane_id, "agent": product_agent, "name": name,
            "kind": kind, "args": public_args, "resume": resume,
        }
        if descriptor.get("instance_id"):
            result["instance_id"] = descriptor["instance_id"]
            result["display_name"] = descriptor.get("display_name") or product_agent
        return result
    finally:
        with _RESTART_GUARD:
            _RESTARTING_PANES.discard(key)


def stop_session(session: str) -> dict[str, Any]:
    """停止一个 herdr session。"""
    try:
        next_profile.require_session(session)
    except next_profile.NextProfileError as exc:
        return {"available": True, "error": str(exc)}
    if not is_available():
        return {"available": False}
    try:
        _run(["session", "stop", session], timeout=10)
        return {"available": True, "stopped": session}
    except RuntimeError as e:
        return {"available": True, "error": str(e)}


def delete_session(session: str) -> dict[str, Any]:
    """删除一个已停止的 session。"""
    try:
        next_profile.require_session(session)
    except next_profile.NextProfileError as exc:
        return {"available": True, "error": str(exc)}
    if not is_available():
        return {"available": False}
    try:
        _run(["session", "delete", session], timeout=10)
    except RuntimeError as e:
        return {"available": True, "error": str(e)}
    # session 已删除：清理该 session 的 launch descriptor，避免同名 session 重建后
    # 把上一代 args 误当当前权威契约（workspace/pane/name ID 会被 Herdr 重新分配）。
    # 清理失败不复活 session，但必须结构化暴露，不静默宣告 descriptor 已安全。
    cleanup = mark_launch_descriptors_retirement_pending(session)
    result: dict[str, Any] = {"available": True, "deleted": session}
    if cleanup.get("error"):
        result["descriptor_cleanup_error"] = cleanup["error"]
    else:
        result["descriptors_cleared"] = cleanup.get("cleared", 0)
        if cleanup.get("instance_ids"):
            result["retirement_pending"] = cleanup["instance_ids"]
    return result
