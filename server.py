#!/usr/bin/env python3
"""Compatibility entry point for the packaged Agent Cockpit server."""
from __future__ import annotations

import os
import sys


_next_instance_lock_owner = None


if __name__ == "__main__":
    from agent_cockpit import next_profile
    from agent_cockpit.artifact_root import resolve_artifact_root

    try:
        next_profile.validate_server_environment(resolve_artifact_root())
    except next_profile.NextProfileError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    if next_profile.enabled():
        from agent_cockpit.instance_lock import LOCK_FD_ENV, InstanceLock, LockError

        try:
            raw_lock_fd = os.environ.pop(LOCK_FD_ENV, None)
            if (
                not isinstance(raw_lock_fd, str)
                or not raw_lock_fd.isdecimal()
                or raw_lock_fd != str(int(raw_lock_fd))
                or int(raw_lock_fd) <= 2
            ):
                raise LockError("lock_fd_invalid")
            _next_instance_lock_owner = InstanceLock.adopt_inherited(
                os.environ, int(raw_lock_fd),
            )
        except LockError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc

    from agent_cockpit import native_launcher

    _helper_result = native_launcher.main()
    if _helper_result is not None:
        raise SystemExit(_helper_result)

from agent_cockpit import server as _implementation

if _next_instance_lock_owner is not None:
    _implementation._next_instance_lock_owner = _next_instance_lock_owner


if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
