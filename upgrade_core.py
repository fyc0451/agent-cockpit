"""U1b：服务外安全升级核心（状态机 / 互斥 / 预检 / 备份 / 回滚）。

发布阻断修复要点：
- flock 内 reconcile ACTIVE，杜绝双 accepted
- 无锁绝不进入事务
- stale worker 可恢复
- venv staging 原子切换
- SQLite backup fail-closed；完整数据 manifest
- 公开状态仅 error_code，原始异常仅 0600 日志
- Release SHA 必须 40hex，且 tag 精确指向、属于 main 历史
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

import settings
import version

logger = logging.getLogger("agent-cockpit.upgrade")

INSTALL_DIR = Path(__file__).resolve().parent
UPGRADE_DIR = settings.DATA_DIR / "upgrade"
STATE_PATH = UPGRADE_DIR / "state.json"
LOCK_PATH = UPGRADE_DIR / "upgrade.lock"
LOG_DIR = UPGRADE_DIR / "logs"
BACKUP_ROOT = UPGRADE_DIR / "backups"
VENV_LIVE = ".venv"
VENV_STAGING = ".venv.upgrade-staging"
VENV_PREV = ".venv.upgrade-previous"
WORKER_SCRIPT = INSTALL_DIR / "cockpit-upgrade-worker.py"

GITHUB_RELEASE_BY_TAG = (
    "https://api.github.com/repos/fyc0451/agent-cockpit/releases/tags/{tag}"
)
GITHUB_REPO = "https://github.com/fyc0451/agent-cockpit.git"
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

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
        "switching",
        "restarting",
        "verifying",
        "rolling_back",
    }
)

# queued 后 worker 尚未回写 pid 的宽限（秒）
QUEUED_HANDSHAKE_GRACE_S = 30.0
HEALTH_TIMEOUT_S = 60.0
HEALTH_POLL_S = 1.0
DISK_MIN_FREE_BYTES = 200 * 1024 * 1024

ALLOWED_DIRTY_PREFIXES = (
    ".venv/",
    ".venv",
    ".venv.upgrade-staging/",
    ".venv.upgrade-staging",
    ".venv.upgrade-previous/",
    ".venv.upgrade-previous",
    "__pycache__/",
    ".pytest_cache/",
)

DATA_MANIFEST = (
    "settings.json",
    "tasks.sqlite3",
    "coordination.sqlite3",
    "push.sqlite3",
    "mail-projects.json",
    "team-sessions.json",
    "team-inbox-route.json",
    "vapid-private.pem",
)

# 稳定错误码 → 对外文案（无路径/密钥）
ERROR_MESSAGES = {
    "upgrade_in_progress": "已有升级任务进行中",
    "upgrade_locked": "升级互斥锁被占用",
    "invalid_target": "目标版本不合法",
    "downgrade_forbidden": "禁止降级",
    "already_current": "已是目标版本",
    "release_unavailable": "无法获取或验证官方 Release",
    "precheck_dirty": "工作区有未提交改动，已停止升级",
    "precheck_disk": "磁盘剩余空间不足",
    "precheck_git": "安装目录 git 状态不可用",
    "precheck_venv": "虚拟环境不可用",
    "precheck_supervisor": "升级控制通道不可用，无法在改代码前验证重启能力",
    "lock_failed": "无法获取升级锁，拒绝执行",
    "backup_failed": "备份失败，已中止（fail closed）",
    "fetch_failed": "拉取目标版本失败",
    "install_failed": "依赖安装失败",
    "switch_failed": "虚拟环境切换失败",
    "restart_failed": "Cockpit 重启失败",
    "health_failed": "health 检查失败",
    "rollback_failed": "回滚失败",
    "stale_worker": "升级进程已异常退出，可重试",
    "spawn_failed": "无法启动升级执行器",
    "internal_error": "升级内部错误",
}

_hooks: dict[str, Callable[..., Any]] = {}


def configure_hooks(**hooks: Callable[..., Any]) -> None:
    _hooks.clear()
    _hooks.update(hooks)


def clear_hooks() -> None:
    _hooks.clear()


def _utc_iso(ts: float | None = None) -> str:
    when = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return when.isoformat().replace("+00:00", "Z")


def _ensure_dirs() -> None:
    UPGRADE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)


def _append_job_log(log_path: str | None, message: str) -> None:
    if not log_path:
        return
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{_utc_iso()} {message}\n")
        os.chmod(path, 0o600)
    except OSError:
        logger.exception("upgrade log write failed")


# ── 状态 ────────────────────────────────────────────────────────

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
        "error_code": None,
        "created_at": None,
        "updated_at": _utc_iso(),
        "finished_at": None,
        "worker_pid": None,
        "worker_started_at": None,
        "worker_start_boot_id": None,
        "log_path": None,
        "backup_id": None,
        "install_dir": None,
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
    # 禁止把 raw exception 文本写进 state 的 error 字段
    if "error" in state:
        state.pop("error", None)
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


# ── 锁 ──────────────────────────────────────────────────────────

class UpgradeLock:
    def __init__(self, path: Path | None = None) -> None:
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


def is_locked() -> bool:
    lock = UpgradeLock()
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


# ── Worker 身份 / stale ─────────────────────────────────────────

def _boot_id() -> str | None:
    if "boot_id" in _hooks:
        return str(_hooks["boot_id"]())
    try:
        p = Path("/proc/sys/kernel/random/boot_id")
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return None


def _proc_start_time(pid: int) -> str | None:
    """Linux: /proc/<pid>/stat 字段 22；其它平台返回 None。"""
    if "proc_start_time" in _hooks:
        return _hooks["proc_start_time"](pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # comm 可能含空格/括号；取最后一个 ) 之后
        rest = stat.split(")", 1)[1].strip().split()
        return rest[19]  # starttime
    except (OSError, IndexError, ValueError):
        return None


def _worker_alive(pid: Any, started_at: Any = None, boot_id: Any = None) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    if "worker_alive" in _hooks:
        return bool(_hooks["worker_alive"](pid, started_at, boot_id))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # PID 复用防护：比对启动 tick + boot_id
    if boot_id and _boot_id() and boot_id != _boot_id():
        return False
    if started_at:
        cur = _proc_start_time(pid)
        if cur is not None and str(started_at) != str(cur):
            return False
    return True


def reconcile_stale_state(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """将死 worker / 超时 handshake 的 active 状态收敛为 failed(stale_worker)。"""
    st = dict(state or read_state())
    if st.get("state") not in ACTIVE_STATES:
        return st
    pid = st.get("worker_pid")
    created = st.get("created_at")
    # queued 且尚无 pid：宽限期内不标 stale
    if pid is None and st.get("state") == "queued":
        try:
            created_ts = datetime.fromisoformat(
                str(created).replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError):
            created_ts = 0.0
        if time.time() - created_ts < QUEUED_HANDSHAKE_GRACE_S:
            return st
        st["state"] = "failed"
        st["error_code"] = "stale_worker"
        st["phase"] = "handshake_timeout"
        st["finished_at"] = _utc_iso()
        write_state(st)
        return st
    if not _worker_alive(pid, st.get("worker_started_at"), st.get("worker_start_boot_id")):
        st["state"] = "failed"
        st["error_code"] = "stale_worker"
        st["phase"] = "worker_dead"
        st["finished_at"] = _utc_iso()
        write_state(st)
    return st


# ── 目标 / Release ──────────────────────────────────────────────

def normalize_target_tag(target: str) -> str:
    parts = version.parse_semver(target)
    if parts is None:
        raise ValueError("invalid_target")
    return "v" + version.format_semver(parts)


def _require_sha40(sha: str) -> str:
    s = (sha or "").lower().strip()
    if not _SHA40_RE.fullmatch(s):
        raise ValueError("release_unavailable")
    return s


def fetch_official_release(tag: str) -> dict[str, Any]:
    if "fetch_release" in _hooks:
        rel = _hooks["fetch_release"](tag)
        rel["sha"] = _require_sha40(str(rel.get("sha") or ""))
        return rel
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
        logger.info("fetch release network error: %s", type(exc).__name__)
        raise ValueError("release_unavailable") from None
    if resp.status_code != 200:
        raise ValueError("release_unavailable")
    try:
        data = resp.json()
    except Exception:
        raise ValueError("release_unavailable") from None
    parsed = version._parse_release_payload(data)
    if parsed is None:
        raise ValueError("release_unavailable")
    raw_tag = str(data.get("tag_name") or "")
    if version.parse_semver(raw_tag) != version.parse_semver(tag):
        raise ValueError("release_unavailable")
    sha = _resolve_tag_sha(tag)
    sha = _require_sha40(sha)
    return {
        "version": parsed["version"],
        "tag": "v" + parsed["version"],
        "sha": sha,
        "url": parsed["url"],
        "name": parsed["name"],
        "published_at": parsed.get("published_at"),
    }


def _resolve_tag_sha(tag: str) -> str:
    if "resolve_tag_sha" in _hooks:
        return str(_hooks["resolve_tag_sha"](tag))
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
            return sha.lower()
    except Exception as exc:
        logger.info("resolve tag sha failed: %s", type(exc).__name__)
        return ""


def verify_tag_points_to_sha(install_dir: Path, tag: str, sha: str) -> None:
    """fetch 后校验 tag 精确指向 sha，且 sha 属于 origin/main 历史。"""
    if "verify_tag_sha" in _hooks:
        _hooks["verify_tag_sha"](install_dir, tag, sha)
        return
    sha = _require_sha40(sha)
    r = _git(["rev-parse", f"{tag}^{{}}"], install_dir, check=False)
    if r.returncode != 0:
        r = _git(["rev-parse", tag], install_dir, check=False)
    pointed = (r.stdout or "").strip().lower()
    if pointed != sha:
        raise ValueError("release_unavailable")
    # 属于 origin/main 祖先
    r2 = _git(["merge-base", "--is-ancestor", sha, "origin/main"], install_dir, check=False)
    if r2.returncode != 0:
        # 尝试 fetch main
        _git(["fetch", "origin", "main"], install_dir, check=False)
        r3 = _git(["merge-base", "--is-ancestor", sha, "origin/main"], install_dir, check=False)
        if r3.returncode != 0:
            raise ValueError("release_unavailable")


# ── 预检 ────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=check,
    )


def preflight_supervisor() -> None:
    """改代码前验证控制通道；失败 fail closed。"""
    if "preflight_supervisor" in _hooks:
        ok = _hooks["preflight_supervisor"]()
        if not ok:
            raise ValueError("precheck_supervisor")
        return
    if sys.platform == "darwin":
        if not shutil.which("launchctl"):
            raise ValueError("precheck_supervisor")
        return
    # Linux: systemctl --user 必须可用
    if not shutil.which("systemctl"):
        raise ValueError("precheck_supervisor")
    r = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    # bus 不可达时 returncode != 0 且 stderr 含 Failed to connect
    err = (r.stderr or "") + (r.stdout or "")
    if r.returncode != 0 and (
        "Failed to connect" in err or "No such file" in err or "not been booted" in err
    ):
        raise ValueError("precheck_supervisor")


def estimate_space_needed(install_dir: Path) -> int:
    """预估所需空间：当前 .venv 大小 + 备份余量。"""
    venv = install_dir / VENV_LIVE
    size = 0
    if venv.is_dir():
        for dirpath, _dirnames, filenames in os.walk(venv):
            for name in filenames:
                try:
                    size += (Path(dirpath) / name).stat().st_size
                except OSError:
                    pass
    # 需要：新 venv ≈ 旧 venv + 代码余量 + 数据备份
    return max(size + 100 * 1024 * 1024, DISK_MIN_FREE_BYTES * 2)


def precheck_install_dir(install_dir: Path | None = None) -> dict[str, Any]:
    root = install_dir or INSTALL_DIR
    if not (root / "server.py").is_file():
        raise ValueError("precheck_git")
    if "skip_venv_check" not in _hooks:
        if not (root / VENV_LIVE / "bin" / "pip").exists():
            raise ValueError("precheck_venv")
    try:
        _git(["rev-parse", "--is-inside-work-tree"], root)
    except subprocess.CalledProcessError:
        raise ValueError("precheck_git") from None
    st = _git(["status", "--porcelain", "-uall"], root, check=False)
    dirty_lines = [ln for ln in (st.stdout or "").splitlines() if ln.strip()]
    blocking: list[str] = []
    for ln in dirty_lines:
        path = ln[3:].strip() if len(ln) > 3 else ln
        if path.startswith("\""):
            path = path.strip("\"")
        if any(path == p.rstrip("/") or path.startswith(p) for p in ALLOWED_DIRTY_PREFIXES):
            continue
        blocking.append(path)
    if blocking:
        raise ValueError("precheck_dirty")
    usage = shutil.disk_usage(str(root))
    needed = estimate_space_needed(root)
    if usage.free < needed:
        raise ValueError("precheck_disk")
    preflight_supervisor()
    return {
        "install_dir": str(root),
        "head": _git(["rev-parse", "HEAD"], root).stdout.strip(),
        "free_bytes": usage.free,
        "needed_bytes": needed,
    }


# ── 备份 / 数据 ─────────────────────────────────────────────────

def create_backup(install_dir: Path, job_id: str) -> dict[str, Any]:
    backup_id = f"{job_id}-{int(time.time())}"
    dest = BACKUP_ROOT / backup_id
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    head = _git(["rev-parse", "HEAD"], install_dir).stdout.strip()
    meta: dict[str, Any] = {
        "backup_id": backup_id,
        "head_sha": head,
        "created_at": _utc_iso(),
        "files": [],
    }
    for name in ("VERSION", "requirements.txt"):
        src = install_dir / name
        if src.is_file():
            shutil.copy2(src, dest / name)
            meta["files"].append(name)
    data = settings.DATA_DIR
    for name in DATA_MANIFEST:
        src = data / name
        if not src.is_file():
            continue
        if src.suffix == ".sqlite3" or name.endswith(".sqlite3"):
            _sqlite_backup_strict(src, dest / name)
        else:
            shutil.copy2(src, dest / name)
        meta["files"].append(name)
    (dest / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(dest / "meta.json", 0o600)
    return meta


def _sqlite_backup_strict(src: Path, dest: Path) -> None:
    """在线 backup；失败 fail closed，禁止 raw copy 活库。"""
    try:
        src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        dst_con = sqlite3.connect(str(dest))
        try:
            src_con.backup(dst_con)
            dst_con.commit()
        finally:
            dst_con.close()
            src_con.close()
    except Exception as exc:
        logger.info("sqlite backup failed type=%s", type(exc).__name__)
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError("backup_failed") from None


def restore_code(install_dir: Path, sha: str) -> None:
    if "git_checkout" in _hooks:
        _hooks["git_checkout"](install_dir, sha)
        return
    _git(["checkout", "--force", sha], install_dir)
    _git(["clean", "-fd", "-e", ".venv", "-e", ".venv.upgrade-staging", "-e", ".venv.upgrade-previous"], install_dir, check=False)


def restore_venv(install_dir: Path) -> None:
    """原子切回 previous venv。"""
    live = install_dir / VENV_LIVE
    prev = install_dir / VENV_PREV
    staging = install_dir / VENV_STAGING
    if prev.is_dir():
        if live.exists():
            shutil.rmtree(live, ignore_errors=True)
        os.rename(prev, live)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)


def restore_data_files(backup_id: str) -> None:
    dest = BACKUP_ROOT / backup_id
    if not dest.is_dir():
        raise ValueError("backup_failed")
    meta_path = dest / "meta.json"
    if not meta_path.is_file():
        raise ValueError("backup_failed")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    files = meta.get("files") or []
    data = settings.DATA_DIR
    # 先写临时再 replace
    for name in files:
        if name in ("VERSION", "requirements.txt"):
            continue
        src = dest / name
        if not src.is_file():
            raise ValueError("backup_failed")
        target = data / name
        tmp = data / f".{name}.restore-tmp"
        shutil.copy2(src, tmp)
        os.replace(tmp, target)


# ── 安装 / 切换 / 重启 / health ─────────────────────────────────

def install_deps_staging(install_dir: Path) -> Path:
    """在 staging venv 安装依赖，返回 staging 路径。"""
    if "install_deps_staging" in _hooks:
        return Path(_hooks["install_deps_staging"](install_dir))
    staging = install_dir / VENV_STAGING
    if staging.exists():
        shutil.rmtree(staging)
    r = subprocess.run(
        [sys.executable, "-m", "venv", str(staging)],
        cwd=str(install_dir),
        text=True,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        raise ValueError("install_failed")
    pip = staging / "bin" / "pip"
    r2 = subprocess.run(
        [str(pip), "install", "-r", str(install_dir / "requirements.txt")],
        cwd=str(install_dir),
        text=True,
        capture_output=True,
        check=False,
    )
    if r2.returncode != 0:
        raise ValueError("install_failed")
    return staging


def atomic_switch_venv(install_dir: Path) -> None:
    """live <- staging；旧 live 移到 previous。"""
    if "atomic_switch_venv" in _hooks:
        _hooks["atomic_switch_venv"](install_dir)
        return
    live = install_dir / VENV_LIVE
    staging = install_dir / VENV_STAGING
    prev = install_dir / VENV_PREV
    if not staging.is_dir():
        raise ValueError("switch_failed")
    if prev.exists():
        shutil.rmtree(prev, ignore_errors=True)
    if live.exists():
        os.rename(live, prev)
    os.rename(staging, live)


def stop_cockpit_for_restore() -> None:
    if "stop_cockpit" in _hooks:
        _hooks["stop_cockpit"]()
        return
    if sys.platform == "darwin":
        script = INSTALL_DIR / "launchd.sh"
        subprocess.run([str(script), "stop"], capture_output=True, check=False)
        return
    subprocess.run(
        ["systemctl", "--user", "stop", "agent-cockpit.service"],
        capture_output=True,
        check=False,
        timeout=30,
    )


def restart_cockpit_only() -> None:
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
            raise ValueError("restart_failed")
        return
    r = subprocess.run(
        ["systemctl", "--user", "restart", "agent-cockpit.service"],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if r.returncode != 0:
        raise ValueError("restart_failed")


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
            if resp.status_code == 200 and resp.json().get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(HEALTH_POLL_S)
    return False


def fetch_and_checkout(install_dir: Path, tag: str, sha: str) -> None:
    if "fetch_and_checkout" in _hooks:
        _hooks["fetch_and_checkout"](install_dir, tag, sha)
        verify_tag_points_to_sha(install_dir, tag, sha)
        return
    _git(["fetch", "--tags", "origin", tag], install_dir, check=False)
    r = _git(["rev-parse", "--verify", sha], install_dir, check=False)
    if r.returncode != 0:
        _git(["fetch", GITHUB_REPO, f"refs/tags/{tag}:refs/tags/{tag}"], install_dir)
    verify_tag_points_to_sha(install_dir, tag, sha)
    _git(["checkout", "--force", sha], install_dir)


# ── 公开状态 / API ──────────────────────────────────────────────

def public_status(state: dict[str, Any] | None = None) -> dict[str, Any]:
    st = reconcile_stale_state(state)
    code = st.get("error_code")
    msg = ERROR_MESSAGES.get(str(code), None) if code else None
    return {
        "job_id": st.get("job_id"),
        "state": st.get("state") or "idle",
        "target_version": st.get("target_version"),
        "target_tag": st.get("target_tag"),
        "from_version": st.get("from_version"),
        "phase": st.get("phase"),
        "error_code": code,
        "error_message": msg,
        "created_at": st.get("created_at"),
        "updated_at": st.get("updated_at"),
        "finished_at": st.get("finished_at"),
        "active": (st.get("state") in ACTIVE_STATES),
        "worker_running": _worker_alive(
            st.get("worker_pid"),
            st.get("worker_started_at"),
            st.get("worker_start_boot_id"),
        ),
    }


def start_upgrade(target: str, *, install_dir: Path | None = None) -> dict[str, Any]:
    """管理员触发升级。accepted 只表示排队/进行中，不代表成功。"""
    root = install_dir or INSTALL_DIR
    # 无锁快速路径：仅用于快速返回；真正决策在锁内
    st0 = reconcile_stale_state()
    if st0.get("state") in ACTIVE_STATES:
        return {
            "accepted": False,
            "reason": "upgrade_in_progress",
            "status": public_status(st0),
        }

    try:
        tag = normalize_target_tag(target)
    except ValueError:
        raise ValueError("invalid_target") from None

    current = version.read_current_version(root / "VERSION")
    cur_parts = version.parse_semver(current)
    tgt_parts = version.parse_semver(tag)
    assert cur_parts and tgt_parts
    if version.compare_semver(tgt_parts, cur_parts) < 0:
        raise ValueError("downgrade_forbidden")
    if version.compare_semver(tgt_parts, cur_parts) == 0:
        raise ValueError("already_current")

    try:
        release = fetch_official_release(tag)
        pre = precheck_install_dir(root)
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("upgrade precheck unexpected")
        raise ValueError("internal_error") from None

    lock = UpgradeLock()
    if not lock.acquire(blocking=False):
        st2 = reconcile_stale_state()
        return {
            "accepted": False,
            "reason": "upgrade_locked",
            "status": public_status(st2),
        }

    try:
        # 关键：获锁后重新 reconcile，再原子写 queued
        st = reconcile_stale_state()
        if st.get("state") in ACTIVE_STATES:
            return {
                "accepted": False,
                "reason": "upgrade_in_progress",
                "status": public_status(st),
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
                "install_dir": str(root),
                "diagnostics": {
                    "precheck_free_bytes": pre["free_bytes"],
                    "needed_bytes": pre["needed_bytes"],
                },
            }
        )
        write_state(state)
    finally:
        lock.release()

    try:
        pid = spawn_worker(job_id, root, log_path)
        latest = read_state()
        if latest.get("job_id") == job_id:
            latest["worker_pid"] = pid
            if latest.get("state") == "queued":
                latest["phase"] = "worker_started"
            # identity 由 worker 自己回写更准；此处先填 pid
            write_state(latest)
            state = latest
    except Exception as exc:
        logger.exception("spawn worker failed")
        state = read_state()
        if state.get("job_id") == job_id and state.get("state") in ACTIVE_STATES | {"queued"}:
            state["state"] = "failed"
            state["error_code"] = "spawn_failed"
            state["finished_at"] = _utc_iso()
            write_state(state)
        raise ValueError("spawn_failed") from None

    return {
        "accepted": True,
        "reason": "queued"
        if state.get("state") in ACTIVE_STATES or state.get("state") == "queued"
        else str(state.get("state") or "queued"),
        "status": public_status(state),
    }


def spawn_worker(job_id: str, install_dir: Path, log_path: Path) -> int:
    if "spawn_worker" in _hooks:
        return int(_hooks["spawn_worker"](job_id, install_dir, log_path))
    _ensure_dirs()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Linux：优先 systemd-run --user --scope 独立 cgroup（bus 可用时）
    if sys.platform != "darwin" and shutil.which("systemd-run"):
        probe = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        bus_ok = probe.returncode == 0 or "running" in (probe.stdout or "")
        if bus_ok:
            unit = f"agent-cockpit-upgrade-{job_id}"
            cmd = [
                "systemd-run",
                "--user",
                "--unit",
                unit,
                "--collect",
                "--property=KillMode=process",
                sys.executable,
                str(WORKER_SCRIPT),
                "--job-id",
                job_id,
                "--install-dir",
                str(install_dir),
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if r.returncode == 0:
                # 无法直接拿 pid；worker 会回写
                return 0
            logger.info("systemd-run failed rc=%s", r.returncode)
    # 回退：setsid 独立 session（依赖 KillMode=process 的 Cockpit unit）
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
            start_new_session=True,
            close_fds=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    finally:
        log_fh.close()
    os.chmod(log_path, 0o600)
    return int(proc.pid)


# ── Worker ──────────────────────────────────────────────────────

def run_job(job_id: str, install_dir: Path | None = None) -> int:
    """worker 入口；**必须**获锁，否则拒绝执行。"""
    root = Path(install_dir) if install_dir else INSTALL_DIR
    lock = UpgradeLock()
    if not lock.acquire(blocking=False):
        st = read_state()
        _append_job_log(st.get("log_path"), "lock_failed refuse run_job")
        # 绝不能进入事务
        return 99
    try:
        return _run_job_locked(job_id, root)
    finally:
        lock.release()


def _transition(
    state: dict[str, Any],
    new_state: str,
    phase: str | None = None,
    *,
    error_code: str | None = None,
) -> dict[str, Any]:
    state["state"] = new_state
    if phase is not None:
        state["phase"] = phase
    if error_code is not None:
        state["error_code"] = error_code
    write_state(state)
    return state


def _record_worker_identity(state: dict[str, Any]) -> dict[str, Any]:
    pid = os.getpid()
    state["worker_pid"] = pid
    state["worker_started_at"] = _proc_start_time(pid)
    state["worker_start_boot_id"] = _boot_id()
    write_state(state)
    return state


def _fail(state: dict[str, Any], code: str, phase: str, log_detail: str) -> int:
    _append_job_log(state.get("log_path"), f"{code} {phase} {log_detail}")
    state["state"] = "failed"
    state["error_code"] = code
    state["phase"] = phase
    state["finished_at"] = _utc_iso()
    write_state(state)
    return 1


def _run_job_locked(job_id: str, root: Path) -> int:
    state = read_state()
    if state.get("job_id") != job_id:
        return 3
    state = _record_worker_identity(state)
    tag = state.get("target_tag")
    sha = state.get("target_sha")
    from_sha = state.get("from_sha")
    log_path = state.get("log_path")
    if not tag or not sha or not from_sha:
        return _fail(state, "internal_error", "bad_meta", "incomplete job meta")

    backup_meta: dict[str, Any] | None = None
    try:
        _transition(state, "prechecking", "precheck")
        precheck_install_dir(root)

        _transition(state, "backing_up", "backup")
        try:
            backup_meta = create_backup(root, job_id)
        except ValueError:
            return _fail(state, "backup_failed", "backup", "backup failed")
        state["backup_id"] = backup_meta["backup_id"]
        write_state(state)

        _transition(state, "fetching", "fetch_checkout")
        try:
            fetch_and_checkout(root, str(tag), str(sha))
        except ValueError as exc:
            code = str(exc) if str(exc) in ERROR_MESSAGES else "fetch_failed"
            return _fail(state, code, "fetch", type(exc).__name__)

        _transition(state, "installing", "venv_staging")
        try:
            install_deps_staging(root)
        except ValueError:
            return _fail(state, "install_failed", "venv_staging", "pip/venv failed")

        _transition(state, "switching", "venv_switch")
        try:
            atomic_switch_venv(root)
        except ValueError:
            return _fail(state, "switch_failed", "venv_switch", "switch failed")

        _transition(state, "restarting", "restart_cockpit")
        try:
            restart_cockpit_only()
        except ValueError:
            raise RuntimeError("restart_failed")

        _transition(state, "verifying", "health")
        if not health_check():
            raise RuntimeError("health_failed")

        state["state"] = "succeeded"
        state["phase"] = "done"
        state["error_code"] = None
        state["finished_at"] = _utc_iso()
        write_state(state)
        _append_job_log(log_path, "succeeded")
        return 0
    except Exception as exc:
        code = str(exc) if str(exc) in ERROR_MESSAGES else "internal_error"
        if "health" in str(exc).lower():
            code = "health_failed"
        if "restart" in str(exc).lower():
            code = "restart_failed"
        _append_job_log(log_path, f"failure type={type(exc).__name__} code={code}")
        try:
            _transition(state, "rolling_back", "rollback", error_code=code)
            # 恢复前停 Cockpit，避免活库覆盖
            try:
                stop_cockpit_for_restore()
            except Exception as stop_exc:
                _append_job_log(log_path, f"stop_cockpit {type(stop_exc).__name__}")
            try:
                restore_code(root, str(from_sha))
            except Exception as rex:
                _append_job_log(log_path, f"restore_code {type(rex).__name__}")
            try:
                restore_venv(root)
            except Exception as rex:
                _append_job_log(log_path, f"restore_venv {type(rex).__name__}")
            if backup_meta or state.get("backup_id"):
                bid = str((backup_meta or {}).get("backup_id") or state.get("backup_id"))
                try:
                    restore_data_files(bid)
                except Exception as rex:
                    _append_job_log(log_path, f"restore_data {type(rex).__name__}")
                    state["state"] = "failed"
                    state["error_code"] = "rollback_failed"
                    state["phase"] = "restore_data_failed"
                    state["finished_at"] = _utc_iso()
                    write_state(state)
                    return 6
            try:
                restart_cockpit_only()
            except Exception as rex:
                _append_job_log(log_path, f"restart_after_rollback {type(rex).__name__}")
            ok = health_check(timeout_s=45.0)
            state["state"] = "rolled_back" if ok else "failed"
            state["error_code"] = None if ok else "rollback_failed"
            state["phase"] = "rollback_done" if ok else "rollback_health_failed"
            state["finished_at"] = _utc_iso()
            write_state(state)
            return 1 if ok else 5
        except Exception as rex:
            _append_job_log(log_path, f"rollback_exception {type(rex).__name__}")
            state["state"] = "failed"
            state["error_code"] = "rollback_failed"
            state["phase"] = "rollback_failed"
            state["finished_at"] = _utc_iso()
            write_state(state)
            return 6
