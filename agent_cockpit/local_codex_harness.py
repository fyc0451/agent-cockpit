"""Read-only Codex/Herdr attach plus private capability/wakeup seam."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from . import herdr_client


PROVIDER = "local_herdr"
HARNESS = "codex_terminal_managed_v1"
KIND = "codex"
SANDBOX = "read-only"
LAUNCH_ARGS = ("--sandbox", "read-only")
WAKEUP_TEXT = (
    "COCKPIT_WAKEUP_V1\n"
    "Start the dispatched-work workflow now. Call claim_current with {} first. "
    "After it returns, read root_message.body and do that work in the managed "
    "checkout. Call apply_patch with claim_revision and lease_revision from "
    "claim_current, plus patch as a unified diff. Then call reply_complete with "
    "the same claim_revision, lease_revision returned by apply_patch, and body "
    "as a concise completion summary."
)
WAKEUP_DIGEST = "sha256:" + hashlib.sha256(WAKEUP_TEXT.encode()).hexdigest()
_ATTACHMENT_ID = re.compile(r"att_[0-9a-f]{32}\Z")
_PROVIDER_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_PROVENANCE_FILE = "relay.config.toml"
_MAX_PROVENANCE_BYTES = 64 * 1024


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


@dataclass(frozen=True)
class ProviderAuthReference:
    model_provider: str
    name: str
    base_url: str
    wire_api: str
    command: str
    args: tuple[str, ...]
    timeout_ms: int
    refresh_interval_ms: int | None
    authority: Path


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
        provenance = _provider_auth_provenance()
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
        self._reject_unusable_existing_authority(attachment_id)
        try:
            _write_private(cap, json.dumps(payload, separators=(",", ":")).encode())
            _write_private(current, str(generation).encode() + b"\n")
            _write_private(root / f"{attachment_id}.fence", fence.encode() + b"\n")
            _write_mcp_home_config(home, cap, root, provenance)
            _validate_auth_reference(provenance.authority)
        except HarnessError as exc:
            if exc.unknown:
                self._retire_private_runtime(attachment_id)
            raise
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
        if not isinstance(sent, dict) or sent.get("available") is False:
            _fail("runtime_unavailable")
        if sent.get("error") or sent.get("executing") is False:
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
        provenance = _provider_auth_provenance()
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
        self._reject_unusable_existing_authority(attachment_id)
        try:
            _write_private(cap, json.dumps(payload, separators=(",", ":")).encode())
            _write_private(root / f"{attachment_id}.generation", str(generation).encode() + b"\n")
            _write_private(root / f"{attachment_id}.fence", fence.encode() + b"\n")
            _write_mcp_home_config(home, cap, root, provenance)
            _validate_auth_reference(provenance.authority)
        except HarnessError as exc:
            if exc.unknown:
                self._retire_private_runtime(attachment_id)
            raise
        return {
            "capability_path": cap, "codex_home": home, "generation": generation,
            "_auth_reference": provenance.authority,
        }

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
        try:
            _write_private(
                self._require_cap(attachment_id),
                json.dumps(record, separators=(",", ":")).encode(),
            )
        except HarnessError as exc:
            if exc.unknown:
                self._retire_private_runtime(attachment_id)
            raise

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

    def _reject_unusable_existing_authority(self, attachment_id: str) -> None:
        root = self._capability_root
        if root is None:
            return
        cap = root / f"{attachment_id}.cap"
        try:
            info = cap.lstat()
        except FileNotFoundError:
            return
        except OSError:
            _fail("invalid_argument", unknown=True)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            _fail("invalid_argument")
        try:
            self._require_capability_record(attachment_id)
        except HarnessError:
            self._retire_private_runtime(attachment_id)
            try:
                cap.lstat()
            except FileNotFoundError:
                return
            _fail("invalid_argument", unknown=True)

    def _retire_private_runtime(self, attachment_id: str) -> None:
        root = self._capability_root
        if root is None:
            return
        names = (
            f"{attachment_id}.generation", f"{attachment_id}.fence",
            f"{attachment_id}.cap",
        )
        for name in names:
            path = root / name
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError:
                continue
        config = root / f"{attachment_id}.home" / "config.toml"
        try:
            os.unlink(config)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        for name in names:
            _invalidate_private_mode(root / name)

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
        try:
            issued = self._prepare_private_runtime(
                attachment_id=attachment_id, identity_id=identity_id,
                generation=generation, fence=fence, session=session,
                checkout=spec.cwd,
            )
        except HarnessError as exc:
            if exc.unknown:
                self._retire_private_runtime(attachment_id)
                raise HarnessError("runtime_unavailable", unknown=True) from None
            raise
        pane_id: str | None = None
        live_id = instance_id
        try:
            _validate_auth_reference(Path(issued["_auth_reference"]))
        except HarnessError:
            self._retire_private_runtime(attachment_id)
            raise
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
    return herdr_client.submit_agent_prompt_until_working(session, pane_id, text)


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


def _write_mcp_home_config(
    home: Path, cap: Path, capability_root: Path,
    provenance: ProviderAuthReference,
) -> None:
    data_root = _safe_public_path(Path(capability_root).parent)
    interpreter = _safe_public_path(Path(sys.executable))
    module_root = _safe_public_path(Path(__file__).resolve().parent.parent)
    cap = _safe_public_path(cap)
    auth_args = json.dumps(
        list(provenance.args), ensure_ascii=True, separators=(",", ":"),
    )
    refresh = (
        ""
        if provenance.refresh_interval_ms is None
        else f"refresh_interval_ms = {provenance.refresh_interval_ms}\n"
    )
    config = (
        f"model_provider = {_toml_basic(provenance.model_provider)}\n"
        'network_access = "enabled"\n'
        'sandbox_mode = "read-only"\n'
        f"[model_providers.{provenance.model_provider}]\n"
        f"name = {_toml_basic(provenance.name)}\n"
        f"base_url = {_toml_basic(provenance.base_url)}\n"
        f"wire_api = {_toml_basic(provenance.wire_api)}\n"
        f"[model_providers.{provenance.model_provider}.auth]\n"
        f"command = {_toml_basic(provenance.command)}\n"
        f"args = {auth_args}\n"
        f"timeout_ms = {provenance.timeout_ms}\n"
        f"{refresh}"
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


def _verified_metadata(path: Path) -> bytes:
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > _MAX_PROVENANCE_BYTES
        ):
            _fail("runtime_unavailable")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if (
                (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_size > _MAX_PROVENANCE_BYTES
            ):
                _fail("runtime_unavailable")
            chunks: list[bytes] = []
            remaining = opened.st_size
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) != opened.st_size:
                _fail("runtime_unavailable")
            return payload
        finally:
            os.close(fd)
    except HarnessError:
        raise
    except OSError:
        _fail("runtime_unavailable")
    raise AssertionError("unreachable")


def _safe_reference_ancestry(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        _fail("runtime_unavailable")
    current_uid = os.getuid()
    current_gid = os.getgid()
    current = Path(path.anchor)
    directories = list(path.parents)
    directories.reverse()
    for current in directories:
        try:
            info = current.lstat()
        except OSError:
            _fail("runtime_unavailable")
        mode = stat.S_IMODE(info.st_mode)
        sticky_root = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
        owned_group_only = (
            not mode & 0o002
            and info.st_uid == current_uid
            and info.st_gid == current_gid
        )
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, current_uid}
            or (mode & 0o022 and not sticky_root and not owned_group_only)
        ):
            _fail("runtime_unavailable")
    try:
        if path.parent.lstat().st_uid != current_uid:
            _fail("runtime_unavailable")
    except OSError:
        _fail("runtime_unavailable")


def _validate_auth_reference(path: Path) -> None:
    _safe_reference_ancestry(path)
    try:
        before = path.lstat()
        flags = getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError:
        _fail("runtime_unavailable")
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
    ):
        _fail("runtime_unavailable")


def _positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        _fail("runtime_unavailable")
    return value


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        _fail("runtime_unavailable")
    return value


def _provider_auth_provenance() -> ProviderAuthReference:
    raw_home = os.environ.get("CODEX_HOME")
    if not isinstance(raw_home, str) or raw_home == "":
        _fail("runtime_unavailable")
    home = Path(raw_home)
    if not home.is_absolute() or ".." in home.parts:
        _fail("runtime_unavailable")
    metadata = home / _PROVENANCE_FILE
    _safe_reference_ancestry(metadata)
    try:
        home_info = home.lstat()
    except OSError:
        _fail("runtime_unavailable")
    if (
        stat.S_ISLNK(home_info.st_mode)
        or not stat.S_ISDIR(home_info.st_mode)
        or home_info.st_uid != os.getuid()
    ):
        _fail("runtime_unavailable")
    try:
        value = tomllib.loads(
            _verified_metadata(metadata).decode("utf-8"),
        )
    except HarnessError:
        raise
    except (UnicodeError, tomllib.TOMLDecodeError):
        _fail("runtime_unavailable")
    name = value.get("model_provider")
    providers = value.get("model_providers")
    if (
        not isinstance(name, str)
        or _PROVIDER_NAME.fullmatch(name) is None
        or not isinstance(providers, dict)
        or not isinstance(providers.get(name), dict)
    ):
        _fail("runtime_unavailable")
    provider = providers[name]
    if not {"name", "base_url", "wire_api", "auth"} <= set(provider):
        _fail("runtime_unavailable")
    auth = provider["auth"]
    if not isinstance(auth, dict) or not {"command", "args", "timeout_ms"} <= set(auth):
        _fail("runtime_unavailable")
    provider_name = provider["name"]
    base_url = provider["base_url"]
    wire_api = provider["wire_api"]
    command = auth["command"]
    args = auth["args"]
    if (
        not all(isinstance(item, str) and item for item in (
            provider_name, base_url, wire_api, command,
        ))
        or not base_url.startswith("https://")
        or wire_api != "responses"
        or command != "/usr/bin/jq"
        or not isinstance(args, list)
        or len(args) != 3
        or args[:2] != ["-r", ".OPENAI_API_KEY"]
        or not isinstance(args[2], str)
    ):
        _fail("runtime_unavailable")
    authority = Path(args[2])
    _validate_auth_reference(authority)
    refresh = auth.get("refresh_interval_ms")
    if refresh is not None:
        refresh = _nonnegative_int(refresh)
    return ProviderAuthReference(
        model_provider=name,
        name=provider_name,
        base_url=base_url,
        wire_api=wire_api,
        command=command,
        args=tuple(args),
        timeout_ms=_positive_int(auth["timeout_ms"]),
        refresh_interval_ms=refresh,
        authority=authority,
    )


def _unlink_owned(path: Path) -> None:
    try:
        os.unlink(path)
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


def _usable_private_regular(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        not stat.S_ISLNK(info.st_mode)
        and stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
    )


def _invalidate_private_mode(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return
    if stat.S_IMODE(info.st_mode) != 0o600:
        return
    try:
        os.chmod(path, 0o000)
    except OSError:
        return


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


def _end_state_is_previous(path: Path, previous: bytes | None) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return previous is None
    if previous is None:
        return False
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
    ):
        return False
    try:
        return _read_private_regular(path) == previous
    except OSError:
        return False


def _write_exclusive_file(path: Path, payload: bytes, *, durable: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    created = False
    fd = -1
    try:
        fd = os.open(path, flags, 0o600)
        created = True
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            wrote = os.write(fd, view[offset:])
            if wrote <= 0:
                raise OSError("short write")
            offset += wrote
        if durable:
            os.fsync(fd)
    except (HarnessError, OSError):
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = -1
        if created:
            _unlink_owned(path)
        raise
    try:
        os.close(fd)
    except OSError:
        _unlink_owned(path)
        raise
    fd = -1
    try:
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
    except (HarnessError, OSError):
        _unlink_owned(path)
        raise


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
            return _end_state_is_previous(path, None)
        if backup is None:
            return False
        os.replace(backup, path)
        _fsync_dir(parent)
        return _end_state_is_previous(path, previous)
    except OSError:
        return _end_state_is_previous(path, previous)


def _retire_published_target(path: Path, parent: Path) -> bool:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        _invalidate_private_mode(path)
        return False
    try:
        _fsync_dir(parent)
    except OSError:
        return _end_state_is_previous(path, None)
    return _end_state_is_previous(path, None)


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
            staged_backup = parent / f".{path.name}.{secrets.token_hex(8)}.bak"
            # Read-back verifies old bytes; skip os.fsync so call #2 stays the
            # post-replace directory fsync.
            _write_exclusive_file(staged_backup, previous, durable=False)
            backup = staged_backup
        staged_tmp = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        _write_exclusive_file(staged_tmp, payload, durable=True)
        tmp = staged_tmp
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
            _unlink_owned(backup)
            backup = None
    except (HarnessError, OSError) as exc:
        if tmp is not None:
            _unlink_owned(tmp)
            tmp = None
        if published:
            proven = _restore_published(path, parent, backup, previous)
            if backup is not None:
                _unlink_owned(backup)
                backup = None
            if not proven:
                _retire_published_target(path, parent)
            if not _end_state_is_previous(path, previous):
                _invalidate_private_mode(path)
                _fail("invalid_argument", unknown=True)
        elif backup is not None:
            _unlink_owned(backup)
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
