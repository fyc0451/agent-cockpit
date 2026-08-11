"""Fixed Linux service mutations for the short maintenance window."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence

SYSTEMCTL = "/usr/bin/systemctl"
COCKPIT_UNIT = "agent-cockpit.service"
MAIL_UNIT = "agent-mail.service"
STOP_ORDER = (COCKPIT_UNIT, MAIL_UNIT)
START_ORDER = (MAIL_UNIT, COCKPIT_UNIT)
COMMAND_TIMEOUT_SECONDS = 10

Runner = Callable[..., subprocess.CompletedProcess[str]]


class ServiceMutationError(RuntimeError):
    def __init__(self, code: str, completed: Sequence[str] = ()) -> None:
        self.code = code
        self.completed = tuple(completed)
        super().__init__(code)


def _fail(code: str, completed: Sequence[str]) -> None:
    raise ServiceMutationError(code, completed)


def _run(
    argv: list[str], runner: Runner, completed: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(
            argv,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        _fail("service_timeout", completed)
    except Exception:
        _fail("service_runner_error", completed)
    if type(getattr(result, "returncode", None)) is not int:
        _fail("service_runner_error", completed)
    if result.returncode != 0:
        _fail("service_nonzero", completed)
    return result


def _mutate(
    action: str,
    units: tuple[str, ...],
    *,
    runner: Runner | None = None,
) -> tuple[str, ...]:
    expected_order = STOP_ORDER if action == "stop" else START_ORDER
    if (
        type(action) is not str
        or type(units) is not tuple
        or not all(type(unit) is str for unit in units)
        or action not in {"stop", "start"}
        or units != expected_order
    ):
        _fail("service_request_invalid", ())
    if not sys.platform.startswith("linux"):
        _fail("service_platform_unsupported", ())
    execute = subprocess.run if runner is None else runner
    completed: list[str] = []
    expected_state = "inactive" if action == "stop" else "active"
    for unit in units:
        _run([SYSTEMCTL, "--user", action, unit], execute, completed)
        status = _run(
            [SYSTEMCTL, "--user", "show", unit, "--property=ActiveState", "--value"],
            execute,
            completed,
        )
        if type(status.stdout) is not str or status.stdout.strip() != expected_state:
            _fail("service_state_mismatch", completed)
        completed.append(unit)
    return tuple(completed)


def stop_services(*, runner: Runner | None = None) -> tuple[str, ...]:
    """Stop Cockpit then Agent Mail and verify each unit is inactive."""
    return _mutate("stop", STOP_ORDER, runner=runner)


def start_services(*, runner: Runner | None = None) -> tuple[str, ...]:
    """Start Agent Mail then Cockpit and verify each unit is active."""
    return _mutate("start", START_ORDER, runner=runner)


__all__ = ["ServiceMutationError", "start_services", "stop_services"]
