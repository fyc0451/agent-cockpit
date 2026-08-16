from __future__ import annotations

from pathlib import Path

import pytest

from agent_cockpit import local_codex_harness as harness_mod
from agent_cockpit import workspace_write_gate as gate_mod


ATTACHMENT = "att_" + "c" * 32
IDENTITY = "idn_" + "d" * 32
FENCE = "sha256:" + "ab" * 32


def _issue(tmp_path: Path, *, generation: int = 1) -> dict[str, object]:
    harness = harness_mod.LocalCodexHarness(capability_root=tmp_path / "caps")
    return harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=generation,
        fence=FENCE, session="s", pane_id="pane-1",
    )


def test_matching_capability_still_refuses_active_writer(tmp_path: Path) -> None:
    issued = _issue(tmp_path)
    target = tmp_path / "checkout" / "secret.txt"
    target.parent.mkdir()
    gate = gate_mod.WorkspaceWriteGate()
    with pytest.raises(gate_mod.WriteGateError) as error:
        gate.authorize(
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
            fence=FENCE, capability_path=issued["capability_path"],
            checkout_path=target.parent,
        )
    assert error.value.code == "lease_not_active"
    assert not target.exists()


def test_old_generation_and_fence_are_rejected(tmp_path: Path) -> None:
    issued = _issue(tmp_path, generation=2)
    gate = gate_mod.WorkspaceWriteGate()
    with pytest.raises(gate_mod.WriteGateError) as generation:
        gate.authorize(
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
            fence=FENCE, capability_path=issued["capability_path"],
            checkout_path=tmp_path,
        )
    assert generation.value.code == "stale_generation"
    with pytest.raises(gate_mod.WriteGateError) as fence:
        gate.authorize(
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=2,
            fence="sha256:" + "00" * 32,
            capability_path=issued["capability_path"],
            checkout_path=tmp_path,
        )
    assert fence.value.code == "fence_rejected"
    missing = tmp_path / "missing.cap"
    with pytest.raises(gate_mod.WriteGateError) as cap:
        gate.authorize(
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=2,
            fence=FENCE, capability_path=missing, checkout_path=tmp_path,
        )
    assert cap.value.code == "runtime_capability_invalid"
