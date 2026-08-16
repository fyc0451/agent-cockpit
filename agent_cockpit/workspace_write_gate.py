"""Synchronous write-boundary stub. C2 never becomes an active writer."""
from __future__ import annotations

from pathlib import Path

from . import local_codex_harness as harness_mod


class WriteGateError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise WriteGateError(code)


class WorkspaceWriteGate:
    def authorize(
        self, *, attachment_id: str, identity_id: str, generation: int,
        fence: str, capability_path: Path, checkout_path: Path,
    ) -> None:
        path = Path(capability_path)
        if type(generation) is not int or generation < 1:
            _fail("invalid_argument")
        try:
            attachment_id = harness_mod.attachment_id_text(attachment_id)
        except harness_mod.HarnessError:
            _fail("invalid_argument")
        try:
            record = harness_mod._read_capability(path)
        except harness_mod.HarnessError:
            _fail("runtime_capability_invalid")
        if (
            record.get("attachment_id") != attachment_id
            or record.get("identity_id") != identity_id
        ):
            _fail("runtime_capability_invalid")
        stored = record.get("generation")
        if type(stored) is not int or stored != generation:
            _fail("stale_generation")
        try:
            current = harness_mod.current_generation(path)
        except harness_mod.HarnessError as exc:
            if exc.code == "stale_generation":
                _fail("stale_generation")
            _fail("runtime_capability_invalid")
        if current != generation:
            _fail("stale_generation")
        if record.get("fence") != fence:
            _fail("fence_rejected")
        if not Path(checkout_path).is_dir():
            _fail("invalid_argument")
        _fail("lease_not_active")
