import subprocess

import pytest

from agent_cockpit import herdr_client
from agent_cockpit import tasks


def test_herdr_timeout_becomes_runtime_error(monkeypatch):
    monkeypatch.setattr(
        herdr_client.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="herdr", timeout=kwargs["timeout"])
        ),
    )

    with pytest.raises(RuntimeError, match="超时"):
        herdr_client._run(["session", "list"], timeout=1)


def test_git_timeout_becomes_value_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tasks.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="git", timeout=kwargs["timeout"])
        ),
    )

    with pytest.raises(ValueError, match="超时"):
        tasks._git(["status", "--short"], tmp_path, timeout=1)
