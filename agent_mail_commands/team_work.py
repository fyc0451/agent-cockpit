#!/usr/bin/env python3
"""主动领取或提交 Cockpit 本机 Team Lead 工作。"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

from .common import load_identity


def _cockpit_url() -> str:
    value = os.environ.get("COCKPIT_URL", "http://127.0.0.1:8790").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SystemExit("COCKPIT_URL 无效") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise SystemExit("COCKPIT_URL 必须是本机 loopback HTTP 地址")
    return value


def _post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{_cockpit_url()}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except (UnicodeError, ValueError, AttributeError):
            detail = None
        raise SystemExit(str(detail or f"Cockpit HTTP {exc.code}")) from exc
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"Cockpit Team Work 请求失败: {exc}") from exc
    if not isinstance(result, dict):
        raise SystemExit("Cockpit Team Work 返回格式无效")
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--instance", default="default")
    parser.add_argument("--project", default=os.getcwd())
    parser.add_argument("--work-id")
    parser.add_argument("--to", action="append", dest="mention_handles")
    parser.add_argument("--subject")
    parser.add_argument("--body")
    parser.add_argument(
        "--importance", choices=("low", "normal", "high", "urgent"),
        default="normal",
    )
    args = parser.parse_args(argv)
    identity, _hub, _token = load_identity(args.agent, args.instance, args.project)
    local_identity = {
        "mail_project": identity["project_key"],
        "sender_name": identity["name"],
        "registration_token": identity["registration_token"],
    }
    responding = any((
        args.work_id, args.mention_handles, args.subject, args.body,
    ))
    if not responding:
        result = _post("/api/agent/team-work/next", local_identity)
    else:
        if not all((args.work_id, args.mention_handles, args.subject, args.body)):
            raise SystemExit("提交回复需要同时提供 --work-id、--to、--subject、--body")
        result = _post(
            f"/api/agent/team-work/{quote(args.work_id, safe='')}/respond",
            {
                **local_identity,
                "mention_handles": args.mention_handles,
                "subject": args.subject,
                "body_md": args.body,
                "importance": args.importance,
            },
        )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
