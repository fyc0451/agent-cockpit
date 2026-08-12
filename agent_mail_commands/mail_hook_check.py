"""Check unread mail for the exact managed identity of the live pane."""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

from . import mail_identity_inject, mail_recv


def _verify_install(rest: list[str]) -> int:
    from . import install_verify

    deploy_root_arg: str | None = None
    index = 0
    while index < len(rest):
        if rest[index] == "--deploy-root" and index + 1 < len(rest):
            deploy_root_arg = rest[index + 1]
            index += 2
        else:
            print(f"mail-hook-check --verify-install: 未知参数: {rest[index]}", file=sys.stderr)
            return 2
    home = Path.home()
    deploy_root = Path(deploy_root_arg) if deploy_root_arg is not None else \
        install_verify.resolve_default_deploy_root(home)
    if deploy_root is None:
        print("INSTALL_VERIFY_FAIL layout_unknown: 无法定位 native 或 source 部署", file=sys.stderr)
        return 1
    findings = install_verify.verify_install(deploy_root, home=home)
    if findings:
        for finding in findings:
            print(f"INSTALL_VERIFY_FAIL {finding.code}: {finding.message}", file=sys.stderr)
        return 1
    print("INSTALL_VERIFY_OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv or [])
    if args and args[0] in {"-h", "--help"}:
        print("usage: mail-hook-check [<agent> [instance]] | --verify-install [--deploy-root PATH]")
        return 0
    if args and args[0] == "--verify-install":
        return _verify_install(args[1:])
    resolved = mail_identity_inject.resolve_managed_identity()
    if resolved is None:
        if mail_identity_inject._has_managed_descriptor_candidate():
            return 0
        selector = mail_identity_inject.legacy_selector(args)
        if selector is None:
            return 2
        resolved = mail_identity_inject.resolve_legacy_identity(*selector)
        if resolved is None:
            return 0
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            mail_recv.main([
                "--agent", resolved.agent, "--instance", resolved.instance_id,
                "--project", resolved.project, "--unread", "--peek",
            ])
    except SystemExit:
        return 0
    lines = [line for line in output.getvalue().splitlines() if line.startswith("--- #")]
    if not lines:
        return 0
    text = (
        f"[agent-mail] 你有 {len(lines)} 条未读消息（项目: {resolved.project}）：\n"
        + "\n".join(lines[:5])
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": text,
        }
    }, ensure_ascii=False))
    return 0
