"""U1b：服务外安全升级核心（状态机 / 互斥 / 预检 / 备份 / 回滚）。

执行器进程与 Cockpit 主进程分离（start_new_session），Cockpit 重启不能杀死升级。
管理员显式触发；只接受官方稳定 Release 的精确 tag/SemVer；失败自动回滚并验证 health。
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

import settings
import version

# ── 路径 ────────────────────────────────────────────────────────

INSTALL_DIR = Path(__file__).resolve().parent
UPGRADE_DIR = settings.DATA_DIR / "upgrade"
STATE_PATH = UPGRADE_DIR / "state.json"
LOCK_PATH = UPGRADE_DIR / "upgrade.lock"
LOG_DIR = UPGRADE_DIR / "logs"
BACKUP_ROOT = UPGRADE_DIR / "backups"
WORKER_SCRIPT = INSTALL_DIR / "cockpit-upgrade-worker.py"

GITHUB_RELEASE_BY_TAG = (
    "https://api.github.com/repos/fyc0451/agent-cockpit/releases/tags/{tag}"
)
GITHUB_REPO = "https://github.com/fyc0451/agent-cockpit.git"

TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "rolled_back", "idle", "rejected"}
)
ACTIVE_STATES = frozenset(
    {
        "queued",
        "prechecking",
        "backing_up",
        "fetching",
        "installing",
        "restarting",
        "verifying",
        "rolling_back",
    }
)

HEALTH_TIMEOUT_S = 60.0
HEALTH_POLL_S = 1.0
DISK_MIN_FREE_BYTES = 200 * 1024 * 1024  # 200 MiB
# 预检允许的未跟踪路径前缀（受管，不阻断）
ALLOWED_DIRTY_PREFIXES = (
    ".venv/",
    ".venv",
    "__pycache__/",
    ".pytest_cache/",
    "dashboard-data/",  # 不应出现在 install tree，防御
)

# 可注入钩子（测试）
SupervisorHooks = dict[str, Callable[..., Any]]
_hooks: SupervisorHooks = {}


def configure_hooks(**hooks: Callable[..., Any]) -> None:
    """测试注入：restart_cockpit / health_check / fetch_release / git_remote。"""
    _hooks.clear()
    _hooks.update(hooks)


def clear_hooks() -> None:
    _hooks.clear()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_dirs() -> None:
    UPGRADE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)


# ── 状态读写 ────────────────────────────────────────────────────

def _default_state() -> dict[str, Any]:
    return {
        "job_id": None,
        "state": "idle",
        "target_version": None,
        "target_tag": None,
        "target_sha": None,
        "from_version": None,
        "from_sha": None,
        "phase": None,
        "error": None,
        "created_at": None,
        "updated_at": _utc_iso(),
        "finished_at": None,
        "worker_pid": None,
        "log_path": None,
        "backup_id": None,
        "diagnostics": {},
    }


def read_state() -> dict[str, Any]:
    _ensure_dirs()
    if not STATE_PATH.is_file():
        return _default_state()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_state()
        base = _default_state()
        base.update(data)
        return base
    except (OSError, ValueError, TypeError):
        return _default_state()


def write_state(state: dict[str, Any]) -> None:
    _ensure_dirs()
    state = dict(state)
    state["updated_at"] = _utc_iso()
    fd, tmp = tempfile.mkstemp(prefix=".upgrade-state.", suffix=".tmp", dir=str(UPGRADE_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, STATE_PATH)
        os.chmod(STATE_PATH, 0o600)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


# ── 互斥锁 ──────────────────────────────────────────────────────

class UpgradeLock:
    """进程间 flock；持有期间不允许第二把锁。"""

    def __init__(self, path: Path | None = None) -> None:
        # 运行时解析 LOCK_PATH，便于测试 monkeypatch
        self.path = path if path is not None else LOCK_PATH
        self._fh: Any = None

    def acquire(self, *, blocking: bool = False) -> bool:
        _ensure_dirs()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(self._fh.fileno(), flags)
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(f"pid={os.getpid()} ts={time.time()}\n")
            self._fh.flush()
            return True
        except BlockingIOError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "UpgradeLock":
        if not self.acquire(blocking=True):
            raise RuntimeError("无法获取升级锁")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def is_locked() -> bool:
    lock = UpgradeLock()
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


# ── 目标解析 ────────────────────────────────────────────────────

def normalize_target_tag(target: str) -> str:
    parts = version.parse_semver(target)
    if parts is None:
        raise ValueError("目标版本必须是 x.y.z 或 vx.y.z")
    return "v" + version.format_semver(parts)


def fetch_official_release(tag: str) -> dict[str, Any]:
    """拉取官方 Release；拒绝 draft/prerelease/非法 URL。"""
    if "fetch_release" in _hooks:
        return _hooks["fetch_release"](tag)
    url = GITHUB_RELEASE_BY_TAG.format(tag=tag)
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "agent-cockpit-upgrade",
                },
            )
    except Exception as exc:
        raise ValueError(f"无法获取 Release: {type(exc).__name__}") from None
    if resp.status_code == 404:
        raise ValueError(f"官方 Release 不存在: {tag}")
    if resp.status_code != 200:
        raise ValueError(f"GitHub 返回 {resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        raise ValueError("Release 响应不是合法 JSON") from None
    parsed = version._parse_release_payload(data)
    if parsed is None:
        raise ValueError("Release 非法、draft 或 prerelease")
    # 精确 tag 必须匹配
    raw_tag = str(data.get("tag_name") or "")
    if version.parse_semver(raw_tag) != version.parse_semver(tag):
        raise ValueError("Release tag 与请求不一致")
    # 解析 commit sha：优先 target_commitish 若为 40hex，否则用 tag 解析
    sha = ""
    tc = data.get("target_commitish")
    if isinstance(tc, str) and len(tc) == 40 and all(c in "0123456789abcdef" for c in tc.lower()):
        sha = tc.lower()
    if not sha:
        # 二次请求 tag 对象（可测试注入）
        if "resolve_tag_sha" in _hooks:
            sha = _hooks["resolve_tag_sha"](tag)
        else:
            sha = _resolve_tag_sha(tag)
    if not sha or len(sha) < 7:
        raise ValueError("无法解析 Release 对应 commit SHA")
    return {
        "version": parsed["version"],
        "tag": "v" + parsed["version"],
        "sha": sha,
        "url": parsed["url"],
        "name": parsed["name"],
        "published_at": parsed.get("published_at"),
    }


def _resolve_tag_sha(tag: str) -> str:
    api = f"https://api.github.com/repos/fyc0451/agent-cockpit/git/ref/tags/{tag}"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                api,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "agent-cockpit-upgrade",
                },
            )
            if resp.status_code != 200:
                return ""
            obj = resp.json().get("object") or {}
            sha = str(obj.get("sha") or "")
            if obj.get("type") == "tag":
                tresp = client.get(
                    f"https://api.github.com/repos/fyc0451/agent-cockpit/git/tags/{sha}",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "agent-cockpit-upgrade",
                    },
                )
                if tresp.status_code == 200:
                    sha = str((tresp.json().get("object") or {}).get("sha") or "")
            return sha
    except Exception:
        return ""


# ── 预检 ────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=check,
    )


def precheck_install_dir(install_dir: Path | None = None) -> dict[str, Any]:
    """升级前预检；失败 raise ValueError(人类可读、无密钥)。"""
    root = install_dir or INSTALL_DIR
    if not (root / "server.py").is_file():
        raise ValueError("安装目录缺少 server.py")
    if not (root / ".venv" / "bin" / "python").exists() and not (
        root / ".venv" / "bin" / "pip"
    ).exists():
        # 测试环境可无 .venv；生产要求有
        if "skip_venv_check" not in _hooks:
            raise ValueError("未找到 .venv，请先运行 install.sh")
    try:
        _git(["rev-parse", "--is-inside-work-tree"], root)
    except subprocess.CalledProcessError:
        raise ValueError("安装目录不是 git 仓库") from None
    # tracked dirty
    st = _git(["status", "--porcelain", "-uall"], root, check=False)
    dirty_lines = [ln for ln in (st.stdout or "").splitlines() if ln.strip()]
    blocking: list[str] = []
    for ln in dirty_lines:
        path = ln[3:].strip() if len(ln) > 3 else ln
        if path.startswith("\""):
            path = path.strip("\"")
        if any(path == p.rstrip("/") or path.startswith(p) for p in ALLOWED_DIRTY_PREFIXES):
            continue
        # untracked allowed only under allowed prefixes
        blocking.append(path)
    if blocking:
        raise ValueError(
            "工作区有未提交/未跟踪改动，已停止升级: "
            + ", ".join(blocking[:8])
            + ("…" if len(blocking) > 8 else "")
        )
    # disk space
    usage = shutil.disk_usage(str(root))
    if usage.free < DISK_MIN_FREE_BYTES:
        raise ValueError("磁盘剩余空间不足 200MB")
    return {
        "install_dir": str(root),
        "head": _git(["rev-parse", "HEAD"], root).stdout.strip(),
        "free_bytes": usage.free,
    }


# ── 备份 / 回滚 ─────────────────────────────────────────────────

def create_backup(install_dir: Path, job_id: str) -> dict[str, Any]:
    backup_id = f"{job_id}-{int(time.time())}"
    dest = BACKUP_ROOT / backup_id
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    head = _git(["rev-parse", "HEAD"], install_dir).stdout.strip()
    meta = {
        "backup_id": backup_id,
        "head_sha": head,
        "created_at": _utc_iso(),
        "files": [],
    }
    # VERSION
    for name in ("VERSION", "requirements.txt"):
        src = install_dir / name
        if src.is_file():
            shutil.copy2(src, dest / name)
            meta["files"].append(name)
    # settings + sqlite snapshots from DATA_DIR
    data = settings.DATA_DIR
    for name in (
        "settings.json",
        "tasks.sqlite3",
        "coordination.sqlite3",
        "mail-projects.json",
        "team-sessions.json",
    ):
        src = data / name
        if src.is_file():
            if src.suffix == ".sqlite3":
                _sqlite_backup(src, dest / name)
            else:
                shutil.copy2(src, dest / name)
            meta["files"].append(name)
    (dest / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return meta


def _sqlite_backup(src: Path, dest: Path) -> None:
    try:
        src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        dst_con = sqlite3.connect(str(dest))
        try:
            src_con.backup(dst_con)
        finally:
            dst_con.close()
            src_con.close()
    except Exception:
        shutil.copy2(src, dest)


def restore_code(install_dir: Path, sha: str) -> None:
    if "git_checkout" in _hooks:
        _hooks["git_checkout"](install_dir, sha)
        return
    _git(["checkout", "--force", sha], install_dir)
    _git(["clean", "-fd", "-e", ".venv"], install_dir, check=False)


def restore_data_files(backup_id: str) -> None:
    dest = BACKUP_ROOT / backup_id
    if not dest.is_dir():
        return
    data = settings.DATA_DIR
    for name in (
        "settings.json",
        "tasks.sqlite3",
        "coordination.sqlite3",
        "mail-projects.json",
        "team-sessions.json",
    ):
        src = dest / name
        if src.is_file():
            shutil.copy2(src, data / name)


# ── 安装 / 重启 / health ────────────────────────────────────────

def install_deps(install_dir: Path) -> None:
    if "install_deps" in _hooks:
        _hooks["install_deps"](install_dir)
        return
    pip = install_dir / ".venv" / "bin" / "pip"
    if not pip.is_file():
        raise ValueError("找不到 .venv/bin/pip")
    r = subprocess.run(
        [str(pip), "install", "-r", str(install_dir / "requirements.txt")],
        cwd=str(install_dir),
        text=True,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        raise ValueError("依赖安装失败")


def restart_cockpit_only() -> None:
    """只重启 Cockpit；不触碰 Herdr / Agent Mail Hub。"""
    if "restart_cockpit" in _hooks:
        _hooks["restart_cockpit"]()
        return
    if sys.platform == "darwin":
        script = INSTALL_DIR / "launchd.sh"
        r = subprocess.run(
            [str(script), "restart"],
            cwd=str(INSTALL_DIR),
            text=True,
            capture_output=True,
            check=False,
        )
        if r.returncode != 0:
            raise ValueError("LaunchAgent 重启失败")
        return
    # Linux user systemd
    r = subprocess.run(
        ["systemctl", "--user", "restart", "agent-cockpit.service"],
        text=True,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        raise ValueError("systemd 重启 Cockpit 失败")


def health_check(*, timeout_s: float = HEALTH_TIMEOUT_S) -> bool:
    if "health_check" in _hooks:
        return bool(_hooks["health_check"]())
    host = os.environ.get("COCKPIT_HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = os.environ.get("COCKPIT_PORT", "8790")
    url = f"http://{host}:{port}/health"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "ok":
                    return True
        except Exception:
            pass
        time.sleep(HEALTH_POLL_S)
    return False


def fetch_and_checkout(install_dir: Path, tag: str, sha: str) -> None:
    if "fetch_and_checkout" in _hooks:
        _hooks["fetch_and_checkout"](install_dir, tag, sha)
        return
    remote = _hooks.get("git_remote_url")
    if callable(remote):
        url = remote()
    else:
        url = GITHUB_REPO
    # ensure remote origin points usable
    _git(["fetch", "--tags", "origin", tag], install_dir, check=False)
    # if origin missing tag, try direct
    r = _git(["rev-parse", "--verify", sha], install_dir, check=False)
    if r.returncode != 0:
        _git(["fetch", url, f"refs/tags/{tag}:refs/tags/{tag}"], install_dir)
    _git(["checkout", "--force", sha], install_dir)


# ── 调度：API 侧 ────────────────────────────────────────────────

def public_status(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """给 API 的脱敏摘要。"""
    st = state or read_state()
    return {
        "job_id": st.get("job_id"),
        "state": st.get("state") or "idle",
        "target_version": st.get("target_version"),
        "target_tag": st.get("target_tag"),
        "from_version": st.get("from_version"),
        "phase": st.get("phase"),
        "error": st.get("error"),
        "created_at": st.get("created_at"),
        "updated_at": st.get("updated_at"),
        "finished_at": st.get("finished_at"),
        "active": (st.get("state") in ACTIVE_STATES),
        # 不暴露 pid 路径细节中的敏感段；仅布尔
        "worker_running": _worker_alive(st.get("worker_pid")),
    }


def _worker_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_upgrade(target: str, *, install_dir: Path | None = None) -> dict[str, Any]:
    """管理员触发升级：校验、落盘、spawn 外部 worker。

    返回 queued/active 状态；**不**表示升级成功。
    """
    root = install_dir or INSTALL_DIR
    st = read_state()
    if st.get("state") in ACTIVE_STATES:
        # 同一进行中任务：幂等返回
        return {
            "accepted": False,
            "reason": "upgrade_in_progress",
            "status": public_status(st),
        }

    tag = normalize_target_tag(target)
    current = version.read_current_version(root / "VERSION")
    cur_parts = version.parse_semver(current)
    tgt_parts = version.parse_semver(tag)
    assert cur_parts and tgt_parts
    if version.compare_semver(tgt_parts, cur_parts) < 0:
        raise ValueError("禁止降级")
    if version.compare_semver(tgt_parts, cur_parts) == 0:
        raise ValueError("已是目标版本")

    release = fetch_official_release(tag)
    pre = precheck_install_dir(root)

    lock = UpgradeLock()
    if not lock.acquire(blocking=False):
        st2 = read_state()
        return {
            "accepted": False,
            "reason": "upgrade_locked",
            "status": public_status(st2),
        }

    job_id = uuid.uuid4().hex[:12]
    log_path = LOG_DIR / f"{job_id}.log"
    state = _default_state()
    state.update(
        {
            "job_id": job_id,
            "state": "queued",
            "phase": "spawn_worker",
            "target_version": release["version"],
            "target_tag": release["tag"],
            "target_sha": release["sha"],
            "from_version": current,
            "from_sha": pre["head"],
            "created_at": _utc_iso(),
            "log_path": str(log_path),
            "diagnostics": {"precheck_free_bytes": pre["free_bytes"]},
            "install_dir": str(root),
        }
    )
    write_state(state)
    # 已写入 queued（ACTIVE）：先释放 flock，再 spawn，避免 worker 无法获锁
    lock.release()
    try:
        pid = spawn_worker(job_id, root, log_path)
        # worker 可能已同步跑完（测试钩子）；合并 pid，勿覆盖终态为 queued
        latest = read_state()
        if latest.get("job_id") == job_id:
            latest["worker_pid"] = pid
            if latest.get("state") == "queued":
                latest["phase"] = "worker_started"
            write_state(latest)
            state = latest
        else:
            state["worker_pid"] = pid
            write_state(state)
    except Exception as exc:
        state = read_state()
        if state.get("job_id") == job_id and state.get("state") in ACTIVE_STATES | {"queued"}:
            state["state"] = "failed"
            state["error"] = f"无法启动升级执行器: {type(exc).__name__}"
            state["finished_at"] = _utc_iso()
            write_state(state)
        raise

    return {
        "accepted": True,
        "reason": "queued"
        if state.get("state") in ACTIVE_STATES or state.get("state") == "queued"
        else str(state.get("state") or "queued"),
        "status": public_status(state),
    }


def spawn_worker(job_id: str, install_dir: Path, log_path: Path) -> int:
    """在新 session 中启动 worker，脱离 Cockpit 进程组。"""
    if "spawn_worker" in _hooks:
        return int(_hooks["spawn_worker"](job_id, install_dir, log_path))
    _ensure_dirs()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(WORKER_SCRIPT),
                "--job-id",
                job_id,
                "--install-dir",
                str(install_dir),
            ],
            cwd=str(install_dir),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # setsid：Cockpit 退出不杀 worker
            close_fds=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    finally:
        log_fh.close()
    return int(proc.pid)


# ── Worker 运行主循环 ───────────────────────────────────────────

def run_job(job_id: str, install_dir: Path | None = None) -> int:
    """worker 入口；持锁执行完整事务。返回进程 exit code。"""
    root = Path(install_dir) if install_dir else INSTALL_DIR
    lock = UpgradeLock()
    if not lock.acquire(blocking=False):
        st = read_state()
        if st.get("job_id") != job_id:
            return 2
        # 同 job 可能重入
    try:
        return _run_job_locked(job_id, root)
    finally:
        lock.release()


def _transition(state: dict[str, Any], new_state: str, phase: str | None = None) -> dict[str, Any]:
    state["state"] = new_state
    if phase is not None:
        state["phase"] = phase
    write_state(state)
    return state


def _run_job_locked(job_id: str, root: Path) -> int:
    state = read_state()
    if state.get("job_id") != job_id:
        return 3
    tag = state.get("target_tag")
    sha = state.get("target_sha")
    from_sha = state.get("from_sha")
    if not tag or not sha or not from_sha:
        state["state"] = "failed"
        state["error"] = "任务元数据不完整"
        state["finished_at"] = _utc_iso()
        write_state(state)
        return 4

    backup_meta: dict[str, Any] | None = None
    try:
        _transition(state, "prechecking", "precheck")
        precheck_install_dir(root)

        _transition(state, "backing_up", "backup")
        backup_meta = create_backup(root, job_id)
        state["backup_id"] = backup_meta["backup_id"]
        write_state(state)

        _transition(state, "fetching", "fetch_checkout")
        fetch_and_checkout(root, str(tag), str(sha))

        _transition(state, "installing", "pip_install")
        install_deps(root)

        _transition(state, "restarting", "restart_cockpit")
        restart_cockpit_only()

        _transition(state, "verifying", "health")
        if not health_check():
            raise RuntimeError("health 检查失败")

        state["state"] = "succeeded"
        state["phase"] = "done"
        state["error"] = None
        state["finished_at"] = _utc_iso()
        write_state(state)
        return 0
    except Exception as exc:
        # 自动回滚
        err = str(exc)[:300]
        state["error"] = err
        state["diagnostics"] = {
            **(state.get("diagnostics") or {}),
            "failure_type": type(exc).__name__,
        }
        try:
            _transition(state, "rolling_back", "rollback")
            restore_code(root, str(from_sha))
            if backup_meta:
                try:
                    install_deps(root)
                except Exception:
                    pass
                restore_data_files(str(backup_meta.get("backup_id") or state.get("backup_id") or ""))
            try:
                restart_cockpit_only()
            except Exception:
                pass
            ok = health_check(timeout_s=45.0)
            state["state"] = "rolled_back" if ok else "failed"
            state["phase"] = "rollback_done" if ok else "rollback_health_failed"
            if not ok:
                state["error"] = (err + "; 回滚后 health 仍失败")[:300]
            state["finished_at"] = _utc_iso()
            write_state(state)
            return 1 if ok else 5
        except Exception as rex:
            state["state"] = "failed"
            state["phase"] = "rollback_failed"
            state["error"] = f"{err}; 回滚异常: {type(rex).__name__}"[:300]
            state["finished_at"] = _utc_iso()
            write_state(state)
            return 6
