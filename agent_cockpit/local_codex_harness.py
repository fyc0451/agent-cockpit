"""Read-only Codex/Herdr attach for Checkpoint B. No prompt or pane I/O."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


PROVIDER = "local_herdr"
HARNESS = "codex_terminal_managed_v1"
KIND = "codex"
SANDBOX = "read-only"
LAUNCH_ARGS = ("--sandbox", "read-only")


class HarnessError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise HarnessError(code)


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
        ensure_session: Callable[[str], Any],
        start_agent: Callable[..., dict[str, Any]],
        get_descriptor: Callable[..., dict[str, Any] | None],
        snapshot: Callable[[str], dict[str, Any]],
        close_pane: Callable[[str, str], dict[str, Any]],
        new_instance_id: Callable[[], str],
    ) -> None:
        self._ensure_session = ensure_session
        self._start_agent = start_agent
        self._get_descriptor = get_descriptor
        self._snapshot = snapshot
        self._close_pane = close_pane
        self._new_instance_id = new_instance_id
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
    ) -> AttachmentEvidence:
        spec = self.build_launch_spec(checkout_path)
        if instance_id is None:
            instance_id = self._new_instance_id()
        try:
            self._ensure_session(session)
        except Exception:
            _fail("runtime_unavailable")
        try:
            started = self._start_agent(
                session, spec.cwd, KIND, spec.argv_text(), instance_id,
                display_name,
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
        return self.observe(
            session=session, instance_id=str(live_id), pane_id=pane_id,
            checkout_path=checkout_path,
        )

    def observe(
        self, *, session: str, instance_id: str, pane_id: str, checkout_path: Path,
    ) -> AttachmentEvidence:
        spec = self.build_launch_spec(checkout_path)
        try:
            snap = self._snapshot(session)
        except Exception:
            _fail("runtime_unavailable")
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
        try:
            descriptor = self._get_descriptor(instance_id)
        except Exception:
            descriptor = None
        if isinstance(descriptor, dict):
            workdir = descriptor.get("workdir") or descriptor.get("cwd")
            if workdir is not None and Path(str(workdir)) != Path(spec.cwd):
                _fail("runtime_identity_unverified")
        return AttachmentEvidence(
            session, instance_id, pane_id, spec.cwd, PROVIDER, HARNESS, False,
        )

    def detach(self, *, session: str, pane_id: str) -> None:
        try:
            closed = self._close_pane(session, pane_id)
        except Exception:
            _fail("runtime_unavailable")
        if isinstance(closed, dict) and closed.get("available") is False:
            _fail("runtime_unavailable")
        try:
            snap = self._snapshot(session)
        except Exception:
            _fail("runtime_unavailable")
        panes = snap.get("panes") if isinstance(snap, dict) else None
        if not isinstance(panes, list):
            _fail("runtime_unavailable")
        if any(isinstance(pane, dict) and pane.get("pane_id") == pane_id for pane in panes):
            _fail("process_exited")
