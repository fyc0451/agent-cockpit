"""Server-side orchestration for one signed maintenance-window upgrade."""

from __future__ import annotations

import os
import platform
import re
import secrets
import sys
import threading
from collections.abc import Callable, Mapping
from typing import Any

import generation_prepare
import maintenance_controller
import maintenance_ipc
import release_prepare
import upgrade_journal
import upgrade_layout
import version


ENABLE_ENV = "COCKPIT_UPGRADE_V2_ENABLED"
ENGINE = upgrade_journal.ENGINE
_REQUEST_RE = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
_ERROR_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ARCHES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
_START_LOCK = threading.Lock()


class UpgradeServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise UpgradeServiceError(code)


def _stable_code(exc: Exception, fallback: str) -> str:
    code = getattr(exc, "code", None)
    if type(code) is str and _ERROR_RE.fullmatch(code):
        return code
    return fallback


def is_enabled(*, environ: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return source.get(ENABLE_ENV) == "1"


def _layout_or_default(
    layout: upgrade_layout.UpgradeLayout | None,
) -> upgrade_layout.UpgradeLayout:
    return upgrade_layout.default_upgrade_layout() if layout is None else layout


def _active(state: str) -> bool:
    return state != "idle" and state not in upgrade_journal.TERMINAL_STAGES


def get_status(
    *, layout: upgrade_layout.UpgradeLayout | None = None,
) -> dict[str, object]:
    """Return controller capability and journal state without writes or network."""
    selected = _layout_or_default(layout)
    reason: str | None = None
    try:
        plan = upgrade_layout.build_controller_plan(selected)
        try:
            upgrade_layout.validate_controller_launcher(selected)
            upgrade_layout.load_release_public_key(selected)
        except upgrade_layout.UpgradeLayoutError as exc:
            reason = exc.code
        status = maintenance_controller.read_controller_status(plan)
    except Exception as exc:
        reason = _stable_code(exc, "layout_invalid")
        status = {"state": "idle", "journal": None}
    state = status.get("state")
    journal = status.get("journal")
    if type(state) is not str or (journal is not None and not isinstance(journal, dict)):
        state, journal, reason = "idle", None, "status_invalid"
    return {
        "active": _active(state),
        "available": reason is None,
        "engine": ENGINE,
        "journal": journal,
        "reason": reason,
        "state": state,
    }


def _target(
    *, platform_name: str | None, machine: str | None,
) -> tuple[str, str]:
    selected_platform = sys.platform if platform_name is None else platform_name
    selected_machine = platform.machine() if machine is None else machine
    if not selected_platform.startswith("linux"):
        _fail("platform_unsupported")
    arch = _ARCHES.get(selected_machine.lower())
    if arch is None:
        _fail("platform_unsupported")
    return "linux", arch


def _latest_version() -> str:
    try:
        info = version.get_version_info(refresh=True)
    except Exception:
        _fail("release_unavailable")
    if not isinstance(info, dict):
        _fail("release_unavailable")
    status = info.get("status")
    if status == "up_to_date":
        _fail("already_current")
    if status != "update_available":
        _fail("release_unavailable")
    current = info.get("current")
    latest = info.get("latest")
    if not isinstance(current, dict) or not isinstance(latest, dict):
        _fail("release_unavailable")
    current_value = current.get("version")
    latest_value = latest.get("version")
    current_parts = version.parse_semver(current_value)
    latest_parts = version.parse_semver(latest_value)
    if (
        type(current_value) is not str
        or type(latest_value) is not str
        or current_parts is None
        or latest_parts is None
        or current_value != version.format_semver(current_parts)
        or latest_value != version.format_semver(latest_parts)
    ):
        _fail("release_unavailable")
    if version.compare_semver(latest_parts, current_parts) <= 0:
        _fail("already_current")
    return latest_value


def _new_request_id() -> str:
    return f"upgrade-{secrets.token_hex(16)}"


def start_latest(
    *,
    layout: upgrade_layout.UpgradeLayout | None = None,
    request_id_factory: Callable[[], str] = _new_request_id,
    platform_name: str | None = None,
    machine: str | None = None,
) -> dict[str, object]:
    """Prepare the latest signed release, then detach the fixed controller."""
    if not _START_LOCK.acquire(blocking=False):
        _fail("upgrade_busy")
    try:
        target_platform, arch = _target(
            platform_name=platform_name, machine=machine,
        )
        selected = _layout_or_default(layout)
        plan = upgrade_layout.build_controller_plan(selected)
        controller_launcher = upgrade_layout.validate_controller_launcher(selected)
        public_key = upgrade_layout.load_release_public_key(selected)
        status = maintenance_controller.read_controller_status(plan)
        state = status.get("state")
        if type(state) is not str:
            _fail("status_invalid")
        if _active(state):
            _fail("upgrade_busy")

        target_version = _latest_version()
        request_id = request_id_factory()
        if type(request_id) is not str or _REQUEST_RE.fullmatch(request_id) is None:
            _fail("request_invalid")
        release = release_prepare.prepare_release_generation(
            f"agent-cockpit-v{target_version}",
            public_key,
            deploy_root=selected.deploy_root,
            platform=target_platform,
            arch=arch,
        )
        prepared = getattr(release, "generation", None)
        if (
            not isinstance(prepared, generation_prepare.PreparedGeneration)
            or prepared.version != target_version
        ):
            _fail("prepared_invalid")
        accepted = maintenance_ipc.spawn_maintenance_controller(
            plan=plan,
            prepared=prepared,
            request_id=request_id,
            controller_launcher=controller_launcher,
        )
        if (
            not isinstance(accepted, maintenance_ipc.ControllerAccepted)
            or not accepted.accepted
        ):
            _fail("spawn_result_invalid")
        return {
            "accepted": True,
            "pid": accepted.pid,
            "request_id": request_id,
            "target_version": target_version,
        }
    except UpgradeServiceError:
        raise
    except Exception as exc:
        _fail(_stable_code(exc, "upgrade_failed"))
    finally:
        _START_LOCK.release()


__all__ = [
    "ENABLE_ENV",
    "ENGINE",
    "UpgradeServiceError",
    "get_status",
    "is_enabled",
    "start_latest",
]
