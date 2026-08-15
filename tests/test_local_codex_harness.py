from __future__ import annotations

from pathlib import Path

import pytest

from agent_cockpit import local_codex_harness as harness_mod


def test_launch_spec_is_readonly_and_adapter_never_sends(tmp_path: Path) -> None:
    checkout = tmp_path / "chk"
    checkout.mkdir()
    calls: list[str] = []
    panes = {"pane-1": str(checkout)}

    def start(session, cwd, kind, args, instance_id, display_name):
        calls.append("start")
        assert kind == "codex"
        assert args == "--sandbox read-only"
        assert Path(cwd) == checkout
        return {
            "available": True, "pane_id": "pane-1", "instance_id": instance_id,
            "cwd": cwd,
        }

    def snapshot(session):
        calls.append("snapshot")
        return {
            "panes": [
                {"pane_id": pane, "cwd": cwd} for pane, cwd in panes.items()
            ],
        }

    def close(session, pane_id):
        calls.append("close")
        panes.pop(pane_id, None)
        return {"available": True, "closed": pane_id}

    harness = harness_mod.LocalCodexHarness(
        ensure_session=lambda session: calls.append("ensure") or {"ok": True},
        start_agent=start,
        get_descriptor=lambda instance_id: {
            "workdir": str(checkout), "kind": "codex",
        },
        snapshot=snapshot,
        close_pane=close,
        new_instance_id=lambda: "i-abcdefghijklmnopqrstuvwxyz",
    )
    spec = harness.build_launch_spec(checkout)
    spec.assert_readonly()
    assert spec.writable is False
    assert spec.sandbox == "read-only"
    attached = harness.attach_readonly(session="s", checkout_path=checkout)
    assert attached.identity_verified is False
    assert attached.cwd == str(checkout)
    assert attached.provider == "local_herdr"
    harness.detach(session="s", pane_id=attached.pane_id)
    assert "pane-1" not in panes
    assert "pane_send" not in calls
    assert "pane_read" not in calls
    assert "prompt" not in calls
    assert not hasattr(harness, "pane_send")
    assert not hasattr(harness, "pane_read")

    with pytest.raises(harness_mod.HarnessError) as bad:
        harness_mod.LaunchSpec(
            "codex", "workspace-write", str(checkout), ("--sandbox", "workspace-write"),
            writable=True,
        ).assert_readonly()
    assert bad.value.code == "invalid_argument"
