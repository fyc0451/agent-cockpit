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

import os
import re
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence

# ── 固定合同 ──────────────────────────────────────────────────────

LINUX_UNIT_NAME = "agent-cockpit.service"
MAC_MAIN_LABEL = "io.github.fyc0451.agent-cockpit"
MAC_CONTROLLER_LABEL = "io.github.fyc0451.agent-cockpit-controller"
FIXED_SERVER_LAUNCHER_RELATIVE_PATH = "bin/agent-cockpit"
LINUX_EVIDENCE_ENV_NAME = "server-evidence.env"
_SYSTEMD_SAFE_ENVIRONMENT_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")

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
    environment_files: tuple[tuple[str, bool], ...]

    @property
    def environment_file(self) -> str | None:
        """Legacy first EnvironmentFile path."""
        return self.environment_files[0][0] if self.environment_files else None


@dataclass(frozen=True)
class MacPlistContract:
    label: str
    program_arguments: tuple[str, ...]
    working_directory: str
    run_at_load: bool
    keep_alive: bool
    throttle_interval: int
    standard_out_path: str
    standard_error_path: str


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


# 固定 Mac 日志文件名（不可由 caller 改写）
MAC_MAIN_STDOUT_NAME = "agent-cockpit.stdout.log"
MAC_MAIN_STDERR_NAME = "agent-cockpit.stderr.log"
MAC_CONTROLLER_STDOUT_NAME = "controller.stdout.log"
MAC_CONTROLLER_STDERR_NAME = "controller.stderr.log"

# Linux unit [Service] 固定安全字段（精确字符串）
LINUX_SERVICE_FIXED_FIELDS: dict[str, str] = {
    "Type": "simple",
    "KillMode": "process",
    "Restart": "always",
    "RestartSec": "3",
    "Environment": "PYTHONUNBUFFERED=1",
    "NoNewPrivileges": "true",
    "UMask": "0077",
}


def require_canonical_current_and_deploy(
    *,
    current: str | Path,
    deploy_root: str | Path,
) -> tuple[Path, Path]:
    """强制字面 ``{deploy_root}/current`` 与显式 deploy_root 一致；禁止省略/伪造。"""
    root = validate_absolute_path(deploy_root, role="deploy_root")
    cur = validate_current_path(current, deploy_root=root, require_name_current=True)
    expected = Path(root.as_posix() + "/current")
    if cur.as_posix() != expected.as_posix():
        raise SupervisorAdapterError("current_deploy_root_mismatch")
    return root, cur


def derive_deploy_root(
    *,
    current: str | Path | None = None,
    deploy_root: str | Path | None = None,
) -> Path | None:
    """显式 deploy_root 优先；否则由 ``.../current`` 推导（仅辅助，controller 合同不用）。"""
    if deploy_root is not None:
        return validate_absolute_path(deploy_root, role="deploy_root")
    if current is not None:
        cur = validate_absolute_path(current, role="current")
        if cur.name == "current":
            return cur.parent
    return None


def validate_controller_path(
    controller: str | Path,
    *,
    current: str | Path,
    deploy_root: str | Path,
) -> Path:
    """Controller 安装根：绝对路径；必须在 release tree 外。

    **必须**同时提供 canonical ``deploy_root`` 与 ``current``
    （``current`` 字面量必须等于 ``{deploy_root}/current``）。

    - 字面 / resolve 不得落在 ``current`` 或其 active generation 内；
    - 拒全部 ``{deploy_root}/generations/**``（含 inactive generation）；
    - 拒整个 release tree（``deploy_root`` 及其子路径）。
    """
    root, cur = require_canonical_current_and_deploy(
        current=current, deploy_root=deploy_root
    )
    path = validate_absolute_path(controller, role="controller")
    if path == cur or _is_relative_to(path, cur):
        raise SupervisorAdapterError("controller_inside_current")
    try:
        path_res = path.resolve(strict=False)
        cur_res = cur.resolve(strict=False)
        root_res = root.resolve(strict=False)
        gens_res = (root / "generations").resolve(strict=False)
    except OSError as exc:
        raise SupervisorAdapterError("controller_path_unresolvable") from exc
    if path_res == cur_res or _is_relative_to(path_res, cur_res):
        raise SupervisorAdapterError("controller_inside_current")
    gens = root / "generations"
    if (
        path == gens
        or _is_relative_to(path, gens)
        or path_res == gens_res
        or _is_relative_to(path_res, gens_res)
    ):
        raise SupervisorAdapterError("controller_inside_generations")
    if (
        path == root
        or _is_relative_to(path, root)
        or path_res == root_res
        or _is_relative_to(path_res, root_res)
    ):
        raise SupervisorAdapterError("controller_inside_release_tree")
    return path


def normalize_controller_argv(
    program_arguments: Sequence[str],
    *,
    controller_dir: str | Path,
    current: str | Path,
    deploy_root: str | Path,
) -> tuple[str, ...]:
    """controller ProgramArguments：argv[0] 字面+resolve 均须在 controller_dir 内且 release 外。"""
    if not program_arguments:
        raise SupervisorAdapterError("empty_program_arguments")
    controller = validate_controller_path(
        controller_dir, current=current, deploy_root=deploy_root
    )
    root, _cur = require_canonical_current_and_deploy(
        current=current, deploy_root=deploy_root
    )
    ctrl = controller.as_posix()
    out: list[str] = []
    for i, arg in enumerate(program_arguments):
        if not isinstance(arg, str) or not arg:
            raise SupervisorAdapterError("invalid_program_argument", str(i))
        if _has_control_chars(arg):
            raise SupervisorAdapterError("control_char_in_argument", str(i))
        if i == 0:
            launcher = validate_absolute_path(arg, role="controller_launcher")
            literal = launcher.as_posix()
            if not (literal == ctrl or literal.startswith(ctrl + "/")):
                raise SupervisorAdapterError("controller_argv_outside_controller", literal)
            try:
                resolved = launcher.resolve(strict=False)
                ctrl_res = controller.resolve(strict=False)
                root_res = root.resolve(strict=False)
            except OSError as exc:
                raise SupervisorAdapterError("controller_argv_unresolvable") from exc
            if not (resolved == ctrl_res or _is_relative_to(resolved, ctrl_res)):
                raise SupervisorAdapterError("controller_argv_symlink_escape")
            if resolved == root_res or _is_relative_to(resolved, root_res):
                raise SupervisorAdapterError("controller_argv_inside_release_tree")
            out.append(literal)
        else:
            if arg.startswith("/"):
                validate_absolute_path(arg, role=f"program_arg[{i}]")
            out.append(arg)
    return tuple(out)


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


def normalize_fixed_server_launcher_argv(
    program_arguments: Sequence[str],
    *,
    current_dir: str | Path,
    deploy_root: str | Path,
) -> tuple[str, ...]:
    """Require the signed server launcher's one canonical literal argv[0].

    This helper never guesses an interpreter, PATH entry, virtualenv, source
    script, or generation path. Existing broad supervisor adapters keep using
    ``normalize_launcher_argv`` until the upgrade controller is wired.
    """
    root, current = require_canonical_current_and_deploy(
        current=current_dir, deploy_root=deploy_root
    )
    normalized = normalize_launcher_argv(
        program_arguments, current_dir=current, deploy_root=root
    )
    expected = f"{current.as_posix()}/{FIXED_SERVER_LAUNCHER_RELATIVE_PATH}"
    if normalized[0] != expected:
        raise SupervisorAdapterError("fixed_launcher_argv0_mismatch")
    launcher = Path(normalized[0])
    try:
        leaf = launcher.lstat()
    except OSError as exc:
        raise SupervisorAdapterError("fixed_launcher_missing") from exc
    if (
        not stat.S_ISREG(leaf.st_mode)
        or leaf.st_uid != os.getuid()
        or stat.S_IMODE(leaf.st_mode) != 0o700
        or leaf.st_nlink != 1
    ):
        raise SupervisorAdapterError("fixed_launcher_unsafe")
    try:
        resolved = launcher.resolve(strict=True)
        expected_resolved = (
            current.resolve(strict=False) / FIXED_SERVER_LAUNCHER_RELATIVE_PATH
        )
    except (OSError, RuntimeError) as exc:
        raise SupervisorAdapterError("launcher_unresolvable") from exc
    if resolved != expected_resolved:
        raise SupervisorAdapterError("fixed_launcher_resolve_mismatch")
    return normalized


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
    except (OSError, RuntimeError) as exc:
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


def validate_release_external_environment_file(
    value: str | Path,
    *,
    deploy_root: str | Path | None,
) -> Path:
    """Validate the fixed release-external Server evidence selector path."""
    path = validate_absolute_path(value, role="evidence_environment_file")
    if _SYSTEMD_SAFE_ENVIRONMENT_PATH_RE.fullmatch(path.as_posix()) is None:
        raise SupervisorAdapterError("evidence_environment_path_unsupported")
    if path.name != LINUX_EVIDENCE_ENV_NAME:
        raise SupervisorAdapterError("evidence_environment_name_mismatch")
    if deploy_root is None:
        raise SupervisorAdapterError("deploy_root_required")
    root = validate_absolute_path(deploy_root, role="deploy_root")
    if path == root or _is_relative_to(path, root):
        raise SupervisorAdapterError("evidence_environment_inside_release_tree")
    try:
        path_resolved = path.resolve(strict=False)
        root_resolved = root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise SupervisorAdapterError("evidence_environment_unresolvable") from exc
    if path_resolved == root_resolved or _is_relative_to(path_resolved, root_resolved):
        raise SupervisorAdapterError("evidence_environment_inside_release_tree")
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink():
                raise SupervisorAdapterError("evidence_environment_symlink")
    except OSError as exc:
        raise SupervisorAdapterError("evidence_environment_unresolvable") from exc
    return path


# ── 渲染 ──────────────────────────────────────────────────────────


def render_linux_unit(
    *,
    current_dir: str | Path,
    program_arguments: Sequence[str],
    deploy_root: str | Path | None = None,
    evidence_environment_file: str | Path | None = None,
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
    evidence_line = ""
    if evidence_environment_file is not None:
        evidence_path = validate_release_external_environment_file(
            evidence_environment_file,
            deploy_root=deploy_root,
        )
        evidence_line = (
            f"EnvironmentFile={escape_systemd_value(evidence_path.as_posix())}\n"
        )
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
        f"{evidence_line}"
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
    stdout = escape_plist_xml(f"{cur}/{MAC_MAIN_STDOUT_NAME}")
    stderr = escape_plist_xml(f"{cur}/{MAC_MAIN_STDERR_NAME}")
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
    current_dir: str | Path,
    deploy_root: str | Path,
) -> str:
    """固定 controller LaunchAgent（release 外）；禁止 upgrade.<job> label。

    必须同时传 canonical ``current_dir`` + ``deploy_root``；日志名固定不可改。
    """
    controller = validate_controller_path(
        controller_dir, current=current_dir, deploy_root=deploy_root
    )
    argv = normalize_controller_argv(
        program_arguments,
        controller_dir=controller,
        current=current_dir,
        deploy_root=deploy_root,
    )
    wd = escape_plist_xml(controller.as_posix())
    stdout = escape_plist_xml(
        f"{controller.as_posix()}/{MAC_CONTROLLER_STDOUT_NAME}"
    )
    stderr = escape_plist_xml(
        f"{controller.as_posix()}/{MAC_CONTROLLER_STDERR_NAME}"
    )
    label = escape_plist_xml(MAC_CONTROLLER_LABEL)
    args_xml = "\n".join(f"    <string>{escape_plist_xml(a)}</string>" for a in argv)
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


def _parse_unit_by_section(text: str) -> dict[str, object]:
    """按 section 解析 unit；关键键仅在 [Service]；拒绝错位、键重复与 section 重复。"""
    section: str | None = None
    seen_sections: set[str] = set()
    # (section, key) → value；再投影 service 字段
    seen: set[tuple[str, str]] = set()
    service_fields: dict[str, object] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if name not in _UNIT_SECTION_KEYS:
                raise SupervisorAdapterError("unit_section_not_allowlisted", name)
            if name in seen_sections:
                raise SupervisorAdapterError("unit_duplicate_section", name)
            seen_sections.add(name)
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
        if section == "Service" and key == "EnvironmentFile":
            if raw != line or not raw.startswith("EnvironmentFile="):
                raise SupervisorAdapterError("environment_file_noncanonical")
            values = service_fields.setdefault(key, [])
            assert isinstance(values, list)
            if value in values:
                raise SupervisorAdapterError("environment_file_duplicate")
            values.append(value)
            if len(values) > 2:
                raise SupervisorAdapterError("environment_file_count_invalid")
            continue
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
    raw_env_files = fields.get("EnvironmentFile", [])
    if isinstance(raw_env_files, str):
        raw_env_files = [raw_env_files]
    assert isinstance(raw_env_files, list)
    environment_files: list[tuple[str, bool]] = []
    for value in raw_env_files:
        assert isinstance(value, str)
        optional = value.startswith("-")
        environment_files.append((value[1:] if optional else value, optional))
    return LinuxUnitContract(
        unit_name=LINUX_UNIT_NAME,
        kill_mode=str(fields["KillMode"]).strip(),
        working_directory=str(fields["WorkingDirectory"]).strip(),
        exec_start=str(fields["ExecStart"]).strip(),
        environment_files=tuple(environment_files),
    )


def validate_linux_unit_contract(
    text: str,
    *,
    current_dir: str | Path,
    program_arguments: Sequence[str],
    deploy_root: str | Path | None = None,
    evidence_environment_file: str | Path | None = None,
) -> LinuxUnitContract:
    """KillMode=process；固定安全字段精确；WD 与 ExecStart argv 同源 current。"""
    current = validate_current_path(current_dir, deploy_root=deploy_root)
    expected = normalize_launcher_argv(
        program_arguments, current_dir=current, deploy_root=deploy_root
    )
    cur = current.as_posix()
    contract = parse_linux_unit_contract(text)
    fields = _parse_unit_by_section(text)
    for key, want in LINUX_SERVICE_FIXED_FIELDS.items():
        got = str(fields.get(key) or "").strip()
        if got != want:
            raise SupervisorAdapterError("linux_fixed_field_mismatch", f"{key}={got!r}")
    if contract.kill_mode != "process":
        raise SupervisorAdapterError("killmode_not_process", contract.kill_mode)
    wd = _unescape_systemd_value(contract.working_directory)
    if wd != cur:
        raise SupervisorAdapterError("working_directory_mismatch")
    actual = _parse_exec_start_argv(contract.exec_start)
    if actual != expected:
        raise SupervisorAdapterError("exec_start_argv_mismatch")
    if not (actual[0] == cur or actual[0].startswith(cur + "/")):
        raise SupervisorAdapterError("launcher_outside_current", actual[0])
    expected_raw_environment_files = (
        f"-{escape_systemd_value(f'{cur}/.env')}",
    )
    if evidence_environment_file is not None:
        evidence_path = validate_release_external_environment_file(
            evidence_environment_file,
            deploy_root=deploy_root,
        )
        expected_raw_environment_files += (
            escape_systemd_value(evidence_path.as_posix()),
        )
    raw_environment_files = fields.get("EnvironmentFile", [])
    if not isinstance(raw_environment_files, list) or tuple(
        raw_environment_files
    ) != expected_raw_environment_files:
        raise SupervisorAdapterError("environment_file_mismatch")
    actual_environment_files = tuple(
        (_unescape_systemd_value(path), optional)
        for path, optional in contract.environment_files
    )
    expected_environment_files: tuple[tuple[str, bool], ...] = (
        (f"{cur}/.env", True),
    )
    if evidence_environment_file is not None:
        expected_environment_files += ((evidence_path.as_posix(), False),)
    if actual_environment_files != expected_environment_files:
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
    if type(label) is not str:
        raise SupervisorAdapterError("plist_missing_label")
    args = mapping.get("ProgramArguments")
    if not isinstance(args, list) or not args or not all(type(a) is str for a in args):
        raise SupervisorAdapterError("plist_missing_program_arguments")
    wd = mapping.get("WorkingDirectory")
    if type(wd) is not str:
        raise SupervisorAdapterError("plist_missing_working_directory")
    # 禁止 bool() 强转：缺省 / 错类型直接 fail-closed
    run_at_load = mapping.get("RunAtLoad", _MISSING)
    keep_alive = mapping.get("KeepAlive", _MISSING)
    throttle = mapping.get("ThrottleInterval", _MISSING)
    stdout = mapping.get("StandardOutPath", _MISSING)
    stderr = mapping.get("StandardErrorPath", _MISSING)
    if run_at_load is _MISSING or type(run_at_load) is not bool:
        raise SupervisorAdapterError("plist_field_type", "RunAtLoad")
    if keep_alive is _MISSING or type(keep_alive) is not bool:
        raise SupervisorAdapterError("plist_field_type", "KeepAlive")
    if throttle is _MISSING or type(throttle) is not int or isinstance(throttle, bool):
        raise SupervisorAdapterError("plist_field_type", "ThrottleInterval")
    if stdout is _MISSING or type(stdout) is not str:
        raise SupervisorAdapterError("plist_field_type", "StandardOutPath")
    if stderr is _MISSING or type(stderr) is not str:
        raise SupervisorAdapterError("plist_field_type", "StandardErrorPath")
    return MacPlistContract(
        label=label,
        program_arguments=tuple(args),
        working_directory=wd,
        run_at_load=run_at_load,
        keep_alive=keep_alive,
        throttle_interval=throttle,
        standard_out_path=stdout,
        standard_error_path=stderr,
    )


_MISSING = object()


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
    """校验固定 label、WD、exact bool/int 字段与**固定**同源 log 文件名。

    日志名不可由 caller 放宽。主 LA 必须提供 program_arguments+current_dir。
    controller LA **无条件**要求 canonical current_dir+deploy_root，并对 plist
    实际 ProgramArguments 始终执行 normalize_controller_argv（argv[0] 字面+
    resolve 须在 controller_dir 内且 release tree 外）。
    """
    assert_fixed_mac_label(expected_label)
    wd = validate_absolute_path(expected_working_directory, role="working_directory")
    contract = parse_mac_plist_contract(text)
    assert_fixed_mac_label(contract.label)
    if contract.label != expected_label:
        raise SupervisorAdapterError("label_mismatch", contract.label)
    if contract.working_directory != wd.as_posix():
        raise SupervisorAdapterError("working_directory_mismatch")
    if contract.run_at_load is not True:
        raise SupervisorAdapterError("plist_run_at_load_not_true")
    if contract.keep_alive is not True:
        raise SupervisorAdapterError("plist_keep_alive_not_true")
    if type(contract.throttle_interval) is not int or contract.throttle_interval != 3:
        raise SupervisorAdapterError("plist_throttle_interval_not_3")
    if expected_label == MAC_MAIN_LABEL:
        out_name = MAC_MAIN_STDOUT_NAME
        err_name = MAC_MAIN_STDERR_NAME
    else:
        out_name = MAC_CONTROLLER_STDOUT_NAME
        err_name = MAC_CONTROLLER_STDERR_NAME
    expected_out = f"{wd.as_posix()}/{out_name}"
    expected_err = f"{wd.as_posix()}/{err_name}"
    if contract.standard_out_path != expected_out:
        raise SupervisorAdapterError("plist_stdout_path_mismatch")
    if contract.standard_error_path != expected_err:
        raise SupervisorAdapterError("plist_stderr_path_mismatch")
    validate_absolute_path(contract.standard_out_path, role="stdout")
    validate_absolute_path(contract.standard_error_path, role="stderr")
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
    elif expected_label == MAC_CONTROLLER_LABEL:
        # R5：不可因省略 program_arguments 而跳过 controller 边界校验
        if current_dir is None or deploy_root is None:
            raise SupervisorAdapterError("controller_canonical_required")
        normalized = normalize_controller_argv(
            contract.program_arguments,
            controller_dir=wd,
            current=current_dir,
            deploy_root=deploy_root,
        )
        if contract.program_arguments != normalized:
            raise SupervisorAdapterError("program_arguments_mismatch")
        if program_arguments is not None:
            expected = normalize_controller_argv(
                program_arguments,
                controller_dir=wd,
                current=current_dir,
                deploy_root=deploy_root,
            )
            if contract.program_arguments != expected:
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
    "FIXED_SERVER_LAUNCHER_RELATIVE_PATH",
    "LINUX_EVIDENCE_ENV_NAME",
    "LINUX_UNIT_NAME",
    "MAC_CONTROLLER_LABEL",
    "MAC_MAIN_LABEL",
    "MAC_PLIST_ALLOWED_KEYS",
    "MAC_PLIST_FORBIDDEN_KEYS",
    "LinuxUnitContract",
    "MacPlistContract",
    "PlannedCommand",
    "SupervisorAdapterError",
    "LINUX_SERVICE_FIXED_FIELDS",
    "MAC_CONTROLLER_STDERR_NAME",
    "MAC_CONTROLLER_STDOUT_NAME",
    "MAC_MAIN_STDERR_NAME",
    "MAC_MAIN_STDOUT_NAME",
    "assert_fixed_mac_label",
    "derive_deploy_root",
    "escape_plist_xml",
    "escape_systemd_exec_value",
    "escape_systemd_value",
    "is_forbidden_mac_label",
    "normalize_controller_argv",
    "normalize_fixed_server_launcher_argv",
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
    "require_canonical_current_and_deploy",
    "validate_absolute_path",
    "validate_controller_path",
    "validate_current_path",
    "validate_linux_unit_contract",
    "validate_release_external_environment_file",
    "validate_mac_plist_contract",
]
