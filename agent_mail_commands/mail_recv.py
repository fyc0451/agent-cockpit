#!/usr/bin/env python3
"""用本机持久化身份读取 Agent Mail 收件箱。"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
import unicodedata

from .common import helper_command, load_identity, mcp_call, mcp_tool
import coordination


def _ack_message(hub, token, identity, message_id):
    mcp_tool(hub, token, "acknowledge_message", {
        "project_key": identity["project_key"],
        "agent_name": identity["name"],
        "registration_token": identity["registration_token"],
        "message_id": int(message_id),
    })
    coordination.mark_acked(identity["project_key"], identity["name"], message_id)


def _base_command(args):
    return (
        f"{shlex.quote(helper_command('mail-recv'))} --agent {shlex.quote(args.agent)} "
        f"--instance {shlex.quote(args.instance)} --project {shlex.quote(args.project)}"
    )


_ANSI_RE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])"
)


def _terminal_text(value: object) -> str:
    """移除 ANSI/OSC 与控制字符，防收件内容操纵终端或剪贴板。"""
    text = _ANSI_RE.sub("", str(value or ""))
    return "".join(
        char
        for char in text
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--instance", default="default")
    parser.add_argument("--project", default=os.getcwd())
    parser.add_argument("--ack", action="store_true")
    parser.add_argument("--unread", action="store_true")
    parser.add_argument(
        "--peek", action="store_true",
        help="只读查看未读摘要，不创建或更新 coordination receipt",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--message", type=int, help="只 claim 指定 message_id")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--complete", type=int, metavar="MESSAGE_ID")
    actions.add_argument("--resume", type=int, metavar="MESSAGE_ID")
    actions.add_argument("--fail", type=int, metavar="MESSAGE_ID")
    actions.add_argument("--checkpoint", type=int, metavar="MESSAGE_ID")
    parser.add_argument("--reason", default="处理失败")
    parser.add_argument("--summary", default="")
    parser.add_argument("--next-step", default="")
    parser.add_argument("--in-flight", default="")
    parser.add_argument("--unsafe-in-flight", action="store_true")
    parser.add_argument("--claim-token", default="")
    args = parser.parse_args(argv)
    if args.peek and (
        args.ack
        or args.message is not None
        or any(
            value is not None
            for value in (args.complete, args.resume, args.fail, args.checkpoint)
        )
    ):
        raise SystemExit("error: --peek 不能与 claim/ack/receipt 操作混用")

    identity, hub, token = load_identity(args.agent, args.instance, args.project)
    mcp_call(hub, token, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "mail-recv", "version": "1.0"},
    })
    if args.checkpoint is not None:
        try:
            data = coordination.checkpoint_message(
                identity["project_key"], identity["name"], args.checkpoint,
                summary=args.summary, next_step=args.next_step,
                in_flight=args.in_flight, safe=not args.unsafe_in_flight,
                claim_token=args.claim_token or None,
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}")
        print(json.dumps({"checkpointed": args.checkpoint, **data}, ensure_ascii=False))
        return
    if args.fail is not None:
        try:
            failed = coordination.fail_message(
                identity["project_key"], identity["name"], args.fail, args.reason,
                claim_token=args.claim_token or None,
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}")
        if not failed:
            raise SystemExit(f"error: 消息 #{args.fail} 尚未 claim")
        print(f"failed: #{args.fail}（未 ack，可稍后重试）")
        return
    if args.resume is not None:
        result = coordination.resume_message(
            identity["project_key"], identity["name"], args.resume
        )
        print(json.dumps(result, ensure_ascii=False))
        if not result.get("resumed"):
            raise SystemExit(2)
        return
    if args.complete is not None:
        try:
            result = coordination.complete_message(
                identity["project_key"], identity["name"], args.complete,
                claim_token=args.claim_token or None,
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}")
        try:
            _ack_message(hub, token, identity, args.complete)
            acked = True
        except SystemExit as exc:
            acked = False
            print(f"warning: Hub ack 失败，已保留本地 processed 回执供重试: {exc}", file=sys.stderr)
        resume = None
        if result.get("needs_resume"):
            resume = coordination.resume_message(
                identity["project_key"], identity["name"], args.complete
            )
        print(json.dumps({
            "completed": args.complete, "acked": acked, "resume": resume,
            "instruction": (
                "已恢复原任务；先核对 checkpoint 中的不安全在途操作再继续"
                if resume and resume.get("resumed") else
                "checkpoint 仍为 uncertain；先检查外部状态并用 --checkpoint 标记安全，再 --resume"
                if resume and resume.get("reason") == "uncertain_checkpoint" else
                "stop/redirect 已生效，不恢复旧任务"
                if result.get("intent") in coordination.NO_RESUME_INTENTS else
                "消息处理完成"
            ),
        }, ensure_ascii=False))
        return
    inbox = mcp_tool(hub, token, "fetch_inbox", {
        "project_key": identity["project_key"],
        "agent_name": identity["name"],
        "registration_token": identity["registration_token"],
        "limit": max(args.limit, 100 if args.message is not None else args.limit),
        "unread_only": args.unread,
        "include_bodies": not args.peek,
    })
    messages = inbox if isinstance(inbox, list) else (inbox.get("messages") or [])
    if args.message is not None:
        messages = [m for m in messages if int(m.get("id") or -1) == args.message]
    messages.sort(key=coordination.message_timestamp)
    if not messages:
        print("(no messages)")
        return

    if args.peek:
        for message in messages:
            timestamp = coordination.message_timestamp(message)
            stamp = (
                time.strftime("%m-%d %H:%M", time.localtime(timestamp))
                if timestamp else str(message.get("created_at") or "-")
            )
            print(
                f"--- #{_terminal_text(message.get('id'))} "
                f"[{_terminal_text(message.get('thread_id') or '-')}] "
                f"{_terminal_text(message.get('from'))} @ {_terminal_text(stamp)}  "
                f"({_terminal_text(message.get('importance'))})"
            )
            print(f"    subject: {_terminal_text(message.get('subject'))}")
        return

    coordination.observe_messages(identity["project_key"], identity["name"], messages)
    delivered = []
    for message in messages:
        claim = coordination.claim_message(
            project_key=identity["project_key"], recipient=identity["name"],
            message=message, claimant=f"{identity['name']}:{os.getpid()}",
            cwd=os.getcwd(),
        )
        if not claim.get("deliver"):
            if claim.get("ack_pending"):
                try:
                    _ack_message(hub, token, identity, message["id"])
                except SystemExit as exc:
                    print(f"warning: #{message['id']} 补 ack 失败: {exc}", file=sys.stderr)
            continue
        delivered.append((message, claim))

    if not delivered:
        print("(no actionable messages; stale/processed messages were suppressed)")
        return

    base = _base_command(args)
    for message, claim in delivered:
        timestamp = coordination.message_timestamp(message)
        stamp = (
            time.strftime("%m-%d %H:%M", time.localtime(timestamp))
            if timestamp else str(message.get("created_at") or "-")
        )
        print(
            f"--- #{_terminal_text(message.get('id'))} "
            f"[{_terminal_text(message.get('thread_id') or '-')}] "
            f"{_terminal_text(message.get('from'))} @ {_terminal_text(stamp)}  "
            f"({_terminal_text(message.get('importance'))})"
        )
        print(f"    subject: {_terminal_text(message.get('subject'))}")
        body = _terminal_text(claim.get("body_md")).strip()
        if body:
            print("    " + body.replace("\n", "\n    "))
        print(
            f"    receipt: claimed intent={claim.get('intent')} "
            f"run={claim.get('run_id') or '-'} task={claim.get('task_id') or '-'} "
            f"revision={claim.get('task_revision') or '-'}"
        )
        lease = f" --claim-token {shlex.quote(str(claim['claim_token']))}"
        print(f"    checkpoint: {base} --checkpoint {message['id']}{lease} --summary \"...\" --next-step \"...\"")
        print(f"    complete: {base} --complete {message['id']}{lease}")
        print(f"    fail: {base} --fail {message['id']}{lease} --reason \"...\"")
        print()

    if args.ack:
        print("warning: --ack 会在 Agent 真正处理前确认整批消息；请改用 --complete ID", file=sys.stderr)
        acked = 0
        for message, claim in delivered:
            try:
                result = coordination.complete_message(
                    identity["project_key"], identity["name"], message["id"],
                    claim_token=claim["claim_token"],
                )
                _ack_message(hub, token, identity, message["id"])
                if result.get("needs_resume"):
                    resume = coordination.resume_message(
                        identity["project_key"], identity["name"], message["id"]
                    )
                    if not resume.get("resumed"):
                        print(
                            f"warning: #{message['id']} 未自动恢复: {resume.get('reason')}",
                            file=sys.stderr,
                        )
                acked += 1
            except (SystemExit, ValueError) as exc:
                print(f"warning: #{message['id']} ack 失败: {exc}", file=sys.stderr)
        print(f"(acked: {acked})")


if __name__ == "__main__":
    main()
