from __future__ import annotations

import inspect
import json
import os
import secrets
import stat
import re
import shutil
import subprocess
import sys
import time
import tomllib
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
AUTH_BYTES_SENTINEL = "AUTH-BYTES-SENTINEL-4e91"


@pytest.fixture(autouse=True)
def _provider_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    home = tmp_path / "provider-home"
    authority = tmp_path / "provider-authority"
    home.mkdir(mode=0o700)
    authority.mkdir(mode=0o700)
    auth = authority / "auth.json"
    auth.write_text(AUTH_BYTES_SENTINEL, encoding="utf-8")
    os.chmod(auth, 0o600)
    config = home / "relay.config.toml"
    config.write_text(
        "\n".join([
            'model_provider = "relay"',
            'network_access = "enabled"',
            "",
            "[model_providers.relay]",
            'name = "Fixture Relay"',
            'base_url = "https://relay.invalid"',
            'wire_api = "responses"',
            "",
            "[model_providers.relay.auth]",
            'command = "/usr/bin/jq"',
            f'args = ["-r", ".OPENAI_API_KEY", {json.dumps(str(auth))}]',
            "timeout_ms = 5000",
            "refresh_interval_ms = 300000",
            "",
            "[model_providers.unselected]",
            f'api_key = "{AUTH_BYTES_SENTINEL}"',
            "",
        ]),
        encoding="utf-8",
    )
    os.chmod(config, 0o600)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return {"home": home, "config": config, "authority": authority, "auth": auth}


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
    tmp_path: Path, _provider_provenance: dict[str, Path],
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
    assert "agent_cockpit.workspace_mcp_entry" in text
    assert "private_codex_mcp" not in text
    assert str(issued["capability_path"]) in text
    assert SENTINEL not in text
    secret = issued["capability_path"].read_text(encoding="utf-8")
    token = json.loads(secret)["token"]
    assert token not in text
    assert FENCE not in text
    assert 'model_provider = "relay"' in text
    assert str(_provider_provenance["auth"]) in text
    assert AUTH_BYTES_SENTINEL not in text
    assert not (home / "auth.json").exists()
    after = user_cfg.stat() if user_cfg.exists() else None
    if before is None:
        assert after is None
    else:
        assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)


def test_provider_auth_reference_is_validated_without_reading_credential_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    _provider_provenance: dict[str, Path],
) -> None:
    auth = _provider_provenance["auth"]
    auth_identity = (auth.stat().st_dev, auth.stat().st_ino)
    original_read = harness_mod.os.read

    def guarded_read(fd: int, size: int) -> bytes:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == auth_identity:
            raise AssertionError("credential bytes must not be read")
        return original_read(fd, size)

    monkeypatch.setattr(harness_mod.os, "read", guarded_read)
    issued = _harness(tmp_path).issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    text = Path(issued["codex_home"], "config.toml").read_text(encoding="utf-8")
    assert str(auth) in text
    assert AUTH_BYTES_SENTINEL not in text


@pytest.mark.parametrize("failure", [
    "missing", "mode", "symlink", "hardlink", "owner", "parent_symlink",
])
def test_provider_auth_reference_rejects_unsafe_authority_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    _provider_provenance: dict[str, Path], failure: str,
) -> None:
    auth = _provider_provenance["auth"]
    config = _provider_provenance["config"]
    if failure == "missing":
        auth.unlink()
    elif failure == "mode":
        os.chmod(auth, 0o640)
    elif failure == "symlink":
        target = auth.with_name("target.json")
        auth.replace(target)
        auth.symlink_to(target)
    elif failure == "hardlink":
        os.link(auth, auth.with_name("linked.json"))
    elif failure == "owner":
        original_fstat = harness_mod.os.fstat
        auth_identity = (auth.stat().st_dev, auth.stat().st_ino)

        def wrong_owner(fd: int):
            info = original_fstat(fd)
            if (info.st_dev, info.st_ino) != auth_identity:
                return info
            values = list(info)
            values[4] = info.st_uid + 1
            return os.stat_result(values)

        monkeypatch.setattr(harness_mod.os, "fstat", wrong_owner)
    else:
        real_parent = tmp_path / "real-parent"
        real_parent.mkdir(mode=0o700)
        moved = real_parent / "auth.json"
        auth.replace(moved)
        linked_parent = tmp_path / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                str(auth), str(linked_parent / "auth.json"),
            ),
            encoding="utf-8",
        )
        os.chmod(config, 0o600)

    started: list[dict[str, object]] = []
    harness, checkout, _calls, _generic, _panes = _attach_harness(tmp_path)
    harness._start_workspace_codex_home = lambda **kwargs: (
        started.append(kwargs) or {"available": True}
    )
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY,
            generation=1, fence=FENCE,
        )
    assert error.value.code == "runtime_unavailable"
    assert started == []


def test_provider_auth_reference_detects_toctou_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    _provider_provenance: dict[str, Path],
) -> None:
    auth = _provider_provenance["auth"]
    original_open = harness_mod.os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == auth and not swapped:
            swapped = True
            replacement = auth.with_name("replacement.json")
            replacement.write_text("replacement", encoding="utf-8")
            os.chmod(replacement, 0o600)
            replacement.replace(auth)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(harness_mod.os, "open", swapping_open)
    started: list[dict[str, object]] = []
    harness, checkout, _calls, _generic, _panes = _attach_harness(tmp_path)
    harness._start_workspace_codex_home = lambda **kwargs: (
        started.append(kwargs) or {"available": True}
    )
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY,
            generation=1, fence=FENCE,
        )
    assert error.value.code == "runtime_unavailable"
    assert started == []


def test_provider_metadata_rejects_symlink_ancestor_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    _provider_provenance: dict[str, Path],
) -> None:
    home = _provider_provenance["home"]
    real_parent = tmp_path / "real-metadata-parent"
    real_parent.mkdir(mode=0o700)
    moved_home = real_parent / home.name
    home.rename(moved_home)
    linked_parent = tmp_path / "linked-metadata-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    monkeypatch.setenv("CODEX_HOME", str(linked_parent / home.name))
    started: list[dict[str, object]] = []
    harness, checkout, _calls, _generic, _panes = _attach_harness(tmp_path)
    harness._start_workspace_codex_home = lambda **kwargs: (
        started.append(kwargs) or {"available": True}
    )

    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY,
            generation=1, fence=FENCE,
        )

    assert error.value.code == "runtime_unavailable"
    assert started == []


def test_auth_recheck_failure_retires_private_files_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    _provider_provenance: dict[str, Path],
) -> None:
    auth = _provider_provenance["auth"]
    auth_identity = (auth.stat().st_dev, auth.stat().st_ino)
    original_validate = harness_mod._validate_auth_reference
    calls = 0

    def fail_final_recheck(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise harness_mod.HarnessError("runtime_unavailable")
        original_validate(path)

    monkeypatch.setattr(
        harness_mod, "_validate_auth_reference", fail_final_recheck,
    )
    started: list[dict[str, object]] = []
    harness, checkout, _calls, _generic, _panes = _attach_harness(tmp_path)
    harness._start_workspace_codex_home = lambda **kwargs: (
        started.append(kwargs) or {"available": True}
    )

    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY,
            generation=1, fence=FENCE,
        )

    root = tmp_path / "caps"
    assert error.value.code == "runtime_unavailable"
    assert started == []
    assert not (root / f"{ATTACHMENT}.cap").exists()
    assert not (root / f"{ATTACHMENT}.generation").exists()
    assert not (root / f"{ATTACHMENT}.fence").exists()
    assert not (root / f"{ATTACHMENT}.home" / "config.toml").exists()
    assert auth.is_file()
    assert (auth.stat().st_dev, auth.stat().st_ino) == auth_identity


def test_retire_private_runtime_never_removes_external_auth_authority(
    tmp_path: Path, _provider_provenance: dict[str, Path],
) -> None:
    auth = _provider_provenance["auth"]
    identity = (auth.stat().st_dev, auth.stat().st_ino)
    harness = _harness(tmp_path)
    harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    harness._retire_private_runtime(ATTACHMENT)
    assert auth.is_file()
    assert (auth.stat().st_dev, auth.stat().st_ino) == identity


_REAL_PROVIDER_HOME = Path(
    os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
)


@pytest.mark.skipif(
    shutil.which("codex") is None
    or shutil.which("herdr") is None
    or not (_REAL_PROVIDER_HOME / "relay.config.toml").is_file(),
    reason="需要真实 herdr/codex 与合法 custom-provider auth provenance",
)
def test_real_private_home_attach_and_fixed_wakeup_reaches_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl
    import pty
    import struct
    import tempfile
    import termios
    import uuid

    isolated = Path(tempfile.mkdtemp(prefix="e3-auth-", dir="/tmp"))
    isolated.chmod(0o700)
    session = "e3-auth-" + uuid.uuid4().hex[:8]
    workdir = isolated / "checkout"
    workdir.mkdir(mode=0o700)
    config_dir = isolated / "herdr"
    config_dir.mkdir(mode=0o700)
    (config_dir / "config.toml").write_text(
        "onboarding = false\n[update]\nmanifest_check = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(_REAL_PROVIDER_HOME))
    real_provider_auth = harness_mod._provider_auth_provenance().authority
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(config_dir / "config.toml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated / "xdg-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(isolated / "xdg-data"))
    monkeypatch.setenv(
        "COCKPIT_LAUNCH_DESCRIPTORS_PATH", str(isolated / "launch.json"),
    )
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.setattr(
        herdr_client.next_profile, "require_session", lambda value: value,
    )
    owned = None
    client = None
    master_fd = None
    slave_fd = None
    pane_id = None
    log_handle = open(isolated / "herdr.log", "wb")
    try:
        owned = subprocess.Popen(
            [herdr_client.HERDR_BIN, "--session", session, "server"],
            stdin=subprocess.DEVNULL, stdout=log_handle,
            stderr=subprocess.STDOUT, close_fds=True, start_new_session=True,
        )
        log_handle.close()
        herdr_client._SESSION_BOOTSTRAP_PROCESSES[session] = owned
        harness = harness_mod.LocalCodexHarness(
            capability_root=isolated / "data" / "capabilities",
        )
        attached = harness.attach_readonly(
            session=session, checkout_path=workdir,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY,
            generation=1, fence=FENCE,
        )
        pane_id = attached.pane_id
        private_home = isolated / "data" / "capabilities" / f"{ATTACHMENT}.home"
        private_config = private_home / "config.toml"
        assert private_home.stat().st_mode & 0o777 == 0o700
        assert private_config.stat().st_mode & 0o777 == 0o600
        assert str(real_provider_auth) in private_config.read_text(encoding="utf-8")
        assert not (private_home / "auth.json").exists()
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(
            slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0),
        )
        client = subprocess.Popen(
            [herdr_client.HERDR_BIN, "--session", session],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            close_fds=True, start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = None
        time.sleep(1)
        assert client.poll() is None
        herdr_client._run(
            ["--session", session, "agent", "focus", pane_id], timeout=8,
        )
        herdr_client._run(
            ["--session", session, "agent", "send-keys", pane_id, "enter"],
            timeout=5,
        )
        deadline = time.monotonic() + 10
        before = herdr_client.inspect_agent(session, pane_id)
        while not before.get("visible_idle") and time.monotonic() < deadline:
            time.sleep(0.2)
            before = herdr_client.inspect_agent(session, pane_id)
        assert before.get("agent_status") == "idle", before
        assert before.get("visible_idle") is True, before
        assert type(before.get("state_change_seq")) is int
        receipt = harness.wakeup(ATTACHMENT)
        after = herdr_client.inspect_agent(session, pane_id)
        assert receipt == {
            "digest": harness_mod.WAKEUP_DIGEST,
            "text": harness_mod.WAKEUP_TEXT,
        }
        assert after.get("agent_status") == "working"
        assert type(after.get("state_change_seq")) is int
        assert type(before.get("state_change_seq")) is int
        assert after["state_change_seq"] > before["state_change_seq"]
    finally:
        if client is not None:
            if client.poll() is None:
                client.terminate()
                try:
                    client.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    client.kill()
                    client.wait(timeout=5)
        if master_fd is not None:
            os.close(master_fd)
        if slave_fd is not None:
            os.close(slave_fd)
        if pane_id is not None:
            try:
                herdr_client.close_pane(session, pane_id)
            except Exception:
                pass
        subprocess.run(
            [herdr_client.HERDR_BIN, "--session", session, "session", "close"],
            capture_output=True, text=True, timeout=15,
        )
        if owned is not None:
            if owned.poll() is None:
                owned.terminate()
                try:
                    owned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    owned.kill()
                    owned.wait(timeout=5)
        if not log_handle.closed:
            log_handle.close()
        herdr_client._SESSION_BOOTSTRAP_PROCESSES.pop(session, None)
        shutil.rmtree(isolated, ignore_errors=True)


def test_wakeup_is_fixed_and_omits_boss_body(tmp_path: Path) -> None:
    expected_wakeup = (
        "COCKPIT_WAKEUP_V1\n"
        "Start the dispatched-work workflow now. Call claim_current with {} first. "
        "After it returns, read root_message.body and do that work in the managed "
        "checkout. Call apply_patch with claim_revision and lease_revision from "
        "claim_current, plus patch as a unified diff. Then call reply_complete with "
        "the same claim_revision, lease_revision returned by apply_patch, and body "
        "as a concise completion summary."
    )
    assert harness_mod.WAKEUP_TEXT == expected_wakeup
    assert harness_mod.WAKEUP_TEXT.count("COCKPIT_WAKEUP_V1") == 1
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
    token = json.loads(
        issued["capability_path"].read_text(encoding="utf-8")
    )["token"]
    assert SENTINEL not in prompts[0]
    assert token not in prompts[0]
    assert FENCE not in prompts[0]
    assert SENTINEL not in issued["capability_path"].read_text(encoding="utf-8")
    spec = harness.build_launch_spec(tmp_path / "chk")
    assert SENTINEL not in spec.argv_text()
    descriptor = harness._get_launch_descriptor(session="s", pane_id="pane-1")
    assert SENTINEL not in str(descriptor)


def test_wakeup_treats_prompt_error_as_failure_not_success(
    tmp_path: Path,
) -> None:
    issued_prompts: list[str] = []

    def prompt(session: str, pane_id: str, text: str) -> dict[str, object]:
        issued_prompts.append(text)
        return {"available": True, "error": "agent_prompt_stalled", "sent": text}

    harness = _harness(tmp_path)
    harness._wakeup_prompt = prompt
    harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.wakeup(ATTACHMENT)
    assert error.value.code == "runtime_unavailable"
    assert error.value.unknown is False
    assert issued_prompts == [harness_mod.WAKEUP_TEXT]
    assert SENTINEL not in issued_prompts[0]


def test_wakeup_requires_executing_receipt_from_default_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def submit(session: str, target: str, text: str) -> dict[str, object]:
        calls.append((session, target, text))
        return {
            "available": True, "submitted": True, "executing": True,
            "status": "working", "target": target,
        }

    monkeypatch.setattr(
        harness_mod.herdr_client, "submit_agent_prompt_until_working", submit,
    )
    harness = harness_mod.LocalCodexHarness(
        capability_root=tmp_path / "caps",
        ensure_session=lambda *, session: None,
        start_agent=lambda **kwargs: {"available": True},
        get_launch_descriptor=lambda **kwargs: {
            "session": "s", "pane_id": "pane-1", "instance_id": INSTANCE,
            "workdir": str(tmp_path / "chk"), "kind": "codex",
        },
        get_launch_descriptor_by_instance=lambda instance_id, **_kw: {
            "session": "s", "pane_id": "pane-1", "instance_id": instance_id,
            "workdir": str(tmp_path / "chk"), "kind": "codex",
        },
        snapshot=lambda *, session: {
            "panes": [{"pane_id": "pane-1", "cwd": str(tmp_path / "chk")}],
        },
        close_pane=lambda *, session, pane_id: {"available": True},
        new_instance_id=lambda: INSTANCE,
    )
    (tmp_path / "chk").mkdir()
    harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    result = harness.wakeup(ATTACHMENT)
    assert result["digest"] == harness_mod.WAKEUP_DIGEST
    assert calls == [("s", "pane-1", harness_mod.WAKEUP_TEXT)]
    assert SENTINEL not in calls[0][2]


def test_wakeup_idle_after_submit_is_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        harness_mod.herdr_client, "submit_agent_prompt_until_working",
        lambda session, target, text: {
            "available": True, "submitted": False, "executing": False,
            "error": "agent_prompt_stalled",
        },
    )
    harness = harness_mod.LocalCodexHarness(
        capability_root=tmp_path / "caps",
        ensure_session=lambda *, session: None,
        start_agent=lambda **kwargs: {"available": True},
        get_launch_descriptor=lambda **kwargs: None,
        get_launch_descriptor_by_instance=lambda instance_id, **_kw: None,
        snapshot=lambda *, session: {"panes": []},
        close_pane=lambda *, session, pane_id: {"available": True},
        new_instance_id=lambda: INSTANCE,
    )
    harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.wakeup(ATTACHMENT)
    assert error.value.code == "runtime_unavailable"
    assert error.value.unknown is False


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
        wakeup_prompt=lambda session, pane_id, text: {
            "available": True, "session": session, "pane_id": pane_id,
        },
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


def _parse_mcp_config(text: str) -> dict[str, object]:
    parsed = tomllib.loads(text)
    cockpit = parsed["mcp_servers"]["cockpit"]
    return {
        "command": cockpit["command"],
        "args": cockpit["args"],
        "env": cockpit["env"],
    }


def _rpc(*messages: dict[str, object]) -> str:
    return "".join(json.dumps(item) + "\n" for item in messages)


def _run_generated_mcp(tmp_path: Path, config_text: str, payload: str) -> subprocess.CompletedProcess[str]:
    parsed = _parse_mcp_config(config_text)
    isolated = tmp_path.parent / f"{tmp_path.name}-iso"
    elsewhere = isolated / "cwd"
    isolated_home = isolated / "home"
    elsewhere.mkdir(parents=True)
    isolated_home.mkdir(parents=True)
    env = {
        "HOME": str(isolated_home),
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
    }
    env.update({key: str(value) for key, value in parsed["env"].items()})
    return subprocess.run(
        [str(parsed["command"]), *[str(item) for item in parsed["args"]]],
        input=payload,
        env=env,
        cwd=elsewhere,
        text=True,
        capture_output=True,
        check=False,
    )


def test_generated_config_subprocess_lists_exact_three_tools(tmp_path: Path) -> None:
    dashboard = Path.home() / "dashboard-data"
    before = set(dashboard.rglob("*")) if dashboard.exists() else None
    harness, checkout, _started, _generic, _panes = _attach_harness(tmp_path)
    attached = harness.attach_readonly(
        session="s", checkout_path=checkout,
        project_id=PROJECT, workspace_id=WORKSPACE,
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1, fence=FENCE,
    )
    config = Path(tmp_path / "caps" / f"{ATTACHMENT}.home" / "config.toml")
    text = config.read_text(encoding="utf-8")
    parsed = _parse_mcp_config(text)
    assert parsed["command"] == str(Path(sys.executable).resolve())
    assert parsed["args"] == ["-P", "-m", "agent_cockpit.workspace_mcp_entry"]
    assert "private_codex_mcp" not in text
    token = json.loads(
        (tmp_path / "caps" / f"{ATTACHMENT}.cap").read_text(encoding="utf-8")
    )["token"]
    assert token not in text
    assert FENCE not in text
    assert SENTINEL not in text
    assert attached.pane_id == "pane-1"
    result = _run_generated_mcp(tmp_path, text, _rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "claim_current", "arguments": {}},
        },
        {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "run", "arguments": {}},
        },
        {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "fail", "arguments": {}},
        },
    ))
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    by_id = {reply["id"]: reply for reply in replies}
    names = [tool["name"] for tool in by_id[2]["result"]["tools"]]
    assert names == ["claim_current", "apply_patch", "reply_complete"]
    assert "run" not in names and "fail" not in names
    assert by_id[3]["result"]["isError"] is True
    assert by_id[3]["result"]["structuredContent"]["code"]
    assert by_id[4]["result"]["isError"] is True
    assert by_id[5]["result"]["isError"] is True
    assert not list(tmp_path.rglob("*.sqlite3"))
    if before is None:
        assert not dashboard.exists()
    else:
        assert set(dashboard.rglob("*")) == before


def test_generated_config_missing_stores_typed_fail_closed(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    issued = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    text = Path(issued["codex_home"] / "config.toml").read_text(encoding="utf-8")
    result = _run_generated_mcp(tmp_path, text, _rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "claim_current", "arguments": {}},
        },
        {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "apply_patch",
                "arguments": {"claim_revision": 1, "lease_revision": 1, "patch": ""},
            },
        },
        {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {
                "name": "reply_complete",
                "arguments": {"claim_revision": 1, "lease_revision": 1, "body": "x"},
            },
        },
    ))
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    by_id = {reply["id"]: reply for reply in replies}
    names = [tool["name"] for tool in by_id[2]["result"]["tools"]]
    assert names == ["claim_current", "apply_patch", "reply_complete"]
    for ident in (3, 4, 5):
        denied = by_id[ident]["result"]
        assert denied["isError"] is True
        assert denied["structuredContent"]["code"] == "workspace_work_schema_missing"
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_private_file_replace_keeps_old_bytes_on_fault(tmp_path: Path, monkeypatch) -> None:
    harness = _harness(tmp_path)
    issued = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    cap = issued["capability_path"]
    before = cap.read_bytes()
    generation = tmp_path / "caps" / f"{ATTACHMENT}.generation"
    fence = tmp_path / "caps" / f"{ATTACHMENT}.fence"
    config = Path(issued["codex_home"]) / "config.toml"
    before_gen = generation.read_bytes()
    before_fence = fence.read_bytes()
    before_config = config.read_bytes()

    def boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("replace lost")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.issue_capability(
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=2,
            fence="sha256:" + "cd" * 32, session="s", pane_id="pane-1",
        )
    assert error.value.code == "invalid_argument"
    assert cap.read_bytes() == before
    assert generation.read_bytes() == before_gen
    assert fence.read_bytes() == before_fence
    assert config.read_bytes() == before_config
    assert cap.stat().st_size > 0
    assert list((tmp_path / "caps").glob(".*.tmp")) == []
    assert list((tmp_path / "caps").glob(".*.bak")) == []


def _authority_snapshot(tmp_path: Path, issued: dict[str, object]) -> dict[str, bytes]:
    home = Path(issued["codex_home"])
    return {
        "cap": issued["capability_path"].read_bytes(),
        "generation": (tmp_path / "caps" / f"{ATTACHMENT}.generation").read_bytes(),
        "fence": (tmp_path / "caps" / f"{ATTACHMENT}.fence").read_bytes(),
        "config": (home / "config.toml").read_bytes(),
    }


def _assert_authority_unchanged(
    tmp_path: Path, issued: dict[str, object], before: dict[str, bytes],
) -> None:
    home = Path(issued["codex_home"])
    assert issued["capability_path"].read_bytes() == before["cap"]
    assert (tmp_path / "caps" / f"{ATTACHMENT}.generation").read_bytes() == before["generation"]
    assert (tmp_path / "caps" / f"{ATTACHMENT}.fence").read_bytes() == before["fence"]
    assert (home / "config.toml").read_bytes() == before["config"]
    assert issued["capability_path"].stat().st_size > 0
    assert list((tmp_path / "caps").glob(".*.tmp")) == []
    assert list((tmp_path / "caps").glob(".*.bak")) == []
    assert list(home.glob(".*.tmp")) == []
    assert list(home.glob(".*.bak")) == []


def test_write_zero_progress_keeps_old_authority_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    issued = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    before = _authority_snapshot(tmp_path, issued)
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.issue_capability(
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=2,
            fence="sha256:" + "cd" * 32, session="s", pane_id="pane-1",
        )
    assert error.value.code == "invalid_argument"
    _assert_authority_unchanged(tmp_path, issued, before)


def test_write_partial_then_complete_publishes_full_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    issued = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    real_write = os.write

    def half(fd: int, data: object) -> int:
        view = memoryview(data)  # type: ignore[arg-type]
        if len(view) <= 1:
            return real_write(fd, view)
        return real_write(fd, view[: max(1, len(view) // 2)])

    monkeypatch.setattr(os, "write", half)
    updated = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=2,
        fence="sha256:" + "cd" * 32, session="s", pane_id="pane-1",
    )
    record = json.loads(updated["capability_path"].read_bytes().decode("utf-8"))
    assert record["generation"] == 2
    assert record["fence"] == "sha256:" + "cd" * 32
    assert list((tmp_path / "caps").glob(".*.tmp")) == []
    assert list((tmp_path / "caps").glob(".*.bak")) == []


def test_write_multiple_partials_then_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    real_write = os.write

    def one_byte(fd: int, data: object) -> int:
        view = memoryview(data)  # type: ignore[arg-type]
        if len(view) == 0:
            return 0
        return real_write(fd, view[:1])

    monkeypatch.setattr(os, "write", one_byte)
    updated = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=3,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    record = json.loads(updated["capability_path"].read_bytes().decode("utf-8"))
    assert record["generation"] == 3
    assert list((tmp_path / "caps").glob(".*.tmp")) == []
    assert list((tmp_path / "caps").glob(".*.bak")) == []


def test_write_partial_then_exception_keeps_old_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    issued = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    before = _authority_snapshot(tmp_path, issued)
    real_write = os.write
    state = {"n": 0}

    def flaky(fd: int, data: object) -> int:
        state["n"] += 1
        view = memoryview(data)  # type: ignore[arg-type]
        if state["n"] == 1 and len(view) > 1:
            return real_write(fd, view[:1])
        raise OSError("write lost after partial")

    monkeypatch.setattr(os, "write", flaky)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.issue_capability(
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=4,
            fence="sha256:" + "ef" * 32, session="s", pane_id="pane-1",
        )
    assert error.value.code == "invalid_argument"
    _assert_authority_unchanged(tmp_path, issued, before)


def _fail_fsync_on(
    monkeypatch: pytest.MonkeyPatch, *fail_at: int,
) -> dict[str, int]:
    real = os.fsync
    state = {"n": 0}
    failed = set(fail_at)

    def flaky(fd: int) -> None:
        state["n"] += 1
        if state["n"] in failed:
            raise OSError(f"fsync #{state['n']} lost")
        real(fd)

    monkeypatch.setattr(os, "fsync", flaky)
    return state


def _assert_no_write_sidecars(folder: Path) -> None:
    leftover = [
        path.name
        for path in folder.iterdir()
        if path.name.startswith(".")
        and (path.name.endswith(".tmp") or path.name.endswith(".bak"))
    ]
    assert leftover == []


def test_second_fsync_failure_restores_old_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "priv"
    parent.mkdir(mode=0o700)
    target = parent / "authority"
    old = b"old-authority-bytes"
    new = b"new-private-authority-payload"
    harness_mod._write_private(target, old)
    _fail_fsync_on(monkeypatch, 2)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness_mod._write_private(target, new)
    assert error.value.code == "invalid_argument"
    assert target.read_bytes() == old
    assert target.read_bytes() != new
    _assert_no_write_sidecars(parent)


def test_second_fsync_failure_without_old_target_leaves_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "priv"
    parent.mkdir(mode=0o700)
    target = parent / "authority"
    new = b"new-private-authority-payload"
    assert not target.exists()
    _fail_fsync_on(monkeypatch, 2)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness_mod._write_private(target, new)
    assert error.value.code == "invalid_argument"
    with pytest.raises(FileNotFoundError):
        target.lstat()
    _assert_no_write_sidecars(parent)


def test_restore_fsync_failure_keeps_old_not_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "priv"
    parent.mkdir(mode=0o700)
    target = parent / "authority"
    old = b"old-authority-bytes"
    new = b"new-private-authority-payload"
    harness_mod._write_private(target, old)
    _fail_fsync_on(monkeypatch, 2, 3)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness_mod._write_private(target, new)
    assert error.value.code == "invalid_argument"
    try:
        body = target.read_bytes()
    except FileNotFoundError:
        body = None
    assert body != new
    if body is not None:
        assert body == old
    _assert_no_write_sidecars(parent)


def test_restore_fsync_failure_without_old_target_leaves_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "priv"
    parent.mkdir(mode=0o700)
    target = parent / "authority"
    new = b"new-private-authority-payload"
    _fail_fsync_on(monkeypatch, 2, 3)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness_mod._write_private(target, new)
    assert error.value.code == "invalid_argument"
    with pytest.raises(FileNotFoundError):
        target.lstat()
    _assert_no_write_sidecars(parent)


def test_issue_capability_second_fsync_keeps_old_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    issued = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    before = _authority_snapshot(tmp_path, issued)
    _fail_fsync_on(monkeypatch, 2)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.issue_capability(
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=2,
            fence="sha256:" + "cd" * 32, session="s", pane_id="pane-1",
        )
    assert error.value.code == "invalid_argument"
    _assert_authority_unchanged(tmp_path, issued, before)
    record = json.loads(issued["capability_path"].read_bytes().decode("utf-8"))
    assert record["generation"] == 1


def _install_restore_and_unlink_faults(
    monkeypatch: pytest.MonkeyPatch, *, old_exists: bool,
) -> None:
    _fail_fsync_on(monkeypatch, 2)
    real_replace = os.replace
    seen = {"replace": 0}

    def flaky_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        seen["replace"] += 1
        if old_exists and seen["replace"] >= 2:
            raise OSError("restore replace lost")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    def boom_unlink(path: str | os.PathLike[str]) -> None:
        raise OSError("unlink lost")

    monkeypatch.setattr(os, "unlink", boom_unlink)

    def boom_path_unlink(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("unlink lost")

    monkeypatch.setattr(Path, "unlink", boom_path_unlink)


def _write_sidecars(folder: Path) -> tuple[list[Path], list[Path]]:
    tmps = [path for path in folder.glob(".*.tmp") if path.is_file() or path.is_symlink()]
    baks = [path for path in folder.glob(".*.bak") if path.is_file() or path.is_symlink()]
    return tmps, baks


def _assert_not_usable_authority(path: Path, new: bytes) -> None:
    if not path.exists():
        return
    info = path.lstat()
    usable = (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o600
    )
    if not usable:
        return
    assert path.read_bytes() != new


def test_triple_fault_old_target_raises_unknown_and_classifies_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "priv"
    parent.mkdir(mode=0o700)
    target = parent / "authority"
    old = b"old-authority-bytes"
    new = b"new-private-authority-payload"
    harness_mod._write_private(target, old)
    _install_restore_and_unlink_faults(monkeypatch, old_exists=True)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness_mod._write_private(target, new)
    assert error.value.code == "invalid_argument"
    assert error.value.unknown is True
    tmps, baks = _write_sidecars(parent)
    assert tmps == []
    assert baks != []
    _assert_not_usable_authority(target, new)


def test_triple_fault_without_old_target_raises_unknown_and_classifies_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "priv"
    parent.mkdir(mode=0o700)
    target = parent / "authority"
    new = b"new-private-authority-payload"
    _install_restore_and_unlink_faults(monkeypatch, old_exists=False)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness_mod._write_private(target, new)
    assert error.value.code == "invalid_argument"
    assert error.value.unknown is True
    tmps, baks = _write_sidecars(parent)
    assert tmps == []
    assert baks == []
    _assert_not_usable_authority(target, new)


def test_triple_fault_issue_capability_rejects_validate_old_and_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    issued = harness.issue_capability(
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1,
        fence=FENCE, session="s", pane_id="pane-1",
    )
    _install_restore_and_unlink_faults(monkeypatch, old_exists=True)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.issue_capability(
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=2,
            fence="sha256:" + "cd" * 32, session="s", pane_id="pane-1",
        )
    assert error.value.unknown is True
    cap = issued["capability_path"]
    tmps, baks = _write_sidecars(tmp_path / "caps")
    assert tmps == []
    assert baks != []
    with pytest.raises(harness_mod.HarnessError):
        harness._require_capability_record(ATTACHMENT)
    with pytest.raises(harness_mod.HarnessError):
        harness.wakeup(ATTACHMENT)
    if harness_mod._usable_private_regular(cap):
        record = json.loads(cap.read_bytes().decode("utf-8"))
        assert record.get("generation") != 2


def test_triple_fault_first_issue_then_attach_rejects_without_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, checkout, started, generic, panes = _attach_harness(tmp_path)
    _install_restore_and_unlink_faults(monkeypatch, old_exists=False)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1, fence=FENCE,
        )
    assert error.value.code == "runtime_unavailable"
    assert error.value.unknown is True
    assert started == []
    assert generic == []
    assert panes == {}
    with pytest.raises(harness_mod.HarnessError):
        harness.wakeup(ATTACHMENT)
    with pytest.raises(harness_mod.HarnessError) as again:
        harness.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1, fence=FENCE,
        )
    assert again.value.unknown is True
    assert started == []
    assert panes == {}
    tmps, _baks = _write_sidecars(tmp_path / "caps")
    assert tmps == []


def test_triple_fault_after_live_attach_does_not_start_second_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, checkout, started, generic, panes = _attach_harness(tmp_path)
    harness.attach_readonly(
        session="s", checkout_path=checkout,
        project_id=PROJECT, workspace_id=WORKSPACE,
        attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1, fence=FENCE,
    )
    assert list(panes) == ["pane-1"]
    assert len(started) == 1
    _install_restore_and_unlink_faults(monkeypatch, old_exists=True)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=2,
            fence="sha256:" + "cd" * 32,
        )
    assert error.value.unknown is True
    assert list(panes) == ["pane-1"]
    assert len(started) == 1
    assert generic == []
    with pytest.raises(harness_mod.HarnessError):
        harness.wakeup(ATTACHMENT)
    with pytest.raises(harness_mod.HarnessError):
        harness._require_capability_record(ATTACHMENT)


def test_backup_name_collision_keeps_preexisting_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "priv"
    parent.mkdir(mode=0o700)
    target = parent / "authority"
    old = b"old-authority-bytes"
    harness_mod._write_private(target, old)
    real = secrets.token_hex

    def fixed(nbytes: int = 32) -> str:
        if nbytes == 8:
            return "ab" * 8
        return real(nbytes)

    monkeypatch.setattr(secrets, "token_hex", fixed)
    preexisting = parent / ".authority.abababababababab.bak"
    preexisting.write_bytes(b"preexisting-backup")
    os.chmod(preexisting, 0o600)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness_mod._write_private(target, b"new-private-authority-payload")
    assert error.value.code == "invalid_argument"
    assert error.value.unknown is False
    assert preexisting.read_bytes() == b"preexisting-backup"
    assert target.read_bytes() == old


def test_backup_name_collision_keeps_preexisting_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "priv"
    parent.mkdir(mode=0o700)
    target = parent / "authority"
    old = b"old-authority-bytes"
    harness_mod._write_private(target, old)
    real = secrets.token_hex

    def fixed(nbytes: int = 32) -> str:
        if nbytes == 8:
            return "cd" * 8
        return real(nbytes)

    monkeypatch.setattr(secrets, "token_hex", fixed)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"link-target-bytes")
    preexisting = parent / ".authority.cdcdcdcdcdcdcdcd.bak"
    preexisting.symlink_to(outside)
    with pytest.raises(harness_mod.HarnessError):
        harness_mod._write_private(target, b"new-private-authority-payload")
    assert preexisting.is_symlink()
    assert outside.read_bytes() == b"link-target-bytes"
    assert target.read_bytes() == old


def test_backup_name_collision_keeps_preexisting_hardlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "priv"
    parent.mkdir(mode=0o700)
    target = parent / "authority"
    old = b"old-authority-bytes"
    harness_mod._write_private(target, old)
    real = secrets.token_hex

    def fixed(nbytes: int = 32) -> str:
        if nbytes == 8:
            return "ef" * 8
        return real(nbytes)

    monkeypatch.setattr(secrets, "token_hex", fixed)
    other = parent / "other"
    other.write_bytes(b"hardlink-bytes")
    os.chmod(other, 0o600)
    preexisting = parent / ".authority.efefefefefefefef.bak"
    os.link(other, preexisting)
    with pytest.raises(harness_mod.HarnessError):
        harness_mod._write_private(target, b"new-private-authority-payload")
    assert other.read_bytes() == b"hardlink-bytes"
    assert preexisting.stat().st_nlink == 2
    assert target.read_bytes() == old


def test_start_unproven_rollback_without_pane_is_unknown(tmp_path: Path) -> None:
    def fail(_kwargs, _panes):
        return {
            "available": True,
            "error_code": "descriptor_cleanup_incomplete",
            "error": "workspace launch cleanup incomplete",
            "rolled_back": False,
        }

    harness, checkout, started, generic, panes = _attach_harness(tmp_path, start=fail)
    with pytest.raises(harness_mod.HarnessError) as error:
        harness.attach_readonly(
            session="s", checkout_path=checkout,
            project_id=PROJECT, workspace_id=WORKSPACE,
            attachment_id=ATTACHMENT, identity_id=IDENTITY, generation=1, fence=FENCE,
        )
    assert error.value.code == "runtime_unavailable"
    assert error.value.unknown is True
    assert getattr(error.value, "pane_id", None) in {None, ""}
    assert generic == []
    assert started
    assert panes == {}
    assert not (tmp_path / "caps" / f"{ATTACHMENT}.cap").exists()
    with pytest.raises(harness_mod.HarnessError) as wakeup:
        harness.wakeup(ATTACHMENT)
    assert wakeup.value.code == "invalid_argument"
