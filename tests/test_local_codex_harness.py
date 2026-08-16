from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from agent_cockpit import herdr_client
from agent_cockpit import local_codex_harness as harness_mod

INSTANCE = "i-abcdefghijklmnopqrstuvwxyz"
PROJECT = "prj_" + "a" * 32
WORKSPACE = "ws_" + "b" * 32
ATTACHMENT = "att_" + "c" * 32
IDENTITY = "idn_" + "d" * 32
SENTINEL = "BOSS-BODY-SENTINEL-9f3c"
FENCE = "sha256:" + "ab" * 32


def test_reference_defaults_bind_real_herdr_signatures() -> None:
    harness = harness_mod.LocalCodexHarness()
    assert harness._start_agent is herdr_client.start_agent
    assert harness._start_workspace_codex_home is herdr_client.start_workspace_codex_home
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
    home_start = inspect.signature(herdr_client.start_workspace_codex_home)
    assert list(home_start.parameters) == [
        "session", "workdir", "instance_id", "project_id", "workspace_id",
        "codex_home", "label", "display_name",
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
    start_sig = inspect.signature(herdr_client.start_workspace_codex_home)
    desc_sig = inspect.signature(herdr_client.get_launch_descriptor)
    inst_sig = inspect.signature(herdr_client.get_launch_descriptor_by_instance)

    def start_workspace_codex_home(*args, **kwargs):
        bound = start_sig.bind(*args, **kwargs)
        bound.apply_defaults()
        calls.append(("start", dict(bound.arguments)))
        assert "args" not in bound.arguments
        assert Path(bound.arguments["workdir"]) == checkout
        assert bound.arguments["project_id"] == PROJECT
        assert bound.arguments["workspace_id"] == WORKSPACE
        assert Path(bound.arguments["codex_home"]).is_dir()
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
        capability_root=tmp_path / "caps",
        ensure_session=lambda *, session: calls.append(("ensure", {"session": session})),
        start_workspace_codex_home=start_workspace_codex_home,
        get_launch_descriptor=get_launch_descriptor,
        get_launch_descriptor_by_instance=get_launch_descriptor_by_instance,
        snapshot=snapshot,
        close_pane=close_pane,
        new_instance_id=lambda: INSTANCE,
    )
    attached = harness.attach_readonly(
        session="s", checkout_path=checkout,
        project_id=PROJECT, workspace_id=WORKSPACE,
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence="sha256:" + "ab" * 32,
    )
    assert attached.identity_verified is True
    assert attached.cwd == str(checkout)
    start_call = next(item for item in calls if item[0] == "start")[1]
    assert "args" not in start_call
    assert start_call["project_id"] == PROJECT
    assert start_call["workspace_id"] == WORKSPACE
    assert Path(start_call["codex_home"]).is_dir()
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
        capability_root=tmp_path / "caps-missing",
        ensure_session=lambda *, session: None,
        start_workspace_codex_home=start_workspace_codex_home,
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
        missing.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
            fence=FENCE,
        )
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

    def start_workspace_codex_home(
        self, *, session: str, workdir: str, instance_id: str,
        project_id: str, workspace_id: str, codex_home: str,
        label: str | None = None, display_name: str | None = None,
    ) -> dict[str, object]:
        self.seq += 1
        pane_id = f"pane-{self.seq}"
        self.started += 1
        self.panes[pane_id] = workdir
        self.last_home = codex_home
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
        capability_root=tmp_path / "caps",
        ensure_session=herdr.ensure_session,
        start_workspace_codex_home=herdr.start_workspace_codex_home,
        get_launch_descriptor=herdr.get_launch_descriptor,
        get_launch_descriptor_by_instance=herdr.get_launch_descriptor_by_instance,
        snapshot=herdr.session_snapshot,
        close_pane=herdr.close_pane,
        new_instance_id=lambda: INSTANCE,
    )
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1, fence=FENCE,
        )
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
        capability_root=tmp_path / "caps",
        ensure_session=herdr.ensure_session,
        start_workspace_codex_home=herdr.start_workspace_codex_home,
        get_launch_descriptor=herdr.get_launch_descriptor,
        get_launch_descriptor_by_instance=herdr.get_launch_descriptor_by_instance,
        snapshot=herdr.session_snapshot,
        close_pane=herdr.close_pane,
        new_instance_id=lambda: INSTANCE,
    )
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1, fence=FENCE,
        )
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
        start_workspace_codex_home=herdr.start_workspace_codex_home,
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


def test_attach_requires_paired_workspace_authority_and_does_not_invent_format(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "chk"
    checkout.mkdir()
    started: list[dict[str, object]] = []

    def start_workspace_codex_home(**kwargs):
        started.append(kwargs)
        return {
            "available": True, "pane_id": "pane-1",
            "instance_id": INSTANCE, "cwd": str(checkout),
        }

    harness = harness_mod.LocalCodexHarness(
        capability_root=tmp_path / "caps",
        ensure_session=lambda *, session: None,
        start_workspace_codex_home=start_workspace_codex_home,
        get_launch_descriptor=lambda *, session, pane_id: {
            "session": session, "pane_id": pane_id, "instance_id": INSTANCE,
            "workdir": str(checkout), "kind": "codex",
        },
        get_launch_descriptor_by_instance=lambda instance_id, **_kw: {
            "session": "s", "pane_id": "pane-1", "instance_id": instance_id,
            "workdir": str(checkout), "kind": "codex",
        },
        snapshot=lambda *, session: {
            "panes": [{"pane_id": "pane-1", "cwd": str(checkout)}],
        },
        close_pane=lambda *, session, pane_id: {"available": True},
        new_instance_id=lambda: INSTANCE,
    )
    with pytest.raises(TypeError):
        harness.attach_readonly(session="s", checkout_path=checkout)
    with pytest.raises(TypeError):
        harness.attach_readonly(
            session="s", checkout_path=checkout, project_id=PROJECT,
        )
    assert started == []

    forged = "not-a-project-id"
    attached = harness.attach_readonly(
        session="s", checkout_path=checkout,
        project_id=forged, workspace_id=WORKSPACE,
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1, fence=FENCE,
    )
    assert attached.identity_verified is True
    assert started[-1]["project_id"] == forged
    assert started[-1]["workspace_id"] == WORKSPACE
    assert "args" not in started[-1]


def _harness(tmp_path: Path, *, prompts: list[str] | None = None) -> harness_mod.LocalCodexHarness:
    checkout = tmp_path / "chk"
    checkout.mkdir(exist_ok=True)
    sent = prompts if prompts is not None else []

    def prompt(session: str, pane_id: str, text: str) -> dict[str, object]:
        sent.append(text)
        return {"available": True, "session": session, "pane_id": pane_id}

    return harness_mod.LocalCodexHarness(
        capability_root=tmp_path / "caps",
        wakeup_prompt=prompt,
        ensure_session=lambda *, session: None,
        start_agent=lambda **kwargs: {
            "available": True, "pane_id": "pane-1",
            "instance_id": INSTANCE, "cwd": str(checkout),
        },
        get_launch_descriptor=lambda *, session, pane_id: {
            "session": session, "pane_id": pane_id, "instance_id": INSTANCE,
            "workdir": str(checkout), "kind": "codex", "args": ["--sandbox", "read-only"],
        },
        get_launch_descriptor_by_instance=lambda instance_id, **_kw: {
            "session": "s", "pane_id": "pane-1", "instance_id": instance_id,
            "workdir": str(checkout), "kind": "codex", "args": ["--sandbox", "read-only"],
        },
        snapshot=lambda *, session: {
            "panes": [{"pane_id": "pane-1", "cwd": str(checkout)}],
        },
        close_pane=lambda *, session, pane_id: {"available": True},
        new_instance_id=lambda: INSTANCE,
    )


def test_launch_spec_stays_readonly_public_args(tmp_path: Path) -> None:
    checkout = tmp_path / "chk"
    checkout.mkdir()
    spec = harness_mod.LocalCodexHarness(
        capability_root=tmp_path / "caps",
    ).build_launch_spec(checkout)
    spec.assert_readonly()
    assert spec.args == harness_mod.LAUNCH_ARGS == ("--sandbox", "read-only")
    assert spec.argv_text() == "--sandbox read-only"
    assert SENTINEL not in spec.argv_text()


def test_private_home_does_not_touch_user_codex_config_and_cap_is_0600(
    tmp_path: Path,
) -> None:
    user_cfg = Path.home() / ".codex" / "config.toml"
    before = user_cfg.stat() if user_cfg.exists() else None
    harness = _harness(tmp_path)
    issued = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence="sha256:" + "ab" * 32, session="s", pane_id="pane-1",
    )
    assert issued["capability_path"].stat().st_mode & 0o777 == 0o600
    home = Path(issued["codex_home"])
    assert home.is_dir()
    assert home.stat().st_mode & 0o777 == 0o700
    config = home / "config.toml"
    assert config.is_file()
    assert config.stat().st_mode & 0o777 == 0o600
    text = config.read_text(encoding="utf-8")
    assert "mcp_servers" in text
    assert "cockpit" in text
    assert str(issued["capability_path"]) in text
    assert SENTINEL not in text
    secret = issued["capability_path"].read_text(encoding="utf-8")
    assert "token" in secret
    after = user_cfg.stat() if user_cfg.exists() else None
    if before is None:
        assert after is None
    else:
        assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)


def test_wakeup_is_fixed_and_omits_boss_body(tmp_path: Path) -> None:
    prompts: list[str] = []
    harness = _harness(tmp_path, prompts=prompts)
    issued = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence="sha256:" + "ab" * 32, session="s", pane_id="pane-1",
    )
    with pytest.raises(harness_mod.HarnessError) as extra:
        harness.wakeup(ATTACHMENT, body=SENTINEL)
    assert extra.value.code == "invalid_argument"
    result = harness.wakeup(ATTACHMENT)
    assert prompts == [harness_mod.WAKEUP_TEXT]
    assert result["digest"] == harness_mod.WAKEUP_DIGEST
    assert SENTINEL not in prompts[0]
    assert SENTINEL not in issued["capability_path"].read_text(encoding="utf-8")
    spec = harness.build_launch_spec(tmp_path / "chk")
    assert SENTINEL not in spec.argv_text()
    descriptor = harness._get_launch_descriptor(session="s", pane_id="pane-1")
    assert SENTINEL not in str(descriptor)


def test_attachment_id_and_private_leaf_paths_fail_closed(tmp_path: Path) -> None:
    outside = tmp_path / "ESCAPED-att.cap"
    outside.write_bytes(SENTINEL.encode())
    outside_stat = (outside.read_bytes(), outside.stat().st_size)
    harness = _harness(tmp_path)
    for bad in (
        "../../../tmp/ESCAPED-att",
        "x/../../ESCAPED-att",
        "att_" + "c" * 31 + "\n",
        "att_" + "c" * 16 + "/" + "d" * 15,
        "att_" + "C" * 32,
        "idn_" + "c" * 32,
    ):
        with pytest.raises(harness_mod.HarnessError) as error:
            harness.issue_capability(
                attachment_id=bad, identity_id=IDENTITY, generation=1,
                fence="sha256:" + "ab" * 32, session="s", pane_id="pane-1",
            )
        assert error.value.code == "invalid_argument"
        assert str(error.value) == "invalid_argument"
        assert bad not in str(error.value)
        with pytest.raises(harness_mod.HarnessError) as wakeup:
            harness.wakeup(bad)
        assert wakeup.value.code == "invalid_argument"
        assert str(wakeup.value) == "invalid_argument"
    assert list((tmp_path / "caps").glob("*") if (tmp_path / "caps").exists() else []) == []
    assert (outside.read_bytes(), outside.stat().st_size) == outside_stat
    assert not (tmp_path / "tmp").exists()

    issued = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence="sha256:" + "ab" * 32, session="s", pane_id="pane-1",
    )
    linked = tmp_path / "hard-outside.cap"
    linked.write_bytes(SENTINEL.encode())
    issued["capability_path"].unlink()
    os.link(linked, issued["capability_path"])
    with pytest.raises(harness_mod.HarnessError) as hardlink:
        harness.issue_capability(
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=2,
            fence="sha256:" + "ab" * 32, session="s", pane_id="pane-1",
        )
    assert hardlink.value.code == "invalid_argument"
    assert linked.read_bytes() == SENTINEL.encode()

    issued["capability_path"].unlink()
    issued["capability_path"].symlink_to(linked)
    with pytest.raises(harness_mod.HarnessError) as leaf:
        harness.wakeup(ATTACHMENT)
    assert leaf.value.code == "invalid_argument"
    assert str(leaf.value) == "invalid_argument"
    assert linked.read_bytes() == SENTINEL.encode()

    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "via-link"
    alias.symlink_to(real)
    escaped = harness_mod.LocalCodexHarness(capability_root=alias / "caps")
    with pytest.raises(harness_mod.HarnessError) as ancestor:
        escaped.issue_capability(
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
            fence="sha256:" + "ab" * 32, session="s", pane_id="pane-1",
        )
    assert ancestor.value.code == "invalid_argument"
    assert not (real / "caps").exists()

    root_link = tmp_path / "caps-link"
    (tmp_path / "caps-real").mkdir()
    root_link.symlink_to(tmp_path / "caps-real")
    linked_root = harness_mod.LocalCodexHarness(capability_root=root_link)
    with pytest.raises(harness_mod.HarnessError) as root:
        linked_root.issue_capability(
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
            fence="sha256:" + "ab" * 32, session="s", pane_id="pane-1",
        )
    assert root.value.code == "invalid_argument"

    issued["capability_path"].unlink()
    restored = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=3,
        fence="sha256:" + "ab" * 32, session="s", pane_id="pane-1",
    )
    sidecar = tmp_path / "caps" / f"{ATTACHMENT}.generation"
    sidecar.unlink()
    with pytest.raises(harness_mod.HarnessError) as missing:
        harness_mod.current_generation(restored["capability_path"])
    assert missing.value.code == "stale_generation"
    assert str(missing.value) == "stale_generation"


def _attach_harness(tmp_path: Path, *, start=None, descriptors: bool = True, close: str = "ok"):
    checkout = tmp_path / "chk"
    checkout.mkdir(exist_ok=True)
    started: list[dict[str, object]] = []
    generic: list[dict[str, object]] = []
    panes: dict[str, str] = {}

    def start_workspace_codex_home(**kwargs):
        started.append(kwargs)
        if start is not None:
            return start(kwargs, panes)
        panes["pane-1"] = kwargs["workdir"]
        return {
            "available": True, "pane_id": "pane-1",
            "instance_id": kwargs["instance_id"], "cwd": kwargs["workdir"],
        }

    def start_agent(**kwargs):
        generic.append(kwargs)
        raise AssertionError("generic start_agent must not be used")

    def get_launch_descriptor(*, session: str, pane_id: str):
        if not descriptors or pane_id not in panes:
            return None
        return {
            "session": session, "pane_id": pane_id, "instance_id": INSTANCE,
            "workdir": str(checkout), "kind": "codex",
            "args": ["--sandbox", "read-only"], "codex_home": str(tmp_path / "caps" / f"{ATTACHMENT}.home"),
        }

    def get_launch_descriptor_by_instance(instance_id: str, **_kw):
        if not descriptors or not panes:
            return None
        pane_id = next(iter(panes))
        return {
            "session": "s", "pane_id": pane_id, "instance_id": instance_id,
            "workdir": str(checkout), "kind": "codex",
            "args": ["--sandbox", "read-only"],
        }

    def snapshot(*, session: str):
        return {"panes": [{"pane_id": pane, "cwd": cwd} for pane, cwd in panes.items()]}

    def close_pane(*, session: str, pane_id: str):
        if close == "raise":
            raise RuntimeError("close lost")
        panes.pop(pane_id, None)
        return {"available": True}

    harness = harness_mod.LocalCodexHarness(
        capability_root=tmp_path / "caps",
        ensure_session=lambda *, session: None,
        start_agent=start_agent,
        start_workspace_codex_home=start_workspace_codex_home,
        get_launch_descriptor=get_launch_descriptor,
        get_launch_descriptor_by_instance=get_launch_descriptor_by_instance,
        snapshot=snapshot,
        close_pane=close_pane,
        new_instance_id=lambda: INSTANCE,
    )
    return harness, checkout, started, generic, panes


def test_attach_issues_private_home_and_never_calls_start_agent(tmp_path: Path) -> None:
    user_cfg = Path.home() / ".codex" / "config.toml"
    before = user_cfg.stat() if user_cfg.exists() else None
    harness, checkout, started, generic, panes = _attach_harness(tmp_path)
    with pytest.raises(harness_mod.HarnessError):
        harness.wakeup(ATTACHMENT)
    attached = harness.attach_readonly(
        session="s", checkout_path=checkout,
        project_id=PROJECT, workspace_id=WORKSPACE,
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1, fence=FENCE,
    )
    assert generic == []
    assert len(started) == 1
    assert "args" not in started[0]
    home = Path(started[0]["codex_home"])
    assert home == tmp_path / "caps" / f"{ATTACHMENT}.home"
    assert home.is_dir()
    assert home.stat().st_mode & 0o777 == 0o700
    cap = tmp_path / "caps" / f"{ATTACHMENT}.cap"
    assert cap.is_file()
    assert cap.stat().st_mode & 0o777 == 0o600
    config = home / "config.toml"
    assert config.is_file()
    assert config.stat().st_mode & 0o777 == 0o600
    record = json.loads(cap.read_text(encoding="utf-8"))
    assert record["attachment_id"] == ATTACHMENT
    assert record["identity_id"] == IDENTITY
    assert record["generation"] == 1
    assert record["fence"] == FENCE
    assert record["checkout"] == str(checkout)
    assert record["pane_id"] == attached.pane_id == "pane-1"
    assert record["instance_id"] == attached.instance_id == INSTANCE
    assert "token" not in str(started[0])
    assert "token" not in str(harness._get_launch_descriptor(session="s", pane_id="pane-1"))
    assert SENTINEL not in config.read_text(encoding="utf-8")
    woken = harness.wakeup(ATTACHMENT)
    assert woken["text"] == harness_mod.WAKEUP_TEXT
    after = user_cfg.stat() if user_cfg.exists() else None
    if before is None:
        assert after is None
    else:
        assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
    harness.detach(session="s", pane_id=attached.pane_id)
    assert panes == {}
    assert home.is_dir()
    assert cap.is_file()


def test_attach_start_failure_leaves_no_usable_cap_or_pane(tmp_path: Path) -> None:
    def fail(kwargs, panes):
        return {"available": True, "error_code": "runtime_unavailable", "error": "no"}

    harness, checkout, started, generic, panes = _attach_harness(tmp_path, start=fail)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1, fence=FENCE,
        )
    assert error.value.code == "runtime_unavailable"
    assert generic == []
    assert started
    assert panes == {}
    assert not (tmp_path / "caps" / f"{ATTACHMENT}.cap").exists()
    with pytest.raises(harness_mod.HarnessError) as wakeup:
        harness.wakeup(ATTACHMENT)
    assert wakeup.value.code == "invalid_argument"


def test_attach_observe_failure_closes_pane_and_retires_cap(tmp_path: Path) -> None:
    harness, checkout, _started, generic, panes = _attach_harness(
        tmp_path, descriptors=False,
    )
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1, fence=FENCE,
        )
    assert error.value.code == "runtime_identity_unverified"
    assert generic == []
    assert panes == {}
    assert not (tmp_path / "caps" / f"{ATTACHMENT}.cap").exists()


def test_old_generation_and_fence_fail_closed(tmp_path: Path) -> None:
    harness, checkout, _started, _generic, _panes = _attach_harness(tmp_path)
    harness.attach_readonly(
        session="s", checkout_path=checkout,
        project_id=PROJECT, workspace_id=WORKSPACE,
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1, fence=FENCE,
    )
    (tmp_path / "caps" / f"{ATTACHMENT}.generation").write_bytes(b"2\n")
    with pytest.raises(harness_mod.HarnessError) as stale:
        harness.wakeup(ATTACHMENT)
    assert stale.value.code in {"stale_generation", "invalid_argument"}
    (tmp_path / "caps" / f"{ATTACHMENT}.generation").write_bytes(b"1\n")
    cap = tmp_path / "caps" / f"{ATTACHMENT}.cap"
    record = json.loads(cap.read_text(encoding="utf-8"))
    record["fence"] = "sha256:" + "cd" * 32
    cap.write_bytes(json.dumps(record).encode())
    os.chmod(cap, 0o600)
    with pytest.raises(harness_mod.HarnessError) as fence:
        harness.wakeup(ATTACHMENT)
    assert fence.value.code == "invalid_argument"
