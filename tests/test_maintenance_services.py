from __future__ import annotations

import inspect
import subprocess
from collections.abc import Callable

import pytest

import maintenance_services as services


Result = subprocess.CompletedProcess[str]


@pytest.fixture(autouse=True)
def _pin_linux_platform_for_service_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """纯 fake 路径在 macOS runner 上也应走 Linux 契约；darwin 拒绝用例自行改 platform。"""
    monkeypatch.setattr(services.sys, "platform", "linux")


def _result(argv: list[str], returncode: int = 0, stdout: str = "") -> Result:
    return subprocess.CompletedProcess(argv, returncode, stdout, "secret stderr")


@pytest.mark.parametrize(
    ("operation", "action", "order", "state"),
    [
        (services.stop_services, "stop", services.STOP_ORDER, "inactive"),
        (services.start_services, "start", services.START_ORDER, "active"),
    ],
)
def test_fixed_order_argv_timeout_and_state_verification(
    operation: Callable[..., tuple[str, ...]],
    action: str,
    order: tuple[str, ...],
    state: str,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv: list[str], **kwargs: object) -> Result:
        calls.append((argv, kwargs))
        return _result(argv, stdout=f"{state}\n" if "show" in argv else "")

    assert operation(runner=runner) == order
    assert [argv for argv, _ in calls] == [
        [services.SYSTEMCTL, "--user", action, order[0]],
        [services.SYSTEMCTL, "--user", "show", order[0], "--property=ActiveState", "--value"],
        [services.SYSTEMCTL, "--user", action, order[1]],
        [services.SYSTEMCTL, "--user", "show", order[1], "--property=ActiveState", "--value"],
    ]
    assert all(
        kwargs
        == {
            "capture_output": True,
            "text": True,
            "check": False,
            "shell": False,
            "timeout": services.COMMAND_TIMEOUT_SECONDS,
        }
        for _, kwargs in calls
    )


@pytest.mark.parametrize(
    ("failure_call", "failure", "code", "completed", "affected"),
    [
        (1, "timeout", "service_timeout", (), (services.COCKPIT_UNIT,)),
        (1, "nonzero", "service_nonzero", (), (services.COCKPIT_UNIT,)),
        (1, "exception", "service_runner_error", (), (services.COCKPIT_UNIT,)),
        (3, "timeout", "service_timeout", (services.COCKPIT_UNIT,), services.STOP_ORDER),
        (3, "nonzero", "service_nonzero", (services.COCKPIT_UNIT,), services.STOP_ORDER),
        (3, "exception", "service_runner_error", (services.COCKPIT_UNIT,), services.STOP_ORDER),
        (2, "timeout", "service_timeout", (), (services.COCKPIT_UNIT,)),
        (2, "nonzero", "service_nonzero", (), (services.COCKPIT_UNIT,)),
        (2, "mismatch", "service_state_mismatch", (), (services.COCKPIT_UNIT,)),
        (4, "timeout", "service_timeout", (services.COCKPIT_UNIT,), services.STOP_ORDER),
        (4, "nonzero", "service_nonzero", (services.COCKPIT_UNIT,), services.STOP_ORDER),
        (4, "mismatch", "service_state_mismatch", (services.COCKPIT_UNIT,), services.STOP_ORDER),
    ],
)
def test_failures_track_verified_completed_and_possibly_changed_affected(
    failure_call: int,
    failure: str,
    code: str,
    completed: tuple[str, ...],
    affected: tuple[str, ...],
) -> None:
    calls = 0

    def runner(argv: list[str], **_kwargs: object) -> Result:
        nonlocal calls
        calls += 1
        if calls == failure_call and failure == "timeout":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=10)
        if calls == failure_call and failure == "exception":
            raise OSError("private details")
        return _result(
            argv,
            returncode=1 if calls == failure_call and failure == "nonzero" else 0,
            stdout=(
                "failed\n"
                if calls == failure_call and failure == "mismatch"
                else "inactive\n" if "show" in argv else ""
            ),
        )

    with pytest.raises(services.ServiceMutationError) as exc:
        services.stop_services(runner=runner)
    assert (exc.value.code, exc.value.completed, exc.value.affected) == (
        code,
        completed,
        affected,
    )
    assert str(exc.value) == code
    assert "private" not in str(exc.value)


def test_start_failure_affected_follows_mail_then_cockpit_order() -> None:
    calls = 0

    def runner(argv: list[str], **_kwargs: object) -> Result:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=10)
        return _result(argv, stdout="active\n" if "show" in argv else "")

    with pytest.raises(services.ServiceMutationError) as exc:
        services.start_services(runner=runner)
    assert (exc.value.completed, exc.value.affected) == (
        (services.MAIL_UNIT,),
        services.START_ORDER,
    )


@pytest.mark.parametrize(
    ("action", "units"),
    [
        ("restart", services.STOP_ORDER),
        ("stop", ("attacker.service", services.MAIL_UNIT)),
        ("start", tuple(reversed(services.START_ORDER))),
    ],
)
def test_arbitrary_action_unit_or_order_is_rejected_without_execution(
    action: str, units: tuple[str, ...]
) -> None:
    called = False

    def runner(argv: list[str], **_kwargs: object) -> Result:
        nonlocal called
        called = True
        return _result(argv)

    with pytest.raises(services.ServiceMutationError) as exc:
        services._mutate(action, units, runner=runner)
    assert (exc.value.code, exc.value.completed, exc.value.affected) == (
        "service_request_invalid",
        (),
        (),
    )
    assert not called


def test_public_api_has_no_argv_unit_or_timeout_injection() -> None:
    stop_parameter = inspect.signature(services.stop_services).parameters["runner"]
    start_parameter = inspect.signature(services.start_services).parameters["runner"]
    assert stop_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert start_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    source = inspect.getsource(services)
    assert source.count("agent-cockpit.service") == 1
    assert source.count("agent-mail.service") == 1
    assert "herdr" not in source.lower()


def test_default_runner_uses_the_same_fixed_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> Result:
        calls.append(argv)
        return _result(argv, stdout="inactive\n" if "show" in argv else "")

    monkeypatch.setattr(services.subprocess, "run", runner)
    assert services.stop_services() == services.STOP_ORDER
    assert all(argv[0] == "/usr/bin/systemctl" for argv in calls)


def test_non_linux_platform_is_rejected_without_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(services.sys, "platform", "darwin")

    def runner(_argv: list[str], **_kwargs: object) -> Result:
        raise AssertionError("runner must not execute")

    with pytest.raises(services.ServiceMutationError) as exc:
        services.stop_services(runner=runner)
    assert (exc.value.code, exc.value.completed, exc.value.affected) == (
        "service_platform_unsupported",
        (),
        (),
    )
