import hashlib
import json
import os

from agent_cockpit import native_helper_install
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from agent_cockpit.release_index import (
    PERSISTED_INDEX_NAME,
    PERSISTED_SIGNATURE_NAME,
    canonical_bytes,
)

from agent_mail_commands import install_verify, mail_hook_check, mail_identity_inject
from agent_mail_commands.install_verify import SOURCE_COMMANDS

SOURCE_SHA = "a" * 40
ARTIFACT_DIGEST = "b" * 64
GEN_ID = f"{SOURCE_SHA}-{ARTIFACT_DIGEST}"


def _digests_for(root):
    from agent_cockpit.store_schema import required_manifest_digest_paths

    return {
        rel: hashlib.sha256((root / rel).read_bytes()).hexdigest()
        for rel in required_manifest_digest_paths(root)
    }


def _make_controller_key(home):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    ctrl = home / ".local" / "share" / "agent-cockpit-controller"
    ctrl.mkdir(parents=True)
    ctrl.chmod(0o700)
    (ctrl / "release-public-key.bin").write_bytes(public)
    (ctrl / "release-public-key.bin").chmod(0o600)
    return key


def _index_dict(gen, *, source_sha=SOURCE_SHA, asset_sha256=ARTIFACT_DIGEST,
                platform="linux", arch="x86_64", launcher_path="bin/agent-cockpit",
                launcher_format="elf", launcher_sha=None, launcher_size=None):
    launcher = gen / "bin" / "agent-cockpit"
    return {
        "schema_version": 2, "tag": "agent-cockpit-v0.0.0", "version": "0.0.0",
        "source_sha": source_sha, "draft": False, "prerelease": False,
        "assets": [{
            "name": "agent-cockpit-server-0.0.0-linux-x86_64.tar.gz",
            "edition": "server", "platform": platform, "arch": arch,
            "size": 1024, "sha256": asset_sha256,
            "launcher": {
                "path": launcher_path,
                "size": launcher_size if launcher_size is not None else launcher.stat().st_size,
                "sha256": launcher_sha or hashlib.sha256(launcher.read_bytes()).hexdigest(),
                "format": launcher_format,
            },
        }],
    }


def _persist_signed(gen, key, index):
    payload = canonical_bytes(index)
    (gen / PERSISTED_INDEX_NAME).write_bytes(payload)
    (gen / PERSISTED_INDEX_NAME).chmod(0o600)
    (gen / PERSISTED_SIGNATURE_NAME).write_bytes(key.sign(payload))
    (gen / PERSISTED_SIGNATURE_NAME).chmod(0o600)


def _build_native(base):
    deploy = base / "deploy"
    deploy.mkdir(parents=True)
    gen = deploy / "generations" / GEN_ID
    bin_dir = gen / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "agent-cockpit"
    launcher.write_bytes(b"\x7fELF" + b"\x00" * 60)
    launcher.chmod(0o700)
    internal = bin_dir / "_internal"
    internal.mkdir()
    (internal / "_internal.so").write_bytes(b"pyinstaller-blob\n")
    (gen / "VERSION").write_text("0.0.0\n")
    (gen / "static").mkdir()
    (gen / "static" / "index.html").write_text("<html></html>\n")
    (gen / "release-manifest.json").write_text(json.dumps({
        "version": "0.0.0", "source_sha": SOURCE_SHA, "edition": "server",
        "digests": _digests_for(gen),
    }))
    (deploy / "current").symlink_to(f"generations/{GEN_ID}")
    native_helper_install.install_helper_links(deploy)
    home = base / "home"
    home.mkdir()
    key = _make_controller_key(home)
    _persist_signed(gen, key, _index_dict(gen))
    return deploy, home, key, gen


def _build_source(base):
    deploy = base / "deploy"
    deploy.mkdir(parents=True)
    tools = deploy / "agent-mail-tools"
    tools.mkdir()
    for cmd in SOURCE_COMMANDS:
        path = tools / cmd
        path.write_text("#!/bin/sh\necho x\n")
        path.chmod(0o755)
    (tools / "agent-mail.opencode-plugin.js").write_text("// opencode plugin\n")
    home = base / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    plugins = home / ".config" / "opencode" / "plugins"
    plugins.mkdir(parents=True)
    for cmd in SOURCE_COMMANDS:
        (bin_dir / cmd).symlink_to(tools / cmd)
    (plugins / "agent-mail.js").symlink_to(tools / "agent-mail.opencode-plugin.js")
    return deploy, home


def _codes(findings):
    return {f.code for f in findings}


# --- native: known signed generation passes ---


def test_native_known_signed_generation_passes(tmp_path):
    deploy, home, _key, _gen = _build_native(tmp_path)
    assert install_verify.verify_install(deploy, home=home) == []


# --- BLOCK 1: tampered launcher no longer passes ---


def test_native_tampered_launcher_fails(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    (gen / "bin" / "agent-cockpit").write_bytes(b"\x7fELF" + b"\x00" * 80)
    assert "launcher_hash_mismatch" in _codes(install_verify.verify_install(deploy, home=home))


# --- BLOCK 2: index/signature/key required + full field binding ---


def test_native_missing_index_fails(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    (gen / PERSISTED_INDEX_NAME).unlink()
    assert "index_missing" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_missing_signature_fails(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    (gen / PERSISTED_SIGNATURE_NAME).unlink()
    assert "signature_missing" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_missing_public_key_fails(tmp_path):
    deploy, home, _key, _gen = _build_native(tmp_path)
    (home / ".local" / "share" / "agent-cockpit-controller" / "release-public-key.bin").unlink()
    assert "trust_unavailable" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_bad_signature_fails(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    (gen / PERSISTED_SIGNATURE_NAME).write_bytes(b"\x00" * 64)
    assert "index_verify_failed" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_index_source_sha_binding(tmp_path):
    deploy, home, key, gen = _build_native(tmp_path)
    _persist_signed(gen, key, _index_dict(gen, source_sha="c" * 40))
    assert "index_source_sha_mismatch" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_asset_digest_binding(tmp_path):
    deploy, home, key, gen = _build_native(tmp_path)
    _persist_signed(gen, key, _index_dict(gen, asset_sha256="d" * 64))
    assert "asset_digest_mismatch" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_index_launcher_path_and_size_validated(tmp_path):
    deploy, home, key, gen = _build_native(tmp_path)
    _persist_signed(gen, key, _index_dict(gen, launcher_path="../../wrong"))
    # signature still verifies over the mutated (structurally-valid) index, then binding fails
    codes = _codes(install_verify.verify_install(deploy, home=home))
    assert "launcher_path_invalid" in codes or "index_verify_failed" in codes
    _persist_signed(gen, key, _index_dict(gen, launcher_size=1))
    assert "launcher_size_mismatch" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_manifest_source_sha_binding(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    manifest = json.loads((gen / "release-manifest.json").read_text())
    manifest["source_sha"] = "e" * 40
    (gen / "release-manifest.json").write_text(json.dumps(manifest))
    assert "manifest_source_sha_mismatch" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_manifest_old_hash(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    (gen / "static" / "index.html").write_text("tampered\n")
    assert "manifest_digest_mismatch" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_manifest_version_must_match_release_index(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    manifest = json.loads((gen / "release-manifest.json").read_text())
    manifest["version"] = "9.9.9"
    (gen / "release-manifest.json").write_text(json.dumps(manifest))
    assert "manifest_version_mismatch" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_manifest_edition_must_be_server(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    manifest = json.loads((gen / "release-manifest.json").read_text())
    manifest["edition"] = "desktop"
    (gen / "release-manifest.json").write_text(json.dumps(manifest))
    assert "manifest_edition_invalid" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_persisted_index_symlink_rejected(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    target = tmp_path / "external-index"
    (gen / PERSISTED_INDEX_NAME).rename(target)
    (gen / PERSISTED_INDEX_NAME).symlink_to(target)
    assert "index_not_regular" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_persisted_signature_symlink_rejected(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    target = tmp_path / "external-sig"
    (gen / PERSISTED_SIGNATURE_NAME).rename(target)
    (gen / PERSISTED_SIGNATURE_NAME).symlink_to(target)
    assert "index_signature_not_regular" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_persisted_index_mode_rejected(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    (gen / PERSISTED_INDEX_NAME).chmod(0o644)
    assert "index_mode_invalid" in _codes(install_verify.verify_install(deploy, home=home))


# --- native structural ---


def test_native_missing_launcher(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    (gen / "bin" / "agent-cockpit").unlink()
    codes = _codes(install_verify.verify_install(deploy, home=home))
    assert "launcher_missing" in codes


def test_native_not_onedir(tmp_path):
    deploy, home, _key, gen = _build_native(tmp_path)
    internal = gen / "bin" / "_internal"
    for child in internal.iterdir():
        child.unlink()
    internal.rmdir()
    assert "launcher_not_onedir" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_wrong_helper_target(tmp_path):
    deploy, home, _key, _gen = _build_native(tmp_path)
    link = deploy / "helpers" / "mail-send"
    link.unlink()
    link.symlink_to("/usr/bin/false")
    assert "helper_target_wrong" in _codes(install_verify.verify_install(deploy, home=home))


def test_native_current_removed(tmp_path):
    deploy, home, _key, _gen = _build_native(tmp_path)
    (deploy / "current").unlink()
    assert "current_missing" in _codes(install_verify.verify_install(deploy, home=home))


# --- source deployment ---


def test_source_known_deployment_passes(tmp_path):
    deploy, home = _build_source(tmp_path)
    assert install_verify.verify_install(deploy, home=home) == []


def test_source_helper_wrong_path(tmp_path):
    deploy, home = _build_source(tmp_path)
    link = home / ".local" / "bin" / "mail-send"
    link.unlink()
    link.symlink_to("/usr/bin/false")
    assert "helper_target_wrong" in _codes(install_verify.verify_install(deploy, home=home))


def test_source_plugin_exact_rejects_other_dir(tmp_path):
    deploy, home = _build_source(tmp_path)
    decoy = tmp_path / "other" / "agent-mail.opencode-plugin.js"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("// decoy\n")
    plugin = home / ".config" / "opencode" / "plugins" / "agent-mail.js"
    plugin.unlink()
    plugin.symlink_to(decoy)
    assert "plugin_target_wrong" in _codes(install_verify.verify_install(deploy, home=home))


def test_source_plugin_regular_file(tmp_path):
    deploy, home = _build_source(tmp_path)
    plugin = home / ".config" / "opencode" / "plugins" / "agent-mail.js"
    plugin.unlink()
    plugin.write_text("not a symlink\n")
    assert "plugin_target_invalid" in _codes(install_verify.verify_install(deploy, home=home))


def test_source_plugin_missing_fails(tmp_path):
    deploy, home = _build_source(tmp_path)
    (home / ".config" / "opencode" / "plugins" / "agent-mail.js").unlink()
    assert "plugin_missing" in _codes(install_verify.verify_install(deploy, home=home))


# --- CLI / general ---


def test_help_returns_zero(capsys):
    assert mail_hook_check.main(["--help"]) == 0
    assert "--verify-install" in capsys.readouterr().out


def test_identity_selector_error_is_nonzero(monkeypatch):
    monkeypatch.setattr(mail_identity_inject, "resolve_managed_identity", lambda: None)
    monkeypatch.setattr(mail_identity_inject, "_has_managed_descriptor_candidate", lambda: False)
    assert mail_hook_check.main(["unknown", "main"]) == 2


def test_verify_install_unknown_arg_returns_two():
    assert mail_hook_check.main(["--verify-install", "--bogus"]) == 2


def test_verify_install_cli_native(monkeypatch, tmp_path, capsys):
    deploy, home, _key, _gen = _build_native(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    assert mail_hook_check.main(["--verify-install", "--deploy-root", str(deploy)]) == 0
    assert "INSTALL_VERIFY_OK" in capsys.readouterr().out


def test_verify_install_cli_source(monkeypatch, tmp_path, capsys):
    deploy, home = _build_source(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    assert mail_hook_check.main(["--verify-install", "--deploy-root", str(deploy)]) == 0
    assert "INSTALL_VERIFY_OK" in capsys.readouterr().out


def test_layout_unknown(tmp_path):
    deploy = tmp_path / "empty"
    deploy.mkdir()
    assert _codes(install_verify.verify_install(deploy, home=tmp_path)) == {"layout_unknown"}


def test_resolve_default_deploy_root_from_source(tmp_path):
    deploy, home = _build_source(tmp_path)
    assert install_verify.resolve_default_deploy_root(home) == deploy


def test_repeat_verify_has_no_side_effects(tmp_path):
    deploy, home, _key, _gen = _build_native(tmp_path)

    def snapshot(root):
        items = {}
        for path in sorted(root.rglob("*")):
            rel = str(path.relative_to(root))
            if path.is_symlink():
                items[rel] = ("link", os.readlink(path))
            elif path.is_file():
                items[rel] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
            else:
                items[rel] = ("dir", None)
        return items

    before = snapshot(deploy)
    first = install_verify.verify_install(deploy, home=home)
    second = install_verify.verify_install(deploy, home=home)
    assert first == second == []
    assert snapshot(deploy) == before
