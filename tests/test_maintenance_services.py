from __future__ import annotations

import inspect
import subprocess
from collections.abc import Callable

import pytest

import maintenance_services as services


Result = subprocess.CompletedProcess[str]


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
    ("failure", "code"),
    [
        (subprocess.TimeoutExpired(cmd="fixed", timeout=10), "service_timeout"),
        (OSError("private details"), "service_runner_error"),
    ],
)
def test_runner_failures_are_stable_and_do_not_leak(
    failure: Exception, code: str
) -> None:
    def runner(_argv: list[str], **_kwargs: object) -> Result:
        raise failure

    with pytest.raises(services.ServiceMutationError) as exc:
        services.stop_services(runner=runner)
    assert (exc.value.code, exc.value.completed, str(exc.value)) == (code, (), code)
    assert "private" not in str(exc.value)


def test_timeout_after_one_verified_unit_preserves_partial_completion() -> None:
    calls = 0

    def runner(argv: list[str], **_kwargs: object) -> Result:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=10)
        return _result(argv, stdout="inactive\n" if "show" in argv else "")

    with pytest.raises(services.ServiceMutationError) as exc:
        services.stop_services(runner=runner)
    assert (exc.value.code, exc.value.completed) == (
        "service_timeout",
        (services.COCKPIT_UNIT,),
    )


@pytest.mark.parametrize(
    ("failure_call", "completed"),
    [(2, ()), (3, (services.COCKPIT_UNIT,))],
)
def test_nonzero_preserves_only_verified_completed_units(
    failure_call: int, completed: tuple[str, ...]
) -> None:
    calls = 0

    def runner(argv: list[str], **_kwargs: object) -> Result:
        nonlocal calls
        calls += 1
        return _result(
            argv,
            returncode=1 if calls == failure_call else 0,
            stdout="inactive\n" if "show" in argv else "",
        )

    with pytest.raises(services.ServiceMutationError) as exc:
        services.stop_services(runner=runner)
    assert exc.value.code == "service_nonzero"
    assert exc.value.completed == completed
    assert str(exc.value) == "service_nonzero"


def test_abnormal_state_is_not_counted_as_completed() -> None:
    calls = 0

    def runner(argv: list[str], **_kwargs: object) -> Result:
        nonlocal calls
        calls += 1
        state = "inactive" if calls == 2 else "failed"
        return _result(argv, stdout=f"{state}\n" if "show" in argv else "")

    with pytest.raises(services.ServiceMutationError) as exc:
        services.stop_services(runner=runner)
    assert exc.value.code == "service_state_mismatch"
    assert exc.value.completed == (services.COCKPIT_UNIT,)


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
    assert (exc.value.code, exc.value.completed) == ("service_request_invalid", ())
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
    assert (exc.value.code, exc.value.completed) == (
        "service_platform_unsupported",
        (),
    )
