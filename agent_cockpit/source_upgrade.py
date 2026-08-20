"""Source-tree one-click upgrade for the 8790 checkout.

Does not enable native V2 (that would switch this process onto the
packaged agent-cockpit.service). The worker fetches a signed GitHub
release tag into the current worktree, rebuilds web/dist, then restarts
the live source unit.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import artifact_root
from . import runtime_paths
from . import version


logger = logging.getLogger("agent-cockpit.source-upgrade")

ENGINE = "source-checkout"
SOURCE_UNIT = "agent-cockpit-source-8790.service"
TAG_PREFIX = "agent-cockpit-v"
GITHUB_REPO = "https://github.com/fyc0451/agent-cockpit.git"
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_ERROR_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_START_LOCK = threading.Lock()

TERMINAL_STATES = frozenset({"succeeded", "failed", "idle"})
ACTIVE_STATES = frozenset(
    {"queued", "prechecking", "fetching", "installing", "restarting", "verifying"}
)
QUEUED_HANDSHAKE_GRACE_S = 30.0
HEALTH_TIMEOUT_S = 90.0
HEALTH_POLL_S = 1.0
DISK_MIN_FREE_BYTES = 200 * 1024 * 1024
ALLOWED_DIRTY_PREFIXES = (
    ".venv/",
    ".venv",
    "__pycache__/",
    ".pytest_cache/",
    "web/node_modules/",
    "web/dist/",
    "web/test-results/",
    "node_modules/",
    "design-system/",
    "docs/screenshots/",
    "cockpit-inbox/",
)

ERROR_MESSAGES = {
    "upgrade_in_progress": "已有升级任务进行中",
    "already_current": "已是最新版本",
    "release_unavailable": "无法获取或验证官方 Release",
    "precheck_dirty": "工作区有未提交改动，已停止升级",
    "precheck_disk": "磁盘剩余空间不足",
    "precheck_git": "源码目录 git 状态不可用",
    "precheck_venv": "虚拟环境不可用",
    "precheck_supervisor": "无法确认当前源码服务，拒绝升级",
    "lock_failed": "无法获取升级锁，拒绝执行",
    "fetch_failed": "拉取目标版本失败",
    "install_failed": "依赖或前端构建失败",
    "restart_failed": "源码服务重启失败",
    "health_failed": "升级后 health 检查失败",
    "stale_worker": "升级进程已异常退出，可重试",
    "spawn_failed": "无法启动升级执行器",
    "edition_unsupported": "当前不是源码版 8790，不能走源码一键升级",
    "native_layout_required": "官方安装包请用签名升级，不要走源码拉取",
    "internal_error": "升级内部错误",
}


class SourceUpgradeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    if not _ERROR_RE.fullmatch(code):
        code = "internal_error"
    raise SourceUpgradeError(code)


def _utc_iso(ts: float | None = None) -> str:
    when = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return when.isoformat().replace("+00:00", "Z")


def _store_dir() -> Path:
    runtime_paths.validate_store("upgrade")
    path = runtime_paths.store("upgrade")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _state_path() -> Path:
    return _store_dir() / "source-state.json"


def _lock_path() -> Path:
    return _store_dir() / "source-upgrade.lock"


def _log_dir() -> Path:
    path = _store_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _default_state() -> dict[str, Any]:
    return {
        "job_id": None,
        "state": "idle",
        "engine": ENGINE,
        "target_version": None,
        "target_tag": None,
        "from_version": None,
        "phase": None,
        "error_code": None,
        "error_message": None,
        "created_at": None,
        "updated_at": _utc_iso(),
        "finished_at": None,
        "worker_pid": None,
        "log_path": None,
        "active": False,
        "available": True,
    }


def _edition() -> str:
    return (os.environ.get("COCKPIT_EDITION") or "").strip() or "source"


def is_source_runtime() -> bool:
    if getattr(sys, "frozen", False):
        return False
    if _edition() != "source":
        return False
    return os.environ.get("COCKPIT_NEXT_PROFILE") == "dev"


def _install_dir() -> Path:
    return artifact_root.resolve_artifact_root()


def _cockpit_unit() -> str:
    """Always the live source 8790 unit. Never the upgrade worker's own unit."""
    return SOURCE_UNIT


def read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_state()
        base = _default_state()
        base.update(data)
        return base
    except (OSError, ValueError, TypeError):
        return _default_state()


def write_state(state: dict[str, Any]) -> None:
    directory = _store_dir()
    state = dict(state)
    state["updated_at"] = _utc_iso()
    state.pop("error", None)
    fd, tmp = tempfile.mkstemp(prefix=".source-state.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, _state_path())
        os.chmod(_state_path(), 0o600)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


class UpgradeLock:
    def __init__(self) -> None:
        self._fh: Any = None

    def acquire(self, *, blocking: bool = False) -> bool:
        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fh = open(path, "a+", encoding="utf-8")
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


def _append_log(log_path: str | None, message: str) -> None:
    if not log_path:
        return
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{_utc_iso()} {message}\n")
        os.chmod(path, 0o600)
    except OSError:
        logger.exception("source upgrade log write failed")


def _worker_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _created_ts(created: Any) -> float:
    try:
        return datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def reconcile_stale_state(state: dict[str, Any] | None = None) -> dict[str, Any]:
    st = dict(state or read_state())
    if st.get("state") not in ACTIVE_STATES:
        return st
    pid = st.get("worker_pid")
    if _worker_alive(pid):
        return st
    if (pid in (None, 0)) and st.get("state") == "queued":
        if time.time() - _created_ts(st.get("created_at")) < QUEUED_HANDSHAKE_GRACE_S:
            return st
    lock = UpgradeLock()
    if not lock.acquire(blocking=False):
        return read_state()
    try:
        st = read_state()
        if st.get("state") not in ACTIVE_STATES:
            return st
        if _worker_alive(st.get("worker_pid")):
            return st
        st["state"] = "failed"
        st["error_code"] = "stale_worker"
        st["error_message"] = ERROR_MESSAGES["stale_worker"]
        st["phase"] = "worker_dead"
        st["finished_at"] = _utc_iso()
        st["active"] = False
        write_state(st)
        return st
    finally:
        lock.release()


def public_status(state: dict[str, Any] | None = None) -> dict[str, Any]:
    st = reconcile_stale_state(state)
    code = st.get("error_code")
    msg = ERROR_MESSAGES.get(str(code), None) if code else st.get("error_message")
    available = is_source_runtime()
    reason = None if available else "edition_unsupported"
    return {
        "job_id": st.get("job_id"),
        "state": st.get("state") or "idle",
        "engine": ENGINE,
        "target_version": st.get("target_version"),
        "target_tag": st.get("target_tag"),
        "from_version": st.get("from_version"),
        "phase": st.get("phase"),
        "error_code": code,
        "error_message": msg,
        "created_at": st.get("created_at"),
        "updated_at": st.get("updated_at"),
        "finished_at": st.get("finished_at"),
        "active": st.get("state") in ACTIVE_STATES,
        "available": available and st.get("state") not in ACTIVE_STATES,
        "reason": reason,
        "worker_running": _worker_alive(st.get("worker_pid")),
    }


def get_status() -> dict[str, Any]:
    return public_status()


def _git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=check,
    )


def _require_sha40(sha: str) -> str:
    value = (sha or "").lower().strip()
    if not _SHA40_RE.fullmatch(value):
        raise ValueError("release_unavailable")
    return value


def _target_tag(version_value: str) -> str:
    parts = version.parse_semver(version_value)
    if parts is None:
        raise ValueError("release_unavailable")
    return f"{TAG_PREFIX}{version.format_semver(parts)}"


def _latest_version() -> str:
    try:
        info = version.get_version_info(refresh=True)
    except Exception:
        _fail("release_unavailable")
    if not isinstance(info, dict):
        _fail("release_unavailable")
    status = info.get("status")
    if status == "up_to_date":
        _fail("already_current")
    if status != "update_available":
        _fail("release_unavailable")
    current = info.get("current")
    latest = info.get("latest")
    if not isinstance(current, dict) or not isinstance(latest, dict):
        _fail("release_unavailable")
    current_value = current.get("version")
    latest_value = latest.get("version")
    current_parts = version.parse_semver(current_value)
    latest_parts = version.parse_semver(latest_value)
    if (
        type(current_value) is not str
        or type(latest_value) is not str
        or current_parts is None
        or latest_parts is None
        or version.compare_semver(latest_parts, current_parts) <= 0
    ):
        _fail("release_unavailable")
    return latest_value


def _user_systemd_bus_ok() -> bool:
    if not shutil.which("systemctl"):
        return False
    result = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    err = (result.stderr or "") + (result.stdout or "")
    if result.returncode != 0 and (
        "Failed to connect" in err or "No such file" in err or "not been booted" in err
    ):
        return False
    return True


def _systemctl_show(unit: str, props: list[str]) -> dict[str, str]:
    if not shutil.which("systemctl"):
        return {}
    args = ["systemctl", "--user", "show", unit]
    for prop in props:
        args.extend(["-p", prop])
    result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=10)
    if result.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _pid_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _pid_command(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _pid_is_source_cockpit(pid: int, install_dir: Path) -> bool:
    cmd = _pid_command(pid)
    cwd = _pid_cwd(pid)
    if "server.py" not in cmd and "dev_server.py" not in cmd:
        return False
    if not cwd:
        return False
    try:
        return Path(cwd).resolve() == install_dir.resolve()
    except OSError:
        return False


def preflight_source() -> None:
    if not is_source_runtime():
        raise ValueError("edition_unsupported")
    if getattr(sys, "frozen", False):
        raise ValueError("native_layout_required")
    install = _install_dir()
    if not (install / "server.py").is_file() or not (install / ".git").exists():
        raise ValueError("precheck_git")
    if not (install / ".venv" / "bin" / "python").exists():
        raise ValueError("precheck_venv")
    if not _user_systemd_bus_ok():
        raise ValueError("precheck_supervisor")
    unit = _cockpit_unit()
    props = _systemctl_show(unit, ["ActiveState", "MainPID", "KillMode"])
    if (props.get("ActiveState") or "") != "active":
        raise ValueError("precheck_supervisor")
    try:
        main_pid = int(props.get("MainPID") or "0")
    except ValueError:
        main_pid = 0
    if main_pid <= 1 or not _pid_is_source_cockpit(main_pid, install):
        # systemd-run transient units often report the launcher, not server.py.
        if main_pid > 1:
            cwd = _pid_cwd(main_pid)
            try:
                if cwd and Path(cwd).resolve() == install.resolve():
                    return
            except OSError:
                pass
        raise ValueError("precheck_supervisor")


def precheck_install_dir(install_dir: Path | None = None) -> dict[str, Any]:
    root = install_dir or _install_dir()
    if not (root / "server.py").is_file():
        raise ValueError("precheck_git")
    if not (root / ".venv" / "bin" / "python").exists():
        raise ValueError("precheck_venv")
    try:
        _git(["rev-parse", "--is-inside-work-tree"], root)
    except subprocess.CalledProcessError as exc:
        raise ValueError("precheck_git") from exc
    status = _git(["status", "--porcelain", "-uall"], root, check=False)
    blocking: list[str] = []
    for line in (status.stdout or "").splitlines():
        if not line.strip():
            continue
        path = line[3:].strip() if len(line) > 3 else line
        if path.startswith('"'):
            path = path.strip('"')
        if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in ALLOWED_DIRTY_PREFIXES):
            continue
        blocking.append(path)
    if blocking:
        raise ValueError("precheck_dirty")
    usage = shutil.disk_usage(str(root))
    if usage.free < DISK_MIN_FREE_BYTES:
        raise ValueError("precheck_disk")
    preflight_source()
    return {
        "install_dir": str(root),
        "head": _git(["rev-parse", "HEAD"], root).stdout.strip(),
        "free_bytes": usage.free,
    }


def _resolve_tag_sha(tag: str, install_dir: Path) -> str:
    result = _git(["ls-remote", "--tags", "origin", tag], install_dir, check=False)
    if result.returncode == 0:
        peeled: str | None = None
        plain: str | None = None
        for line in (result.stdout or "").splitlines():
            sha, _, ref = line.partition("\t")
            try:
                digest = _require_sha40(sha)
            except ValueError:
                continue
            if ref.endswith("^{}"):
                peeled = digest
            elif ref.endswith(tag):
                plain = digest
        if peeled or plain:
            return peeled or plain or ""
    api = f"https://api.github.com/repos/fyc0451/agent-cockpit/git/ref/tags/{tag}"
    try:
        import httpx

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-cockpit-source-upgrade",
        }
        with httpx.Client(timeout=15.0) as client:
            response = client.get(api, headers=headers)
            if response.status_code != 200:
                raise ValueError("release_unavailable")
            obj = response.json().get("object") or {}
            sha = str(obj.get("sha") or "")
            if obj.get("type") == "tag":
                annotated = client.get(
                    f"https://api.github.com/repos/fyc0451/agent-cockpit/git/tags/{sha}",
                    headers=headers,
                )
                if annotated.status_code == 200:
                    sha = str((annotated.json().get("object") or {}).get("sha") or "")
        return _require_sha40(sha)
    except Exception as exc:
        raise ValueError("release_unavailable") from exc


def fetch_and_checkout(install_dir: Path, tag: str) -> str:
    sha = _resolve_tag_sha(tag, install_dir)
    fetch = _git(["fetch", "--tags", "origin", f"refs/tags/{tag}:refs/tags/{tag}"], install_dir, check=False)
    if fetch.returncode != 0:
        fetch = _git(
            ["fetch", GITHUB_REPO, f"refs/tags/{tag}:refs/tags/{tag}"],
            install_dir,
            check=False,
        )
        if fetch.returncode != 0:
            raise ValueError("fetch_failed")
    pointed = _git(["rev-parse", f"{tag}^{{}}"], install_dir, check=False)
    if pointed.returncode != 0:
        pointed = _git(["rev-parse", tag], install_dir, check=False)
    actual = (pointed.stdout or "").strip().lower()
    if actual != sha:
        raise ValueError("release_unavailable")
    ancestor = _git(["merge-base", "--is-ancestor", sha, "origin/main"], install_dir, check=False)
    if ancestor.returncode != 0:
        _git(["fetch", "origin", "main"], install_dir, check=False)
        ancestor = _git(["merge-base", "--is-ancestor", sha, "origin/main"], install_dir, check=False)
        if ancestor.returncode != 0:
            raise ValueError("release_unavailable")
    checkout = _git(["checkout", "--force", sha], install_dir, check=False)
    if checkout.returncode != 0:
        raise ValueError("fetch_failed")
    return sha


def install_dependencies(install_dir: Path) -> None:
    python = install_dir / ".venv" / "bin" / "python"
    pip = install_dir / ".venv" / "bin" / "pip"
    req = install_dir / "requirements.txt"
    if req.is_file():
        result = subprocess.run(
            [str(pip), "install", "-r", str(req)],
            cwd=str(install_dir),
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("install_failed")
    web_dir = install_dir / "web"
    if (web_dir / "package.json").is_file():
        npm = shutil.which("npm")
        if not npm:
            raise ValueError("install_failed")
        result = subprocess.run(
            [npm, "run", "build"],
            cwd=str(web_dir),
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "CI": "1"},
        )
        if result.returncode != 0:
            raise ValueError("install_failed")
    if not python.is_file():
        raise ValueError("precheck_venv")


def restart_source_unit() -> None:
    if not _user_systemd_bus_ok() or not shutil.which("systemctl"):
        raise ValueError("restart_failed")
    unit = _cockpit_unit()
    result = subprocess.run(
        ["systemctl", "--user", "restart", unit],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise ValueError("restart_failed")


def health_check(*, timeout_s: float = HEALTH_TIMEOUT_S) -> bool:
    host = os.environ.get("COCKPIT_HOST") or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    try:
        port = int(os.environ.get("COCKPIT_PORT") or "8790")
    except (TypeError, ValueError):
        port = 8790
    url = f"http://{host}:{port}/health/live"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            import httpx

            with httpx.Client(timeout=2.0) as client:
                response = client.get(url)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("status") == "live":
                    return True
        except Exception:
            pass
        time.sleep(HEALTH_POLL_S)
    return False


def _mark(state: dict[str, Any], *, phase: str, job_state: str | None = None) -> dict[str, Any]:
    state["phase"] = phase
    if job_state is not None:
        state["state"] = job_state
        state["active"] = job_state in ACTIVE_STATES
    write_state(state)
    _append_log(state.get("log_path"), f"phase={phase} state={state.get('state')}")
    return state


def _fail_job(state: dict[str, Any], code: str) -> int:
    state["state"] = "failed"
    state["error_code"] = code
    state["error_message"] = ERROR_MESSAGES.get(code, ERROR_MESSAGES["internal_error"])
    state["finished_at"] = _utc_iso()
    state["active"] = False
    write_state(state)
    _append_log(state.get("log_path"), f"failed {code}")
    return 1


def run_job(job_id: str) -> int:
    lock = UpgradeLock()
    if not lock.acquire(blocking=True):
        return 1
    try:
        state = read_state()
        if state.get("job_id") != job_id:
            return 1
        install = _install_dir()
        tag = str(state.get("target_tag") or "")
        try:
            _mark(state, phase="precheck", job_state="prechecking")
            precheck_install_dir(install)
            _mark(state, phase="fetch", job_state="fetching")
            fetch_and_checkout(install, tag)
            _mark(state, phase="install", job_state="installing")
            install_dependencies(install)
            _mark(state, phase="restart", job_state="restarting")
            restart_source_unit()
            _mark(state, phase="health", job_state="verifying")
            if not health_check():
                return _fail_job(state, "health_failed")
            state["state"] = "succeeded"
            state["phase"] = "done"
            state["error_code"] = None
            state["error_message"] = None
            state["finished_at"] = _utc_iso()
            state["active"] = False
            write_state(state)
            _append_log(state.get("log_path"), "succeeded")
            return 0
        except SourceUpgradeError as exc:
            return _fail_job(state, exc.code)
        except ValueError as exc:
            code = str(exc) if _ERROR_RE.fullmatch(str(exc)) else "internal_error"
            return _fail_job(state, code)
        except Exception:
            logger.exception("source upgrade failed")
            return _fail_job(state, "internal_error")
    finally:
        lock.release()


def _worker_env() -> dict[str, str]:
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(_install_dir())}
    env.setdefault("COCKPIT_NEXT_PROFILE", "dev")
    env.setdefault("COCKPIT_EDITION", "source")
    env.setdefault("COCKPIT_HOST", os.environ.get("COCKPIT_HOST") or "127.0.0.1")
    env.setdefault("COCKPIT_PORT", os.environ.get("COCKPIT_PORT") or "8790")
    return env


def spawn_worker(job_id: str) -> int:
    log_path = _log_dir() / f"{job_id}.log"
    install = _install_dir()
    env = _worker_env()
    worker = [
        sys.executable,
        "-m",
        "agent_cockpit.source_upgrade",
        "--job-id",
        job_id,
    ]
    if shutil.which("systemd-run") and _user_systemd_bus_ok():
        unit = f"agent-cockpit-source-upgrade-{job_id}"
        cmd = [
            "systemd-run",
            "--user",
            "--unit",
            unit,
            "--collect",
            "--property=KillMode=process",
            "--working-directory",
            str(install),
        ]
        for key in (
            "PYTHONUNBUFFERED",
            "PYTHONPATH",
            "COCKPIT_NEXT_PROFILE",
            "COCKPIT_EDITION",
            "COCKPIT_HOST",
            "COCKPIT_PORT",
            "COCKPIT_DATA_DIR",
            "COCKPIT_CONFIG_DIR",
            "COCKPIT_STATE_DIR",
            "COCKPIT_UPLOADS_DIR",
            "HOME",
        ):
            value = env.get(key)
            if value:
                cmd.append(f"--setenv={key}={value}")
        cmd.extend(worker)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=str(install))
        if result.returncode == 0:
            return -1
        logger.info("systemd-run source upgrade failed rc=%s", result.returncode)
    log_fh = open(log_path, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            worker,
            cwd=str(install),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    finally:
        log_fh.close()
    return int(proc.pid)


def start_latest() -> dict[str, Any]:
    if not _START_LOCK.acquire(blocking=False):
        _fail("upgrade_in_progress")
    try:
        if not is_source_runtime():
            _fail("edition_unsupported")
        status = public_status()
        if status.get("active"):
            _fail("upgrade_in_progress")
        target = _latest_version()
        tag = _target_tag(target)
        current = version.read_current_version()
        lock = UpgradeLock()
        if not lock.acquire(blocking=False):
            _fail("lock_failed")
        try:
            latest = public_status()
            if latest.get("active"):
                _fail("upgrade_in_progress")
            try:
                precheck_install_dir()
            except ValueError as exc:
                _fail(str(exc) if _ERROR_RE.fullmatch(str(exc)) else "internal_error")
            job_id = f"source-upgrade-{secrets.token_hex(8)}"
            log_path = str(_log_dir() / f"{job_id}.log")
            state = {
                **_default_state(),
                "job_id": job_id,
                "state": "queued",
                "engine": ENGINE,
                "target_version": target,
                "target_tag": tag,
                "from_version": current,
                "phase": "queued",
                "created_at": _utc_iso(),
                "log_path": log_path,
                "active": True,
                "available": False,
            }
            write_state(state)
        finally:
            lock.release()
        try:
            pid = spawn_worker(job_id)
        except Exception:
            failed = read_state()
            if failed.get("job_id") == job_id:
                _fail_job(failed, "spawn_failed")
            _fail("spawn_failed")
        if pid > 1:
            locked = UpgradeLock()
            if locked.acquire(blocking=False):
                try:
                    current_state = read_state()
                    if current_state.get("job_id") == job_id:
                        current_state["worker_pid"] = pid
                        write_state(current_state)
                finally:
                    locked.release()
        return {
            "accepted": True,
            "job_id": job_id,
            "target_version": target,
            "target_tag": tag,
            "engine": ENGINE,
        }
    finally:
        _START_LOCK.release()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Agent Cockpit source-tree upgrade worker")
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    return run_job(args.job_id)


if __name__ == "__main__":
    raise SystemExit(main())
