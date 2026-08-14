#!/usr/bin/env python3
"""Ordinary TERM-003 live/E2E harness.

Starts one real Cockpit Next ephemeral process on a random loopback port with a
private 0700 runtime root (data/config/state/uploads). Does not bind 8790/18790,
does not fake the backend, and does not put cwd/command/PID/env/Herdr values
into the browser. Cleanup always signals only the started process group.
Never reuses a stale web/dist: run_suite always clean-builds and records
Web/E2E/Lead provenance plus bundle hashes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
DECLARED_WEB_EXACT = "173341dad1d8022aa42ae73f7463fe9ad706b209"
WEB_PROVENANCE_PATHS = (
    "web/api/terminals.ts",
    "web/pages/TerminalPage.tsx",
    "web/state/capabilities.tsx",
    "web/styles/global.css",
    "web/test/terminal.test.tsx",
    "web/test/capabilities.test.tsx",
)
PROVENANCE_SCHEMA = "term003-live-provenance-v1"
E2E_PROVENANCE_PATHS = (
    "web/e2e-live/terminal-live.spec.ts",
    "web/playwright.live.config.ts",
    "scripts/terminal_live_e2e.py",
    "docs/testing/terminal-live-e2e.md",
)
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
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


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def git_head() -> str | None:
    try:
        proc = _run_git("rev-parse", "--verify", "HEAD")
    except OSError:
        return None
    text = (proc.stdout or "").strip()
    if proc.returncode == 0 and HEX40.fullmatch(text):
        return text
    return None


def _git_text(*args: str) -> str:
    try:
        proc = _run_git(*args)
    except OSError as exc:
        raise HarnessError("e2e_provenance_mismatch") from exc
    text = (proc.stdout or "").strip()
    if proc.returncode != 0 or not text:
        raise HarnessError("e2e_provenance_mismatch")
    return text


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_hashes(paths: tuple[str, ...]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for rel in paths:
        path = ROOT / rel
        if not path.is_file():
            raise HarnessError(f"provenance_file_missing:{rel}")
        hashes[rel] = _sha256_file(path)
    return hashes


def _bundle_hashes(dist: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(dist.rglob("*")):
        if not path.is_file():
            continue
        hashes[str(path.relative_to(dist))] = _sha256_file(path)
    if not hashes:
        raise HarnessError("bundle_hashes_empty")
    return hashes


def verify_web_blobs(web_exact: str) -> dict[str, str]:
    blobs: dict[str, str] = {}
    for rel in WEB_PROVENANCE_PATHS:
        expected = _git_text("rev-parse", f"{web_exact}:{rel}")
        actual = _git_text("hash-object", rel)
        if expected != actual:
            raise HarnessError(f"web_blob_mismatch:{rel}")
        blobs[rel] = actual
    return blobs


def require_lead_exact() -> str:
    lead = os.environ.get("TERM003_LEAD_EXACT", "").strip()
    if not HEX40.fullmatch(lead):
        raise HarnessError("lead_exact_missing")
    return lead


def require_web_exact() -> str:
    declared = os.environ.get("TERM003_WEB_EXACT", DECLARED_WEB_EXACT).strip()
    if declared != DECLARED_WEB_EXACT:
        raise HarnessError("web_exact_mismatch")
    return declared


def resolve_e2e_exact() -> tuple[str, str]:
    """Return (e2e_exact, mode). Archive mode requires TERM003_E2E_EXACT."""
    env_exact = os.environ.get("TERM003_E2E_EXACT", "").strip()
    head = git_head()
    if head is not None:
        if env_exact:
            if not HEX40.fullmatch(env_exact):
                raise HarnessError("e2e_exact_missing")
            if env_exact != head:
                raise HarnessError("e2e_provenance_mismatch")
            return env_exact, "worktree"
        return head, "worktree"
    if not HEX40.fullmatch(env_exact):
        raise HarnessError("e2e_exact_missing")
    return env_exact, "archive"


def load_external_manifest() -> dict[str, object] | None:
    raw = os.environ.get("TERM003_E2E_MANIFEST", "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError("e2e_provenance_mismatch") from exc
    if not isinstance(payload, dict):
        raise HarnessError("e2e_provenance_mismatch")
    return payload


def bind_e2e_manifest(
    manifest: dict[str, object] | None,
    *,
    mode: str,
    e2e_exact: str,
    web_exact: str,
    e2e_files: dict[str, str],
) -> dict[str, object] | None:
    if mode == "archive" and manifest is None:
        raise HarnessError("e2e_provenance_mismatch")
    if manifest is None:
        return None
    declared_e2e = manifest.get("e2e_exact")
    if declared_e2e != e2e_exact:
        raise HarnessError("e2e_provenance_mismatch")
    declared_web = manifest.get("web_exact")
    if declared_web is not None and declared_web != web_exact:
        raise HarnessError("e2e_provenance_mismatch")
    files = manifest.get("e2e_files")
    if not isinstance(files, dict):
        raise HarnessError("e2e_provenance_mismatch")
    for rel in E2E_PROVENANCE_PATHS:
        expected = files.get(rel)
        if not isinstance(expected, str) or not HEX64.fullmatch(expected):
            raise HarnessError("e2e_provenance_mismatch")
        if expected != e2e_files[rel]:
            raise HarnessError("e2e_provenance_mismatch")
    return manifest


def build_provenance(
    *,
    verify_web: bool,
    bundle_hashes: dict[str, str] | None,
    require_lead: bool,
) -> dict[str, object]:
    e2e_exact, mode = resolve_e2e_exact()
    web_exact = require_web_exact() if verify_web else os.environ.get(
        "TERM003_WEB_EXACT", DECLARED_WEB_EXACT
    ).strip() or DECLARED_WEB_EXACT
    if web_exact != DECLARED_WEB_EXACT:
        raise HarnessError("web_exact_mismatch")
    lead_exact = require_lead_exact() if require_lead else (
        os.environ.get("TERM003_LEAD_EXACT", "").strip() or None
    )
    e2e_files = _file_hashes(E2E_PROVENANCE_PATHS)
    manifest = bind_e2e_manifest(
        load_external_manifest(),
        mode=mode,
        e2e_exact=e2e_exact,
        web_exact=web_exact,
        e2e_files=e2e_files,
    )
    if lead_exact is None and isinstance(manifest, dict):
        declared_lead = manifest.get("lead_exact")
        if isinstance(declared_lead, str) and HEX40.fullmatch(declared_lead):
            lead_exact = declared_lead
    return {
        "schema": PROVENANCE_SCHEMA,
        "mode": mode,
        "web_exact": web_exact,
        "e2e_exact": e2e_exact,
        "lead_exact": lead_exact,
        "e2e_files": e2e_files,
        "web_blobs": verify_web_blobs(web_exact) if verify_web else None,
        "bundle_hashes": bundle_hashes,
        "manifest": os.environ.get("TERM003_E2E_MANIFEST", "").strip() or None,
    }


def record_provenance(
    artifact: Path,
    *,
    verify_web: bool,
    bundle_hashes: dict[str, str] | None,
    require_lead: bool = False,
) -> dict[str, object]:
    payload = build_provenance(
        verify_web=verify_web,
        bundle_hashes=bundle_hashes,
        require_lead=require_lead,
    )
    _write_json(artifact / "provenance.json", payload)
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


def require_ready(ready: dict[str, object]) -> dict[str, object]:
    if ready.get("ready") is not True:
        raise HarnessError("ephemeral_ready_false")
    return ready


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
    """Create a real git work under runner-owned uploads (allowlist system root).

    Browser never sees this absolute path; the wizard only selects the public
    `uploads` descriptor and the `term003-live-seed` child.
    """
    uploads = runtime_root / "uploads"
    if not uploads.is_dir():
        raise HarnessError("uploads_root_missing")
    sample = uploads / "term003-live-seed"
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


def ensure_web_build() -> dict[str, str]:
    dist = WEB / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    subprocess.run(
        ["npm", "--prefix", str(WEB), "run", "build"],
        cwd=str(ROOT),
        check=True,
    )
    if not (dist / "index.html").is_file():
        raise HarnessError("next_web_build_unavailable")
    hashes = _bundle_hashes(dist)
    if "index.html" not in hashes:
        raise HarnessError("bundle_index_missing")
    return hashes


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


def provenance_check() -> None:
    artifact = Path(os.environ.get("TERM003_LIVE_ARTIFACT", "/tmp/term003-live-provenance"))
    artifact.mkdir(parents=True, exist_ok=True)
    record_provenance(artifact, verify_web=False, bundle_hashes=None)
    print("PROVENANCE-CHECK PASS", flush=True)


def self_check() -> None:
    artifact = Path(os.environ.get("TERM003_LIVE_ARTIFACT", "/tmp/term003-live-self-check"))
    if artifact.exists():
        shutil.rmtree(artifact)
    artifact.mkdir(parents=True, exist_ok=True)
    provenance = build_provenance(verify_web=False, bundle_hashes=None, require_lead=False)
    runtime = artifact / "runtime"
    runtime.mkdir(mode=0o700)
    process = None
    try:
        process, descriptor, ready = start_server(runtime)
        seed_discoverable_project(runtime)
        empty_registry(str(descriptor["base_url"]))
        require_ready(ready)
        _write_json(artifact / "descriptor.json", descriptor)
        _write_json(artifact / "ready.json", ready)
        _write_json(artifact / "provenance.json", provenance)
        print("SELF-CHECK PASS", flush=True)
    finally:
        if process is not None:
            stop_process(process)
        shutil.rmtree(runtime, ignore_errors=True)


def run_suite(*, keep_on_fail: bool) -> int:
    provenance = build_provenance(verify_web=True, bundle_hashes=None, require_lead=True)
    bundle_hashes = ensure_web_build()
    provenance["bundle_hashes"] = bundle_hashes
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
        require_ready(ready)
        _write_json(artifact / "descriptor.json", descriptor)
        _write_json(artifact / "ready.json", ready)
        _write_json(artifact / "provenance.json", provenance)
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
    parser.add_argument("--provenance-check", action="store_true")
    parser.add_argument("--keep-on-fail", action="store_true")
    args = parser.parse_args(argv)
    try:
        ensure_runtime_python()
        if args.provenance_check:
            provenance_check()
            return 0
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
