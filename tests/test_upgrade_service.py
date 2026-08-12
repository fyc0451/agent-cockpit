from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_cockpit import generation_prepare
from agent_cockpit import maintenance_ipc
from agent_cockpit import upgrade_layout
from agent_cockpit import upgrade_service


SOURCE_SHA = "a" * 40
ARTIFACT_DIGEST = "b" * 64


def _installed(tmp_path: Path) -> tuple[upgrade_layout.UpgradeLayout, bytes]:
    layout = upgrade_layout.default_upgrade_layout(home=tmp_path)
    layout.deploy_root.mkdir(parents=True, mode=0o700)
    layout.controller_launcher.parent.mkdir(parents=True, mode=0o700)
    layout.controller_launcher.write_bytes(b"\x7fELFcontroller")
    layout.controller_launcher.chmod(0o700)
    key = bytes(range(32))
    layout.public_key_path.write_bytes(key)
    layout.public_key_path.chmod(0o600)
    return layout, key


def _prepared(layout: upgrade_layout.UpgradeLayout) -> generation_prepare.PreparedGeneration:
    generation_id = f"{SOURCE_SHA}-{ARTIFACT_DIGEST}"
    root = layout.deploy_root / "generations" / generation_id
    return generation_prepare.PreparedGeneration(
        version="0.3.0",
        source_sha=SOURCE_SHA,
        artifact_digest=ARTIFACT_DIGEST,
        generation_id=generation_id,
        generation_path=root,
        launcher_path=root / "bin/agent-cockpit",
    )


def test_feature_gate_requires_exact_enabled_value() -> None:
    for value, expected in ((None, False), ("", False), ("true", False), ("1", True)):
        environ = {} if value is None else {upgrade_service.ENABLE_ENV: value}
        assert upgrade_service.is_enabled(environ=environ) is expected


def test_status_is_pure_read_and_reports_unavailable_layout(tmp_path: Path) -> None:
    layout = upgrade_layout.default_upgrade_layout(home=tmp_path)
    before = tuple(tmp_path.iterdir())

    assert upgrade_service.get_status(layout=layout) == {
        "active": False,
        "available": False,
        "engine": upgrade_service.ENGINE,
        "journal": None,
        "reason": "controller_unavailable",
        "state": "idle",
    }
    assert tuple(tmp_path.iterdir()) == before


def test_status_reads_controller_journal_without_network_or_writes(
    tmp_path: Path,
) -> None:
    layout, _key = _installed(tmp_path)
    before = tuple(layout.controller_root.rglob("*"))

    assert upgrade_service.get_status(layout=layout) == {
        "active": False,
        "available": True,
        "engine": upgrade_service.ENGINE,
        "journal": None,
        "reason": None,
        "state": "idle",
    }
    assert tuple(layout.controller_root.rglob("*")) == before
    assert not layout.state_root.exists()


def test_start_latest_prepares_signed_generation_and_spawns_exact_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, key = _installed(tmp_path)
    prepared = _prepared(layout)
    calls: list[tuple[Any, ...]] = []

    monkeypatch.setattr(
        upgrade_service.version,
        "get_version_info",
        lambda **kwargs: (
            calls.append(("version", kwargs))
            or {
                "status": "update_available",
                "current": {"version": "0.2.0"},
                "latest": {"version": "0.3.0"},
            }
        ),
    )

    def prepare(tag: str, public_key: bytes, **kwargs: Any):
        calls.append(("prepare", tag, public_key, kwargs))
        return type("Release", (), {"generation": prepared})()

    def spawn(**kwargs: Any):
        calls.append(("spawn", kwargs))
        return maintenance_ipc.ControllerAccepted(pid=4321, accepted=True)

    monkeypatch.setattr(upgrade_service.release_prepare, "prepare_release_generation", prepare)
    monkeypatch.setattr(upgrade_service.maintenance_ipc, "spawn_maintenance_controller", spawn)

    result = upgrade_service.start_latest(
        layout=layout,
        request_id_factory=lambda: "request-1",
        platform_name="linux",
        machine="x86_64",
    )

    assert result == {
        "accepted": True,
        "pid": 4321,
        "request_id": "request-1",
        "target_version": "0.3.0",
    }
    assert calls[0] == ("version", {"refresh": True})
    assert calls[1] == (
        "prepare",
        "agent-cockpit-v0.3.0",
        key,
        {
            "deploy_root": layout.deploy_root,
            "platform": "linux",
            "arch": "x86_64",
        },
    )
    assert calls[2][0] == "spawn"
    assert calls[2][1] == {
        "plan": upgrade_layout.build_controller_plan(layout),
        "prepared": prepared,
        "request_id": "request-1",
        "controller_launcher": layout.controller_launcher,
    }


@pytest.mark.parametrize(
    ("info", "code"),
    [
        ({"status": "unavailable", "latest": None}, "release_unavailable"),
        (
            {
                "status": "up_to_date",
                "current": {"version": "0.2.0"},
                "latest": {"version": "0.2.0"},
            },
            "already_current",
        ),
        (
            {"status": "update_available", "latest": {"version": "bad"}},
            "release_unavailable",
        ),
    ],
)
def test_start_rejects_missing_or_non_new_release_before_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    info: dict[str, object],
    code: str,
) -> None:
    layout, _key = _installed(tmp_path)
    monkeypatch.setattr(
        upgrade_service.version, "get_version_info", lambda **_kwargs: info,
    )
    monkeypatch.setattr(
        upgrade_service.release_prepare,
        "prepare_release_generation",
        lambda *_args, **_kwargs: pytest.fail("prepare must not run"),
    )

    with pytest.raises(upgrade_service.UpgradeServiceError) as exc:
        upgrade_service.start_latest(
            layout=layout,
            request_id_factory=lambda: "request-1",
            platform_name="linux",
            machine="x86_64",
        )

    assert exc.value.code == code


@pytest.mark.parametrize(
    ("platform_name", "machine", "code"),
    [
        ("darwin", "x86_64", "platform_unsupported"),
        ("linux", "mips64", "platform_unsupported"),
    ],
)
def test_start_rejects_unsupported_target_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    machine: str,
    code: str,
) -> None:
    layout, _key = _installed(tmp_path)
    monkeypatch.setattr(
        upgrade_service.version,
        "get_version_info",
        lambda **_kwargs: pytest.fail("network must not run"),
    )

    with pytest.raises(upgrade_service.UpgradeServiceError) as exc:
        upgrade_service.start_latest(
            layout=layout,
            platform_name=platform_name,
            machine=machine,
        )
    assert exc.value.code == code


def test_start_is_single_flight(tmp_path: Path) -> None:
    layout, _key = _installed(tmp_path)
    assert upgrade_service._START_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(upgrade_service.UpgradeServiceError) as exc:
            upgrade_service.start_latest(layout=layout)
        assert exc.value.code == "upgrade_busy"
    finally:
        upgrade_service._START_LOCK.release()
