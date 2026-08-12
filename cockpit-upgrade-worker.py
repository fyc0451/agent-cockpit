#!/usr/bin/env python3
"""Compatibility entry point for the retired upgrade worker."""
from agent_cockpit.upgrade_worker import main


if __name__ == "__main__":
    raise SystemExit(main())
