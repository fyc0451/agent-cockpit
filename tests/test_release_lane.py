from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "release_lane.py"
RECEIPT_FIELDS = {
    "schema_version",
    "release_id",
    "state",
    "expected_main",
    "candidate",
    "rollback_sha",
    "observed_main_before",
    "observed_main_after",
    "started_at",
    "finished_at",
    "publisher_pid",
    "child_returncode",
    "signal",
    "error_code",
    "exit_code",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    path = repo / f"{name}.txt"
    path.write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", path.name)
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def release_repo(tmp_path: Path) -> tuple[Path, str, str]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.name", "Release Lane Test")
    _git(repo, "config", "user.email", "release-lane@example.invalid")
    base = _commit(repo, "base")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    candidate = _commit(repo, "candidate")
    return repo, base, candidate


def _command(
    repo: Path,
    state_dir: Path,
    release_id: str,
    expected: str,
    candidate: str,
    child: list[str],
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "run",
        "--repo",
        str(repo),
        "--state-dir",
        str(state_dir),
        "--release-id",
        release_id,
        "--expected-main",
        expected,
        "--candidate",
        candidate,
        "--",
        *child,
    ]


def _run(
    repo: Path,
    state_dir: Path,
    release_id: str,
    expected: str,
    candidate: str,
    child: list[str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(repo, state_dir, release_id, expected, candidate, child),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _receipt(state_dir: Path, release_id: str) -> dict[str, object]:
    return json.loads((state_dir / "receipts" / f"{release_id}.json").read_text())


def _push_child(candidate: str) -> list[str]:
    return ["git", "push", "origin", f"{candidate}:refs/heads/main"]


def test_success_holds_lane_checks_final_main_and_writes_safe_receipt(
    release_repo: tuple[Path, str, str], tmp_path: Path,
) -> None:
    repo, base, candidate = release_repo
    state_dir = tmp_path / "state"

    result = _run(repo, state_dir, "success", base, candidate, _push_child(candidate))

    assert result.returncode == 0, result.stderr
    receipt = _receipt(state_dir, "success")
    assert set(receipt) == RECEIPT_FIELDS
    assert receipt["state"] == "succeeded"
    assert receipt["expected_main"] == base
    assert receipt["rollback_sha"] == base
    assert receipt["candidate"] == candidate
    assert receipt["observed_main_before"] == base
    assert receipt["observed_main_after"] == candidate
    assert receipt["error_code"] is None
    assert "command" not in receipt
    assert "environment" not in receipt
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    receipt_path = state_dir / "receipts" / "success.json"
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert not list((state_dir / "receipts").glob(".receipt-*"))


def test_stale_expected_main_rejects_before_child(
    release_repo: tuple[Path, str, str], tmp_path: Path,
) -> None:
    repo, base, candidate = release_repo
    _git(repo, "push", "origin", f"{candidate}:refs/heads/main")
    next_candidate = _commit(repo, "next")
    marker = tmp_path / "child-ran"
    child = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]

    result = _run(repo, tmp_path / "state", "stale", base, next_candidate, child)

    assert result.returncode == 2
    assert not marker.exists()
    receipt = _receipt(tmp_path / "state", "stale")
    assert receipt["state"] == "rejected"
    assert receipt["error_code"] == "expected_main_mismatch"
    assert receipt["observed_main_before"] == candidate


def test_second_publisher_is_rejected_while_first_child_runs(
    release_repo: tuple[Path, str, str], tmp_path: Path,
) -> None:
    repo, base, candidate = release_repo
    state_dir = tmp_path / "state"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    helper = tmp_path / "wait_then_push.py"
    helper.write_text(
        "import pathlib, subprocess, sys, time\n"
        "ready = pathlib.Path(sys.argv[1])\n"
        "release = pathlib.Path(sys.argv[2])\n"
        "repo = pathlib.Path(sys.argv[3])\n"
        "candidate = sys.argv[4]\n"
        "ready.touch()\n"
        "while not release.exists(): time.sleep(0.01)\n"
        "subprocess.check_call(['git', '-C', str(repo), 'push', 'origin', "
        "candidate + ':refs/heads/main'])\n",
        encoding="utf-8",
    )
    first = subprocess.Popen(
        _command(
            repo,
            state_dir,
            "first",
            base,
            candidate,
            [sys.executable, str(helper), str(ready), str(release), str(repo), candidate],
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        marker = tmp_path / "second-ran"
        child = [
            sys.executable, "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ]

        second = _run(repo, state_dir, "second", base, candidate, child)

        assert second.returncode == 75
        assert not marker.exists()
        assert _receipt(state_dir, "second")["error_code"] == "lane_busy"
    finally:
        release.touch()
        stdout, stderr = first.communicate(timeout=10)
    assert first.returncode == 0, (stdout, stderr)


def test_child_failure_releases_lane_for_next_release(
    release_repo: tuple[Path, str, str], tmp_path: Path,
) -> None:
    repo, base, candidate = release_repo
    state_dir = tmp_path / "state"

    failed = _run(
        repo, state_dir, "failed", base, candidate,
        [sys.executable, "-c", "raise SystemExit(7)"],
    )
    succeeded = _run(
        repo, state_dir, "retry", base, candidate, _push_child(candidate),
    )

    assert failed.returncode == 7
    assert _receipt(state_dir, "failed")["error_code"] == "release_command_failed"
    assert succeeded.returncode == 0, succeeded.stderr


def test_child_keeps_lock_if_guard_is_killed(
    release_repo: tuple[Path, str, str], tmp_path: Path,
) -> None:
    repo, base, candidate = release_repo
    state_dir = tmp_path / "state"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    helper = tmp_path / "wait.py"
    helper.write_text(
        "import pathlib, sys, time\n"
        "ready, release = map(pathlib.Path, sys.argv[1:])\n"
        "ready.touch()\n"
        "while not release.exists(): time.sleep(0.01)\n",
        encoding="utf-8",
    )
    guard = subprocess.Popen(
        _command(
            repo, state_dir, "killed", base, candidate,
            [sys.executable, str(helper), str(ready), str(release)],
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        os.kill(guard.pid, signal.SIGKILL)
        guard.wait(timeout=5)

        blocked = _run(
            repo, state_dir, "still-busy", base, candidate, ["/usr/bin/true"],
        )
        assert blocked.returncode == 75
        assert _receipt(state_dir, "still-busy")["error_code"] == "lane_busy"
    finally:
        release.touch()

    lock_path = state_dir / "release-lane.lock"
    deadline = time.monotonic() + 5
    while True:
        fd = os.open(lock_path, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    pytest.fail("child did not release inherited lane lock")
        finally:
            os.close(fd)
        time.sleep(0.01)

    retry = _run(repo, state_dir, "after-kill", base, candidate, _push_child(candidate))
    assert retry.returncode == 0, retry.stderr


def test_successful_child_without_push_fails_final_main_check(
    release_repo: tuple[Path, str, str], tmp_path: Path,
) -> None:
    repo, base, candidate = release_repo
    state_dir = tmp_path / "state"

    result = _run(repo, state_dir, "no-push", base, candidate, ["/usr/bin/true"])

    assert result.returncode == 2
    receipt = _receipt(state_dir, "no-push")
    assert receipt["state"] == "failed"
    assert receipt["error_code"] == "final_main_mismatch"
    assert receipt["observed_main_after"] == base


def test_candidate_must_equal_clean_worktree_head(
    release_repo: tuple[Path, str, str], tmp_path: Path,
) -> None:
    repo, base, candidate = release_repo
    _commit(repo, "later")
    marker = tmp_path / "child-ran"
    child = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]

    result = _run(repo, tmp_path / "state", "not-head", base, candidate, child)

    assert result.returncode == 2
    assert not marker.exists()
    assert _receipt(tmp_path / "state", "not-head")["error_code"] == "candidate_not_head"


def test_candidate_must_descend_from_expected_main(
    release_repo: tuple[Path, str, str], tmp_path: Path,
) -> None:
    repo, base, _candidate = release_repo
    _git(repo, "switch", "--orphan", "unrelated")
    unrelated = _commit(repo, "unrelated")
    marker = tmp_path / "child-ran"
    child = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]

    result = _run(repo, tmp_path / "state", "unrelated", base, unrelated, child)

    assert result.returncode == 2
    assert not marker.exists()
    receipt = _receipt(tmp_path / "state", "unrelated")
    assert receipt["error_code"] == "candidate_not_descendant"


def test_failed_receipt_omits_child_command_environment_and_secret(
    release_repo: tuple[Path, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, candidate = release_repo
    state_dir = tmp_path / "state"
    secret = "release-test-secret-should-not-be-persisted"
    monkeypatch.setenv("RELEASE_LANE_TEST_TOKEN", secret)
    child = [
        sys.executable,
        "-c",
        (
            "import os; "
            f"assert os.environ['RELEASE_LANE_TEST_TOKEN'] == {secret!r}; "
            "raise SystemExit(9)"
        ),
    ]

    result = _run(repo, state_dir, "safe-failure", base, candidate, child)

    assert result.returncode == 9
    raw_receipt = (state_dir / "receipts" / "safe-failure.json").read_text()
    assert secret not in raw_receipt
    receipt = json.loads(raw_receipt)
    assert set(receipt) == RECEIPT_FIELDS
    assert receipt["state"] == "failed"
    assert receipt["error_code"] == "release_command_failed"
    assert not list((state_dir / "receipts").glob(".receipt-*"))


def test_broken_state_dir_symlink_is_rejected_without_running_child(
    release_repo: tuple[Path, str, str], tmp_path: Path,
) -> None:
    repo, base, candidate = release_repo
    state_dir = tmp_path / "broken-state"
    state_dir.symlink_to(tmp_path / "missing", target_is_directory=True)
    marker = tmp_path / "child-ran"
    child = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]

    result = _run(repo, state_dir, "broken-state", base, candidate, child)

    assert result.returncode == 2
    assert "release_lane:state_dir_symlink:" in result.stderr
    assert "Traceback" not in result.stderr
    assert not marker.exists()
