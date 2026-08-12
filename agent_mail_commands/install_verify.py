"""Read-only install integrity verification for mail-hook-check.

Two real deployment layouts are recognized:

* **native signed Release** — ``deploy_root/current`` -> ``generations/<source_sha>-<artifact_digest>``.
  Each prepared generation persists its canonical, already-verified
  ``release-index.json`` + signature (written by ``generation_prepare``). The
  verifier re-verifies that signature with the controller
  ``release-public-key.bin`` (host platform/arch selector) and binds the signed
  ``source_sha`` + asset ``sha256`` to the generation id, the launcher
  path/size/sha256/format to the live launcher, and the release-manifest
  version/source_sha/edition. Missing index/signature/public-key always fails.
* **source deployment** — ``deploy_root/agent-mail-tools/`` with the
  ``~/.local/bin/<cmd>`` entrypoints and the opencode plugin resolving exactly
  into that tree; the plugin entrypoint is required.

Stat/read only and idempotent. The public key is read (never generated); the
long-term trust root itself is provisioned elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

import native_helper_install
from native_helper_install import (
    HELPER_COMMANDS,
    HELPER_TARGET,
    RECEIPT_NAME,
)
from release_index import (
    PERSISTED_INDEX_NAME,
    PERSISTED_SIGNATURE_NAME,
    SERVER_LAUNCHER_FORMATS,
    SERVER_LAUNCHER_PATH,
    ReleaseIndexError,
    verify_release_index,
)

SOURCE_COMMANDS = (
    "am-register",
    "am-retire",
    "am-init-project",
    "mail-send",
    "mail-recv",
    "mail-identity-inject",
    "mail-hook-check",
    "task-report",
)
_PLUGIN_REL = Path(".config/opencode/plugins/agent-mail.js")
_PLUGIN_SOURCE_NAME = "agent-mail.opencode-plugin.js"
_CURRENT_TARGET_RE = re.compile(
    r"^generations/([0-9a-f]{40})-([0-9a-f]{64})$"
)


@dataclass(frozen=True)
class Finding:
    code: str
    message: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _require_owned_regular(path: Path, kind: str, findings: list[Finding]) -> bool:
    """Bind a persisted generation metadata file as a regular, non-symlink,
    current-user-owned 0600 file (the generation immutability contract)."""
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        findings.append(Finding(f"{kind}_not_regular", f"{path} 必须是 generation 拥有的普通文件（非符号链接）"))
        return False
    if info.st_uid != os.getuid():
        findings.append(Finding(f"{kind}_not_owned", f"{path} 属主必须为当前用户"))
        return False
    if stat.S_IMODE(info.st_mode) != 0o600:
        findings.append(Finding(f"{kind}_mode_invalid", f"{path} 权限必须为 0600"))
        return False
    return True


def _runtime_platform() -> str:
    import platform

    sysname = platform.system().lower()
    if sysname == "linux":
        return "linux"
    if sysname == "darwin":
        return "macos"
    return sysname


def _runtime_arch() -> str:
    import platform

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine


def _required_manifest_digest_paths(root: Path) -> tuple[str, ...]:
    try:
        from store_schema import required_manifest_digest_paths
    except Exception:
        pass
    else:
        try:
            return required_manifest_digest_paths(root)
        except Exception:
            pass
    paths: list[str] = []
    if (root / "VERSION").is_file():
        paths.append("VERSION")
    static = root / "static"
    if static.is_dir():
        for path in sorted(static.rglob("*")):
            if path.is_file() and not path.is_symlink():
                paths.append(str(path.relative_to(root)).replace("\\", "/"))
    return tuple(paths)


def detect_layout(deploy_root: Path) -> str | None:
    current = deploy_root / "current"
    if current.is_symlink() and _CURRENT_TARGET_RE.fullmatch(os.readlink(current)):
        return "native"
    if (deploy_root / "generations").is_dir() or (deploy_root / "helpers").is_dir():
        return "native"
    if (deploy_root / "agent-mail-tools").is_dir():
        return "source"
    return None


def resolve_default_deploy_root(home: Path) -> Path | None:
    try:
        from upgrade_layout import default_upgrade_layout

        layout = default_upgrade_layout(home=home)
        if (layout.deploy_root / "current").is_symlink():
            return layout.deploy_root
    except Exception:
        pass
    link = home / ".local" / "bin" / "mail-hook-check"
    if link.is_symlink():
        target = Path(os.readlink(link))
        if target.name == "mail-hook-check" and len(target.parts) >= 2 \
                and target.parent.name == "agent-mail-tools":
            return target.parent.parent
    return None


def _resolve_generation(deploy_root: Path) -> tuple[str, str, str, Path] | None:
    """Return ``(generation_id, source_sha, artifact_digest, generation_root)``."""
    current = deploy_root / "current"
    if not current.is_symlink():
        return None
    match = _CURRENT_TARGET_RE.fullmatch(os.readlink(current))
    if not match:
        return None
    source_sha, artifact_digest = match.group(1), match.group(2)
    return f"{source_sha}-{artifact_digest}", source_sha, artifact_digest, deploy_root / os.readlink(current)


def _check_native(deploy_root: Path, home: Path, findings: list[Finding]) -> None:
    current = deploy_root / "current"
    resolved = _resolve_generation(deploy_root)
    gen_root: Path | None = None
    gen_source_sha = artifact_digest = None
    if not current.is_symlink():
        code = "current_missing" if not os.path.lexists(current) else "current_not_symlink"
        findings.append(Finding(code, f"{current} 必须是指向 generations/<id> 的符号链接"))
    elif resolved is None:
        findings.append(Finding(
            "current_target_invalid",
            f"{current} -> {os.readlink(current)} 不匹配 generations/<source_sha>-<artifact_digest>",
        ))
    else:
        _, gen_source_sha, artifact_digest, gen_root = resolved
        if not gen_root.is_dir():
            findings.append(Finding("generation_missing", f"generation 目录不存在: {gen_root}"))
            gen_root = None

    root = gen_root if gen_root is not None else current
    launcher = root / SERVER_LAUNCHER_PATH
    if not launcher.is_file():
        findings.append(Finding("launcher_missing", f"launcher 不存在或非普通文件: {launcher}"))
    elif not (launcher.stat().st_mode & 0o111):
        findings.append(Finding("launcher_not_executable", f"launcher 不可执行: {launcher}"))
    internal = root / "bin" / "_internal"
    if not internal.is_dir() or not any(internal.iterdir()):
        findings.append(Finding("launcher_not_onedir", f"缺少 PyInstaller _internal 目录: {internal}"))

    manifest_path = root / "release-manifest.json"
    manifest = _load_json(manifest_path)
    if manifest is None:
        findings.append(Finding("manifest_missing", f"release-manifest.json 缺失或无效: {manifest_path}"))
    elif not isinstance(manifest, dict) or not isinstance(manifest.get("digests"), dict):
        findings.append(Finding("manifest_invalid", "release-manifest.json 缺少 digests 对象"))
    else:
        if not isinstance(manifest.get("version"), str) or not manifest["version"]:
            findings.append(Finding("manifest_invalid", "release-manifest.json 缺少 version"))
        if manifest.get("edition") != "server":
            findings.append(Finding("manifest_edition_invalid", "release-manifest.json edition 必须为 server"))
        if gen_source_sha is not None and manifest.get("source_sha") != gen_source_sha:
            findings.append(Finding("manifest_source_sha_mismatch", "release-manifest source_sha 与 generation id 不符"))
        if gen_root is not None:
            for rel in _required_manifest_digest_paths(gen_root):
                entry = gen_root / rel
                digest = manifest["digests"].get(rel)
                if not entry.is_file():
                    findings.append(Finding("manifest_digest_missing", f"manifest 引用的文件缺失: {rel}"))
                elif not isinstance(digest, str) or _sha256(entry) != digest:
                    findings.append(Finding("manifest_digest_mismatch", f"文件 hash 与 manifest 不符: {rel}"))

    helpers_dir = deploy_root / "helpers"
    receipt_path = helpers_dir / RECEIPT_NAME
    if not os.path.lexists(receipt_path):
        findings.append(Finding("receipt_missing", f"helper ownership receipt 缺失: {receipt_path}"))
    else:
        try:
            native_helper_install._load_receipt(receipt_path)
        except native_helper_install.HelperInstallError:
            findings.append(Finding("receipt_invalid", f"helper ownership receipt 无效: {receipt_path}"))
    for cmd in HELPER_COMMANDS:
        link = helpers_dir / cmd
        if not link.is_symlink():
            findings.append(Finding("helper_missing", f"helper 软链缺失: {cmd}"))
            continue
        if os.readlink(link) != HELPER_TARGET:
            findings.append(Finding("helper_target_wrong", f"{cmd} -> {os.readlink(link)} 应为 {HELPER_TARGET}"))
        elif launcher.is_file():
            try:
                if link.resolve() != launcher.resolve():
                    findings.append(Finding("helper_target_wrong", f"{cmd} 未解析到 launcher"))
            except OSError:
                findings.append(Finding("helper_target_wrong", f"{cmd} 解析失败"))

    _check_signed_release(root, gen_source_sha, artifact_digest, launcher, home, manifest, findings)


def _check_signed_release(root, gen_source_sha, artifact_digest, launcher, home, manifest, findings):
    """Re-verify the persisted signed release-index and bind it to the live generation."""
    index_path = root / PERSISTED_INDEX_NAME
    signature_path = root / PERSISTED_SIGNATURE_NAME
    if not os.path.lexists(index_path):
        findings.append(Finding("index_missing", f"持久化 release-index 缺失: {index_path}"))
        return
    if not os.path.lexists(signature_path):
        findings.append(Finding("signature_missing", f"持久化 release-index 签名缺失: {signature_path}"))
        return
    if not _require_owned_regular(index_path, "index", findings):
        return
    if not _require_owned_regular(signature_path, "index_signature", findings):
        return
    try:
        from upgrade_layout import UpgradeLayoutError, default_upgrade_layout, load_release_public_key

        public_key = load_release_public_key(default_upgrade_layout(home=home))
    except UpgradeLayoutError as exc:
        findings.append(Finding("trust_unavailable", f"release public key 不可用: {exc.code}"))
        return
    except Exception as exc:
        findings.append(Finding("trust_unavailable", f"release public key 读取失败: {exc}"))
        return
    platform, arch = _runtime_platform(), _runtime_arch()
    try:
        verified = verify_release_index(
            index_path.read_bytes(),
            signature_path.read_bytes(),
            public_key,
            platform=platform,
            arch=arch,
        )
    except ReleaseIndexError as exc:
        findings.append(Finding("index_verify_failed", f"release-index 验签失败: {exc.code}"))
        return
    if isinstance(manifest, dict):
        if manifest.get("version") != verified.get("version"):
            findings.append(Finding("manifest_version_mismatch", "release-manifest version 与 release-index 不符"))
    if gen_source_sha is not None and verified.get("source_sha") != gen_source_sha:
        findings.append(Finding("index_source_sha_mismatch", "release-index source_sha 与 generation id 不符"))
    asset = verified.get("selected_asset")
    if not isinstance(asset, dict):
        findings.append(Finding("index_verify_failed", "release-index 未选出 asset"))
        return
    if artifact_digest is not None and asset.get("sha256") != artifact_digest:
        findings.append(Finding("asset_digest_mismatch", "asset sha256 与 generation id artifact_digest 不符"))
    launcher_field = asset.get("launcher")
    if not isinstance(launcher_field, dict):
        findings.append(Finding("index_verify_failed", "release-index launcher 字段缺失"))
        return
    if launcher_field.get("path") != SERVER_LAUNCHER_PATH:
        findings.append(Finding("launcher_path_invalid", f"launcher.path 应为 {SERVER_LAUNCHER_PATH}"))
    expected_format = SERVER_LAUNCHER_FORMATS.get(platform)
    if expected_format is not None and launcher_field.get("format") != expected_format:
        findings.append(Finding("launcher_format_invalid", f"launcher.format 应为 {expected_format}"))
    if launcher.is_file():
        if launcher_field.get("size") != launcher.stat().st_size:
            findings.append(Finding("launcher_size_mismatch", "launcher size 与 release-index 不符"))
        if launcher_field.get("sha256") != _sha256(launcher):
            findings.append(Finding("launcher_hash_mismatch", "launcher hash 与 release-index 不符"))


def _check_source(deploy_root: Path, home: Path, findings: list[Finding]) -> None:
    tools_dir = deploy_root / "agent-mail-tools"
    bin_dir = home / ".local" / "bin"
    for cmd in SOURCE_COMMANDS:
        link = bin_dir / cmd
        expected = tools_dir / cmd
        if not link.is_symlink():
            findings.append(Finding("helper_missing", f"入口软链缺失: ~/.local/bin/{cmd}"))
            continue
        try:
            resolved = link.resolve()
            expected_resolved = expected.resolve()
        except OSError:
            findings.append(Finding("helper_target_wrong", f"{cmd} 解析失败"))
            continue
        if resolved != expected_resolved:
            findings.append(Finding("helper_target_wrong", f"~/.local/bin/{cmd} 未精确指向部署 {expected}"))
        elif not resolved.is_file():
            findings.append(Finding("helper_missing", f"入口解析到的文件缺失: {cmd}"))

    plugin = home / _PLUGIN_REL
    expected_plugin = tools_dir / _PLUGIN_SOURCE_NAME
    if not os.path.lexists(plugin):
        findings.append(Finding("plugin_missing", f"OpenCode plugin 入口缺失: {plugin}"))
    elif not plugin.is_symlink():
        findings.append(Finding("plugin_target_invalid", f"plugin 应为符号链接: {plugin}"))
    else:
        try:
            if plugin.resolve() != expected_plugin.resolve():
                findings.append(Finding("plugin_target_wrong", f"plugin 未精确绑定到 {expected_plugin}"))
        except OSError:
            findings.append(Finding("plugin_target_wrong", "plugin 解析失败"))


def verify_install(deploy_root: Path, *, home: Path | None = None) -> list[Finding]:
    """Verify the install at ``deploy_root`` (read-only). Empty list = consistent."""
    deploy_root = Path(deploy_root)
    home = Path(home) if home is not None else Path.home()
    findings: list[Finding] = []
    layout = detect_layout(deploy_root)
    if layout == "native":
        _check_native(deploy_root, home, findings)
    elif layout == "source":
        _check_source(deploy_root, home, findings)
    else:
        findings.append(Finding(
            "layout_unknown",
            f"{deploy_root} 既非 native (current->generations/<id>) 也非 source (agent-mail-tools/) 部署",
        ))
    return findings


__all__ = ["Finding", "detect_layout", "resolve_default_deploy_root", "verify_install"]
