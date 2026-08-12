#!/usr/bin/env python3
"""Serialize managed releases and reject a stale main baseline before mutation."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GIT_TIMEOUT_SECONDS = 30
EXIT_REJECTED = 2
EXIT_BUSY = 75


class LaneError(RuntimeError):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_REJECTED):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def _secure_dir(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise LaneError("state_dir_not_absolute", "state directory must be absolute")
    target = Path(os.path.abspath(expanded))
    try:
        info = target.lstat()
    except FileNotFoundError:
        try:
            target.mkdir(parents=True, mode=0o700)
            info = target.lstat()
        except OSError as exc:
            raise LaneError(
                "state_dir_unavailable", "state directory could not be created",
            ) from exc
    except OSError as exc:
        raise LaneError(
            "state_dir_unavailable", "state directory could not be inspected",
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise LaneError("state_dir_symlink", "state directory must not be a symlink")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise LaneError("state_dir_unsafe", "state directory is not owned by this user")
    try:
        os.chmod(target, 0o700)
    except OSError as exc:
        raise LaneError(
            "state_dir_unavailable", "state directory permissions could not be secured",
        ) from exc
    return target


def _default_state_dir() -> Path:
    override = os.environ.get("AGENT_COCKPIT_RELEASE_STATE_DIR")
    if override:
        return Path(override)
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "agent-cockpit" / "release-lane"


def _atomic_create_json(path: Path, payload: dict[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _atomic_replace_json(path: Path, payload: dict[str, Any]) -> None:
    fd, raw_tmp = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise LaneError("lock_file_unsafe", "release lock is not a user-owned file")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LaneError(
                    "lane_busy", "another managed release is active", EXIT_BUSY,
                ) from exc
            raise
        return fd
    except BaseException:
        os.close(fd)
        raise


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise LaneError("git_timeout", "git preflight timed out") from exc
    if result.returncode != 0:
        raise LaneError("git_failed", "git preflight failed")
    return result.stdout.strip()


def _remote_main(repo: Path) -> str:
    lines = _git(repo, "ls-remote", "--exit-code", "origin", "refs/heads/main").splitlines()
    matches = [line.split() for line in lines if line.endswith("refs/heads/main")]
    if len(matches) != 1 or len(matches[0]) != 2 or not SHA_RE.fullmatch(matches[0][0]):
        raise LaneError("remote_main_invalid", "origin/main did not resolve to one commit")
    return matches[0][0]


def _validate_repo(repo: Path) -> Path:
    expanded = repo.expanduser()
    if not expanded.is_absolute():
        raise LaneError("repo_not_absolute", "repository path must be absolute")
    root = Path(os.path.abspath(expanded))
    if not root.is_dir():
        raise LaneError("repo_missing", "repository path does not exist")
    top = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root.resolve():
        raise LaneError("repo_not_root", "repository path must be the worktree root")
    if _git(root, "status", "--porcelain"):
        raise LaneError("repo_dirty", "release worktree must be clean")
    return root


def _validate_sha(value: str, name: str) -> str:
    normalized = value.lower()
    if not SHA_RE.fullmatch(normalized):
        raise LaneError(f"{name}_invalid", f"{name} must be a full commit SHA")
    return normalized


def _validate_candidate(repo: Path, expected: str, candidate: str) -> None:
    resolved = _git(repo, "rev-parse", "--verify", f"{candidate}^{{commit}}")
    if resolved != candidate:
        raise LaneError("candidate_unresolved", "candidate does not resolve exactly")
    if _git(repo, "rev-parse", "HEAD") != candidate:
        raise LaneError("candidate_not_head", "release worktree HEAD must equal candidate")
    if candidate == expected:
        raise LaneError("candidate_not_new", "candidate must differ from expected main")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", expected, candidate],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT_SECONDS,
        env=env,
    )
    if result.returncode != 0:
        raise LaneError("candidate_not_descendant", "candidate is not based on expected main")


def _run_child(command: Sequence[str], repo: Path, lock_fd: int) -> tuple[int, int | None]:
    try:
        process = subprocess.Popen(
            list(command), cwd=repo, pass_fds=(lock_fd,), start_new_session=True,
        )
    except OSError as exc:
        raise LaneError("command_start_failed", "release command could not start") from exc

    received_signal: int | None = None
    previous: dict[int, Any] = {}

    def forward(signum: int, _frame: Any) -> None:
        nonlocal received_signal
        received_signal = signum
        if process.poll() is None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[signum] = signal.signal(signum, forward)
    try:
        returncode = process.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if received_signal is not None:
        return 128 + received_signal, received_signal
    if returncode < 0:
        return 128 + abs(returncode), abs(returncode)
    return returncode, None


def _finish(
    receipt_path: Path,
    receipt: dict[str, Any],
    *,
    state: str,
    error_code: str | None,
    exit_code: int,
) -> int:
    receipt.update({
        "state": state,
        "error_code": error_code,
        "exit_code": exit_code,
        "finished_at": _utc_now(),
    })
    try:
        _atomic_replace_json(receipt_path, receipt)
    except OSError:
        print(
            json.dumps({
                "state": "failed",
                "error_code": "receipt_write_failed",
                "receipt": None,
            }),
            file=sys.stderr,
        )
        return 1
    stream = sys.stdout if state == "succeeded" else sys.stderr
    print(
        json.dumps({"state": state, "error_code": error_code, "receipt": str(receipt_path)}),
        file=stream,
    )
    return exit_code


def run_release(args: argparse.Namespace) -> int:
    if not RELEASE_ID_RE.fullmatch(args.release_id):
        raise LaneError("release_id_invalid", "release id has an invalid format")
    expected = _validate_sha(args.expected_main, "expected_main")
    candidate = _validate_sha(args.candidate, "candidate")
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise LaneError("command_missing", "a release command is required after --")

    state_dir = _secure_dir(Path(args.state_dir) if args.state_dir else _default_state_dir())
    receipts_dir = _secure_dir(state_dir / "receipts")
    receipt_path = receipts_dir / f"{args.release_id}.json"
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "release_id": args.release_id,
        "state": "starting",
        "expected_main": expected,
        "candidate": candidate,
        "rollback_sha": expected,
        "observed_main_before": None,
        "observed_main_after": None,
        "started_at": _utc_now(),
        "finished_at": None,
        "publisher_pid": os.getpid(),
        "child_returncode": None,
        "signal": None,
        "error_code": None,
        "exit_code": None,
    }
    try:
        _atomic_create_json(receipt_path, receipt)
    except FileExistsError as exc:
        raise LaneError("release_id_exists", "release id already has a receipt") from exc
    except OSError as exc:
        raise LaneError("receipt_create_failed", "release receipt could not be created") from exc

    lock_fd: int | None = None
    try:
        try:
            lock_fd = _open_lock(state_dir / "release-lane.lock")
        except LaneError as exc:
            return _finish(
                receipt_path, receipt, state="rejected",
                error_code=exc.code, exit_code=exc.exit_code,
            )

        try:
            repo = _validate_repo(Path(args.repo))
            observed = _remote_main(repo)
            receipt["observed_main_before"] = observed
            if observed != expected:
                raise LaneError("expected_main_mismatch", "origin/main changed before release")
            _validate_candidate(repo, expected, candidate)
        except LaneError as exc:
            return _finish(
                receipt_path, receipt, state="rejected",
                error_code=exc.code, exit_code=exc.exit_code,
            )

        receipt["state"] = "running"
        _atomic_replace_json(receipt_path, receipt)
        child_returncode, received_signal = _run_child(command, repo, lock_fd)
        receipt["child_returncode"] = child_returncode
        receipt["signal"] = received_signal
        try:
            receipt["observed_main_after"] = _remote_main(repo)
        except LaneError:
            receipt["observed_main_after"] = None

        if child_returncode != 0:
            return _finish(
                receipt_path, receipt, state="failed",
                error_code="release_command_failed", exit_code=child_returncode,
            )
        if receipt["observed_main_after"] != candidate:
            return _finish(
                receipt_path, receipt, state="failed",
                error_code="final_main_mismatch", exit_code=EXIT_REJECTED,
            )
        return _finish(
            receipt_path, receipt, state="succeeded", error_code=None, exit_code=0,
        )
    except LaneError as exc:
        return _finish(
            receipt_path, receipt, state="failed",
            error_code=exc.code, exit_code=exc.exit_code,
        )
    except Exception:
        return _finish(
            receipt_path, receipt, state="failed",
            error_code="internal_error", exit_code=1,
        )
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    run = subparsers.add_parser("run", help="run one complete release under the lane lock")
    run.add_argument("--repo", required=True, help="absolute clean release worktree root")
    run.add_argument("--expected-main", required=True, help="full origin/main SHA before release")
    run.add_argument("--candidate", required=True, help="full descendant candidate SHA")
    run.add_argument("--release-id", required=True, help="unique non-secret receipt id")
    run.add_argument("--state-dir", help="absolute lock/receipt directory")
    run.add_argument("command", nargs=argparse.REMAINDER, help="command after --")
    run.set_defaults(handler=run_release)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except LaneError as exc:
        print(f"release_lane:{exc.code}:{exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
