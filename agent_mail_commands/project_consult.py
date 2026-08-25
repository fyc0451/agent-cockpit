#!/usr/bin/env python3
"""普通开发 Lead 主动领取或答复 Cockpit 本机项目咨询。"""
from __future__ import annotations

import argparse
import json
import os
from urllib.parse import quote

from .common import load_identity
from .team_work import _post


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--instance", default="default")
    parser.add_argument("--project", default=os.getcwd())
    parser.add_argument("--request-id")
    parser.add_argument("--response")
    args = parser.parse_args(argv)
    identity, _hub, _token = load_identity(args.agent, args.instance, args.project)
    local_identity = {
        "mail_project": identity["project_key"],
        "sender_name": identity["name"],
        "registration_token": identity["registration_token"],
    }
    if args.request_id or args.response:
        if not args.request_id or not args.response:
            raise SystemExit("答复咨询需要同时提供 --request-id 和 --response")
        result = _post(
            f"/api/agent/project-consult/{quote(args.request_id, safe='')}/respond",
            {**local_identity, "response": args.response},
        )
    else:
        result = _post("/api/agent/project-consult/next", local_identity)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
