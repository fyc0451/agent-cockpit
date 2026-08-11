"""Release-external schema evidence files and active Server environment selector."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

import generation_switch
import maintenance_controller
import release_readiness
import supervisor_adapter


EVIDENCE_DIR_NAME = "schema-evidence"
ACTIVE_ENV_NAME = "server-evidence.env"
MAX_ENVIRONMENT_BYTES = 8192
EVIDENCE_ROLES = frozenset({"previous", "target"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
_EVIDENCE_FILE_RE = re.compile(
    r"^([0-9a-f]{64})-(previous|target)-([0-9a-f]{40})-([0-9a-f]{64})\.json$"
)
_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_NOFOLLOW", 0
)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class EvidenceEnvironmentError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EvidenceBinding:
    path: Path
    sha256: str
    request_id: str
    role: str
    version: str
    generation: generation_switch.GenerationIdentity


def _fail(code: str) -> None:
    raise EvidenceEnvironmentError(code)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _secure_directory(info: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700
    )


def _secure_file(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
    )


def _file_signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _validate_plan(plan: maintenance_controller.ControllerPlan) -> None:
    if not isinstance(plan, maintenance_controller.ControllerPlan):
        _fail("plan_invalid")
    try:
        rebuilt = maintenance_controller.build_controller_plan(
            state_root=plan.state_root,
            deploy_root=plan.deploy_root,
            current=plan.current,
            controller_root=plan.controller_root,
        )
    except maintenance_controller.ControllerPreflightError:
        _fail("plan_invalid")
    if rebuilt != plan:
        _fail("plan_invalid")
    selector = plan.state_root / ACTIVE_ENV_NAME
    if _SAFE_PATH_RE.fullmatch(selector.as_posix()) is None:
        _fail("environment_path_unsupported")
    try:
        supervisor_adapter.validate_release_external_environment_file(
            selector,
            deploy_root=plan.deploy_root,
        )
    except supervisor_adapter.SupervisorAdapterError:
        _fail("plan_invalid")


def validate_evidence_plan(
    plan: maintenance_controller.ControllerPlan,
) -> Path:
    """Validate all fixed evidence paths before any maintenance mutation."""
    _validate_plan(plan)
    return plan.state_root / ACTIVE_ENV_NAME


def _open_state(plan: maintenance_controller.ControllerPlan) -> int:
    _validate_plan(plan)
    fd = -1
    try:
        fd = os.open("/", _DIR_FLAGS)
        for component in plan.state_root.parts[1:]:
            before = os.stat(component, dir_fd=fd, follow_symlinks=False)
            child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            after = os.fstat(child)
            if not _same_inode(before, after) or not stat.S_ISDIR(after.st_mode):
                os.close(child)
                _fail("state_unsafe")
            os.close(fd)
            fd = child
        if not _secure_directory(os.fstat(fd)):
            _fail("state_unsafe")
        return fd
    except EvidenceEnvironmentError:
        if fd >= 0:
            os.close(fd)
        raise
    except OSError:
        if fd >= 0:
            os.close(fd)
        _fail("state_unsafe")


def _open_evidence_dir(state_fd: int, *, create: bool) -> int:
    try:
        before = os.stat(EVIDENCE_DIR_NAME, dir_fd=state_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            _fail("evidence_missing")
        try:
            os.mkdir(EVIDENCE_DIR_NAME, 0o700, dir_fd=state_fd)
            os.fsync(state_fd)
            before = os.stat(
                EVIDENCE_DIR_NAME, dir_fd=state_fd, follow_symlinks=False
            )
        except OSError:
            _fail("evidence_write_failed")
    except OSError:
        _fail("evidence_unsafe")
    try:
        fd = os.open(EVIDENCE_DIR_NAME, _DIR_FLAGS, dir_fd=state_fd)
        opened = os.fstat(fd)
    except OSError:
        _fail("evidence_unsafe")
    if not _same_inode(before, opened) or not _secure_directory(opened):
        os.close(fd)
        _fail("evidence_unsafe")
    return fd


def _read_at(
    directory_fd: int,
    name: str,
    *,
    missing_code: str,
    unsafe_code: str,
    max_bytes: int,
) -> bytes:
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        _fail(missing_code)
    except OSError:
        _fail(unsafe_code)
    if not _secure_file(before) or before.st_size <= 0 or before.st_size > max_bytes:
        _fail(unsafe_code)
    fd = -1
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
        opened = os.fstat(fd)
        if not _same_inode(before, opened) or not _secure_file(opened):
            _fail(unsafe_code)
        raw = bytearray()
        while len(raw) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            len(raw) != before.st_size
            or not _secure_file(after)
            or not _secure_file(current)
            or _file_signature(before) != _file_signature(opened)
            or _file_signature(opened) != _file_signature(after)
            or _file_signature(after) != _file_signature(current)
        ):
            _fail(unsafe_code)
        return bytes(raw)
    except EvidenceEnvironmentError:
        raise
    except OSError:
        _fail(unsafe_code)
    finally:
        if fd >= 0:
            os.close(fd)


def _atomic_replace(
    directory_fd: int,
    name: str,
    raw: bytes,
    *,
    unsafe_code: str,
    write_code: str,
) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError:
        _fail(unsafe_code)
    else:
        if not _secure_file(current):
            _fail(unsafe_code)
    temp_name = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = -1
    replaced = False
    try:
        fd = os.open(temp_name, _WRITE_FLAGS, 0o600, dir_fd=directory_fd)
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                _fail(write_code)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(
            temp_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        replaced = True
        try:
            os.fsync(directory_fd)
        except OSError:
            # Replace outcome is ambiguous; reconcile by exact secure reread.
            if _read_at(
                directory_fd,
                name,
                missing_code=write_code,
                unsafe_code=unsafe_code,
                max_bytes=max(len(raw), 1),
            ) != raw:
                _fail(write_code)
        if _read_at(
            directory_fd,
            name,
            missing_code=write_code,
            unsafe_code=unsafe_code,
            max_bytes=max(len(raw), 1),
        ) != raw:
            _fail(write_code)
    except EvidenceEnvironmentError:
        raise
    except OSError:
        _fail(write_code)
    finally:
        if fd >= 0:
            os.close(fd)
        if not replaced:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except OSError:
                pass


def _publish_immutable(
    directory_fd: int,
    name: str,
    raw: bytes,
) -> None:
    """Install final by no-replace hard link and reconcile ambiguous outcomes."""
    temp_name = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = -1
    temp_exists = False
    try:
        fd = os.open(temp_name, _WRITE_FLAGS, 0o600, dir_fd=directory_fd)
        temp_exists = True
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                _fail("evidence_write_failed")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            os.link(
                temp_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError:
            # EEXIST and lost responses are both reconciled from the final name.
            pass
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
            temp_exists = False
        except OSError:
            _fail("evidence_write_failed")
        try:
            os.fsync(directory_fd)
        except OSError:
            pass

        try:
            installed = _read_at(
                directory_fd,
                name,
                missing_code="evidence_write_failed",
                unsafe_code="evidence_unsafe",
                max_bytes=release_readiness.MAX_EVIDENCE_BYTES,
            )
        except EvidenceEnvironmentError:
            raise
        if installed != raw:
            _fail("evidence_conflict")
    except EvidenceEnvironmentError:
        raise
    except OSError:
        _fail("evidence_write_failed")
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_exists:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except OSError:
                pass


def _request_hash(request_id: str) -> str:
    if (
        type(request_id) is not str
        or not request_id
        or len(request_id) > 200
        or any(ord(character) < 32 for character in request_id)
    ):
        _fail("request_id_invalid")
    return hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def _binding_name(
    request_id: str,
    role: str,
    generation: generation_switch.GenerationIdentity,
) -> str:
    if type(role) is not str or role not in EVIDENCE_ROLES:
        _fail("evidence_role_invalid")
    if not isinstance(generation, generation_switch.GenerationIdentity):
        _fail("generation_invalid")
    return f"{_request_hash(request_id)}-{role}-{generation.generation_id}.json"


def evidence_binding_path(
    *,
    plan: maintenance_controller.ControllerPlan,
    request_id: str,
    role: str,
    generation: generation_switch.GenerationIdentity,
) -> Path:
    """Return the deterministic release-external path without touching storage."""
    _validate_plan(plan)
    return plan.state_root / EVIDENCE_DIR_NAME / _binding_name(
        request_id, role, generation
    )


def _validate_expected(
    plan: maintenance_controller.ControllerPlan,
    *,
    role: str,
    version: str,
    generation: generation_switch.GenerationIdentity,
    artifact_root: Path,
) -> None:
    if (
        type(role) is not str
        or role not in EVIDENCE_ROLES
        or type(version) is not str
        or _VERSION_RE.fullmatch(version) is None
        or not isinstance(generation, generation_switch.GenerationIdentity)
        or not isinstance(artifact_root, Path)
        or artifact_root
        != plan.deploy_root / "generations" / generation.generation_id
    ):
        _fail("evidence_expected_invalid")


def _validate_binding(
    plan: maintenance_controller.ControllerPlan,
    binding: EvidenceBinding,
    *,
    expected_request_id: str,
    expected_role: str,
    expected_version: str,
    expected_generation: generation_switch.GenerationIdentity,
) -> str:
    if (
        not isinstance(binding, EvidenceBinding)
        or not isinstance(binding.path, Path)
        or not isinstance(binding.generation, generation_switch.GenerationIdentity)
        or not isinstance(binding.sha256, str)
        or not _SHA256_RE.fullmatch(binding.sha256)
        or binding.request_id != expected_request_id
        or binding.role != expected_role
        or binding.version != expected_version
        or binding.generation != expected_generation
    ):
        _fail("evidence_binding_invalid")
    expected_parent = plan.state_root / EVIDENCE_DIR_NAME
    if binding.path.parent != expected_parent:
        _fail("evidence_path_invalid")
    match = _EVIDENCE_FILE_RE.fullmatch(binding.path.name)
    if (
        match is None
        or match.group(1) != _request_hash(binding.request_id)
        or match.group(2) != binding.role
        or match.group(3) != binding.generation.source_sha
        or match.group(4) != binding.generation.artifact_digest
    ):
        _fail("evidence_path_invalid")
    return binding.path.name


def _probe_binding(
    plan: maintenance_controller.ControllerPlan,
    binding: EvidenceBinding,
    *,
    expected_request_id: str,
    expected_role: str,
    expected_version: str,
    expected_generation: generation_switch.GenerationIdentity,
    artifact_root: Path,
) -> None:
    _validate_binding(
        plan,
        binding,
        expected_request_id=expected_request_id,
        expected_role=expected_role,
        expected_version=expected_version,
        expected_generation=expected_generation,
    )
    result = release_readiness.probe_server_evidence(
        {
            "version": expected_version,
            "source_sha": expected_generation.source_sha,
            "edition": "server",
        },
        artifact_root=artifact_root,
        environ=environment_mapping(binding),
    )
    if result.get("state") != "compatible":
        _fail("evidence_payload_invalid")


def load_schema_evidence(
    *,
    plan: maintenance_controller.ControllerPlan,
    request_id: str,
    role: str,
    expected_version: str,
    expected_generation: generation_switch.GenerationIdentity,
    artifact_root: Path,
) -> EvidenceBinding:
    """Load one deterministic binding without directory scanning or guessing."""
    _validate_plan(plan)
    _validate_expected(
        plan,
        role=role,
        version=expected_version,
        generation=expected_generation,
        artifact_root=artifact_root,
    )
    path = evidence_binding_path(
        plan=plan,
        request_id=request_id,
        role=role,
        generation=expected_generation,
    )
    name = path.name
    state_fd = _open_state(plan)
    evidence_fd = -1
    try:
        evidence_fd = _open_evidence_dir(state_fd, create=False)
        raw = _read_at(
            evidence_fd,
            name,
            missing_code="evidence_missing",
            unsafe_code="evidence_unsafe",
            max_bytes=release_readiness.MAX_EVIDENCE_BYTES,
        )
        binding = EvidenceBinding(
            path=path,
            sha256=hashlib.sha256(raw).hexdigest(),
            request_id=request_id,
            role=role,
            version=expected_version,
            generation=expected_generation,
        )
        _probe_binding(
            plan,
            binding,
            expected_request_id=request_id,
            expected_role=role,
            expected_version=expected_version,
            expected_generation=expected_generation,
            artifact_root=artifact_root,
        )
        return binding
    finally:
        if evidence_fd >= 0:
            os.close(evidence_fd)
        os.close(state_fd)


def publish_schema_evidence(
    *,
    plan: maintenance_controller.ControllerPlan,
    request_id: str,
    role: str,
    expected_version: str,
    expected_generation: generation_switch.GenerationIdentity,
    artifact_root: Path,
    evidence: Mapping[str, object],
) -> EvidenceBinding:
    """Publish one immutable request+generation evidence artifact."""
    if not isinstance(evidence, dict):
        _fail("evidence_invalid")
    _validate_plan(plan)
    _validate_expected(
        plan,
        role=role,
        version=expected_version,
        generation=expected_generation,
        artifact_root=artifact_root,
    )
    target = evidence.get("target")
    if target != {
        "version": expected_version,
        "source_sha": expected_generation.source_sha,
        "edition": "server",
    }:
        _fail("evidence_identity_mismatch")
    try:
        raw = release_readiness.canonical_evidence_bytes(evidence)
    except release_readiness.ReadinessEvidenceError:
        _fail("evidence_invalid")
    path = evidence_binding_path(
        plan=plan,
        request_id=request_id,
        role=role,
        generation=expected_generation,
    )
    name = path.name
    state_fd = _open_state(plan)
    evidence_fd = -1
    try:
        evidence_fd = _open_evidence_dir(state_fd, create=True)
        try:
            existing = _read_at(
                evidence_fd,
                name,
                missing_code="evidence_missing",
                unsafe_code="evidence_unsafe",
                max_bytes=release_readiness.MAX_EVIDENCE_BYTES,
            )
        except EvidenceEnvironmentError as exc:
            if exc.code != "evidence_missing":
                raise
            _publish_immutable(evidence_fd, name, raw)
        else:
            if existing != raw:
                _fail("evidence_conflict")
        binding = EvidenceBinding(
            path=path,
            sha256=hashlib.sha256(raw).hexdigest(),
            request_id=request_id,
            role=role,
            version=expected_version,
            generation=expected_generation,
        )
        _probe_binding(
            plan,
            binding,
            expected_request_id=request_id,
            expected_role=role,
            expected_version=expected_version,
            expected_generation=expected_generation,
            artifact_root=artifact_root,
        )
        return binding
    finally:
        if evidence_fd >= 0:
            os.close(evidence_fd)
        os.close(state_fd)


def _quote_environment_value(value: str) -> str:
    if not value or any(ord(character) < 32 for character in value):
        _fail("environment_invalid")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _environment_bytes(binding: EvidenceBinding) -> bytes:
    return (
        f"{release_readiness.EVIDENCE_PATH_ENV}="
        f"{_quote_environment_value(binding.path.as_posix())}\n"
        f"{release_readiness.EVIDENCE_SHA256_ENV}="
        f"{_quote_environment_value(binding.sha256)}\n"
    ).encode("utf-8")


def _unquote_environment_value(value: str) -> str:
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        _fail("environment_invalid")
    inner = value[1:-1]
    out: list[str] = []
    index = 0
    while index < len(inner):
        if inner[index] == "\\":
            index += 1
            if index >= len(inner) or inner[index] not in {'"', "\\"}:
                _fail("environment_invalid")
        elif inner[index] == '"':
            _fail("environment_invalid")
        out.append(inner[index])
        index += 1
    result = "".join(out)
    if not result or any(ord(character) < 32 for character in result):
        _fail("environment_invalid")
    return result


def _parse_environment(raw: bytes) -> tuple[Path, str]:
    if not raw or len(raw) > MAX_ENVIRONMENT_BYTES or not raw.endswith(b"\n"):
        _fail("environment_invalid")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        _fail("environment_invalid")
    expected = (
        release_readiness.EVIDENCE_PATH_ENV,
        release_readiness.EVIDENCE_SHA256_ENV,
    )
    if len(lines) != len(expected):
        _fail("environment_invalid")
    values: list[str] = []
    for line, key in zip(lines, expected, strict=True):
        prefix = key + "="
        if not line.startswith(prefix):
            _fail("environment_invalid")
        values.append(_unquote_environment_value(line[len(prefix):]))
    path = Path(values[0])
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        _fail("environment_invalid")
    if not _SHA256_RE.fullmatch(values[1]):
        _fail("environment_invalid")
    return path, values[1]


def _read_active_source(
    *,
    plan: maintenance_controller.ControllerPlan,
    expected_version: str,
    expected_generation: generation_switch.GenerationIdentity,
    artifact_root: Path,
) -> tuple[dict[str, object], bytes]:
    _validate_expected(
        plan,
        role="target",
        version=expected_version,
        generation=expected_generation,
        artifact_root=artifact_root,
    )
    state_fd = _open_state(plan)
    evidence_fd = -1
    try:
        raw_env = _read_at(
            state_fd,
            ACTIVE_ENV_NAME,
            missing_code="environment_missing",
            unsafe_code="environment_unsafe",
            max_bytes=MAX_ENVIRONMENT_BYTES,
        )
        path, digest = _parse_environment(raw_env)
        match = _EVIDENCE_FILE_RE.fullmatch(path.name)
        if (
            match is None
            or match.group(2) != "target"
            or match.group(3) != expected_generation.source_sha
            or match.group(4) != expected_generation.artifact_digest
            or path.parent != plan.state_root / EVIDENCE_DIR_NAME
        ):
            _fail("evidence_binding_invalid")
        evidence_fd = _open_evidence_dir(state_fd, create=False)
        raw = _read_at(
            evidence_fd,
            path.name,
            missing_code="evidence_missing",
            unsafe_code="evidence_unsafe",
            max_bytes=release_readiness.MAX_EVIDENCE_BYTES,
        )
        if not secrets.compare_digest(hashlib.sha256(raw).hexdigest(), digest):
            _fail("evidence_tampered")
        result = release_readiness.probe_server_evidence(
            {
                "version": expected_version,
                "source_sha": expected_generation.source_sha,
                "edition": "server",
            },
            artifact_root=artifact_root,
            environ={
                release_readiness.EVIDENCE_PATH_ENV: path.as_posix(),
                release_readiness.EVIDENCE_SHA256_ENV: digest,
            },
        )
        if result.get("state") != "compatible":
            _fail("evidence_payload_invalid")
        try:
            evidence = json.loads(raw)
        except (UnicodeError, ValueError, TypeError):
            _fail("evidence_payload_invalid")
        if (
            not isinstance(evidence, dict)
            or release_readiness.canonical_evidence_bytes(evidence) != raw
        ):
            _fail("evidence_payload_invalid")
        return evidence, raw_env
    finally:
        if evidence_fd >= 0:
            os.close(evidence_fd)
        os.close(state_fd)


def _read_active_environment_bytes(
    plan: maintenance_controller.ControllerPlan,
) -> bytes:
    state_fd = _open_state(plan)
    try:
        return _read_at(
            state_fd,
            ACTIVE_ENV_NAME,
            missing_code="environment_missing",
            unsafe_code="environment_unsafe",
            max_bytes=MAX_ENVIRONMENT_BYTES,
        )
    finally:
        os.close(state_fd)


def freeze_active_server_evidence_under_lease(
    *,
    controller_lease: maintenance_controller.ControllerLease,
    plan: maintenance_controller.ControllerPlan,
    request_id: str,
    expected_version: str,
    expected_generation: generation_switch.GenerationIdentity,
    artifact_root: Path,
) -> EvidenceBinding:
    """Rebind active target as request previous under the caller's lease."""
    try:
        maintenance_controller.require_controller_lease(
            plan=plan, lease=controller_lease
        )
    except maintenance_controller.ControllerPreflightError as exc:
        _fail(exc.code)
    _validate_plan(plan)
    _request_hash(request_id)
    _validate_expected(
        plan,
        role="target",
        version=expected_version,
        generation=expected_generation,
        artifact_root=artifact_root,
    )
    evidence, active_before = _read_active_source(
        plan=plan,
        expected_version=expected_version,
        expected_generation=expected_generation,
        artifact_root=artifact_root,
    )
    frozen = publish_schema_evidence(
        plan=plan,
        request_id=request_id,
        role="previous",
        expected_version=expected_version,
        expected_generation=expected_generation,
        artifact_root=artifact_root,
        evidence=evidence,
    )
    if _read_active_environment_bytes(plan) != active_before:
        _fail("environment_changed")
    return frozen


@contextmanager
def freeze_active_server_evidence(
    *,
    plan: maintenance_controller.ControllerPlan,
    request_id: str,
    expected_version: str,
    expected_generation: generation_switch.GenerationIdentity,
    artifact_root: Path,
) -> Iterator[EvidenceBinding]:
    """Hold controller lock while rebinding active target as request previous."""
    _validate_plan(plan)
    _request_hash(request_id)
    _validate_expected(
        plan,
        role="target",
        version=expected_version,
        generation=expected_generation,
        artifact_root=artifact_root,
    )
    with maintenance_controller.controller_lock(plan) as lease:
        yield freeze_active_server_evidence_under_lease(
            controller_lease=lease,
            plan=plan,
            request_id=request_id,
            expected_version=expected_version,
            expected_generation=expected_generation,
            artifact_root=artifact_root,
        )


def activate_server_evidence(
    *,
    plan: maintenance_controller.ControllerPlan,
    binding: EvidenceBinding,
    expected_request_id: str,
    expected_role: str,
    expected_version: str,
    expected_generation: generation_switch.GenerationIdentity,
    artifact_root: Path,
) -> Path:
    """Atomically point the fixed Server EnvironmentFile at one sealed evidence."""
    _validate_plan(plan)
    _validate_expected(
        plan,
        role=expected_role,
        version=expected_version,
        generation=expected_generation,
        artifact_root=artifact_root,
    )
    name = _validate_binding(
        plan,
        binding,
        expected_request_id=expected_request_id,
        expected_role=expected_role,
        expected_version=expected_version,
        expected_generation=expected_generation,
    )
    state_fd = _open_state(plan)
    evidence_fd = -1
    try:
        evidence_fd = _open_evidence_dir(state_fd, create=False)
        raw = _read_at(
            evidence_fd,
            name,
            missing_code="evidence_missing",
            unsafe_code="evidence_unsafe",
            max_bytes=release_readiness.MAX_EVIDENCE_BYTES,
        )
        if not secrets.compare_digest(hashlib.sha256(raw).hexdigest(), binding.sha256):
            _fail("evidence_tampered")
        _probe_binding(
            plan,
            binding,
            expected_request_id=expected_request_id,
            expected_role=expected_role,
            expected_version=expected_version,
            expected_generation=expected_generation,
            artifact_root=artifact_root,
        )
        _atomic_replace(
            state_fd,
            ACTIVE_ENV_NAME,
            _environment_bytes(binding),
            unsafe_code="environment_unsafe",
            write_code="environment_write_failed",
        )
        return plan.state_root / ACTIVE_ENV_NAME
    finally:
        if evidence_fd >= 0:
            os.close(evidence_fd)
        os.close(state_fd)


def read_active_server_evidence(
    *,
    plan: maintenance_controller.ControllerPlan,
    expected_request_id: str,
    expected_role: str,
    expected_version: str,
    expected_generation: generation_switch.GenerationIdentity,
    artifact_root: Path,
) -> EvidenceBinding:
    """Read and verify the active release-external evidence selector."""
    state_fd = _open_state(plan)
    evidence_fd = -1
    try:
        raw_env = _read_at(
            state_fd,
            ACTIVE_ENV_NAME,
            missing_code="environment_missing",
            unsafe_code="environment_unsafe",
            max_bytes=MAX_ENVIRONMENT_BYTES,
        )
        path, digest = _parse_environment(raw_env)
        match = _EVIDENCE_FILE_RE.fullmatch(path.name)
        if match is None:
            _fail("environment_invalid")
        binding = EvidenceBinding(
            path=path,
            sha256=digest,
            request_id=expected_request_id,
            role=match.group(2),
            version=expected_version,
            generation=expected_generation,
        )
        _validate_expected(
            plan,
            role=expected_role,
            version=expected_version,
            generation=expected_generation,
            artifact_root=artifact_root,
        )
        name = _validate_binding(
            plan,
            binding,
            expected_request_id=expected_request_id,
            expected_role=expected_role,
            expected_version=expected_version,
            expected_generation=expected_generation,
        )
        evidence_fd = _open_evidence_dir(state_fd, create=False)
        raw = _read_at(
            evidence_fd,
            name,
            missing_code="evidence_missing",
            unsafe_code="evidence_unsafe",
            max_bytes=release_readiness.MAX_EVIDENCE_BYTES,
        )
        if not secrets.compare_digest(hashlib.sha256(raw).hexdigest(), digest):
            _fail("evidence_tampered")
        _probe_binding(
            plan,
            binding,
            expected_request_id=expected_request_id,
            expected_role=expected_role,
            expected_version=expected_version,
            expected_generation=expected_generation,
            artifact_root=artifact_root,
        )
        return binding
    finally:
        if evidence_fd >= 0:
            os.close(evidence_fd)
        os.close(state_fd)


def environment_mapping(binding: EvidenceBinding) -> dict[str, str]:
    """Return the exact non-secret variables consumed by release_readiness."""
    if not isinstance(binding, EvidenceBinding) or not _SHA256_RE.fullmatch(
        binding.sha256
    ):
        _fail("evidence_binding_invalid")
    return {
        release_readiness.EVIDENCE_PATH_ENV: binding.path.as_posix(),
        release_readiness.EVIDENCE_SHA256_ENV: binding.sha256,
    }


__all__ = [
    "ACTIVE_ENV_NAME",
    "EVIDENCE_DIR_NAME",
    "EVIDENCE_ROLES",
    "EvidenceBinding",
    "EvidenceEnvironmentError",
    "activate_server_evidence",
    "evidence_binding_path",
    "environment_mapping",
    "freeze_active_server_evidence",
    "freeze_active_server_evidence_under_lease",
    "load_schema_evidence",
    "publish_schema_evidence",
    "read_active_server_evidence",
    "validate_evidence_plan",
]
