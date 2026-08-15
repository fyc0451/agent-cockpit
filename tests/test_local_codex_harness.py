from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from agent_cockpit import herdr_client
from agent_cockpit import local_codex_harness as harness_mod

INSTANCE = "i-abcdefghijklmnopqrstuvwxyz"


def test_reference_defaults_bind_real_herdr_signatures() -> None:
    harness = harness_mod.LocalCodexHarness()
    assert harness._start_agent is herdr_client.start_agent
    assert harness._get_launch_descriptor is herdr_client.get_launch_descriptor
    assert (
        harness._get_launch_descriptor_by_instance
        is herdr_client.get_launch_descriptor_by_instance
    )
    assert harness._ensure_session is herdr_client.ensure_session
    assert harness._snapshot is herdr_client.session_snapshot
    assert harness._close_pane is herdr_client.close_pane
    start = inspect.signature(herdr_client.start_agent)
    assert list(start.parameters) == [
        "session", "workdir", "agent", "model", "layout", "label", "args",
        "instance_id", "project_id", "workspace_id",
    ]
    by_pane = inspect.signature(herdr_client.get_launch_descriptor)
    assert list(by_pane.parameters) == ["session", "pane_id"]
    by_instance = inspect.signature(herdr_client.get_launch_descriptor_by_instance)
    assert list(by_instance.parameters) == ["instance_id", "include_retired"]


def test_start_agent_uses_real_keywords_and_requires_verified_descriptor(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "chk"
    checkout.mkdir()
    calls: list[tuple[str, dict[str, object]]] = []
    panes = {"pane-1": str(checkout)}
    start_sig = inspect.signature(herdr_client.start_agent)
    desc_sig = inspect.signature(herdr_client.get_launch_descriptor)
    inst_sig = inspect.signature(herdr_client.get_launch_descriptor_by_instance)

    def start_agent(*args, **kwargs):
        bound = start_sig.bind(*args, **kwargs)
        bound.apply_defaults()
        calls.append(("start", dict(bound.arguments)))
        assert bound.arguments["model"] is None
        assert bound.arguments["layout"] == "tab"
        assert bound.arguments["args"] == "--sandbox read-only"
        assert bound.arguments["agent"] == "codex"
        assert Path(bound.arguments["workdir"]) == checkout
        assert bound.arguments["project_id"] is None
        assert bound.arguments["workspace_id"] is None
        return {
            "available": True, "pane_id": "pane-1",
            "instance_id": bound.arguments["instance_id"],
            "cwd": bound.arguments["workdir"],
        }

    def get_launch_descriptor(*args, **kwargs):
        bound = desc_sig.bind(*args, **kwargs)
        calls.append(("desc_pane", dict(bound.arguments)))
        return {
            "session": bound.arguments["session"],
            "pane_id": bound.arguments["pane_id"],
            "instance_id": INSTANCE,
            "workdir": str(checkout),
            "kind": "codex",
        }

    def get_launch_descriptor_by_instance(*args, **kwargs):
        bound = inst_sig.bind(*args, **kwargs)
        calls.append(("desc_instance", dict(bound.arguments)))
        return {
            "session": "s",
            "pane_id": "pane-1",
            "instance_id": bound.arguments["instance_id"],
            "workdir": str(checkout),
            "kind": "codex",
        }

    def snapshot(*, session: str):
        calls.append(("snapshot", {"session": session}))
        return {
            "panes": [
                {"pane_id": pane, "cwd": cwd} for pane, cwd in panes.items()
            ],
        }

    def close_pane(*, session: str, pane_id: str):
        calls.append(("close", {"session": session, "pane_id": pane_id}))
        panes.pop(pane_id, None)
        return {"available": True, "closed": pane_id}

    harness = harness_mod.LocalCodexHarness(
        ensure_session=lambda *, session: calls.append(("ensure", {"session": session})),
        start_agent=start_agent,
        get_launch_descriptor=get_launch_descriptor,
        get_launch_descriptor_by_instance=get_launch_descriptor_by_instance,
        snapshot=snapshot,
        close_pane=close_pane,
        new_instance_id=lambda: INSTANCE,
    )
    attached = harness.attach_readonly(session="s", checkout_path=checkout)
    assert attached.identity_verified is True
    assert attached.cwd == str(checkout)
    start_call = next(item for item in calls if item[0] == "start")[1]
    assert start_call["args"] == "--sandbox read-only"
    assert start_call["model"] is None
    assert next(item for item in calls if item[0] == "desc_pane")[1] == {
        "session": "s", "pane_id": "pane-1",
    }
    assert next(item for item in calls if item[0] == "desc_instance")[1][
        "instance_id"
    ] == INSTANCE
    harness.detach(session="s", pane_id=attached.pane_id)
    assert "pane-1" not in panes
    assert all(name != "pane_send" for name, _payload in calls)

    missing = harness_mod.LocalCodexHarness(
        ensure_session=lambda *, session: None,
        start_agent=start_agent,
        get_launch_descriptor=lambda *, session, pane_id: None,
        get_launch_descriptor_by_instance=lambda instance_id, **_kw: None,
        snapshot=lambda *, session: {
            "panes": [{"pane_id": "pane-1", "cwd": str(checkout)}],
        },
        close_pane=close_pane,
        new_instance_id=lambda: INSTANCE,
    )
    with pytest.raises(harness_mod.HarnessError) as unverified:
        missing.attach_readonly(session="s", checkout_path=checkout)
    assert unverified.value.code == "runtime_identity_unverified"

    with pytest.raises(harness_mod.HarnessError) as bad:
        harness_mod.LaunchSpec(
            "codex", "workspace-write", str(checkout), ("--sandbox", "workspace-write"),
            writable=True,
        ).assert_readonly()
    assert bad.value.code == "invalid_argument"
