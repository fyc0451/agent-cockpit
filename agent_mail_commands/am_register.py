#!/usr/bin/env python3
"""注册并持久化一个 Agent Mail 项目-agent-实例身份。"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

from .common import REGISTRY_DIR, load_client_config, mcp_call, mcp_tool, slugify

# 用于 registry 文件名的 agent/instance 组件只允许安全字符,
# 禁止 /、\、..、空格等,防止路径穿越或文件名注入。
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_OPAQUE_INSTANCE_RE = re.compile(r"^i-[a-z2-7]{26}$")


def _validate_component(name: str, label: str) -> str:
    if not _COMPONENT_RE.fullmatch(name):
        raise SystemExit(
            f"{label} 仅允许字母、数字、下划线、点和连字符"
            f"(1-64 字符,不能以标点开头): {name!r}"
        )
    return name


def _pending_path(registry_file: Path) -> Path:
    return registry_file.with_name(registry_file.name + ".pending")


def _secure_read_identity(path: Path, label: str = "身份文件") -> dict:
    """读取前安全门：拒绝 symlink/非普通文件/非当前 uid/group-other 权限。"""
    try:
        st = path.lstat()
    except OSError as exc:
        raise SystemExit(f"{label}不可读: {path}（capability 值不会输出）") from exc
    if stat.S_ISLNK(st.st_mode):
        raise SystemExit(f"拒绝 symlink {label}: {path}（capability 值不会输出）")
    if not stat.S_ISREG(st.st_mode):
        raise SystemExit(f"{label}不是普通文件: {path}（capability 值不会输出）")
    if st.st_uid != os.getuid():
        raise SystemExit(f"{label}属主不是当前用户: {path}（capability 值不会输出）")
    if st.st_mode & 0o077:
        raise SystemExit(f"{label}权限过宽（要求 0600）: {path}（capability 值不会输出）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"{label}损坏不可读: {path}（capability 值不会输出）") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{label}格式错误: {path}")
    return data


def _lock_path(registry_file: Path) -> Path:
    return registry_file.with_name(registry_file.name + ".lock")


def _fsync_dir(path: Path) -> None:
    """fsync 目录；失败向上抛（fail-closed），由调用方中止成功宣告。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_identity(path: Path, identity: dict) -> None:
    """原子写身份文件:mkstemp + fsync + chmod 0600 + os.replace + 目录 fsync。

    不先写目标再 chmod(中间态可能 0644 暴露 registration_token),也避免
    写一半中断留下损坏文件;replace 保证读者永远看到完整内容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".am-register.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(identity, ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _unlink_pending(pending: Path) -> None:
    """删除 pending 并 fsync 目录；失败向上抛（fail-closed）。"""
    pending.unlink()
    _fsync_dir(pending.parent)


class _RegistryLock:
    """同 registry 独占锁（flock LOCK_EX|LOCK_NB）：防止互删/覆盖同一 pending。"""

    def __init__(self, registry_file: Path):
        self._path = _lock_path(registry_file)
        self._fh = None

    def __enter__(self) -> "_RegistryLock":
        import fcntl

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "w")
        try:
            os.fchmod(self._fh.fileno(), 0o600)
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise SystemExit(
                "另一轮换/恢复进程正在进行，已拒绝并发操作"
                f"（锁文件: {self._path}；capability 值不会输出）"
            ) from exc
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _capability_valid(hub: str, token: str, project_key: str, name: str, value: str) -> bool:
    """探测某 capability 当前是否有效；不输出值。"""
    try:
        profile = mcp_tool(hub, token, "whois", {
            "project_key": project_key,
            "agent_name": name,
            "registration_token": value,
            "include_recent_commits": False,
        })
    except SystemExit:
        return False
    return isinstance(profile, dict) and profile.get("name") == name


# pending 中除 capability 外必须与正式 registry 一致的不可变字段
_PENDING_IMMUTABLE_KEYS = (
    "project_key", "project_slug", "agent", "instance", "name",
    "program", "model", "hub",
)


def _load_pending(registry_file: Path, identity: dict) -> dict:
    """读取并强校验 pending：安全门 + 格式合法 + 不可变字段与正式 registry 一致。"""
    pending = _pending_path(registry_file)
    if not pending.is_file():
        raise SystemExit("pending 不存在")
    pending_identity = _secure_read_identity(pending, label="pending 文件")
    if not pending_identity.get("registration_token"):
        raise SystemExit(
            f"pending 文件格式无效: {pending}（capability 值不会输出；请人工处理）"
        )
    for key in _PENDING_IMMUTABLE_KEYS:
        if pending_identity.get(key) != identity.get(key):
            raise SystemExit(
                f"pending 与正式 registry 身份不一致（字段 {key}），"
                f"拒绝恢复外来/陈旧 pending（capability 值不会输出）"
            )
    return pending_identity


def _recover_pending(
    registry_file: Path, identity: dict, hub: str, token: str
) -> str:
    """收敛未完成轮换：探测新旧哪个有效并安全 promote/rollback。幂等。"""
    pending = _pending_path(registry_file)
    if not pending.is_file():
        return "no_pending"
    pending_identity = _load_pending(registry_file, identity)
    old_ok = _capability_valid(
        hub, token, identity["project_key"], identity["name"],
        identity["registration_token"],
    )
    new_ok = _capability_valid(
        hub, token, pending_identity["project_key"], pending_identity["name"],
        pending_identity["registration_token"],
    )
    if new_ok:
        # Hub 已轮换（或新值已生效）：promote。old 同时有效也不危险——
        # 新值对同一身份可用，正式 registry 指向新值即可收敛。
        try:
            _atomic_write_identity(registry_file, pending_identity)
        except OSError as exc:
            raise SystemExit(
                "恢复失败：promote 落盘失败，pending 已保留"
                "（capability 值不会输出）"
            ) from exc
        try:
            _unlink_pending(pending)
        except OSError as exc:
            raise SystemExit(
                "恢复失败：promote 已生效但收敛失败，registry 已更新；"
                "pending 保留，重试 --recover 可幂等收敛（capability 值不会输出）"
            ) from exc
        return "promoted"
    if old_ok:
        # Hub 从未轮换：回滚，丢弃 pending
        try:
            _unlink_pending(pending)
        except OSError as exc:
            raise SystemExit(
                "恢复失败：rollback 收敛失败，registry 未变，pending 保留"
                "（capability 值不会输出）"
            ) from exc
        return "rolled_back"
    raise SystemExit(
        "恢复失败：新旧 capability 均无效或 Hub 不可达；"
        "registry 与 pending 均保留（capability 值不会输出）"
    )


def _settle_uncertain(
    registry_file: Path, identity: dict, hub: str, token: str
) -> None:
    """Hub 结果不确定（响应丢失/超时/非精确成功）时探测收敛；不输出值。"""
    try:
        outcome = _recover_pending(registry_file, identity, hub, token)
    except SystemExit as exc:
        raise SystemExit(
            "轮换结果不确定：pending 已保留，请运行 --recover 收敛"
            "（capability 值不会输出）"
        ) from exc
    if outcome == "promoted":
        print("注意：Hub 已生效但响应不确定，本地已收敛为新值", file=sys.stderr)
    elif outcome == "rolled_back":
        print("注意：Hub 未接受轮换（旧值仍有效），已回滚", file=sys.stderr)


def _rotate_capability(registry_file: Path, identity: dict, hub: str, token: str) -> None:
    """原位轮换：pending 两阶段协议 + 独占锁，任何失败均可恢复且不输出值。"""
    with _RegistryLock(registry_file):
        if _pending_path(registry_file).is_file():
            outcome = _recover_pending(registry_file, identity, hub, token)
            if outcome == "promoted":
                print("注意：检测到已生效但未落盘的轮换，已自动完成本地收敛", file=sys.stderr)
            elif outcome == "rolled_back":
                print("注意：检测到未生效的轮换残留，已自动回滚", file=sys.stderr)
            # 恢复后重载 registry：promote 后旧值已失效，必须用新值继续
            identity = _secure_read_identity(registry_file)

        new_value = secrets.token_urlsafe(32)
        if new_value == identity["registration_token"]:
            new_value = secrets.token_urlsafe(32)
        pending_identity = dict(identity)
        pending_identity["registration_token"] = new_value

        # 阶段 1：先落本地 pending（0600+fsync），结果不确定时本地可安全恢复
        try:
            _atomic_write_identity(_pending_path(registry_file), pending_identity)
        except OSError as exc:
            raise SystemExit(
                "轮换失败：本地 pending 写入失败，未触碰 Hub"
                "（capability 值不会输出）"
            ) from exc
        try:
            result = mcp_tool(hub, token, "rotate_agent_capability", {
                "project_key": identity["project_key"],
                "agent_name": identity["name"],
                "old_registration_token": identity["registration_token"],
                "new_registration_token": new_value,
            })
        except SystemExit:
            # 可能 Hub 已提交但响应超时/解析失败：结果不确定，先探测再定论
            _settle_uncertain(registry_file, identity, hub, token)
            return
        if not (isinstance(result, dict) and result.get("status") == "rotated"):
            _settle_uncertain(registry_file, identity, hub, token)
            return

        # 阶段 2：Hub 已生效，正式落盘 registry；rename/fsync 失败保留 pending 供 --recover
        try:
            _atomic_write_identity(registry_file, pending_identity)
        except OSError:
            raise SystemExit(
                "轮换已在 Hub 生效，但本地正式写入失败；"
                "pending 已保留，请运行 --recover 收敛（capability 值不会输出）"
            )
        try:
            _unlink_pending(_pending_path(registry_file))
        except OSError:
            raise SystemExit(
                "轮换已在 Hub 生效并落盘，但本地收敛失败；"
                "pending 已保留，请运行 --recover 收敛（capability 值不会输出）"
            )
        print(f"轮换成功: {identity['name']}  @ {identity['project_key']}（旧 capability 已失效）")
        print(f"registry: {registry_file}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--instance", default="default")
    parser.add_argument("--project", default=str(Path.cwd()))
    parser.add_argument("--program", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--task", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--show", action="store_true")
    rotate_group = parser.add_mutually_exclusive_group()
    rotate_group.add_argument("--rotate", action="store_true", help="原位安全轮换本地 registry 的 registration capability")
    rotate_group.add_argument("--recover", action="store_true", help="收敛未完成的轮换（探测新旧值并 promote/rollback）")
    args = parser.parse_args(argv)

    agent = _validate_component(args.agent, "agent")
    instance = _validate_component(args.instance, "instance")
    project_key = str(Path(args.project).resolve())
    registry_file = REGISTRY_DIR / slugify(project_key) / f"{agent}--{instance}.json"

    if (args.rotate or args.recover) and (args.force or args.show):
        raise SystemExit("--rotate/--recover 不能与 --force/--show 混用")

    identity = {}
    if registry_file.is_file():
        identity = _secure_read_identity(registry_file)
        if identity.get("project_key") != project_key:
            raise SystemExit(
                f"本地身份项目不匹配: {identity.get('project_key')!r} != {project_key!r}"
            )
        if args.show:
            safe = {key: value for key, value in identity.items() if key != "registration_token"}
            print(json.dumps(safe, ensure_ascii=False, indent=2))
            return
        if identity.get("status") == "retired" or identity.get("retired_at"):
            raise SystemExit(
                "该 instance 已退休，禁止恢复或复用；请创建新的 opaque instance id"
            )
        if args.force and _OPAQUE_INSTANCE_RE.fullmatch(instance):
            raise SystemExit(
                "opaque instance 已存在，--force 也不能覆盖；请创建新的 instance id"
            )
    elif args.show:
        raise SystemExit(f"尚未注册: {agent}--{instance} @ {project_key}")

    if args.rotate or args.recover:
        if not registry_file.is_file():
            raise SystemExit(f"尚未注册，无法轮换: {registry_file}")
        hub, token = load_client_config()
        if not token:
            raise SystemExit("缺少 token，请配置 ~/.agent-mail/client.env（hub=/token=）")
        if args.rotate:
            _rotate_capability(registry_file, identity, hub, token)
            return
        with _RegistryLock(registry_file):
            outcome = _recover_pending(registry_file, identity, hub, token)
        if outcome == "no_pending":
            print("无待恢复的 pending，无需操作")
        else:
            print(
                f"恢复完成: {identity['name']}  @ {identity['project_key']}"
                f"（{'promote：新值已生效' if outcome == 'promoted' else 'rollback：新值未生效'}）"
            )
        return

    hub, token = load_client_config()
    if not token:
        raise SystemExit("缺少 token，请配置 ~/.agent-mail/client.env（hub=/token=）")
    mcp_call(hub, token, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "am-register", "version": "1.0"},
    })

    if identity and not args.force:
        try:
            profile = mcp_tool(hub, token, "whois", {
                "project_key": project_key,
                "agent_name": identity["name"],
                "registration_token": identity["registration_token"],
                "include_recent_commits": False,
            })
            if not isinstance(profile, dict):
                raise SystemExit("whois 返回格式无效，无法确认身份状态")
            if profile.get("status") == "retired" or profile.get("retired_at"):
                raise SystemExit(
                    "Hub 身份已退休，禁止恢复或复用；请创建新的 opaque instance id"
                )
            probe = mcp_tool(hub, token, "fetch_inbox", {
                "project_key": project_key,
                "agent_name": identity["name"],
                "registration_token": identity["registration_token"],
                "limit": 1,
            })
        except SystemExit as exc:
            raise SystemExit(
                "已有身份无效且无法自动恢复；不会覆盖本地 registry。"
                f"如需重新注册，请显式追加 --force。\n原始错误: {exc}"
            ) from exc
        if identity.get("status") != "active":
            identity["status"] = "active"
            _atomic_write_identity(registry_file, identity)
        print(f"已注册（复用）: {identity['name']}  @ {project_key}")
        print(f"inbox: {len(probe) if isinstance(probe, list) else 'ok'}")
        print(f"registry: {registry_file}")
        return

    project = mcp_tool(hub, token, "ensure_project", {"human_key": project_key})
    registration = {
        "project_key": project_key,
        "program": args.program or agent,
        "model": args.model or "unknown",
        "task_description": args.task,
    }
    if args.name:
        registration["name"] = args.name
    registered = mcp_tool(hub, token, "register_agent", registration)
    if args.name and registered["name"] != args.name:
        print(f"警告：服务端未接受名字 {args.name!r}，实际分配 {registered['name']!r}")
    try:
        mcp_tool(hub, token, "set_contact_policy", {
            "project_key": project_key,
            "agent_name": registered["name"],
            "policy": "open",
            "registration_token": registered["registration_token"],
        })
    except SystemExit as exc:
        print(f"警告：身份已注册，但联系人策略设置失败: {exc}")
    identity = {
        "project_key": project_key,
        "project_slug": project.get("slug", slugify(project_key)),
        "agent": agent,
        "instance": instance,
        "name": registered["name"],
        "registration_token": registered["registration_token"],
        "program": registered.get("program"),
        "model": registered.get("model"),
        "hub": hub,
        "status": "active",
    }
    _atomic_write_identity(registry_file, identity)
    print(f"注册成功: {registered['name']}  ({agent}--{instance} @ {project_key})")
    print(f"身份已保存: {registry_file}")


if __name__ == "__main__":
    main()
