"""固定 systemd / launchd supervisor 纯适配层（U2 S0）。

职责（仅此）：
- 渲染固定 Linux user unit 与固定 Mac 主 / controller LaunchAgent，均指向受控
  ``current`` generation（或 release 外 controller 根）。
- 校验绝对路径、控制字符、symlink 边界，以及 allowlisted 合同（KillMode、
  固定 label、WD/Exec 同源）。
- 产出 systemctl / launchctl **命令计划**（argv 列表），**从不执行**。

明确不做：
- 不调用 ``systemctl`` / ``launchctl`` / ``subprocess``。
- 不切换 ``current`` symlink，不写生产 unit/plist 路径。
- 不改 install.sh / launchd.sh 等安装入口。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence

# ── 固定合同 ──────────────────────────────────────────────────────

LINUX_UNIT_NAME = "agent-cockpit.service"
MAC_MAIN_LABEL = "io.github.fyc0451.agent-cockpit"
MAC_CONTROLLER_LABEL = "io.github.fyc0451.agent-cockpit-controller"

# 允许的固定 Mac labels（V1 动态 upgrade.<job> 一律禁止）
FIXED_MAC_LABELS: frozenset[str] = frozenset({MAC_MAIN_LABEL, MAC_CONTROLLER_LABEL})

# V1 oneshot 动态 label 形态：io.github.fyc0451.agent-cockpit.upgrade.<job>
_FORBIDDEN_MAC_LABEL_RE = re.compile(
    r"(?:^|\.)upgrade(?:\.|$)",
    re.IGNORECASE,
)

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class SupervisorAdapterError(Exception):
    """路径 / 合同 / 标签校验失败；``reason`` 机器可读。

    刻意不继承 ValueError，避免与 Path.relative_to 等 API 的 except ValueError 混淆。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        msg = reason if not detail else f"{reason}: {detail}"
        super().__init__(msg)


@dataclass(frozen=True)
class PlannedCommand:
    """尚未执行的 supervisor 命令。``argv[0]`` 为工具名。"""

    argv: tuple[str, ...]
    purpose: str

    def as_list(self) -> list[str]:
        return list(self.argv)


@dataclass(frozen=True)
class LinuxUnitContract:
    unit_name: str
    kill_mode: str
    working_directory: str
    exec_start: str
    environment_file: str | None


@dataclass(frozen=True)
class MacPlistContract:
    label: str
    program_arguments: tuple[str, ...]
    working_directory: str
    run_at_load: bool
    keep_alive: bool


# ── 路径校验 ──────────────────────────────────────────────────────


def _has_control_chars(value: str) -> bool:
    return bool(_CONTROL_CHAR_RE.search(value))


def validate_absolute_path(
    value: str | Path,
    *,
    role: str = "path",
    must_exist: bool = False,
) -> Path:
    """要求 POSIX 风格绝对路径、无控制字符；可选存在性。

    返回未 resolve 的 Path（保留 ``current`` symlink 字面量用于 unit 渲染）。
    """
    if isinstance(value, Path):
        text = value.as_posix() if value.anchor else str(value)
    else:
        text = str(value)
    if not text or text != text.strip():
        raise SupervisorAdapterError("empty_or_padded_path", role)
    if _has_control_chars(text):
        raise SupervisorAdapterError("control_char_in_path", role)
    if "\x00" in text:
        raise SupervisorAdapterError("nul_in_path", role)
    # 拒绝 Windows 盘符与相对段开头
    if not text.startswith("/"):
        raise SupervisorAdapterError("relative_path", role)
    pure = PurePosixPath(text)
    if ".." in pure.parts:
        raise SupervisorAdapterError("path_parent_segment", role)
    path = Path(text)
    if must_exist and not path.exists():
        raise SupervisorAdapterError("path_missing", role)
    return path


def validate_current_path(
    current: str | Path,
    *,
    deploy_root: str | Path | None = None,
    require_name_current: bool = True,
) -> Path:
    """受控 ``current`` 路径：绝对、无控制字符；可选落在 deploy_root 内。

    - 字面量路径用于 unit/plist（不 resolve 进 generation 目录）。
    - 若路径已存在且为 symlink / 目录，resolve 后不得逃出 ``deploy_root``。
    - 默认要求最后一级名为 ``current``。
    """
    path = validate_absolute_path(current, role="current")
    if require_name_current and path.name != "current":
        raise SupervisorAdapterError("current_name_required", "basename must be 'current'")
    if deploy_root is not None:
        root = validate_absolute_path(deploy_root, role="deploy_root")
        # 字面量必须位于 root 之下
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise SupervisorAdapterError("current_outside_deploy_root") from exc
        if path.exists() or path.is_symlink():
            try:
                resolved = path.resolve(strict=False)
                root_resolved = root.resolve(strict=False)
                resolved.relative_to(root_resolved)
            except (OSError, ValueError) as exc:
                raise SupervisorAdapterError("symlink_escape", "current") from exc
    return path


def validate_controller_path(
    controller: str | Path,
    *,
    current: str | Path | None = None,
) -> Path:
    """Controller 安装根：绝对路径；不得落在 ``current`` 或其 generation 内。

    同时检查：
    - 字面量路径不得为 ``current`` 或其子路径；
    - resolve 后不得落在 ``current.resolve()``（即 generation）边界内，
      防止 ``current → generations/g1`` 时把 generation 内路径冒充 release 外 controller。
    """
    path = validate_absolute_path(controller, role="controller")
    if current is not None:
        cur = validate_absolute_path(current, role="current")
        if path == cur or _is_relative_to(path, cur):
            raise SupervisorAdapterError("controller_inside_current")
        try:
            path_res = path.resolve(strict=False)
            cur_res = cur.resolve(strict=False)
        except OSError as exc:
            raise SupervisorAdapterError("controller_path_unresolvable") from exc
        if path_res == cur_res or _is_relative_to(path_res, cur_res):
            raise SupervisorAdapterError("controller_inside_current")
    return path


def _is_relative_to(path: Path, root: Path) -> bool:
    """Path.is_relative_to 兼容封装；不用 except ValueError（Error 亦为其子类）。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ── 转义（对齐 install-paths.sh） ─────────────────────────────────


def escape_systemd_value(value: str) -> str:
    """Environment / WorkingDirectory 等：``\\`` ``\"`` ``%``。"""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
    )


def escape_systemd_exec_value(value: str) -> str:
    """ExecStart 参数：在 systemd_value 之上再把 ``$`` → ``$$``。"""
    return escape_systemd_value(value).replace("$", "$$")


def escape_plist_xml(value: str) -> str:
    """plist 文本节点：``&`` ``<`` ``>`` ``"``。"""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ── Mac label 合同 ────────────────────────────────────────────────


def is_forbidden_mac_label(label: str) -> bool:
    if not label or _has_control_chars(label):
        return True
    if label not in FIXED_MAC_LABELS:
        # 任何含 upgrade 段的动态 label 明确禁止
        if _FORBIDDEN_MAC_LABEL_RE.search(label):
            return True
        return True
    return False


def assert_fixed_mac_label(label: str) -> str:
    if _has_control_chars(label):
        raise SupervisorAdapterError("control_char_in_label")
    if _FORBIDDEN_MAC_LABEL_RE.search(label):
        raise SupervisorAdapterError("forbidden_dynamic_upgrade_label", label)
    if label not in FIXED_MAC_LABELS:
        raise SupervisorAdapterError("label_not_allowlisted", label)
    return label


# ── launcher argv（调用方显式提供；不猜 .venv / launchd.sh） ──────


def normalize_launcher_argv(
    program_arguments: Sequence[str],
    *,
    current_dir: str | Path,
    deploy_root: str | Path | None = None,
) -> tuple[str, ...]:
    """规范化主进程 launcher argv。

    - 非空；每个元素为非空 str，无控制字符。
    - ``argv[0]`` 必须是落在受控 ``current`` **字面量路径**下的绝对路径
      （保留 ``.../current/...``，不 resolve 成 generation）。
    - 不猜测 artifact 内文件名（无默认 ``.venv`` / ``launchd.sh``）。
    """
    if not program_arguments:
        raise SupervisorAdapterError("empty_program_arguments")
    current = validate_current_path(current_dir, deploy_root=deploy_root)
    cur = current.as_posix()
    out: list[str] = []
    for i, arg in enumerate(program_arguments):
        if not isinstance(arg, str) or not arg:
            raise SupervisorAdapterError("invalid_program_argument", str(i))
        if _has_control_chars(arg):
            raise SupervisorAdapterError("control_char_in_argument", str(i))
        if i == 0:
            launcher = validate_absolute_path(arg, role="launcher")
            literal = launcher.as_posix()
            # 输出 / 合同字面量必须挂在 current 字面路径下（不 resolve 成 generation）
            if not (literal == cur or literal.startswith(cur + "/")):
                raise SupervisorAdapterError("launcher_outside_current", literal)
            # 若路径（含中间 symlink）可解析，resolved 必须仍在 current/deploy 边界内
            _assert_launcher_resolved_inside(
                launcher, current=current, deploy_root=deploy_root
            )
            out.append(literal)
        else:
            if arg.startswith("/"):
                validate_absolute_path(arg, role=f"program_arg[{i}]")
            out.append(arg)
    return tuple(out)


def _assert_launcher_resolved_inside(
    launcher: Path,
    *,
    current: Path,
    deploy_root: str | Path | None,
) -> None:
    """拒 ``current/bin → /evil`` 一类子 symlink 逃逸；字面量仍保留 current 前缀。"""
    try:
        resolved = launcher.resolve(strict=False)
        cur_res = current.resolve(strict=False)
    except OSError as exc:
        raise SupervisorAdapterError("launcher_unresolvable") from exc
    if not (resolved == cur_res or _is_relative_to(resolved, cur_res)):
        raise SupervisorAdapterError("launcher_symlink_escape")
    if deploy_root is not None:
        root = validate_absolute_path(deploy_root, role="deploy_root")
        try:
            root_res = root.resolve(strict=False)
        except OSError as exc:
            raise SupervisorAdapterError("launcher_unresolvable") from exc
        if not (resolved == root_res or _is_relative_to(resolved, root_res)):
            raise SupervisorAdapterError("launcher_symlink_escape")


def _format_exec_start(argv: Sequence[str]) -> str:
    """将 argv 格式化为 ExecStart= 右侧；参数一律双引号 + systemd exec 转义。"""
    return " ".join(f'"{escape_systemd_exec_value(a)}"' for a in argv)


def _parse_exec_start_argv(exec_start: str) -> tuple[str, ...]:
    """解析 ExecStart 右侧为 argv（仅支持双引号参数序列，与渲染器对称）。

    引号内保留转义原文，统一交给 ``_unescape_systemd_exec_value``，避免双重反转义。
    """
    s = exec_start.strip()
    if not s:
        raise SupervisorAdapterError("exec_start_empty")
    args: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            break
        if s[i] != '"':
            raise SupervisorAdapterError("exec_start_unquoted_token")
        i += 1
        raw: list[str] = []
        while i < n:
            ch = s[i]
            if ch == "\\" and i + 1 < n:
                raw.append(ch)
                raw.append(s[i + 1])
                i += 2
                continue
            if ch == '"':
                i += 1
                break
            raw.append(ch)
            i += 1
        else:
            raise SupervisorAdapterError("exec_start_unclosed_quote")
        args.append(_unescape_systemd_exec_value("".join(raw)))
    if not args:
        raise SupervisorAdapterError("exec_start_empty")
    return tuple(args)


# ── 渲染 ──────────────────────────────────────────────────────────


def render_linux_unit(
    *,
    current_dir: str | Path,
    program_arguments: Sequence[str],
    deploy_root: str | Path | None = None,
    description: str = "Agent Cockpit (FastAPI :8790)",
) -> str:
    """渲染固定 user unit，指向受控 current；强制 KillMode=process。

    ``program_arguments`` 必须由调用方显式提供（自含 artifact 入口），
    适配层不默认 ``.venv/bin/python`` 或源码 ``server.py``。
    """
    current = validate_current_path(current_dir, deploy_root=deploy_root)
    argv = normalize_launcher_argv(
        program_arguments, current_dir=current, deploy_root=deploy_root
    )
    cur = current.as_posix()
    wd = escape_systemd_value(cur)
    env_file = escape_systemd_value(f"{cur}/.env")
    exec_start = _format_exec_start(argv)
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "# Cockpit 会启动独立的 Herdr server/agent；发布时只替换主进程，保留 Herdr session。\n"
        "KillMode=process\n"
        f"WorkingDirectory={wd}\n"
        f"ExecStart={exec_start}\n"
        "Restart=always\n"
        "RestartSec=3\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        f"EnvironmentFile=-{env_file}\n"
        "NoNewPrivileges=true\n"
        "UMask=0077\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_mac_main_plist(
    *,
    current_dir: str | Path,
    program_arguments: Sequence[str],
    deploy_root: str | Path | None = None,
) -> str:
    """固定主 LaunchAgent：label 仅允许 MAC_MAIN_LABEL，路径指向 current。

    ``program_arguments`` 必须由调用方显式提供；不默认 ``launchd.sh run``。
    """
    current = validate_current_path(current_dir, deploy_root=deploy_root)
    argv = normalize_launcher_argv(
        program_arguments, current_dir=current, deploy_root=deploy_root
    )
    cur = current.as_posix()
    wd = escape_plist_xml(cur)
    stdout = escape_plist_xml(f"{cur}/agent-cockpit.stdout.log")
    stderr = escape_plist_xml(f"{cur}/agent-cockpit.stderr.log")
    label = escape_plist_xml(MAC_MAIN_LABEL)
    args_xml = "\n".join(
        f"    <string>{escape_plist_xml(a)}</string>" for a in argv
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{label}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{args_xml}\n"
        "  </array>\n"
        "  <key>WorkingDirectory</key>\n"
        f"  <string>{wd}</string>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <true/>\n"
        "  <key>ThrottleInterval</key>\n"
        "  <integer>3</integer>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{stdout}</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{stderr}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def render_mac_controller_plist(
    *,
    controller_dir: str | Path,
    program_arguments: Sequence[str],
    current_dir: str | Path | None = None,
    stdout_name: str = "controller.stdout.log",
    stderr_name: str = "controller.stderr.log",
) -> str:
    """固定 controller LaunchAgent（release 外）；禁止 upgrade.<job> label。"""
    controller = validate_controller_path(controller_dir, current=current_dir)
    if not program_arguments:
        raise SupervisorAdapterError("empty_program_arguments")
    args: list[str] = []
    for i, arg in enumerate(program_arguments):
        if not isinstance(arg, str) or not arg:
            raise SupervisorAdapterError("invalid_program_argument", str(i))
        if _has_control_chars(arg):
            raise SupervisorAdapterError("control_char_in_argument", str(i))
        # 首参若为路径则必须绝对
        if i == 0 and arg.startswith("/") is False and "/" in arg:
            raise SupervisorAdapterError("relative_program_path")
        if arg.startswith("/"):
            validate_absolute_path(arg, role=f"program_arg[{i}]")
        args.append(escape_plist_xml(arg))
    wd = escape_plist_xml(controller.as_posix())
    stdout = escape_plist_xml(f"{controller.as_posix()}/{stdout_name}")
    stderr = escape_plist_xml(f"{controller.as_posix()}/{stderr_name}")
    label = escape_plist_xml(MAC_CONTROLLER_LABEL)
    args_xml = "\n".join(f"    <string>{a}</string>" for a in args)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{label}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{args_xml}\n"
        "  </array>\n"
        "  <key>WorkingDirectory</key>\n"
        f"  <string>{wd}</string>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <true/>\n"
        "  <key>ThrottleInterval</key>\n"
        "  <integer>3</integer>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{stdout}</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{stderr}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


# ── 解析 / 校验合同 ──────────────────────────────────────────────


# systemd section → allowlisted keys（关键服务键仅允许在 [Service]）
_UNIT_SECTION_KEYS: dict[str, frozenset[str]] = {
    "Unit": frozenset({"Description", "After"}),
    "Service": frozenset(
        {
            "Type",
            "KillMode",
            "WorkingDirectory",
            "ExecStart",
            "Restart",
            "RestartSec",
            "Environment",
            "EnvironmentFile",
            "NoNewPrivileges",
            "UMask",
        }
    ),
    "Install": frozenset({"WantedBy"}),
}
_SERVICE_REQUIRED_KEYS = frozenset({"KillMode", "WorkingDirectory", "ExecStart"})

# macOS LaunchAgent 顶层键精确 allowlist；Program 可覆盖 ProgramArguments，显式拒绝
MAC_PLIST_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "Label",
        "ProgramArguments",
        "WorkingDirectory",
        "RunAtLoad",
        "KeepAlive",
        "ThrottleInterval",
        "StandardOutPath",
        "StandardErrorPath",
    }
)
MAC_PLIST_FORBIDDEN_KEYS: frozenset[str] = frozenset({"Program"})


def _parse_unit_by_section(text: str) -> dict[str, str]:
    """按 section 解析 unit；关键键仅在 [Service]；拒绝错位与重复。"""
    section: str | None = None
    # (section, key) → value；再投影 service 字段
    seen: set[tuple[str, str]] = set()
    service_fields: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if name not in _UNIT_SECTION_KEYS:
                raise SupervisorAdapterError("unit_section_not_allowlisted", name)
            section = name
            continue
        if section is None:
            raise SupervisorAdapterError("unit_key_outside_section", line[:40])
        if "=" not in line:
            raise SupervisorAdapterError("unit_malformed_line", line[:40])
        key, _, value = line.partition("=")
        key = key.strip()
        allowed = _UNIT_SECTION_KEYS[section]
        if key not in allowed:
            # 服务关键键出现在错误 section 时给出更精确 reason
            if key in _UNIT_SECTION_KEYS["Service"] and section != "Service":
                raise SupervisorAdapterError("unit_key_wrong_section", f"{key} in [{section}]")
            raise SupervisorAdapterError("unit_key_not_allowlisted", key)
        sk = (section, key)
        if sk in seen:
            raise SupervisorAdapterError("unit_duplicate_key", f"[{section}] {key}")
        seen.add(sk)
        if section == "Service":
            service_fields[key] = value
        elif section == "Unit" and key == "Description":
            service_fields.setdefault("_description", value)
        elif section == "Install" and key == "WantedBy":
            service_fields.setdefault("_wanted_by", value)
    return service_fields


def parse_linux_unit_contract(text: str) -> LinuxUnitContract:
    if _has_control_chars(text.replace("\n", "").replace("\t", "")):
        # 允许常见空白；其它控制字符拒绝
        stripped = text.replace("\n", "").replace("\r", "").replace("\t", "")
        if _has_control_chars(stripped):
            raise SupervisorAdapterError("control_char_in_unit")
    fields = _parse_unit_by_section(text)
    for required in _SERVICE_REQUIRED_KEYS:
        if required not in fields:
            raise SupervisorAdapterError("unit_missing_field", required)
    env_file = fields.get("EnvironmentFile")
    if env_file and env_file.startswith("-"):
        env_file = env_file[1:]
    return LinuxUnitContract(
        unit_name=LINUX_UNIT_NAME,
        kill_mode=fields["KillMode"].strip(),
        working_directory=fields["WorkingDirectory"].strip(),
        exec_start=fields["ExecStart"].strip(),
        environment_file=env_file.strip() if env_file else None,
    )


def validate_linux_unit_contract(
    text: str,
    *,
    current_dir: str | Path,
    program_arguments: Sequence[str],
    deploy_root: str | Path | None = None,
) -> LinuxUnitContract:
    """KillMode=process；WD 与 ExecStart argv 精确对齐调用方 launcher，同源 current。"""
    current = validate_current_path(current_dir, deploy_root=deploy_root)
    expected = normalize_launcher_argv(
        program_arguments, current_dir=current, deploy_root=deploy_root
    )
    cur = current.as_posix()
    contract = parse_linux_unit_contract(text)
    if contract.kill_mode.lower() != "process":
        raise SupervisorAdapterError("killmode_not_process", contract.kill_mode)
    wd = _unescape_systemd_value(contract.working_directory)
    if wd != cur:
        raise SupervisorAdapterError("working_directory_mismatch")
    actual = _parse_exec_start_argv(contract.exec_start)
    if actual != expected:
        raise SupervisorAdapterError("exec_start_argv_mismatch")
    if not (actual[0] == cur or actual[0].startswith(cur + "/")):
        raise SupervisorAdapterError("launcher_outside_current", actual[0])
    if contract.environment_file is not None:
        ef = _unescape_systemd_value(contract.environment_file)
        if ef != f"{cur}/.env":
            raise SupervisorAdapterError("environment_file_mismatch")
    return contract


def _unescape_systemd_value(value: str) -> str:
    # 逆序于 escape：%% → %；\" → "；\\ → \
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "%" and i + 1 < len(value) and value[i + 1] == "%":
            out.append("%")
            i += 2
        elif value[i] == "\\" and i + 1 < len(value):
            out.append(value[i + 1])
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def _unescape_systemd_exec_value(value: str) -> str:
    # 先 undo $$ → $，再 undo systemd value
    tmp: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "$" and i + 1 < len(value) and value[i + 1] == "$":
            tmp.append("$")
            i += 2
        else:
            tmp.append(value[i])
            i += 1
    return _unescape_systemd_value("".join(tmp))


def parse_mac_plist_contract(text: str) -> MacPlistContract:
    if "<!ENTITY" in text.upper() or "SYSTEM" in text and "ENTITY" in text.upper():
        raise SupervisorAdapterError("plist_entity_forbidden")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SupervisorAdapterError("plist_xml_invalid", str(exc)[:80]) from exc
    if root.tag != "plist":
        raise SupervisorAdapterError("plist_root_invalid", root.tag)
    # 找顶层 dict
    dict_el = root.find("dict")
    if dict_el is None:
        raise SupervisorAdapterError("plist_missing_dict")
    mapping = _plist_dict_to_map(dict_el, top_level=True)
    label = mapping.get("Label")
    if not isinstance(label, str):
        raise SupervisorAdapterError("plist_missing_label")
    args = mapping.get("ProgramArguments")
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        raise SupervisorAdapterError("plist_missing_program_arguments")
    wd = mapping.get("WorkingDirectory")
    if not isinstance(wd, str):
        raise SupervisorAdapterError("plist_missing_working_directory")
    return MacPlistContract(
        label=label,
        program_arguments=tuple(args),
        working_directory=wd,
        run_at_load=bool(mapping.get("RunAtLoad")),
        keep_alive=bool(mapping.get("KeepAlive")),
    )


def _plist_dict_to_map(
    dict_el: ET.Element, *, top_level: bool = False
) -> dict[str, object]:
    """最小 plist dict 解析（string / array / true / false / integer）。

    顶层 dict 执行精确 key allowlist，并显式拒绝 ``Program``。
    """
    out: dict[str, object] = {}
    children = list(dict_el)
    i = 0
    while i < len(children):
        el = children[i]
        if el.tag != "key":
            raise SupervisorAdapterError("plist_dict_malformed")
        key = el.text or ""
        if i + 1 >= len(children):
            raise SupervisorAdapterError("plist_dict_malformed")
        val_el = children[i + 1]
        if top_level:
            if key in MAC_PLIST_FORBIDDEN_KEYS or key == "Program":
                raise SupervisorAdapterError("plist_program_key_forbidden", key)
            if key not in MAC_PLIST_ALLOWED_KEYS:
                raise SupervisorAdapterError("plist_key_not_allowlisted", key)
            if key in out:
                raise SupervisorAdapterError("plist_duplicate_key", key)
        out[key] = _plist_value(val_el)
        i += 2
    return out


def _plist_value(el: ET.Element) -> object:
    if el.tag == "string":
        return el.text or ""
    if el.tag == "true":
        return True
    if el.tag == "false":
        return False
    if el.tag == "integer":
        return int(el.text or "0")
    if el.tag == "array":
        return [_plist_value(c) for c in list(el)]
    if el.tag == "dict":
        return _plist_dict_to_map(el)
    raise SupervisorAdapterError("plist_value_not_allowlisted", el.tag)


def validate_mac_plist_contract(
    text: str,
    *,
    expected_label: str,
    expected_working_directory: str | Path,
    program_arguments: Sequence[str] | None = None,
    current_dir: str | Path | None = None,
    deploy_root: str | Path | None = None,
) -> MacPlistContract:
    """校验固定 label、WD，以及（若提供）精确 program argv。

    主 LA（``MAC_MAIN_LABEL``）必须提供 ``program_arguments`` 与 ``current_dir``，
    以验证 argv[0] 落在 current 字面路径下。controller 仅要求 argv 非空且路径合法。
    """
    assert_fixed_mac_label(expected_label)
    wd = validate_absolute_path(expected_working_directory, role="working_directory")
    contract = parse_mac_plist_contract(text)
    assert_fixed_mac_label(contract.label)
    if contract.label != expected_label:
        raise SupervisorAdapterError("label_mismatch", contract.label)
    if contract.working_directory != wd.as_posix():
        raise SupervisorAdapterError("working_directory_mismatch")
    for arg in contract.program_arguments:
        if _has_control_chars(arg):
            raise SupervisorAdapterError("control_char_in_argument")
        if arg.startswith("/"):
            validate_absolute_path(arg, role="program_arg")
    if expected_label == MAC_MAIN_LABEL:
        if program_arguments is None or current_dir is None:
            raise SupervisorAdapterError("main_launcher_required")
        expected = normalize_launcher_argv(
            program_arguments, current_dir=current_dir, deploy_root=deploy_root
        )
        if contract.program_arguments != expected:
            raise SupervisorAdapterError("program_arguments_mismatch")
    elif program_arguments is not None:
        if tuple(program_arguments) != contract.program_arguments:
            raise SupervisorAdapterError("program_arguments_mismatch")
    return contract


# ── 命令计划（不执行） ────────────────────────────────────────────


def plan_linux_install_unit(
    *,
    unit_path: str | Path,
    unit_name: str = LINUX_UNIT_NAME,
) -> list[PlannedCommand]:
    """写出 unit 后的 allowlisted systemctl 计划（本函数不执行）。

    ``unit_path`` 仅校验为绝对安全路径，计划本身不包含写文件步骤——
    字节落盘由上层 controller 完成后再执行本计划。
    """
    validate_absolute_path(unit_path, role="unit_path")
    if unit_name != LINUX_UNIT_NAME:
        raise SupervisorAdapterError("unit_name_not_allowlisted", unit_name)
    return [
        PlannedCommand(
            ("systemctl", "--user", "daemon-reload"),
            "reload_user_units",
        ),
        PlannedCommand(
            ("systemctl", "--user", "enable", "--now", unit_name),
            "enable_and_start_cockpit",
        ),
    ]


def plan_linux_restart(*, unit_name: str = LINUX_UNIT_NAME) -> list[PlannedCommand]:
    if unit_name != LINUX_UNIT_NAME:
        raise SupervisorAdapterError("unit_name_not_allowlisted", unit_name)
    return [
        PlannedCommand(
            ("systemctl", "--user", "restart", unit_name),
            "restart_cockpit_killmode_process",
        ),
    ]


def plan_mac_bootstrap(
    *,
    label: str,
    plist_path: str | Path,
    uid: int,
) -> list[PlannedCommand]:
    """固定 label 的 bootstrap/kickstart 计划；拒绝 upgrade.<job>。"""
    assert_fixed_mac_label(label)
    path = validate_absolute_path(plist_path, role="plist_path")
    if uid < 0:
        raise SupervisorAdapterError("invalid_uid")
    domain = f"gui/{uid}"
    service = f"{domain}/{label}"
    return [
        PlannedCommand(
            ("launchctl", "bootout", service),
            "bootout_existing_optional",
        ),
        PlannedCommand(
            ("launchctl", "bootstrap", domain, path.as_posix()),
            "bootstrap_fixed_launch_agent",
        ),
        PlannedCommand(
            ("launchctl", "enable", service),
            "enable_launch_agent",
        ),
        PlannedCommand(
            ("launchctl", "kickstart", "-k", service),
            "kickstart_launch_agent",
        ),
    ]


def plan_mac_bootout(*, label: str, uid: int) -> list[PlannedCommand]:
    assert_fixed_mac_label(label)
    if uid < 0:
        raise SupervisorAdapterError("invalid_uid")
    domain = f"gui/{uid}"
    service = f"{domain}/{label}"
    return [
        PlannedCommand(
            ("launchctl", "bootout", service),
            "bootout_fixed_launch_agent",
        ),
    ]


def planned_argv_tools(commands: Iterable[PlannedCommand]) -> frozenset[str]:
    """测试辅助：计划中出现的工具名集合（``#`` 元数据除外）。"""
    tools: set[str] = set()
    for cmd in commands:
        if cmd.argv and cmd.argv[0] != "#":
            tools.add(cmd.argv[0])
    return frozenset(tools)


# 故意不提供 execute_* / run_* / apply_*：S0 纯适配层禁止触达 supervisor。


__all__ = [
    "FIXED_MAC_LABELS",
    "LINUX_UNIT_NAME",
    "MAC_CONTROLLER_LABEL",
    "MAC_MAIN_LABEL",
    "MAC_PLIST_ALLOWED_KEYS",
    "MAC_PLIST_FORBIDDEN_KEYS",
    "LinuxUnitContract",
    "MacPlistContract",
    "PlannedCommand",
    "SupervisorAdapterError",
    "assert_fixed_mac_label",
    "escape_plist_xml",
    "escape_systemd_exec_value",
    "escape_systemd_value",
    "is_forbidden_mac_label",
    "normalize_launcher_argv",
    "parse_linux_unit_contract",
    "parse_mac_plist_contract",
    "plan_linux_install_unit",
    "plan_linux_restart",
    "plan_mac_bootout",
    "plan_mac_bootstrap",
    "planned_argv_tools",
    "render_linux_unit",
    "render_mac_controller_plist",
    "render_mac_main_plist",
    "validate_absolute_path",
    "validate_controller_path",
    "validate_current_path",
    "validate_linux_unit_contract",
    "validate_mac_plist_contract",
]
