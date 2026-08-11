"""Composable fixed command dispatcher for the native launcher."""
from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Sequence

import native_helper_install


HELPER_COMMANDS = {
    "am-register": "agent_mail_commands.am_register",
    "am-retire": "agent_mail_commands.am_retire",
    "am-init-project": "agent_mail_commands.am_init_project",
    "mail-send": "agent_mail_commands.mail_send",
    "mail-recv": "agent_mail_commands.mail_recv",
    "mail-identity-inject": "agent_mail_commands.mail_identity_inject",
    "task-report": "agent_mail_commands.task_report",
}


def dispatch_helper(command: str, argv: Sequence[str]) -> int:
    module = importlib.import_module(HELPER_COMMANDS[command])
    result = module.main(list(argv))
    return 0 if result is None else int(result)


def dispatch_schema_probe(argv: Sequence[str]) -> int:
    module = importlib.import_module("release_readiness")
    result = module.main(list(argv))
    return 0 if result is None else int(result)


def dispatch_maintenance_controller(argv: Sequence[str]) -> int:
    module = importlib.import_module("maintenance_cli")
    result = module.main(list(argv))
    return 0 if result is None else int(result)


def _usage() -> None:
    commands = "|".join(HELPER_COMMANDS)
    print(f"usage: agent-cockpit helper <{commands}> [args ...]", file=sys.stderr)


def main(
    argv: Sequence[str] | None = None, *, program: str | None = None,
) -> int | None:
    args = list(sys.argv[1:] if argv is None else argv)
    basename = os.path.basename(sys.argv[0] if program is None else program)
    if basename in HELPER_COMMANDS:
        return dispatch_helper(basename, args)
    if args and args[0] == "install-helpers":
        return native_helper_install.main(args[1:])
    if args and args[0] == "helper":
        if len(args) >= 2 and args[1] in HELPER_COMMANDS:
            return dispatch_helper(args[1], args[2:])
        _usage()
        return 2
    if args and args[0] == "schema-probe":
        return dispatch_schema_probe(args[1:])
    if args and args[0] == "maintenance-controller":
        return dispatch_maintenance_controller(args[1:])
    return None
