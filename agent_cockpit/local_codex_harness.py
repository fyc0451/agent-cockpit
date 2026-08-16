"""Read-only Codex/Herdr attach for Checkpoint B. No prompt or pane I/O."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from . import herdr_client


PROVIDER = "local_herdr"
HARNESS = "codex_terminal_managed_v1"
KIND = "codex"
SANDBOX = "read-only"
LAUNCH_ARGS = ("--sandbox", "read-only")


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
        self, *, session: str, checkout_path: Path, instance_id: str,
        display_name: str,
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
        get_launch_descriptor: Callable[..., dict[str, Any] | None] | None = None,
        get_launch_descriptor_by_instance: (
            Callable[..., dict[str, Any] | None] | None
        ) = None,
        snapshot: Callable[..., dict[str, Any]] | None = None,
        close_pane: Callable[..., dict[str, Any]] | None = None,
        new_instance_id: Callable[[], str] | None = None,
    ) -> None:
        self._ensure_session = ensure_session or herdr_client.ensure_session
        self._start_agent = start_agent or herdr_client.start_agent
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

    def attach_readonly(
        self, *, session: str, checkout_path: Path, instance_id: str | None = None,
        display_name: str = "codex",
        project_id: str | None = None, workspace_id: str | None = None,
    ) -> AttachmentEvidence:
        spec = self.build_launch_spec(checkout_path)
        if instance_id is None:
            instance_id = self._new_instance_id()
        if project_id is not None or workspace_id is not None:
            _fail("invalid_argument")
        try:
            self._ensure_session(session=session)
        except Exception:
            _fail("runtime_unavailable")
        try:
            started = self._start_agent(
                session=session,
                workdir=spec.cwd,
                agent=KIND,
                args=spec.argv_text(),
                instance_id=instance_id,
                label=display_name,
                project_id=None,
                workspace_id=None,
            )
        except Exception:
            _fail("runtime_unavailable")
        if not isinstance(started, dict) or started.get("available") is False:
            _fail("runtime_unavailable")
        if started.get("error") or started.get("error_code"):
            _fail("runtime_unavailable")
        pane_id = started.get("pane_id")
        live_id = started.get("instance_id") or instance_id
        if not isinstance(pane_id, str) or not pane_id:
            _fail("runtime_unavailable")
        try:
            return self.observe(
                session=session, instance_id=str(live_id), pane_id=pane_id,
                checkout_path=checkout_path,
            )
        except HarnessError as exc:
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
