#!/usr/bin/env python3
"""Ordinary TERM-003 live/E2E harness.

Starts one real Cockpit Next ephemeral process on a random loopback port with a
private 0700 runtime root (data/config/state/uploads). Does not bind 8790/18790,
does not fake the backend, and does not put cwd/command/PID/env/Herdr values
into the browser. Cleanup always signals only the started process group.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "next_ephemeral_server.py"
WEB = ROOT / "web"
RESERVED_PORTS = {8790, 18790}
FORBIDDEN_BROWSER_KEYS = (
    "cwd",
    "command",
    "pid",
    "env",
    "herdr_session",
    "herdr_pane",
    "HERDR_SESSION",
    "HERDR_PANE_ID",
    "HERDR_ENV",
)


class HarnessError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def ensure_runtime_python() -> None:
    try:
        import fastapi  # noqa: F401
    except ImportError:
        if os.environ.get("TERM003_LIVE_BOOTSTRAPPED") == "1":
            raise HarnessError("fastapi_missing")
        env = os.environ.copy()
        env["TERM003_LIVE_BOOTSTRAPPED"] = "1"
        raise SystemExit(
            subprocess.call(
                [
                    "uv",
                    "run",
                    "--isolated",
                    "--no-project",
                    "--with-requirements",
                    str(ROOT / "requirements-dev.txt"),
                    "python",
                    str(Path(__file__).resolve()),
                    *sys.argv[1:],
                ],
                cwd=str(ROOT),
                env=env,
            )
        )


def _owned_process_group(process: subprocess.Popen[str]) -> int | None:
    try:
        process_group = os.getpgid(process.pid)
    except ProcessLookupError:
        return None
    if process_group != process.pid:
        raise HarnessError("ephemeral_process_group_mismatch")
    return process_group


def _port(descriptor: dict[str, object]) -> int:
    base_url = descriptor.get("base_url")
    if not isinstance(base_url, str) or not base_url.startswith("http://127.0.0.1:"):
        raise HarnessError("ephemeral_base_url_invalid")
    port = int(base_url.rsplit(":", 1)[1])
    if port in RESERVED_PORTS:
        raise HarnessError("ephemeral_reserved_port")
    return port


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _http_json(url: str, timeout: float = 2) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise HarnessError("ephemeral_http_not_object")
    return payload


def wait_ready(descriptor: dict[str, object], timeout: float = 20) -> dict[str, object]:
    base_url = str(descriptor["base_url"])
    ready_path = str(descriptor["ready_path"])
    deadline = time.monotonic() + timeout
    last_error = "not_attempted"
    while time.monotonic() < deadline:
        try:
            return _http_json(base_url + ready_path, timeout=1)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, HarnessError) as exc:
            last_error = type(exc).__name__
            time.sleep(0.05)
    raise HarnessError(f"ephemeral_not_ready:{last_error}")


def stop_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Signal the owned pgid first, then communicate. Never read pipes while live."""
    stdout = ""
    stderr = ""
    try:
        if process.poll() is None:
            process_group = _owned_process_group(process)
            if process_group is not None:
                os.killpg(process_group, signal.SIGTERM)
        try:
            out, err = process.communicate(timeout=5)
            stdout, stderr = out or "", err or ""
        except subprocess.TimeoutExpired:
            process_group = _owned_process_group(process)
            if process_group is not None:
                os.killpg(process_group, signal.SIGKILL)
            out, err = process.communicate(timeout=5)
            stdout, stderr = out or "", err or ""
        return stdout, stderr
    finally:
        if process.poll() is None:
            try:
                process_group = _owned_process_group(process)
            except HarnessError:
                process_group = None
            if process_group is not None:
                os.killpg(process_group, signal.SIGKILL)
            try:
                out, err = process.communicate(timeout=5)
                stdout = stdout or out or ""
                stderr = stderr or err or ""
            except Exception:
                pass
    return stdout, stderr


def start_server(runtime_root: Path) -> tuple[subprocess.Popen[str], dict[str, object], dict[str, object]]:
    if not runtime_root.is_absolute():
        raise HarnessError("runtime_root_not_absolute")
    process = subprocess.Popen(
        [sys.executable, str(LAUNCHER), "--runtime-root", str(runtime_root)],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    reaped = False
    try:
        if process.stdout is None:
            raise HarnessError("ephemeral_stdout_missing")
        readable, _, _ = select.select([process.stdout], [], [], 15)
        if not readable:
            _stdout, stderr = stop_process(process)
            reaped = True
            raise HarnessError(f"ephemeral_descriptor_timeout:{(stderr or '')[:500]}")
        line = process.stdout.readline()
        descriptor = json.loads(line)
        if set(descriptor) != {
            "schema_version",
            "state",
            "base_url",
            "pid",
            "ready_path",
            "ready_token",
        }:
            raise HarnessError("ephemeral_descriptor_keys")
        _port(descriptor)
        ready = wait_ready(descriptor)
        return process, descriptor, ready
    except BaseException:
        if not reaped:
            stop_process(process)
        raise


def seed_discoverable_project(runtime_root: Path) -> Path:
    """Create a real git work inside ephemeral HOME. Browser never sees this path."""
    home = runtime_root / "home"
    sample = home / "term003-live-seed"
    sample.mkdir(parents=True, exist_ok=True)
    (sample / "README.md").write_text("# TERM-003 live seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(sample),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(sample),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=term003@local",
            "-c",
            "user.name=term003",
            "commit",
            "--quiet",
            "-m",
            "seed",
        ],
        cwd=str(sample),
        check=True,
        capture_output=True,
        text=True,
    )
    return sample


def ensure_web_build() -> None:
    index = WEB / "dist" / "index.html"
    if index.is_file():
        return
    subprocess.run(
        ["npm", "--prefix", str(WEB), "run", "build"],
        cwd=str(ROOT),
        check=True,
    )
    if not index.is_file():
        raise HarnessError("next_web_build_unavailable")


def empty_registry(base_url: str) -> dict[str, object]:
    payload = _http_json(base_url + "/api/project-registry/projects", timeout=5)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HarnessError("registry_shape")
    items = data.get("items")
    if items != []:
        raise HarnessError("registry_not_empty")
    return payload


def run_playwright(*, base_url: str, artifact_dir: Path) -> int:
    env = os.environ.copy()
    env["PLAYWRIGHT_LIVE_BASE_URL"] = base_url
    env["PLAYWRIGHT_LIVE_ARTIFACT_DIR"] = str(artifact_dir)
    for key in ("HERDR_ENV", "HERDR_SESSION", "HERDR_PANE_ID", "COCKPIT_B0_MODE"):
        env.pop(key, None)
    proc = subprocess.run(
        [
            "npx",
            "playwright",
            "test",
            "-c",
            "playwright.live.config.ts",
        ],
        cwd=str(WEB),
        env=env,
        check=False,
    )
    return proc.returncode


def self_check() -> None:
    artifact = Path(os.environ.get("TERM003_LIVE_ARTIFACT", "/tmp/term003-live-self-check"))
    if artifact.exists():
        shutil.rmtree(artifact)
    artifact.mkdir(parents=True, exist_ok=True)
    runtime = artifact / "runtime"
    runtime.mkdir(mode=0o700)
    process = None
    try:
        process, descriptor, ready = start_server(runtime)
        seed_discoverable_project(runtime)
        empty_registry(str(descriptor["base_url"]))
        if ready.get("ready") is not True:
            raise HarnessError("ephemeral_ready_false")
        _write_json(artifact / "descriptor.json", descriptor)
        _write_json(artifact / "ready.json", ready)
        print("SELF-CHECK PASS", flush=True)
    finally:
        if process is not None:
            stop_process(process)
        shutil.rmtree(runtime, ignore_errors=True)


def run_suite(*, keep_on_fail: bool) -> int:
    ensure_web_build()
    artifact = Path(
        os.environ.get("TERM003_LIVE_ARTIFACT", f"/tmp/term003-live-{os.getpid()}")
    )
    if artifact.exists():
        shutil.rmtree(artifact)
    artifact.mkdir(parents=True, exist_ok=True)
    runtime = artifact / "runtime"
    runtime.mkdir(mode=0o700)
    process = None
    failed = False
    try:
        process, descriptor, ready = start_server(runtime)
        seed_discoverable_project(runtime)
        empty_registry(str(descriptor["base_url"]))
        _write_json(artifact / "descriptor.json", descriptor)
        _write_json(artifact / "ready.json", ready)
        code = run_playwright(base_url=str(descriptor["base_url"]), artifact_dir=artifact)
        if code != 0:
            failed = True
        return code
    except BaseException:
        failed = True
        raise
    finally:
        if process is not None:
            _stdout, stderr = stop_process(process)
            if failed:
                (artifact / "server.stderr.log").write_text(stderr or "", encoding="utf-8")
        if not (failed and keep_on_fail):
            shutil.rmtree(runtime, ignore_errors=True)
        elif failed:
            print(f"kept artifact {artifact}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--keep-on-fail", action="store_true")
    args = parser.parse_args(argv)
    try:
        ensure_runtime_python()
        if args.self_check:
            self_check()
            return 0
        return run_suite(keep_on_fail=args.keep_on_fail)
    except HarnessError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"command_failed:{exc.returncode}", file=sys.stderr)
        return exc.returncode or 2


if __name__ == "__main__":
    raise SystemExit(main())
