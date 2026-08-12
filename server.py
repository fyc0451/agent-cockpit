#!/usr/bin/env python3
"""Compatibility entry point for the packaged Agent Cockpit server."""
from __future__ import annotations

import sys


if __name__ == "__main__":
    from agent_cockpit import next_profile
    from agent_cockpit.artifact_root import resolve_artifact_root

    try:
        next_profile.validate_server_environment(resolve_artifact_root())
    except next_profile.NextProfileError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    from agent_cockpit import native_launcher

    _helper_result = native_launcher.main()
    if _helper_result is not None:
        raise SystemExit(_helper_result)

from agent_cockpit import server as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
