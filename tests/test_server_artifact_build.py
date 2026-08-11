from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import stat
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import artifact_extract
from release_index import canonical_bytes, verify_release_index


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_server_artifact.py"
HELPER_COMMANDS = (
    "am-register",
    "am-retire",
    "am-init-project",
    "mail-send",
    "mail-recv",
    "mail-identity-inject",
    "task-report",
)


def _build_module():
    assert BUILD_SCRIPT.is_file()
    spec = importlib.util.spec_from_file_location("server_artifact_build", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_elf() -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # little endian
    header[6] = 1  # current ELF version
    header[16:18] = (2).to_bytes(2, "little")
    header[18:20] = (62).to_bytes(2, "little")  # EM_X86_64
    return bytes(header) + b"fake-launcher"


def test_native_server_entry_dispatches_helpers_before_app_imports() -> None:
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    dispatch = "native_launcher.main()"

    assert dispatch in source
    assert source.index(dispatch) < source.index("from fastapi import")


def test_pyinstaller_collects_all_dynamic_helper_modules() -> None:
    spec = (ROOT / "packaging/agent-cockpit-server.spec").read_text(encoding="utf-8")

    for command in HELPER_COMMANDS:
        module = command.replace("-", "_")
        assert f'"agent_mail_commands.{module}"' in spec


def _source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    static = source / "static"
    static.mkdir(parents=True)
    (source / "VERSION").write_text("1.2.3\n", encoding="ascii")
    (static / "index.html").write_text("ready\n", encoding="ascii")
    (static / "sw.js").write_text("self.ready=true;\n", encoding="ascii")
    return source


def _onedir(tmp_path: Path) -> Path:
    onedir = tmp_path / "onedir"
    internal = onedir / "_internal"
    internal.mkdir(parents=True)
    (onedir / "agent-cockpit").write_bytes(_fake_elf())
    (internal / "runtime.dat").write_bytes(b"runtime")
    return onedir


def _install_artifact(
    tmp_path: Path, index_path: Path, archive_path: Path,
) -> tuple[Path, dict]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    index_bytes = index_path.read_bytes()
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    verified = verify_release_index(
        index_bytes, key.sign(index_bytes), public, platform="linux", arch="x86_64",
    )
    selected = verified["selected_asset"]
    cache = tmp_path / "cache"
    staging = tmp_path / "staging"
    cache.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    cached = cache / selected["sha256"]
    cached.write_bytes(archive_path.read_bytes())
    cached.chmod(0o600)
    extracted = artifact_extract.extract_verified_tarball(
        cached, selected, staging / "installed",
    )
    return extracted, selected


def test_builds_deterministic_unsigned_server_assets_accepted_by_runtime_contract(
    tmp_path: Path,
) -> None:
    build = _build_module()
    source = _source_tree(tmp_path)
    generation = tmp_path / "generation"
    source_sha = "a" * 40
    build.assemble_generation(source, _onedir(tmp_path), generation, source_sha)

    assert sorted(path.relative_to(generation).as_posix() for path in generation.iterdir()) == [
        "VERSION", "bin", "release-manifest.json", "static",
    ]
    assert (generation / "bin/agent-cockpit").read_bytes() == _fake_elf()
    assert (generation / "bin/_internal/runtime.dat").read_bytes() == b"runtime"
    manifest = json.loads((generation / "release-manifest.json").read_text("ascii"))
    assert manifest == {
        "version": "1.2.3",
        "source_sha": source_sha,
        "edition": "server",
        "digests": {
            rel: hashlib.sha256((generation / rel).read_bytes()).hexdigest()
            for rel in ("VERSION", "static/index.html", "static/sw.js")
        },
    }

    first = tmp_path / "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz"
    second = tmp_path / "second.tar.gz"
    build.write_deterministic_tar(generation, first, source_date_epoch=1_700_000_000)
    build.write_deterministic_tar(generation, second, source_date_epoch=1_700_000_000)
    assert first.read_bytes() == second.read_bytes()

    index_path = tmp_path / "release-index.json"
    index = build.write_release_index(generation, first, index_path, source_sha)
    index_bytes = index_path.read_bytes()
    assert index_bytes == canonical_bytes(index)
    assert index["tag"] == "agent-cockpit-v1.2.3"
    asset = index["assets"][0]
    assert asset["name"] == "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz"
    assert asset["launcher"]["path"] == "bin/agent-cockpit"

    extracted, selected = _install_artifact(tmp_path, index_path, first)
    launcher = artifact_extract.verify_server_launcher(
        extracted, selected["launcher"],
    )
    assert launcher == extracted / "bin/agent-cockpit"
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o700
    assert stat.S_IMODE((extracted / "bin/_internal/runtime.dat").stat().st_mode) == 0o600


def test_assemble_requires_pinned_onedir_internal_layout(tmp_path: Path) -> None:
    build = _build_module()
    source = _source_tree(tmp_path)
    onedir = tmp_path / "onedir"
    onedir.mkdir()
    (onedir / "agent-cockpit").write_bytes(_fake_elf())

    with pytest.raises(ValueError, match="onedir_layout_invalid"):
        build.assemble_generation(source, onedir, tmp_path / "generation", "a" * 40)


@pytest.mark.skipif(
    not os.environ.get("P3A_BUILD_PYTHON"),
    reason="set P3A_BUILD_PYTHON to a requirements-build.txt environment",
)
def test_real_pyinstaller_onedir_runs_from_random_cwd(tmp_path: Path) -> None:
    build_python = Path(os.environ["P3A_BUILD_PYTHON"])
    output = tmp_path / "output"
    source_sha = "b" * 40
    subprocess.run(
        [
            str(build_python), str(BUILD_SCRIPT),
            "--source-root", str(ROOT),
            "--output-dir", str(output),
            "--source-sha", source_sha,
            "--source-date-epoch", "1700000000",
        ],
        cwd=tmp_path,
        check=True,
    )
    version = (ROOT / "VERSION").read_text("ascii").strip()
    archive = output / f"agent-cockpit-server-{version}-linux-x86_64.tar.gz"
    generation, selected = _install_artifact(
        tmp_path / "install", output / "release-index.json", archive,
    )
    launcher = artifact_extract.verify_server_launcher(
        generation, selected["launcher"],
    )

    home = tmp_path / "home"
    cwd = tmp_path / "random-cwd"
    home.mkdir()
    cwd.mkdir()
    helper_env = {**os.environ, "HOME": str(home)}
    for command in HELPER_COMMANDS:
        explicit = subprocess.run(
            [str(launcher), "helper", command, "--help"],
            cwd=cwd, env=helper_env, text=True, capture_output=True, check=False,
        )
        assert explicit.returncode == 0, explicit.stderr
        assert "usage:" in explicit.stdout

        alias = cwd / command
        alias.symlink_to(launcher)
        multicall = subprocess.run(
            [str(alias), "--help"],
            cwd=cwd, env=helper_env, text=True, capture_output=True, check=False,
        )
        assert multicall.returncode == 0, multicall.stderr
        assert "usage:" in multicall.stdout

    deploy = tmp_path / "native-deploy"
    deploy.mkdir()
    install = subprocess.run(
        [str(launcher), "install-helpers", "--deploy-root", str(deploy)],
        cwd=cwd, env=helper_env, text=True, capture_output=True, check=False,
    )
    assert install.returncode == 0, install.stderr
    for command in HELPER_COMMANDS:
        assert os.readlink(deploy / "helpers" / command) == (
            "../current/bin/agent-cockpit"
        )

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [str(launcher)],
        cwd=cwd,
        env={
            **os.environ,
            "HOME": str(home),
            "COCKPIT_PORT": str(port),
            "COCKPIT_HOST": "127.0.0.1",
            "COCKPIT_EDITION": "server",
            "COCKPIT_SOURCE_SHA": source_sha,
            "COCKPIT_HERDR_STATE_MODE": "off",
            "COCKPIT_B0_MODE": "off",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        live = None
        for _attempt in range(40):
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health/live", timeout=1,
                ) as response:
                    live = json.load(response)
                break
            except OSError:
                time.sleep(0.25)
        assert live is not None, "native launcher did not become live"
        assert live["identity"] == {
            "version": version,
            "source_sha": source_sha,
            "edition": "server",
            "instance_id": live["identity"]["instance_id"],
            "pid": process.pid,
        }
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/", timeout=3,
        ) as response:
            assert b"<!doctype html>" in response.read(128)
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
