"""Durable minimal journal for the maintenance-window upgrade controller.

An I/O error after atomic replace has an ambiguous durability result: callers
must reload and reconcile the journal before retrying any external mutation.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any


JOURNAL_NAME = "upgrade-journal.json"
SCHEMA_VERSION = 1
ENGINE = "immutable-upgrade-controller"
MAX_JOURNAL_BYTES = 64 * 1024

STAGES = (
    "prepared",
    "services_stopped",
    "switched",
    "services_started",
    "committed",
    "rolled_back",
)
TERMINAL_STAGES = frozenset({"committed", "rolled_back"})

INTENT_STOP_SERVICES = "stop_services"
INTENT_SWITCH_CURRENT = "switch_current"
INTENT_START_SERVICES = "start_services"
INTENT_COMMIT = "commit"
INTENT_ROLLBACK = "rollback"

_FIELDS = frozenset(
    {
        "schema_version",
        "engine",
        "request_id",
        "target_digest",
        "target_source_sha",
        "target_generation",
        "previous_generation",
        "stage",
        "intent",
        "revision",
        "primary_error_code",
        "rollback_error_code",
    }
)
_NEXT_STAGE = {
    "prepared": (INTENT_STOP_SERVICES, "services_stopped"),
    "services_stopped": (INTENT_SWITCH_CURRENT, "switched"),
    "switched": (INTENT_START_SERVICES, "services_started"),
    "services_started": (INTENT_COMMIT, "committed"),
}
_NORMAL_INTENT = {stage: intent for stage, (intent, _next) in _NEXT_STAGE.items()}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_GENERATION_RE = re.compile(r"[0-9a-f]{40}-[0-9a-f]{64}\Z")
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_NOFOLLOW", 0
)


class UpgradeJournalError(RuntimeError):
    """A fail-closed journal error exposing only a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _DuplicateKeyError(ValueError):
    pass


def _reject(code: str) -> None:
    raise UpgradeJournalError(code)


def _canonical_root(root: Path) -> bool:
    return root.is_absolute() and Path(os.path.abspath(root)) == root


def _has_symlink_component(path: Path) -> bool:
    try:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink():
                return True
    except OSError:
        return True
    return False


def _secure_root(info: os.stat_result) -> bool:
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


def _path(value: Any) -> Path:
    if not isinstance(value, (str, Path)):
        _reject("journal_unsafe")
    try:
        root = Path(value)
    except (TypeError, ValueError, OSError):
        _reject("journal_unsafe")
    if not _canonical_root(root) or _has_symlink_component(root):
        _reject("journal_unsafe")
    return root


def _fsync_parent(root: Path) -> None:
    try:
        fd = os.open(root.parent, _DIRECTORY_FLAGS)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        _reject("journal_io_error")


def _open_root(root: Path, *, create: bool) -> int:
    created = False
    if create:
        try:
            root.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        except OSError:
            _reject("journal_io_error")
        if created:
            _fsync_parent(root)
    fd = -1
    try:
        before = root.lstat()
        if not _secure_root(before):
            _reject("journal_unsafe")
        fd = os.open(root, _DIRECTORY_FLAGS)
        opened = os.fstat(fd)
        after = root.lstat()
    except FileNotFoundError:
        if fd >= 0:
            os.close(fd)
        _reject("journal_missing")
    except UpgradeJournalError:
        if fd >= 0:
            os.close(fd)
        raise
    except OSError:
        if fd >= 0:
            os.close(fd)
        _reject("journal_unsafe")
    if (
        not _secure_root(opened)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(fd)
        _reject("journal_unsafe")
    return fd


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    try:
        raw = (
            json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _reject("journal_invalid")
    if len(raw) > MAX_JOURNAL_BYTES:
        _reject("journal_invalid")
    return raw


def _valid_identity(value: dict[str, Any]) -> bool:
    request_id = value.get("request_id")
    target_digest = value.get("target_digest")
    return (
        type(request_id) is str
        and 0 < len(request_id) <= 200
        and not any(ord(character) < 32 for character in request_id)
        and type(target_digest) is str
        and _SHA256_RE.fullmatch(target_digest) is not None
    )


def _valid_error_code(value: Any) -> bool:
    return value is None or (
        type(value) is str and _ERROR_CODE_RE.fullmatch(value) is not None
    )


def _valid_generation_identity(value: dict[str, Any]) -> bool:
    source_sha = value.get("target_source_sha")
    target_generation = value.get("target_generation")
    previous_generation = value.get("previous_generation")
    if source_sha is None:
        return target_generation is None and previous_generation is None
    return (
        type(source_sha) is str
        and _SOURCE_SHA_RE.fullmatch(source_sha) is not None
        and type(target_generation) is str
        and target_generation == f"{source_sha}-{value['target_digest']}"
        and (
            previous_generation is None
            or (
                type(previous_generation) is str
                and _GENERATION_RE.fullmatch(previous_generation) is not None
            )
        )
    )


def _validate(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _FIELDS:
        _reject("journal_invalid")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or value["engine"] != ENGINE
        or not _valid_identity(value)
        or not _valid_generation_identity(value)
        or type(value["stage"]) is not str
        or value["stage"] not in STAGES
        or type(value["revision"]) is not int
        or value["revision"] < 0
        or not _valid_error_code(value["primary_error_code"])
        or not _valid_error_code(value["rollback_error_code"])
    ):
        _reject("journal_invalid")

    stage = value["stage"]
    intent = value["intent"]
    allowed_intents = {None}
    if stage not in TERMINAL_STAGES:
        allowed_intents.update({_NORMAL_INTENT[stage], INTENT_ROLLBACK})
    if type(intent) is not str and intent is not None:
        _reject("journal_invalid")
    if intent not in allowed_intents:
        _reject("journal_invalid")
    if value["rollback_error_code"] is not None and intent != INTENT_ROLLBACK:
        _reject("journal_invalid")
    if intent == INTENT_ROLLBACK and value["primary_error_code"] is None:
        _reject("journal_invalid")
    if (
        value["primary_error_code"] is not None
        and intent != INTENT_ROLLBACK
        and stage != "rolled_back"
    ):
        _reject("journal_invalid")
    identity_bound = value["target_generation"] is not None
    if stage == "prepared" and identity_bound:
        _reject("journal_invalid")
    if intent == INTENT_SWITCH_CURRENT and not identity_bound:
        _reject("journal_invalid")
    if stage == "services_stopped" and identity_bound and intent not in {
        INTENT_SWITCH_CURRENT,
        INTENT_ROLLBACK,
    }:
        _reject("journal_invalid")
    if stage in {"switched", "services_started", "committed"} and not identity_bound:
        _reject("journal_invalid")
    if stage == "committed" and (
        value["primary_error_code"] is not None
        or value["rollback_error_code"] is not None
    ):
        _reject("journal_invalid")
    if stage == "rolled_back" and value["primary_error_code"] is None:
        _reject("journal_invalid")
    return value


def _read_from_fd(root_fd: int) -> dict[str, Any]:
    try:
        fd = os.open(JOURNAL_NAME, _FILE_FLAGS, dir_fd=root_fd)
    except FileNotFoundError:
        _reject("journal_missing")
    except OSError:
        _reject("journal_unsafe")
    try:
        before = os.fstat(fd)
        if not _secure_file(before) or before.st_size <= 0 or before.st_size > MAX_JOURNAL_BYTES:
            _reject("journal_unsafe")
        chunks: list[bytes] = []
        remaining = MAX_JOURNAL_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        entry = os.stat(JOURNAL_NAME, dir_fd=root_fd, follow_symlinks=False)
    except UpgradeJournalError:
        raise
    except OSError:
        _reject("journal_unsafe")
    finally:
        os.close(fd)
    if (
        not _secure_file(after)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or (entry.st_dev, entry.st_ino) != (after.st_dev, after.st_ino)
    ):
        _reject("journal_unsafe")
    raw = b"".join(chunks)
    if not raw or len(raw) > MAX_JOURNAL_BYTES:
        _reject("journal_invalid")
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
        _reject("journal_invalid")
    validated = _validate(value)
    if _canonical_bytes(validated) != raw:
        _reject("journal_invalid")
    return validated


def _atomic_write(root_fd: int, value: dict[str, Any]) -> None:
    raw = _canonical_bytes(_validate(value))
    temporary = f".{JOURNAL_NAME}.{secrets.token_hex(8)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = -1
    try:
        fd = os.open(temporary, flags, 0o600, dir_fd=root_fd)
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(
            temporary,
            JOURNAL_NAME,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        os.fsync(root_fd)
    except UpgradeJournalError:
        raise
    except OSError:
        _reject("journal_io_error")
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _check_identity(value: dict[str, Any], request_id: Any, target_digest: Any) -> None:
    supplied = {"request_id": request_id, "target_digest": target_digest}
    if not _valid_identity(supplied):
        _reject("journal_identity_invalid")
    if value["request_id"] != request_id or value["target_digest"] != target_digest:
        _reject("journal_identity_mismatch")


def create_journal(*, root: Path, request_id: str, target_digest: str) -> dict[str, Any]:
    """Create ``prepared`` with the durable stop-services intent."""
    identity = {"request_id": request_id, "target_digest": target_digest}
    if not _valid_identity(identity):
        _reject("journal_identity_invalid")
    path = _path(root)
    root_fd = _open_root(path, create=True)
    try:
        try:
            os.stat(JOURNAL_NAME, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError:
            _reject("journal_unsafe")
        else:
            _reject("journal_exists")
        value = {
            "schema_version": SCHEMA_VERSION,
            "engine": ENGINE,
            "request_id": request_id,
            "target_digest": target_digest,
            "target_source_sha": None,
            "target_generation": None,
            "previous_generation": None,
            "stage": "prepared",
            "intent": INTENT_STOP_SERVICES,
            "revision": 0,
            "primary_error_code": None,
            "rollback_error_code": None,
        }
        _atomic_write(root_fd, value)
        return dict(value)
    finally:
        os.close(root_fd)


def load_journal(*, root: Path) -> dict[str, Any]:
    """Read and validate the journal without creating, repairing, or mutating it."""
    path = _path(root)
    root_fd = _open_root(path, create=False)
    try:
        return dict(_read_from_fd(root_fd))
    finally:
        os.close(root_fd)


def record_intent(
    *,
    root: Path,
    request_id: str,
    target_digest: str,
    intent: str,
    primary_error_code: str | None = None,
) -> dict[str, Any]:
    """Durably record the next mutation; repeated identical calls are idempotent."""
    path = _path(root)
    root_fd = _open_root(path, create=False)
    try:
        value = _read_from_fd(root_fd)
        _check_identity(value, request_id, target_digest)
        stage = value["stage"]
        if stage in TERMINAL_STAGES or type(intent) is not str:
            _reject("journal_transition_invalid")
        allowed = {_NORMAL_INTENT[stage], INTENT_ROLLBACK}
        if intent not in allowed:
            _reject("journal_transition_invalid")
        if intent == INTENT_SWITCH_CURRENT:
            _reject("journal_transition_invalid")
        if intent == INTENT_ROLLBACK:
            if not _valid_error_code(primary_error_code) or primary_error_code is None:
                _reject("journal_error_code_invalid")
        elif primary_error_code is not None:
            _reject("journal_error_code_invalid")
        if value["intent"] == intent and value["primary_error_code"] == primary_error_code:
            return dict(value)
        if value["intent"] not in {None, _NORMAL_INTENT[stage]} or (
            intent != INTENT_ROLLBACK and value["intent"] is not None
        ):
            _reject("journal_transition_invalid")
        updated = dict(value)
        updated.update(
            intent=intent,
            revision=value["revision"] + 1,
            primary_error_code=primary_error_code,
            rollback_error_code=None,
        )
        _atomic_write(root_fd, updated)
        return updated
    finally:
        os.close(root_fd)


def record_switch_intent(
    *,
    root: Path,
    request_id: str,
    target_digest: str,
    target_source_sha: str,
    previous_generation: str | None,
) -> dict[str, Any]:
    """Atomically freeze exact switch identities and the durable switch intent."""
    if (
        type(target_source_sha) is not str
        or _SOURCE_SHA_RE.fullmatch(target_source_sha) is None
        or (
            previous_generation is not None
            and (
                type(previous_generation) is not str
                or _GENERATION_RE.fullmatch(previous_generation) is None
            )
        )
    ):
        _reject("journal_identity_invalid")
    path = _path(root)
    root_fd = _open_root(path, create=False)
    try:
        value = _read_from_fd(root_fd)
        _check_identity(value, request_id, target_digest)
        target_generation = f"{target_source_sha}-{target_digest}"
        if value["stage"] != "services_stopped" or value["intent"] not in {
            None,
            INTENT_SWITCH_CURRENT,
        }:
            _reject("journal_transition_invalid")
        frozen = (
            value["target_source_sha"],
            value["target_generation"],
            value["previous_generation"],
        )
        requested = (target_source_sha, target_generation, previous_generation)
        if frozen == requested and value["intent"] == INTENT_SWITCH_CURRENT:
            return dict(value)
        if frozen != (None, None, None):
            _reject("journal_identity_mismatch")
        updated = dict(value)
        updated.update(
            target_source_sha=target_source_sha,
            target_generation=target_generation,
            previous_generation=previous_generation,
            intent=INTENT_SWITCH_CURRENT,
            revision=value["revision"] + 1,
        )
        _atomic_write(root_fd, updated)
        return updated
    finally:
        os.close(root_fd)


def record_rollback_error(
    *,
    root: Path,
    request_id: str,
    target_digest: str,
    rollback_error_code: str,
) -> dict[str, Any]:
    """Preserve a sanitized rollback failure while leaving recovery retryable."""
    path = _path(root)
    root_fd = _open_root(path, create=False)
    try:
        value = _read_from_fd(root_fd)
        _check_identity(value, request_id, target_digest)
        if not _valid_error_code(rollback_error_code) or rollback_error_code is None:
            _reject("journal_error_code_invalid")
        if value["stage"] in TERMINAL_STAGES or value["intent"] != INTENT_ROLLBACK:
            _reject("journal_transition_invalid")
        if value["rollback_error_code"] == rollback_error_code:
            return dict(value)
        updated = dict(value)
        updated["rollback_error_code"] = rollback_error_code
        updated["revision"] += 1
        _atomic_write(root_fd, updated)
        return updated
    finally:
        os.close(root_fd)


def begin_rollback_retry(
    *, root: Path, request_id: str, target_digest: str
) -> dict[str, Any]:
    """Clear a recorded rollback failure before a new recovery mutation."""
    path = _path(root)
    root_fd = _open_root(path, create=False)
    try:
        value = _read_from_fd(root_fd)
        _check_identity(value, request_id, target_digest)
        if value["stage"] in TERMINAL_STAGES or value["intent"] != INTENT_ROLLBACK:
            _reject("journal_transition_invalid")
        if value["rollback_error_code"] is None:
            return dict(value)
        updated = dict(value)
        updated["rollback_error_code"] = None
        updated["revision"] += 1
        _atomic_write(root_fd, updated)
        return updated
    finally:
        os.close(root_fd)


def advance_journal(
    *, root: Path, request_id: str, target_digest: str, stage: str
) -> dict[str, Any]:
    """Complete the recorded intent using the only allowed forward transition."""
    path = _path(root)
    root_fd = _open_root(path, create=False)
    try:
        value = _read_from_fd(root_fd)
        _check_identity(value, request_id, target_digest)
        if value["stage"] in TERMINAL_STAGES:
            if value["stage"] == stage:
                return dict(value)
            _reject("journal_transition_invalid")
        if stage == "rolled_back":
            valid = (
                value["intent"] == INTENT_ROLLBACK
                and value["rollback_error_code"] is None
            )
        else:
            required_intent, expected_stage = _NEXT_STAGE[value["stage"]]
            valid = value["intent"] == required_intent and stage == expected_stage
        if not valid:
            _reject("journal_transition_invalid")
        updated = dict(value)
        updated["stage"] = stage
        updated["intent"] = None
        updated["revision"] += 1
        _atomic_write(root_fd, updated)
        return updated
    finally:
        os.close(root_fd)


__all__ = [
    "ENGINE",
    "INTENT_COMMIT",
    "INTENT_ROLLBACK",
    "INTENT_START_SERVICES",
    "INTENT_STOP_SERVICES",
    "INTENT_SWITCH_CURRENT",
    "JOURNAL_NAME",
    "MAX_JOURNAL_BYTES",
    "SCHEMA_VERSION",
    "STAGES",
    "TERMINAL_STAGES",
    "UpgradeJournalError",
    "advance_journal",
    "begin_rollback_retry",
    "create_journal",
    "load_journal",
    "record_intent",
    "record_rollback_error",
    "record_switch_intent",
]
