import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _launcher(tmp_path: Path) -> tuple[Path, Path]:
    generation = tmp_path / "generation"
    launcher = generation / "bin" / "agent-cockpit"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"launcher")
    return generation, launcher


def test_source_root_is_module_root_and_ignores_process_context(
    tmp_path, monkeypatch,
):
    from agent_cockpit import artifact_root

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    monkeypatch.setenv("COCKPIT_ARTIFACT_ROOT", str(tmp_path / "override"))
    monkeypatch.chdir(tmp_path)

    assert artifact_root.resolve_artifact_root() == REPO_ROOT


def test_frozen_root_uses_canonical_fixed_launcher_layout(
    tmp_path, monkeypatch,
):
    from agent_cockpit import artifact_root

    generation, launcher = _launcher(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(launcher))
    monkeypatch.setattr(sys, "_MEIPASS", str(elsewhere), raising=False)
    monkeypatch.setenv("COCKPIT_ARTIFACT_ROOT", str(elsewhere))
    monkeypatch.chdir(elsewhere)

    assert artifact_root.resolve_artifact_root() == generation.resolve()


@pytest.mark.parametrize("layout", ["relative", "wrong-parent", "wrong-name"])
def test_invalid_frozen_layout_fails_stably(tmp_path, monkeypatch, layout):
    from agent_cockpit import artifact_root

    generation, launcher = _launcher(tmp_path)
    if layout == "relative":
        executable = "generation/bin/agent-cockpit"
    elif layout == "wrong-parent":
        executable = generation / "agent-cockpit"
        executable.write_bytes(b"launcher")
    else:
        executable = launcher.with_name("other")
        executable.write_bytes(b"launcher")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    with pytest.raises(artifact_root.ArtifactRootError) as error:
        artifact_root.resolve_artifact_root()
    assert error.value.reason == "invalid_frozen_layout"


def test_invalid_frozen_layout_does_not_create_files(tmp_path, monkeypatch):
    from agent_cockpit import artifact_root

    missing = tmp_path / "generation" / "bin" / "agent-cockpit"
    before = tuple(tmp_path.rglob("*"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(missing))

    with pytest.raises(artifact_root.ArtifactRootError):
        artifact_root.resolve_artifact_root()

    assert tuple(tmp_path.rglob("*")) == before


def test_frozen_consumers_share_generation_root_in_isolated_process(tmp_path):
    generation, launcher = _launcher(tmp_path)
    static = generation / "static"
    static.mkdir()
    version = generation / "VERSION"
    index = static / "index.html"
    version.write_text("1.2.3\n", encoding="ascii")
    index.write_text("ready\n", encoding="ascii")
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "version": "1.2.3",
        "source_sha": "a" * 40,
        "edition": "server",
        "digests": {
            "VERSION": digest(version),
            "static/index.html": digest(index),
        },
    }
    manifest_path = generation / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="ascii")
    bundle = tmp_path / "bundle"
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    bundle.mkdir()
    cwd.mkdir()
    home.mkdir()
    script = """
import json, os, sys
from pathlib import Path
sys.frozen = True
sys.executable = sys.argv[1]
sys._MEIPASS = sys.argv[2]
os.chdir(sys.argv[3])
from agent_cockpit import release_readiness, runtime_paths, store_schema, version
import server
seen = {}
def capture(identity, *, artifact_root, environ):
    seen["readiness_root"] = str(artifact_root)
release_readiness._validate_server_evidence = capture
identity = {"version": "1.2.3", "source_sha": "a" * 40, "edition": "server"}
ready = release_readiness.probe_server_evidence(identity, environ={})
manifest = store_schema.probe_manifest("server", identity=identity)
print(json.dumps({
    "install_root": str(runtime_paths.INSTALL_ROOT),
    "server_root": str(server.ROOT_DIR),
    "static_dir": str(server.STATIC_DIR),
    "tools_dir": str(server.AGENT_MAIL_TOOLS_DIR),
    "version_path": str(version.VERSION_PATH),
    "version": version.read_current_version(),
    "inventory": list(store_schema.required_manifest_digest_paths()),
    "manifest": manifest["state"],
    "readiness": ready["state"],
    **seen,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(launcher), str(bundle), str(cwd)],
        cwd=REPO_ROOT,
        env={**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO_ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    root = str(generation.resolve())
    assert payload == {
        "install_root": root,
        "server_root": root,
        "static_dir": str(generation.resolve() / "static"),
        "tools_dir": str(generation.resolve() / "agent-mail-tools"),
        "version_path": str(generation.resolve() / "VERSION"),
        "version": "1.2.3",
        "inventory": ["VERSION", "static/index.html"],
        "manifest": "compatible",
        "readiness": "compatible",
        "readiness_root": root,
    }


def test_source_consumers_keep_repository_root(monkeypatch):
    from agent_cockpit import release_readiness
    from agent_cockpit import runtime_paths
    import server
    from agent_cockpit import store_schema
    from agent_cockpit import version

    seen = {}

    def capture(identity, *, artifact_root, environ):
        seen["artifact_root"] = artifact_root

    monkeypatch.setattr(release_readiness, "_validate_server_evidence", capture)
    result = release_readiness.probe_server_evidence({}, environ={})

    assert runtime_paths.INSTALL_ROOT == REPO_ROOT
    assert server.ROOT_DIR == REPO_ROOT
    assert server.STATIC_DIR == REPO_ROOT / "static"
    assert version.VERSION_PATH == REPO_ROOT / "VERSION"
    assert "static/index.html" in store_schema.required_manifest_digest_paths()
    assert result["state"] == "compatible"
    assert seen["artifact_root"] == REPO_ROOT


def test_server_has_no_direct_module_path_resource_lookup():
    source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
    assert "Path(__file__)" not in source
