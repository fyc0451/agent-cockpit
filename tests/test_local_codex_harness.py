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
        panes["pane-1"] = bound.arguments["workdir"]
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
            "panes": [
                {"pane_id": pane, "cwd": cwd} for pane, cwd in panes.items()
            ],
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


class _CountingHerdr:
    def __init__(self, checkout: Path, *, descriptors: bool = True, close: str = "ok") -> None:
        self.checkout = checkout
        self.descriptors = descriptors
        self.close = close
        self.panes: dict[str, str] = {}
        self.started = 0
        self.closed = 0
        self.seq = 0

    def start_agent(
        self, session: str, workdir: str, agent: str = "codex",
        model: str | None = None, layout: str = "tab", label: str | None = None,
        args: str = "", instance_id: str | None = None,
        project_id: str | None = None, workspace_id: str | None = None,
    ) -> dict[str, object]:
        self.seq += 1
        pane_id = f"pane-{self.seq}"
        self.started += 1
        self.panes[pane_id] = workdir
        return {
            "available": True, "pane_id": pane_id, "instance_id": instance_id,
            "cwd": workdir,
        }

    def get_launch_descriptor(self, session: str, pane_id: str) -> dict[str, object] | None:
        if not self.descriptors:
            return None
        return {
            "session": session, "pane_id": pane_id, "instance_id": INSTANCE,
            "workdir": str(self.checkout), "kind": "codex",
        }

    def get_launch_descriptor_by_instance(
        self, instance_id: str, *, include_retired: bool = False,
    ) -> dict[str, object] | None:
        if not self.descriptors:
            return None
        return {
            "session": "s", "pane_id": next(iter(self.panes), "pane-1"),
            "instance_id": instance_id, "workdir": str(self.checkout),
            "kind": "codex",
        }

    def session_snapshot(self, session: str) -> dict[str, object]:
        return {
            "panes": [
                {"pane_id": pane, "cwd": cwd} for pane, cwd in self.panes.items()
            ],
        }

    def close_pane(self, session: str, pane_id: str) -> dict[str, object]:
        if self.close == "raise":
            raise RuntimeError("close lost")
        if self.close == "fail":
            return {"available": False}
        self.closed += 1
        self.panes.pop(pane_id, None)
        return {"available": True, "closed": pane_id}

    def ensure_session(self, session: str) -> dict[str, object]:
        return {"ok": True}


def test_known_identity_failure_closes_live_pane(tmp_path: Path) -> None:
    checkout = tmp_path / "chk"
    checkout.mkdir()
    herdr = _CountingHerdr(checkout, descriptors=False, close="ok")
    harness = harness_mod.LocalCodexHarness(
        ensure_session=herdr.ensure_session,
        start_agent=herdr.start_agent,
        get_launch_descriptor=herdr.get_launch_descriptor,
        get_launch_descriptor_by_instance=herdr.get_launch_descriptor_by_instance,
        snapshot=herdr.session_snapshot,
        close_pane=herdr.close_pane,
        new_instance_id=lambda: INSTANCE,
    )
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(session="s", checkout_path=checkout)
    assert error.value.code == "runtime_identity_unverified"
    assert herdr.started == 1
    assert herdr.closed == 1
    assert herdr.panes == {}
    assert getattr(error.value, "pane_id", None) in {None, ""}


def test_known_identity_failure_unknown_close_does_not_pretend(tmp_path: Path) -> None:
    checkout = tmp_path / "chk"
    checkout.mkdir()
    herdr = _CountingHerdr(checkout, descriptors=False, close="raise")
    harness = harness_mod.LocalCodexHarness(
        ensure_session=herdr.ensure_session,
        start_agent=herdr.start_agent,
        get_launch_descriptor=herdr.get_launch_descriptor,
        get_launch_descriptor_by_instance=herdr.get_launch_descriptor_by_instance,
        snapshot=herdr.session_snapshot,
        close_pane=herdr.close_pane,
        new_instance_id=lambda: INSTANCE,
    )
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(session="s", checkout_path=checkout)
    assert error.value.code == "runtime_unavailable"
    assert herdr.started == 1
    assert herdr.closed == 0
    assert list(herdr.panes) == ["pane-1"]
    assert getattr(error.value, "pane_id", None) == "pane-1"


def test_detach_close_transport_loss_is_unknown(tmp_path: Path) -> None:
    checkout = tmp_path / "chk"
    checkout.mkdir()
    herdr = _CountingHerdr(checkout, descriptors=True, close="raise")
    herdr.panes["pane-1"] = str(checkout)
    harness = harness_mod.LocalCodexHarness(
        ensure_session=herdr.ensure_session,
        start_agent=herdr.start_agent,
        get_launch_descriptor=herdr.get_launch_descriptor,
        get_launch_descriptor_by_instance=herdr.get_launch_descriptor_by_instance,
        snapshot=herdr.session_snapshot,
        close_pane=herdr.close_pane,
        new_instance_id=lambda: INSTANCE,
    )
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.detach(session="s", pane_id="pane-1")
    assert error.value.code == "runtime_unavailable"
    assert getattr(error.value, "unknown", False) is True
    assert herdr.panes == {"pane-1": str(checkout)}
