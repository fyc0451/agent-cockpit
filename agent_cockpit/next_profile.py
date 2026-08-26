"""Fail-closed runtime scope for the isolated Cockpit Next source tree."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Mapping

from agent_cockpit import agent_mail_discovery, auth_token


PROFILE_ENV = "COCKPIT_NEXT_PROFILE"
PROJECT_ROOT_ENV = "COCKPIT_PROJECT_ROOT"
FIXED_PROFILE = "1"
EPHEMERAL_PROFILE = "ephemeral"
DEV_PROFILE = "dev"
PROJECT_MARKER = "agent-cockpit-next"
SESSION = "github-agent-cockpit-next"
HOST = "127.0.0.1"
FIXED_HOSTS = frozenset({HOST, "0.0.0.0"})
PORT = "18790"
DEV_PORT = "8790"
DEV_UNIT = "agent-cockpit-dev.service"


def default_dev_project_root(repo: Path, home: Path | None = None) -> Path:
    """Discovery root for source 8790. Never Home, never a forced ~/github."""
    home_root = (Path.home() if home is None else home).resolve()
    repo = repo.resolve()
    blocked = {Path("/"), home_root, home_root.parent.resolve()}
    for candidate in (repo.parent, repo):
        try:
            if candidate in blocked or not candidate.is_dir() or candidate.is_symlink():
                continue
        except OSError:
            continue
        return candidate
    raise NextProfileError("project_root_missing")


def dev_layout(home: Path, repo: Path) -> dict[str, str]:
    """8790 正式根：与 dashboard-data / .config/agent-cockpit 同一套，不再用 *-next*。"""
    home = home.resolve()
    repo = repo.resolve()
    data = home / "dashboard-data"
    config = home / ".config" / "agent-cockpit"
    state = home / ".local" / "state" / "agent-cockpit"
    uploads = home / "dashboard-uploads"
    agent_mail_db = agent_mail_discovery.discover_agent_mail_db_path(
        home=home,
        data_home=home / ".local" / "share",
    )
    return {
        "COCKPIT_NEXT_WORKTREE": str(repo),
        "COCKPIT_PORT": DEV_PORT,
        "COCKPIT_DATA_DIR": str(data),
        "COCKPIT_CONFIG_DIR": str(config),
        "COCKPIT_STATE_DIR": str(state),
        "COCKPIT_UPLOADS_DIR": str(uploads),
        "COCKPIT_COORDINATION_DB": str(data / "coordination.sqlite3"),
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH": str(data / "launch-descriptors.json"),
        "AGENT_COCKPIT_RELEASE_STATE_DIR": str(state / "release-lane"),
        "COCKPIT_SYSTEMD_UNIT": DEV_UNIT,
        "COCKPIT_UPGRADE_V2_ENABLED": "0",
        "COCKPIT_B0_MODE": "off",
        "COCKPIT_HERDR_STATE_MODE": "off",
        "COCKPIT_EDITION": "source",
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "HERDR_CONFIG_PATH": str(home / ".config" / "herdr" / "config.toml"),
        "AGENT_MAIL_DB_PATH": str(agent_mail_db),
        "AGENT_MAIL_PROJECT": str(repo),
        "HERDR_SESSION": SESSION,
        "TEAM_HUB_URL": "http://127.0.0.1:8765",
        "HUMAN_AUTH_URL": "http://127.0.0.1:8766",
    }
EPHEMERAL_ROOT_ENV = "COCKPIT_EPHEMERAL_ROOT"
EPHEMERAL_LISTEN_FD_ENV = "COCKPIT_EPHEMERAL_LISTEN_FD"
EPHEMERAL_READY_TOKEN_ENV = "COCKPIT_EPHEMERAL_READY_TOKEN"
EPHEMERAL_MARKER = ".cockpit-ephemeral-root.json"
EPHEMERAL_CATALOG = ".cockpit-ephemeral-catalog.json"
_EPHEMERAL_MUTABLE_LEASE = "data/instance.lock"
_EPHEMERAL_SESSION_PREFIX = "ephemeral-"
_AUTHORIZED_SOCKET_NAMES = frozenset({"herdr.sock", "herdr-client.sock"})
_AUTHORIZED_LOG_NAMES = frozenset({"herdr-server.log"})
_EPHEMERAL_SCHEMA_VERSION = 1
_MAX_EPHEMERAL_CATALOG_BYTES = 1024 * 1024
_EPHEMERAL_HERDR_HOME_ROOT = Path("/tmp")
_EPHEMERAL_HERDR_HOME_PREFIX = "e-"
_EPHEMERAL_HERDR_HOME_BINDING = ".cockpit-root.json"
_AF_UNIX_PATH_MAX = 107
EPHEMERAL_LAYOUT = {
    "COCKPIT_DATA_DIR": "data",
    "COCKPIT_CONFIG_DIR": "config",
    "COCKPIT_STATE_DIR": "state",
    "COCKPIT_UPLOADS_DIR": "uploads",
    "AGENT_MAIL_DB_PATH": "mail/storage.sqlite3",
    "AGENT_COCKPIT_RELEASE_STATE_DIR": "release",
    "HERDR_CONFIG_PATH": "herdr/config.toml",
    "HOME": "home",
    "TMPDIR": "tmp",
}
FULL_ENV_NAMES = (
    "COCKPIT_NEXT_WORKTREE",
    "COCKPIT_HOST",
    "COCKPIT_PORT",
    "COCKPIT_DATA_DIR",
    "COCKPIT_CONFIG_DIR",
    "COCKPIT_STATE_DIR",
    "COCKPIT_UPLOADS_DIR",
    "COCKPIT_COORDINATION_DB",
    "COCKPIT_LAUNCH_DESCRIPTORS_PATH",
    "AGENT_COCKPIT_RELEASE_STATE_DIR",
    "COCKPIT_SYSTEMD_UNIT",
    "COCKPIT_UPGRADE_V2_ENABLED",
    "COCKPIT_B0_MODE",
    "COCKPIT_HERDR_STATE_MODE",
    "COCKPIT_EDITION",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "HERDR_CONFIG_PATH",
    "AGENT_MAIL_DB_PATH",
    "AGENT_MAIL_PROJECT",
    "HERDR_SESSION",
    "TEAM_HUB_URL",
    "HUMAN_AUTH_URL",
)


class NextProfileError(RuntimeError):
    pass


PRIVATE_HERDR_CONFIG = b'''onboarding = false

[ui]
agent_panel_sort = "spaces"

[ui.toast]
delivery = "terminal"

[theme]
name = "catppuccin"
auto_switch = false

[terminal]
default_shell = "/bin/sh"
shell_mode = "non_login"
'''


def _herdr_config_error(code: str, cause: OSError | None = None) -> NextProfileError:
    error = NextProfileError(code)
    if cause is not None:
        error.__cause__ = cause
    return error


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise _herdr_config_error("next_herdr_config_unsafe")
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_bound_directory(path: Path) -> int:
    if not path.is_absolute() or path != Path(os.path.normpath(path)):
        raise _herdr_config_error("next_herdr_config_unsafe")
    flags = _directory_flags()
    descriptor: int | None = None
    try:
        descriptor = os.open(path.anchor, flags)
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            parent = descriptor
            descriptor = child
            try:
                os.close(parent)
            except OSError:
                try:
                    os.close(child)
                except OSError:
                    pass
                descriptor = None
                raise
        return descriptor
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise _herdr_config_error("next_herdr_config_unsafe", exc)


def _require_private_directory(descriptor: int) -> None:
    try:
        info = os.fstat(descriptor)
    except OSError as exc:
        raise _herdr_config_error("next_herdr_config_unsafe", exc)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise _herdr_config_error("next_herdr_config_unsafe")


def _safe_config_info(directory: int) -> os.stat_result | None:
    try:
        info = os.stat("config.toml", dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _herdr_config_error("next_herdr_config_unsafe", exc)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise _herdr_config_error("next_herdr_config_unsafe")
    return info


def _read_bound_config(directory: int, expected: os.stat_result) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open("config.toml", flags, dir_fd=directory)
        actual = os.fstat(descriptor)
        if (
            (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
            or not stat.S_ISREG(actual.st_mode)
            or actual.st_uid != os.getuid()
            or stat.S_IMODE(actual.st_mode) != 0o600
            or actual.st_nlink != 1
        ):
            raise _herdr_config_error("next_herdr_config_unsafe")
        chunks: list[bytes] = []
        remaining = len(PRIVATE_HERDR_CONFIG) + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except NextProfileError:
        raise
    except OSError as exc:
        raise _herdr_config_error("next_herdr_config_unsafe", exc)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise _herdr_config_error("next_herdr_config_unsafe", exc)


def _write_bound_config(directory: int) -> None:
    descriptor: int | None = None
    temporary_exists = False
    try:
        temporary = f".config.toml.tmp-{os.getpid()}-{os.urandom(8).hex()}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory,
        )
        temporary_exists = True
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(PRIVATE_HERDR_CONFIG):
            written = os.write(descriptor, PRIVATE_HERDR_CONFIG[offset:])
            if written <= 0:
                raise OSError("short private Herdr config write")
            offset += written
        os.fsync(descriptor)
        completed = descriptor
        descriptor = None
        os.close(completed)
        os.replace(
            temporary, "config.toml",
            src_dir_fd=directory, dst_dir_fd=directory,
        )
        temporary_exists = False
        os.fsync(directory)
    except OSError as exc:
        raise _herdr_config_error("next_herdr_config_write_failed", exc)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_exists:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass


def ensure_private_herdr_config(environment: Mapping[str, str]) -> Path:
    """Create the exact product-owned Herdr config before Herdr can start."""
    try:
        profile = environment.get(PROFILE_ENV)
        if profile in {FIXED_PROFILE, DEV_PROFILE}:
            authority = Path(_required("COCKPIT_CONFIG_DIR", environment))
        elif profile == EPHEMERAL_PROFILE:
            authority = _ephemeral_root(environment)
        else:
            raise _herdr_config_error("next_herdr_config_unsafe")
        config = Path(_required("HERDR_CONFIG_PATH", environment))
    except NextProfileError as exc:
        if str(exc).startswith("next_herdr_config_"):
            raise
        raise _herdr_config_error("next_herdr_config_unsafe") from exc
    expected = authority / "herdr" / "config.toml"
    if config != expected:
        raise _herdr_config_error("next_herdr_config_unsafe")

    authority_fd: int | None = None
    herdr_fd: int | None = None
    try:
        authority_fd = _open_bound_directory(authority)
        _require_private_directory(authority_fd)
        try:
            os.mkdir("herdr", mode=0o700, dir_fd=authority_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _herdr_config_error("next_herdr_config_write_failed", exc)
        try:
            herdr_fd = os.open("herdr", _directory_flags(), dir_fd=authority_fd)
        except OSError as exc:
            raise _herdr_config_error("next_herdr_config_unsafe", exc)
        _require_private_directory(herdr_fd)
        info = _safe_config_info(herdr_fd)
        if info is not None and _read_bound_config(herdr_fd, info) == PRIVATE_HERDR_CONFIG:
            return config
        _write_bound_config(herdr_fd)
        written = _safe_config_info(herdr_fd)
        if (
            written is None
            or _read_bound_config(herdr_fd, written) != PRIVATE_HERDR_CONFIG
        ):
            raise _herdr_config_error("next_herdr_config_write_failed")
        return config
    finally:
        close_error: OSError | None = None
        for descriptor in (herdr_fd, authority_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    close_error = close_error or exc
        if close_error is not None:
            raise _herdr_config_error("next_herdr_config_write_failed", close_error)


def _ephemeral_error(code: str) -> NextProfileError:
    return NextProfileError(f"ephemeral_catalog_{code}")


def _strict_json(payload: bytes, code: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise _ephemeral_error(code) from exc


def _private_file(path: Path, *, required: bool = True) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not required:
            return None
        raise _ephemeral_error("invalid") from None
    except OSError as exc:
        raise _ephemeral_error("invalid") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_size > _MAX_EPHEMERAL_CATALOG_BYTES
    ):
        raise _ephemeral_error("invalid")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _ephemeral_error("invalid") from exc


def _write_private_json(path: Path, value: object) -> bytes:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short private write")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        return payload
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise _ephemeral_error("write_failed") from exc


def _root_marker(root: Path, *, require_ready: bool) -> dict[str, object]:
    payload = _private_file(root / EPHEMERAL_MARKER)
    assert payload is not None
    value = _strict_json(payload, "invalid")
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "root_id", "state", "catalog_sha256",
    }:
        raise _ephemeral_error("invalid")
    root_id = value["root_id"]
    digest = value["catalog_sha256"]
    if (
        value["schema_version"] != _EPHEMERAL_SCHEMA_VERSION
        or isinstance(value["schema_version"], bool)
        or not isinstance(root_id, str)
        or len(root_id) != 64
        or any(character not in "0123456789abcdef" for character in root_id)
        or value["state"] not in {"initializing", "running", "ready"}
        or (digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ))
        or (value["state"] in {"initializing", "running"} and digest is not None)
        or (value["state"] == "ready" and digest is None)
        or (require_ready and value["state"] != "ready")
    ):
        raise _ephemeral_error("invalid")
    return value


def ephemeral_session_name(root_id: str) -> str:
    if (
        not isinstance(root_id, str)
        or len(root_id) != 64
        or any(character not in "0123456789abcdef" for character in root_id)
    ):
        raise _ephemeral_error("invalid")
    return f"{_EPHEMERAL_SESSION_PREFIX}{root_id[:32]}"


def ephemeral_session_for_root(root: Path) -> str:
    marker = _root_marker(root, require_ready=False)
    return ephemeral_session_name(str(marker["root_id"]))


def _ephemeral_herdr_home_error(code: str) -> NextProfileError:
    return NextProfileError(f"ephemeral_herdr_home_{code}")


def ephemeral_herdr_config_home(root: Path) -> Path:
    marker = _root_marker(root, require_ready=False)
    root_id = str(marker["root_id"])
    home = _EPHEMERAL_HERDR_HOME_ROOT / (
        _EPHEMERAL_HERDR_HOME_PREFIX + root_id[:20]
    )
    session = ephemeral_session_name(root_id)
    for name in _AUTHORIZED_SOCKET_NAMES:
        path = home / "herdr" / "sessions" / session / name
        if len(os.fsencode(path)) > _AF_UNIX_PATH_MAX:
            raise _ephemeral_herdr_home_error("path_too_long")
    return home


def _private_owned_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise _ephemeral_herdr_home_error("invalid") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise _ephemeral_herdr_home_error("invalid")


def _herdr_home_binding(root: Path) -> dict[str, object]:
    marker = _root_marker(root, require_ready=False)
    return {
        "schema_version": 1,
        "root_id": marker["root_id"],
        "runtime_root": str(root),
    }


def _read_herdr_home_binding(path: Path) -> dict[str, object]:
    try:
        payload = _private_file(path)
        assert payload is not None
        value = _strict_json(payload, "invalid")
    except NextProfileError as exc:
        raise _ephemeral_herdr_home_error("invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "root_id", "runtime_root",
    }:
        raise _ephemeral_herdr_home_error("invalid")
    return value


def _validate_ephemeral_herdr_config_home(root: Path, home: Path) -> Path:
    expected = ephemeral_herdr_config_home(root)
    if home != expected:
        raise _ephemeral_herdr_home_error("invalid")
    _private_owned_directory(home)
    binding = _read_herdr_home_binding(home / _EPHEMERAL_HERDR_HOME_BINDING)
    if binding != _herdr_home_binding(root):
        if (
            binding.get("schema_version") == 1
            and isinstance(binding.get("root_id"), str)
            and isinstance(binding.get("runtime_root"), str)
        ):
            raise _ephemeral_herdr_home_error("collision")
        raise _ephemeral_herdr_home_error("invalid")
    link = home / "herdr"
    target = root / "config" / "herdr"
    try:
        info = link.lstat()
        linked = os.readlink(link)
        names = {entry.name for entry in home.iterdir()}
    except OSError as exc:
        raise _ephemeral_herdr_home_error("invalid") from exc
    if (
        not stat.S_ISLNK(info.st_mode)
        or linked != str(target)
        or names != {_EPHEMERAL_HERDR_HOME_BINDING, "herdr"}
    ):
        raise _ephemeral_herdr_home_error("invalid")
    _private_owned_directory(target)
    return home


def prepare_ephemeral_herdr_config_home(root: Path) -> Path:
    runtime_root = _ephemeral_root({EPHEMERAL_ROOT_ENV: str(root)})
    home = ephemeral_herdr_config_home(runtime_root)
    base = home.parent
    try:
        base_info = base.lstat()
    except OSError as exc:
        raise _ephemeral_herdr_home_error("invalid") from exc
    if (
        not stat.S_ISDIR(base_info.st_mode)
        or stat.S_ISLNK(base_info.st_mode)
        or (base_info.st_mode & stat.S_ISVTX) == 0
    ):
        raise _ephemeral_herdr_home_error("invalid")

    config = runtime_root / "config"
    _private_owned_directory(config)
    target = config / "herdr"
    created_target = False
    created_home = False
    completed = False
    try:
        try:
            target.mkdir(mode=0o700)
            created_target = True
        except FileExistsError:
            pass
        _private_owned_directory(target)
        try:
            home.mkdir(mode=0o700)
            created_home = True
        except FileExistsError:
            result = _validate_ephemeral_herdr_config_home(runtime_root, home)
            completed = True
            return result
        _write_private_json(
            home / _EPHEMERAL_HERDR_HOME_BINDING,
            _herdr_home_binding(runtime_root),
        )
        os.symlink(str(target), home / "herdr")
        directory = os.open(home, _directory_flags())
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        result = _validate_ephemeral_herdr_config_home(runtime_root, home)
        completed = True
        return result
    except NextProfileError:
        raise
    except OSError as exc:
        raise _ephemeral_herdr_home_error("write_failed") from exc
    finally:
        if created_home and not completed:
            try:
                (home / "herdr").unlink()
            except OSError:
                pass
            try:
                (home / _EPHEMERAL_HERDR_HOME_BINDING).unlink()
            except OSError:
                pass
            try:
                home.rmdir()
            except OSError:
                pass
        if created_target and not completed:
            try:
                target.rmdir()
            except OSError:
                pass


def release_ephemeral_herdr_config_home(root: Path) -> None:
    runtime_root = _ephemeral_root({EPHEMERAL_ROOT_ENV: str(root)})
    home = ephemeral_herdr_config_home(runtime_root)
    try:
        home.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _ephemeral_herdr_home_error("cleanup_failed") from exc
    _validate_ephemeral_herdr_config_home(runtime_root, home)
    try:
        (home / "herdr").unlink()
        (home / _EPHEMERAL_HERDR_HOME_BINDING).unlink()
        home.rmdir()
    except OSError as exc:
        raise _ephemeral_herdr_home_error("cleanup_failed") from exc


def _authorized_session_prefix(root_id: str) -> str:
    return f"config/herdr/sessions/{ephemeral_session_name(root_id)}"


def _authorized_session_leaf(relative: str, root_id: str) -> str | None:
    prefix = _authorized_session_prefix(root_id)
    if relative == prefix:
        return ""
    head = f"{prefix}/"
    if not relative.startswith(head):
        return None
    leaf = relative[len(head):]
    if leaf == "" or "/" in leaf:
        return None
    return leaf


def _is_authorized_socket(relative: str, root_id: str) -> bool:
    return _authorized_session_leaf(relative, root_id) in _AUTHORIZED_SOCKET_NAMES


def _is_authorized_log(relative: str, root_id: str) -> bool:
    return _authorized_session_leaf(relative, root_id) in _AUTHORIZED_LOG_NAMES


def _omitted_runtime_leaf(relative: str, root_id: str) -> bool:
    return _is_authorized_socket(relative, root_id) or _is_authorized_log(relative, root_id)


def _catalog_entries(root: Path) -> list[dict[str, object]]:
    root_id = str(_root_marker(root, require_ready=False)["root_id"])
    result: list[dict[str, object]] = []
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        names = sorted([*directories, *filenames])
        for name in names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if relative in {EPHEMERAL_MARKER, EPHEMERAL_CATALOG}:
                continue
            try:
                info = path.lstat()
            except OSError as exc:
                raise _ephemeral_error("invalid") from exc
            if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
                raise _ephemeral_error("invalid")
            mode = stat.S_IMODE(info.st_mode)
            if _is_authorized_socket(relative, root_id):
                if (
                    not stat.S_ISSOCK(info.st_mode)
                    or mode != 0o600
                    or info.st_nlink != 1
                ):
                    raise _ephemeral_error("invalid")
                continue
            if _is_authorized_log(relative, root_id):
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or (mode & 0o400) == 0
                    or (mode & 0o022) != 0
                ):
                    raise _ephemeral_error("invalid")
                continue
            if stat.S_ISDIR(info.st_mode):
                result.append({
                    "path": relative,
                    "type": "directory",
                    "mode": mode,
                    "uid": info.st_uid,
                    "sha256": None,
                })
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise _ephemeral_error("invalid")
                if relative == _EPHEMERAL_MUTABLE_LEASE:
                    digest = None
                else:
                    with path.open("rb") as opened:
                        digest = hashlib.file_digest(opened, "sha256").hexdigest()
                result.append({
                    "path": relative,
                    "type": "file",
                    "mode": mode,
                    "uid": info.st_uid,
                    "sha256": digest,
                })
            else:
                raise _ephemeral_error("invalid")
    return sorted(result, key=lambda entry: str(entry["path"]))


def _valid_catalog_entries(value: object, root_id: str) -> list[dict[str, object]]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "root_id", "entries",
    }:
        raise _ephemeral_error("invalid")
    if (
        value["schema_version"] != _EPHEMERAL_SCHEMA_VERSION
        or isinstance(value["schema_version"], bool)
        or value["root_id"] != root_id
        or not isinstance(value["entries"], list)
    ):
        raise _ephemeral_error("invalid")
    entries: list[dict[str, object]] = []
    previous = ""
    for entry in value["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "path", "type", "mode", "uid", "sha256",
        }:
            raise _ephemeral_error("invalid")
        path = entry["path"]
        entry_type = entry["type"]
        mode = entry["mode"]
        owner = entry["uid"]
        digest = entry["sha256"]
        if (
            not isinstance(path, str)
            or not path
            or path != Path(path).as_posix()
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path in {EPHEMERAL_MARKER, EPHEMERAL_CATALOG}
            or path <= previous
            or _omitted_runtime_leaf(path, root_id)
            or entry_type not in {"directory", "file"}
            or not isinstance(mode, int)
            or isinstance(mode, bool)
            or not 0 <= mode <= 0o777
            or owner != os.getuid()
            or isinstance(owner, bool)
            or (entry_type == "directory" and digest is not None)
            or (path == _EPHEMERAL_MUTABLE_LEASE and digest is not None)
            or (entry_type == "file" and (
                path != _EPHEMERAL_MUTABLE_LEASE and (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                )
            ))
        ):
            raise _ephemeral_error("invalid")
        previous = path
        entries.append(entry)
    return entries


def initialize_empty_ephemeral_runtime_root(root: Path) -> bool:
    """Bind a completely empty caller-owned root before its first launch."""
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise _ephemeral_error("invalid") from exc
    if not entries:
        marker = {
            "schema_version": _EPHEMERAL_SCHEMA_VERSION,
            "root_id": os.urandom(32).hex(),
            "state": "initializing",
            "catalog_sha256": None,
        }
        _write_private_json(root / EPHEMERAL_MARKER, marker)
        return True
    return False


def prepare_ephemeral_runtime_root(root: Path) -> None:
    """Verify a non-empty root was produced by this harness's clean stop."""
    try:
        if not list(root.iterdir()):
            raise _ephemeral_error("invalid")
    except OSError as exc:
        raise _ephemeral_error("invalid") from exc
    marker = _root_marker(root, require_ready=True)
    payload = _private_file(root / EPHEMERAL_CATALOG)
    assert payload is not None
    digest = hashlib.sha256(payload).hexdigest()
    if digest != marker["catalog_sha256"]:
        raise _ephemeral_error("invalid")
    expected = _valid_catalog_entries(_strict_json(payload, "invalid"), marker["root_id"])
    if _catalog_entries(root) != expected:
        raise _ephemeral_error("invalid")


def activate_ephemeral_runtime_root(root: Path) -> None:
    """Invalidate restart evidence before the launcher can exec the server."""
    marker = _root_marker(root, require_ready=False)
    if marker["state"] not in {"initializing", "ready"}:
        raise _ephemeral_error("invalid")
    marker["state"] = "running"
    marker["catalog_sha256"] = None
    _write_private_json(root / EPHEMERAL_MARKER, marker)


def finalize_ephemeral_runtime_root(
    environment: Mapping[str, str] | None = None,
) -> None:
    """Record the exact clean-stop layout before a same-root restart is allowed."""
    env = os.environ if environment is None else environment
    root = _ephemeral_root(env)
    marker = _root_marker(root, require_ready=False)
    if marker["state"] != "running":
        raise _ephemeral_error("invalid")
    entries = _catalog_entries(root)
    catalog = {
        "schema_version": _EPHEMERAL_SCHEMA_VERSION,
        "root_id": marker["root_id"],
        "entries": entries,
    }
    payload = _write_private_json(root / EPHEMERAL_CATALOG, catalog)
    release_ephemeral_herdr_config_home(root)
    marker["state"] = "ready"
    marker["catalog_sha256"] = hashlib.sha256(payload).hexdigest()
    _write_private_json(root / EPHEMERAL_MARKER, marker)


def enabled(environment: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environment is None else environment
    return env.get(PROFILE_ENV) in {FIXED_PROFILE, EPHEMERAL_PROFILE, DEV_PROFILE}


def is_ephemeral(environment: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environment is None else environment
    return env.get(PROFILE_ENV) == EPHEMERAL_PROFILE


def is_dev(environment: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environment is None else environment
    return env.get(PROFILE_ENV) == DEV_PROFILE


def is_next_source(root: Path) -> bool:
    try:
        return (root / ".agent-memory-project").read_text(
            encoding="ascii"
        ) == f"{PROJECT_MARKER}\n"
    except (OSError, UnicodeError):
        return False


def _required(name: str, environment: Mapping[str, str] | None = None) -> str:
    env = os.environ if environment is None else environment
    value = env.get(name)
    if not isinstance(value, str) or not value:
        raise NextProfileError(f"next_profile_missing:{name}")
    return value


def configured_project_root(
    environment: Mapping[str, str] | None = None, *, home: Path | None = None,
) -> Path:
    """Return the one explicit, non-sensitive root exposed to fixed discovery."""
    env = os.environ if environment is None else environment
    raw = _required(PROJECT_ROOT_ENV, env)
    lexical = Path(raw)
    if (
        not lexical.is_absolute()
        or raw != str(lexical)
        or ".." in lexical.parts
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise NextProfileError("project_root_invalid")
    try:
        resolved = lexical.resolve(strict=True)
        info = lexical.lstat()
    except FileNotFoundError:
        raise NextProfileError("project_root_missing") from None
    except (OSError, RuntimeError, ValueError):
        raise NextProfileError("project_root_invalid") from None
    if (
        resolved != lexical
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
    ):
        raise NextProfileError("project_root_invalid")

    home_root = (Path.home() if home is None else home).resolve()
    if resolved in {Path("/"), home_root, home_root.parent.resolve()}:
        raise NextProfileError("project_root_unsafe")
    blocked = [
        Path("/etc"), Path("/proc"), Path("/sys"), Path("/dev"), Path("/run"),
        home_root / ".ssh", home_root / ".gnupg", home_root / ".agent-mail",
        home_root / ".config" / "agent-cockpit",
    ]
    blocked.extend(
        Path(value).resolve(strict=False)
        for name in (
            "COCKPIT_DATA_DIR", "COCKPIT_CONFIG_DIR", "COCKPIT_STATE_DIR",
            "COCKPIT_UPLOADS_DIR",
        )
        if (value := env.get(name))
    )
    for root in blocked:
        try:
            resolved.relative_to(root.resolve(strict=False))
        except ValueError:
            continue
        raise NextProfileError("project_root_unsafe")
    return resolved


def project(environment: Mapping[str, str] | None = None) -> str | None:
    if not enabled(environment):
        return None
    if is_ephemeral(environment) or is_dev(environment):
        env = os.environ if environment is None else environment
        project = _required("AGENT_MAIL_PROJECT", env)
        worktree = _required("COCKPIT_NEXT_WORKTREE", env)
        try:
            if Path(project).resolve(strict=True) != Path(worktree).resolve(strict=True):
                raise NextProfileError("next_profile_invalid:AGENT_MAIL_PROJECT")
        except OSError as exc:
            raise NextProfileError("next_profile_invalid:AGENT_MAIL_PROJECT") from exc
        return project
    raw = _required("AGENT_MAIL_PROJECT", environment)
    path = Path(raw).expanduser()
    wanted = Path.home().resolve() / "github" / "agent-cockpit-next"
    if (
        not path.is_absolute()
        or path.resolve(strict=False) != path
        or path != wanted
    ):
        raise NextProfileError("next_profile_invalid:AGENT_MAIL_PROJECT")
    return str(path)


def require_project(
    value: str, environment: Mapping[str, str] | None = None,
) -> str:
    if is_dev(environment):
        lexical = Path(value).expanduser()
        if not lexical.is_absolute():
            raise NextProfileError("next_project_forbidden")
        try:
            resolved = lexical.resolve(strict=True)
        except OSError as exc:
            raise NextProfileError("next_project_forbidden") from exc
        if not resolved.is_dir():
            raise NextProfileError("next_project_forbidden")
        home_root = Path.home().resolve()
        try:
            resolved.relative_to(home_root)
        except ValueError as exc:
            raise NextProfileError("next_project_forbidden") from exc
        blocked = (
            home_root / ".ssh",
            home_root / ".gnupg",
            home_root / ".agent-mail",
            home_root / ".config",
        )
        for root in blocked:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            raise NextProfileError("next_project_forbidden")
        return str(resolved)
    scoped = project(environment)
    resolved = str(Path(value).expanduser().resolve(strict=False))
    if scoped is not None and resolved != scoped:
        raise NextProfileError("next_project_forbidden")
    return resolved


def require_retirement_project(
    value: str, environment: Mapping[str, str] | None = None,
) -> str:
    """校验身份退休目标项目。

    8790 删除工作区后，目录可能已不存在，但退休仍可依据本地
    registry 中精确的 ``project_key`` 收敛。此入口只放宽目录存在性；
    绝对路径、Home 范围和敏感目录边界保持与 ``require_project`` 一致。
    调用方必须继续做精确 registry 身份匹配，不能把它当通用项目路径。
    """
    if not is_dev(environment):
        return require_project(value, environment)
    lexical = Path(value).expanduser()
    if not lexical.is_absolute():
        raise NextProfileError("next_project_forbidden")
    try:
        resolved = lexical.resolve(strict=False)
    except OSError as exc:
        raise NextProfileError("next_project_forbidden") from exc
    home_root = Path.home().resolve()
    try:
        resolved.relative_to(home_root)
    except ValueError as exc:
        raise NextProfileError("next_project_forbidden") from exc
    blocked = (
        home_root / ".ssh",
        home_root / ".gnupg",
        home_root / ".agent-mail",
        home_root / ".config",
    )
    for root in blocked:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        raise NextProfileError("next_project_forbidden")
    return str(resolved)


def session(environment: Mapping[str, str] | None = None) -> str | None:
    if not enabled(environment):
        return None
    value = _required("HERDR_SESSION", environment)
    if is_dev(environment):
        # dev 允许多会话；HERDR_SESSION 只作 profile 必填项，不锁死唯一 session 名。
        return None
    if is_ephemeral(environment):
        env = os.environ if environment is None else environment
        if (
            not value.startswith(_EPHEMERAL_SESSION_PREFIX)
            or len(value) != len(_EPHEMERAL_SESSION_PREFIX) + 32
            or any(
                character not in "0123456789abcdef"
                for character in value[len(_EPHEMERAL_SESSION_PREFIX):]
            )
        ):
            raise NextProfileError("next_profile_invalid:HERDR_SESSION")
        if env.get(EPHEMERAL_ROOT_ENV):
            if value != ephemeral_session_for_root(_ephemeral_root(env)):
                raise NextProfileError("next_profile_invalid:HERDR_SESSION")
        return value
    if value != SESSION:
        raise NextProfileError("next_profile_invalid:HERDR_SESSION")
    return value


def require_session(
    value: str, environment: Mapping[str, str] | None = None,
) -> str:
    scoped = session(environment)
    if scoped is not None and value != scoped:
        raise NextProfileError("next_session_forbidden")
    return value


def require_helper_environment(
    names: tuple[str, ...], environment: Mapping[str, str] | None = None,
) -> None:
    if not enabled(environment):
        return
    env = os.environ if environment is None else environment
    for name in (*FULL_ENV_NAMES, *names):
        _required(name, environment)
    validate_server_environment(
        Path(_required("COCKPIT_NEXT_WORKTREE", env)), env,
    )


def _validate_fixed_server_environment(
    root: Path, environment: Mapping[str, str] | None = None,
) -> None:
    env = os.environ if environment is None else environment
    source_is_next = is_next_source(root)
    if source_is_next and not enabled(env):
        raise NextProfileError("next_profile_required")
    if not enabled(env):
        return
    if not source_is_next:
        raise NextProfileError("next_profile_wrong_source")

    home = Path.home().resolve()
    worktree = Path(_required("COCKPIT_NEXT_WORKTREE", env))
    expected = {
        "COCKPIT_NEXT_WORKTREE": str(root.resolve()),
        "COCKPIT_PORT": PORT,
        "COCKPIT_DATA_DIR": str(home / ".local/share/agent-cockpit-next-data"),
        "COCKPIT_CONFIG_DIR": str(home / ".config/agent-cockpit-next"),
        "COCKPIT_STATE_DIR": str(home / ".local/state/agent-cockpit-next"),
        "COCKPIT_UPLOADS_DIR": str(home / ".local/share/agent-cockpit-next-uploads"),
        "COCKPIT_COORDINATION_DB": str(
            home / ".local/share/agent-cockpit-next-data/coordination.sqlite3"
        ),
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH": str(
            home / ".local/share/agent-cockpit-next-data/launch-descriptors.json"
        ),
        "AGENT_COCKPIT_RELEASE_STATE_DIR": str(
            home / ".local/state/agent-cockpit-next/release-lane"
        ),
        "COCKPIT_SYSTEMD_UNIT": "agent-cockpit-next.service",
        "COCKPIT_UPGRADE_V2_ENABLED": "0",
        "COCKPIT_B0_MODE": "off",
        "COCKPIT_HERDR_STATE_MODE": "off",
        "COCKPIT_EDITION": "source",
        "XDG_DATA_HOME": str(home / ".local/share/agent-cockpit-next-data"),
        "XDG_CONFIG_HOME": str(home / ".config/agent-cockpit-next"),
        "XDG_STATE_HOME": str(home / ".local/state/agent-cockpit-next"),
        "HERDR_CONFIG_PATH": str(home / ".config/agent-cockpit-next/herdr/config.toml"),
        "AGENT_MAIL_DB_PATH": str(home / "mcp_agent_mail/storage.sqlite3"),
        "AGENT_MAIL_PROJECT": str(root.resolve()),
        "HERDR_SESSION": SESSION,
        "TEAM_HUB_URL": "http://127.0.0.1:8765",
        "HUMAN_AUTH_URL": "http://127.0.0.1:8766",
    }
    if not worktree.is_absolute() or worktree.resolve(strict=False) != root.resolve():
        raise NextProfileError("next_profile_invalid:COCKPIT_NEXT_WORKTREE")
    host = env.get("COCKPIT_HOST")
    if host not in FIXED_HOSTS:
        raise NextProfileError("next_profile_invalid:COCKPIT_HOST")
    for name, wanted in expected.items():
        if env.get(name) != wanted:
            raise NextProfileError(f"next_profile_invalid:{name}")
    if host == "0.0.0.0":
        try:
            token = auth_token.load_cockpit_token(env)
        except auth_token.TokenFileError as exc:
            raise NextProfileError(exc.code) from exc
        if token is None:
            raise NextProfileError("next_profile_invalid:LAN_HOST_TOKEN_REQUIRED")
        if env.get("COCKPIT_TOKEN") != token:
            raise NextProfileError("next_profile_invalid:LAN_HOST_TOKEN_MISMATCH")
    configured_project_root(env)
    project(env)
    session(env)


def _ephemeral_root(environment: Mapping[str, str]) -> Path:
    raw = _required(EPHEMERAL_ROOT_ENV, environment)
    path = Path(raw)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.lstat()
    except OSError as exc:
        raise NextProfileError("ephemeral_root_invalid") from exc
    if (
        not path.is_absolute()
        or path != resolved
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise NextProfileError("ephemeral_root_invalid")
    return resolved


def _ephemeral_token(environment: Mapping[str, str]) -> str:
    token = _required(EPHEMERAL_READY_TOKEN_ENV, environment)
    if (
        len(token) != 32
        or any(character not in "0123456789abcdef" for character in token)
    ):
        raise NextProfileError("ephemeral_token_invalid")
    return token


def ephemeral_runtime(
    environment: Mapping[str, str] | None = None, *, include_listener: bool = True,
) -> dict[str, object] | None:
    if not is_ephemeral(environment):
        return None
    env = os.environ if environment is None else environment
    token = _ephemeral_token(env)
    port_text = _required("COCKPIT_PORT", env)
    if (
        not port_text.isdecimal()
        or port_text != str(int(port_text))
        or not 1 <= int(port_text) <= 65535
        or port_text in {"8790", PORT}
    ):
        raise NextProfileError("ephemeral_port_invalid")
    result: dict[str, object] = {"port": int(port_text), "ready_token": token}
    if include_listener:
        raw_fd = _required(EPHEMERAL_LISTEN_FD_ENV, env)
        if (
            not raw_fd.isdecimal()
            or raw_fd != str(int(raw_fd))
            or int(raw_fd) <= 2
        ):
            raise NextProfileError("ephemeral_listen_fd_invalid")
        result["listen_fd"] = int(raw_fd)
    return result


def validate_ephemeral_environment(
    root: Path, environment: Mapping[str, str] | None = None,
) -> None:
    env = os.environ if environment is None else environment
    if not is_ephemeral(env) or not is_next_source(root):
        raise NextProfileError("ephemeral_profile_invalid")
    try:
        worktree = Path(_required("COCKPIT_NEXT_WORKTREE", env)).resolve(strict=True)
    except OSError as exc:
        raise NextProfileError("ephemeral_worktree_invalid") from exc
    if worktree != root.resolve():
        raise NextProfileError("ephemeral_worktree_invalid")
    runtime_root = _ephemeral_root(env)
    for name, relative in EPHEMERAL_LAYOUT.items():
        if _required(name, env) != str(runtime_root / relative):
            raise NextProfileError(f"ephemeral_path_invalid:{name}")
    for name in (
        "COCKPIT_DATA_DIR", "COCKPIT_CONFIG_DIR", "COCKPIT_STATE_DIR",
        "COCKPIT_UPLOADS_DIR", "HOME", "TMPDIR",
    ):
        try:
            info = Path(_required(name, env)).lstat()
        except OSError as exc:
            raise NextProfileError(f"ephemeral_path_invalid:{name}") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise NextProfileError(f"ephemeral_path_invalid:{name}")
    _ephemeral_token(env)
    expected = {
        "COCKPIT_HOST": HOST,
        "COCKPIT_COORDINATION_DB": str(runtime_root / "data/coordination.sqlite3"),
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH": str(runtime_root / "data/launch-descriptors.json"),
        "AGENT_MAIL_PROJECT": str(worktree),
        "HERDR_SESSION": ephemeral_session_for_root(runtime_root),
        "COCKPIT_SYSTEMD_UNIT": "agent-cockpit-next-ephemeral.service",
        "COCKPIT_UPGRADE_V2_ENABLED": "0",
        "COCKPIT_B0_MODE": "off",
        "COCKPIT_HERDR_STATE_MODE": "off",
        "COCKPIT_EDITION": "source",
        "XDG_DATA_HOME": str(runtime_root / "data"),
        "XDG_CONFIG_HOME": str(ephemeral_herdr_config_home(runtime_root)),
        "XDG_STATE_HOME": str(runtime_root / "state"),
        "TEAM_HUB_URL": "http://127.0.0.1:9",
        "HUMAN_AUTH_URL": "http://127.0.0.1:9",
    }
    for name, value in expected.items():
        if _required(name, env) != value:
            raise NextProfileError(f"ephemeral_value_invalid:{name}")
    _validate_ephemeral_herdr_config_home(
        runtime_root, Path(_required("XDG_CONFIG_HOME", env)),
    )
    ephemeral_runtime(env)
    project(env)
    session(env)


def _validate_dev_server_environment(
    root: Path, environment: Mapping[str, str] | None = None,
) -> None:
    """Real-HOME source checkout on :8790. Not the isolated next/ephemeral profiles."""
    env = os.environ if environment is None else environment
    if not is_dev(env) or not is_next_source(root):
        raise NextProfileError("next_profile_wrong_source")
    home_env = env.get("HOME")
    if home_env:
        try:
            if Path(home_env).expanduser().resolve() != Path.home().resolve():
                raise NextProfileError("next_profile_invalid:HOME")
        except OSError as exc:
            raise NextProfileError("next_profile_invalid:HOME") from exc
    if env.get(EPHEMERAL_ROOT_ENV):
        raise NextProfileError("next_profile_invalid:COCKPIT_EPHEMERAL_ROOT")
    home = Path.home().resolve()
    worktree = Path(_required("COCKPIT_NEXT_WORKTREE", env))
    expected = dev_layout(home, root.resolve())
    if not worktree.is_absolute() or worktree.resolve(strict=False) != root.resolve():
        raise NextProfileError("next_profile_invalid:COCKPIT_NEXT_WORKTREE")
    host = env.get("COCKPIT_HOST")
    if host not in FIXED_HOSTS:
        raise NextProfileError("next_profile_invalid:COCKPIT_HOST")
    for name, wanted in expected.items():
        # 与 session() 一致：dev 允许多 herdr session，不锁死 github-agent-cockpit-next。
        if name == "HERDR_SESSION":
            continue
        # 启动后 Agent Mail 可能迁移/自愈；已选中的新旧兼容路径都保持合法，
        # 避免后续 helper 因文件出现或消失而把同一 server 环境判为失效。
        if name == "AGENT_MAIL_DB_PATH":
            compatible = {
                str(path)
                for path in agent_mail_discovery.agent_mail_db_candidates(
                    home=home,
                    data_home=home / ".local" / "share",
                )
            }
            if env.get(name) not in compatible:
                raise NextProfileError(f"next_profile_invalid:{name}")
            continue
        if env.get(name) != wanted:
            raise NextProfileError(f"next_profile_invalid:{name}")
    if host == "0.0.0.0":
        try:
            token = auth_token.load_cockpit_token(env)
        except auth_token.TokenFileError as exc:
            raise NextProfileError(exc.code) from exc
        if token is None:
            raise NextProfileError("next_profile_invalid:LAN_HOST_TOKEN_REQUIRED")
        if env.get("COCKPIT_TOKEN") != token:
            raise NextProfileError("next_profile_invalid:LAN_HOST_TOKEN_MISMATCH")
    configured_project_root(env)
    project(env)
    session(env)


def validate_server_environment(
    root: Path, environment: Mapping[str, str] | None = None,
) -> None:
    if is_ephemeral(environment):
        validate_ephemeral_environment(root, environment)
        return
    if is_dev(environment):
        _validate_dev_server_environment(root, environment)
        return
    _validate_fixed_server_environment(root, environment)
