#!/usr/bin/env python3
"""Compatibility entry point for the serialized release lane."""
from __future__ import annotations

import sys

from agent_cockpit import release_lane as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
