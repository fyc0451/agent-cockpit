"""Resolve the immutable runtime artifact root without process context."""
from __future__ import annotations

import sys
from pathlib import Path


FROZEN_LAUNCHER_NAME = "agent-cockpit"
FROZEN_LAUNCHER_DIRECTORY = "bin"
_SOURCE_ROOT = Path(__file__).resolve().parent.parent


class ArtifactRootError(RuntimeError):
    """Stable failure raised when a frozen launcher has an invalid layout."""

    def __init__(self, reason: str = "invalid_frozen_layout") -> None:
        self.reason = reason
        super().__init__(reason)


def resolve_artifact_root() -> Path:
    """Return the source root or ``<generation>`` for the frozen launcher."""
    if not getattr(sys, "frozen", False):
        return _SOURCE_ROOT

    try:
        raw_executable = sys.executable
        if (
            not isinstance(raw_executable, str)
            or not raw_executable
            or "\x00" in raw_executable
        ):
            raise ArtifactRootError()
        executable = Path(raw_executable)
        if not executable.is_absolute():
            raise ArtifactRootError()
        executable = executable.resolve(strict=True)
        if (
            executable.name != FROZEN_LAUNCHER_NAME
            or executable.parent.name != FROZEN_LAUNCHER_DIRECTORY
            or not executable.is_file()
        ):
            raise ArtifactRootError()
        generation = executable.parent.parent
        if generation == generation.parent or not generation.is_dir():
            raise ArtifactRootError()
        return generation
    except ArtifactRootError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ArtifactRootError() from exc


__all__ = ["ArtifactRootError", "resolve_artifact_root"]
