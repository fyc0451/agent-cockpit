#!/usr/bin/env python3
"""Compatibility entry point for source-to-native migration."""
from __future__ import annotations

import sys

from agent_cockpit import source_native_migrate as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
