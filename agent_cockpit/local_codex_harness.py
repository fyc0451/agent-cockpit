"""Read-only Codex/Herdr attach plus private capability/wakeup seam."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from . import herdr_client


PROVIDER = "local_herdr"
HARNESS = "codex_terminal_managed_v1"
KIND = "codex"
SANDBOX = "read-only"
LAUNCH_ARGS = ("--sandbox", "read-only")
WAKEUP_TEXT = "COCKPIT_WAKEUP_V1"
WAKEUP_DIGEST = "sha256:" + hashlib.sha256(WAKEUP_TEXT.encode()).hexdigest()
_ATTACHMENT_ID = re.compile(r"att_[0-9a-f]{32}\Z")


class HarnessError(RuntimeError):
    def __init__(
        self, code: str, pane_id: str | None = None, instance_id: str | None = None,
        unknown: bool = False,
    ):
        self.code = code
        self.pane_id = pane_id
        self.instance_id = instance_id
        self.unknown = unknown
        super().__init__(code)


def _fail(code: str, pane_id: str | None = None, unknown: bool = False) -> None:
    raise HarnessError(code, pane_id=pane_id, unknown=unknown)


def attachment_id_text(value: object) -> str:
    if not isinstance(value, str) or _ATTACHMENT_ID.fullmatch(value) is None:
        _fail("invalid_argument")
    return value


@dataclass(frozen=True)
class LaunchSpec:
    kind: str
    sandbox: str
    cwd: str
    args: tuple[str, ...]
    writable: bool = False

    def argv_text(self) -> str:
        return " ".join(self.args)

    def assert_readonly(self) -> None:
        if (
            self.kind != KIND
            or self.sandbox != SANDBOX
            or self.writable
            or self.args != LAUNCH_ARGS
            or "--sandbox" not in self.args
            or "read-only" not in self.args
        ):
            _fail("invalid_argument")


@dataclass(frozen=True)
class AttachmentEvidence:
    session: str
    instance_id: str
    pane_id: str
    cwd: str
    provider: str
    harness: str
    identity_verified: bool


class AgentHarnessAdapter(Protocol):
    def build_launch_spec(self, checkout_path: Path) -> LaunchSpec: ...

    def attach_readonly(
        self, *, session: str, checkout_path: Path,
        project_id: str, workspace_id: str,
        instance_id: str | None = None, display_name: str = "codex",
        attachment_id: str | None = None, identity_id: str | None = None,
        generation: int | None = None, fence: str | None = None,
    ) -> AttachmentEvidence: ...

    def observe(
        self, *, session: str, instance_id: str, pane_id: str, checkout_path: Path,
    ) -> AttachmentEvidence: ...

    def detach(self, *, session: str, pane_id: str) -> None: ...


_FORBIDDEN = frozenset({
    "pane_send", "pane_read", "agent.prompt", "agent_prompt", "prompt",
    "transcript",
})


class LocalCodexHarness:
    def __init__(
        self,
        *,
        ensure_session: Callable[..., Any] | None = None,
        start_agent: Callable[..., dict[str, Any]] | None = None,
        start_workspace_codex_home: Callable[..., dict[str, Any]] | None = None,
        get_launch_descriptor: Callable[..., dict[str, Any] | None] | None = None,
        get_launch_descriptor_by_instance: (
            Callable[..., dict[str, Any] | None] | None
        ) = None,
        snapshot: Callable[..., dict[str, Any]] | None = None,
        close_pane: Callable[..., dict[str, Any]] | None = None,
        new_instance_id: Callable[[], str] | None = None,
        capability_root: Path | None = None,
        wakeup_prompt: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self._ensure_session = ensure_session or herdr_client.ensure_session
        self._start_agent = start_agent or herdr_client.start_agent
        self._start_workspace_codex_home = (
            start_workspace_codex_home or herdr_client.start_workspace_codex_home
        )
        self._get_launch_descriptor = (
            get_launch_descriptor or herdr_client.get_launch_descriptor
        )
        self._get_launch_descriptor_by_instance = (
            get_launch_descriptor_by_instance
            or herdr_client.get_launch_descriptor_by_instance
        )
        self._snapshot = snapshot or herdr_client.session_snapshot
        self._close_pane = close_pane or herdr_client.close_pane
        self._new_instance_id = (
            new_instance_id or herdr_client.new_agent_instance_id
        )
        self._capability_root = (
            None if capability_root is None else Path(capability_root)
        )
        self._wakeup_prompt = wakeup_prompt or _default_wakeup_prompt
        for name in _FORBIDDEN:
            if hasattr(self, name):
                _fail("invalid_argument")

    def build_launch_spec(self, checkout_path: Path) -> LaunchSpec:
        path = Path(checkout_path)
        if not path.is_absolute() or ".." in path.parts:
            _fail("invalid_argument")
        spec = LaunchSpec(
            KIND, SANDBOX, str(path), LAUNCH_ARGS, writable=False,
        )
        spec.assert_readonly()
        return spec

    def issue_capability(
        self, *, attachment_id: str, identity_id: str, generation: int,
        fence: str, session: str, pane_id: str,
    ) -> dict[str, Any]:
        root = self._capability_root
        attachment_id = attachment_id_text(attachment_id)
        if root is None or generation < 1:
            _fail("invalid_argument")
        _private_dir(root)
        token = secrets.token_hex(32)
        payload = {
            "attachment_id": attachment_id,
            "identity_id": identity_id,
            "generation": generation,
            "fence": fence,
            "session": session,
            "pane_id": pane_id,
            "token": token,
        }
        cap = root / f"{attachment_id}.cap"
        current = root / f"{attachment_id}.generation"
        home = root / f"{attachment_id}.home"
        _private_dir(home)
        _write_private(cap, json.dumps(payload, separators=(",", ":")).encode())
        _write_private(current, str(generation).encode() + b"\n")
        _write_private(root / f"{attachment_id}.fence", fence.encode() + b"\n")
        _write_mcp_home_config(home, cap, root)
        return {
            "capability_path": cap,
            "codex_home": home,
            "generation": generation,
        }

    def wakeup(self, attachment_id: str, **extra: object) -> dict[str, str]:
        if extra:
            _fail("invalid_argument")
        record = self._bound_capability(attachment_id)
        sent = self._wakeup_prompt(
            record["session"], record["pane_id"], WAKEUP_TEXT,
        )
        if isinstance(sent, dict) and sent.get("available") is False:
            _fail("runtime_unavailable")
        return {"digest": WAKEUP_DIGEST, "text": WAKEUP_TEXT}

    def _require_cap(self, attachment_id: str) -> Path:
        root = self._capability_root
        attachment_id = attachment_id_text(attachment_id)
        if root is None:
            _fail("invalid_argument")
        _assert_private_ancestry(root)
        path = root / f"{attachment_id}.cap"
        try:
            info = path.lstat()
        except OSError:
            _fail("invalid_argument")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail("invalid_argument")
        return path

    def _prepare_private_runtime(
        self, *, attachment_id: str, identity_id: str, generation: int,
        fence: str, session: str, checkout: str,
    ) -> dict[str, Any]:
        root = self._capability_root
        if root is None or generation < 1:
            _fail("invalid_argument")
        _private_dir(root)
        token = secrets.token_hex(32)
        payload = {
            "attachment_id": attachment_id,
            "identity_id": identity_id,
            "generation": generation,
            "fence": fence,
            "session": session,
            "pane_id": None,
            "instance_id": None,
            "checkout": checkout,
            "token": token,
        }
        cap = root / f"{attachment_id}.cap"
        home = root / f"{attachment_id}.home"
        _private_dir(home)
        _write_private(cap, json.dumps(payload, separators=(",", ":")).encode())
        _write_private(root / f"{attachment_id}.generation", str(generation).encode() + b"\n")
        _write_private(root / f"{attachment_id}.fence", fence.encode() + b"\n")
        _write_mcp_home_config(home, cap, root)
        return {"capability_path": cap, "codex_home": home, "generation": generation}

    def _bind_private_runtime(
        self, *, attachment_id: str, pane_id: str, instance_id: str,
    ) -> None:
        record = self._require_capability_record(attachment_id)
        if record.get("pane_id") not in {None, ""}:
            if record.get("pane_id") != pane_id or record.get("instance_id") != instance_id:
                _fail("invalid_argument")
            return
        record["pane_id"] = pane_id
        record["instance_id"] = instance_id
        _write_private(
            self._require_cap(attachment_id),
            json.dumps(record, separators=(",", ":")).encode(),
        )

    def _bound_capability(self, attachment_id: str) -> dict[str, Any]:
        record = self._require_capability_record(attachment_id)
        pane_id = record.get("pane_id")
        if not isinstance(pane_id, str) or pane_id == "":
            _fail("invalid_argument")
        if not isinstance(record.get("session"), str) or record["session"] == "":
            _fail("invalid_argument")
        return record

    def _require_capability_record(self, attachment_id: str) -> dict[str, Any]:
        path = self._require_cap(attachment_id)
        record = _read_capability(path)
        if current_generation(path) != record.get("generation"):
            _fail("stale_generation")
        fence = record.get("fence")
        if not isinstance(fence, str) or fence == "":
            _fail("invalid_argument")
        sidecar = path.parent / f"{attachment_id_text(record.get('attachment_id'))}.fence"
        try:
            info = sidecar.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
            ):
                _fail("invalid_argument")
            raw = sidecar.read_bytes().decode("ascii").strip()
        except (OSError, UnicodeError):
            _fail("invalid_argument")
        if raw != fence:
            _fail("invalid_argument")
        return record

    def _retire_private_runtime(self, attachment_id: str) -> None:
        root = self._capability_root
        if root is None:
            return
        for name in (
            f"{attachment_id}.cap", f"{attachment_id}.generation",
            f"{attachment_id}.fence",
        ):
            try:
                (root / name).unlink()
            except FileNotFoundError:
                continue
            except OSError:
                continue
        try:
            (root / f"{attachment_id}.home" / "config.toml").unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def attach_readonly(
        self, *, session: str, checkout_path: Path,
        project_id: str, workspace_id: str,
        instance_id: str | None = None, display_name: str = "codex",
        attachment_id: str | None = None, identity_id: str | None = None,
        generation: int | None = None, fence: str | None = None,
    ) -> AttachmentEvidence:
        spec = self.build_launch_spec(checkout_path)
        if instance_id is None:
            instance_id = self._new_instance_id()
        if project_id is None or workspace_id is None:
            _fail("invalid_argument")
        if (
            self._capability_root is None
            or not isinstance(identity_id, str) or identity_id == ""
            or type(generation) is not int
            or not isinstance(fence, str) or fence == ""
        ):
            _fail("invalid_argument")
        attachment_id = attachment_id_text(attachment_id)
        issued = self._prepare_private_runtime(
            attachment_id=attachment_id, identity_id=identity_id,
            generation=generation, fence=fence, session=session,
            checkout=spec.cwd,
        )
        pane_id: str | None = None
        live_id = instance_id
        try:
            self._ensure_session(session=session)
        except Exception:
            self._retire_private_runtime(attachment_id)
            _fail("runtime_unavailable")
        try:
            started = self._start_workspace_codex_home(
                session=session,
                workdir=spec.cwd,
                instance_id=instance_id,
                project_id=project_id,
                workspace_id=workspace_id,
                codex_home=str(issued["codex_home"]),
                label=display_name,
                display_name=display_name,
            )
        except Exception:
            self._retire_private_runtime(attachment_id)
            _fail("runtime_unavailable")
        if not isinstance(started, dict) or started.get("available") is False:
            self._retire_private_runtime(attachment_id)
            _fail("runtime_unavailable")
        if started.get("error") or started.get("error_code"):
            pane_id = started.get("pane_id")
            if isinstance(pane_id, str) and pane_id:
                try:
                    self.detach(session=session, pane_id=pane_id)
                except HarnessError:
                    self._retire_private_runtime(attachment_id)
                    raise HarnessError(
                        "runtime_unavailable", pane_id=pane_id, instance_id=str(live_id),
                        unknown=True,
                    ) from None
            self._retire_private_runtime(attachment_id)
            if started.get("rolled_back") is False and not (
                isinstance(pane_id, str) and pane_id
            ):
                raise HarnessError(
                    "runtime_unavailable", pane_id=None, instance_id=str(live_id),
                    unknown=True,
                )
            _fail("runtime_unavailable")
        pane_id = started.get("pane_id")
        live_id = started.get("instance_id") or instance_id
        if not isinstance(pane_id, str) or not pane_id:
            self._retire_private_runtime(attachment_id)
            _fail("runtime_unavailable")
        try:
            evidence = self.observe(
                session=session, instance_id=str(live_id), pane_id=pane_id,
                checkout_path=checkout_path,
            )
            self._bind_private_runtime(
                attachment_id=attachment_id, pane_id=pane_id,
                instance_id=str(live_id),
            )
            return evidence
        except HarnessError as exc:
            self._retire_private_runtime(attachment_id)
            if exc.code in {"runtime_identity_unverified", "process_exited"}:
                try:
                    self.detach(session=session, pane_id=pane_id)
                except HarnessError:
                    raise HarnessError(
                        "runtime_unavailable", pane_id=pane_id, instance_id=str(live_id),
                    ) from None
                raise HarnessError(exc.code, pane_id=None, instance_id=str(live_id)) from None
            raise HarnessError(
                exc.code, pane_id=pane_id, instance_id=str(live_id),
                unknown=exc.unknown,
            ) from None
        except Exception:
            self._retire_private_runtime(attachment_id)
            raise HarnessError(
                "runtime_unavailable", pane_id=pane_id, instance_id=str(live_id),
                unknown=True,
            ) from None

    def observe(
        self, *, session: str, instance_id: str, pane_id: str, checkout_path: Path,
    ) -> AttachmentEvidence:
        spec = self.build_launch_spec(checkout_path)
        try:
            snap = self._snapshot(session=session)
        except Exception:
            _fail("runtime_unavailable", unknown=True)
        panes = snap.get("panes") if isinstance(snap, dict) else None
        if not isinstance(panes, list):
            _fail("runtime_unavailable")
        match = [
            pane for pane in panes
            if isinstance(pane, dict) and pane.get("pane_id") == pane_id
        ]
        if not match:
            _fail("process_exited")
        pane = match[0]
        cwd = pane.get("cwd") or pane.get("foreground_cwd")
        if not isinstance(cwd, str) or Path(cwd) != Path(spec.cwd):
            _fail("runtime_identity_unverified")
        if not _descriptor_matches(
            self._load_descriptors(session=session, pane_id=pane_id,
                                   instance_id=instance_id),
            session=session, pane_id=pane_id, instance_id=instance_id,
            cwd=spec.cwd,
        ):
            _fail("runtime_identity_unverified")
        return AttachmentEvidence(
            session, instance_id, pane_id, spec.cwd, PROVIDER, HARNESS, True,
        )

    def _load_descriptors(
        self, *, session: str, pane_id: str, instance_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        try:
            by_pane = self._get_launch_descriptor(session=session, pane_id=pane_id)
        except Exception:
            _fail("runtime_identity_unverified")
        try:
            by_instance = self._get_launch_descriptor_by_instance(
                instance_id=instance_id,
            )
        except Exception:
            _fail("runtime_identity_unverified")
        if by_pane is not None and not isinstance(by_pane, dict):
            _fail("runtime_identity_unverified")
        if by_instance is not None and not isinstance(by_instance, dict):
            _fail("runtime_identity_unverified")
        return by_pane, by_instance

    def detach(self, *, session: str, pane_id: str) -> None:
        try:
            closed = self._close_pane(session=session, pane_id=pane_id)
        except Exception:
            _fail("runtime_unavailable", pane_id=pane_id, unknown=True)
        if isinstance(closed, dict) and closed.get("available") is False:
            _fail("runtime_unavailable", pane_id=pane_id)
        try:
            snap = self._snapshot(session=session)
        except Exception:
            _fail("runtime_unavailable", pane_id=pane_id, unknown=True)
        panes = snap.get("panes") if isinstance(snap, dict) else None
        if not isinstance(panes, list):
            _fail("runtime_unavailable", pane_id=pane_id, unknown=True)
        if any(isinstance(pane, dict) and pane.get("pane_id") == pane_id for pane in panes):
            _fail("runtime_unavailable", pane_id=pane_id, unknown=True)

    def confirm_absent(
        self, *, session: str, pane_id: str, instance_id: str | None,
    ) -> bool:
        try:
            snap = self._snapshot(session=session)
        except Exception:
            return False
        panes = snap.get("panes") if isinstance(snap, dict) else None
        if not isinstance(panes, list):
            return False
        if any(isinstance(pane, dict) and pane.get("pane_id") == pane_id for pane in panes):
            return False
        try:
            by_pane = self._get_launch_descriptor(session=session, pane_id=pane_id)
        except Exception:
            return False
        if isinstance(by_pane, dict):
            return False
        if instance_id:
            try:
                by_instance = self._get_launch_descriptor_by_instance(
                    instance_id=instance_id,
                )
            except Exception:
                return False
            if isinstance(by_instance, dict) and by_instance.get("pane_id") not in {
                None, "",
            }:
                return False
        return True


def _descriptor_matches(
    pair: tuple[dict[str, Any] | None, dict[str, Any] | None], *,
    session: str, pane_id: str, instance_id: str, cwd: str,
) -> bool:
    by_pane, by_instance = pair
    if by_pane is None and by_instance is None:
        return False
    for descriptor in (by_pane, by_instance):
        if descriptor is None:
            continue
        workdir = descriptor.get("workdir") or descriptor.get("cwd")
        if not isinstance(workdir, str) or Path(workdir) != Path(cwd):
            return False
        if descriptor.get("session") not in (None, session):
            return False
        if descriptor.get("pane_id") not in (None, "", pane_id):
            return False
        if descriptor.get("instance_id") not in (None, instance_id):
            return False
        if descriptor.get("kind") not in (None, KIND):
            return False
    return True


def _default_wakeup_prompt(session: str, pane_id: str, text: str) -> dict[str, Any]:
    if text != WAKEUP_TEXT:
        _fail("invalid_argument")
    return herdr_client.pane_send(session, pane_id, text, mode="prompt")


def _assert_private_ancestry(path: Path) -> Path:
    try:
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            _fail("invalid_argument")
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                _fail("invalid_argument")
            if current != path and not stat.S_ISDIR(info.st_mode):
                _fail("invalid_argument")
        return path
    except HarnessError:
        raise
    except OSError:
        _fail("invalid_argument")
    raise AssertionError("unreachable")


def _private_dir(path: Path) -> None:
    path = _assert_private_ancestry(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        info = path.lstat()
    except HarnessError:
        raise
    except OSError:
        _fail("invalid_argument")
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        _fail("invalid_argument")


def _toml_basic(value: str) -> str:
    if value == "" or any(
        ord(char) < 32 or char in '"\\' for char in value
    ):
        _fail("invalid_argument")
    return f'"{value}"'


def _safe_public_path(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        _fail("invalid_argument")
    try:
        resolved = path.resolve()
    except OSError:
        _fail("invalid_argument")
    text = str(resolved)
    if text == "" or any(ord(char) < 32 or char in '"\\' for char in text):
        _fail("invalid_argument")
    return resolved


def _write_mcp_home_config(home: Path, cap: Path, capability_root: Path) -> None:
    data_root = _safe_public_path(Path(capability_root).parent)
    interpreter = _safe_public_path(Path(sys.executable))
    module_root = _safe_public_path(Path(__file__).resolve().parent.parent)
    cap = _safe_public_path(cap)
    config = (
        'sandbox_mode = "read-only"\n'
        "[mcp_servers.cockpit]\n"
        f"command = {_toml_basic(str(interpreter))}\n"
        'args = ["-P", "-m", "agent_cockpit.workspace_mcp_entry"]\n'
        "[mcp_servers.cockpit.env]\n"
        f"COCKPIT_CAPABILITY_FILE = {_toml_basic(str(cap))}\n"
        f"COCKPIT_DATA_DIR = {_toml_basic(str(data_root))}\n"
        f"PYTHONPATH = {_toml_basic(str(module_root))}\n"
        'PYTHONNOUSERSITE = "1"\n'
    )
    _write_private(home / "config.toml", config.encode())


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _fsync_dir(parent: Path) -> None:
    dirfd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def _read_private_regular(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            raise OSError("unsafe private file")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if chunk == b"":
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_exclusive_file(path: Path, payload: bytes, *, durable: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            wrote = os.write(fd, view[offset:])
            if wrote <= 0:
                raise OSError("short write")
            offset += wrote
        if durable:
            os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    staged = path.lstat()
    if (
        stat.S_ISLNK(staged.st_mode)
        or not stat.S_ISREG(staged.st_mode)
        or staged.st_nlink != 1
        or staged.st_uid != os.getuid()
        or stat.S_IMODE(staged.st_mode) != 0o600
    ):
        _fail("invalid_argument")
    if _read_private_regular(path) != payload:
        _fail("invalid_argument")


def _restore_published(
    path: Path,
    parent: Path,
    backup: Path | None,
    previous: bytes | None,
) -> bool:
    try:
        if previous is None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            _fsync_dir(parent)
            try:
                path.lstat()
            except FileNotFoundError:
                return True
            return False
        if backup is None:
            return False
        os.replace(backup, path)
        _fsync_dir(parent)
        return _read_private_regular(path) == previous
    except OSError:
        try:
            if previous is None:
                try:
                    path.lstat()
                except FileNotFoundError:
                    return True
                return False
            return _read_private_regular(path) == previous
        except (FileNotFoundError, OSError):
            return False


def _retire_published_target(path: Path, parent: Path) -> None:
    _unlink_quiet(path)
    try:
        _fsync_dir(parent)
    except OSError:
        return


def _write_private(path: Path, payload: bytes) -> None:
    _assert_private_ancestry(path)
    parent = path.parent
    tmp: Path | None = None
    backup: Path | None = None
    previous: bytes | None = None
    published = False
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            _fail("invalid_argument")
        parent_info = parent.lstat()
        if (
            stat.S_ISLNK(parent_info.st_mode)
            or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.getuid()
        ):
            _fail("invalid_argument")
        if info is not None:
            previous = _read_private_regular(path)
            backup = parent / f".{path.name}.{secrets.token_hex(8)}.bak"
            # Read-back verifies old bytes; skip os.fsync so call #2 stays the
            # post-replace directory fsync.
            _write_exclusive_file(backup, previous, durable=False)
        tmp = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        _write_exclusive_file(tmp, payload, durable=True)
        os.replace(tmp, path)
        published = True
        tmp = None
        _fsync_dir(parent)
        written = path.lstat()
        if (
            stat.S_ISLNK(written.st_mode)
            or not stat.S_ISREG(written.st_mode)
            or written.st_nlink != 1
            or written.st_uid != os.getuid()
            or stat.S_IMODE(written.st_mode) != 0o600
        ):
            _fail("invalid_argument")
        if backup is not None:
            _unlink_quiet(backup)
            backup = None
    except (HarnessError, OSError) as exc:
        if tmp is not None:
            _unlink_quiet(tmp)
            tmp = None
        if published:
            proven = _restore_published(path, parent, backup, previous)
            if backup is not None:
                _unlink_quiet(backup)
                backup = None
            if not proven:
                _retire_published_target(path, parent)
        elif backup is not None:
            _unlink_quiet(backup)
            backup = None
        if isinstance(exc, HarnessError):
            raise
        _fail("invalid_argument")


def _read_capability(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            _fail("invalid_argument")
        value = json.loads(path.read_bytes().decode("utf-8"))
    except HarnessError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("invalid_argument")
    if not isinstance(value, dict):
        _fail("invalid_argument")
    return value


def current_generation(capability_path: Path) -> int:
    record = _read_capability(capability_path)
    name = attachment_id_text(record.get("attachment_id"))
    stamp = capability_path.parent / f"{name}.generation"
    try:
        info = stamp.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            _fail("stale_generation")
        raw = stamp.read_bytes().decode("ascii").strip()
    except FileNotFoundError:
        _fail("stale_generation")
    except HarnessError:
        raise
    except OSError:
        _fail("invalid_argument")
    if not raw.isdigit() or int(raw) < 1:
        _fail("stale_generation")
    return int(raw)
