#!/usr/bin/env python3
"""向 Agent Cockpit 的本机 sidecar 提交结构化任务进度。"""
from __future__ import annotations

import argparse
import json
from agent_cockpit import coordination, next_profile


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="提交 Agent Cockpit 任务进度")
    parser.add_argument("--session", required=True)
    parser.add_argument("--pane", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--progress", type=int, required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--next", default="", dest="next_step")
    parser.add_argument("--blocker", default="")
    args = parser.parse_args(argv)
    try:
        next_profile.require_helper_environment(())
        session = next_profile.require_session(args.session)
        report = coordination.submit_task_report(
            session=session,
            pane_id=args.pane,
            request_id=args.request_id,
            progress=args.progress,
            summary=args.summary,
            next_step=args.next_step,
            blocker=args.blocker,
        )
    except (ValueError, next_profile.NextProfileError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "ok": True,
        "session": report["session"],
        "pane_id": report["pane_id"],
        "progress": report["progress"],
        "reported_ts": report["reported_ts"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
