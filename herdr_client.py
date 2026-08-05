"""herdr_client.py — herdr CLI subprocess 封装(多 session 聚合)。

herdr 以多个 session 运行,每个 session 有独立 socket。本模块遍历所有 session,
聚合 pane 状态,这是"每个 agent 都可视化"的数据源。

关键修正(对比旧版):不再只查 default socket,而是 herdr session list 枚举所有 session,
逐个 --session <name> 取 snapshot 聚合。
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

# herdr 二进制:优先用环境变量,其次 PATH 探测,最后试 ~/.local/bin
_HERDR_ENV = os.environ.get("HERDR_BIN")
HERDR_BIN = _HERDR_ENV or shutil.which("herdr") or str(Path.home() / ".local" / "bin" / "herdr")
# herdr 所在的额外 PATH(供子进程找到它)
_HERDR_DIR = str(Path(HERDR_BIN).parent) if HERDR_BIN else ""
PANE_CREATE_TIMEOUT = 3.0
AGENT_START_TIMEOUT = 10.0
AGENT_STABLE_SECONDS = 1.5
AGENT_POLL_INTERVAL = 0.2


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
def _agent_cmd(agent: str, workdir: str) -> str:
    """构造 agent 启动命令(完整路径 + shlex 安全)。"""
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
    return builder(bin_path)


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


def _run(args: list[str], timeout: int = 10) -> str:
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


def list_sessions() -> list[dict[str, Any]]:
    """枚举所有 herdr session。返回 [{name, status, directory, socket}]。"""
    if not is_available():
        return []
    try:
        out = _run(["session", "list", "--json"], timeout=8)
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
            out = _run(["session", "list"], timeout=8)
        except RuntimeError:
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


def _snapshot_session(session: str) -> dict[str, Any]:
    """取单个 session 的 snapshot,返回精简后的 {panes, agents}。"""
    try:
        out = _run(["api", "snapshot", "--session", session], timeout=8)
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


def snapshot() -> dict[str, Any]:
    """聚合所有 running session 的 pane,这是 agent 全景视图。"""
    if not is_available():
        return {"available": False, "sessions": [], "panes": [], "agents": []}
    sessions = list_sessions()
    running = [s for s in sessions if s.get("status") == "running"]
    results = []
    all_panes = []
    for s in running:
        snap = _snapshot_session(s["name"])
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
    正确语法统一为 `herdr --session <s> <subcmd> <pane_id> ...`(session 全局前置)。
    """
    if not is_available():
        return {"available": False}
    try:
        if mode == "prompt":
            _run(["--session", session, "agent", "prompt", pane_id, text], timeout=10)
        elif mode == "send":
            # send-text 发文本,再 send-keys 发回车执行
            _run(["--session", session, "pane", "send-text", pane_id, text], timeout=5)
            _run(["--session", session, "pane", "send-keys", pane_id, "Enter"], timeout=5)
        else:  # keys
            keys = text.split()
            _run(["--session", session, "pane", "send-keys", pane_id] + keys, timeout=5)
        return {"available": True, "sent": text, "mode": mode}
    except RuntimeError as e:
        return {"available": True, "error": str(e)}


def _rename_agent_context(
    session: str, pane: dict[str, Any], agent: str, layout: str,
) -> None:
    """把 Herdr 实际展示的 workspace/tab/pane 都改成可辨认名称。"""
    pane_id = pane.get("pane_id")
    tab_id = pane.get("tab_id")
    workspace_id = pane.get("workspace_id")
    commands = []
    if pane_id:
        commands.append(["--session", session, "pane", "rename", pane_id, agent])
    if tab_id:
        tab_label = agent if layout == "tab" else session
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
    layout: str = "tab",
) -> dict[str, Any]:
    """在指定 session 里启动一个 agent pane(新建 window/pane 跑 agent)。

    agent: codex | kimi | qodercli
    返回新 pane 信息(尽力而为,herdr 版本不同命令可能略异)。
    """
    if not is_available():
        return {"available": False}
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

    existing = next(
        (
            p for p in snap.get("panes", [])
            if p.get("agent") == agent and matching_cwd(p)
        ),
        None,
    )
    if existing:
        _rename_agent_context(session, existing, agent, layout)
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
        return result
    agent_bin = _find_agent_bin(agent)
    if not (Path(agent_bin).is_file() and os.access(agent_bin, os.X_OK)):
        return {"available": True, "error": f"{agent} 未安装或不在 PATH"}
    # 构造 agent 启动命令(用 _agent_cmd 统一处理完整路径)
    cmd_str = _agent_cmd(agent, workdir)
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

        # 无论 Herdr 是否返回 id，都用前后 snapshot 验证它确实是本次新增 pane。
        deadline = time.monotonic() + PANE_CREATE_TIMEOUT
        while time.monotonic() < deadline:
            after = _snapshot_session(session)
            after_ids = {
                str(p.get("pane_id"))
                for p in after.get("panes", [])
                if p.get("pane_id")
            }
            created_ids = after_ids - before_ids
            if reported_pid and str(reported_pid) in created_ids:
                new_pid = str(reported_pid)
                break
            if not reported_pid and len(created_ids) == 1:
                new_pid = created_ids.pop()
                break
            if not reported_pid and len(created_ids) > 1:
                raise RuntimeError("创建 pane 时同时出现多个新 pane，无法安全识别")
            time.sleep(AGENT_POLL_INTERVAL)
        if not new_pid:
            raise RuntimeError("split/tab 后找不到本次创建的新 pane")
        # pane run 接收完整命令字符串；拆分后交给 Herdr 重组会破坏引号，并让路径
        # 中的 shell 元字符在下一层被重新解释。
        _run(["--session", session, "pane", "run", new_pid, cmd_str], timeout=8)

        # pane run 成功只代表命令已发出。等 Herdr 识别到 agent，再经过稳定窗口
        # 复查，捕获 OpenCode/Bun 这类启动后立即崩溃、只留下空 pane 的情况。
        saw_agent = False
        confirmed_pane = None
        deadline = time.monotonic() + AGENT_START_TIMEOUT
        while time.monotonic() < deadline:
            current = next(
                (
                    p for p in _snapshot_session(session).get("panes", [])
                    if str(p.get("pane_id")) == new_pid
                ),
                None,
            )
            if current and current.get("agent") == agent:
                saw_agent = True
                time.sleep(AGENT_STABLE_SECONDS)
                confirmed_pane = next(
                    (
                        p for p in _snapshot_session(session).get("panes", [])
                        if str(p.get("pane_id")) == new_pid
                    ),
                    None,
                )
                if confirmed_pane and confirmed_pane.get("agent") == agent:
                    # pane 命名成 agent 名，tab/workspace 也改成可辨认名称
                    # (默认是序号,看板/TUI 里分不清);失败不影响启动
                    _rename_agent_context(
                        session, confirmed_pane, agent, effective_layout
                    )
                    return {
                        "available": True,
                        "pane_id": new_pid,
                        "agent": agent,
                        "cmd": cmd_str,
                        "layout": effective_layout,
                    }
                break
            time.sleep(AGENT_POLL_INTERVAL)
        if saw_agent:
            raise RuntimeError(f"{agent} 启动后未能保持运行")
        raise RuntimeError(f"{agent} 启动超时，Herdr 未识别到运行中的 agent")
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
        return {"available": True, "closed": pane_id}
    except RuntimeError as e:
        return {"available": True, "error": str(e)}


def restart_pane(
    session: str, pane_id: str, agent: str | None = None,
    workdir: str | None = None, resume: bool = False,
) -> dict[str, Any]:
    """重启 pane 里的 agent(Ctrl+C 退出 + 重新启动)。

    场景:agent 卡死 / thread 损坏 / 想用新 PATH。
    resume=True 时尝试 codex resume --last 恢复历史会话。
    """
    if not is_available():
        return {"available": False}
    try:
        # 1. 在发送退出按键前确认 pane 和 agent，避免检测丢失后误启 Codex。
        snap = _snapshot_session(session)
        p = next(
            (x for x in snap.get("panes", []) if x.get("pane_id") == pane_id),
            None,
        )
        if p is None:
            return {"available": True, "error": f"找不到 pane: {pane_id}"}
        previous_agent = p.get("agent")
        agent = agent or previous_agent
        if not agent:
            return {
                "available": True,
                "error": f"无法识别 pane {pane_id} 的 agent，已取消重启",
            }
        if agent not in {
            "codex", "kimi", "claude", "qoder", "qodercli", "qodercn", "grok", "opencode",
        }:
            return {"available": True, "error": f"不支持的 agent: {agent}"}
        workdir = workdir or p.get("cwd") or str(Path.home())

        # 2. 先 Esc(取消任何 TUI 子模式/输入),再 Ctrl+C 退出 agent
        _run(["--session", session, "pane", "send-keys", pane_id, "Escape"], timeout=3)
        time.sleep(0.5)
        _run(["--session", session, "pane", "send-keys", pane_id, "C-c"], timeout=3)
        time.sleep(1.5)
        # 再发一次确保退到 shell(agent 可能需要两次 C-c)
        _run(["--session", session, "pane", "send-keys", pane_id, "C-c"], timeout=3)
        time.sleep(1)
        # 3. 清空当前输入行(防有残留):Ctrl+U 清行(herdr 可能不支持,失败忽略)
        try:
            _run(["--session", session, "pane", "send-keys", pane_id, "C-u"], timeout=3)
            time.sleep(0.3)
        except RuntimeError:
            pass
        # 4. 构造启动命令(用 _agent_cmd 统一处理所有 agent 类型)
        if agent == "codex" and resume:
            codex_bin = shlex.quote(_find_agent_bin("codex"))
            cmd_str = f'cd {shlex.quote(workdir)} && {codex_bin} resume --last'
        else:
            base = _agent_cmd(agent, workdir)
            cmd_str = f'cd {shlex.quote(workdir)} && {base}'
        # 5. 用 pane run 启动命令(比 send-text+Enter 可靠,不会被 agent TUI 当 prompt)
        # pane run 发命令+回车,语义是"在 pane 里执行命令"
        _run(["--session", session, "pane", "run", pane_id, cmd_str], timeout=8)
        return {
            "available": True, "restarted": True, "pane_id": pane_id,
            "agent": agent, "previous_agent": previous_agent,
            "cmd": cmd_str, "resume": resume,
        }
    except RuntimeError as e:
        return {"available": True, "error": str(e)}


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
        return {"available": True, "deleted": session}
    except RuntimeError as e:
        return {"available": True, "error": str(e)}
