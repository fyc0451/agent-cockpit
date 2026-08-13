"""Fail-closed compatibility boundary for the retired remote Inbox router.

Remote Team messages remain in the durable Human Inbox. They are never fetched
or submitted to an interactive Agent pane by this module.
"""
from __future__ import annotations

from typing import Any

from . import runtime_paths
from . import team_sessions


# Keep the legacy store path addressable for upgrade/store-schema compatibility.
# SEC-002 no longer reads or writes this state.
ROUTE_STATE = runtime_paths.store("inbox_route")
DISABLED_REASON = "remote_inbox_pane_delivery_disabled"


def _load_bindings(hub: str, human_id: int) -> list[dict[str, Any]]:
    try:
        return team_sessions.list_bindings(hub, int(human_id))
    except (OSError, TypeError, ValueError):
        return []


def _disabled_result(bound_projects: int) -> dict[str, Any]:
    return {
        "available": False,
        "reason": DISABLED_REASON,
        "fetched": 0,
        "matched": 0,
        "delivered": 0,
        "pending": 0,
        "skipped_offline": 0,
        "bound_projects": bound_projects,
    }


def route_inbox(
    authorization: str,
    *,
    hub: str,
    human_id: int,
    fetch_inbox=None,
    reply_command_for=None,
) -> dict[str, Any]:
    """Return the retired route contract without consuming remote content."""
    del authorization, fetch_inbox, reply_command_for
    bindings = _load_bindings(hub, human_id)
    bound_projects = {
        str(binding["project_slug"])
        for binding in bindings
        if binding.get("project_slug")
    }
    return _disabled_result(len(bound_projects))


def route_status(*, hub: str, human_id: int) -> dict[str, Any]:
    """Describe the disabled capability without exposing stale route state."""
    bindings = _load_bindings(hub, human_id)
    safe_bindings = []
    for binding in bindings:
        lead = binding.get("lead")
        if not isinstance(lead, dict):
            lead = {}
        safe_bindings.append({
            "project_slug": (
                binding.get("project_slug")
                if isinstance(binding.get("project_slug"), str) else None
            ),
            "session": (
                binding.get("session")
                if isinstance(binding.get("session"), str) else None
            ),
            "lead": {
                "agent": lead.get("agent") if isinstance(lead.get("agent"), str) else None,
                "mail_name": (
                    lead.get("mail_name")
                    if isinstance(lead.get("mail_name"), str) else None
                ),
            },
        })
    return {
        "available": False,
        "reason": DISABLED_REASON,
        "bindings": safe_bindings,
        "pending": [],
        "delivered_count": 0,
        "last_delivered": [],
    }
