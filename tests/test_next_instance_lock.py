"""RUNTIME-001 process-lock safety and lifecycle tests."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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


def test_adopt_inherited_validates_owner_and_sets_close_on_exec(tmp_path: Path) -> None:
    values = profile(tmp_path)
    launcher = instance_lock.InstanceLock(values).acquire()
    assert launcher.fd is not None
    fd, launcher.fd = launcher.fd, None
    owner = instance_lock.InstanceLock.adopt_inherited(values, fd)
    try:
        assert owner.fd == fd
        assert not os.get_inheritable(fd)
        assert owner.read_metadata(current_owner=True)["pid"] == os.getpid()
        with pytest.raises(instance_lock.LockError, match="instance_locked"):
            instance_lock.InstanceLock(values).acquire()
    finally:
        owner.release()


def test_adopt_rejects_forged_unlocked_fd(tmp_path: Path) -> None:
    values = profile(tmp_path)
    lock = instance_lock.InstanceLock(values).acquire()
    assert lock.fd is not None
    lock.release()
    fd = os.open(
        Path(values["COCKPIT_DATA_DIR"]) / instance_lock.LOCK_NAME,
        os.O_RDWR,
    )
    with pytest.raises(instance_lock.LockError, match="lock_not_held"):
        instance_lock.InstanceLock.adopt_inherited(values, fd)
    with pytest.raises(OSError):
        os.fstat(fd)


def test_adopt_rejects_forged_fd_while_another_owner_holds_lock(
    tmp_path: Path,
) -> None:
    values = profile(tmp_path)
    holder = run_helper(values, "hold")
    try:
        wait_acquired(holder)
        path = Path(values["COCKPIT_DATA_DIR"]) / instance_lock.LOCK_NAME
        forged_fd = os.open(path, os.O_RDWR)
        metadata = {
            "version": 1,
            "pid": os.getpid(),
            "process_starttime": instance_lock._process_starttime(),
            "profile_id": instance_lock.profile_id(values),
        }
        os.ftruncate(forged_fd, 0)
        os.write(forged_fd, json.dumps(metadata).encode("ascii"))
        with pytest.raises(instance_lock.LockError, match="lock_fd_forged"):
            instance_lock.InstanceLock.adopt_inherited(values, forged_fd)
        with pytest.raises(OSError):
            os.fstat(forged_fd)
    finally:
        terminate(holder)


def _stub_lifespan_dependencies(monkeypatch: pytest.MonkeyPatch, server: object) -> None:
    async def waiting_loop() -> None:
        import asyncio

        await asyncio.Event().wait()

    monkeypatch.setattr(server, "H0_STATE_MODE", "off")
    monkeypatch.setattr(server, "B0_MODE", "off")
    monkeypatch.setattr(server, "_poll_live_state", waiting_loop)
    monkeypatch.setattr(server, "_poll_message_state", waiting_loop)
    monkeypatch.setattr(server, "_worktree_cleanup_loop", waiting_loop)
    monkeypatch.setattr(server, "_identity_retirement_loop", waiting_loop)
    monkeypatch.setattr(server.tasks, "recover_pending_tasks", lambda: {"skipped": True})
    monkeypatch.setattr(server, "_release_all_zoom_leases", lambda: None)


def test_next_lifespan_requires_adopted_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_cockpit import server

    monkeypatch.setenv("COCKPIT_NEXT_PROFILE", "1")
    monkeypatch.setattr(server, "_next_instance_lock_owner", None)
    with pytest.raises(RuntimeError, match="next_instance_lock_required"), TestClient(
        server.app,
    ):
        pass

    values = profile(tmp_path)
    owner = instance_lock.InstanceLock(values).acquire()
    assert owner.fd is not None
    fd, owner.fd = owner.fd, None
    adopted = instance_lock.InstanceLock.adopt_inherited(values, fd)
    monkeypatch.setattr(server, "_next_instance_lock_owner", adopted)
    _stub_lifespan_dependencies(monkeypatch, server)
    try:
        with TestClient(server.app):
            pass
    finally:
        adopted.release()


def test_production_lifespan_does_not_require_next_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_cockpit import server

    monkeypatch.delenv("COCKPIT_NEXT_PROFILE", raising=False)
    monkeypatch.setattr(server, "_next_instance_lock_owner", None)
    _stub_lifespan_dependencies(monkeypatch, server)
    with TestClient(server.app):
        pass


def test_direct_uvicorn_lifespan_off_fails_before_listening(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "COCKPIT_NEXT_PROFILE": "1",
    }
    result = subprocess.run(
        [
            sys.executable, "-m", "uvicorn", "agent_cockpit.server:app",
            "--lifespan", "off", "--host", "127.0.0.1", "--port", "0",
        ],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=10,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "next_instance_lock_required" in output
    assert "Started server process" not in output
    assert "Uvicorn running" not in output


def test_owner_registry_rejects_invalid_and_replacement(tmp_path: Path) -> None:
    values = profile(tmp_path)
    launcher = instance_lock.InstanceLock(values).acquire()
    with pytest.raises(instance_lock.LockError, match="lock_owner_invalid"):
        instance_lock.register_adopted_owner(instance_lock.InstanceLock(values))
    assert launcher.fd is not None
    fd, launcher.fd = launcher.fd, None
    owner = instance_lock.InstanceLock.adopt_inherited(values, fd)
    previous = instance_lock._adopted_owner
    instance_lock._adopted_owner = None
    try:
        instance_lock.register_adopted_owner(owner)
        assert instance_lock.require_registered_owner() is owner
        replacement_values = profile(tmp_path, "replacement")
        replacement_launcher = instance_lock.InstanceLock(replacement_values).acquire()
        assert replacement_launcher.fd is not None
        replacement_fd, replacement_launcher.fd = replacement_launcher.fd, None
        replacement = instance_lock.InstanceLock.adopt_inherited(
            replacement_values, replacement_fd,
        )
        try:
            with pytest.raises(
                instance_lock.LockError, match="lock_owner_already_registered",
            ):
                instance_lock.register_adopted_owner(replacement)
        finally:
            replacement.release()
    finally:
        instance_lock._adopted_owner = previous
        owner.release()


def test_launcher_exec_server_imports_and_binds_with_adopted_owner(
    tmp_path: Path,
) -> None:
    gate_spec = importlib.util.spec_from_file_location("next_dev", NEXT_DEV)
    assert gate_spec and gate_spec.loader
    gate = importlib.util.module_from_spec(gate_spec)
    gate_spec.loader.exec_module(gate)
    values = gate.expected(tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    values["COCKPIT_PORT"] = str(port)
    Path(values["COCKPIT_DATA_DIR"]).mkdir(parents=True)
    Path(values["COCKPIT_CONFIG_DIR"]).mkdir(parents=True)
    env_file = tmp_path / "next.env"
    env_file.write_text("ignored=1\n", encoding="ascii")
    child_code = """
import os, runpy
from agent_cockpit import next_profile
next_profile.validate_server_environment = lambda *_args, **_kwargs: None
runpy.run_path(os.environ["NEXT_SERVER"], run_name="__main__")
"""
    launcher_code = """
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location(
    "next_dev_handoff", os.environ["NEXT_DEV"],
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
values = json.loads(os.environ["LOCK_PROFILE"])
gate.validate = lambda *_args, **_kwargs: values
gate._unit_not_installed = lambda: True
gate._port_available = lambda *_args: True
gate.ensure_runtime_roots = lambda _values: None
gate.Path.is_file = lambda _path: True
real_execve = os.execve
def exec_server(_executable, _argv, environment):
    real_execve(
        sys.executable,
        [sys.executable, "-c", os.environ["CHILD_CODE"]],
        environment,
    )
gate.os.execve = exec_server
raise SystemExit(gate.main(["start", "--env-file", os.environ["LOCK_ENV_FILE"]]))
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "LOCK_PROFILE": json.dumps(values),
        "LOCK_ENV_FILE": str(env_file),
        "NEXT_DEV": str(NEXT_DEV),
        "NEXT_SERVER": str(ROOT / "server.py"),
        "CHILD_CODE": child_code,
    }
    process = subprocess.Popen(
        [sys.executable, "-c", launcher_code],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        import time

        deadline = time.monotonic() + 15
        connected = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    connected = True
                    break
            except OSError:
                time.sleep(0.05)
        assert connected, process.stderr.read() if process.stderr else ""
    finally:
        if process.poll() is None:
            process.terminate()
        stdout, stderr = process.communicate(timeout=10)
    assert "Uvicorn running" in stdout + stderr


@pytest.mark.parametrize("field", ("pid", "process_starttime", "profile_id"))
def test_adopt_rejects_owner_metadata_mismatch(
    tmp_path: Path, field: str,
) -> None:
    values = profile(tmp_path)
    launcher = instance_lock.InstanceLock(values).acquire()
    assert launcher.fd is not None
    metadata = launcher.read_metadata()
    fd, launcher.fd = launcher.fd, None
    metadata[field] = {
        "pid": os.getpid() + 1,
        "process_starttime": "1",
        "profile_id": "sha256:" + "0" * 64,
    }[field]
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, json.dumps(metadata).encode("ascii"))
    with pytest.raises(instance_lock.LockError):
        instance_lock.InstanceLock.adopt_inherited(values, fd)
    with pytest.raises(OSError):
        os.fstat(fd)


def test_exec_handoff_does_not_leak_lock_to_exec_child(tmp_path: Path) -> None:
    values = profile(tmp_path)
    values.update(COCKPIT_HOST="127.0.0.1", COCKPIT_PORT="1")
    code = """
import importlib.util, json, os, sys, time
from agent_cockpit.instance_lock import LOCK_FD_ENV, InstanceLock
values = json.loads(os.environ["LOCK_PROFILE"])
if os.environ.get("LOCK_STAGE") == "launcher":
    spec = importlib.util.spec_from_file_location("next_dev_handoff", os.environ["NEXT_DEV"])
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    gate.validate = lambda *_args, **_kwargs: values
    gate._unit_not_installed = lambda: True
    gate._port_available = lambda *_args: True
    gate.ensure_runtime_roots = lambda _values: None
    gate.Path.is_file = lambda _path: True
    real_execve = os.execve
    def exec_helper(_executable, _argv, environment):
        environment["LOCK_STAGE"] = "owner"
        real_execve(sys.executable, [sys.executable, "-c", environment["LOCK_CODE"]], environment)
    gate.os.execve = exec_helper
    raise SystemExit(gate.main(["start", "--env-file", os.environ["LOCK_ENV_FILE"]]))
fd = int(os.environ.pop(LOCK_FD_ENV))
owner = InstanceLock.adopt_inherited(values, fd)
child = os.fork()
if child == 0:
    null_fd = os.open(os.devnull, os.O_RDWR)
    os.dup2(null_fd, 1)
    os.dup2(null_fd, 2)
    os.close(null_fd)
    os.execve(sys.executable, [sys.executable, "-c", "import time; time.sleep(30)"], os.environ)
print(json.dumps({"pid": os.getpid(), "child": child, "inheritable": os.get_inheritable(fd)}), flush=True)
time.sleep(30)
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "LOCK_PROFILE": json.dumps(values),
        "LOCK_STAGE": "launcher",
        "LOCK_CODE": code,
        "LOCK_ENV_FILE": str(tmp_path / "next.env"),
        "NEXT_DEV": str(NEXT_DEV),
        instance_lock.LOCK_FD_ENV: "999999",
    }
    Path(env["LOCK_ENV_FILE"]).write_text("ignored=1\n", encoding="ascii")
    owner = subprocess.Popen(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    child_pid = None
    try:
        assert owner.stdout is not None
        output = owner.stdout.readline()
        if not output:
            _, stderr = owner.communicate(timeout=5)
            pytest.fail(f"launcher handoff failed: {stderr}")
        handoff = json.loads(output)
        child_pid = handoff["child"]
        assert handoff["inheritable"] is False
        contender = run_helper(values, "hold")
        stdout, _ = contender.communicate(timeout=5)
        assert contender.returncode == 23
        assert stdout.strip() == "instance_locked"

        owner.kill()
        owner.wait(timeout=5)
        replacement = run_helper(values, "hold")
        try:
            wait_acquired(replacement)
        finally:
            terminate(replacement)
    finally:
        terminate(owner)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


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
