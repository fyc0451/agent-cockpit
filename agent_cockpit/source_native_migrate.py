"""One-shot managed source→native migration (release-manager only).

GUI must never invoke this path. After a successful migration, ongoing upgrades
are native→native via the short-window controller.

Default CLI mode is ``plan`` / ``preflight`` only. ``execute`` requires both
``--confirm-source-native-migration`` and ``--allow-live-service-ops`` and still
accepts injectable service/health seams for tests. This module never generates
keys, tags, or Releases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from . import generation_prepare
from . import generation_switch
from .artifact_download import download_verified_artifact
from .artifact_extract import extract_verified_tarball
from . import native_controller_install
from . import native_helper_install
from . import store_schema
from . import supervisor_adapter
from . import upgrade_layout
from .generation_switch import GenerationIdentity
from .release_index import verify_release_index

# Source-only tree (rollback / fullscreen checkout). Never native current/generations.
SOURCE_DEPLOYMENTS_DIR_NAME = ".agent-cockpit-deployments"
PERSISTED_INDEX_NAME = ".release-index.json"
PERSISTED_SIGNATURE_NAME = ".release-index.json.sig"
_MANIFEST_KEYS = {"version", "source_sha", "edition", "digests"}


class MigrationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ServiceOps(Protocol):
    def stop(self, unit: str) -> None: ...

    def start(self, unit: str) -> None: ...

    def daemon_reload(self) -> None: ...

    def is_active(self, unit: str) -> bool: ...


class HealthProbe(Protocol):
    def live_source_sha(self) -> str: ...


@dataclass(frozen=True)
class MigrationInputs:
    release_tag: str
    public_key_path: Path
    index_path: Path
    signature_path: Path
    deploy_root: Path
    source_unit_path: Path
    source_env_path: Path
    persistent_env_path: Path
    diagnostics_dir: Path
    platform: str = "linux"
    arch: str = "x86_64"
    cockpit_unit: str = "agent-cockpit.service"
    mail_unit: str = "agent-mail.service"
    ready_url: str = "http://127.0.0.1:8790/health/live"
    expected_source_sha: str | None = None
    home: Path | None = None
    evidence_environment_file: Path | None = None


@dataclass(frozen=True)
class MigrationPlan:
    mode: str
    release_tag: str
    deploy_root: str
    current_path: str
    helpers_dir: str
    controller_root: str
    source_env_path: str
    persistent_env_path: str
    native_unit_path: str
    source_unit_path: str
    diagnostics_dir: str
    cockpit_unit: str
    mail_unit: str
    ready_url: str
    expected_source_sha: str | None
    public_key_path: str
    index_path: str
    signature_path: str
    platform: str
    arch: str
    evidence_environment_file: str | None
    notes: tuple[str, ...]


@dataclass(frozen=True)
class MigrationResult:
    ok: bool
    source_sha: str
    generation_id: str
    unit_backup: str
    diagnostics_dir: str


def _fail(code: str) -> None:
    raise MigrationError(code)


def _require_file(path: Path, *, code: str) -> None:
    if not path.is_absolute():
        _fail(code)
    try:
        info = path.lstat()
    except OSError:
        _fail(code)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _fail(code)


def _validate_source_env_path_shape(path: Path) -> None:
    """Path shape only — does not require the file to exist."""
    if not isinstance(path, Path) or not path.is_absolute():
        _fail("source_env_invalid")
    # Source deployment uses `.env` next to the fullscreen tree (not generation).
    if path.name != ".env":
        _fail("source_env_name_invalid")


def _source_env_present(path: Path) -> bool:
    """True when source .env exists as a valid regular 0600 file owned by self.

    Missing file is allowed (production often has EnvironmentFile=-…/.env with no
    file; runtime config lives in unit Environment= lines). Present-but-invalid
    fails closed. Never returns file contents.
    """
    _validate_source_env_path_shape(path)
    if not os.path.lexists(path):
        return False
    try:
        info = path.lstat()
    except OSError:
        _fail("source_env_invalid")
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _fail("source_env_invalid")
    if info.st_uid != os.getuid():
        _fail("source_env_owner_invalid")
    if stat.S_IMODE(info.st_mode) != 0o600:
        _fail("source_env_mode_invalid")
    return True


def _validate_provisioned_server_env(
    path: Path,
    *,
    deploy_root: Path,
) -> None:
    """Existing release-external server.env: regular, owner=self, 0600.

    Path must already be release-external (name/location). Never returns contents.
    """
    try:
        supervisor_adapter.validate_release_external_server_environment_file(
            path, deploy_root=deploy_root,
        )
    except supervisor_adapter.SupervisorAdapterError as exc:
        _fail(exc.reason if hasattr(exc, "reason") else "persistent_env_invalid")
    try:
        info = path.lstat()
    except OSError:
        _fail("persistent_env_missing")
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _fail("persistent_env_invalid")
    if info.st_uid != os.getuid():
        _fail("persistent_env_owner_invalid")
    if stat.S_IMODE(info.st_mode) != 0o600:
        _fail("persistent_env_mode_invalid")


def _resolve_env_strategy(
    *,
    source_env_path: Path,
    persistent_env_path: Path,
    deploy_root: Path,
) -> str:
    """Choose (A) copy source .env or (B) reuse pre-provisioned server.env.

    - (A) source present: validate meta; copy or require same-bytes persistent.
    - (B) source missing: require pre-provisioned server.env (owner/mode/0600).
    - neither → ``env_unavailable``; both with different bytes → ``env_content_mismatch``.
    Never scrapes unit ``Environment=`` lines. Never returns secret values.
    """
    source_present = _source_env_present(source_env_path)
    try:
        supervisor_adapter.validate_release_external_server_environment_file(
            persistent_env_path, deploy_root=deploy_root,
        )
    except supervisor_adapter.SupervisorAdapterError as exc:
        _fail(exc.reason if hasattr(exc, "reason") else "persistent_env_invalid")
    persistent_present = os.path.lexists(persistent_env_path)

    if not source_present and not persistent_present:
        _fail("env_unavailable")

    if source_present and persistent_present:
        _validate_provisioned_server_env(
            persistent_env_path, deploy_root=deploy_root,
        )
        try:
            source_bytes = source_env_path.read_bytes()
            persistent_bytes = persistent_env_path.read_bytes()
        except OSError:
            _fail("env_content_unreadable")
        if source_bytes != persistent_bytes:
            _fail("env_content_mismatch")
        return "reuse_matching"

    if source_present:
        return "copy_source"

    # (B) source missing: release manager must pre-provision server.env.
    _validate_provisioned_server_env(
        persistent_env_path, deploy_root=deploy_root,
    )
    return "reuse_provisioned"


def native_layout(*, home: Path | None = None) -> upgrade_layout.UpgradeLayout:
    """Same frozen layout as GUI / upgrade_service / default_upgrade_layout."""
    try:
        return upgrade_layout.default_upgrade_layout(home=home)
    except upgrade_layout.UpgradeLayoutError as exc:
        raise MigrationError(
            getattr(exc, "code", None) or "layout_invalid"
        ) from exc


def require_native_layout(inputs: MigrationInputs) -> upgrade_layout.UpgradeLayout:
    """Return frozen layout; deploy_root must exact-equal layout.deploy_root.

    ``~/.agent-cockpit-deployments`` is source/rollback only and must not be
    passed as native current/generations root.
    """
    layout = native_layout(home=inputs.home)
    if not isinstance(inputs.deploy_root, Path) or not inputs.deploy_root.is_absolute():
        _fail("deploy_root_invalid")
    if inputs.deploy_root != layout.deploy_root:
        _fail("deploy_root_mismatch")
    return layout


def build_plan(inputs: MigrationInputs) -> MigrationPlan:
    layout = require_native_layout(inputs)
    deploy = layout.deploy_root
    return MigrationPlan(
        mode="plan",
        release_tag=inputs.release_tag,
        deploy_root=str(deploy),
        current_path=str(layout.current),
        helpers_dir=str(deploy / "helpers"),
        controller_root=str(layout.controller_root),
        source_env_path=str(inputs.source_env_path),
        persistent_env_path=str(inputs.persistent_env_path),
        native_unit_path=str(inputs.source_unit_path),
        source_unit_path=str(inputs.source_unit_path),
        diagnostics_dir=str(inputs.diagnostics_dir),
        cockpit_unit=inputs.cockpit_unit,
        mail_unit=inputs.mail_unit,
        ready_url=inputs.ready_url,
        expected_source_sha=inputs.expected_source_sha,
        public_key_path=str(inputs.public_key_path),
        index_path=str(inputs.index_path),
        signature_path=str(inputs.signature_path),
        platform=inputs.platform,
        arch=inputs.arch,
        evidence_environment_file=(
            str(inputs.evidence_environment_file)
            if inputs.evidence_environment_file is not None
            else None
        ),
        notes=(
            "plan_only",
            "gui_must_not_invoke",
            "execute_requires_confirm_and_allow_live_service_ops",
            "stop_order_cockpit_then_mail",
            "start_order_mail_then_cockpit",
            "stop_cockpit_and_mail_only",
            "herdr_and_agents_untouched",
            "server_env_release_external_only",
            "plan_does_not_emit_env_values",
            "env_path_a_copy_source_or_b_preprovision_server_env",
            "never_scrape_unit_environment_lines",
            "half_complete_retry_idempotent",
            "native_layout_default_upgrade_layout",
            "source_tree_agent_cockpit_deployments_not_native_root",
            "atomic_unit_install_and_rollback",
            "prior_current_restored_on_failure",
            "diagnostics_never_skip_rollback",
            "default_prepare_controller_strict_reuse",
            "rollback_stop_before_unit_restore_then_mail_cockpit_start",
        ),
    )


def preflight(inputs: MigrationInputs) -> MigrationPlan:
    if not inputs.release_tag or "\x00" in inputs.release_tag:
        _fail("release_tag_invalid")
    _require_file(inputs.public_key_path, code="public_key_missing")
    key = inputs.public_key_path.read_bytes()
    if len(key) != 32:
        _fail("public_key_invalid")
    _require_file(inputs.index_path, code="index_missing")
    _require_file(inputs.signature_path, code="signature_missing")
    _require_file(inputs.source_unit_path, code="source_unit_missing")
    layout = require_native_layout(inputs)
    if not inputs.persistent_env_path.is_absolute():
        _fail("persistent_env_invalid")
    # Dual env path (A/B). May compare bytes when both exist; never emit values.
    strategy = _resolve_env_strategy(
        source_env_path=inputs.source_env_path,
        persistent_env_path=inputs.persistent_env_path,
        deploy_root=layout.deploy_root,
    )
    if not inputs.diagnostics_dir.is_absolute():
        _fail("diagnostics_dir_invalid")
    if inputs.expected_source_sha is not None:
        if (
            type(inputs.expected_source_sha) is not str
            or len(inputs.expected_source_sha) != 40
            or any(c not in "0123456789abcdef" for c in inputs.expected_source_sha)
        ):
            _fail("expected_source_sha_invalid")
    if inputs.evidence_environment_file is not None:
        try:
            supervisor_adapter.validate_release_external_environment_file(
                inputs.evidence_environment_file,
                deploy_root=layout.deploy_root,
            )
        except supervisor_adapter.SupervisorAdapterError as exc:
            _fail(exc.reason if hasattr(exc, "reason") else "evidence_env_invalid")
    plan = build_plan(inputs)
    return MigrationPlan(
        **{
            **asdict(plan),
            "mode": "preflight",
            "notes": plan.notes + ("preflight_ok", f"env_strategy_{strategy}"),
        }
    )


def _ensure_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Same-dir temp + fsync + atomic replace. Never truncate-in-place.

    Never logs ``data``.
    """
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    import tempfile

    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class PriorCurrent:
    """Exact pre-migration ``current`` state (missing or symlink target text)."""

    present: bool
    link_target: str | None


def _snapshot_prior_current(deploy_root: Path) -> PriorCurrent:
    current = deploy_root / "current"
    if not os.path.lexists(current):
        return PriorCurrent(present=False, link_target=None)
    if not current.is_symlink():
        _fail("prior_current_not_symlink")
    try:
        target = os.readlink(current)
    except OSError as exc:
        raise MigrationError("prior_current_unavailable") from exc
    return PriorCurrent(present=True, link_target=target)


def _restore_prior_current(deploy_root: Path, prior: PriorCurrent) -> None:
    """Restore exact prior ``current`` symlink text or remove if it was absent."""
    current = deploy_root / "current"
    if not prior.present:
        if not os.path.lexists(current):
            return
        if not current.is_symlink():
            _fail("prior_current_restore_failed")
        try:
            current.unlink()
        except OSError as exc:
            raise MigrationError("prior_current_restore_failed") from exc
        return
    assert prior.link_target is not None
    if current.is_symlink():
        try:
            if os.readlink(current) == prior.link_target:
                return
        except OSError:
            pass
    temp_name = f".current.restore-{secrets.token_hex(8)}"
    temp_path = deploy_root / temp_name
    try:
        os.symlink(prior.link_target, temp_path)
        os.replace(temp_path, current)
    except OSError as exc:
        try:
            if temp_path.exists() or temp_path.is_symlink():
                temp_path.unlink()
        except OSError:
            pass
        raise MigrationError("prior_current_restore_failed") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _require_owned_regular(
    path: Path,
    *,
    mode: int,
    code: str,
) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise MigrationError(code) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != mode
        or info.st_nlink != 1
    ):
        _fail(code)
    return info


def _validate_reusable_generation(
    generation_path: Path,
    *,
    verified: dict[str, Any],
    index_bytes: bytes,
    signature_bytes: bytes,
) -> None:
    """Bind an existing generation to this exact signed release payload."""
    for name, expected in (
        (PERSISTED_INDEX_NAME, index_bytes),
        (PERSISTED_SIGNATURE_NAME, signature_bytes),
    ):
        path = generation_path / name
        _require_owned_regular(
            path, mode=0o600, code="generation_inconsistent",
        )
        try:
            if path.read_bytes() != expected:
                _fail("generation_inconsistent")
        except MigrationError:
            raise
        except OSError as exc:
            raise MigrationError("generation_inconsistent") from exc

    manifest_path = generation_path / "release-manifest.json"
    _require_owned_regular(
        manifest_path, mode=0o600, code="generation_inconsistent",
    )
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MigrationError("generation_inconsistent") from exc
    if (
        type(manifest) is not dict
        or set(manifest) != _MANIFEST_KEYS
        or manifest.get("version") != verified.get("version")
        or manifest.get("source_sha") != verified.get("source_sha")
        or manifest.get("edition") != "server"
        or type(manifest.get("digests")) is not dict
    ):
        _fail("generation_inconsistent")
    required = set(store_schema.required_manifest_digest_paths(generation_path))
    digests = manifest["digests"]
    if not required or set(digests) != required:
        _fail("generation_inconsistent")
    for relative in required:
        target = generation_path / relative
        _require_owned_regular(
            target, mode=0o600, code="generation_inconsistent",
        )
        digest = digests[relative]
        if type(digest) is not str or _sha256_file(target) != digest:
            _fail("generation_inconsistent")


def _generation_snapshot(
    root: Path,
    *,
    omit_receipts: bool,
) -> dict[str, tuple[Any, ...]]:
    snapshot: dict[str, tuple[Any, ...]] = {}
    omitted = {PERSISTED_INDEX_NAME, PERSISTED_SIGNATURE_NAME}
    pending = [(root, ".")]
    while pending:
        directory, relative = pending.pop()
        try:
            info = directory.lstat()
        except OSError as exc:
            raise MigrationError("generation_inconsistent") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            _fail("generation_inconsistent")
        snapshot[relative] = ("dir", 0o700, info.st_uid)
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise MigrationError("generation_inconsistent") from exc
        for entry in entries:
            path = Path(entry.path)
            child = entry.name if relative == "." else f"{relative}/{entry.name}"
            if omit_receipts and relative == "." and entry.name in omitted:
                continue
            try:
                child_info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise MigrationError("generation_inconsistent") from exc
            if stat.S_ISDIR(child_info.st_mode):
                pending.append((path, child))
            elif stat.S_ISREG(child_info.st_mode):
                expected_mode = 0o700 if child == "bin/agent-cockpit" else 0o600
                if (
                    child_info.st_uid != os.getuid()
                    or stat.S_IMODE(child_info.st_mode) != expected_mode
                    or child_info.st_nlink != 1
                ):
                    _fail("generation_inconsistent")
                snapshot[child] = (
                    "file",
                    expected_mode,
                    child_info.st_uid,
                    child_info.st_size,
                    _sha256_file(path),
                )
            else:
                _fail("generation_inconsistent")
    return snapshot


def _persist_signed_release(
    generation_path: Path,
    *,
    index_bytes: bytes,
    signature_bytes: bytes,
) -> None:
    _atomic_write_bytes(
        generation_path / PERSISTED_INDEX_NAME, index_bytes, 0o600,
    )
    _atomic_write_bytes(
        generation_path / PERSISTED_SIGNATURE_NAME, signature_bytes, 0o600,
    )


def _bin_snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Return a strict, no-follow snapshot for one PyInstaller ``bin`` tree."""
    snapshot: dict[str, tuple[Any, ...]] = {}
    pending = [(root, ".")]
    while pending:
        directory, relative = pending.pop()
        try:
            info = directory.lstat()
        except OSError as exc:
            raise MigrationError("controller_inconsistent") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            _fail("controller_inconsistent")
        snapshot[relative] = ("dir", 0o700, info.st_uid)
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise MigrationError("controller_inconsistent") from exc
        for entry in entries:
            path = Path(entry.path)
            child = entry.name if relative == "." else f"{relative}/{entry.name}"
            try:
                child_info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise MigrationError("controller_inconsistent") from exc
            if stat.S_ISDIR(child_info.st_mode):
                pending.append((path, child))
            elif stat.S_ISREG(child_info.st_mode):
                expected_mode = 0o700 if child == "agent-cockpit" else 0o600
                if (
                    child_info.st_uid != os.getuid()
                    or stat.S_IMODE(child_info.st_mode) != expected_mode
                    or child_info.st_nlink != 1
                ):
                    _fail("controller_inconsistent")
                snapshot[child] = (
                    "file",
                    expected_mode,
                    child_info.st_uid,
                    child_info.st_size,
                    _sha256_file(path),
                )
            else:
                _fail("controller_inconsistent")
    if "_internal" not in snapshot or not any(
        path.startswith("_internal/") and value[0] == "file"
        for path, value in snapshot.items()
    ):
        _fail("controller_inconsistent")
    return snapshot


def _reuse_or_prepare_generation(
    *,
    index_bytes: bytes,
    signature_bytes: bytes,
    public_key_bytes: bytes,
    deploy_root: Path,
    platform: str,
    arch: str,
) -> generation_prepare.PreparedGeneration:
    """Default production prepare: strict reuse of exact generation, else extract.

    Existing ``generations/<id>`` must match verified index launcher path/size/hash;
    mismatch fail-closed (``generation_inconsistent``). Never leaves partial extract
    as success.
    """
    verified = verify_release_index(
        index_bytes,
        signature_bytes,
        public_key_bytes,
        platform=platform,
        arch=arch,
    )
    asset: dict[str, Any] = verified["selected_asset"]
    identity = GenerationIdentity(
        source_sha=verified["source_sha"],
        artifact_digest=asset["sha256"],
    )
    generation_path = deploy_root / "generations" / identity.generation_id
    launcher_rel = asset["launcher"]["path"]
    if type(launcher_rel) is not str or not launcher_rel:
        _fail("generation_inconsistent")
    launcher_path = generation_path.joinpath(*launcher_rel.split("/"))

    if os.path.lexists(generation_path):
        try:
            info = generation_path.lstat()
        except OSError as exc:
            raise MigrationError("generation_inconsistent") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            _fail("generation_inconsistent")
        index_path = generation_path / PERSISTED_INDEX_NAME
        signature_path = generation_path / PERSISTED_SIGNATURE_NAME
        index_exists = os.path.lexists(index_path)
        signature_exists = os.path.lexists(signature_path)
        if index_exists != signature_exists:
            _fail("generation_inconsistent")
        artifact_path = download_verified_artifact(
            asset, deploy_root / "artifact-cache",
        )
        temporary = deploy_root / "generations" / (
            f".reuse-verify-{secrets.token_hex(12)}"
        )
        comparison_completed = False
        try:
            try:
                extract_verified_tarball(artifact_path, asset, temporary)
            except Exception as exc:
                raise MigrationError("generation_inconsistent") from exc
            if _generation_snapshot(
                generation_path, omit_receipts=True,
            ) != _generation_snapshot(temporary, omit_receipts=False):
                _fail("generation_inconsistent")
            if not index_exists:
                _persist_signed_release(
                    generation_path,
                    index_bytes=index_bytes,
                    signature_bytes=signature_bytes,
                )
            _validate_reusable_generation(
                generation_path,
                verified=verified,
                index_bytes=index_bytes,
                signature_bytes=signature_bytes,
            )
            comparison_completed = True
        finally:
            if comparison_completed and os.path.lexists(temporary):
                try:
                    info = temporary.lstat()
                    if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
                        shutil.rmtree(temporary)
                except OSError:
                    pass
        return generation_prepare.PreparedGeneration(
            version=verified["version"],
            source_sha=identity.source_sha,
            artifact_digest=identity.artifact_digest,
            generation_id=identity.generation_id,
            generation_path=generation_path,
            launcher_path=launcher_path,
        )

    prepared = generation_prepare.prepare_generation(
        index_bytes,
        signature_bytes,
        public_key_bytes,
        deploy_root=deploy_root,
        platform=platform,
        arch=arch,
    )
    _persist_signed_release(
        prepared.generation_path,
        index_bytes=index_bytes,
        signature_bytes=signature_bytes,
    )
    _validate_reusable_generation(
        prepared.generation_path,
        verified=verified,
        index_bytes=index_bytes,
        signature_bytes=signature_bytes,
    )
    return prepared


def _reuse_or_install_controller(
    layout: upgrade_layout.UpgradeLayout,
    prepared: generation_prepare.PreparedGeneration,
    release_public_key: bytes,
) -> Path:
    """Default production controller install: strict reuse or first install.

    Existing controller must match prepared launcher digest and release public key;
    mismatch fail-closed (``controller_inconsistent``).
    """
    if type(release_public_key) is not bytes or len(release_public_key) != 32:
        _fail("public_key_invalid")
    if not os.path.lexists(layout.controller_root):
        return native_controller_install.install_native_controller(
            layout, prepared, release_public_key,
        )
    try:
        root_info = layout.controller_root.lstat()
    except OSError as exc:
        raise MigrationError("controller_inconsistent") from exc
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        _fail("controller_inconsistent")
    try:
        children = {entry.name for entry in os.scandir(layout.controller_root)}
    except OSError as exc:
        raise MigrationError("controller_inconsistent") from exc
    if children != {"bin", upgrade_layout.PUBLIC_KEY_NAME}:
        _fail("controller_inconsistent")
    try:
        upgrade_layout.validate_controller_launcher(layout)
        installed_key = upgrade_layout.load_release_public_key(layout)
    except Exception as exc:
        raise MigrationError("controller_inconsistent") from exc
    if installed_key != release_public_key:
        _fail("controller_inconsistent")
    try:
        if _bin_snapshot(prepared.generation_path / "bin") != _bin_snapshot(
            layout.controller_root / "bin"
        ):
            _fail("controller_inconsistent")
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError("controller_inconsistent") from exc
    return layout.controller_root


def _apply_env_strategy(
    *,
    source_env_path: Path,
    persistent_env_path: Path,
    deploy_root: Path,
) -> str:
    """Apply (A) atomic copy or (B) reuse pre-provisioned server.env.

    Never mutates ``source_env_path``. Never scrapes unit Environment= lines.
    Never logs file contents.
    """
    strategy = _resolve_env_strategy(
        source_env_path=source_env_path,
        persistent_env_path=persistent_env_path,
        deploy_root=deploy_root,
    )
    if strategy in ("reuse_provisioned", "reuse_matching"):
        # Already validated present; do not re-read for logging.
        return strategy
    if strategy != "copy_source":
        _fail("env_strategy_invalid")
    # (A) source present, persistent absent → atomic copy exact bytes.
    try:
        data = source_env_path.read_bytes()
    except OSError:
        _fail("source_env_missing")
    if os.path.lexists(persistent_env_path):
        # Race: appeared after strategy resolve — re-check equality.
        _validate_provisioned_server_env(
            persistent_env_path, deploy_root=deploy_root,
        )
        try:
            if persistent_env_path.read_bytes() != data:
                _fail("env_content_mismatch")
        except OSError:
            _fail("persistent_env_invalid")
        return "reuse_matching"
    _atomic_write_bytes(persistent_env_path, data, 0o600)
    return "copy_source"


def _render_native_unit(
    deploy_root: Path,
    *,
    server_environment_file: Path,
    evidence_environment_file: Path | None = None,
) -> str:
    current = deploy_root / "current"
    launcher = current / "bin" / "agent-cockpit"
    return supervisor_adapter.render_linux_unit(
        current_dir=current,
        deploy_root=deploy_root,
        program_arguments=(str(launcher), "serve"),
        server_environment_file=server_environment_file,
        evidence_environment_file=evidence_environment_file,
    )


def _expected_previous_for_migration(
    deploy_root: Path,
    target: GenerationIdentity,
) -> GenerationIdentity | None:
    """Half-complete retry: allow current already at target; else require absent.

    First migration expects no ``current``. After a prior failure that left
    ``current -> generations/<target>``, re-activate with expected_previous=target
    (idempotent fast path). Any other current is fail-closed.
    """
    current = deploy_root / "current"
    if not os.path.lexists(current):
        return None
    if not current.is_symlink():
        _fail("current_not_symlink")
    try:
        link = os.readlink(current)
    except OSError as exc:
        raise MigrationError("current_unavailable") from exc
    want = f"generations/{target.generation_id}"
    if link == want:
        return target
    _fail("current_unexpected")


def _best_effort_failure_diagnostics(
    diagnostics_dir: Path,
    *,
    exc: BaseException,
    unit_switched: bool,
    cockpit_stopped: bool,
    mail_stopped: bool,
    prior_current: PriorCurrent | None,
) -> None:
    """Write failure.json best-effort. Must never raise or skip rollback."""
    try:
        payload = {
            "error": getattr(exc, "code", None) or type(exc).__name__,
            "message": str(exc),
            "unit_switched": unit_switched,
            "cockpit_stopped": cockpit_stopped,
            "mail_stopped": mail_stopped,
            "prior_current_present": (
                None if prior_current is None else prior_current.present
            ),
        }
        _atomic_write_bytes(
            diagnostics_dir / "failure.json",
            json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
            + b"\n",
            0o600,
        )
    except Exception:
        return


def _restore_source_stack(
    *,
    service_ops: ServiceOps,
    health: HealthProbe,
    deploy_root: Path,
    prior_current: PriorCurrent | None,
    prior_health_sha: str | None,
    source_unit_path: Path,
    source_unit_bytes: bytes,
    unit_switched: bool,
    cockpit_stopped: bool,
    mail_stopped: bool,
    cockpit_unit: str,
    mail_unit: str,
) -> None:
    """Quiesce native stack, restore prior current + source unit, restart old stack.

    systemd semantics: after post-native health mismatch Cockpit is already
    *active*; restoring unit bytes then ``start`` alone does not restart it.
    Rollback must therefore:

    1. stop Cockpit then Mail (same order as maintenance window);
    2. restore prior ``current`` and exact source unit (atomic);
    3. ``daemon-reload``;
    4. start Mail then Cockpit;
    5. verify old ``/health/live`` matches pre-stop SHA.

    Never mutates source deployment ``.env``. Any restore step failure raises
    ``rollback_failed`` (stable). Callers merge with the primary exception.
    """
    problems: list[str] = []
    need_service_restore = cockpit_stopped or mail_stopped or unit_switched

    if need_service_restore:
        # Ensure active native Cockpit (and Mail) are down before unit replace.
        try:
            service_ops.stop(cockpit_unit)
        except Exception:
            problems.append("rollback_cockpit_stop_failed")
        try:
            service_ops.stop(mail_unit)
        except Exception:
            problems.append("rollback_mail_stop_failed")

    if prior_current is not None:
        try:
            _restore_prior_current(deploy_root, prior_current)
        except MigrationError as exc:
            problems.append(exc.code)
        except Exception:
            problems.append("prior_current_restore_failed")

    if unit_switched or cockpit_stopped or mail_stopped:
        try:
            _atomic_write_bytes(source_unit_path, source_unit_bytes, 0o600)
        except OSError:
            problems.append("unit_restore_failed")

    if need_service_restore:
        try:
            service_ops.daemon_reload()
        except Exception:
            problems.append("daemon_reload_failed")
        # Start order always Mail → Cockpit (strict inverse of stop).
        try:
            service_ops.start(mail_unit)
        except Exception:
            problems.append("mail_start_failed")
        try:
            service_ops.start(cockpit_unit)
        except Exception:
            problems.append("cockpit_start_failed")
        try:
            live = health.live_source_sha()
            if prior_health_sha is not None and live != prior_health_sha:
                problems.append("rollback_health_mismatch")
        except Exception:
            problems.append("rollback_health_failed")

    if problems:
        _fail("rollback_failed")


def execute(
    inputs: MigrationInputs,
    plan: MigrationPlan,
    *,
    confirm: bool,
    allow_live_service_ops: bool,
    service_ops: ServiceOps | None,
    health: HealthProbe | None,
    prepare: Callable[..., generation_prepare.PreparedGeneration] | None = None,
    install_controller: Callable[..., Path] | None = None,
    install_helpers: Callable[[Path], Any] | None = None,
    activate: Callable[..., Any] | None = None,
) -> MigrationResult:
    if not confirm:
        _fail("confirm_required")
    if not allow_live_service_ops:
        _fail("live_ops_required")
    if service_ops is None or health is None:
        _fail("live_ops_required")

    # Production defaults are reuse-aware; injected seams are test-only.
    prepare_fn = (
        prepare if prepare is not None else _reuse_or_prepare_generation
    )
    install_ctl = (
        install_controller
        if install_controller is not None
        else _reuse_or_install_controller
    )
    install_help = install_helpers or native_helper_install.install_helper_links
    activate_fn = activate or generation_switch.activate_generation

    _ensure_dir(inputs.diagnostics_dir)
    source_unit_bytes = inputs.source_unit_path.read_bytes()
    unit_backup = inputs.diagnostics_dir / "agent-cockpit.service.source.bak"
    _atomic_write_bytes(unit_backup, source_unit_bytes, 0o600)

    public_key = inputs.public_key_path.read_bytes()
    # Single native layout only — same as GUI / upgrade_service.
    layout = require_native_layout(inputs)
    deploy_root = layout.deploy_root

    cockpit_stopped = False
    mail_stopped = False
    unit_switched = False
    prior_current: PriorCurrent | None = None
    prior_health_sha: str | None = None
    try:
        prepared = prepare_fn(
            index_bytes=inputs.index_path.read_bytes(),
            signature_bytes=inputs.signature_path.read_bytes(),
            public_key_bytes=public_key,
            deploy_root=deploy_root,
            platform=inputs.platform,
            arch=inputs.arch,
        )
        if (
            inputs.expected_source_sha is not None
            and prepared.source_sha != inputs.expected_source_sha
        ):
            _fail("prepared_sha_mismatch")

        install_ctl(layout, prepared, public_key)

        # Snapshot pre-maintenance state for exact rollback.
        prior_current = _snapshot_prior_current(deploy_root)
        prior_health_sha = health.live_source_sha()

        # Maintenance window: Cockpit then Mail only (never Herdr/agents).
        service_ops.stop(inputs.cockpit_unit)
        cockpit_stopped = True
        service_ops.stop(inputs.mail_unit)
        mail_stopped = True

        identity = GenerationIdentity(
            source_sha=prepared.source_sha,
            artifact_digest=prepared.artifact_digest,
        )
        # Half-complete retry: current already at target is idempotent.
        expected_previous = _expected_previous_for_migration(
            deploy_root, identity,
        )
        activate_fn(
            deploy_root, identity, expected_previous=expected_previous,
        )
        install_help(deploy_root)
        # Host config: (A) copy source .env or (B) reuse pre-provisioned server.env.
        # Never scrape unit Environment= lines into server.env.
        source_present_before = os.path.lexists(inputs.source_env_path)
        source_env_before: bytes | None = None
        if source_present_before:
            try:
                source_env_before = inputs.source_env_path.read_bytes()
            except OSError:
                _fail("source_env_invalid")
        _apply_env_strategy(
            source_env_path=inputs.source_env_path,
            persistent_env_path=inputs.persistent_env_path,
            deploy_root=deploy_root,
        )
        # Integrity: if source .env existed, leave it byte-identical; never invent it.
        if source_present_before:
            try:
                if inputs.source_env_path.read_bytes() != source_env_before:
                    _fail("source_env_mutated")
            except OSError:
                _fail("source_env_mutated")
        elif os.path.lexists(inputs.source_env_path):
            _fail("source_env_created")

        native_unit = _render_native_unit(
            deploy_root,
            server_environment_file=inputs.persistent_env_path,
            evidence_environment_file=inputs.evidence_environment_file,
        )
        if f"{deploy_root.as_posix()}/current/.env" in native_unit:
            _fail("unit_references_generation_env")
        if "EnvironmentFile=-" in native_unit and "/.env" in native_unit:
            # optional server.env is fine; generation-local .env is not
            for line in native_unit.splitlines():
                if line.startswith("EnvironmentFile=") and line.rstrip().endswith("/.env"):
                    _fail("unit_references_generation_env")
        # Atomic unit install (never truncate-in-place).
        _atomic_write_bytes(
            inputs.source_unit_path, native_unit.encode("utf-8"), 0o600,
        )
        unit_switched = True

        service_ops.daemon_reload()
        # Start order: Mail → Cockpit (strict).
        service_ops.start(inputs.mail_unit)
        service_ops.start(inputs.cockpit_unit)

        live_sha = health.live_source_sha()
        if live_sha != prepared.source_sha:
            _fail("health_sha_mismatch")

        return MigrationResult(
            ok=True,
            source_sha=prepared.source_sha,
            generation_id=prepared.generation_id,
            unit_backup=str(unit_backup),
            diagnostics_dir=str(inputs.diagnostics_dir),
        )
    except Exception as exc:
        # Diagnostics must never prevent rollback.
        _best_effort_failure_diagnostics(
            inputs.diagnostics_dir,
            exc=exc,
            unit_switched=unit_switched,
            cockpit_stopped=cockpit_stopped,
            mail_stopped=mail_stopped,
            prior_current=prior_current,
        )
        rollback_error: BaseException | None = None
        try:
            # Never delete or rewrite source deployment .env on rollback.
            _restore_source_stack(
                service_ops=service_ops,
                health=health,
                deploy_root=deploy_root,
                prior_current=prior_current,
                prior_health_sha=prior_health_sha,
                source_unit_path=inputs.source_unit_path,
                source_unit_bytes=source_unit_bytes,
                unit_switched=unit_switched,
                cockpit_stopped=cockpit_stopped,
                mail_stopped=mail_stopped,
                cockpit_unit=inputs.cockpit_unit,
                mail_unit=inputs.mail_unit,
            )
        except BaseException as rb_exc:
            rollback_error = rb_exc

        if rollback_error is not None:
            if isinstance(rollback_error, MigrationError):
                raise MigrationError("rollback_failed") from exc
            raise MigrationError("rollback_failed") from exc

        if isinstance(exc, MigrationError):
            raise
        if isinstance(exc, (generation_switch.GenerationSwitchError,
                            native_controller_install.NativeControllerInstallError,
                            native_helper_install.HelperInstallError,
                            supervisor_adapter.SupervisorAdapterError)):
            _fail(str(exc) or "migration_failed")
        raise MigrationError("migration_failed") from exc


class LiveServiceOps:
    """Real systemctl --user ops; only used when CLI opt-in flags are set."""

    def stop(self, unit: str) -> None:
        import subprocess
        r = subprocess.run(
            ["systemctl", "--user", "stop", unit],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            _fail("service_stop_failed")

    def start(self, unit: str) -> None:
        import subprocess
        r = subprocess.run(
            ["systemctl", "--user", "start", unit],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            _fail("service_start_failed")

    def daemon_reload(self) -> None:
        import subprocess
        r = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            _fail("daemon_reload_failed")

    def is_active(self, unit: str) -> bool:
        import subprocess
        r = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0 and r.stdout.strip() == "active"


class UrlHealthProbe:
    def __init__(
        self,
        url: str,
        timeout: float = 1.0,
        attempts: int = 50,
        retry_interval: float = 0.2,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.attempts = attempts
        self.retry_interval = retry_interval

    def live_source_sha(self) -> str:
        for attempt in range(self.attempts):
            try:
                req = urllib.request.Request(
                    self.url, headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read(64 * 1024)
                payload = json.loads(raw.decode("utf-8"))
                sha = payload.get("identity", {}).get("source_sha")
                if type(sha) is not str or len(sha) != 40:
                    _fail("health_payload_invalid")
                return sha
            except MigrationError:
                raise
            except Exception as exc:
                if attempt + 1 >= self.attempts:
                    raise MigrationError("health_unavailable") from exc
                time.sleep(self.retry_interval)
        raise MigrationError("health_unavailable")


def default_deploy_root(*, home: Path | None = None) -> Path:
    """Native deploy root from frozen ``default_upgrade_layout`` only."""
    return native_layout(home=home).deploy_root


def _inputs_from_args(args: argparse.Namespace) -> MigrationInputs:
    home = Path(args.home) if getattr(args, "home", None) else None
    layout = native_layout(home=home)
    if getattr(args, "deploy_root", None):
        # Optional explicit value must exact-equal frozen layout (no second layout).
        deploy_root = Path(args.deploy_root)
    else:
        deploy_root = layout.deploy_root
    return MigrationInputs(
        release_tag=args.release_tag,
        public_key_path=Path(args.public_key),
        index_path=Path(args.index),
        signature_path=Path(args.signature),
        deploy_root=deploy_root,
        source_unit_path=Path(args.source_unit),
        source_env_path=Path(args.source_env),
        persistent_env_path=Path(args.persistent_env),
        diagnostics_dir=Path(args.diagnostics_dir),
        platform=args.platform,
        arch=args.arch,
        cockpit_unit=args.cockpit_unit,
        mail_unit=args.mail_unit,
        ready_url=args.ready_url,
        expected_source_sha=args.expected_source_sha,
        home=home,
        evidence_environment_file=(
            Path(args.evidence_env) if getattr(args, "evidence_env", None) else None
        ),
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--index", required=True, help="Verified release index JSON path")
    parser.add_argument("--signature", required=True, help="Detached index signature path")
    parser.add_argument(
        "--deploy-root",
        default=None,
        help=(
            "Native deploy root; must exact-equal "
            "upgrade_layout.default_upgrade_layout(home).deploy_root "
            "(~/.local/share/agent-cockpit-server). "
            "Default: that frozen path. "
            "~/.agent-cockpit-deployments is source/rollback only."
        ),
    )
    parser.add_argument("--source-unit", required=True)
    parser.add_argument(
        "--source-env",
        required=True,
        help=(
            "Candidate source deployment .env path (may be absent in production). "
            "If present: owner/mode-validated and copied to --persistent-env. "
            "If absent: release manager must pre-provision --persistent-env. "
            "Plan/preflight never print values; never scrapes unit Environment=."
        ),
    )
    parser.add_argument(
        "--persistent-env",
        required=True,
        help=(
            "Release-external server.env (e.g. ~/.config/agent-cockpit/server.env). "
            "When source .env is missing, this file must already exist as regular "
            "0600 owned by the current user (pre-provisioned); migration reuses it "
            "without rewriting contents."
        ),
    )
    parser.add_argument(
        "--evidence-env",
        default=None,
        help="Optional release-external server-evidence.env selector path",
    )
    parser.add_argument("--diagnostics-dir", required=True)
    parser.add_argument("--platform", default="linux")
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--cockpit-unit", default="agent-cockpit.service")
    parser.add_argument("--mail-unit", default="agent-mail.service")
    parser.add_argument("--ready-url", default="http://127.0.0.1:8790/health/live")
    parser.add_argument("--expected-source-sha", default=None)
    parser.add_argument(
        "--home",
        default=None,
        help="Override Path.home() for default_upgrade_layout roots",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Managed one-shot source→native migration (release manager only). "
            "Default modes are plan/preflight; execute is opt-in and never GUI-driven."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan_p = sub.add_parser("plan", help="Print migration plan JSON (no mutation)")
    _add_common(plan_p)
    pre_p = sub.add_parser("preflight", help="Validate inputs and print plan JSON")
    _add_common(pre_p)
    exe_p = sub.add_parser("execute", help="Execute migration (requires dual confirm flags)")
    _add_common(exe_p)
    exe_p.add_argument(
        "--confirm-source-native-migration",
        action="store_true",
        help="Required. Acknowledge irreversible maintenance-window migration.",
    )
    exe_p.add_argument(
        "--allow-live-service-ops",
        action="store_true",
        help="Required. Permit systemctl --user stop/start (never default).",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)
    inputs = _inputs_from_args(args)
    try:
        if args.command == "plan":
            plan = build_plan(inputs)
            print(json.dumps(asdict(plan), ensure_ascii=True, sort_keys=True, indent=2))
            return 0
        if args.command == "preflight":
            plan = preflight(inputs)
            print(json.dumps(asdict(plan), ensure_ascii=True, sort_keys=True, indent=2))
            return 0
        if args.command == "execute":
            plan = preflight(inputs)
            result = execute(
                inputs,
                plan,
                confirm=bool(args.confirm_source_native_migration),
                allow_live_service_ops=bool(args.allow_live_service_ops),
                service_ops=LiveServiceOps() if args.allow_live_service_ops else None,
                health=UrlHealthProbe(inputs.ready_url) if args.allow_live_service_ops else None,
            )
            print(json.dumps(asdict(result), ensure_ascii=True, sort_keys=True, indent=2))
            return 0
    except MigrationError as exc:
        print(json.dumps({"ok": False, "error": exc.code}, ensure_ascii=True), flush=True)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
