"""为一个 canonical 项目路径注册 Cockpit 支持的 Agent Mail 身份。"""
from __future__ import annotations

import argparse
import contextlib
import io
from pathlib import Path

from agent_cockpit import next_profile

from . import am_register


AGENTS = (
    ("codex", "codex-cli", "gpt-5"),
    ("kimi", "kimi-work", "kimi"),
    ("claude", "claude-code", "unknown"),
    ("qodercn", "qoder-cn", "unknown"),
    ("opencode", "opencode", "unknown"),
    ("zcode", "zcode", "unknown"),
    ("grok", "grok", "unknown"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=str(Path.cwd()))
    parser.add_argument("--instance", default="main")
    parser.add_argument("--only", default="")
    args = parser.parse_args(argv)

    try:
        next_profile.require_helper_environment(())
        project = Path(next_profile.require_project(args.project))
    except next_profile.NextProfileError as exc:
        parser.error(str(exc))
    if not project.is_dir():
        parser.error(f"项目目录不存在：{args.project}")
    selected = set(filter(None, args.only.split(","))) if args.only else None
    print(f"项目：{project}")
    print(f"实例：{args.instance}")
    print("----------------------------------------")
    ok = skip = fail = 0
    for agent, program, model in AGENTS:
        if selected is not None and agent not in selected:
            continue
        output = io.StringIO()
        command = [
            "--agent", agent, "--instance", args.instance,
            "--project", str(project), "--program", program, "--model", model,
        ]
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                am_register.main(command)
        except SystemExit as exc:
            if exc.code not in (None, 0):
                if isinstance(exc.code, str):
                    print(exc.code, file=output)
                lines = output.getvalue().splitlines()
                print(f"  {agent:<10} ✗ 失败")
                for line in lines[:2]:
                    print(f"      {line}")
                fail += 1
                continue
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}", file=output)
            print(f"  {agent:<10} ✗ 失败")
            for line in output.getvalue().splitlines()[:2]:
                print(f"      {line}")
            fail += 1
            continue
        text = output.getvalue()
        marker = "已注册（复用）: "
        if marker in text:
            name = text.split(marker, 1)[1].split()[0]
            print(f"  {agent:<10} ↻ 复用 {name}")
            skip += 1
        else:
            marker = "注册成功: "
            name = text.split(marker, 1)[1].split()[0] if marker in text else "?"
            print(f"  {agent:<10} ✓ 注册 {name}")
            ok += 1
    print("----------------------------------------")
    print(f"完成：新注册 {ok} / 复用 {skip} / 失败 {fail}")
    print(f'收信：mail-recv --agent <agent> --instance {args.instance} --project "{project}"')
    print(f'发信：mail-send --agent <agent> --instance {args.instance} --project "{project}" --to <花名> --subject ... --body ...')
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
