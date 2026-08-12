"""RUNTIME-001 process-lock safety and lifecycle tests."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cockpit import instance_lock


ROOT = Path(__file__).resolve().parents[1]
NEXT_DEV = ROOT / "scripts" / "next_dev.py"


def profile(tmp_path: Path, name: str = "one") -> dict[str, str]:
    data = tmp_path / name / "data"
    config = tmp_path / name / "config"
    data.mkdir(parents=True)
    config.mkdir(parents=True)
    return {"COCKPIT_DATA_DIR": str(data), "COCKPIT_CONFIG_DIR": str(config)}


def run_helper(
    values: dict[str, str], action: str, *, pass_fds: tuple[int, ...] = (),
) -> subprocess.Popen[str]:
    code = """
import json, os, sys, time
from agent_cockpit.instance_lock import InstanceLock, LockError
values = json.loads(os.environ["LOCK_PROFILE"])
if "LOCK_START_FD" in os.environ:
    os.read(int(os.environ["LOCK_START_FD"]), 1)
try:
    lock = InstanceLock(values).acquire()
except LockError as exc:
    print(exc.code, flush=True)
    raise SystemExit(23)
print("acquired", flush=True)
if os.environ["LOCK_ACTION"] == "exec":
    os.execve(sys.executable, [sys.executable, "-c", "import time; time.sleep(30)"], os.environ)
if os.environ["LOCK_ACTION"] == "race":
    time.sleep(1)
    raise SystemExit(0)
time.sleep(30)
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "HOME": str(Path(values["COCKPIT_DATA_DIR"]).parent),
        "TMPDIR": str(Path(values["COCKPIT_DATA_DIR"]).parent),
        "LOCK_PROFILE": json.dumps(values),
        "LOCK_ACTION": action,
    }
    if pass_fds:
        env["LOCK_START_FD"] = str(pass_fds[0])
    return subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=pass_fds,
    )


def wait_acquired(process: subprocess.Popen[str]) -> None:
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "acquired"


def terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)


def test_two_real_processes_compete_for_same_profile(tmp_path: Path) -> None:
    values = profile(tmp_path)
    read_fd, write_fd = os.pipe()
    first = run_helper(values, "race", pass_fds=(read_fd,))
    second = run_helper(values, "race", pass_fds=(read_fd,))
    try:
        os.close(read_fd)
        os.write(write_fd, b"12")
        first_stdout, _ = first.communicate(timeout=5)
        second_stdout, _ = second.communicate(timeout=5)
        assert sorted((first.returncode, second.returncode)) == [0, 23]
        assert sorted((first_stdout.strip(), second_stdout.strip())) == [
            "acquired", "instance_locked",
        ]
    finally:
        os.close(write_fd)
        terminate(first)
        terminate(second)


def test_lock_survives_exec_and_different_profile_runs(tmp_path: Path) -> None:
    values = profile(tmp_path)
    holder = run_helper(values, "exec")
    other = None
    try:
        wait_acquired(holder)
        contender = run_helper(values, "hold")
        stdout, _ = contender.communicate(timeout=5)
        assert contender.returncode == 23
        assert stdout.strip() == "instance_locked"

        other = run_helper(profile(tmp_path, "two"), "hold")
        wait_acquired(other)
    finally:
        terminate(holder)
        if other is not None:
            terminate(other)


def test_symlink_alias_has_same_profile_identity(tmp_path: Path) -> None:
    values = profile(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(Path(values["COCKPIT_DATA_DIR"]).parent, target_is_directory=True)
    aliased = {
        "COCKPIT_DATA_DIR": str(alias / "data"),
        "COCKPIT_CONFIG_DIR": str(alias / "config"),
    }
    assert instance_lock.profile_id(values) == instance_lock.profile_id(aliased)
    holder = run_helper(values, "hold")
    try:
        wait_acquired(holder)
        contender = run_helper(aliased, "hold")
        stdout, _ = contender.communicate(timeout=5)
        assert contender.returncode == 23
        assert stdout.strip() == "instance_locked"
    finally:
        terminate(holder)


def test_sigkill_releases_lock_immediately(tmp_path: Path) -> None:
    values = profile(tmp_path)
    holder = run_helper(values, "hold")
    wait_acquired(holder)
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=5)
    replacement = run_helper(values, "hold")
    try:
        wait_acquired(replacement)
    finally:
        terminate(replacement)


def test_stale_metadata_is_replaced_when_kernel_lock_is_free(tmp_path: Path) -> None:
    values = profile(tmp_path)
    path = Path(values["COCKPIT_DATA_DIR"]) / instance_lock.LOCK_NAME
    path.write_text("not json", encoding="ascii")
    path.chmod(0o600)
    lock = instance_lock.InstanceLock(values).acquire()
    try:
        metadata = lock.read_metadata()
        assert metadata["pid"] == os.getpid()
        assert metadata["profile_id"] == instance_lock.profile_id(values)
    finally:
        lock.release()


def test_metadata_partial_writes_are_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = profile(tmp_path)
    real_write = instance_lock.os.write

    def partial(fd: int, payload: bytes) -> int:
        return real_write(fd, payload[: max(1, len(payload) // 2)])

    monkeypatch.setattr(instance_lock.os, "write", partial)
    lock = instance_lock.InstanceLock(values).acquire()
    try:
        assert lock.read_metadata()["version"] == 1
    finally:
        lock.release()


def test_metadata_failure_closes_fd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = profile(tmp_path)
    opened: list[int] = []
    real_open = instance_lock.os.open

    def record_open(*args: object, **kwargs: object) -> int:
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(instance_lock.os, "open", record_open)
    monkeypatch.setattr(
        instance_lock.os, "ftruncate", lambda *_: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(instance_lock.LockError, match="lock_metadata_write_failed"):
        instance_lock.InstanceLock(values).acquire()
    assert opened
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_file_validation_failure_closes_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = profile(tmp_path)
    opened: list[int] = []
    real_open = instance_lock.os.open

    def record_open(*args: object, **kwargs: object) -> int:
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(instance_lock.os, "open", record_open)
    monkeypatch.setattr(
        instance_lock.os, "fstat", lambda _fd: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(instance_lock.LockError, match="lock_file_validation_failed"):
        instance_lock.InstanceLock(values).acquire()
    assert opened
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize(
    "metadata",
    [
        b"not json",
        b"[]",
        b'{"version":1,"pid":"1","process_starttime":"1","profile_id":"x"}',
        b'{"version":1,"pid":1,"process_starttime":1,"profile_id":"x"}',
        b'{"version":1,"pid":1,"process_starttime":"1","profile_id":"x"}',
    ],
)
def test_invalid_metadata_shape_fails_closed(tmp_path: Path, metadata: bytes) -> None:
    values = profile(tmp_path)
    lock = instance_lock.InstanceLock(values).acquire()
    assert lock.fd is not None
    try:
        os.ftruncate(lock.fd, 0)
        os.lseek(lock.fd, 0, os.SEEK_SET)
        os.write(lock.fd, metadata)
        with pytest.raises(instance_lock.LockError, match="lock_metadata_invalid"):
            lock.read_metadata()
    finally:
        lock.release()


def test_unsafe_lock_files_fail_closed(tmp_path: Path) -> None:
    for kind in ("mode", "symlink", "hardlink", "fifo"):
        values = profile(tmp_path, kind)
        path = Path(values["COCKPIT_DATA_DIR"]) / instance_lock.LOCK_NAME
        if kind == "mode":
            path.touch()
            path.chmod(0o644)
        elif kind == "symlink":
            target = tmp_path / kind / "target"
            target.touch(mode=0o600)
            path.symlink_to(target)
        elif kind == "hardlink":
            target = tmp_path / kind / "target"
            target.touch(mode=0o600)
            os.link(target, path)
        else:
            os.mkfifo(path, mode=0o600)
        with pytest.raises(instance_lock.LockError):
            instance_lock.InstanceLock(values).acquire()


def test_uid_mismatch_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = profile(tmp_path)
    path = Path(values["COCKPIT_DATA_DIR"]) / instance_lock.LOCK_NAME
    path.touch(mode=0o600)
    real_uid = os.getuid()
    monkeypatch.setattr(instance_lock.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(instance_lock.LockError, match="lock_file_unsafe"):
        instance_lock.InstanceLock(values).acquire()


def next_dev_module():
    spec = importlib.util.spec_from_file_location("next_dev_lock_test", NEXT_DEV)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_start_exec_failure_releases_lock_and_preserves_other_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = next_dev_module()
    values = gate.expected(tmp_path)
    gate.ensure_runtime_roots(values)
    marker = tmp_path / "repo" / ".agent-memory-project"
    marker.parent.mkdir()
    marker.write_text("agent-cockpit-next\n", encoding="ascii")
    env_file = tmp_path / "next.env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="ascii",
    )
    extra_fd = os.open(tmp_path / "unrelated", os.O_RDWR | os.O_CREAT, 0o600)
    os.set_inheritable(extra_fd, True)
    monkeypatch.setattr(gate, "validate", lambda *_args, **_kwargs: values)
    monkeypatch.setattr(gate, "_unit_not_installed", lambda: True)
    monkeypatch.setattr(gate, "_port_available", lambda *_: True)
    monkeypatch.setattr(gate.Path, "is_file", lambda _self: True)
    monkeypatch.setattr(gate.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        gate.os, "execve", lambda *_: (_ for _ in ()).throw(OSError("boom")),
    )
    try:
        with pytest.raises(OSError, match="boom"):
            gate.main(["start", "--env-file", str(env_file)])
        os.fstat(extra_fd)
        assert not os.get_inheritable(extra_fd)
        lock = instance_lock.InstanceLock(values).acquire()
        lock.release()
    finally:
        os.close(extra_fd)


def test_check_does_not_create_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = next_dev_module()
    values = gate.expected(tmp_path)
    env_file = tmp_path / "next.env"
    env_file.write_text("ignored=1\n", encoding="ascii")
    monkeypatch.setattr(gate, "validate", lambda *_args, **_kwargs: values)
    assert gate.main(["check", "--env-file", str(env_file)]) == 0
    assert not (Path(values["COCKPIT_DATA_DIR"]) / instance_lock.LOCK_NAME).exists()
