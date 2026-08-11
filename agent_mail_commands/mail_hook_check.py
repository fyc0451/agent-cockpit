"""Check unread mail for the exact managed identity of the live pane."""
from __future__ import annotations

import contextlib
import io
import json

from . import mail_identity_inject, mail_recv


def main(argv: list[str] | None = None) -> int:
    if argv:
        return 2
    resolved = mail_identity_inject.resolve_managed_identity()
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
